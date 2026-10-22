"""Hurry modes: ``triage off``, ``triage capture``, ``triage on``.

The branch-discipline rules make these session-scoped, human-invoked and
never carried into a new conversation. Here they are a label on the session
(``ae.triage``), set by the exact message and read by anything that gates
(Phase 4's classifier abstains while it is set). ``triage on`` clears it by
writing ``""``, the only way a policy can unset a label.
"""

from __future__ import annotations

import re

from omnigent.policies.schema import PolicyEvent, PolicyResponse, request_user_text

from ae_omni_policies import labels

_MODE = re.compile(r"^\s*triage\s+(off|capture|on)\s*$", re.IGNORECASE)


def hurry_modes(event: PolicyEvent) -> PolicyResponse | None:
    """Set or clear ``ae.triage`` from a bare ``triage <mode>`` message.

    :param event: The policy event; only the ``request`` phase is read.
    :returns: ALLOW with the label write for a mode message, ``None`` (abstain)
        for everything else.
    """
    if event.get("type") != "request":
        return None
    match = _MODE.match(request_user_text(event.get("data")))
    if match is None:
        return None
    mode = match.group(1).lower()
    value = "" if mode == "on" else mode
    return {"result": "ALLOW", "set_labels": {labels.TRIAGE: value}}


__all__ = ["hurry_modes"]
