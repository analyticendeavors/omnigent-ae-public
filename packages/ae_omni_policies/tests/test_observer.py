"""The observer's pure scan, its registered form and the request-phase fallback."""

from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any

import pytest
from omnigent.entities import PagedList
from omnigent.entities.conversation import Conversation, ConversationItem

from ae_omni_policies import labels, sessions
from ae_omni_policies import observer as observer_module
from ae_omni_policies.observer import (
    glyph_status,
    message_text,
    observe,
    observe_items,
    updates_for,
)

GATE = (
    "Padding fix is mid-edit.\n\n"
    "FORK: build a customer testimonials section for the home page\n"
    "QUADRANT: important, not urgent · SIZE: a block, medium confidence\n"
    "RECOMMEND: C, self-describing\n"
    "A act now · B handoff note here · C file to the parking lot · or close it"
)
NOTE = (
    "THREAD: eur-currency-formatting\n"
    "STATE: The helper drops the EUR symbol.\n"
    "NEXT: Open shared/js/money.js and add the EUR branch.\n"
    "SLOT: never opened\n"
    "QUADRANT: feature · EST: about an hour · FROM: pricing fix, Claude Code, 2026-09-23"
)


def _message(
    index: int, text: str, role: str = "assistant", *, meta: bool = False
) -> ConversationItem:
    data: dict[str, Any] = {
        "role": role,
        "content": [{"type": "output_text", "text": text}],
        **({"agent": "worker"} if role == "assistant" else {}),
        **({"is_meta": True} if meta else {}),
    }
    return ConversationItem(
        id=f"item_{index}",
        type="message",
        status="completed",
        response_id="r",
        created_at=1,
        data=data,
    )


def _rename(index: int, title: str) -> ConversationItem:
    return ConversationItem(
        id=f"item_{index}",
        type="function_call",
        status="completed",
        response_id="r",
        created_at=1,
        data={
            "agent": "worker",
            "name": "sys_session_rename",
            "arguments": json.dumps({"title": title}),
            "call_id": "c1",
        },
    )


# ── updates_for: the pure scan ───────────────────────────────────────


def test_a_fork_line_opens_the_gate_with_the_clause() -> None:
    assert updates_for([_message(1, GATE)], {}) == {
        labels.GATE: "open",
        labels.FORK: "build a customer testimonials section for the home page",
    }


def test_tokens_count_only_at_the_start_of_a_line() -> None:
    prose = (
        "No FORK: needed here; this is the current task. "
        "Not Absorbed: either, and UNFILED in prose."
    )
    assert updates_for([_message(1, prose)], {}) == {}
    indented = "  FORK: indented does not count either"
    assert updates_for([_message(1, indented)], {}) == {}


def test_the_last_fork_in_a_batch_wins_and_is_clipped() -> None:
    long_clause = "x" * 300
    items = [_message(1, "FORK: first"), _message(2, f"FORK: {long_clause}")]
    updates = updates_for(items, {})
    assert updates[labels.GATE] == "open"
    assert len(updates[labels.FORK]) == labels.TEXT_CAP
    assert updates[labels.FORK].endswith("...")


def test_counters_continue_from_the_current_labels() -> None:
    text = (
        "Absorbed: one, tiny.\nAbsorbed: two, also tiny.\n"
        "UNFILED (target unreachable), file this yourself"
    )
    current = {labels.ABSORBED: "3", labels.UNFILED: "1"}
    assert updates_for([_message(1, text)], current) == {
        labels.ABSORBED: "5",
        labels.UNFILED: "2",
        "ae.absorbed.4": "one, tiny.",
        "ae.absorbed.5": "two, also tiny.",
        "ae.unfiled.2": "(target unreachable), file this yourself",
    }


def test_each_counted_line_is_saved_as_a_note_of_its_own() -> None:
    # 2026-10-19: the Triage card lists these labels, so a note stays readable after
    # its message is too old to be loaded; the text is the line after its token.
    long_note = "y" * 300
    items = [
        _message(1, f"Absorbed: {long_note}\nAbsorbed:"),
        _message(2, "UNFILED:"),
        _message(3, "Absorbed: from the user", role="user"),
    ]
    updates = updates_for(items, {})
    assert updates[labels.ABSORBED] == "2"
    assert len(updates["ae.absorbed.1"]) == labels.TEXT_CAP
    assert updates["ae.absorbed.1"].endswith("...")
    assert updates["ae.absorbed.2"] == "Absorbed"
    assert updates["ae.unfiled.1"] == "UNFILED"
    assert "ae.absorbed.3" not in updates


def test_notes_stop_at_the_cap_while_the_count_goes_on() -> None:
    current = {labels.ABSORBED: str(labels.NOTE_CAP - 1)}
    updates = updates_for([_message(1, "Absorbed: a\nAbsorbed: b")], current)
    assert updates[labels.ABSORBED] == str(labels.NOTE_CAP + 1)
    assert updates[labels.note_key(labels.ABSORBED, labels.NOTE_CAP)] == "a"
    assert labels.note_key(labels.ABSORBED, labels.NOTE_CAP + 1) not in updates


def test_a_note_sets_next_only_with_thread() -> None:
    assert updates_for([_message(1, NOTE)], {}) == {
        labels.NEXT: "Open shared/js/money.js and add the EUR branch."
    }
    bare_next = "NEXT: without a THREAD line this is just prose."
    assert updates_for([_message(1, bare_next)], {}) == {}


def test_a_user_turn_answers_an_open_gate() -> None:
    assert updates_for([_message(1, "B", role="user")], {labels.GATE: "open"}) == {
        labels.GATE: "answered"
    }
    # In one batch: the gate opens, then the human answers.
    items = [_message(1, GATE), _message(2, "C", role="user")]
    assert updates_for(items, {})[labels.GATE] == "answered"
    # Nothing to answer, nothing written; a meta item is not the human.
    assert updates_for([_message(1, "hi", role="user")], {labels.GATE: "answered"}) == {}
    assert updates_for([_message(1, "ctx", role="user", meta=True)], {labels.GATE: "open"}) == {}


def test_user_lines_never_open_a_gate() -> None:
    assert updates_for([_message(1, GATE, role="user")], {}) == {}


@pytest.mark.parametrize(
    ("title", "status"),
    [
        ("🟡 Fix the build", "working"),
        ("🔴 Waiting on CI", "blocked"),
        ("✅ Renewal note", "review"),
        ("📌 Desk height", "parked"),
        ("🗂️ Old notes", "reference"),
        ("🗂 Old notes", "reference"),
        ("  🟡 padded", "working"),
        ("Fix the build 🟡", None),
        ("Fix the build", None),
        ("", None),
        (None, None),
    ],
)
def test_glyph_status(title: str | None, status: str | None) -> None:
    assert glyph_status(title) == status


def test_a_title_line_or_a_glyph_rename_maps_to_a_status() -> None:
    assert updates_for([_message(1, "Done.\nTitle: ✅ Renewal note")], {}) == {
        labels.STATUS: "review"
    }
    assert updates_for([_message(1, "Title: Renewal note")], {}) == {}
    assert updates_for([_rename(1, "📌 Desk height")], {}) == {labels.STATUS: "parked"}
    assert updates_for([_rename(1, "Desk height")], {}) == {}


def test_message_text_joins_text_blocks_only() -> None:
    item = _message(1, "a")
    item.data.content.append({"type": "input_image", "url": "x"})
    item.data.content.append({"type": "output_text", "text": "b"})
    assert message_text(item.data) == "a\nb"


# ── observe_items: the registered observer against a stub store ──────


class _Store:
    def __init__(self, rows: dict[str, Conversation], items: list[ConversationItem] | None = None):
        self.rows = rows
        self.items = items or []
        self.writes: list[tuple[str, dict[str, str]]] = []

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.rows.get(conversation_id)

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.writes.append((conversation_id, updates))
        self.rows[conversation_id].labels.update(updates)

    def list_items(self, conversation_id: str, **kwargs: Any) -> PagedList[ConversationItem]:
        assert kwargs == {"limit": observer_module.FALLBACK_LIMIT, "order": "desc"}
        page = list(reversed(self.items))[: kwargs["limit"]]
        return PagedList(data=page, first_id=None, last_id=None, has_more=False)


def _conv(
    id: str, title: str | None = None, labels_: dict[str, str] | None = None
) -> Conversation:
    return Conversation(
        id=id,
        created_at=1,
        updated_at=1,
        root_conversation_id=id,
        title=title,
        labels=labels_ or {},
    )


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    stub = _Store({"conv_a": _conv("conv_a", "Fix the build", {labels.ABSORBED: "1"})})
    monkeypatch.setattr(observer_module, "get_conversation_store", lambda: stub)
    return stub


def test_observe_items_writes_through_the_store(store: _Store) -> None:
    written = observe_items("conv_a", [_message(1, "Absorbed: x, y.\n" + GATE)])
    assert written == {
        labels.ABSORBED: "2",
        "ae.absorbed.2": "x, y.",
        labels.GATE: "open",
        labels.FORK: "build a customer testimonials section for the home page",
    }
    assert store.writes == [("conv_a", written)]
    assert observe_items("conv_a", [_message(2, "plain prose")]) == {}
    assert observe_items("conv_missing", [_message(3, GATE)]) == {}
    assert len(store.writes) == 1


def test_overlapping_batches_take_different_note_numbers(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The registry runs each batch on a worker thread of its own. Unlocked, both
    # would read ae.absorbed=1 and save their notes under ae.absorbed.2.
    read = store.get_conversation

    def slow_read(conversation_id: str) -> Conversation | None:
        snapshot = copy.deepcopy(read(conversation_id))
        time.sleep(0.05)
        return snapshot

    monkeypatch.setattr(store, "get_conversation", slow_read)
    batches = [
        threading.Thread(
            target=observe_items, args=("conv_a", [_message(i, f"Absorbed: line {i}")])
        )
        for i in (1, 2)
    ]
    for batch in batches:
        batch.start()
    for batch in batches:
        batch.join()
    saved = store.rows["conv_a"].labels
    assert saved[labels.ABSORBED] == "3"
    assert {saved["ae.absorbed.2"], saved["ae.absorbed.3"]} == {"line 1", "line 2"}


def test_a_legacy_title_glyph_maps_once_while_unplaced(store: _Store) -> None:
    store.rows["conv_g"] = _conv("conv_g", "🔴 Waiting on CI")
    assert observe_items("conv_g", [_message(1, "hello", role="user")]) == {
        labels.STATUS: "blocked"
    }
    # Placed now: the glyph never overrides a status, and nothing writes a glyph back.
    store.rows["conv_g"].labels[labels.STATUS] = "review"
    assert observe_items("conv_g", [_message(2, "again", role="user")]) == {}
    assert store.rows["conv_g"].title == "🔴 Waiting on CI"


def test_the_observer_registered_with_the_patched_upstream() -> None:
    from omnigent.server.item_observers import registered_item_observers

    assert observer_module.HAVE_ITEM_OBSERVERS is True
    assert observer_module.REGISTERED is True
    assert observe_items in registered_item_observers()


# ── observe: the request-phase fallback ──────────────────────────────


def _request(state: dict[str, Any] | None = None, labels_: dict[str, str] | None = None) -> dict:
    return {
        "type": "request",
        "data": {"user_content": "hello"},
        "context": {"labels": labels_ or {}},
        "session_state": state or {},
    }


def test_fallback_abstains_when_the_patch_is_present(store: _Store) -> None:
    assert observer_module.HAVE_ITEM_OBSERVERS is True
    assert observe(_request()) is None


def test_fallback_scans_new_items_once_and_answers_the_gate(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer_module, "HAVE_ITEM_OBSERVERS", False)
    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_a")
    store.items = [_message(1, "fix it", role="user"), _message(2, GATE)]

    first = observe(_request())
    assert first == {
        "result": "ALLOW",
        "state_updates": [{"key": observer_module.SEEN_KEY, "action": "set", "value": "item_2"}],
        "set_labels": {
            labels.GATE: "answered",
            labels.FORK: "build a customer testimonials section for the home page",
        },
    }
    # The engine applied the state; the same items are not scanned again.
    assert observe(_request({observer_module.SEEN_KEY: "item_2"})) is None
    # A newer item is: the gate is open again in the labels and this prompt answers it.
    store.items.append(_message(3, "Absorbed: tiny.\n" + GATE))
    store.rows["conv_a"].labels.update({labels.GATE: "open"})
    second = observe(_request({observer_module.SEEN_KEY: "item_2"}))
    assert second is not None
    assert second["set_labels"] == {
        labels.ABSORBED: "2",
        "ae.absorbed.2": "tiny.",
        labels.GATE: "answered",
        labels.FORK: "build a customer testimonials section for the home page",
    }
    assert second["state_updates"] == [
        {"key": observer_module.SEEN_KEY, "action": "set", "value": "item_3"}
    ]


def test_fallback_abstains_off_a_session_route_and_on_other_phases(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(observer_module, "HAVE_ITEM_OBSERVERS", False)
    monkeypatch.setattr(sessions, "current_session_id", lambda: None)
    assert observe(_request()) is None
    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_a")
    assert observe({"type": "tool_call", "data": {"name": "x"}, "context": {}}) is None


def test_fallback_fails_open_when_the_store_does_not_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import OperationalError

    class _Broken:
        def list_items(self, *args: Any, **kwargs: Any) -> Any:
            raise OperationalError("select", {}, Exception("locked"))

    monkeypatch.setattr(observer_module, "HAVE_ITEM_OBSERVERS", False)
    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_a")
    monkeypatch.setattr(observer_module, "get_conversation_store", lambda: _Broken())
    assert observe(_request()) is None
