"""hurry_modes: exact messages set or clear ae.triage, everything else is untouched."""

from __future__ import annotations

import pytest

from ae_omni_policies import labels
from ae_omni_policies.hurry import hurry_modes


def _request(text: str) -> dict:
    return {"type": "request", "data": {"user_content": text}, "context": {"labels": {}}}


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("triage off", "off"),
        ("  Triage   CAPTURE ", "capture"),
        ("triage on", ""),
        ("triage off\n", "off"),
    ],
)
def test_mode_messages_write_the_label(text: str, value: str) -> None:
    assert hurry_modes(_request(text)) == {"result": "ALLOW", "set_labels": {labels.TRIAGE: value}}


@pytest.mark.parametrize(
    "text",
    ["triage off, I am slammed today. Now fix the padding", "please triage off", "triage", ""],
)
def test_other_messages_abstain(text: str) -> None:
    assert hurry_modes(_request(text)) is None


def test_native_hook_bare_string_data() -> None:
    assert hurry_modes({"type": "request", "data": "triage capture", "context": {}}) == {
        "result": "ALLOW",
        "set_labels": {labels.TRIAGE: "capture"},
    }


def test_other_phases_abstain() -> None:
    assert hurry_modes({"type": "tool_call", "data": {"name": "x"}, "context": {}}) is None
