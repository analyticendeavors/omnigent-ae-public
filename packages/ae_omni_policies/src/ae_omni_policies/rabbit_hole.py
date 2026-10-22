"""Rabbit hole: one proposal per session when a task runs long.

A working session that has been open for hours or has gone through dozens
of turns is often a task that should have been split, parked or handed off.
This policy counts the session's turns in ``session_state`` and, once past
either threshold, holds the next prompt with one ASK naming the elapsed time,
the turn count and the session's title. Accepting acknowledges it for the
session (no second ask); declining drops that prompt, which is the human's
cue to park or hand off first. Propose, never lecture (decided 2026-09-23).

``hours: 0`` turns the policy off, Reid's switch; ``turns: 0`` drops the turn
threshold and keeps the time one. Elapsed time is measured from ``ae.started``
(the slot cap writes it when the session claims a slot); a session with no
start has no clock, so only the turn threshold applies to it. Sub-agent
conversations are never counted or asked: they run inside their parent's
slot. A note the server posts to continue existing work (the merge watcher's,
:mod:`ae_omni_policies.continuation`, 2026-10-21) is neither counted nor asked.
"""

from __future__ import annotations

import logging
import time

from omnigent.policies.schema import (
    PolicyCallable,
    PolicyEvent,
    PolicyResponse,
    StateUpdateEntry,
    request_user_text,
)

from ae_omni_policies import labels, sessions
from ae_omni_policies.continuation import is_system_continuation
from ae_omni_policies.slot_cap import STORE_ERRORS

_logger = logging.getLogger(__name__)

TURNS_KEY = "_ae_turns"
ACK_KEY = "_ae_rabbit_ack"


def _now() -> float:
    """The clock, as a function so tests can pin it."""
    return time.time()


def reason_text(
    title: str, elapsed_s: int | None, turns: int, hours: float, turn_limit: int
) -> str:
    """The proposal: where the session stands and what accepting or declining does."""
    stood = (
        f"{sessions.format_elapsed(elapsed_s)} over {turns} turns"
        if elapsed_s is not None
        else f"{turns} turns"
    )
    thresholds = [f"{hours:g}h"]
    if turn_limit > 0:
        thresholds.append(f"{turn_limit} turns")
    return (
        f'Rabbit hole check: "{title}" has run for {stood} (threshold {" or ".join(thresholds)}). '
        "Is this still one task? Accept to keep going without another ask in this session; "
        "decline to stop here and park or hand off the rest first."
    )


def rabbit_hole(hours: float = 4, turns: int = 60) -> PolicyCallable:
    """Factory: propose a check-in once a session runs past *hours* or *turns*.

    :param hours: Hours since ``ae.started`` before the ask; ``0`` disables the policy.
    :param turns: Prompts before the ask; ``0`` drops the turn threshold.
    :returns: The policy callable.
    :raises ValueError: On a negative threshold.
    """
    if hours < 0 or turns < 0:
        raise ValueError(
            f"rabbit_hole thresholds must be at least 0, got hours={hours!r}, turns={turns!r}"
        )

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        if event.get("type") != "request" or hours == 0:
            return None
        if is_system_continuation(request_user_text(event.get("data"))):
            return None
        state = event.get("session_state") or {}
        count = state.get(TURNS_KEY)
        count = (count if isinstance(count, int) else 0) + 1
        updates: list[StateUpdateEntry] = [{"key": TURNS_KEY, "action": "increment", "value": 1}]
        counted: PolicyResponse = {"result": "ALLOW", "state_updates": updates}
        if state.get(ACK_KEY):
            return counted
        context = event.get("context") or {}
        own = context.get("labels") or {}
        raw = labels.label(own, labels.STARTED)
        elapsed = int(_now()) - int(raw) if raw and raw.isdigit() else None
        over_time = elapsed is not None and elapsed >= hours * 3600
        over_turns = turns > 0 and count >= turns
        if not (over_time or over_turns):
            return counted
        try:
            row = sessions.own_row()
        except STORE_ERRORS:
            _logger.warning("rabbit_hole could not read the store; not asking", exc_info=True)
            return counted
        if row is not None and not sessions.is_top_level(row):
            return None
        title = sessions.display_title(row) if row is not None else "this session"
        return {
            "result": "ASK",
            "reason": reason_text(title, elapsed, count, hours, turns),
            "state_updates": [*updates, {"key": ACK_KEY, "action": "set", "value": "1"}],
        }

    return evaluate


__all__ = ["ACK_KEY", "TURNS_KEY", "rabbit_hole", "reason_text"]
