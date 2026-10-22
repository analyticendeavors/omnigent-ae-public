"""Save the Triage card's notes for lines counted before the observer saved them.

Since 2026-10-19 the observer saves each ``Absorbed:`` and ``UNFILED`` line it
counts as a label of its own (:func:`ae_omni_policies.labels.note_key`). A line
counted before then has only its count, so the session rail's Triage card shows
it only while its message is loaded, and says "The lines are in older messages
not loaded here." once it is not. :func:`backfill` reads the history of every
session the caller owns (archived and sub-agent sessions included; the list
route leaves side chats out) through the server's API, finds the lines the way
the observer does, and writes the notes that are missing.

It writes a counter's notes only when the history holds exactly as many of its
lines as the counter says; otherwise it cannot tell which lines were counted,
so it leaves that counter alone and reports it. It never changes a count or a
note already saved, so running it twice writes nothing the second time, and a
session it cannot read or write is reported without stopping the run.

Run it once after the deploy that carries the observer change:
``just triage-notes-backfill <server url>`` (``scripts/backfill-triage-notes.py``).
Identity is the token ``omnigent login <url>`` stored, or ``OMNIGENT_TOKEN``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from ae_omni_policies import labels
from ae_omni_policies.observer import ABSORBED_LINE, UNFILED_LINE, note_text
from ae_omni_policies.omni import SERVER_URL_ENV, TOKEN_ENV, Settings, http_client, stored_token

SESSION_PAGE = 100
# The items route's largest page.
ITEM_PAGE = 1000
# Each counter with the pattern and the token its notes fall back to.
COUNTERS = (
    (labels.ABSORBED, ABSORBED_LINE, "Absorbed"),
    (labels.UNFILED, UNFILED_LINE, "UNFILED"),
)


class BackfillError(RuntimeError):
    """The server refused a read; the message says which and why."""


def _unsaved(current: Mapping[str, str], counter: str) -> list[int]:
    """The note numbers *counter* counts, up to the cap, that hold no saved note."""
    top = min(labels.count(current, counter), labels.NOTE_CAP)
    return [
        number
        for number in range(1, top + 1)
        if labels.label(current, labels.note_key(counter, number)) is None
    ]


def missing_notes(
    current: Mapping[str, str], found: Mapping[str, Sequence[str]]
) -> tuple[dict[str, str], list[str]]:
    """The note labels a session lacks, from the lines its history holds.

    Pure. Note ``n`` of a counter is the ``n``-th line of that kind in the
    session's history, oldest first, the order the observer counted them in. A
    counter whose history holds a different number of lines than it counts is
    left alone: which lines it counted cannot be told apart.

    :param current: The session's labels.
    :param found: Each counter's lines (the text after the token), oldest first,
        e.g. ``{"ae.absorbed": ["rename the field, blocks the build"]}``.
    :returns: The ``{key: note}`` writes for ``PATCH /v1/sessions/{id}``, and a
        sentence for each counter left alone.
    """
    updates: dict[str, str] = {}
    skipped: list[str] = []
    for counter, _pattern, token in COUNTERS:
        numbers = _unsaved(current, counter)
        if not numbers:
            continue
        lines = found.get(counter, ())
        counted = labels.count(current, counter)
        if len(lines) != counted:
            skipped.append(
                f"{counter} counts {counted} but the history holds {len(lines)} such lines"
            )
            continue
        for number in numbers:
            updates[labels.note_key(counter, number)] = note_text(lines[number - 1], token)
    return updates, skipped


def _item_text(item: Mapping[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    parts = [
        part["text"]
        for part in content or []
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return "\n".join(parts)


async def _get(http: httpx.AsyncClient, path: str, params: dict[str, str]) -> dict[str, Any]:
    response = await http.get(path, params=params)
    if response.status_code != 200:
        raise BackfillError(
            f"GET {path} answered {response.status_code}: {response.text[:200]}. "
            "Check the server URL and that `omnigent login` has a token for it."
        )
    body = response.json()
    return body if isinstance(body, dict) else {}


async def _pages(http: httpx.AsyncClient, path: str, params: dict[str, str]) -> list[dict]:
    rows: list[dict] = []
    after: str | None = None
    while True:
        page = await _get(http, path, {**params, **({"after": after} if after else {})})
        data = [row for row in page.get("data") or [] if isinstance(row, dict)]
        rows.extend(data)
        # Follow the cursor, not the row count: the session list leaves side chats
        # out after paging, so a page can come back empty with more to come.
        cursor = page.get("last_id") or (data[-1].get("id") if data else None)
        if not page.get("has_more") or not cursor or cursor == after:
            return rows
        after = cursor


async def history_lines(http: httpx.AsyncClient, session_id: str) -> dict[str, list[str]]:
    """Each counter's lines in the session's assistant messages, oldest first.

    :param http: A client for the server (:func:`ae_omni_policies.omni.http_client`).
    :param session_id: The session to read.
    :returns: ``{counter: [line text, ...]}`` for both counters.
    :raises BackfillError: When the server refuses the read.
    """
    found: dict[str, list[str]] = {counter: [] for counter, _pattern, _token in COUNTERS}
    items = await _pages(
        http, f"/v1/sessions/{session_id}/items", {"limit": str(ITEM_PAGE), "order": "asc"}
    )
    for item in items:
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        text = _item_text(item)
        for counter, pattern, _token in COUNTERS:
            found[counter].extend(pattern.findall(text))
    return found


async def backfill(http: httpx.AsyncClient, *, dry_run: bool = False) -> list[dict[str, Any]]:
    """Write the missing notes on every session the caller owns that lacks some.

    :param http: A client for the server.
    :param dry_run: Report what would be written without writing it.
    :returns: One row per session that lacked notes: id, title, the keys written
        (or that a dry run would write), the counters left alone and why, and
        ``error`` when the session could not be read or written.
    :raises BackfillError: When the server refuses to list the sessions.
    """
    sessions = await _pages(
        http,
        "/v1/sessions",
        {
            "limit": str(SESSION_PAGE),
            "include_archived": "true",
            "kind": "any",
            "visibility": "mine",
        },
    )
    results: list[dict[str, Any]] = []
    for session in sessions:
        current = session.get("labels") or {}
        if not any(_unsaved(current, counter) for counter, _pattern, _token in COUNTERS):
            continue
        session_id = str(session.get("id"))
        row: dict[str, Any] = {"id": session_id, "title": session.get("title") or ""}
        try:
            updates, skipped = missing_notes(current, await history_lines(http, session_id))
        except BackfillError as exc:
            results.append({**row, "written": [], "skipped": [], "error": str(exc)})
            continue
        row.update({"written": sorted(updates), "skipped": skipped})
        if updates and not dry_run:
            response = await http.patch(f"/v1/sessions/{session_id}", json={"labels": updates})
            if response.status_code != 200:
                row["written"] = []
                row["error"] = (
                    f"PATCH /v1/sessions/{session_id} answered {response.status_code}: "
                    f"{response.text[:200]}"
                )
        results.append(row)
    return results


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backfill-triage-notes",
        description=(
            "Save the Triage card's note for every Absorbed: and UNFILED line a session "
            "counted before the observer saved notes (2026-10-19). Identity: the token "
            f"`omnigent login <url>` stored, or {TOKEN_ENV}."
        ),
    )
    parser.add_argument(
        "server_url",
        nargs="?",
        default=os.environ.get(SERVER_URL_ENV, ""),
        help=f"The server, e.g. https://ae-llm-01.example.ts.net:8000 (default: {SERVER_URL_ENV})",
    )
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """The script's entry point; prints one line per session, 1 when any session failed."""
    return asyncio.run(amain(argv))


async def amain(argv: list[str] | None = None) -> int:
    """:func:`main` without the event loop, for a caller that already runs one."""
    args = _parse_args(argv)
    url = str(args.server_url).strip().rstrip("/")
    if not url:
        print(
            f"backfill-triage-notes: give the server URL, or set {SERVER_URL_ENV}.",
            file=sys.stderr,
        )
        return 2
    token = (os.environ.get(TOKEN_ENV) or "").strip() or stored_token(url)
    settings = Settings(server_url=url, token=token)
    try:
        async with http_client(settings) as http:
            results = await backfill(http, dry_run=args.dry_run)
    except (BackfillError, httpx.HTTPError) as exc:
        print(f"backfill-triage-notes: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(results, indent=2))
    elif not results:
        print("no session lacks a note")
    verb = "would write" if args.dry_run else "wrote"
    for row in [] if args.json else results:
        name = f"{row['id']}  {row['title']}"
        if row.get("error"):
            print(f"failed on {name}: {row['error']}. Rerun once the cause is fixed.")
        elif row["written"]:
            print(f"{verb} {len(row['written'])} note(s) on {name}")
        for reason in row["skipped"]:
            print(f"left alone on {name}: {reason}")
    return 1 if any(row.get("error") for row in results) else 0


__all__ = [
    "COUNTERS",
    "ITEM_PAGE",
    "SESSION_PAGE",
    "BackfillError",
    "amain",
    "backfill",
    "history_lines",
    "main",
    "missing_notes",
]
