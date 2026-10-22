"""The three-slot rule as a request-phase policy.

"At most three open tasks; everything else parked with a resumable note or
deliberately closed. One conversation is one slot." A slot is a top-level
session with ``ae.status=working``. The first prompt on a session with no
status claims one, which is how sessions get placed without anyone doing
anything. Past the limit the prompt is held for approval (``mode: warn``, an
ASK the web UI's approval card answers) or dropped (``mode: block``, a DENY
the Claude Code TUI shows with the reason), and the reason names the working
sessions with their NEXT line or elapsed time and the way out.

Decisions this encodes (2026-09-23):

- A session waiting on the human (``pending_elicitation_count > 0``) is
  paused and frees its slot until it resumes: red is not yellow. A session
  whose last turn failed (``live_status`` ``failed``) frees it the same way
  (2026-09-25; seen live 2026-09-24 holding a slot for 6h 34m). Both are
  derived in ``sessions.working_sessions``, never written as a label.
- ``ride unparked`` at the start of a message runs a fourth anyway, in either
  mode, and the session carries ``ae.overflow=1`` so the overrun is visible
  ("the second is riding unparked"). Below the limit the phrase is just a
  prompt: the session claims a slot like any other.
- Accepting the warn-mode ASK does the same; the labels ride on the ASK and
  the engine applies them only on accept, so a declined prompt writes nothing.
- ``working`` already holds a slot and ``reference`` never takes one, so both
  pass without a count; that second edge is how an agent opts out of slots,
  by seeding ``ae.status: reference`` in its YAML. Every other status
  (``blocked``, ``parked``, ``review``) goes through the count again on its
  next prompt: returning to a session is opening it.
- A sub-agent runs inside its parent's slot: the policy abstains on any
  conversation that has a parent. Server-wide policies reach every
  conversation, so this needs the row, found through upstream's ambient
  session id (``sessions.own_row``).
- A note the server posts to continue existing work (the merge watcher's,
  :mod:`ae_omni_policies.continuation`, 2026-10-21) passes without a count
  and without claiming a slot: it is not a new task.
- The store failing to answer fails open, like the native hook's own
  transport rule: a cap that cannot count must not stop work. Only store
  errors count as that; a programming error (a renamed store method after a
  sync, say) propagates, and the engine turns it into a fail-closed DENY,
  which is the failure that gets noticed.
- A claim is a label write the engine applies after this callable returns, so
  two first prompts evaluated in the same instant would both count the same
  rows. Claims are remembered in this process until the row shows them or a
  minute passes, and counted with the rows under one lock.
"""

from __future__ import annotations

import logging
import re
import threading
import time

from omnigent.entities.conversation import Conversation
from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse, request_user_text
from sqlalchemy.exc import SQLAlchemyError

from ae_omni_policies import labels, sessions
from ae_omni_policies.continuation import is_system_continuation

_logger = logging.getLogger(__name__)

MODES = ("warn", "block")
RIDE_UNPARKED = "ride unparked"
_RIDE = re.compile(r"^\s*ride\s+unparked\b", re.IGNORECASE)
# Errors that mean "the store did not answer". Everything else propagates.
STORE_ERRORS = (SQLAlchemyError, OSError)
PENDING_CLAIM_TTL_S = 60.0

_lock = threading.Lock()
_pending_claims: dict[str, float] = {}


def _now() -> float:
    """The clock, as a function so tests can pin it."""
    return time.time()


def reset_pending_claims() -> None:
    """Forget in-flight claims (tests)."""
    with _lock:
        _pending_claims.clear()


def reserve(
    key: str, rows: list[Conversation], limit: int = labels.SLOT_LIMIT, *, riding: bool = False
) -> tuple[bool, bool, list[Conversation], int]:
    """Hold a slot for a session that does not exist yet (``POST /v1/ae/sessions``).

    The same count the policy makes, under the same lock, so a session the
    orchestrator is creating and a first prompt typed in the same instant do
    not both take the last slot. The hold is a pending claim under *key* until
    :func:`release`, which the caller runs once the row exists (it is created
    already labelled ``working``, so the rows count it from then on) or the
    create failed.

    :param key: Any string that is not a conversation id, unique per create.
    :param rows: Top-level sessions (:func:`sessions.list_top_level`).
    :param limit: The slot limit.
    :param riding: Reid chose to run one more past the limit.
    :returns: ``(held, full, working, pending)``: whether the slot is held,
        whether the slots were already full (a held slot is then an overflow),
        the rows holding slots, and how many claims have not landed yet.
    """
    now = _now()
    with _lock:
        working = sessions.working_sessions(rows)
        landed = {item.id for item in working}
        pending = {
            cid: until
            for cid, until in _pending_claims.items()
            if until > now and cid not in landed and cid != key
        }
        _pending_claims.clear()
        _pending_claims.update(pending)
        full = len(working) + len(pending) >= limit
        if full and not riding:
            return False, True, working, len(pending)
        _pending_claims[key] = now + PENDING_CLAIM_TTL_S
        return True, full, working, len(pending)


def release(key: str) -> None:
    """Drop the hold :func:`reserve` took under *key*."""
    with _lock:
        _pending_claims.pop(key, None)


def _claim(overflow: bool = False) -> dict[str, str]:
    writes = {labels.STATUS: labels.WORKING, labels.STARTED: str(int(_now()))}
    if overflow:
        writes[labels.OVERFLOW] = "1"
    return writes


def reason_text(
    working: list[sessions.SessionSummary],
    limit: int,
    mode: str,
    lot_url: str | None,
    pending: int = 0,
) -> str:
    """The context a held or dropped prompt carries: who holds the slots and the way out.

    :param working: The sessions holding slots, oldest start first.
    :param limit: The slot limit.
    :param mode: ``warn`` or ``block``; the way out is phrased for each.
    :param lot_url: Optional link to the Parking Lot page.
    :param pending: Claims made a moment ago whose rows do not show them yet.
    """
    lines = [f"Slots full: {len(working) + pending} of {limit} working."]
    for index, item in enumerate(working, start=1):
        detail = f"next: {item.next}" if item.next else f"elapsed {item.elapsed}"
        lines.append(f"{index}. {item.title} ({detail})")
    if pending:
        lines.append(f"{pending} more claimed a slot a moment ago.")
    lot = f"Park one in the Parking Lot ({lot_url})" if lot_url else "Park one in the Parking Lot"
    if mode == "block":
        lines.append(
            f"{lot}, flip one to review, or resend starting with "
            f'"{RIDE_UNPARKED}" to run one more.'
        )
    else:
        lines.append(
            "Accept to run one more (marked overflow), or decline, then "
            f"{lot[0].lower()}{lot[1:]} or flip one to review."
        )
    return " ".join(lines)


def slot_cap(
    limit: int = labels.SLOT_LIMIT, mode: str = "warn", lot_url: str | None = None
) -> PolicyCallable:
    """Factory: gate the request phase on the number of working sessions.

    :param limit: Working sessions allowed at once; :data:`labels.SLOT_LIMIT`, which
        is ``AE_SLOT_LIMIT`` from the environment (three when unset), unless the
        policy's parameters set one. The seed sets none; a limit set on Settings >
        Global Policies wins, and since 2026-10-22 every count in the UI follows it
        too (:mod:`ae_omni_policies.slot_limit`).
    :param mode: ``warn`` holds the prompt for approval (ASK); ``block`` drops it (DENY).
    :param lot_url: Optional link to the Parking Lot page, named in the reason text.
    :returns: The policy callable.
    :raises ValueError: On a mode outside ``warn`` / ``block`` or a limit below one.
    """
    if mode not in MODES:
        raise ValueError(f"slot_cap mode must be one of {MODES}, got {mode!r}")
    if limit < 1:
        raise ValueError(f"slot_cap limit must be at least 1, got {limit!r}")

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        if event.get("type") != "request":
            return None
        try:
            row = sessions.own_row()
            if row is not None and not sessions.is_top_level(row):
                return None
            context = event.get("context") or {}
            own = row.labels if row is not None else context.get("labels") or {}
            status = labels.status_of(own)
            if status in (labels.WORKING, labels.REFERENCE):
                return {"result": "ALLOW"}
            if status and is_system_continuation(request_user_text(event.get("data"))):
                # The merge watcher's note continues work the session already holds
                # (2026-10-21, continuation.py): through, without claiming a slot.
                # Only on a session that has a status, so a first prompt cannot use
                # the words to take a slot unseen (review, 2026-10-21).
                return {"result": "ALLOW"}
            rows = sessions.list_top_level()
        except STORE_ERRORS:
            _logger.warning(
                "slot_cap could not read the store; allowing the prompt", exc_info=True
            )
            return {"result": "ALLOW"}

        own_id = row.id if row is not None else None
        riding = _RIDE.match(request_user_text(event.get("data"))) is not None
        now = _now()
        with _lock:
            working = sessions.working_sessions(rows)
            landed = {item.id for item in working}
            pending = {
                cid: until
                for cid, until in _pending_claims.items()
                if until > now and cid not in landed and cid != own_id
            }
            _pending_claims.clear()
            _pending_claims.update(pending)
            full = len(working) + len(pending) >= limit
            if not full or riding:
                if own_id is not None:
                    _pending_claims[own_id] = now + PENDING_CLAIM_TTL_S
                if full:
                    return {
                        "result": "ALLOW",
                        "reason": "Riding unparked.",
                        "set_labels": _claim(overflow=True),
                    }
                return {"result": "ALLOW", "set_labels": _claim()}
            pending_count = len(pending)

        reason = reason_text(
            [sessions.summarize(item, now=now) for item in working],
            limit,
            mode,
            lot_url,
            pending=pending_count,
        )
        if mode == "block":
            return {"result": "DENY", "reason": reason}
        return {"result": "ASK", "reason": reason, "set_labels": _claim(overflow=True)}

    return evaluate


__all__ = [
    "MODES",
    "PENDING_CLAIM_TTL_S",
    "RIDE_UNPARKED",
    "STORE_ERRORS",
    "reason_text",
    "release",
    "reserve",
    "reset_pending_claims",
    "slot_cap",
]
