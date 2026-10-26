"""Top-level session listing, shared by the slot cap and the capacity route.

A slot is a session whose ``ae.status`` is ``working``, not a session whose live
status is ``running``: sub-agents and scheduled runs run without being slots.
Only top-level (``kind="default"``), non-archived rows are read, straight from
the conversation store (``omnigent.runtime.get_conversation_store``), which
hydrates ``labels`` and ``pending_elicitation_count`` per row.

The row being evaluated is found through upstream's ambient session id: the
audit middleware stamps every session route's ``{session_id}`` path parameter
into a context variable before routing (``omnigent/server/app.py``,
``set_current_session_id``), and ``asyncio.to_thread`` copies the context into
the thread a sync policy callable runs on. Both request-phase entry points are
session routes: ``POST /v1/sessions/{id}/policies/evaluate`` (native hooks) and
``POST /v1/sessions/{id}/events`` (web and sub-agent messages).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from omnigent.debug_logging import current_session_id
from omnigent.entities.conversation import Conversation
from omnigent.runtime import get_conversation_store

from ae_omni_policies import labels

_logger = logging.getLogger(__name__)

PAGE_SIZE = 100
# One person's sessions; a cap keeps a runaway store from turning every prompt
# into a full-table walk. Newest first, so a store past the cap loses its
# oldest rows, the ones least likely to be working.
MAX_ROWS = 500
# Upstream's live_status for a turn that errored ("idle", "running", "waiting",
# "failed"; omnigent/entities/conversation.py).
FAILED = "failed"
# A turn that ended and is waiting for the next prompt.
IDLE = "idle"
# A helper started this recently still counts as busy when it reads idle: the row has not
# reported its first turn yet, and a burst of creates must not slip past the family cap.
FRESH_HELPER_S = 60


def list_top_level(
    store: Any | None = None, *, accessible_by: str | None = None
) -> list[Conversation]:
    """Every top-level, non-archived conversation, newest first, capped at :data:`MAX_ROWS`.

    :param store: A conversation store; defaults to the runtime's.
    :param accessible_by: Only the sessions this user has a grant on (upstream's
        ``GET /v1/sessions`` filter); ``None`` lists every user's sessions.
    :returns: Conversation rows with labels hydrated.
    """
    store = store if store is not None else get_conversation_store()
    rows: list[Conversation] = []
    after: str | None = None
    while len(rows) < MAX_ROWS:
        page = store.list_conversations(
            limit=PAGE_SIZE,
            after=after,
            kind="default",
            include_archived=False,
            order="desc",
            sort_by="created_at",
            accessible_by=accessible_by,
        )
        rows.extend(page.data)
        if not page.has_more or not page.last_id:
            break
        after = page.last_id
    return rows[:MAX_ROWS]


def current_conversation_id() -> str | None:
    """The conversation the request being handled belongs to, or ``None`` off a session route."""
    return current_session_id()


def own_row(
    store: Any | None = None, event: Mapping[str, Any] | None = None
) -> Conversation | None:
    """The conversation being evaluated, fresh from the store.

    :param store: A conversation store; defaults to the runtime's.
    :param event: The policy event. Its ``context.conversation_id`` (injected by the
        engine) names the session first, since the ambient id can be unset or another
        session's on the path a child's first message takes (2026-10-24).
    :returns: The row, or ``None`` when the id is unknown or the row is gone.
    """
    context = (event or {}).get("context") or {}
    session_id = context.get("conversation_id") or current_conversation_id()
    if not session_id:
        return None
    store = store if store is not None else get_conversation_store()
    return store.get_conversation(session_id)


def is_top_level(conv: Conversation) -> bool:
    """A session of its own, not a sub-agent spawned inside another one."""
    return conv.parent_conversation_id is None and conv.kind == "default"


def is_blocked(conv: Conversation) -> bool:
    """Waiting on the human: at least one pending elicitation."""
    return (conv.pending_elicitation_count or 0) > 0


def is_failed(conv: Conversation) -> bool:
    """The last turn failed (upstream's relay-observed ``live_status``).

    A failed session holds no slot, like a blocked one (decided 2026-09-25 with
    the orchestrator): the self-review of that date found one holding a slot
    for six and a half hours unnoticed. Its next prompt counts again.
    """
    return conv.live_status == FAILED


def working_sessions(rows: Sequence[Conversation]) -> list[Conversation]:
    """The rows that hold a slot: ``ae.status=working``, not blocked, not failed, oldest first."""
    working = [
        row
        for row in rows
        if labels.is_working(row.labels) and not is_blocked(row) and not is_failed(row)
    ]
    working.sort(key=started_at)
    return working


# How far up the ae.orch chain a family root is looked for: a lead's children
# start their own children rarely, and a cycle must end.
_FAMILY_DEPTH = 8


def family_key(conv: Conversation, by_id: Mapping[str, Conversation]) -> str:
    """The id of the session at the top of *conv*'s ``ae.orch`` chain, or its own id.

    A parent that is not in *by_id* (archived, or never listed) still names the
    family, so its children keep counting as one.
    """
    key = conv.id
    seen = {key}
    parent = labels.orch_parent(conv.labels)
    for _ in range(_FAMILY_DEPTH):
        if parent is None or parent in seen:
            break
        key = parent
        seen.add(key)
        row = by_id.get(key)
        parent = labels.orch_parent(row.labels) if row is not None else None
    return key


def family_keys(rows: Sequence[Conversation]) -> dict[str, str]:
    """Each row's :func:`family_key`, by row id."""
    by_id = {row.id: row for row in rows}
    return {row.id: family_key(row, by_id) for row in rows}


def is_helper(conv: Conversation, by_id: Mapping[str, Conversation] | None = None) -> bool:
    """A session another one started (``ae.orch`` names a parent): a helper, builder or reviewer.

    A row that names itself as its parent, or whose ``ae.orch`` chain (as far as *by_id*
    reaches) leads back to it, has no lead above it: it counts as a lead (2026-10-26).
    """
    parent = labels.orch_parent(conv.labels)
    if parent is None:
        return False
    seen = {conv.id}
    for _ in range(_FAMILY_DEPTH):
        if parent in seen:
            return parent != conv.id  # a loop through this row means no lead above it
        seen.add(parent)
        row = by_id.get(parent) if by_id is not None else None
        above = labels.orch_parent(row.labels) if row is not None else None
        if above is None:
            return True  # a lead (or a parent out of sight) sits above it
        parent = above
    return True


def held_keys(
    working: Sequence[Conversation],
    *,
    helpers_count: bool = False,
    by_id: Mapping[str, Conversation] | None = None,
) -> set[str]:
    """The ids that hold a slot among the *working* rows.

    A family holds one slot, through its lead: a working row with no ``ae.orch`` parent.
    A working helper whose lead is parked, archived or absent holds nothing (2026-10-26,
    Codex: it used to keep its family's slot alive). With *helpers_count* on, each working
    helper also holds a slot of its own, in addition to its lead's. *by_id* (every row)
    lets an ``ae.orch`` cycle through a parked row be seen; it defaults to the working rows.
    """
    ids = by_id if by_id is not None else {row.id: row for row in working}
    return {row.id for row in working if helpers_count or not is_helper(row, ids)}


def slots_held(
    working: Sequence[Conversation],
    *,
    helpers_count: bool = False,
    by_id: Mapping[str, Conversation] | None = None,
) -> int:
    """Slots the *working* rows hold: one per lead, plus one per helper when *helpers_count*."""
    return len(held_keys(working, helpers_count=helpers_count, by_id=by_id))


def family_children_working(
    family: str,
    working: Sequence[Conversation],
    keys: Mapping[str, str],
    now: float | None = None,
) -> list[Conversation]:
    """The busy rows in *family* other than the family's own top session.

    Busy is any live status but ``idle`` (2026-10-24): a helper that finished its turn keeps
    ``ae.status=working`` until someone flips it, and counting it left a lead with three
    idle helpers stuck on ``409 family_full``. A row that never reported (``None``) is
    just starting and counts, so a burst of creates still stops at the cap; so does an idle
    one started under :data:`FRESH_HELPER_S` ago (``ae.started``, else ``created_at``).
    """
    now = time.time() if now is None else now
    return [
        row
        for row in working
        if row.id != family
        and keys.get(row.id, row.id) == family
        and (row.live_status != IDLE or now - started_at(row) < FRESH_HELPER_S)
    ]


def started_at(conv: Conversation) -> int:
    """Epoch seconds the slot was claimed (``ae.started``), else the row's ``created_at``."""
    raw = labels.label(conv.labels, labels.STARTED)
    if raw and raw.isdigit():
        return int(raw)
    return int(conv.created_at)


def display_title(conv: Conversation) -> str:
    """The row's title, its task summary, or the id's first eight characters."""
    return (conv.title or "").strip() or (conv.task_summary or "").strip() or conv.id[:8]


def format_elapsed(seconds: float) -> str:
    """``2h 10m`` style: days and hours, hours and minutes, or minutes."""
    total = max(0, int(seconds))
    days, rest = divmod(total, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "under a minute"


@dataclass(frozen=True)
class SessionSummary:
    """What the capacity strip and the reason text need per session."""

    id: str
    title: str
    status: str | None
    live_status: str | None
    blocked: bool
    failed: bool
    started: int
    elapsed_s: int
    next: str | None
    gate: str | None
    fork: str | None
    overflow: bool
    task: str | None

    @property
    def elapsed(self) -> str:
        return format_elapsed(self.elapsed_s)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "elapsed": self.elapsed}


def summarize(conv: Conversation, *, now: float | None = None) -> SessionSummary:
    """Project one conversation row onto :class:`SessionSummary`."""
    current = time.time() if now is None else now
    started = started_at(conv)
    return SessionSummary(
        id=conv.id,
        title=display_title(conv),
        status=labels.status_of(conv.labels),
        live_status=conv.live_status,
        blocked=is_blocked(conv),
        failed=is_failed(conv),
        started=started,
        elapsed_s=max(0, int(current - started)),
        next=labels.label(conv.labels, labels.NEXT),
        gate=labels.label(conv.labels, labels.GATE),
        fork=labels.label(conv.labels, labels.FORK),
        overflow=labels.label(conv.labels, labels.OVERFLOW) == "1",
        task=labels.label(conv.labels, labels.TASK),
    )


__all__ = [
    "FAILED",
    "IDLE",
    "MAX_ROWS",
    "PAGE_SIZE",
    "SessionSummary",
    "current_conversation_id",
    "display_title",
    "family_children_working",
    "family_key",
    "family_keys",
    "format_elapsed",
    "held_keys",
    "is_blocked",
    "is_failed",
    "is_helper",
    "is_top_level",
    "list_top_level",
    "own_row",
    "slots_held",
    "started_at",
    "summarize",
    "working_sessions",
]
