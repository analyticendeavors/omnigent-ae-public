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
from collections.abc import Sequence
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


def own_row(store: Any | None = None) -> Conversation | None:
    """The conversation being evaluated, fresh from the store.

    :param store: A conversation store; defaults to the runtime's.
    :returns: The row, or ``None`` when the ambient id is unknown or the row is gone.
    """
    session_id = current_conversation_id()
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
    "MAX_ROWS",
    "PAGE_SIZE",
    "SessionSummary",
    "current_conversation_id",
    "display_title",
    "format_elapsed",
    "is_blocked",
    "is_failed",
    "is_top_level",
    "list_top_level",
    "own_row",
    "started_at",
    "summarize",
    "working_sessions",
]
