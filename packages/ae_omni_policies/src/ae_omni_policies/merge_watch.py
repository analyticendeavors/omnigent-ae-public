"""The merge watcher: a session continues by itself when its pull request merges.

Added 2026-10-21. Claude Code Desktop notices a merge on GitHub and carries on
within seconds; in OmniGent every session asked the owner to say "merged" first.
This module closes that gap for every session on the server.

What it watches: the pull requests attached to sessions, the same
``resources/github`` ``prs`` list that draws the merge cards in chat
(:func:`ae_omni_policies.prs.tracked_by_session`, read every
:data:`DISCOVERY_INTERVAL_S` in the background). Only the open ones are
polled, every :data:`POLL_INTERVAL_S`, with one conditional
``GET /repos/{repo}/pulls/{n}`` each (``If-None-Match`` on GitHub's ETag;
GitHub does not count a 304 against the rate limit), through
``AE_GITHUB_TOKEN`` (:mod:`ae_omni_policies.github`). A 403 or 429 that names
a rate limit, or a 200 with ``x-ratelimit-remaining: 0``, pauses every poll
until ``retry-after`` or ``x-ratelimit-reset`` (doubling from a minute when
GitHub names neither, capped at an hour); any other failure backs off that one
pull request. Polling spends no model tokens.

What it does on a change: the first time a pull request is seen merged (or
closed without merging) after being seen open, it posts ONE note into every
session that tracks it (:func:`note_text`), drops the PR card from the card
cache so the chat card shows the merge on its next read, and stops polling
that pull request. A pull request first seen already merged is only announced
when the merge is under :data:`FIRST_SIGHT_WINDOW_S` old, which covers a
merge that landed before the next scan listed the pull request; an old merged
pull request attached later stays quiet.

Exactly once, across restarts: the last seen state, the ETag and any note not
yet delivered are kept in ``/data/ae-merge-watch.json`` (``AE_MERGE_WATCH_FILE``
overrides; in memory when neither exists, as in tests and a bare local run).
The file is written before a note is posted and again after each delivery, so
a restart neither posts a delivered note again (outside the instant between a
post and its write) nor loses one, and a merge that happened while the server
was down is caught on the first poll after start, since the pull request is
still ``open`` in the file.

How the note is posted: ``POST /v1/sessions/{id}/events`` in process, through
``httpx.ASGITransport``, as the session's owner (the owner grant in the
permission store; a five-minute session token minted with the server's own
provider in OIDC and accounts mode, the identity header in header mode). That
is the route an external post uses, so it wakes an idle session the same way.
The note starts with ``Merge watcher: PR #n (``, which the slot cap, the
rabbit hole and the fork classifier recognize
(:mod:`ae_omni_policies.continuation`), so a session in ``review`` with the
slots full, or past four hours, still continues.

Mounted through :data:`lifespan_router` in :data:`ae_omni_policies.router.DEBUG_ROUTERS`:
FastAPI merges an included router's lifespan into the app's, so the watcher
starts after upstream's own startup and stops with it, with no upstream patch.
``AE_MERGE_WATCH=0`` turns it off; without a GitHub token it never starts.
One watcher per server process: a deployment with several replicas would post
once per replica.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, FastAPI

from ae_omni_policies import auth, github, labels, prs, sessions
from ae_omni_policies.continuation import MERGE_WATCH_PREFIX

_logger = logging.getLogger(__name__)

ENABLED_ENV = "AE_MERGE_WATCH"
STATE_FILE_ENV = "AE_MERGE_WATCH_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-merge-watch.json"

#: Seconds between polls of the open pull requests (approved by the owner, 2026-10-21).
POLL_INTERVAL_S = 10.0
#: Seconds between reads of which pull requests the sessions track. Each read asks
#: the session's runner or host, which looks the pull request up with ``gh``, so
#: this is two minutes rather than one (review, 2026-10-21).
DISCOVERY_INTERVAL_S = 120.0
#: Sessions a scan reads: touched this recently, plus any with a pull request still watched.
ACTIVE_S = 24 * 3600.0
#: How long a tick waits for a scan before polling with what it has.
DISCOVERY_WAIT_S = 2.0
#: A pull request is polled while a scan listed it this recently.
SEEN_TTL_S = 6 * 3600.0
#: Records dropped from the file: open ones no scan listed for a week,
#: settled ones three days after their note went out.
PRUNE_OPEN_S = 7 * 86400.0
PRUNE_SETTLED_S = 3 * 86400.0
#: A pull request first seen merged or closed is announced when the change is this recent.
FIRST_SIGHT_WINDOW_S = 600.0
POLL_CONCURRENCY = 8
#: A note post that has not answered by then is logged and not tried again: the
#: route may have stored it and be waiting on a runner launch or an approval.
POST_TIMEOUT_S = 60.0
MAX_ERROR_BACKOFF_S = 900.0
RATE_LIMIT_DEFAULT_S = 60.0
RATE_LIMIT_MAX_S = 3600.0
#: Tries per note and session before it is given up (about an hour of backoff).
MAX_POST_ATTEMPTS = 12
MAX_POST_BACKOFF_S = 600.0
#: Lifetime of the token minted to act as a session's owner.
TOKEN_TTL_S = 300
_LOOPBACK_TIMEOUT_S = 30.0
_TITLE_MAX = 120

State = Literal["unknown", "open", "merged", "closed"]
PostOutcome = Literal["posted", "retry", "drop"]


@dataclass(frozen=True)
class Scan:
    """One scan: ``{pull request URL: [session ids]}`` and the sessions it could read."""

    by_url: dict[str, list[str]]
    read: frozenset[str]


#: One scan of the tracked pull requests, or ``None`` when it failed.
Discover = Callable[[set[str]], Awaitable[Scan | None]]
#: Post *text* into session *id*.
Post = Callable[[str, str], Awaitable[PostOutcome]]


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the watcher runs (default on; ``0``, ``false``, ``off``, ``no`` turn it off)."""
    value = (environ if environ is not None else os.environ).get(ENABLED_ENV, "")
    return value.strip().lower() not in {"0", "false", "off", "no"}


# ── the state file ──────────────────────────────────────────────────────


@dataclass
class Watched:
    """One pull request: what was last seen, and any note still to deliver."""

    repo: str
    number: int
    url: str
    state: State = "unknown"
    etag: str | None = None
    title: str | None = None
    sessions: list[str] = field(default_factory=list)
    seen_at: float = 0.0
    settled_at: float | None = None
    note: str | None = None
    #: ``{session id: {"attempts": n, "next": epoch}}`` for notes not yet delivered.
    pending: dict[str, dict[str, float]] = field(default_factory=dict)
    #: Sessions the note was delivered to, dropped for, or given up on.
    notified: list[str] = field(default_factory=list)
    errors: int = 0
    next_poll: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"


def _record(raw: Any) -> Watched | None:
    if not isinstance(raw, dict):
        return None
    try:
        record = Watched(**raw)
    except TypeError:
        return None
    if record.state not in ("unknown", "open", "merged", "closed"):
        return None
    return record


class StateFile:
    """The watcher's records, as one JSON file written atomically; in memory without a path."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._memory: dict[str, dict[str, Any]] = {}

    def load(self) -> dict[str, Watched]:
        """Every record; an unreadable file is logged and read as empty."""
        rows: Any
        if self.path is None:
            rows = self._memory
        else:
            try:
                body = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except (OSError, ValueError) as exc:
                _logger.warning(
                    "merge watcher: could not read %s (%s); starting empty, so a merge made "
                    "while the server was down is not announced",
                    self.path,
                    exc,
                )
                return {}
            rows = body.get("prs") if isinstance(body, dict) else None
        records: dict[str, Watched] = {}
        for raw in (rows or {}).values() if isinstance(rows, dict) else []:
            record = _record(dict(raw)) if isinstance(raw, dict) else None
            if record is not None:
                records[record.key] = record
        return records

    def save(self, records: Mapping[str, Watched]) -> None:
        """Replace the file with *records*; a failed write is logged, never raised."""
        rows = {key: asdict(record) for key, record in records.items()}
        if self.path is None:
            self._memory = json.loads(json.dumps(rows))
            return
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-merge-watch-", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "prs": rows}, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            _logger.warning(
                "merge watcher: could not write %s (%s); check the folder is writable. "
                "Until it is, a restart may announce a merge twice or miss one.",
                self.path,
                exc,
            )


def state_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """``AE_MERGE_WATCH_FILE``, else ``/data/ae-merge-watch.json`` when ``/data`` exists."""
    raw = ((environ if environ is not None else os.environ).get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


# ── the note ────────────────────────────────────────────────────────────


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _clock_time(value: Any) -> str | None:
    """``2026-10-21T17:00:04Z`` as ``17:00Z``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.strftime("%H:%MZ")


def _one_line(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= _TITLE_MAX else f"{flat[: _TITLE_MAX - 3].rstrip()}..."


def note_text(record: Watched, pull: Mapping[str, Any]) -> str:
    """The one line posted into the session, plain English.

    ``Merge watcher: PR #196 (Title) merged at 17:00Z as 11c89f51 by login.
    https://github.com/... Continue: delete its branch and worktree, then
    check the deploy.`` A close without a merge says so and asks for the
    call instead.
    """
    title = _one_line(str(pull.get("title") or record.title or f"#{record.number}"))
    url = pull.get("html_url") if isinstance(pull.get("html_url"), str) else record.url
    head = f"{MERGE_WATCH_PREFIX}PR #{record.number} ({title})"
    if prs.pull_state(dict(pull)) == "merged":
        parts = [f"{head} merged"]
        when = _clock_time(pull.get("merged_at"))
        sha = pull.get("merge_commit_sha")
        merged_by = pull.get("merged_by")
        login = merged_by.get("login") if isinstance(merged_by, dict) else None
        if when:
            parts.append(f"at {when}")
        if isinstance(sha, str) and sha:
            parts.append(f"as {sha[:8]}")
        if isinstance(login, str) and login:
            parts.append(f"by {login}")
        return (
            f"{' '.join(parts)}. {url} Continue: delete its branch and worktree, "
            "then check the deploy."
        )
    when = _clock_time(pull.get("closed_at"))
    closed = f"{head} was closed without merging" + (f" at {when}" if when else "")
    return (
        f"{closed}. {url} Continue: if the work is dropped, delete its branch and worktree; "
        "if it moves to another pull request, say which."
    )


# ── the watcher ─────────────────────────────────────────────────────────


def _rate_limited(answer: github.ConditionalPull) -> bool:
    if answer.status == 429:
        return True
    return answer.status == 403 and (
        answer.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in answer.headers
    )


class MergeWatcher:
    """Polls the open attached pull requests and posts one note per merge or close.

    :param settings: The GitHub token and API origin.
    :param discover: Which pull requests the sessions track, by URL.
    :param post: Delivers a note to one session.
    :param store: Where the records live between restarts.
    :param clock: Epoch seconds (tests pin it).
    :param transport: An httpx transport for GitHub (tests mock it).
    """

    def __init__(
        self,
        settings: github.GitHubSettings,
        *,
        discover: Discover,
        post: Post,
        store: StateFile,
        clock: Callable[[], float] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._discover = discover
        self._post = post
        self._store = store
        self._clock = clock or time.time
        self._client = github.GitHubClient(settings, transport=transport)
        self.records: dict[str, Watched] = store.load()
        self.paused_until = 0.0
        self._rate_strikes = 0
        self._next_discovery = 0.0
        self._discovery: asyncio.Task[dict[str, list[str]] | None] | None = None
        self._dirty = False

    async def aclose(self) -> None:
        if self._discovery is not None and not self._discovery.done():
            self._discovery.cancel()
        await self._client.__aexit__(None, None, None)

    def pollable(self, now: float) -> list[Watched]:
        """Pull requests not merged that a scan listed recently, not backing off.

        A closed one stays in the set (its 304s cost nothing), so a pull request
        closed and reopened, to rerun CI say, is announced when it merges.
        """
        return [
            record
            for record in self.records.values()
            if record.state != "merged"
            and now - record.seen_at <= SEEN_TTL_S
            and record.next_poll <= now
        ]

    async def tick(self) -> None:
        """One round: take in a finished scan, poll what is due, deliver notes, save."""
        now = self._clock()
        if self._discovery is None and now >= self._next_discovery:
            self._next_discovery = now + DISCOVERY_INTERVAL_S
            self._discovery = asyncio.ensure_future(self._discover(self.watched_sessions()))
        if self._discovery is not None:
            await asyncio.wait({self._discovery}, timeout=DISCOVERY_WAIT_S)
            if self._discovery.done():
                finished, self._discovery = self._discovery, None
                self._take_scan(finished, self._clock())
        now = self._clock()
        if now >= self.paused_until:
            await self._poll(now)
        self._prune(self._clock())
        if self._dirty:
            self._save()
        await self._deliver()

    async def run(self) -> None:
        """Tick every :data:`POLL_INTERVAL_S` until cancelled; a failed tick is logged."""
        try:
            while True:
                started = self._clock()
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception("merge watcher: a poll round failed; trying again")
                elapsed = self._clock() - started
                await asyncio.sleep(max(1.0, POLL_INTERVAL_S - elapsed))
        finally:
            await self.aclose()

    def watched_sessions(self) -> set[str]:
        """Sessions every scan reads whatever their age: they track a watched pull request."""
        return {
            session
            for record in self.records.values()
            if record.state != "merged"
            for session in record.sessions
        }

    def _save(self) -> None:
        self._store.save(self.records)
        self._dirty = False

    def _take_scan(self, finished: asyncio.Future[Scan | None], now: float) -> None:
        try:
            found = finished.result()
        except Exception as exc:  # noqa: BLE001 (a failed scan keeps the last one; polling goes on)
            _logger.warning("merge watcher: reading the tracked pull requests failed: %r", exc)
            return
        if found is None:
            return
        for url, session_ids in found.by_url.items():
            parsed = prs.parse_pull_url(url)
            if parsed is None:
                continue
            repo, number = parsed
            key = f"{repo}#{number}"
            record = self.records.get(key)
            if record is None:
                record = Watched(repo=repo, number=number, url=url)
                self.records[key] = record
            # A session this scan could not read (its host offline) keeps its place.
            kept = {sid for sid in record.sessions if sid not in found.read}
            record.sessions = sorted(kept | set(session_ids))
            record.seen_at = now
            self._late_subscribers(record, now)
        self._dirty = True

    def _late_subscribers(self, record: Watched, now: float) -> None:
        """A session that attached the pull request after its note went out gets it too.

        Only while the change is recent (:data:`FIRST_SIGHT_WINDOW_S`), the same
        bound a first sight has, and never twice to one session.
        """
        if record.note is None or record.settled_at is None:
            return
        if now - record.settled_at > FIRST_SIGHT_WINDOW_S:
            return
        for session in record.sessions:
            if session not in record.notified and session not in record.pending:
                record.pending[session] = {"attempts": 0, "next": 0.0}

    async def _poll(self, now: float) -> None:
        due = self.pollable(now)
        if not due:
            return
        gate = asyncio.Semaphore(POLL_CONCURRENCY)

        async def one(record: Watched) -> None:
            async with gate:
                if self._clock() < self.paused_until:
                    return
                try:
                    answer = await self._client.pull_if_changed(
                        record.repo, record.number, record.etag
                    )
                except github.GitHubError as exc:
                    self._failed(record, now, str(exc))
                    return
                self._answered(record, answer, now)

        await asyncio.gather(*(one(record) for record in due))

    def _answered(self, record: Watched, answer: github.ConditionalPull, now: float) -> None:
        if _rate_limited(answer):
            self._pause(answer, now)
            return
        if answer.status == 304:
            record.errors, record.next_poll = 0, 0.0
            self._rate_strikes = 0
            return
        if answer.status != 200 or answer.pull is None:
            self._failed(record, now, f"answered {answer.status}")
            return
        record.errors, record.next_poll = 0, 0.0
        self._rate_strikes = 0
        record.etag = answer.etag
        self._dirty = True
        if answer.headers.get("x-ratelimit-remaining") == "0":
            self._pause(answer, now)
        pull = answer.pull
        record.title = _one_line(str(pull.get("title") or record.title or f"#{record.number}"))
        state = prs.pull_state(pull)
        before = record.state
        if state == "open":
            if before == "closed":
                # Reopened: a later merge or close is a new change to announce.
                record.note, record.settled_at = None, None
                record.notified, record.pending = [], {}
            record.state = "open"
            return
        if state == before:
            return
        record.state = "merged" if state == "merged" else "closed"
        record.settled_at = now
        changed_at = _epoch(pull.get("merged_at") or pull.get("closed_at"))
        recent = changed_at is not None and now - changed_at <= FIRST_SIGHT_WINDOW_S
        if before == "open" or (before == "unknown" and recent):
            record.note = note_text(record, pull)
            record.pending = {sid: {"attempts": 0, "next": 0.0} for sid in record.sessions}
            record.notified = []
            prs.forget(record.repo, record.number)
            _logger.info(
                "merge watcher: %s is %s; telling %d session(s)",
                record.key,
                record.state,
                len(record.pending),
            )

    def _pause(self, answer: github.ConditionalPull, now: float) -> None:
        if now < self.paused_until:
            # Already paused by another answer in the same round: one strike per pause.
            return
        wait: float | None = None
        retry_after = answer.headers.get("retry-after", "")
        reset = answer.headers.get("x-ratelimit-reset", "")
        if retry_after.isdigit():
            wait = float(retry_after)
        elif answer.headers.get("x-ratelimit-remaining") == "0" and reset.isdigit():
            wait = float(reset) - now
        if answer.status in (403, 429):
            self._rate_strikes += 1
        if wait is None or wait <= 0:
            wait = RATE_LIMIT_DEFAULT_S * 2 ** max(0, self._rate_strikes - 1)
        wait = min(wait, RATE_LIMIT_MAX_S)
        until = now + wait
        if until > self.paused_until:
            self.paused_until = until
            _logger.warning(
                "merge watcher: GitHub rate limit (%s); pausing polls for %.0f s",
                answer.status,
                wait,
            )

    def _failed(self, record: Watched, now: float, why: str) -> None:
        record.errors += 1
        record.next_poll = now + min(POLL_INTERVAL_S * 2**record.errors, MAX_ERROR_BACKOFF_S)
        log = _logger.warning if record.errors == 1 else _logger.debug
        log("merge watcher: reading %s failed (%s); backing off", record.key, why)

    def _prune(self, now: float) -> None:
        for key, record in list(self.records.items()):
            if record.pending:
                continue
            settled = record.state in ("merged", "closed")
            if (settled and now - (record.settled_at or 0.0) > PRUNE_SETTLED_S) or (
                not settled and now - record.seen_at > PRUNE_OPEN_S
            ):
                del self.records[key]
                self._dirty = True

    async def _deliver(self) -> None:
        for record in list(self.records.values()):
            if not record.pending or not record.note:
                continue
            for session_id, slot in list(record.pending.items()):
                now = self._clock()
                if slot.get("next", 0.0) > now:
                    continue
                try:
                    outcome = await asyncio.wait_for(
                        self._post(session_id, record.note), POST_TIMEOUT_S
                    )
                except TimeoutError:
                    # It may have landed and be waiting on a runner or an approval:
                    # never post it twice (review, 2026-10-21).
                    _logger.warning(
                        "merge watcher: posting to %s took over %.0f s; not trying again",
                        session_id,
                        POST_TIMEOUT_S,
                    )
                    outcome = "drop"
                except Exception as exc:  # noqa: BLE001 (one session's failure is retried, not fatal)
                    _logger.warning("merge watcher: posting to %s raised %r", session_id, exc)
                    outcome = "retry"
                if outcome == "retry":
                    attempts = int(slot.get("attempts", 0)) + 1
                    if attempts >= MAX_POST_ATTEMPTS:
                        del record.pending[session_id]
                        record.notified.append(session_id)
                        _logger.warning(
                            "merge watcher: gave up telling %s about %s after %d tries",
                            session_id,
                            record.key,
                            attempts,
                        )
                    else:
                        backoff = min(POLL_INTERVAL_S * 2**attempts, MAX_POST_BACKOFF_S)
                        record.pending[session_id] = {"attempts": attempts, "next": now + backoff}
                else:
                    del record.pending[session_id]
                    record.notified.append(session_id)
                    _logger.info(
                        "merge watcher: %s note for %s %s",
                        record.key,
                        session_id,
                        "posted" if outcome == "posted" else "dropped",
                    )
                # After every try: a restart repeats at most the one post in flight.
                self._save()


# ── live wiring: the server's own routes, as the session's owner ───────────


class OwnerIdentity:
    """Request headers that act as a session's owner on this server's own routes."""

    def __init__(self) -> None:
        self._permissions: Any | None = None
        # A session's owner grant does not move; asked once per session per process.
        self._owners: dict[str, str] = {}

    def _permission_store(self) -> Any | None:
        if self._permissions is None:
            from omnigent.runtime import get_conversation_store
            from omnigent.stores.permission_store.sqlalchemy_store import (
                SqlAlchemyPermissionStore,
            )

            # The permission store lives in the server's main database, which
            # the conversation store opens first (omnigent/cli.py builds both
            # from the same URI); the engine is cached per URI, so this shares it.
            location = getattr(get_conversation_store(), "storage_location", None)
            if not isinstance(location, str):
                return None
            self._permissions = SqlAlchemyPermissionStore(location)
        return self._permissions

    def owner(self, session_id: str) -> str | None:
        from omnigent.server.routes._auth_helpers import get_session_owner_id

        if session_id in self._owners:
            return self._owners[session_id]
        try:
            owner = get_session_owner_id(session_id, self._permission_store())
        except Exception as exc:  # noqa: BLE001 (no owner means no post, never a crashed loop)
            _logger.warning("merge watcher: no owner for %s (%r)", session_id, exc)
            return None
        if owner:
            self._owners[session_id] = owner
        return owner

    async def headers(self, session_id: str) -> dict[str, str] | None:
        """The owner's credentials, or ``None`` when the server cannot act as them."""
        from omnigent.server.auth import (
            auth_mode,
            authentication_requires_identity,
            resolve_auth_header,
        )

        provider = auth.provider()
        mode = auth_mode(provider)
        if mode == "none":
            return {}
        owner = await asyncio.to_thread(self.owner, session_id)
        if mode == "header":
            if owner:
                return {resolve_auth_header(): owner}
            return None if authentication_requires_identity(provider) else {}
        if mode in ("oidc", "accounts") and owner:
            # The mint reads the account generation from the database: off the loop.
            token = await asyncio.to_thread(provider.mint_runner_token, owner, TOKEN_TTL_S)
            return {"authorization": f"Bearer {token}"} if token else None
        return None


async def _call(
    app: FastAPI, method: str, path: str, headers: dict[str, str], body: Any = None
) -> httpx.Response:
    """In process, or through ``AE_SELF_URL`` when set, as ``router._loopback`` does."""
    self_url = (os.environ.get("AE_SELF_URL") or "").rstrip("/")
    if self_url:
        async with httpx.AsyncClient(base_url=self_url, timeout=_LOOPBACK_TIMEOUT_S) as http:
            return await http.request(method, path, json=body, headers=headers)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://ae-loopback", timeout=_LOOPBACK_TIMEOUT_S
    ) as http:
        return await http.request(method, path, json=body, headers=headers)


async def discover_live(app: FastAPI, identity: OwnerIdentity, watched: set[str]) -> Scan:
    """Which pull requests the sessions track, read as each session's owner.

    Read: every session touched in the last :data:`ACTIVE_S`, and every session in
    *watched* whatever its age, so an old session still waiting on a merge is not
    lost (review, 2026-10-21). ``tracked_by_session`` reads at most
    ``prs.MAX_SESSIONS`` per call, so the rows go in batches of that size.
    """
    now = time.time()
    rows = [
        row
        for row in await asyncio.to_thread(sessions.list_top_level)
        if row.id in watched or now - (row.updated_at or 0) <= ACTIVE_S
    ]

    async def fetch(path: str) -> httpx.Response:
        session_id = path.split("/")[3]
        headers = await identity.headers(session_id)
        if headers is None:
            return httpx.Response(401)
        return await _call(app, "GET", path, headers)

    found: dict[str, list[str]] = {}
    failed: set[str] = set()
    for start in range(0, len(rows), prs.MAX_SESSIONS):
        batch = rows[start : start + prs.MAX_SESSIONS]
        by_url, errors, _scanned = await prs.tracked_by_session(batch, fetch)
        failed |= {str(row.get("session_id")) for row in errors}
        for url, refs in by_url.items():
            found.setdefault(url, []).extend(ref["id"] for ref in refs)
    return Scan(by_url=found, read=frozenset(row.id for row in rows) - failed)


_WORKSPACE_GONE_RE = re.compile(r"workspace_missing|workspace no longer exists", re.IGNORECASE)


def _in_container() -> bool:
    """Whether this server runs in a Docker or Podman container."""
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


async def _workspace_gone(app: FastAPI, headers: dict[str, str], session_id: str) -> bool:
    """True when the session names a workspace directory that is gone from this disk.

    A post to such a session makes the server relaunch the runner in the deleted
    worktree, and the user sees a "workspace no longer exists" pill. A local check
    only means something when the server shares the host's filesystem. On 2026-10-28
    the server ran in a container that mounts only ``/data`` and ships an empty
    ``/home``, so every host workspace read as missing and four notes into live
    sessions were dropped (/home/u and /home/u/worktrees/run). So the
    check is off in a container (the box's Docker deployment): the 4xx
    ``workspace_missing`` rule in :func:`post_live` is the guard there. It is also
    skipped for a path that is not absolute POSIX (``C:\\...``, relative). Anything
    else unknown (the read fails, no workspace field, a stat error other than "not
    found") is not gone: the post goes ahead as before.
    """
    try:
        response = await _call(app, "GET", f"/v1/sessions/{session_id}", headers)
        if response.status_code >= 300:
            return False
        workspace = response.json().get("workspace")
    except (httpx.HTTPError, ValueError, AttributeError):
        return False
    if not isinstance(workspace, str) or not workspace.strip():
        return False
    if not workspace.startswith("/") or _in_container():
        return False
    try:
        return not stat.S_ISDIR(os.stat(workspace).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False


#: Hops of ``ae.handoff_to`` a post follows, the same cap ``post_message`` has.
HANDOFF_HOPS = 5


def _handoff_target(session_id: str) -> str:
    """The lead that took over *session_id* (``ae.handoff_to``), following up to five hops.

    A session without the label, an unreadable one and a loop stop where they are. Lead handoff
    (2026-10-29): a merge or child wake note for the old lead must reach the new one, not wake a
    300k reference chat.
    """
    from omnigent.runtime import get_conversation_store

    try:
        store = get_conversation_store()
    except Exception:  # noqa: BLE001 (no store: post to where we are)
        return session_id
    seen = {session_id}
    current = session_id
    for _ in range(HANDOFF_HOPS):
        try:
            conv = store.get_conversation(current)
        except Exception:  # noqa: BLE001 (post to where we are)
            break
        target = labels.label(conv.labels if conv else None, labels.HANDOFF_TO)
        if not target or target in seen:
            break
        seen.add(target)
        current = target
    return current


async def post_live(
    app: FastAPI, identity: OwnerIdentity, session_id: str, text: str
) -> PostOutcome:
    """``POST /v1/sessions/{id}/events`` with *text* as a user message, as the owner.

    A session that handed off to a new lead is not posted to: its ``ae.handoff_to`` is followed.
    """
    session_id = await asyncio.to_thread(_handoff_target, session_id)
    headers = await identity.headers(session_id)
    if headers is None:
        _logger.warning(
            "merge watcher: cannot act as the owner of %s (no owner grant, or an auth mode "
            "that cannot mint a token); the note is dropped",
            session_id,
        )
        return "drop"
    if await _workspace_gone(app, headers, session_id):
        _logger.info(
            "merge watcher: the workspace of %s no longer exists; the note is dropped", session_id
        )
        return "drop"
    body = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }
    try:
        response = await _call(
            app,
            "POST",
            f"/v1/sessions/{session_id}/events",
            {**headers, "content-type": "application/json"},
            body,
        )
    except httpx.HTTPError as exc:
        _logger.warning("merge watcher: posting to %s failed: %r", session_id, exc)
        return "retry"
    if response.status_code < 300:
        return "posted"
    if response.status_code >= 400 and _WORKSPACE_GONE_RE.search(response.text):
        _logger.info(
            "merge watcher: %s has no workspace on the host; the note is dropped", session_id
        )
        return "drop"
    if response.status_code in (408, 409, 425, 429) or response.status_code >= 500:
        return "retry"
    _logger.warning(
        "merge watcher: %s refused the note: %s %s",
        session_id,
        response.status_code,
        response.text[:200],
    )
    return "drop"


def start(app: FastAPI) -> asyncio.Task[None] | None:
    """Start the watcher on the running loop, or ``None`` when it is off."""
    if not enabled():
        _logger.info("merge watcher off (%s=0)", ENABLED_ENV)
        return None
    settings = github.GitHubSettings.from_env()
    if not settings.configured:
        _logger.info("merge watcher off: the server has no GitHub token (%s)", github.TOKEN_ENV)
        return None
    identity = OwnerIdentity()
    watcher = MergeWatcher(
        settings,
        discover=partial(discover_live, app, identity),
        post=partial(post_live, app, identity),
        store=StateFile(state_path()),
    )
    return asyncio.get_running_loop().create_task(watcher.run(), name="ae-merge-watch")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the watcher for the life of the server."""
    task = start(app)
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


#: No routes; included for its lifespan (see the module docstring).
lifespan_router = APIRouter(lifespan=lifespan)

__all__ = [
    "MergeWatcher",
    "OwnerIdentity",
    "Scan",
    "StateFile",
    "Watched",
    "enabled",
    "lifespan_router",
    "note_text",
    "state_path",
]
