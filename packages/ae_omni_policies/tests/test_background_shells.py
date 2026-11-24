"""The Stop edge's background shell count, kept as a label, and the note that revives the chat."""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any

import pytest
from omnigent.server.ae_turn_edges import TurnEdge

from ae_omni_policies import background_shells, compact_queue, labels
from ae_omni_policies.continuation import BACKGROUND_RESUME_TEXT, is_system_continuation


@pytest.fixture()
def store(ae_stores: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    the_store = ae_stores["conversation_store"]
    monkeypatch.setattr(background_shells, "_store", lambda: the_store)
    return the_store


def _session(store: Any, session_labels: dict[str, str] | None = None) -> str:
    conv = store.create_conversation(
        title="planner", agent_id=uuid.uuid4().hex, labels=session_labels or {}
    )
    return conv.id


def _shells(store: Any, sid: str) -> str | None:
    return (store.get_conversation(sid).labels or {}).get(labels.BG_SHELLS)


def test_a_count_sets_the_label_and_zero_removes_it(store: Any) -> None:
    sid = _session(store)
    background_shells.record(sid, 2)
    assert _shells(store, sid) == "2"
    assert background_shells.pending(store.get_conversation(sid))

    background_shells.record(sid, 0)
    assert _shells(store, sid) is None
    assert not background_shells.pending(store.get_conversation(sid))
    # Zero on a chat without the label, or on a missing chat, changes nothing.
    background_shells.record(sid, 0)
    background_shells.record(uuid.uuid4().hex, 0)


@pytest.mark.parametrize("value", ["0", "", "x", "-1"])
def test_only_a_positive_count_is_pending(store: Any, value: str) -> None:
    sid = _session(store, {labels.BG_SHELLS: value})
    assert not background_shells.pending(store.get_conversation(sid))


async def test_a_stop_edge_from_a_worker_thread_reaches_the_label(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = _session(store)
    monkeypatch.setattr(background_shells, "_loop", asyncio.get_running_loop())

    edge = TurnEdge(sid, "idle", None, True, 3)
    await asyncio.to_thread(background_shells.on_edge, edge)
    for _ in range(50):
        if _shells(store, sid) == "3":
            break
        await asyncio.sleep(0.02)
    assert _shells(store, sid) == "3"


async def test_writes_for_one_session_run_in_order_and_the_newest_count_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two Stop edges close together: a slow "1" must not land after the "0" that followed.
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(background_shells, "_loop", loop)
    release = threading.Event()
    written: list[tuple[str, int]] = []
    active: list[str] = []

    def slow_record(session_id: str, count: int) -> None:
        assert session_id not in active, "two writes for one session overlapped"
        active.append(session_id)
        try:
            if session_id == "conv_a" and count == 1:
                release.wait(5)
            written.append((session_id, count))
        finally:
            active.remove(session_id)

    monkeypatch.setattr(background_shells, "record", slow_record)
    background_shells.on_edge(TurnEdge("conv_a", "idle", None, True, 1))
    for _ in range(100):
        if active:
            break
        await asyncio.sleep(0.01)
    assert active == ["conv_a"]
    # The "1" is mid-write; the next two edges wait, and only the newest is written.
    background_shells.on_edge(TurnEdge("conv_a", "idle", None, True, 2))
    background_shells.on_edge(TurnEdge("conv_a", "idle", None, True, 0))
    background_shells.on_edge(TurnEdge("conv_b", "idle", None, True, 4))
    for _ in range(100):
        if ("conv_b", 4) in written:
            break
        await asyncio.sleep(0.01)
    # Another session is not held up by conv_a's slow write.
    assert ("conv_b", 4) in written
    release.set()
    for _ in range(100):
        if not background_shells._writing:
            break
        await asyncio.sleep(0.02)

    assert [count for sid, count in written if sid == "conv_a"] == [1, 0]
    assert ("conv_b", 4) in written
    assert not background_shells._latest


@pytest.mark.parametrize(
    "edge",
    [
        TurnEdge("conv_x", "idle", None, False, 3),
        TurnEdge("conv_x", "idle", None, True, None),
        TurnEdge("conv_x", "running", None, False, None),
    ],
)
def test_other_edges_are_ignored(monkeypatch: pytest.MonkeyPatch, edge: TurnEdge) -> None:
    calls: list[Any] = []

    class Loop:
        def call_soon_threadsafe(self, *args: Any) -> None:
            calls.append(args)

    monkeypatch.setattr(background_shells, "_loop", Loop())
    background_shells.on_edge(edge)
    assert calls == []


class Poster:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, answers: list[tuple[int, str]]) -> None:
        self.answers = answers
        self.bodies: list[tuple[str, dict[str, Any]]] = []

        async def _post(app: Any, identity: Any, sid: str, body: dict[str, Any]) -> Any:
            self.bodies.append((sid, body))
            return self.answers.pop(0)

        monkeypatch.setattr(compact_queue, "post_event", _post)
        monkeypatch.setattr(background_shells, "_app", object())
        monkeypatch.setattr(background_shells, "_identity", object())
        monkeypatch.setattr(background_shells, "RETRY_WAIT_S", 0.0)


async def test_wake_posts_the_note_and_retries_a_busy_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poster = Poster(monkeypatch, [(503, "starting"), (202, "")])
    assert await background_shells.wake("conv_a") == "posted"
    assert len(poster.bodies) == 2
    sid, body = poster.bodies[0]
    assert sid == "conv_a"
    assert body["data"]["content"] == [{"type": "input_text", "text": BACKGROUND_RESUME_TEXT}]


async def test_wake_stops_on_a_refusal_and_is_off_without_the_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poster = Poster(monkeypatch, [(403, "forbidden")])
    assert await background_shells.wake("conv_a") == "HTTP 403: forbidden"
    assert len(poster.bodies) == 1

    monkeypatch.setattr(background_shells, "_app", None)
    assert await background_shells.wake("conv_a") == "off"


def test_the_note_passes_the_continuation_gates_only_whole() -> None:
    assert is_system_continuation(BACKGROUND_RESUME_TEXT)
    assert not is_system_continuation("Please also " + BACKGROUND_RESUME_TEXT)


async def test_the_lifespan_registers_and_unregisters_the_observer() -> None:
    from fastapi import FastAPI
    from omnigent.server.ae_turn_edges import _observers

    app = FastAPI()
    async with background_shells.lifespan(app):
        assert background_shells.on_edge in _observers
        assert background_shells._app is app
    assert background_shells.on_edge not in _observers
    assert background_shells._app is None
