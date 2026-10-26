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
  session id (``sessions.own_row``); the event's own conversation id wins over it
  (2026-10-24: a Codex child's first message was counted as a top-level session
  when the ambient id was missing).
- A note the server posts to continue existing work (the merge watcher's,
  :mod:`ae_omni_policies.continuation`, 2026-10-21) passes without a count
  and without claiming a slot: it is not a new task.
- A member of a family (a session with an ``ae.orch`` parent) is never held or
  dropped (2026-10-24, Reid: the parent is the single session, driving the rest).
  It is claimed like any first prompt so it shows as working, whether or not its
  lead is working, and past ``family_children`` busy members it carries
  ``ae.overflow=1`` instead of asking. ``family_children_working`` counts busy
  members only, so a helper that finished its turn frees its place.
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

from ae_omni_policies import labels, sessions, slot_limit
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
# The family a pending claim rides in, when it takes no slot of its own (2026-10-23).
_pending_family: dict[str, str] = {}


class FamilyFull(Exception):
    """The family already has its most children working (``slot_limit.family_children``)."""

    def __init__(self, children: list[Conversation], cap: int) -> None:
        super().__init__(f"{len(children)} of {cap} family sessions working")
        self.children = children
        self.cap = cap


def _now() -> float:
    """The clock, as a function so tests can pin it."""
    return time.time()


def reset_pending_claims() -> None:
    """Forget in-flight claims (tests)."""
    with _lock:
        _pending_claims.clear()
        _pending_family.clear()


def _live_pending(now: float, landed: set[str], skip: str | None) -> dict[str, float]:
    """Drop claims that landed or expired (under :data:`_lock`); the rest, minus *skip*."""
    pending = {
        cid: until
        for cid, until in _pending_claims.items()
        if until > now and cid not in landed and cid != skip
    }
    _pending_claims.clear()
    _pending_claims.update(pending)
    for cid in [cid for cid in _pending_family if cid not in pending]:
        del _pending_family[cid]
    return pending


def _slot_claims(pending: dict[str, float], helpers_count: bool = False) -> int:
    """Pending claims that take a slot: a helper's claim takes one only with *helpers_count*."""
    return sum(1 for cid in pending if helpers_count or cid not in _pending_family)


def _family_children(
    family: str,
    working: list[Conversation],
    keys: dict[str, str],
    pending: dict[str, float],
) -> tuple[list[Conversation], int]:
    """The family's working children and how many more claimed a place a moment ago."""
    children = sessions.family_children_working(family, working, keys, _now())
    claimed = sum(1 for cid in pending if _pending_family.get(cid) == family)
    return children, claimed


def reserve(
    key: str,
    rows: list[Conversation],
    limit: int = labels.SLOT_LIMIT,
    *,
    riding: bool = False,
    family: str | None = None,
    family_cap: int = 0,
    helpers_count: bool = False,
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
    :param family: The family the new session joins (:func:`sessions.family_key` of
        the session starting it). One that already holds a slot gives it no new one.
    :param family_cap: Children that family may have working at once, counting
        claims not landed yet; ``0`` is no cap.
    :returns: ``(held, full, working, pending)``: whether the slot is held,
        whether the slots were already full (a held slot is then an overflow),
        the rows holding slots, and how many claims that take a slot have not
        landed yet.
    :raises FamilyFull: When the family holds a slot and is at *family_cap*.
    """
    now = _now()
    with _lock:
        working = sessions.working_sessions(rows)
        keys = sessions.family_keys(rows)
        pending = _live_pending(now, {item.id for item in working}, key)
        held = sessions.held_keys(
            working, helpers_count=helpers_count, by_id={row.id: row for row in rows}
        )
        claims = _slot_claims(pending, helpers_count)
        if family is not None:
            children, claimed = _family_children(family, working, keys, pending)
            if family_cap and len(children) + claimed >= family_cap:
                raise FamilyFull(children, family_cap)
            if not helpers_count:
                # A family member never waits on the slot limit (2026-10-24); only the
                # family cap (busy children) applies, and it holds no slot at all.
                _pending_claims[key] = now + PENDING_CLAIM_TTL_S
                _pending_family[key] = family
                return True, False, working, claims
            # Helpers count (Settings, 2026-10-26): it takes a slot like any session.
            full = len(held) + claims >= limit
            if full and not riding:
                return False, True, working, claims
            _pending_claims[key] = now + PENDING_CLAIM_TTL_S
            _pending_family[key] = family
            return True, full, working, claims
        full = len(held) + claims >= limit
        if full and not riding:
            return False, True, working, claims
        _pending_claims[key] = now + PENDING_CLAIM_TTL_S
        return True, full, working, claims


def release(key: str) -> None:
    """Drop the hold :func:`reserve` took under *key*."""
    with _lock:
        _pending_claims.pop(key, None)
        _pending_family.pop(key, None)


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
    used: int | None = None,
) -> str:
    """The context a held or dropped prompt carries: who holds the slots and the way out.

    :param working: The sessions holding slots, oldest start first.
    :param limit: The slot limit.
    :param mode: ``warn`` or ``block``; the way out is phrased for each.
    :param lot_url: Optional link to the Parking Lot page.
    :param pending: Claims made a moment ago whose rows do not show them yet.
    :param used: Slots held, when that is not one per session: a family holds one.
    """
    held = len(working) if used is None else used
    taken = held + pending
    # "4 of 4 working" read as if the held session were the 4th (2026-10-24, Reid):
    # name the count it would make, so the card plainly asks only past the limit.
    lines = [f"Slots full: {taken} of {limit} taken; this one would make {taken + 1} of {limit}."]
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
    limit: int = labels.SLOT_LIMIT,
    mode: str = "warn",
    lot_url: str | None = None,
    family_children: int = 3,
    helpers_count: bool = False,
) -> PolicyCallable:
    """Factory: gate the request phase on the number of working sessions.

    :param limit: Working sessions allowed at once; :data:`labels.SLOT_LIMIT`, which
        is ``AE_SLOT_LIMIT`` from the environment (three when unset), unless the
        policy's parameters set one. The seed sets none; a limit set on Settings >
        Global Policies wins, and since 2026-10-22 every count in the UI follows it
        too (:mod:`ae_omni_policies.slot_limit`).
    :param mode: ``warn`` holds the prompt for approval (ASK); ``block`` drops it (DENY).
    :param lot_url: Optional link to the Parking Lot page, named in the reason text.
    :param family_children: Children of one family working at once, ``0`` for no cap.
        The gate reads the Settings value through :func:`slot_limit.resolve` and falls
        back to this one when the store cannot be read.
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
            row = sessions.own_row(event=event)
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
        working = sessions.working_sessions(rows)
        keys = sessions.family_keys(rows)
        held = sessions.held_keys(
            working, helpers_count=helpers_count, by_id={row.id: row for row in rows}
        )
        own_family = keys.get(own_id) if own_id is not None else None
        orch_parent = labels.orch_parent(own)
        if own_family is None and orch_parent is not None:
            own_family = orch_parent
        # A member of a family never waits on the slot limit (2026-10-24): the lead drives
        # it and is the one session. Only top-level sessions with no ae.orch parent count.
        member = orch_parent is not None and own_family is not None and own_family != own_id
        # A lead whose family already holds a slot rides in it (2026-10-24): its own id is
        # the family key and that key is held, so it needs no second slot.
        rides_family = own_id is not None and own_family == own_id and own_id in held
        # The family cap shows as overflow on every member (2026-10-23), held for no one.
        cap = (
            slot_limit.resolve(
                slot_limit.SlotConfig(limit, helpers_count, family_children)
            ).family_children
            if member
            else 0
        )
        with _lock:
            pending = _live_pending(now, {item.id for item in working}, own_id)
            over = False
            if member and own_family is not None:
                children, claimed = _family_children(own_family, working, keys, pending)
                over = bool(cap) and len(children) + claimed >= cap
                if not helpers_count:
                    if own_id is not None:
                        _pending_claims[own_id] = now + PENDING_CLAIM_TTL_S
                        _pending_family[own_id] = own_family
                    return {"result": "ALLOW", "set_labels": _claim(overflow=over)}
            claims = _slot_claims(pending, helpers_count)
            if member and own_family is not None and own_id is not None:
                # Helpers count: it takes a slot like any session, and keeps its family tag.
                _pending_family[own_id] = own_family
            full = len(held) + claims >= limit
            if rides_family and own_id is not None:
                _pending_claims[own_id] = now + PENDING_CLAIM_TTL_S
                return {"result": "ALLOW", "set_labels": _claim(overflow=over)}
            if not full or riding:
                if own_id is not None:
                    _pending_claims[own_id] = now + PENDING_CLAIM_TTL_S
                if full:
                    return {
                        "result": "ALLOW",
                        "reason": "Riding unparked.",
                        "set_labels": _claim(overflow=True),
                    }
                return {"result": "ALLOW", "set_labels": _claim(overflow=over)}
            pending_count = claims

        reason = reason_text(
            [sessions.summarize(item, now=now) for item in working],
            limit,
            mode,
            lot_url,
            pending=pending_count,
            used=len(held),
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
    "FamilyFull",
    "reason_text",
    "release",
    "reserve",
    "reset_pending_claims",
    "slot_cap",
]
