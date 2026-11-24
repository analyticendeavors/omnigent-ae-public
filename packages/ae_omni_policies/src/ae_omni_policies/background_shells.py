"""Remember the background shells a claude-native turn left running, and revive their session.

On 2026-11-23 a planner launched a 20-minute render script as a background shell and ended
its turn ("the results come in once the renders finish"). Its session then read ``idle``. A
deploy restarted the host 26 minutes later, which killed the shell, and the planner slept on:
auto-resume (:mod:`ae_omni_policies.auto_resume`) revives only sessions that were mid-turn,
and nothing persistent said this idle session was still waiting on work. The Stop hook does
report the background shells still running, but the server kept that count only in memory,
and the server restart that comes before the host restart emptied it.

This module keeps the count where a restart cannot lose it:

- Fork patch 0443 hands the Stop edge's ``background_task_count`` to turn-edge observers
  (``omnigent.server.ae_turn_edges``). :func:`on_edge` writes it to the session's
  ``ae.bg_shells`` label when it is above zero, and removes the label when a later Stop
  reports none. Each write runs off the request path on the event loop, one at a time per
  session and newest count last, so a slow write can never land after a newer one.
- On a host connect, auto-resume treats an idle session carrying the label as interrupted
  (the same age, sub-agent, one-try-per-runner and runner-alive rules) and calls
  :func:`wake`, which posts :data:`~ae_omni_policies.continuation.BACKGROUND_RESUME_TEXT`
  as the session's owner. The message relaunches the dead runner the way any message does.

A shell that ended without a later Stop leaves the label set until the next turn ends, so a
deploy inside auto-resume's six-hour window can post one note the session did not need; the
note asks it to check first. A lead waiting only on an in-process ``ScheduleWakeup`` sends no
such signal and is still not revived.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI

from ae_omni_policies import compact_queue, labels, merge_watch
from ae_omni_policies.continuation import BACKGROUND_RESUME_TEXT

_logger = logging.getLogger(__name__)

#: Tries to post the note when the server answers with a retryable status, and the wait between.
POST_TRIES = 3
RETRY_WAIT_S = 5.0

_app: FastAPI | None = None
_identity: merge_watch.OwnerIdentity | None = None
_loop: asyncio.AbstractEventLoop | None = None
_tasks: set[asyncio.Task[None]] = set()
# Per session, the newest count not yet written, and the sessions with a writer running.
# Touched only on the event loop thread.
_latest: dict[str, int] = {}
_writing: set[str] = set()


def _store() -> Any:
    from omnigent.runtime import get_conversation_store

    return get_conversation_store()


def record(session_id: str, count: int) -> None:
    """Write *count* to the session's ``ae.bg_shells`` label, or remove it when zero."""
    store = _store()
    if count > 0:
        store.set_labels(session_id, {labels.BG_SHELLS: str(count)})
        return
    conv = store.get_conversation(session_id)
    if conv is not None and labels.BG_SHELLS in (conv.labels or {}):
        store.delete_label(session_id, labels.BG_SHELLS)


async def _drain(session_id: str) -> None:
    """Write the session's newest count until no newer one is waiting."""
    try:
        while session_id in _latest:
            count = _latest.pop(session_id)
            try:
                await asyncio.to_thread(record, session_id, count)
            except Exception:  # noqa: BLE001 (a lost tally costs one resume, never the turn)
                _logger.warning(
                    "background shells: recording %s failed", session_id, exc_info=True
                )
    finally:
        _writing.discard(session_id)


def _spawn(session_id: str, count: int) -> None:
    _latest[session_id] = count
    if session_id in _writing:
        return
    _writing.add(session_id)
    task = asyncio.get_running_loop().create_task(_drain(session_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def on_edge(edge: Any) -> None:
    """Turn-edge observer: hand a Stop edge's shell count to the event loop."""
    count = getattr(edge, "background_task_count", None)
    if not edge.turn_completed or not isinstance(count, int) or _loop is None:
        return
    _loop.call_soon_threadsafe(_spawn, edge.session_id, count)


def pending(conv: Any) -> bool:
    """Whether *conv* ended its last turn with background shells still running."""
    value = labels.label(conv.labels, labels.BG_SHELLS)
    return value is not None and value.isdigit() and int(value) > 0


async def wake(session_id: str) -> str:
    """Post the background resume note into *session_id*; returns what happened.

    :returns: ``"posted"``, ``"off"`` when the lifespan is not running, or the last failure.
    """
    if _app is None or _identity is None:
        return "off"
    body = {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": BACKGROUND_RESUME_TEXT}],
        },
    }
    outcome = ""
    for attempt in range(POST_TRIES):
        status, detail = await compact_queue.post_event(_app, _identity, session_id, body)
        if 200 <= status < 300:
            return "posted"
        outcome = f"HTTP {status}: {detail}" if status else detail
        if status not in (0, 409, 429, 502, 503, 504):
            break
        if attempt + 1 < POST_TRIES:
            await asyncio.sleep(RETRY_WAIT_S)
    return outcome


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Register the observer and hold the app for :func:`wake` for the life of the server."""
    global _app, _identity, _loop
    try:
        from omnigent.server import ae_turn_edges
    except ImportError:  # pragma: no cover - exercised only on an unpatched upstream
        _logger.info("background shells off: upstream has no turn-edge observers")
        yield
        return
    _app, _identity, _loop = app, merge_watch.OwnerIdentity(), asyncio.get_running_loop()
    ae_turn_edges.register_turn_edge_observer(on_edge)
    try:
        yield
    finally:
        ae_turn_edges.unregister_turn_edge_observer(on_edge)
        _app = _identity = _loop = None
        _latest.clear()
        for task in list(_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


#: No routes; included for its lifespan (see the module docstring).
lifespan_router = APIRouter(lifespan=lifespan)

__all__ = ["lifespan_router", "on_edge", "pending", "record", "wake"]
