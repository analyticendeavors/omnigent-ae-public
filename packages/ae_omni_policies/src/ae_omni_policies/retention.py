"""Scheduled-run retention: after a task fires, delete its older run sessions.

Every firing of a scheduled task starts a new session. The steward and the
shutdown note fire every weekday, so about ten sessions a week piled up in the
sidebar, all left in ``reference`` or archived, with nothing to reuse or prune
them. The owner decided on 2026-10-13: after a task fires, keep its newest
``keep_runs`` run sessions (5 by default, one working week, a per-task field on
``/v1/scheduled-tasks``) and delete older ones that are safe to delete.

Patch P29 (``omnigent/server/scheduled/retention.py``) supplies the setting,
the post-fire seam and a request-free delete; this module is the policy, the
observer registered on import. Without the patch the import still succeeds and
nothing is ever deleted.

A run session is a session a ``scheduled_task_runs`` row of the same task
points at; nothing else is ever considered. Ranked newest first, the first
``keep_runs`` that still exist are kept whatever their state; an older one is
deleted only when :func:`keep_reason` finds no reason to keep it:

- it is the run that just fired;
- it is running or waiting, has a running sub-agent or a background shell, or
  its last turn failed (only idle sessions are eligible);
- it has an approval outstanding, or an open gate (``ae.gate=open``);
- its ``ae.status`` is ``working``, ``blocked``, ``review`` or ``parked``, or
  it is neither ``reference`` nor archived;
- its title is no longer the task's name: the owner renamed it, so they kept it.

No migration: the first firing after deploy walks every run row the task has,
so the existing backlog goes under the same rules. A task with
``keep_runs: null`` is never pruned.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from omnigent.entities.conversation import Conversation

from ae_omni_policies import labels

try:  # Patch P29; absent on an unpatched upstream.
    from omnigent.server.scheduled.retention import (
        FireEvent,
        delete_session_server_side,
        register_fire_observer,
    )
except ImportError:  # pragma: no cover - exercised only without the patch
    FireEvent = Any  # type: ignore[misc,assignment]
    delete_session_server_side = None  # type: ignore[assignment]
    register_fire_observer = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

#: ``ae.status`` values that mean someone is using the session.
IN_USE = (labels.WORKING, labels.BLOCKED, labels.REVIEW, labels.PARKED)
RUNNING = "running"
FAILED = "failed"
IDLE = "idle"
# One page of a task's run rows, and how many pages a prune reads at most.
RUNS_PAGE = 200
MAX_RUN_PAGES = 50


def keep_reason(conv: Conversation, *, task_name: str, just_fired: str, live: str) -> str | None:
    """Why *conv*, a run session older than the kept window, must stay; ``None`` to delete.

    :param conv: The run session, fresh from the store.
    :param task_name: The task's name, the title every run session starts with.
    :param just_fired: The session the current firing created.
    :param live: :func:`live_state` of *conv*: ``idle``, ``running`` or ``failed``.
    """
    if conv.id == just_fired:
        return "the run that just fired"
    if conv.parent_conversation_id is not None or conv.kind != "default":
        return "not a top-level session"
    if live == RUNNING:
        return "running"
    if live != IDLE:
        return f"not idle ({live})"
    if (conv.pending_elicitation_count or 0) > 0:
        return "approval outstanding"
    if labels.label(conv.labels, labels.GATE) == labels.GATE_OPEN:
        return "open gate"
    status = labels.status_of(conv.labels)
    if status in IN_USE:
        return f"status {status}"
    if status != labels.REFERENCE and not conv.archived:
        return "neither reference nor archived"
    if (conv.title or "") != task_name:
        return "renamed"
    return None


async def live_state(store: Any, conv: Conversation) -> str:
    """``running``, ``failed`` or ``idle``, as upstream's session list and delete read it.

    Running covers the session itself (relay cache, then the persisted
    ``live_status``), any sub-agent at any depth, and background shells, the
    same notion ``_best_effort_stop`` uses before a delete.
    """
    from omnigent.server.routes._sessions.common import _session_background_task_count_cache
    from omnigent.server.routes._sessions.helpers import (
        _collect_descendant_conversation_ids,
        _session_status_with_child_rollup,
    )

    descendants = await _collect_descendant_conversation_ids(store, conv.id)
    status = _session_status_with_child_rollup(conv.id, descendants, conv.live_status)
    if status == RUNNING or _session_background_task_count_cache.get(conv.id, 0) > 0:
        return RUNNING
    return FAILED if status == FAILED else IDLE


def run_conversation_ids(task_store: Any, task_id: str) -> list[str]:
    """The sessions *task_id*'s runs created, newest run first, each once."""
    seen: dict[str, None] = {}
    after: str | None = None
    for _ in range(MAX_RUN_PAGES):
        runs, after = task_store.list_runs(task_id, limit=RUNS_PAGE, after_id=after)
        for run in runs:
            if run.conversation_id:
                seen.setdefault(run.conversation_id, None)
        if after is None:
            break
    return list(seen)


async def prune_after_fire(event: FireEvent) -> list[str]:
    """The fire observer: delete *event*'s task's run sessions past ``keep_runs``.

    :returns: The ids deleted, oldest last.
    """
    task = event.task
    keep = task.keep_runs
    if keep is None or delete_session_server_side is None:
        return []
    deps = event.deps
    store = deps.conversation_store
    ids = await asyncio.to_thread(run_conversation_ids, deps.scheduled_task_store, task.id)
    existing = 0
    deleted: list[str] = []
    kept: dict[str, str] = {}
    for session_id in ids:
        conv = await asyncio.to_thread(store.get_conversation, session_id)
        if conv is None:
            continue
        existing += 1
        if existing <= keep:
            continue
        reason = keep_reason(
            conv,
            task_name=task.name,
            just_fired=event.conversation_id,
            live=await live_state(store, conv),
        )
        if reason is not None:
            kept[session_id] = reason
            continue
        if await delete_session_server_side(deps, session_id):
            deleted.append(session_id)
    if deleted or kept:
        _logger.info(
            "retention: task %s (%s) keeps %s, deleted %d, kept %d older: %s",
            task.id,
            task.name,
            keep,
            len(deleted),
            len(kept),
            "; ".join(f"{sid} {why}" for sid, why in kept.items()) or "none",
        )
    return deleted


def register() -> bool:
    """Register :func:`prune_after_fire` when patch P29 is present."""
    if register_fire_observer is None:
        _logger.info(
            "omnigent.server.scheduled.retention is missing (patch P29 not applied); "
            "scheduled run sessions are never pruned"
        )
        return False
    register_fire_observer(prune_after_fire)
    return True


REGISTERED = register()

__all__ = [
    "IN_USE",
    "REGISTERED",
    "keep_reason",
    "live_state",
    "prune_after_fire",
    "register",
    "run_conversation_ids",
]
