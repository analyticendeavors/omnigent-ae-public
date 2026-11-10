"""The queued Compact: the server saves a chat's notes and then compacts it, by itself.

Added 2026-11-06. Compact used to be queued in the open page (``aeCompactPlan.ts``), so
leaving the chat cancelled it and a long silent think read as a turn end. On 2026-11-06
one chat lost two of three attempts that way: the save prompt went into a turn that was
still running, and a second plan died with a "you left the chat" toast. Reid's decision:
"if I queue compact, and leave chat, it should continue to finish compacting". The plan now
lives here, one per chat, in ``/data/ae-compact-queue.json`` (``AE_COMPACT_QUEUE_FILE``
overrides; in memory when neither exists, as in tests and a bare run), so it survives
leaving the chat, a reload, closing the tab and a deploy.

The turn-end signal is the claude-native ``Stop`` hook edge: an ``external_session_status``
``idle`` carrying ``turn_completed=True``, or a ``failed`` status. The server never stores
that flag, so the fork patch ``omnigent/server/ae_turn_edges.py`` hands every status edge,
interrupt and completed idle to :class:`EdgeBook`. A bare ``idle`` (PTY quiet, status file)
is only "an edge happened" and never closes a turn: that is exactly the guess that failed.
Prompt items arrive through the existing item observers (``omnigent.server.item_observers``).

The steps, each written to the state file before it acts (so a restart repeats nothing):

1. ``queued``: wait until the chat is settled (a real turn end after the last turn start,
   nothing pending, :data:`STABLE_S` without an edge). A chat the server has seen no turn
   end for since it started (a deploy on an idle chat) is settled after
   :data:`UNKNOWN_QUIET_S` of quiet with a live status of ``idle``.
2. ``preparing``: post :data:`~ae_omni_policies.continuation.COMPACT_PREP_TEXT` as the chat's
   owner. The chat answers; once a reply to the prompt is in and a Stop has come since the
   send (in either order: the forwarder can post a fast turn's Stop before its items),
   the chat must settle again before the compact.
3. ``compacting``: post ``{"type": "compact"}``, drop the plan, and snooze the dock card's
   suggestion when the click came from it.

A failure deletes the plan and keeps a message for the page to show once
(:meth:`CompactQueue.state`, :meth:`CompactQueue.ack`); nothing compacts by accident.
Only claude-native chats queue: the other harnesses stamp no turn-completed edge yet.

Posting is in process through ``httpx.ASGITransport`` as the owner, like the merge watcher
(:mod:`ae_omni_policies.merge_watch`), and the prompt is a recognized continuation
(:mod:`ae_omni_policies.continuation`) so the slot cap, the rabbit hole and the fork
classifier let it through. The loop ticks once a second over the chats that have a plan and
sleeps when there are none. ``AE_COMPACT_QUEUE=0`` turns it all off. One driver per server
process, as for the merge watcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import APIRouter, FastAPI

from ae_omni_policies import lead_handoff, merge_watch
from ae_omni_policies.continuation import COMPACT_PREP_TEXT

_logger = logging.getLogger(__name__)

ENABLED_ENV = "AE_COMPACT_QUEUE"
STATE_FILE_ENV = "AE_COMPACT_QUEUE_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-compact-queue.json"

#: Seconds between ticks while any plan exists.
TICK_S = 1.0
#: A chat must see no edge for this long before a step goes ahead (turns hand off to queued work).
STABLE_S = 2.0
#: The same, for a chat the server has seen no turn end for since it started.
UNKNOWN_QUIET_S = 60.0
#: The save prompt never started a turn within this long: give up.
START_LIMIT_S = 30.0
#: The save turn never ended within this long: give up.
PREP_LIMIT_S = 900.0
#: A plan still queued after this long: give up (a safety net against a lost Stop).
PLAN_MAX_AGE_S = 86400.0
#: A stored failure is kept this long if the page never acknowledges it.
FAILURE_KEEP_S = 7 * 86400.0
#: Sessions the edge book remembers; the oldest are dropped.
BOOK_MAX_SESSIONS = 5000
#: Slack when matching a stored time against an item's time.
CLOCK_SLACK_S = 2.0
_LOOPBACK_BODY_MAX = 200

Phase = Literal["queued", "preparing", "compacting"]

MSG_NEVER_STARTED = "Compact cancelled: the chat never started saving its notes."
MSG_TOO_LONG = "Compact cancelled: the chat took too long to save its notes."
MSG_NOT_CLEAN = (
    "Compact cancelled: the chat did not finish saving its notes cleanly, "
    "so nothing was compacted."
)
MSG_TOO_OLD = "Compact cancelled: the chat did not reach a turn end within a day."
MSG_RESTART = (
    "The server restarted just after the compact went out. "
    "If this chat still shows its full size, click Compact again."
)
MSG_RESTART_PREPARING = (
    "The server restarted while the chat was saving its notes, so nothing was compacted. "
    "Click Compact again."
)


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether queued Compact runs (default on; ``0``, ``false``, ``off``, ``no`` turn it off)."""
    value = (environ if environ is not None else os.environ).get(ENABLED_ENV, "")
    return value.strip().lower() not in {"0", "false", "off", "no"}


class AlreadyQueued(Exception):
    """A plan already exists for the chat; ``phase`` is where it stands."""

    def __init__(self, phase: Phase) -> None:
        super().__init__(f"a compact is already queued ({phase})")
        self.phase = phase


class CompactSending(Exception):
    """The compact is already going out and cannot be cancelled."""


# ── the edge book ───────────────────────────────────────────────────────


@dataclass
class SessionEdges:
    """What the server has seen of one chat's turns since it started."""

    last_open_seq: int = 0
    #: The newest completed Stop edge or failed status.
    last_end_seq: int = 0
    last_failed_seq: int = 0
    last_interrupt_seq: int = 0
    #: Monotonic time of the newest edge of any kind, or of the newest item while a plan
    #: watches the chat, or ``None``.
    last_edge_mono: float | None = None


@dataclass
class Watch:
    """The items of a chat while a plan waits on its save prompt."""

    send_seq: int
    prep_item_seq: int | None = None
    prep_item_id: str | None = None
    reply_seq: int | None = None
    error_seq: int | None = None


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _text_of(item: Any) -> str:
    parts = _field(_field(item, "data"), "content") or []
    return "".join(str(_field(part, "text") or "") for part in parts).strip()


class EdgeBook:
    """Turn edges and watched items, in one process-wide order (``seq``).

    Called from ``_publish_status``, which can run off the event loop, so every method
    takes the lock and does O(1) dict work.
    """

    def __init__(self, mono: Callable[[], float] = time.monotonic) -> None:
        self._mono = mono
        self._lock = threading.Lock()
        self._seq = 0
        self._sessions: OrderedDict[str, SessionEdges] = OrderedDict()
        self._watches: dict[str, Watch] = {}

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def _entry(self, session_id: str) -> SessionEdges:
        entry = self._sessions.get(session_id)
        if entry is None:
            entry = self._sessions[session_id] = SessionEdges()
            while len(self._sessions) > BOOK_MAX_SESSIONS:
                self._sessions.popitem(last=False)
        else:
            self._sessions.move_to_end(session_id)
        return entry

    def note(self, session_id: str, status: str, *, turn_completed: bool = False) -> None:
        """Record one edge: ``running``/``waiting`` open a turn, a completed ``idle`` or a
        ``failed`` ends it, ``interrupted`` ends and cancels it; a bare ``idle`` is only an
        edge."""
        with self._lock:
            self._seq += 1
            entry = self._entry(session_id)
            entry.last_edge_mono = self._mono()
            if status in ("running", "waiting"):
                entry.last_open_seq = self._seq
            elif status == "idle" and turn_completed:
                entry.last_end_seq = self._seq
            elif status == "failed":
                entry.last_end_seq = self._seq
                entry.last_failed_seq = self._seq
            elif status == "interrupted":
                # An interrupt ends the turn too; a plan in preparing still reads it as a cancel.
                entry.last_end_seq = self._seq
                entry.last_interrupt_seq = self._seq

    def on_edge(self, edge: Any) -> None:
        """The turn edge observer (``omnigent.server.ae_turn_edges``)."""
        self.note(edge.session_id, edge.status, turn_completed=bool(edge.turn_completed))

    def info(self, session_id: str) -> SessionEdges:
        """A copy of what is known about the chat (all zeros when nothing was seen)."""
        with self._lock:
            entry = self._sessions.get(session_id)
            return SessionEdges(**asdict(entry)) if entry else SessionEdges()

    def watch(self, session_id: str, send_seq: int) -> None:
        """Start reading the chat's items for the save prompt and its reply."""
        with self._lock:
            self._watches[session_id] = Watch(send_seq=send_seq)

    def unwatch(self, session_id: str) -> None:
        with self._lock:
            self._watches.pop(session_id, None)

    def watched(self, session_id: str) -> Watch | None:
        with self._lock:
            watch = self._watches.get(session_id)
            return Watch(**asdict(watch)) if watch else None

    def note_items(self, session_id: str, items: Sequence[Any]) -> None:
        """Stamp the save prompt, the first reply after it, and the first error after it."""
        with self._lock:
            watch = self._watches.get(session_id)
            if watch is None:
                return
            if items:
                # Items count as activity for the quiet check: the turn's running edge
                # comes from the runner, not the forwarder, and can land just after them.
                self._entry(session_id).last_edge_mono = self._mono()
            for item in items:
                self._seq += 1
                kind = _field(item, "type")
                data = _field(item, "data")
                if kind == "message":
                    role = _field(data, "role")
                    if (
                        role == "user"
                        and watch.prep_item_seq is None
                        and _text_of(item) == COMPACT_PREP_TEXT
                    ):
                        watch.prep_item_seq = self._seq
                        watch.prep_item_id = _field(item, "id")
                    elif (
                        role == "assistant"
                        and watch.prep_item_seq is not None
                        and watch.reply_seq is None
                    ):
                        watch.reply_seq = self._seq
                elif (
                    kind == "error"
                    and watch.prep_item_seq is not None
                    and watch.error_seq is None
                    and _field(data, "level") != "info"
                ):
                    watch.error_seq = self._seq

    async def on_items(self, session_id: str, items: Sequence[Any]) -> None:
        """The item observer (``omnigent.server.item_observers``), run as a task on the loop."""
        self.note_items(session_id, items)


# ── the state file ──────────────────────────────────────────────────────


@dataclass
class Plan:
    """One chat's queued Compact."""

    session_id: str
    phase: Phase
    snooze: bool = False
    created_at: float = 0.0
    #: The process that wrote the seq fields below; another process cannot read them.
    boot_id: str = ""
    send_seq: int | None = None
    prepare_sent_at: float | None = None
    send_tail_item_id: str | None = None
    compact_sent_at: float | None = None


@dataclass
class Failure:
    """Why a plan ended, kept until the page has shown it once."""

    id: str
    message: str
    at: float


class StateFile:
    """The plans and failures as one JSON file written atomically; in memory without a path."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._memory: dict[str, Any] = {}

    def load(self) -> tuple[dict[str, Plan], dict[str, Failure]]:
        """Every plan and failure; an unreadable file is logged and read as empty."""
        body: Any
        if self.path is None:
            body = self._memory
        else:
            try:
                body = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}, {}
            except (OSError, ValueError) as exc:
                _logger.warning(
                    "compact queue: could not read %s (%s); starting empty, so a queued "
                    "compact is forgotten",
                    self.path,
                    exc,
                )
                return {}, {}
        plans: dict[str, Plan] = {}
        failures: dict[str, Failure] = {}
        if not isinstance(body, dict):
            return plans, failures
        for key, raw in (body.get("plans") or {}).items():
            with contextlib.suppress(TypeError):
                plan = Plan(**raw)
                if plan.phase in ("queued", "preparing", "compacting"):
                    plans[str(key)] = plan
        for key, raw in (body.get("failures") or {}).items():
            with contextlib.suppress(TypeError):
                failures[str(key)] = Failure(**raw)
        return plans, failures

    def save(self, plans: Mapping[str, Plan], failures: Mapping[str, Failure]) -> bool:
        """Replace the file; a failed write is logged, never raised. ``True`` when it landed."""
        body = {
            "version": 1,
            "plans": {key: asdict(plan) for key, plan in plans.items()},
            "failures": {key: asdict(failure) for key, failure in failures.items()},
        }
        if self.path is None:
            self._memory = json.loads(json.dumps(body))
            return True
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-compact-queue-", dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            return True
        except OSError as exc:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            _logger.warning(
                "compact queue: could not write %s (%s); check the folder is writable. "
                "Until it is, a restart may forget a queued compact or send a step twice.",
                self.path,
                exc,
            )
            return False


def state_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """``AE_COMPACT_QUEUE_FILE``, else ``/data/ae-compact-queue.json`` when ``/data`` exists."""
    raw = ((environ if environ is not None else os.environ).get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


# ── the driver ──────────────────────────────────────────────────────────

#: ``post(session_id, body)`` -> ``(status, first 200 characters of the answer)``; 0 on no answer.
PostFn = Callable[[str, dict[str, Any]], Awaitable[tuple[int, str]]]
#: ``snooze(session_id)`` -> ``lead_handoff.snooze``'s answer.
SnoozeFn = Callable[[str], Awaitable[tuple[int, str, str] | int]]


def prepare_body() -> dict[str, Any]:
    """The save prompt as a user message, the shape ``merge_watch.post_live`` sends."""
    return {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": COMPACT_PREP_TEXT}]},
    }


def compact_body() -> dict[str, Any]:
    """The compact event, the shape the page's ``store.compact()`` sends."""
    return {"type": "compact", "data": {}}


def _reason(status: int, text: str) -> str:
    return (text or "").strip()[:_LOOPBACK_BODY_MAX] or f"HTTP {status}"


class CompactQueue:
    """The plans, and the tick that moves them along. All collaborators are injected."""

    def __init__(
        self,
        *,
        store: StateFile,
        book: EdgeBook,
        post: PostFn,
        snooze: SnoozeFn,
        pending: Callable[[str], Sequence[str]],
        live_status: Callable[[str], str | None],
        list_items: Callable[..., Sequence[Any]],
        clock: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        boot_id: str | None = None,
    ) -> None:
        self.store = store
        self.book = book
        self._post = post
        self._snooze = snooze
        self._pending = pending
        self._live_status = live_status
        self._list_items = list_items
        self._clock = clock
        self._mono = mono
        self.boot_id = boot_id or uuid.uuid4().hex
        self._boot_mono = mono()
        self.wake = asyncio.Event()
        self.plans, self.failures = store.load()
        # When each plan began waiting in this process (for the unknown-state quiet rule).
        self._since: dict[str, float] = dict.fromkeys(self.plans, self._boot_mono)
        self._prune_failures()
        self._fail_preparing_after_restart()

    def _fail_preparing_after_restart(self) -> None:
        """A plan loaded in ``preparing``: its save turn's state died with the old process."""
        for plan in [p for p in self.plans.values() if p.phase == "preparing"]:
            self.plans.pop(plan.session_id, None)
            self._since.pop(plan.session_id, None)
            self._store_failure(plan.session_id, MSG_RESTART_PREPARING)

    # ── the page's side ────────────────────────────────────────────────

    def state(self, session_id: str) -> tuple[Phase | None, Failure | None]:
        """The chat's phase (or ``None``) and any failure not yet acknowledged."""
        plan = self.plans.get(session_id)
        return (plan.phase if plan else None), self.failures.get(session_id)

    def queue(self, session_id: str, *, snooze: bool = False) -> None:
        """Add a plan for the chat. Raises :class:`AlreadyQueued` when it has one."""
        existing = self.plans.get(session_id)
        if existing is not None:
            raise AlreadyQueued(existing.phase)
        self.failures.pop(session_id, None)
        self.plans[session_id] = Plan(
            session_id=session_id, phase="queued", snooze=snooze, created_at=self._clock()
        )
        self._since[session_id] = self._mono()
        self._save()
        self.wake.set()

    def cancel(self, session_id: str) -> None:
        """Drop a queued or preparing plan (also when there is none). A compact already
        going out raises :class:`CompactSending`."""
        plan = self.plans.get(session_id)
        if plan is None:
            return
        if plan.phase == "compacting":
            raise CompactSending
        self._drop(plan)

    def ack(self, session_id: str, failure_id: str) -> None:
        """The page showed the failure: forget it."""
        failure = self.failures.get(session_id)
        if failure is not None and failure.id == failure_id:
            del self.failures[session_id]
            self._save()

    # ── the loop ───────────────────────────────────────────────────────

    async def run(self) -> None:
        """Tick while a plan exists; sleep until :meth:`queue` wakes it otherwise."""
        while True:
            if not self.plans:
                self.wake.clear()
                await self.wake.wait()
            with contextlib.suppress(Exception):
                await self.tick()
            with contextlib.suppress(asyncio.TimeoutError):
                self.wake.clear()
                await asyncio.wait_for(self.wake.wait(), TICK_S)

    async def tick(self) -> None:
        """Move every plan one step."""
        self._prune_failures()
        for session_id in list(self.plans):
            plan = self.plans.get(session_id)
            if plan is None:
                continue
            try:
                await self._step(plan)
            except Exception as exc:
                _logger.exception("compact queue: step failed for %s", session_id)
                if self.plans.get(session_id) is plan and plan.phase != "compacting":
                    self._fail(plan, f"Compact cancelled: something went wrong ({exc!r}).")

    # ── one step ───────────────────────────────────────────────────────

    async def _step(self, plan: Plan) -> None:
        now = self._clock()
        if plan.phase == "queued":
            if now - plan.created_at > PLAN_MAX_AGE_S:
                self._fail(plan, MSG_TOO_OLD)
                return
            if await self._settled(plan) and self._current(plan):
                await self._send_prepare(plan)
        elif plan.phase == "preparing":
            await self._check_preparing(plan, now)
        else:
            await self._after_restart_compacting(plan)

    async def _tail_item_id(self, session_id: str) -> str | None:
        rows = await asyncio.to_thread(self._list_items, session_id, order="desc", limit=1)
        return rows[0].id if rows else None

    def _current(self, plan: Plan) -> bool:
        """Whether *plan* is still the chat's plan (a cancel or re-queue may have replaced it)."""
        return self.plans.get(plan.session_id) is plan

    async def _send_prepare(self, plan: Plan) -> None:
        sid = plan.session_id
        if not self._current(plan):
            return
        tail = await self._tail_item_id(sid)
        if not self._current(plan):
            return
        plan.phase = "preparing"
        plan.boot_id = self.boot_id
        plan.send_seq = self.book.seq
        plan.prepare_sent_at = self._clock()
        plan.send_tail_item_id = tail
        if not self._save():
            # Write before act: without the write a restart could repeat the step.
            plan.phase = "queued"
            plan.boot_id = ""
            plan.send_seq = plan.prepare_sent_at = plan.send_tail_item_id = None
            return
        self.book.watch(sid, plan.send_seq)
        status, text = await self._post(sid, prepare_body())
        if not self._current(plan):
            return
        if status == 0 or status >= 300:
            self._fail(
                plan,
                "Compact cancelled: could not ask the chat to save its notes "
                f"({_reason(status, text)}).",
            )

    async def _check_preparing(self, plan: Plan, now: float) -> None:
        sent_at = plan.prepare_sent_at or plan.created_at
        if plan.boot_id != self.boot_id or plan.send_seq is None:
            self._fail(plan, MSG_RESTART_PREPARING)
            return
        found = self._read_book(plan)
        if found == "unclean":
            self._fail(plan, MSG_NOT_CLEAN)
            return
        if now - sent_at > PREP_LIMIT_S:
            self._fail(plan, MSG_TOO_LONG)
            return
        if found == "none" and now - sent_at > START_LIMIT_S:
            self._fail(plan, MSG_NEVER_STARTED)
            return
        if found == "ended" and await self._settled(plan) and self._current(plan):
            await self._send_compact(plan)

    def _read_book(self, plan: Plan) -> str:
        """``unclean``, ``none`` (no turn started), ``started``, or ``ended`` (save turn over)."""
        sid = plan.session_id
        watch = self.book.watched(sid)
        if watch is None:
            # The watch is lost only if the book was reset; read the store instead.
            return "started"
        info = self.book.info(sid)
        send_seq = plan.send_seq or 0
        if info.last_interrupt_seq > send_seq or watch.error_seq is not None:
            return "unclean"
        # Turn ends are measured from the send, never from the items: each forwarder poll
        # reads the transcript before the hook file, so a turn that finishes between the two
        # reads posts its Stop (or StopFailure) before its own prompt and reply items.
        if info.last_failed_seq > send_seq:
            return "unclean"
        prep = watch.prep_item_seq
        if prep is None:
            return "started" if info.last_open_seq > send_seq else "none"
        if watch.reply_seq is not None and info.last_end_seq > send_seq:
            # ``_settled`` still needs no turn opened since that end, and 2 s of quiet.
            return "ended"
        return "started"

    async def _send_compact(self, plan: Plan) -> None:
        sid = plan.session_id
        if not self._current(plan):
            return
        plan.phase = "compacting"
        plan.compact_sent_at = self._clock()
        if not self._save():
            # Write before act: a compact sent without it could be sent again after a restart.
            plan.phase = "preparing"
            plan.compact_sent_at = None
            return
        self.book.unwatch(sid)
        status, text = await self._post(sid, compact_body())
        if status == 0 or status >= 300:
            self._fail(
                plan,
                f"Could not compact the session ({_reason(status, text)}). "
                "Try again, or send /compact from the composer.",
            )
            return
        snooze_failure = await self._run_snooze(plan)
        self._drop(plan)
        if snooze_failure:
            self._store_failure(sid, snooze_failure)

    async def _run_snooze(self, plan: Plan) -> str | None:
        if not plan.snooze:
            return None
        try:
            result = await self._snooze(plan.session_id)
        except Exception as exc:  # noqa: BLE001 (the compact went out; only the card is at stake)
            return f"Compacting started, but the card could not be snoozed ({exc!r})."
        if isinstance(result, tuple):
            status, _code, message = result
            if status == 409:
                return None
            return f"Compacting started, but the card could not be snoozed ({message})."
        return None

    async def _after_restart_compacting(self, plan: Plan) -> None:
        """A plan found in ``compacting``: the process died around the POST. Never post again."""
        sid = plan.session_id
        sent_at = plan.compact_sent_at or plan.created_at
        rows = await asyncio.to_thread(
            self._list_items, sid, order="desc", limit=1, type="compaction"
        )
        if rows and (_field(rows[0], "created_at") or 0) >= sent_at - CLOCK_SLACK_S:
            self._drop(plan)
            return
        self._fail(plan, MSG_RESTART)

    # ── settled ────────────────────────────────────────────────────────

    async def _settled(self, plan: Plan) -> bool:
        """Whether the chat is at a safe point to send the next step."""
        sid = plan.session_id
        if self._pending(sid):
            return False
        info = self.book.info(sid)
        now = self._mono()
        if info.last_end_seq > info.last_open_seq:
            return info.last_edge_mono is not None and now - info.last_edge_mono >= STABLE_S
        # Unknown: no turn end since the last turn start (nothing seen since this process
        # started, or a running edge with no real turn behind it).
        status = await asyncio.to_thread(self._live_status, sid)
        if status != "idle":
            return False
        quiet_from = max(
            self._since.get(sid, self._boot_mono),
            info.last_edge_mono if info.last_edge_mono is not None else self._boot_mono,
        )
        return now - quiet_from >= UNKNOWN_QUIET_S

    # ── bookkeeping ────────────────────────────────────────────────────

    def _save(self) -> bool:
        try:
            return bool(self.store.save(self.plans, self.failures))
        except Exception:  # a failed write must stop the step, never the loop
            _logger.exception("compact queue: could not save the state")
            return False

    def _drop(self, plan: Plan) -> None:
        """Remove *plan*, only if it is still the chat's plan (a re-queue must survive)."""
        if self.plans.get(plan.session_id) is not plan:
            return
        del self.plans[plan.session_id]
        self._since.pop(plan.session_id, None)
        self.book.unwatch(plan.session_id)
        self._save()

    def _store_failure(self, session_id: str, message: str) -> None:
        _logger.warning("compact queue: session %s: %s", session_id, message)
        self.failures[session_id] = Failure(id=uuid.uuid4().hex, message=message, at=self._clock())
        self._save()

    def _fail(self, plan: Plan, message: str) -> None:
        if self.plans.get(plan.session_id) is not plan:
            return
        del self.plans[plan.session_id]
        self._since.pop(plan.session_id, None)
        self.book.unwatch(plan.session_id)
        self._store_failure(plan.session_id, message)

    def _prune_failures(self) -> None:
        now = self._clock()
        stale = [sid for sid, f in self.failures.items() if now - f.at > FAILURE_KEEP_S]
        for sid in stale:
            del self.failures[sid]
        if stale:
            self._save()


# ── live wiring: the server's own routes, as the chat's owner ───────────


async def post_event(
    app: FastAPI,
    identity: merge_watch.OwnerIdentity,
    session_id: str,
    body: dict[str, Any],
) -> tuple[int, str]:
    """POST *body* to the chat's events route as its owner; ``(0, reason)`` on no answer.

    Not ``merge_watch.post_live``: that follows ``ae.handoff_to`` and drops review, helper and
    archived chats, and neither is right for a chat Reid asked to compact.
    """
    headers = await identity.headers(session_id)
    if headers is None:
        return 0, "the server cannot act as this chat's owner"
    try:
        response = await merge_watch._call(
            app,
            "POST",
            f"/v1/sessions/{session_id}/events",
            {**headers, "content-type": "application/json"},
            body,
        )
    except httpx.HTTPError as exc:
        return 0, repr(exc)
    return response.status_code, response.text[:_LOOPBACK_BODY_MAX]


def _live_status(session_id: str) -> str | None:
    from omnigent.runtime import get_conversation_store

    conv = get_conversation_store().get_conversation(session_id)
    return conv.live_status if conv is not None else None


def _list_items(session_id: str, **kwargs: Any) -> Sequence[Any]:
    from omnigent.runtime import get_conversation_store

    return get_conversation_store().list_items(session_id, **kwargs).data


def _pending(session_id: str) -> Sequence[str]:
    from omnigent.runtime import pending_inputs

    return pending_inputs.pending_ids(session_id)


_current: CompactQueue | None = None


def current() -> CompactQueue | None:
    """The running driver, or ``None`` when it is off."""
    return _current


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the driver and its observers for the life of the server."""
    global _current
    if not enabled():
        _logger.info("queued compact off (%s=0)", ENABLED_ENV)
        yield
        return
    from omnigent.server import ae_turn_edges, item_observers

    identity = merge_watch.OwnerIdentity()
    book = EdgeBook()
    queue = CompactQueue(
        store=StateFile(state_path()),
        book=book,
        post=lambda sid, body: post_event(app, identity, sid, body),
        snooze=lead_handoff.snooze,
        pending=_pending,
        live_status=_live_status,
        list_items=_list_items,
    )
    ae_turn_edges.register_turn_edge_observer(book.on_edge)
    item_observers.register_item_observer(book.on_items)
    _current = queue
    if queue.plans:
        queue.wake.set()
    task = asyncio.get_running_loop().create_task(queue.run(), name="ae-compact-queue")
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        ae_turn_edges.unregister_turn_edge_observer(book.on_edge)
        item_observers.unregister_item_observer(book.on_items)
        _current = None


#: No routes; included for its lifespan (see the module docstring).
lifespan_router = APIRouter(lifespan=lifespan)

__all__ = [
    "AlreadyQueued",
    "CompactQueue",
    "CompactSending",
    "EdgeBook",
    "Failure",
    "Plan",
    "StateFile",
    "compact_body",
    "current",
    "enabled",
    "lifespan_router",
    "post_event",
    "prepare_body",
    "state_path",
]
