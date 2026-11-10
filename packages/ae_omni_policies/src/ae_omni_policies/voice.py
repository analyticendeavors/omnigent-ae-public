"""Voice mode: a spoken-style line on each message while ``ae.voice`` is ``on``.

The Voice mode switch in the composer writes the label (``POST /v1/ae/sessions/{id}/voice``).
While it is set, every message the person sends carries one extra line asking for a reply
that reads well aloud. The label is the only state; clearing it ends the effect.
"""

from __future__ import annotations

from typing import Any

from omnigent.policies.schema import PolicyEvent, PolicyResponse, request_user_text

from ae_omni_policies import labels, sessions
from ae_omni_policies.slot_cap import STORE_ERRORS

ON = "on"
SPOKEN_STYLE = (
    "Spoken reply: lead with the answer, short plain sentences, "
    "no tables, code, links or headers unless asked."
)


def _session_labels(event: PolicyEvent) -> dict[str, str]:
    """The session's labels from the store; the event's own copy when the store is silent."""
    context = event.get("context") or {}
    try:
        row = sessions.own_row(event=event)
    except STORE_ERRORS:
        row = None
    own = row.labels if row is not None else context.get("labels")
    return own if isinstance(own, dict) else {}


def voice_style(event: PolicyEvent) -> PolicyResponse | None:
    """Append the spoken-style line to the message when the session is in Voice mode.

    :param event: The policy event; only the ``request`` phase is read.
    :returns: ALLOW with the replaced ``user_content`` while ``ae.voice`` is ``on``,
        ``None`` (abstain) otherwise.
    """
    if event.get("type") != "request":
        return None
    if labels.label(_session_labels(event), labels.VOICE) != ON:
        return None
    data = event.get("data")
    text = request_user_text(data)
    if not text.strip() or SPOKEN_STYLE in text:
        return None
    # Keep the payload's own shape: the style line rides inside it, nothing is rebuilt.
    if isinstance(data, dict):
        replaced: Any = {**data, "user_content": f"{text}\n\n{SPOKEN_STYLE}"}
    elif isinstance(data, str):
        replaced = f"{data}\n\n{SPOKEN_STYLE}"
    elif isinstance(data, list):
        replaced = [*data, {"type": "text", "text": SPOKEN_STYLE}]
    else:
        return None
    return {"result": "ALLOW", "data": replaced}


__all__ = ["SPOKEN_STYLE", "voice_style"]
