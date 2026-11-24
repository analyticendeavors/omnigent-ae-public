"""Retired Decision-1 nudges: this policy no longer sends anything.

From 2026-11-13 to 2026-11-19 a server-wide policy added a short line at the start of a chat,
after a test run and after a reviewer returned, telling the agent to put its open small choices
in one ``ask_judge`` call. A replay of real calls showed a per-choice judge call costs about
146 output tokens plus an extra step to re-read context, while the model decides inline in
about 28 tokens, with no better quality. The reminder was a net token cost, so it is gone.

The module stays for one reason: a server whose policy store was seeded earlier still holds a
row with the handler ``ae_omni_policies.decision_nudge.nudge``, and that row must keep loading.
:func:`nudge` always abstains. Delete the row from the Global Policies page, then this file.
The judge itself (``ask_judge``, ``agent_judge.py``, the route, ``decision_status``, the shadow
log) is untouched.
"""

from __future__ import annotations

from omnigent.policies.schema import PolicyEvent, PolicyResponse


def nudge(_event: PolicyEvent) -> PolicyResponse | None:
    """Abstain on every event (the nudges are retired)."""
    return None


__all__ = ["nudge"]
