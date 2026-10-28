"""Wake an idle lead when Reid answers its child's prompt (2026-10-28).

A lead's action dock shows the prompts of the family children it started (an MCP
elicitation, a tool permission, a question), and Reid answers them there. The
answer reaches the child, which resumes; but a lead whose turn had already ended
slept on until Reid typed "continue" (child 75db0142 resumed at 01:55:23Z on
2026-10-28 and its lead stayed idle).

What happens now: when a verdict is accepted for an elicitation that was pending
on a family child, and the child's lead is idle, the server posts ONE note into
the lead (:func:`note_text`) the way the merge watcher does
(:func:`ae_omni_policies.merge_watch.post_live`, as the lead's owner). The note
starts with :data:`ae_omni_policies.continuation.CHILD_WAKE_PREFIX`, which the
slot cap, the rabbit hole and the fork classifier let through
(:mod:`ae_omni_policies.continuation`).

When it stays quiet:

- The session is not a family child of a lead with the same owner:
  ``ae_family_prompts.valid_lead_id`` checks that ``ae.orch`` names an existing
  session of the same owner and excludes forks, native subagents and self
  (``ae.orch`` is not a reserved label, so without the owner check one user could
  wake another user's lead). The lead must not be archived or failed (a failed
  lead belongs to auto-resume).
- The lead stays mid-turn for :data:`WAIT_S` seconds. That also covers the lead
  answering through its own ``answer_child_question`` tool. A busy lead is
  re-read every :data:`POLL_S` seconds, so a turn that is just ending still gets
  the note, and the lead is read again right before every post attempt. "Mid-turn"
  is the lead's relay status cache (``running`` or ``waiting``) or an entry in the
  active-response cache; on a cache miss the persisted ``live_status`` is the
  fallback (the server settles it to idle on restart, so there is no restart
  probe). No stronger signal exists for a native Claude lead that has published
  no status edge, so such a lead reads idle.
- The elicitation was not pending on that child (a repeat or invented verdict),
  the verdict's ``action`` is not accept, decline or cancel, or the same question
  (session, id and params) already produced a note. A failed or dropped post gives
  the key back.

It rides upstream's elicitation-observers registry (patch 0216), registered from
the app lifespan, and runs off the resolve request: a failure is logged and never
reaches the route. Without the patch it stays off.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI

from ae_omni_policies import labels, merge_watch
from ae_omni_policies.continuation import CHILD_WAKE_PREFIX, PICK_CAP, TITLE_CAP

try:  # Patch 0216; absent on an unpatched upstream.
    from omnigent.server.elicitation_observers import (
        register_elicitation_observer,
        unregister_elicitation_observer,
    )
except ImportError:  # pragma: no cover - exercised only on an unpatched upstream
    register_elicitation_observer = None  # type: ignore[assignment]
    unregister_elicitation_observer = None  # type: ignore[assignment]

_logger = logging.getLogger(__name__)

#: Seconds a busy lead is waited on, and how often it is read again.
WAIT_S = 30.0
POLL_S = 5.0
#: Questions remembered, so one question never posts twice.
SEEN_CAP = 512
#: Tries to post a note the server answered with a retryable status, and the wait between.
POST_TRIES = 3
RETRY_WAIT_S = 5.0

_seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
_app: FastAPI | None = None
_identity: merge_watch.OwnerIdentity | None = None


def _key(session_id: str, elicitation_id: str, pending: Mapping[str, Any]) -> tuple[str, str, str]:
    """A later, different question can be re-parked under one id; the params tell them apart."""
    params = json.dumps(pending.get("params"), sort_keys=True, default=str)
    return session_id, elicitation_id, hashlib.sha256(params.encode()).hexdigest()[:12]


def _claim(key: tuple[str, str, str]) -> bool:
    """Whether *key* is new; remembers it, oldest forgotten past :data:`SEEN_CAP`."""
    if key in _seen:
        return False
    _seen[key] = None
    while len(_seen) > SEEN_CAP:
        _seen.popitem(last=False)
    return True


def _enum_label(schema: Mapping[str, Any], value: Any) -> str | None:
    """The display label of *value* in a field schema (``enumNames`` or ``oneOf`` titles)."""
    names = schema.get("enumNames")
    values = schema.get("enum")
    if isinstance(names, list) and isinstance(values, list) and value in values:
        index = values.index(value)
        if index < len(names) and isinstance(names[index], str) and names[index].strip():
            return names[index]
    options = schema.get("oneOf")
    if isinstance(options, list):
        for option in options:
            if (
                isinstance(option, dict)
                and option.get("const") == value
                and isinstance(option.get("title"), str)
                and option["title"].strip()
            ):
                return option["title"]
    return None


def picked(data: Mapping[str, Any], pending: Mapping[str, Any]) -> str:
    """What Reid picked, in words: the enum label or value on accept, else the verdict.

    :param data: The verdict payload (``action``, optional ``content``).
    :param pending: The pending ``response.elicitation_request`` event.
    :returns: e.g. ``"Continue the operation"``, ``"Decline"``, ``"Cancel"``. Periods are
        dropped, so the note's one closing period stays the only one after "picked".
    """
    action = data.get("action")
    if action == "decline":
        return "Decline"
    if action != "accept":
        return "Cancel"
    content = data.get("content")
    params = pending.get("params")
    schema = params.get("requestedSchema") if isinstance(params, dict) else None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(content, dict) and len(content) == 1:
        (field, value), *_ = content.items()
        if isinstance(value, str) and value.strip():
            field_schema = properties.get(field) if isinstance(properties, dict) else None
            named = _enum_label(field_schema, value) if isinstance(field_schema, dict) else None
            # Clip first: a cut label then loses its "..." and the note still matches.
            return labels.clip(named or value, PICK_CAP).replace(".", "").strip() or "Accept"
    return "Accept"


def note_text(child_id: str, title: str | None, choice: str) -> str:
    """The one line posted into the lead.

    :param child_id: The child's session id.
    :param title: The child's title, or ``None``.
    :param choice: What :func:`picked` returned.
    :returns: A line starting with :data:`CHILD_WAKE_PREFIX`.
    """
    name = labels.clip(title or "Untitled", TITLE_CAP)
    short = f"{child_id[:4]}..."
    return f"{CHILD_WAKE_PREFIX}{name} ({short}): Reid picked {choice}."


def lead_state(lead: Any) -> str:
    """``"idle"``, ``"busy"`` (mid-turn) or ``"skip"`` (archived or failed) for a lead row."""
    from omnigent.server.routes._sessions.common import (
        _session_active_response_cache,
        _session_status_cache,
    )

    if lead.archived:
        return "skip"
    status = _session_status_cache.get(lead.id, lead.live_status)
    if status == "failed":
        return "skip"
    if status in ("running", "waiting") or _session_active_response_cache.get(lead.id):
        return "busy"
    return "idle"


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _read_state(store: Any, lead_id: str) -> str:
    lead = await asyncio.to_thread(store.get_conversation, lead_id)
    return "skip" if lead is None else lead_state(lead)


async def _post(
    app: FastAPI, identity: merge_watch.OwnerIdentity, store: Any, lead_id: str, text: str
) -> bool:
    """Post *text*; ``True`` only when delivered. The lead is read again before every attempt."""
    for attempt in range(POST_TRIES):
        if await _read_state(store, lead_id) != "idle":
            return False
        outcome = await merge_watch.post_live(app, identity, lead_id, text)
        if outcome == "posted":
            return True
        if outcome != "retry":
            _logger.warning("child wake: the note for lead %s was dropped", lead_id)
            return False
        if attempt + 1 < POST_TRIES:
            await _sleep(RETRY_WAIT_S)
    _logger.warning("child wake: posting to lead %s failed %d times", lead_id, POST_TRIES)
    return False


async def on_resolved(
    session_id: str, elicitation_id: str, data: dict[str, Any], pending: dict[str, Any] | None
) -> None:
    """Elicitation observer: post the wake note when the rules in the module docstring hold."""
    if pending is None or _app is None or _identity is None:
        return
    if data.get("action") not in ("accept", "decline", "cancel"):
        return
    key: tuple[str, str, str] | None = None
    try:
        from omnigent.runtime import get_conversation_store
        from omnigent.server.ae_family_prompts import valid_lead_id

        store = get_conversation_store()
        child = await asyncio.to_thread(store.get_conversation, session_id)
        if child is None:
            return
        lead_id = await asyncio.to_thread(valid_lead_id, child, store)
        if lead_id is None:
            return
        key = _key(session_id, elicitation_id, pending)
        if not _claim(key):
            key = None
            return
        waited = 0.0
        state = await _read_state(store, lead_id)
        while state == "busy" and waited < WAIT_S:
            await _sleep(POLL_S)
            waited += POLL_S
            state = await _read_state(store, lead_id)
        if state == "idle":
            text = note_text(session_id, child.title, picked(data, pending))
            if await _post(_app, _identity, store, lead_id, text):
                key = None
    except Exception:  # noqa: BLE001 (a wake note is best effort and must not surface anywhere)
        _logger.warning("child wake failed for session %s", session_id, exc_info=True)
    finally:
        if key is not None:
            _seen.pop(key, None)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Register the observer for the life of the server."""
    global _app, _identity
    if register_elicitation_observer is None or unregister_elicitation_observer is None:
        _logger.info("child wake off: upstream has no elicitation observers (patch 0216)")
        yield
        return
    _app, _identity = app, merge_watch.OwnerIdentity()
    register_elicitation_observer(on_resolved)
    try:
        yield
    finally:
        unregister_elicitation_observer(on_resolved)
        _app = _identity = None
        with contextlib.suppress(Exception):
            _seen.clear()


#: No routes; included for its lifespan (see the module docstring).
lifespan_router = APIRouter(lifespan=lifespan)

__all__ = [
    "lead_state",
    "lifespan_router",
    "note_text",
    "on_resolved",
    "picked",
]
