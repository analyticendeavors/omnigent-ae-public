"""Resume sessions a host restart interrupted, once per dead runner.

A host restart (a deploy, an update, a crash) kills every runner it spawned,
and the host comes back with new runner ids. A session that was mid-turn is
left ``failed`` with ``runner_disconnected`` until someone clicks Resume, and
Resume only relaunches the runner: the lost turn does not continue. The owner asked
on 2026-10-13 for those sessions to come back by themselves.

Patch P34 gives the seam: an observer run after a host connects, and
``resume_interrupted_session``, which relaunches the runner with upstream's own
"your runner was interrupted, continue" turn. This module is the decision:

- only the connecting host's top-level sessions (a host has one owner) that
  upstream itself counts as interrupted and restorable (mid-turn, or
  failed with ``runner_disconnected`` / ``runner_failed_to_start``; not
  archived, closed, or stopped on purpose). Sub-agents follow their parent
  through upstream's child restoration;
- touched within :data:`MAX_AGE_S` (six hours), so an old failure is never
  revived by surprise;
- one try per dead runner: ``ae.autoresume`` records the runner id the try was
  for, so a resume that fails is not retried on every reconnect, and a later
  disconnect of the new runner gets its own try;
- not while the runner is still alive. A deploy restarts the server first,
  and the old host reconnects to it for a few seconds with its runners alive.
  Upstream's recovery sees a connected runner and skips the continue turn, so
  a try spent then would be wasted. A session whose runner is alive is left
  alone and unstamped, and keeps its one try for the restart that really kills
  the runner. "Alive" is either the server's runner router having the runner
  connected, or the connecting host reporting the process alive: runner tunnels
  reconnect on their own and can lag the host's tunnel, so the router alone can
  miss a live runner at the moment the host connects, and the host, which holds
  the process, answers authoritatively. Found on 2026-10-18 in the deploy of ae-v0.16.0.50: the old
  host's brief reconnect spent the one try, and the real restart three seconds
  later was skipped as "already tried for this runner".

Interrupted sessions that are skipped are logged: "runner still alive" and
"already tried for this runner" at INFO, "too old" at DEBUG (it repeats on every
reconnect); other skips are silent.

``AE_AUTO_RESUME=0`` in ``config/server.env`` turns it off.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from omnigent.entities.conversation import Conversation

from ae_omni_policies import labels

try:  # Patch P34; absent on an unpatched upstream.
    from omnigent.server.host_connect_observers import (
        HostConnectEvent,
        register_host_connect_observer,
        resume_interrupted_session,
    )
except ImportError:  # pragma: no cover - exercised only without the patch
    HostConnectEvent = Any  # type: ignore[misc,assignment]
    register_host_connect_observer = None  # type: ignore[assignment]
    resume_interrupted_session = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

ENABLED_ENV = "AE_AUTO_RESUME"
#: How recently a session must have been touched to be resumed.
MAX_AGE_S = 6 * 60 * 60
#: How many of the most recently updated sessions a connect reads.
SCAN_LIMIT = 100


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether auto-resume is on (default on; ``0``, ``false``, ``off`` turn it off)."""
    value = (environ if environ is not None else os.environ).get(ENABLED_ENV, "")
    return value.strip().lower() not in {"0", "false", "off", "no"}


def skip_reason(conv: Conversation, *, host_id: str, now: float) -> str | None:
    """Why *conv* is not resumed on *host_id* connecting, or ``None`` to resume it."""
    from omnigent.server.child_session_recovery import _interrupted, _restorable

    if conv.host_id != host_id:
        return "other host"
    if conv.parent_conversation_id is not None:
        return "sub-agent"
    if not _restorable(conv):
        return "not restorable"
    if not _interrupted(conv):
        return "not interrupted"
    if now - conv.updated_at > MAX_AGE_S:
        return "too old"
    tried = labels.label(conv.labels, labels.AUTORESUME)
    if tried is not None and tried == (conv.runner_id or ""):
        return "already tried for this runner"
    return None


def resumable_failure(conv: Conversation, now: float) -> bool:
    """Whether a ``failed`` *conv* is one auto-resume (or upstream's child recovery) would revive.

    2026-10-29, for the auto-archive sweep: upstream's own :func:`_interrupted` (failed with
    ``runner_disconnected`` / ``runner_failed_to_start``) and the :data:`MAX_AGE_S` window,
    the same two tests :func:`skip_reason` applies. Archiving such a session before its host
    reconnects would cost it the interrupted turn, since both recoveries need it unarchived.
    """
    from omnigent.server.child_session_recovery import _interrupted

    return _interrupted(conv) and now - conv.updated_at <= MAX_AGE_S


_check_failure_logged = False


async def runner_alive(conv: Conversation, event: HostConnectEvent) -> bool:
    """Whether *conv*'s runner is still alive, so a resume would be wasted.

    True when the server's runner router has the runner connected (the check
    upstream's recovery makes), or the connecting host says its process is
    alive. Any failure answers ``False``, which is the behavior from before
    this check existed (resume the session), and logs one warning per process.
    """
    global _check_failure_logged
    try:
        from omnigent.server.routes.sessions import _get_runner_client, _query_host_runner_status
        from omnigent.server.routes.sessions.routes_events import get_server_runner_router

        router = get_server_runner_router()
        # Without a router the in-process fallback would claim every session is connected.
        if router is not None and await _get_runner_client(conv.id, router) is not None:
            return True
        registry = getattr(event.app_state, "host_registry", None)
        conn = registry.get(event.host_id) if registry is not None else None
        if conn is None or not conv.runner_id:
            return False
        return await _query_host_runner_status(conn, registry, conv.runner_id) == "alive"
    except Exception as exc:  # noqa: BLE001 - any failure means resume, as before the check
        if not _check_failure_logged:
            _check_failure_logged = True
            _logger.warning(
                "auto-resume: could not check whether runners are alive (%s); resuming as before",
                exc,
            )
        return False


#: Skip reasons for interrupted sessions, with the level each is logged at.
_LOGGED_SKIPS = {
    "runner still alive": logging.INFO,
    "already tried for this runner": logging.INFO,
    "too old": logging.DEBUG,
}


async def resume_after_host_connect(event: HostConnectEvent) -> list[str]:
    """Resume the connecting host's interrupted sessions; returns the ids tried."""
    import asyncio

    if not enabled():
        return []
    store = event.conversation_store
    page = await asyncio.to_thread(
        store.list_conversations,
        limit=SCAN_LIMIT,
        sort_by="updated_at",
    )
    now = time.time()
    tried: list[str] = []
    for conv in page.data:
        reason = skip_reason(conv, host_id=event.host_id, now=now)
        if reason is None and await runner_alive(conv, event):
            reason = "runner still alive"
        if reason is not None:
            if reason in _LOGGED_SKIPS:
                _logger.log(
                    _LOGGED_SKIPS[reason],
                    "auto-resume: session %s on host %s skipped: %s",
                    conv.id,
                    event.host_id,
                    reason,
                )
            continue
        await asyncio.to_thread(
            store.set_labels, conv.id, {labels.AUTORESUME: conv.runner_id or ""}
        )
        tried.append(conv.id)
        try:
            result = await resume_interrupted_session(event.app_state, store, conv.id)
            _logger.info("auto-resume: session %s on host %s: %s", conv.id, event.host_id, result)
        except Exception:
            _logger.exception("auto-resume: session %s on host %s failed", conv.id, event.host_id)
    return tried


def register() -> bool:
    """Register :func:`resume_after_host_connect` when patch P34 is present."""
    if register_host_connect_observer is None:
        _logger.info(
            "omnigent.server.host_connect_observers is missing (patch P34 not applied); "
            "interrupted sessions wait for Resume"
        )
        return False
    register_host_connect_observer(resume_after_host_connect)
    return True


REGISTERED = register()

__all__ = [
    "ENABLED_ENV",
    "MAX_AGE_S",
    "REGISTERED",
    "enabled",
    "resumable_failure",
    "resume_after_host_connect",
    "runner_alive",
    "skip_reason",
]
