"""voice_style: the spoken-style line rides on a message only while ae.voice is on."""

from __future__ import annotations

from typing import Any

import pytest

from ae_omni_policies import labels, sessions
from ae_omni_policies import voice as voice_module
from ae_omni_policies.voice import SPOKEN_STYLE, voice_style


class _Store:
    def __init__(self, row: Any | None = None, *, fail: Exception | None = None) -> None:
        self.row = row
        self.fail = fail

    def get_conversation(self, _id: str) -> Any | None:
        if self.fail is not None:
            raise self.fail
        return self.row


class _Row:
    def __init__(self, own: dict[str, str]) -> None:
        self.labels = own


def _request(text: str = "hello", own: dict[str, str] | None = None) -> dict[str, Any]:
    return {
        "type": "request",
        "data": {"user_content": text, "attachments": [{"filename": "a.txt"}]},
        "context": {"labels": own or {}, "conversation_id": "conv_x"},
    }


@pytest.fixture(autouse=True)
def _store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "get_conversation_store", lambda: _Store(None))


def test_on_appends_one_line_and_keeps_attachments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sessions, "get_conversation_store", lambda: _Store(_Row({labels.VOICE: "on"}))
    )
    got = voice_style(_request("What next?"))
    assert got is not None and got["result"] == "ALLOW"
    assert got["data"]["user_content"] == f"What next?\n\n{SPOKEN_STYLE}"
    assert got["data"]["attachments"] == [{"filename": "a.txt"}]


def test_event_labels_are_used_when_the_store_has_no_row() -> None:
    got = voice_style(_request(own={labels.VOICE: "on"}))
    assert got is not None and got["data"]["user_content"].endswith(SPOKEN_STYLE)


@pytest.mark.parametrize("own", [{}, {labels.VOICE: ""}, {labels.VOICE: "off"}])
def test_other_values_abstain(own: dict[str, str]) -> None:
    assert voice_style(_request(own=own)) is None


def test_other_phases_blank_text_and_repeats_abstain() -> None:
    assert (
        voice_style({"type": "response", "data": "x", "context": {"labels": {labels.VOICE: "on"}}})
        is None
    )
    assert voice_style(_request("  ", {labels.VOICE: "on"})) is None
    assert voice_style(_request(f"hi\n\n{SPOKEN_STYLE}", {labels.VOICE: "on"})) is None


def test_a_silent_store_falls_back_to_the_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sessions, "get_conversation_store", lambda: _Store(fail=OSError("down")))
    assert voice_style(_request(own={labels.VOICE: "on"})) is not None
    assert voice_module.ON == "on"


def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sessions, "get_conversation_store", lambda: _Store(_Row({labels.VOICE: "on"}))
    )


def _with_data(data: Any) -> dict[str, Any]:
    return {"type": "request", "data": data, "context": {"conversation_id": "conv_x"}}


def test_a_bare_string_stays_a_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    got = voice_style(_with_data("What next?"))
    assert got == {"result": "ALLOW", "data": f"What next?\n\n{SPOKEN_STYLE}"}


def test_content_blocks_keep_their_blocks_and_gain_one_text_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _on(monkeypatch)
    image = {"type": "image", "source": {"data": "abc"}}
    blocks = [{"type": "text", "text": "Look at this"}, image]
    got = voice_style(_with_data(blocks))
    assert got is not None
    assert got["data"] == [*blocks, {"type": "text", "text": SPOKEN_STYLE}]
    assert blocks[1] is image and len(blocks) == 2


def test_a_dict_keeps_its_other_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch)
    got = voice_style(_with_data({"user_content": "Hi", "attachments": [], "extra": 1}))
    assert got is not None
    assert got["data"] == {
        "user_content": f"Hi\n\n{SPOKEN_STYLE}",
        "attachments": [],
        "extra": 1,
    }


@pytest.mark.parametrize("data", [None, 5, 1.5, {"attachments": []}])
def test_any_other_shape_abstains(monkeypatch: pytest.MonkeyPatch, data: Any) -> None:
    _on(monkeypatch)
    assert voice_style(_with_data(data)) is None
