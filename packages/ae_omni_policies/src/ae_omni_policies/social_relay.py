"""The box relay for the Social card: reads the YouTube uploads and posts them (2026-11-06).

``python -m ae_omni_policies.social_relay`` runs the ae-claude-skills lister
(``skills/youtube-video-publish/scripts/list-uploads.js``, ``AE_SOCIAL_LISTER`` overrides the
path), keeps only the contract's fields and posts them to ``/v1/ae/social/uploads`` with the
admin's stored login, the way :mod:`.secrets_relay` posts the vault reading. The server never
holds the YouTube login; the box does, so the box reads.

``--dry-run`` prints what it would post; ``--from-file FILE`` reads a saved lister output
instead of running node, and with ``--dry-run`` also plans and scores it locally. ``week``
prints ``GET /social/week`` (the open posts that still need a draft) and ``put-drafts FILE``
posts a drafts file to ``/social/drafts``, both for the weekly drafting run. No token is ever
printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from ae_omni_policies import omni, social

LISTER_ENV = "AE_SOCIAL_LISTER"
DEFAULT_LISTER = (
    Path.home() / "src/ae-claude-skills/skills/youtube-video-publish/scripts/list-uploads.js"
)
LISTER_TIMEOUT_S = 120


class RelayError(Exception):
    """A step the relay could not finish; the message says what to do."""


def run_lister(script: Path, *, runner: Any = subprocess.run) -> dict[str, Any]:
    """The lister's JSON. Exit 0 (some channel read) and 1 (all failed) both count.

    :raises RelayError: When node or the script is missing, it times out, or prints no JSON.
    """
    node = shutil.which("node")
    if node is None:
        raise RelayError("no node on PATH; the box keeps it in ~/.local/node/bin")
    if not script.is_file():
        raise RelayError(f"the lister {script} is missing; pull ae-claude-skills on the box")
    skill_dir = script.resolve().parent.parent
    try:
        done = runner(
            [node, str(script)],
            cwd=str(skill_dir),
            capture_output=True,
            text=True,
            timeout=LISTER_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RelayError(f"the lister took over {LISTER_TIMEOUT_S} s and was stopped") from exc
    except OSError as exc:
        raise RelayError(f"could not run the lister: {exc}") from exc
    if done.returncode not in (0, 1):
        raise RelayError(f"the lister exited {done.returncode}: {(done.stderr or '')[:200]}")
    return _parse(done.stdout, "the lister")


def _parse(text: str, what: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RelayError(f"{what} did not print JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise RelayError(f"{what} printed something other than a JSON object")
    return data


def build_body(data: dict[str, Any]) -> social.UploadsPost:
    """Keep only the contract's fields, validated the way the server will validate them."""
    try:
        return social.UploadsPost.model_validate(data)
    except ValueError as exc:
        raise RelayError(f"the lister's output is not in the contract's shape: {exc}") from exc


def dry_report(body: social.UploadsPost, now: Any = None) -> dict[str, Any]:
    """Plan and score the reading locally on an empty state: counts the lead checks by eye."""
    now = now or social.utcnow()
    state = social.State()
    social.apply_uploads(state, body, now)
    planned = social.plan(state, now)
    scored = social.score(planned, now)
    by_kind: dict[str, int] = {}
    for slot in planned.slots.values():
        by_kind[slot.kind] = by_kind.get(slot.kind, 0) + 1
    return {
        "items": {ch: len(reading.items) for ch, reading in planned.uploads.items()},
        "slots": len(planned.slots),
        "slots_by_kind": by_kind,
        "gaps": social.gaps(planned, now),
        "score": {k: v for k, v in scored.items() if k != "weeks"},
    }


async def _call(
    client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    try:
        response = await client.request(method, path, json=body)
    except httpx.HTTPError as exc:
        raise RelayError(f"{method} {path} failed: {exc}") from exc
    if response.status_code == 401:
        raise RelayError(
            f"{method} {path} answered 401: the stored login expired; run `omnigent login "
            f"{client.base_url}` as this user"
        )
    if response.status_code >= 300:
        raise RelayError(f"{method} {path} answered {response.status_code}: {response.text[:200]}")
    try:
        return dict(response.json())
    except ValueError as exc:
        raise RelayError(f"{method} {path} answered something that is not JSON") from exc


async def post_uploads(client: httpx.AsyncClient, body: social.UploadsPost) -> dict[str, Any]:
    return await _call(client, "POST", "/v1/ae/social/uploads", body.model_dump(mode="json"))


async def run(
    *,
    script: Path,
    from_file: Path | None,
    settings: omni.Settings,
    dry_run: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    out: Any = None,
    runner: Any = subprocess.run,
) -> int:
    """Read, then print (dry run) or post; the exit status (1 when every channel failed)."""
    out = out or sys.stdout
    try:
        if from_file is not None:
            data = _parse(from_file.read_text(encoding="utf-8"), str(from_file))
        else:
            data = await asyncio.to_thread(run_lister, script, runner=runner)
        body = build_body(data)
    except (RelayError, OSError) as exc:
        print(f"social: {exc}", file=out)
        return 1
    failed = [ch for ch, post in body.channels.items() if not post.ok]
    for ch in failed:
        print(f"social: {ch} read failed: {body.channels[ch].error}", file=out)
    if dry_run:
        print(json.dumps(body.model_dump(mode="json"), indent=2), file=out)
        print(json.dumps(dry_report(body), indent=2), file=out)
        return 1 if body.channels and len(failed) == len(body.channels) else 0
    if not settings.configured:
        print(settings.problem, file=out)
        return 1
    async with omni.http_client(settings, transport=transport) as client:
        try:
            kept = await post_uploads(client, body)
        except RelayError as exc:
            print(f"social: {exc}", file=out)
            return 1
    sent = sum(len(post.items) for post in body.channels.values())
    print(
        f"social: posted {sent} uploads; the server now plans {kept.get('slots', 0)} posts",
        file=out,
    )
    return 1 if body.channels and len(failed) == len(body.channels) else 0


async def run_week(
    settings: omni.Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    out: Any = None,
    redraft: bool = False,
) -> int:
    out = out or sys.stdout
    if not settings.configured:
        print(settings.problem, file=out)
        return 1
    async with omni.http_client(settings, transport=transport) as client:
        try:
            path = "/v1/ae/social/week-redraft" if redraft else "/v1/ae/social/week"
            answer = await _call(client, "GET", path)
        except RelayError as exc:
            print(f"social: {exc}", file=out)
            return 1
    print(json.dumps(answer, indent=2), file=out)
    return 0


async def run_put_drafts(
    path: Path,
    settings: omni.Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    out: Any = None,
) -> int:
    out = out or sys.stdout
    try:
        payload = _parse(path.read_text(encoding="utf-8"), str(path))
    except (RelayError, OSError) as exc:
        print(f"social: {exc}", file=out)
        return 1
    if not settings.configured:
        print(settings.problem, file=out)
        return 1
    async with omni.http_client(settings, transport=transport) as client:
        try:
            answer = await _call(client, "POST", "/v1/ae/social/drafts", payload)
        except RelayError as exc:
            print(f"social: {exc}", file=out)
            return 1
    print(json.dumps(answer, indent=2), file=out)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ae_omni_policies.social_relay",
        description="Post the YouTube uploads to the OmniGent server's Social card.",
    )
    parser.add_argument("--dry-run", action="store_true", help="print, do not post")
    parser.add_argument(
        "--from-file", default=None, help="a saved lister output to use instead of running node"
    )
    parser.add_argument(
        "--lister",
        default=None,
        help=f"the lister script (default: ${LISTER_ENV}, else the box path)",
    )
    sub = parser.add_subparsers(dest="command")
    week = sub.add_parser("week", help="print the open posts that still need a draft")
    week.add_argument(
        "--redraft",
        action="store_true",
        help="also list open posts whose draft is the drafter's own and unedited",
    )
    put = sub.add_parser("put-drafts", help="post a drafts JSON file to the server")
    put.add_argument("file")
    args = parser.parse_args(argv)
    settings = omni.Settings.from_env()
    if args.command == "week":
        return asyncio.run(run_week(settings, redraft=args.redraft))
    if args.command == "put-drafts":
        return asyncio.run(run_put_drafts(Path(args.file), settings))
    script = Path(args.lister or os.environ.get(LISTER_ENV) or DEFAULT_LISTER)
    return asyncio.run(
        run(
            script=script,
            from_file=Path(args.from_file) if args.from_file else None,
            settings=settings,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
