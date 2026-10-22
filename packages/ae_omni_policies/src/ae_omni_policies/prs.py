"""Pull request cards with a Merge button, built on upstream's per-session PR tracking.

Added 2026-09-24. Reid is travelling for three weeks, works from his phone,
and merged every pull request by hand on github.com. The cards put CI,
mergeability, reviews and a Merge button next to the session that opened the
pull request (patch P11, the session's GitHub panel) and on the Dashboard
(the extension's Open PRs section).

Which pull requests: upstream v0.14 tracks them per session
(https://github.com/omnigent-ai/omnigent/pull/6935): the host keeps a
registry per session (``omnigent/runner/session_prs.py``) of every pull
request the agent created, worked on, or the user linked, and serves it as
``prs`` on ``GET /v1/sessions/{id}/resources/github``. This module reads that
route through the server's own loopback, as the caller, rather than a second
copy of the registry: the registry lives on the host, not the server, and the
route already falls back from the runner to the host.

What is on a card: GitHub's REST API, with ``AE_GITHUB_TOKEN``
(:mod:`ae_omni_policies.github`). Four calls per pull request (the pull, its
reviews, the head's check runs, the head's commit statuses) plus the repo's
merge settings, cached for :data:`CARD_TTL_S` (the Dashboard polls every 20
seconds and a fine-grained token has 5,000 calls an hour). The merge reads
everything fresh.

The merge gate is decided here, once, so the React card and the Dashboard card
show the same reason: open, not a draft, CI neither failing nor running,
GitHub's ``mergeable_state`` clean (or ``has_hooks``), and no review asking for
changes. The route re-checks it on a fresh read and pins the merge to the head
SHA the card showed, so a push after the card was read is never merged unseen.

Stale-while-revalidate (2026-09-26): the Dashboard opened in 3 to 5 seconds
on Reid's phone because ``GET /v1/ae/prs`` awaited the tracked-PR scan (2321
ms cold, 12 ms warm on the box) whenever the page had been closed for a
minute. The scan and the list's cards are now served from what is held and
refreshed in the background (:mod:`ae_omni_policies.swr`); only the first
scan after a server start, and a card never read, are awaited. ``fresh=true``
still awaits a scan (a merge and the MCP ``list_prs`` tool rely on it). A
card on the list can be up to :data:`CARD_TTL_S` plus one poll old, which is
safe because Merge re-reads the gate fresh and refuses a moved head.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx
from omnigent.entities.conversation import Conversation

from ae_omni_policies import github, labels, pr_compliance, sessions
from ae_omni_policies.swr import SwrCache

_logger = logging.getLogger(__name__)

# Cards (four GitHub calls each) and the repo settings (one call), per process.
CARD_TTL_S = 60.0
REPO_TTL_S = 600.0
# Which pull requests each session tracks; the scan is one loopback per session.
SCAN_TTL_S = 60.0
# One person's sessions: the newest this many are scanned, this many at a time.
MAX_SESSIONS = 40
# 12 since 2026-09-26 (was 6): the loopbacks are cheap in-process reads, and
# the cold scan of about 40 sessions took 2.3 s at 6.
SCAN_CONCURRENCY = 12
# GitHub computes mergeability on the first read after a push; one retry after
# this long usually has the answer.
MERGEABLE_RETRY_S = 1.5

CODE_NOT_CONFIGURED = "github_not_configured"
CODE_GITHUB_FAILED = "github_unavailable"
CODE_MERGE_BLOCKED = "merge_blocked"
CODE_HEAD_MOVED = "merge_head_moved"
CODE_MERGE_REFUSED = "merge_refused"
CODE_READY_BLOCKED = "ready_blocked"
CODE_READY_REFUSED = "ready_refused"
CODE_UPDATE_BLOCKED = "update_branch_blocked"
CODE_UPDATE_REFUSED = "update_branch_refused"

NOT_CONFIGURED = (
    "The server has no GitHub token (AE_GITHUB_TOKEN), so pull request cards and "
    "Merge are off. deploy/README.md, 'The GitHub token', has the one-time setup."
)
# Merging needs more than reading; said when GitHub refuses the merge itself.
MERGE_SCOPES_HINT = (
    "the token needs Contents and Pull requests: read and write on this repo "
    "(and Workflows: read and write when the pull request changes .github/workflows)"
)

_OWNER_REPO = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")
_PULL_PATH = re.compile(r"^/([^/]+)/([^/]+)/pull/([1-9][0-9]*)/?$")

LoopbackGet = Callable[[str], Awaitable[httpx.Response]]

_cards: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
_repos: dict[str, tuple[float, dict[str, Any]]] = {}
_scan = SwrCache[None, dict[str, Any]]("pull request scan", SCAN_TTL_S)
# The list's background card reads (2026-09-26): one per card at most, the
# last failure per card, and a generation that :func:`forget` bumps so a read
# that started before a merge never stores the pre-merge card.
_card_refreshes: dict[tuple[str, int], asyncio.Task[None]] = {}
_card_errors: dict[tuple[str, int], str] = {}
_card_generation = 0


def reset_cache() -> None:
    """Forget every cached card, repo and scan (tests, a changed setting)."""
    global _card_generation
    _cards.clear()
    _repos.clear()
    _scan.reset()
    _card_generation += 1
    for task in list(_card_refreshes.values()):
        # RuntimeError: its loop is closed (a finished test).
        with contextlib.suppress(RuntimeError):
            task.cancel()
    _card_refreshes.clear()
    _card_errors.clear()
    pr_compliance.reset_cache()


def valid_repo(owner: str, repo: str) -> bool:
    """Owner and name as GitHub spells them; never a dot-only segment."""
    return all(_OWNER_REPO.match(part) and part.strip(".") for part in (owner, repo))


def parse_pull_url(url: str) -> tuple[str, int] | None:
    """``owner/repo`` (lowercased, as upstream stores it) and the number, github.com only."""
    parts = urlsplit(url.strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() != "github.com":
        return None
    match = _PULL_PATH.match(parts.path)
    if match is None or not valid_repo(match[1], match[2]):
        return None
    return f"{match[1]}/{match[2]}".lower(), int(match[3])


# ── the card ────────────────────────────────────────────────────────────


def ci_summary(runs: list[dict[str, Any]], statuses: list[dict[str, Any]]) -> dict[str, Any]:
    """Check runs and commit statuses folded into counts and one state.

    ``neutral`` and ``skipped`` runs count as passing (GitHub's own rollup);
    ``stale`` and ``action_required`` count as failing. Any failing makes the
    state failing, else any pending makes it pending.
    """
    passing = failing = pending = 0
    for run in runs:
        if run.get("status") != "completed":
            pending += 1
        elif run.get("conclusion") in github._FAILING:
            failing += 1
        else:
            passing += 1
    for status in statuses:
        state = status.get("state")
        if state == "success":
            passing += 1
        elif state in ("failure", "error"):
            failing += 1
        else:
            pending += 1
    total = passing + failing + pending
    if total == 0:
        state = "none"
    elif failing:
        state = "failing"
    elif pending:
        state = "pending"
    else:
        state = "passing"
    return {
        "state": state,
        "passing": passing,
        "failing": failing,
        "pending": pending,
        "total": total,
    }


def review_summary(reviews: list[dict[str, Any]], pull: dict[str, Any]) -> dict[str, Any]:
    """The latest deciding review per reviewer, and who is still asked.

    A comment review does not change a reviewer's standing; a dismissed one
    clears it. ``changes_requested`` wins over ``approved``; with neither,
    ``review_required`` when someone is still asked, else ``none``.
    """
    latest: dict[str, str] = {}
    for review in reviews:
        login = ((review.get("user") or {}).get("login") or "").lower()
        state = review.get("state")
        if not login or state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        latest[login] = state
    approvals = sum(1 for state in latest.values() if state == "APPROVED")
    changes = sum(1 for state in latest.values() if state == "CHANGES_REQUESTED")
    requested = [
        str(person.get("login"))
        for person in pull.get("requested_reviewers") or []
        if isinstance(person, dict) and person.get("login")
    ] + [
        str(team.get("slug") or team.get("name"))
        for team in pull.get("requested_teams") or []
        if isinstance(team, dict) and (team.get("slug") or team.get("name"))
    ]
    if changes:
        state = "changes_requested"
    elif approvals:
        state = "approved"
    elif requested:
        state = "review_required"
    else:
        state = "none"
    return {
        "state": state,
        "approvals": approvals,
        "changes_requested": changes,
        "requested": requested,
    }


def pull_state(pull: dict[str, Any]) -> str:
    """``open``, ``merged`` or ``closed``."""
    if pull.get("merged") or pull.get("merged_at"):
        return "merged"
    return "open" if pull.get("state") == "open" else "closed"


def update_branch_gate(pull: dict[str, Any]) -> dict[str, Any]:
    """Use raw mergeability so a draft's display state cannot hide behind/conflicts."""
    if pull_state(pull) != "open":
        return {"allowed": False, "reason": "Only an open pull request can be updated."}
    if pull.get("mergeable_state") == "dirty" or pull.get("mergeable") is False:
        return {"allowed": False, "reason": "Resolve conflicts with the base branch manually."}
    if pull.get("mergeable_state") == "behind":
        return {"allowed": True, "reason": None}
    return {"allowed": False, "reason": "GitHub has not confirmed this branch is behind its base."}


def mergeability(pull: dict[str, Any]) -> dict[str, str]:
    """GitHub's ``mergeable_state`` in the card's words.

    ``clean`` (and ``has_hooks``) merge; ``unstable`` is mergeable with
    non-required checks failing, which the CI gate catches on its own;
    ``dirty`` is ``conflicted``; ``blocked`` is branch protection (a required
    review or check); ``behind`` is a strict protection wanting the base merged
    in; ``unknown`` (or a null ``mergeable``) is GitHub still computing.
    """
    state = pull_state(pull)
    if state != "open":
        return {"state": state, "detail": f"The pull request is {state}."}
    raw = str(pull.get("mergeable_state") or "unknown")
    if pull.get("draft") or raw == "draft":
        return {"state": "draft", "detail": "Draft: review details and mark ready for review."}
    if raw in ("clean", "has_hooks"):
        return {"state": "clean", "detail": "No conflicts with the base branch."}
    if raw == "unstable":
        return {
            "state": "unstable",
            "detail": "Mergeable, but a check that is not required failed.",
        }
    if raw == "dirty" or pull.get("mergeable") is False:
        return {"state": "conflicted", "detail": "Conflicts with the base branch."}
    if raw == "blocked":
        return {
            "state": "blocked",
            "detail": "Blocked by branch protection (a required review or check).",
        }
    if raw == "behind":
        return {
            "state": "behind",
            "detail": "Behind the base branch, which protection requires to be merged in.",
        }
    return {"state": "checking", "detail": "GitHub is still checking mergeability."}


def merge_methods(repo: dict[str, Any] | None) -> tuple[list[str], str | None]:
    """The methods the repo allows, of squash and merge, and the default.

    The default is squash when allowed, else merge: GitHub's API has no
    per-repo default (the web UI remembers the last one per browser), so the
    first allowed method in :data:`github.MERGE_METHODS` stands in. A settings
    read that failed, or a token GitHub hides the fields from, offers both.
    """
    fields = {"squash": "allow_squash_merge", "merge": "allow_merge_commit"}
    if not repo or not any(name in repo for name in fields.values()):
        allowed = list(github.MERGE_METHODS)
    else:
        allowed = [method for method in github.MERGE_METHODS if repo.get(fields[method])]
    return allowed, (allowed[0] if allowed else None)


LONG_LIVED_BRANCHES = frozenset({"main", "master", "dev", "develop", "staging"})
LONG_LIVED_PREFIXES = ("release/", "hotfix/")


def recommend_merge_method(
    methods: list[str], head_ref: str | None, base_ref: str | None
) -> tuple[str | None, str | None]:
    """The method a card recommends, and the one-line reason it shows.

    Added 2026-09-28 at Reid's request: the confirm step preselected squash on
    every card, and a squash is wrong when a pull request promotes one
    long-lived branch into another (dev into main, a release branch into
    main). Squashing that makes a new commit on the base that the head never
    gets, so the next promotion replays and conflicts. So: merge commit when
    the head branch is long-lived (:data:`LONG_LIVED_BRANCHES`, or a name under
    :data:`LONG_LIVED_PREFIXES`), squash for everything else, which is one
    change per pull request. When the repo allows only the other method, that
    one is recommended, since it is the only choice; with none allowed there
    is nothing to recommend.
    """
    if not methods:
        return None, None
    head = head_ref or ""
    base = base_ref or "the base branch"
    if head in LONG_LIVED_BRANCHES or head.startswith(LONG_LIVED_PREFIXES):
        method = "merge"
        reason = (
            f"Merge commit keeps {head} and {base} in step; "
            "a squash would make the next promotion conflict."
        )
    else:
        method = "squash"
        reason = f"Squash lands this change as one commit on {base}."
    if method not in methods:
        return methods[0], "The only method this repo allows."
    return method, reason


def merge_gate(card: dict[str, Any]) -> tuple[bool, str | None]:
    """Whether the Merge button is live, and the reason it is not."""
    state = card["state"]
    if state == "merged":
        return False, "Already merged."
    if state == "closed":
        return False, "Closed without merging."
    ci = card["ci"]
    if ci["state"] == "failing":
        return False, f"CI is failing: {ci['failing']} of {ci['total']} checks."
    if ci["state"] == "pending":
        return False, (
            f"CI is still running: {ci['passing']} of {ci['total']} checks passed, "
            f"{ci['pending']} to go."
        )
    if card["review"]["state"] == "changes_requested":
        return False, "A reviewer requested changes."
    mergeable = card["mergeable"]
    if mergeable["state"] not in ("clean", "unstable"):
        return False, mergeable["detail"]
    if not card["merge"]["methods"]:
        return False, "The repo allows neither squash nor merge commits."
    return True, None


async def _repo_settings(client: github.GitHubClient, repo: str) -> dict[str, Any] | None:
    now = time.monotonic()
    held = _repos.get(repo)
    if held is not None and now - held[0] < REPO_TTL_S:
        return held[1]
    try:
        settings = await client.repository(repo)
    except github.GitHubError:
        return None
    _repos[repo] = (now, settings)
    return settings


async def read_card(
    client: github.GitHubClient, repo: str, number: int, *, fresh: bool = False
) -> dict[str, Any]:
    """One pull request's card; from the cache unless *fresh* or older than the TTL.

    :raises github.GitHubError: When the pull request itself cannot be read.
    """
    key = (repo.lower(), number)
    now = time.monotonic()
    held = _cards.get(key)
    if not fresh and held is not None and now - held[0] < CARD_TTL_S:
        return held[1]
    generation = _card_generation
    card = await _read_card_now(client, repo, number)
    if generation == _card_generation:
        _cards[key] = (time.monotonic(), card)
        _card_errors.pop(key, None)
    return card


async def _read_card_now(client: github.GitHubClient, repo: str, number: int) -> dict[str, Any]:
    """Read one card from GitHub, bypassing and not touching the card cache."""
    pull = await client.pull(repo, number)
    if pull_state(pull) == "open" and pull.get("mergeable") is None:
        await asyncio.sleep(MERGEABLE_RETRY_S)
        pull = await client.pull(repo, number)
    sha = str((pull.get("head") or {}).get("sha") or "")

    async def optional(call: Awaitable[list[dict[str, Any]]]) -> list[dict[str, Any]] | None:
        try:
            return await call
        except github.GitHubError:
            return None

    reviews, runs, statuses, settings = await asyncio.gather(
        optional(client.reviews(repo, number)),
        optional(client.check_runs(repo, sha)) if sha else asyncio.sleep(0, []),
        optional(client.statuses(repo, sha)) if sha else asyncio.sleep(0, []),
        _repo_settings(client, repo),
    )
    ci = ci_summary(runs or [], statuses or [])
    if runs is None or statuses is None:
        # A check read that failed is not a pass: the button stays off.
        ci = {**ci, "state": "unknown"}
    methods, _ = merge_methods(settings)
    head_ref = (pull.get("head") or {}).get("ref")
    base_ref = (pull.get("base") or {}).get("ref")
    recommended, why = recommend_merge_method(methods, head_ref, base_ref)
    card: dict[str, Any] = {
        "repo": repo.lower(),
        "number": number,
        "url": pull.get("html_url") or f"https://github.com/{repo}/pull/{number}",
        "title": pull.get("title") or f"#{number}",
        "body": pull.get("body") if isinstance(pull.get("body"), str) else "",
        "additions": pull.get("additions") if isinstance(pull.get("additions"), int) else 0,
        "deletions": pull.get("deletions") if isinstance(pull.get("deletions"), int) else 0,
        "changed_files": (
            pull.get("changed_files") if isinstance(pull.get("changed_files"), int) else 0
        ),
        "state": pull_state(pull),
        "draft": bool(pull.get("draft")),
        "author": (pull.get("user") or {}).get("login"),
        "head_ref": head_ref,
        "base_ref": base_ref,
        "head_sha": sha or None,
        "updated_at": pull.get("updated_at"),
        "merged_at": pull.get("merged_at"),
        "ci": ci,
        "mergeable": mergeability(pull),
        "review": review_summary(reviews or [], pull),
        "merge": {
            "methods": methods,
            "default_method": recommended,
            "recommended_method": recommended,
            "recommended_reason": why,
        },
    }
    if pull_state(pull) == "open" and (
        pull.get("mergeable_state") in ("behind", "dirty") or pull.get("mergeable") is False
    ):
        card["update_branch"] = update_branch_gate(pull)
    if reviews is None:
        card["review"]["state"] = "unknown"
    allowed, reason = merge_gate(card)
    if allowed and ci["state"] == "unknown":
        allowed, reason = False, "CI could not be read; refresh to try again."
    card["merge"] |= {"allowed": allowed, "reason": reason}
    return card


def _refresh_card_later(settings: github.GitHubSettings, repo: str, number: int) -> None:
    """Re-read one list card in the background; the held card is served meanwhile.

    One read per card at most. A failure keeps the held card and is listed in
    ``errors`` beside it until a read succeeds; any other exception is logged
    by class name, never raised unobserved.
    """
    key = (repo.lower(), number)
    running = _card_refreshes.get(key)
    loop = asyncio.get_running_loop()
    if running is not None and not running.done() and running.get_loop() is loop:
        return
    generation = _card_generation

    async def run() -> None:
        try:
            async with github.GitHubClient(settings) as client:
                card = await _read_card_now(client, repo, number)
        except github.GitHubError as exc:
            if generation == _card_generation:
                _card_errors[key] = str(exc)
            return
        except Exception as exc:  # noqa: BLE001 (a background task must not raise unobserved)
            _logger.warning("prs: re-reading %s#%s failed: %s", repo, number, type(exc).__name__)
            if generation == _card_generation:
                _card_errors[key] = f"{type(exc).__name__}"
            return
        if generation == _card_generation:
            _cards[key] = (time.monotonic(), card)
            _card_errors.pop(key, None)

    task = loop.create_task(run(), name=f"ae-prs:card:{repo}#{number}")
    _card_refreshes[key] = task

    def done(finished: asyncio.Task[None]) -> None:
        if _card_refreshes.get(key) is finished:
            del _card_refreshes[key]

    task.add_done_callback(done)


def forget(repo: str, number: int) -> None:
    """Drop one card after a PR write, and refresh the scan on its next read.

    Background reads started before the write cannot replace the new card.
    """
    global _card_generation
    key = (repo.lower(), number)
    _card_generation += 1
    _cards.pop(key, None)
    _card_errors.pop(key, None)
    pr_compliance.forget(repo, number)
    _scan.mark_stale()


# ── which pull requests the sessions track ─────────────────────────────


def _session_ref(conv: Conversation) -> dict[str, Any]:
    return {
        "id": conv.id,
        "title": sessions.display_title(conv),
        "status": labels.status_of(conv.labels),
    }


async def tracked_by_session(
    rows: Iterable[Conversation], fetch: LoopbackGet
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, str]], int]:
    """Every tracked pull request URL, with the sessions that track it.

    :param rows: Top-level sessions, newest first; the first :data:`MAX_SESSIONS` are read.
    :param fetch: GET a server path as the caller (the router's loopback).
    :returns: ``(url -> [session refs], errors, sessions scanned)``.
    """
    chosen = list(rows)[:MAX_SESSIONS]
    gate = asyncio.Semaphore(SCAN_CONCURRENCY)
    by_url: dict[str, list[dict[str, Any]]] = {}
    errors: list[dict[str, str]] = []

    async def one(conv: Conversation) -> None:
        async with gate:
            try:
                response = await fetch(f"/v1/sessions/{conv.id}/resources/github")
            except httpx.HTTPError as exc:
                errors.append({"session_id": conv.id, "message": type(exc).__name__})
                return
        if response.status_code >= 300:
            # Runner and host both offline is the common case: say it quietly.
            errors.append({"session_id": conv.id, "message": f"{response.status_code}"})
            return
        try:
            payload = response.json()
        except ValueError:
            return
        for entry in (payload.get("prs") if isinstance(payload, dict) else None) or []:
            url = entry.get("url") if isinstance(entry, dict) else None
            if isinstance(url, str) and parse_pull_url(url) is not None:
                by_url.setdefault(url.lower(), []).append(_session_ref(conv))

    await asyncio.gather(*(one(conv) for conv in chosen))
    return by_url, errors, len(chosen)


async def scan(fetch: LoopbackGet, *, fresh: bool = False) -> dict[str, Any]:
    """The tracked-PR map, stale-while-revalidate (2026-09-26).

    Under :data:`SCAN_TTL_S` old it is served as is; older, it is served at
    once while one background scan replaces it; only the first scan after a
    server start is awaited. *fresh* awaits a scan (sharing one in flight).
    A background scan that fails keeps the last map and adds
    ``refresh_error``.
    """

    async def load() -> dict[str, Any]:
        rows = await asyncio.to_thread(sessions.list_top_level)
        by_url, errors, scanned = await tracked_by_session(rows, fetch)
        return {"by_url": by_url, "errors": errors, "scanned": scanned}

    answer = await _scan.get(None, load, fresh=fresh)
    failed = _scan.last_error(None)
    return answer if failed is None else {**answer, "refresh_error": failed}


async def open_pull_requests(fetch: LoopbackGet, *, fresh: bool = False) -> dict[str, Any]:
    """``GET /v1/ae/prs``: every open pull request any session tracks, as cards.

    Always a 200 when the token is set: a session that could not be read and a
    pull request that could not be read are listed in ``errors`` beside the
    cards that could.
    """
    settings = github.GitHubSettings.from_env()
    if not settings.configured:
        return {"configured": False, "note": NOT_CONFIGURED, "items": [], "errors": []}
    tracked = await scan(fetch, fresh=fresh)
    errors: list[dict[str, Any]] = [
        {"session_id": row["session_id"], "message": f"session not read: {row['message']}"}
        for row in tracked["errors"]
    ]
    if tracked.get("refresh_error"):
        errors.append(
            {
                "message": "which sessions track what was not re-read "
                f"({tracked['refresh_error']}); showing the last scan"
            }
        )
    wanted = [(url, parse_pull_url(url)) for url in tracked["by_url"]]
    async with github.GitHubClient(settings) as client:

        async def one(url: str, repo: str, number: int) -> dict[str, Any] | None:
            key = (repo.lower(), number)
            held = _cards.get(key)
            if held is not None:
                # Stale-while-revalidate (2026-09-26): the held card now, a
                # fresh one on a later poll. Merge re-reads the gate fresh.
                if time.monotonic() - held[0] >= CARD_TTL_S:
                    _refresh_card_later(settings, repo, number)
                card = held[1]
                failed = _card_errors.get(key)
                if failed:
                    message = f"showing the last read; re-reading failed: {failed}"
                    errors.append({"url": url, "message": message})
                return {**card, "sessions": tracked["by_url"][url]}
            try:
                card = await read_card(client, repo, number)
            except github.GitHubError as exc:
                errors.append({"url": url, "message": str(exc)})
                return None
            return {**card, "sessions": tracked["by_url"][url]}

        cards = await asyncio.gather(
            *(one(url, *parsed) for url, parsed in wanted if parsed is not None)
        )
    items = [card for card in cards if card is not None and card["state"] == "open"]
    items.sort(key=lambda card: str(card.get("updated_at") or ""), reverse=True)
    return {
        "configured": True,
        "items": items,
        "errors": errors,
        "sessions_scanned": tracked["scanned"],
    }


__all__ = [
    "CARD_TTL_S",
    "CODE_GITHUB_FAILED",
    "CODE_HEAD_MOVED",
    "CODE_MERGE_BLOCKED",
    "CODE_MERGE_REFUSED",
    "CODE_NOT_CONFIGURED",
    "MERGE_SCOPES_HINT",
    "NOT_CONFIGURED",
    "ci_summary",
    "forget",
    "merge_gate",
    "merge_methods",
    "mergeability",
    "open_pull_requests",
    "parse_pull_url",
    "read_card",
    "recommend_merge_method",
    "reset_cache",
    "review_summary",
    "scan",
    "tracked_by_session",
    "valid_repo",
]
