"""The auto-archive sweep: a chat left in ``reference`` leaves the session list on its own.

Added 2026-10-24. The owner closes a finished chat by setting ``ae.status`` to
``reference``; it then sat in their list until they archived it by hand. A sweep
every :data:`SWEEP_INTERVAL_S` archives it for them.

A session is archived when ALL hold: it is top level (no parent, kind
``default``), not archived, ``ae.status`` is ``reference``, it is idle
(:func:`ae_omni_policies.retention.live_state`: no running turn, sub-agent or
background shell; a ``failed`` session, runner gone, counts as idle, see
:func:`blocker`), no approval is outstanding, no gate is open, and it has been
in ``reference`` for :data:`DEFAULT_MINUTES` (``AE_AUTO_ARCHIVE_MINUTES``).

"In reference since" is the first sweep that saw the status, kept in
``/data/ae-auto-archive.json`` (``AE_AUTO_ARCHIVE_FILE`` overrides; empty or no
``/data`` means memory only). An entry is dropped when a sweep sees the session
outside reference, so a flip back resets the clock; the clock also restarts on
every sweep that finds the session busy (running, approval, open gate), and a
session is due only ``AE_AUTO_ARCHIVE_MINUTES`` after the later of that clock and
its last update, so a reference chat that just got a follow-up turn is left
alone. The first archive after deploy lands about ten minutes in. The row is read
again right before the archive and every condition is checked a second time.

Sessions with a grant besides the owner's are never archived: upstream limits
archive to the owner and it stops the runner for everyone. A session the sweep
archived and someone later unarchived is not archived again until its status
leaves reference and returns (``archived_by_sweep`` in the state file). After
:data:`MAX_FAILURES` failed attempts (no owner, 403, 404) a session is left
alone, with one WARNING, until its status changes. The list is paged to the end
(no row cap); a clock is dropped only for a session seen outside reference or
confirmed archived or gone by the store.

How it archives: ``PATCH /v1/sessions/{id}`` with ``{"archived": true}``, in
process through the same loopback the merge watcher uses, as the session's
owner. That is the route the web's Archive button calls, so the runtime stop,
pin cleanup and the sidebar's live updates all happen as for a manual archive.

Children follow their lead (2026-10-24). Once a lead is archived, by this sweep or by
hand, its still-open descendants are archived too: top-level sessions whose ``ae.orch``
chain leads to it, and sessions whose ``parent_conversation_id`` is it or a descendant
(a Codex reviewer started with ``sys_session_create`` stays a child). A child that is
busy (running, approval, open gate; a failed one counts as idle), or is shared, is left,
and its lead is looked at again next sweep. Only leads archived this pass or recorded in
the state file are walked, then dropped once nothing is left open under them. Unarchiving
a lead does not bring its children back. A lead archived by hand is found through the ``ae.orch``
parent of a listed member, or through the parent of a plain child that is not running (kind
``sub_agent``, at most :data:`MAX_CHILD_ROWS` rows read per sweep, each parent looked up once);
an archived row is not listed, so a lead with neither is not found. The walk goes down
through every child whatever its own outcome, so an idle grandchild under a busy or
already archived child is still archived. Known limit: after a restart the sweep re-walks
old archived leads once, and a child its user unarchived under an archived lead can be
archived again.

One line per archive is logged (id, title, time in reference).
``AE_AUTO_ARCHIVE=0`` turns it off. With several server workers only one sweeps:
each tries a non-blocking ``flock`` on ``<state file>.lock`` (``/tmp`` when memory
only) before every sweep and skips it without the lock; the holder re-reads the
state file before changing it. Startup never fails: any error while starting is
logged and leaves the sweep off.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI

from ae_omni_policies import auto_resume, labels, merge_watch, retention

_logger = logging.getLogger(__name__)

ENABLED_ENV = "AE_AUTO_ARCHIVE"
MINUTES_ENV = "AE_AUTO_ARCHIVE_MINUTES"
STATE_FILE_ENV = "AE_AUTO_ARCHIVE_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-auto-archive.json"

SWEEP_INTERVAL_S = 60.0
DEFAULT_MINUTES = 10.0
_TITLE_MAX = 80

#: Returns ``True`` when session *id* was archived.
Archive = Callable[[str], Awaitable[bool]]

DONE, SKIPPED, FAILED = "done", "skipped", "failed"


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the sweep runs (default on; ``0``, ``false``, ``off``, ``no`` turn it off)."""
    value = (environ if environ is not None else os.environ).get(ENABLED_ENV, "")
    return value.strip().lower() not in {"0", "false", "off", "no"}


def wait_seconds(environ: Mapping[str, str] | None = None) -> float:
    """``AE_AUTO_ARCHIVE_MINUTES`` in seconds; a missing or bad value is the default."""
    raw = ((environ if environ is not None else os.environ).get(MINUTES_ENV) or "").strip()
    try:
        minutes = float(raw) if raw else DEFAULT_MINUTES
    except ValueError:
        minutes = DEFAULT_MINUTES
    if not minutes >= 0:  # also catches nan
        minutes = DEFAULT_MINUTES
    return minutes * 60.0


def state_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """``AE_AUTO_ARCHIVE_FILE`` (empty: memory only), else ``/data`` file when ``/data`` exists."""
    env = environ if environ is not None else os.environ
    if STATE_FILE_ENV in env:
        raw = (env[STATE_FILE_ENV] or "").strip()
        return Path(raw) if raw else None  # set but empty: memory only, on purpose
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


#: Failed archive attempts after which a session is left alone until its status changes.
MAX_FAILURES = 3
#: A state-file time past this (year 5138) is corruption, not a clock.
_MAX_EPOCH = 1e11
_LOCK_FALLBACK = Path("/tmp") / "ae-auto-archive.lock"
_LIST_PAGE = 200
_LIST_MAX_PAGES = 5000
# Plain child rows read per sweep to find hand-archived leads; newest first, so a store
# past the cap loses its oldest children, the ones most likely archived already.
MAX_CHILD_ROWS = 500


def _sane_time(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and 0 <= number <= _MAX_EPOCH else None


class StateFile:
    """``{"since": {id: epoch first seen in reference}, "archived_by_sweep": [id]}``.

    Memory only when *path* is None. ``_memory`` always mirrors the last save so
    an unreadable or unwritable file falls back to what this process knows.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._memory: dict[str, float] = {}
        self._memory_archived: set[str] = set()

    def _read(self) -> tuple[dict[str, float], set[str]]:
        if self.path is None:
            return dict(self._memory), set(self._memory_archived)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return dict(self._memory), set(self._memory_archived)
        except (OSError, ValueError) as exc:
            _logger.warning(
                "auto-archive: unreadable state file %s (%r); starting clocks over", self.path, exc
            )
            return dict(self._memory), set(self._memory_archived)
        if not isinstance(raw, dict):
            return {}, set()
        since: dict[str, float] = {}
        seen = raw.get("since")
        if isinstance(seen, dict):
            for key, value in seen.items():
                number = _sane_time(value)
                if number is not None:
                    since[str(key)] = number
        archived: set[str] = set()
        listed = raw.get("archived_by_sweep")
        if isinstance(listed, list):
            archived = {item for item in listed if isinstance(item, str)}
        return since, archived

    def load(self) -> dict[str, float]:
        return self._read()[0]

    def load_archived(self) -> set[str]:
        return self._read()[1]

    def save(self, since: Mapping[str, float], archived: Collection[str] = ()) -> None:
        self._memory = dict(since)
        self._memory_archived = set(archived)
        if self.path is None:
            return
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"since": dict(since), "archived_by_sweep": sorted(archived)}, handle)
            os.replace(tmp, self.path)
            tmp = None
        except OSError as exc:
            _logger.warning(
                "auto-archive: cannot write %s (%r); clocks kept in memory", self.path, exc
            )
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)


class SweepLock:
    """A non-blocking exclusive ``flock``: only one server process sweeps."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: Any | None = None

    @classmethod
    def beside(cls, state_path: Path | None) -> SweepLock:
        if state_path is None:
            return cls(_LOCK_FALLBACK)
        return cls(state_path.with_name(state_path.name + ".lock"))

    def acquire(self) -> bool:
        """``True`` when this process holds (or just took) the lock."""
        if self._handle is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a")  # noqa: SIM115 (held for the process's life)
        except OSError as exc:
            _logger.warning(
                "auto-archive: cannot open lock file %s (%r); sweeping without a lock",
                self.path,
                exc,
            )
            return True
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            with contextlib.suppress(OSError):
                self._handle.close()
            self._handle = None


def list_all_top_level(store: Any) -> list[Any]:
    """Every non-archived top-level session of every user, paged to the end (no row cap)."""
    rows: list[Any] = []
    after: str | None = None
    for _ in range(_LIST_MAX_PAGES):
        page = store.list_conversations(
            limit=_LIST_PAGE,
            after=after,
            kind="default",
            include_archived=False,
            order="desc",
            sort_by="created_at",
            accessible_by=None,
        )
        rows.extend(page.data)
        if not page.has_more or not page.last_id:
            return rows
        after = page.last_id
    raise RuntimeError(f"session list did not end after {_LIST_MAX_PAGES} pages")


def has_other_grants(identity: merge_watch.OwnerIdentity, session_id: str) -> bool:
    """Whether anyone besides the session's owner holds a grant on it.

    Upstream restricts archive to the owner; a shared session's runner stops
    for everyone, so the sweep leaves it to its owner. Unknown counts as shared.
    """
    try:
        permissions = identity._permission_store()
        if permissions is None:
            return False
        grants, _ = permissions.list_for_session(session_id, limit=1000)
        owner = identity.owner(session_id)
    except Exception as exc:  # noqa: BLE001 (cannot tell, so leave it)
        _logger.warning("auto-archive: cannot read grants of %s (%r); left alone", session_id, exc)
        return True
    return any(g.user_id != owner for g in grants)


def _title(conv: Any) -> str:
    text = " ".join(str(getattr(conv, "title", "") or "").split())
    return text[:_TITLE_MAX]


def _updated(conv: Any) -> float:
    return _sane_time(getattr(conv, "updated_at", 0)) or 0.0


def _human(seconds: float) -> str:
    minutes = int(max(seconds, 0) // 60)
    return f"{minutes // 60}h{minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def is_candidate(conv: Any) -> bool:
    """Top level, not archived, in reference (the cheap checks, no store reads)."""
    return (
        getattr(conv, "parent_conversation_id", None) is None
        and getattr(conv, "kind", None) == "default"
        and not getattr(conv, "archived", False)
        and labels.status_of(conv.labels) == labels.REFERENCE
    )


def blocker(conv: Any, live: str, now: float) -> str | None:
    """Why *conv* must not be archived now (beyond the clock); ``None`` when it may be.

    2026-10-29: only a running session blocks. A ``failed`` one (runner gone, nothing
    running) is archived like an idle one; treating it as busy restarted its clock every
    sweep and left the children of an archived lead open forever. Exception, same day: a
    failed session that :func:`auto_resume.resumable_failure` says a host reconnect would
    still revive (``runner_disconnected`` or ``runner_failed_to_start``, touched within
    ``auto_resume.MAX_AGE_S``) waits, because both recoveries need it unarchived and a deploy
    would otherwise lose its interrupted turn. Past that window it is archived.
    """
    if live == retention.RUNNING:
        return live
    if live == retention.FAILED and auto_resume.resumable_failure(conv, now):
        return "resumable"
    if (getattr(conv, "pending_elicitation_count", 0) or 0) > 0:
        return "approval outstanding"
    if labels.label(conv.labels, labels.GATE) == labels.GATE_OPEN:
        return "open gate"
    return None


class Sweeper:
    """One sweep per :meth:`sweep`; :meth:`run` repeats it."""

    def __init__(
        self,
        *,
        store: Any,
        archive: Archive,
        state: StateFile,
        wait_s: float,
        list_rows: Callable[[], Any] | None = None,
        live: Callable[[Any, Any], Awaitable[str]] = retention.live_state,
        clock: Callable[[], float] = time.time,
        shared: Callable[[str], bool] | None = None,
        lock: SweepLock | None = None,
    ) -> None:
        self._store = store
        self._archive = archive
        self._state = state
        self._wait_s = wait_s
        self._list_rows = list_rows or partial(list_all_top_level, store)
        self._live = live
        self._clock = clock
        self._shared = shared
        self._lock = lock
        self._since = state.load()
        self._archived = state.load_archived()
        self._failures: dict[str, int] = {}
        # Archived leads whose descendants still need a look; filled from the state file on
        # the first sweep, so a restart picks up where it stopped.
        self._watch: set[str] | None = None
        self._child_failures: dict[str, int] = {}

    @property
    def since(self) -> dict[str, float]:
        return dict(self._since)

    @property
    def archived_by_sweep(self) -> set[str]:
        return set(self._archived)

    async def _save(self) -> None:
        await asyncio.to_thread(self._state.save, dict(self._since), set(self._archived))

    async def _forget_unseen(self, seen: Mapping[str, Any]) -> bool:
        """Drop records of sessions the list no longer shows, once the store confirms."""
        changed = False
        for session_id in set(self._since) | self._archived | set(self._failures):
            if session_id in seen:
                continue
            conv = await asyncio.to_thread(self._store.get_conversation, session_id)
            gone = conv is None
            if session_id in self._archived:
                # Still archived: keep the record so an unarchive is noticed.
                if gone or (not getattr(conv, "archived", False)):
                    self._archived.discard(session_id)
                    changed = True
            elif gone or not is_candidate(conv):
                changed = self._since.pop(session_id, None) is not None or changed
                self._failures.pop(session_id, None)
        return changed

    async def sweep(self) -> list[str]:
        """Start, reset and drop clocks, archive what is due; returns the ids archived."""
        if self._lock is not None and not self._lock.acquire():
            return []
        now = self._clock()
        if self._state.path is not None:
            # Under the lock the file is ours; take its view before changing it.
            self._since = await asyncio.to_thread(self._state.load)
            self._archived = await asyncio.to_thread(self._state.load_archived)
        rows = await asyncio.to_thread(self._list_rows)
        seen = {row.id: row for row in rows}
        changed = False
        in_reference: dict[str, Any] = {}
        for session_id, row in seen.items():
            if is_candidate(row):
                if session_id not in self._archived:
                    in_reference[session_id] = row
                continue
            # Seen, and no longer in reference: the clock, the unarchive record
            # and the failure count all start over if it returns.
            self._failures.pop(session_id, None)
            if self._since.pop(session_id, None) is not None:
                changed = True
            if session_id in self._archived:
                self._archived.discard(session_id)
                changed = True
        for session_id in [k for k in self._since if k in self._archived]:
            del self._since[session_id]
            changed = True
        if await self._forget_unseen(seen):
            changed = True
        due: list[tuple[str, Any, float]] = []
        for session_id, row in in_reference.items():
            try:
                why = blocker(row, await self._live(self._store, row), self._clock())
            except Exception:
                _logger.exception("auto-archive: cannot read the state of %s", session_id)
                continue
            start = self._since.get(session_id)
            if why is not None or start is None or start > now:
                # Busy (a resumed chat), first sight, or a clock that ran backwards.
                self._since[session_id] = now
                changed = True
                continue
            if now - max(start, min(_updated(row), now)) >= self._wait_s:
                due.append((session_id, row, now - start))
        if changed:
            await self._save()
        archived: list[str] = []
        for session_id, row, sat in due:
            if self._failures.get(session_id, 0) >= MAX_FAILURES:
                continue
            try:
                outcome = await self._archive_one(session_id, row, sat)
            except Exception:
                _logger.exception("auto-archive: %s failed", session_id)
                outcome = FAILED
            if outcome == DONE:
                archived.append(session_id)
            elif outcome == FAILED:
                count = self._failures.get(session_id, 0) + 1
                self._failures[session_id] = count
                if count == MAX_FAILURES:
                    _logger.warning(
                        "auto-archive: gave up on %s after %d failed attempts; set its status to "
                        "something other than reference, or archive it by hand",
                        session_id,
                        count,
                    )
        if archived:
            for session_id in archived:
                self._since.pop(session_id, None)
                self._archived.add(session_id)
            await self._save()
        try:
            await self._with_their_lead(seen, set(archived))
        except Exception:
            _logger.exception("auto-archive: the pass over children failed")
        return archived

    def _children_of(self, parent_id: str) -> list[Any]:
        """Open sessions of any kind whose parent is *parent_id*, paged to the end."""
        rows: list[Any] = []
        after: str | None = None
        for _ in range(_LIST_MAX_PAGES):
            page = self._store.list_conversations(
                limit=_LIST_PAGE,
                after=after,
                kind=None,
                parent_conversation_id=parent_id,
                include_archived=False,
                order="desc",
                sort_by="created_at",
                accessible_by=None,
            )
            rows.extend(page.data)
            if not page.has_more or not page.last_id:
                break
            after = page.last_id
        return rows

    def _idle_plain_children(self) -> list[Any]:
        """Open sub-agent rows whose status is not ``running``, newest MAX_CHILD_ROWS read.

        ``failed`` counts, and so does ``waiting``; a waiting child is still left alone later,
        because :func:`retention.live_state` reads it as running.
        """
        rows: list[Any] = []
        after: str | None = None
        # Bound the rows read, not only the rows kept: busy sub-agents would page on otherwise.
        for _ in range(-(-MAX_CHILD_ROWS // _LIST_PAGE)):
            page = self._store.list_conversations(
                limit=_LIST_PAGE,
                after=after,
                kind="sub_agent",
                include_archived=False,
                order="desc",
                sort_by="created_at",
                accessible_by=None,
            )
            rows.extend(
                row for row in page.data if getattr(row, "live_status", None) != retention.RUNNING
            )
            if not page.has_more or not page.last_id or len(rows) >= MAX_CHILD_ROWS:
                break
            after = page.last_id
        return rows[:MAX_CHILD_ROWS]

    async def _with_their_lead(self, seen: Mapping[str, Any], fresh: set[str]) -> None:
        """Archive the idle descendants of archived leads; keep watching leads with any left."""
        if self._watch is None:
            self._watch = set(self._archived)
        watch = self._watch | fresh
        # A lead archived by hand is not in the list; a listed member's ae.orch parent names it.
        by_parent: dict[str, list[Any]] = {}
        for row in seen.values():
            parent = labels.orch_parent(row.labels)
            if parent is not None:
                by_parent.setdefault(parent, []).append(row)
        for parent in by_parent:
            if parent in seen or parent in watch:
                continue
            conv = await asyncio.to_thread(self._store.get_conversation, parent)
            if conv is not None and getattr(conv, "archived", False):
                watch.add(parent)
        # A lead whose only children are plain child sessions has no listed member; the
        # parent of an idle child names it. One lookup per distinct parent per sweep.
        looked: set[str] = set()
        for row in await asyncio.to_thread(self._idle_plain_children):
            parent = getattr(row, "parent_conversation_id", None)
            if parent is None or parent in looked or parent in seen or parent in watch:
                continue
            looked.add(parent)
            conv = await asyncio.to_thread(self._store.get_conversation, parent)
            if conv is not None and getattr(conv, "archived", False):
                watch.add(parent)
        unfinished: set[str] = set()
        visited = set(watch)
        queue = [(lead, lead) for lead in sorted(watch)]
        while queue:
            lead, root = queue.pop()
            kids = {row.id: row for row in by_parent.get(lead, [])}
            for row in await asyncio.to_thread(self._children_of, lead):
                kids.setdefault(row.id, row)
            for child_id, row in kids.items():
                if child_id in visited:
                    continue
                visited.add(child_id)
                outcome = await self._archive_child(child_id, row, lead)
                # Descend whatever the child's own outcome (2026-10-24): an idle grandchild
                # under a busy or already archived child still goes.
                queue.append((child_id, root))
                if outcome == FAILED:
                    unfinished.add(root)
        self._watch = {lead for lead in watch if lead in unfinished}

    async def _archive_child(self, child_id: str, listed: Any, lead: str) -> str:
        """Archive one idle child of an archived lead; ``FAILED`` means look again later."""
        if self._child_failures.get(child_id, 0) >= MAX_FAILURES:
            return SKIPPED
        conv = await asyncio.to_thread(self._store.get_conversation, child_id)
        if conv is None or getattr(conv, "archived", False):
            return SKIPPED
        try:
            why = blocker(conv, await self._live(self._store, conv), self._clock())
        except Exception:
            _logger.exception("auto-archive: cannot read the state of child %s", child_id)
            return FAILED
        if why is not None:
            _logger.debug("auto-archive: child %s stays (%s)", child_id, why)
            return FAILED
        if self._shared is not None and await asyncio.to_thread(self._shared, child_id):
            _logger.debug("auto-archive: child %s is shared; left to its owner", child_id)
            return SKIPPED
        if not await self._archive(child_id):
            count = self._child_failures.get(child_id, 0) + 1
            self._child_failures[child_id] = count
            if count == MAX_FAILURES:
                _logger.warning(
                    "auto-archive: gave up on child %s after %d failed attempts", child_id, count
                )
            return FAILED
        _logger.info(
            "auto-archive: archived child %s %r with its lead %s",
            child_id,
            _title(conv) or _title(listed),
            lead,
        )
        return DONE

    async def _archive_one(self, session_id: str, listed: Any, sat: float) -> str:
        # Read the row again: the list may be a moment old and the session may have been reopened.
        conv = await asyncio.to_thread(self._store.get_conversation, session_id)
        if conv is None or not is_candidate(conv):
            return SKIPPED
        now = self._clock()
        if now - min(_updated(conv), now) < self._wait_s:
            return SKIPPED  # a turn landed after the list was read
        why = blocker(conv, await self._live(self._store, conv), self._clock())
        if why is not None:
            _logger.debug("auto-archive: %s stays (%s)", session_id, why)
            return SKIPPED
        if self._shared is not None and await asyncio.to_thread(self._shared, session_id):
            _logger.debug("auto-archive: %s is shared; left to its owner", session_id)
            return SKIPPED
        if not await self._archive(session_id):
            return FAILED
        _logger.info(
            "auto-archive: archived %s %r after %s in reference",
            session_id,
            _title(conv) or _title(listed),
            _human(sat),
        )
        return DONE

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self.sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception("auto-archive: sweep failed")
                await asyncio.sleep(SWEEP_INTERVAL_S)
        finally:
            if self._lock is not None:
                self._lock.release()


async def archive_live(app: FastAPI, identity: merge_watch.OwnerIdentity, session_id: str) -> bool:
    """``PATCH /v1/sessions/{id}`` ``{"archived": true}``, in process, as the session's owner."""
    headers = await identity.headers(session_id)
    if headers is None:
        _logger.warning(
            "auto-archive: cannot act as the owner of %s; left in the list", session_id
        )
        return False
    try:
        response = await merge_watch._call(
            app,
            "PATCH",
            f"/v1/sessions/{session_id}",
            {**headers, "content-type": "application/json"},
            {"archived": True},
        )
    except httpx.HTTPError as exc:
        _logger.warning("auto-archive: archiving %s failed: %r", session_id, exc)
        return False
    if response.status_code < 300:
        return True
    _logger.warning(
        "auto-archive: %s refused the archive: %s %s",
        session_id,
        response.status_code,
        response.text[:200],
    )
    return False


def start(app: FastAPI) -> asyncio.Task[None] | None:
    """Start the sweep on the running loop, or ``None`` when it is off or cannot start."""
    if not enabled():
        _logger.info("auto-archive off (%s=0)", ENABLED_ENV)
        return None
    try:
        from omnigent.runtime import get_conversation_store

        identity = merge_watch.OwnerIdentity()
        path = state_path()
        sweeper = Sweeper(
            store=get_conversation_store(),
            archive=partial(archive_live, app, identity),
            state=StateFile(path),
            wait_s=wait_seconds(),
            shared=partial(has_other_grants, identity),
            lock=SweepLock.beside(path),
        )
        return asyncio.get_running_loop().create_task(sweeper.run(), name="ae-auto-archive")
    except Exception:
        # Must never stop the server starting; the sweep stays off until a restart.
        _logger.exception("auto-archive: could not start; the sweep is off until a restart")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the sweep for the life of the server."""
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
    "StateFile",
    "SweepLock",
    "Sweeper",
    "enabled",
    "lifespan_router",
    "state_path",
    "wait_seconds",
]
