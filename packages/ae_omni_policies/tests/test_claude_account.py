"""Switching a Claude chat's login in place, and moving every open chat (2026-10-27)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.onboarding.claude_accounts import CLAUDE_ACCOUNT_LABEL_KEY as KEY

from ae_omni_policies import claude_account, limits
from ae_omni_policies import router as router_module


class _Store:
    """Holds each chat's labels; ``set_labels`` and ``delete_label`` are the real surface used."""

    def __init__(self, chats: dict[str, dict[str, str]]) -> None:
        self.chats = chats

    def _conv(self, session_id: str) -> Any:
        return SimpleNamespace(
            id=session_id,
            agent_id="agent",
            host_id="host",
            title=f"Chat {session_id}",
            labels=dict(self.chats[session_id]),
        )

    def get_conversation(self, session_id: str) -> Any:
        return self._conv(session_id) if session_id in self.chats else None

    def set_labels(self, session_id: str, updates: dict[str, str]) -> None:
        self.chats[session_id].update(updates)

    def delete_label(self, session_id: str, key: str) -> None:
        self.chats[session_id].pop(key, None)


class _Runner:
    """Answers the forwarded event: ``status`` and ``body``, or ``None`` for no answer."""

    def __init__(self) -> None:
        self.bound = True
        self.answers: dict[str, tuple[int, dict[str, str]] | None] = {}
        self.delays: dict[str, float] = {}
        self.in_flight = 0
        self.peak = 0
        self.sent: list[tuple[str, dict[str, Any], float]] = []


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    from omnigent.server.routes import sessions as facade
    from omnigent.server.routes.sessions import ae_fork_claude_account, routes_events

    store = _Store({"conv_a": {}, "conv_b": {}})
    runner = _Runner()
    refusals: dict[str, OmnigentError] = {}
    owned: set[str] = {"conv_a", "conv_b"}
    state = SimpleNamespace(store=store, runner=runner, refusals=refusals, owned=owned)

    async def validate(_request: Any, *, source: Any, **_kw: Any) -> None:
        if source.id in refusals:
            raise refusals[source.id]

    async def bound(_session_id: str, _router: Any, **_kw: Any) -> Any:
        return object() if runner.bound else None

    async def forward(
        session_id: str, _router: Any, event: dict[str, Any], timeout_s: float = 5.0
    ) -> Any:
        runner.sent.append((session_id, event, timeout_s))
        runner.in_flight += 1
        runner.peak = max(runner.peak, runner.in_flight)
        try:
            await asyncio.sleep(runner.delays.get(session_id, 0))
        finally:
            runner.in_flight -= 1
        answer = runner.answers.get(session_id, (200, {"relaunched": True}))
        if answer is None:
            return None
        return SimpleNamespace(status_code=answer[0], body=json.dumps(answer[1]))

    async def may_write(_user: Any, session_id: str) -> bool:
        return session_id in owned

    async def conversation(session_id: str) -> Any:
        return store.get_conversation(session_id)

    monkeypatch.setattr(claude_account, "get_conversation_store", lambda: store)
    monkeypatch.setattr(
        claude_account, "get_agent_store", lambda: SimpleNamespace(get=lambda _i: 1)
    )
    monkeypatch.setattr(ae_fork_claude_account, "validate_fork_claude_account", validate)
    monkeypatch.setattr(facade, "_get_runner_client", bound)
    monkeypatch.setattr(facade, "_forward_session_change_to_runner", forward)
    monkeypatch.setattr(routes_events, "get_server_runner_router", lambda: None)
    monkeypatch.setattr(router_module, "_may_write_session", may_write)
    monkeypatch.setattr(router_module, "_conversation", conversation)
    monkeypatch.setattr(
        claude_account.sessions,
        "list_top_level",
        lambda **_kw: [store._conv(c) for c in store.chats],
    )
    yield state


async def _put(client: httpx.AsyncClient, session_id: str, account: str) -> httpx.Response:
    return await client.put(
        f"/v1/ae/sessions/{session_id}/claude-account", json={"account": account}
    )


async def test_the_label_is_written_and_the_runner_restarts(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    response = await _put(client, "conv_a", "Backup")
    assert response.status_code == 200
    assert response.json() == {"account": "Backup", "relaunched": True}
    assert world.store.chats["conv_a"][KEY] == "Backup"
    assert world.runner.sent == [("conv_a", {"type": "claude_account_change"}, 90.0)]


async def test_the_default_login_is_an_empty_label(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.store.chats["conv_a"][limits.ACCOUNT_LIVE_LABEL] = "Backup"
    assert (await _put(client, "conv_a", "")).json()["relaunched"] is True
    assert world.store.chats["conv_a"][KEY] == ""


async def test_a_runner_refusal_restores_the_old_label(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.store.chats["conv_a"][KEY] = "Work"
    world.runner.answers["conv_a"] = (
        409,
        {"error": "claude_account_busy", "detail": "Mid-reply."},
    )
    response = await _put(client, "conv_a", "Backup")
    assert response.status_code == 409
    assert "Mid-reply." in json.dumps(response.json())
    assert world.store.chats["conv_a"][KEY] == "Work"


async def test_a_refusal_removes_a_label_the_chat_never_had(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.runner.answers["conv_a"] = (409, {"error": "claude_account_busy", "detail": "Busy."})
    assert (await _put(client, "conv_a", "Backup")).status_code == 409
    assert KEY not in world.store.chats["conv_a"]


async def test_the_fork_checks_pass_through_and_write_nothing(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.refusals["conv_a"] = OmnigentError(
        "Selected Claude account is unavailable on this host.", code=ErrorCode.INVALID_INPUT
    )
    response = await _put(client, "conv_a", "Gone")
    assert response.status_code == 400
    assert "unavailable on this host" in json.dumps(response.json())
    assert world.store.chats["conv_a"] == {}
    assert world.runner.sent == []


async def test_no_runner_keeps_the_label(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.runner.bound = False
    response = await _put(client, "conv_a", "Backup")
    assert response.status_code == 200
    assert response.json() == {"account": "Backup", "relaunched": False}
    assert world.store.chats["conv_a"][KEY] == "Backup"
    assert world.runner.sent == []


async def test_an_unanswered_restart_is_a_pending_202(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.runner.answers["conv_a"] = None
    response = await _put(client, "conv_a", "Backup")
    assert response.status_code == 202
    assert response.json()["pending"] is True
    assert world.store.chats["conv_a"][KEY] == "Backup"


async def test_a_chat_already_on_the_login_is_locked_without_a_restart(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.store.chats["conv_a"][limits.ACCOUNT_LIVE_LABEL] = "Backup"
    response = await _put(client, "conv_a", "Backup")
    assert response.json() == {"account": "Backup", "relaunched": False}
    assert world.store.chats["conv_a"][KEY] == "Backup"
    assert world.runner.sent == []


async def test_a_locked_chat_on_the_login_is_left_alone(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.store.chats["conv_a"].update({KEY: "Backup", limits.ACCOUNT_LIVE_LABEL: "Backup"})
    response = await _put(client, "conv_a", "Backup")
    assert response.json() == {"account": "Backup", "relaunched": False}
    assert world.runner.sent == []


async def test_retrying_the_server_label_login_restarts_a_terminal_on_another_login(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    # A failed restart left the server label on Backup while the terminal still runs on Work.
    world.store.chats["conv_a"].update({KEY: "Backup", limits.ACCOUNT_LIVE_LABEL: "Work"})
    response = await _put(client, "conv_a", "Backup")
    assert response.json() == {"account": "Backup", "relaunched": True}
    assert [sent[0] for sent in world.runner.sent] == ["conv_a"]


async def test_a_refusal_puts_the_live_label_back_too(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.store.chats["conv_a"].update({KEY: "Work", limits.ACCOUNT_LIVE_LABEL: "Work"})
    world.runner.answers["conv_a"] = (
        409,
        {"error": "claude_account_not_started", "detail": "No."},
    )
    assert (await _put(client, "conv_a", "Backup")).status_code == 409
    assert world.store.chats["conv_a"] == {KEY: "Work", limits.ACCOUNT_LIVE_LABEL: "Work"}


async def test_only_the_owner_may_switch(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.owned.discard("conv_a")
    assert (await _put(client, "conv_a", "Backup")).status_code == 403
    assert (await _put(client, "conv_gone", "Backup")).status_code == 404
    assert world.store.chats["conv_a"] == {}


async def test_move_all_moves_idle_chats_skips_busy_and_leaves_same_login(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.store.chats["conv_c"] = {limits.ACCOUNT_LIVE_LABEL: "Backup"}
    world.store.chats["conv_d"] = {}
    world.refusals["conv_d"] = OmnigentError(
        "Claude account selection requires a Claude native target.", code=ErrorCode.INVALID_INPUT
    )
    world.runner.answers["conv_b"] = (409, {"error": "claude_account_busy", "detail": "Busy."})
    response = await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})
    assert response.status_code == 200
    assert response.json() == {
        "moved": ["conv_a"],
        "skipped": [{"id": "conv_b", "title": "Chat conv_b", "reason": "mid-reply"}],
        "pending": [],
    }
    assert sorted(sent[0] for sent in world.runner.sent) == ["conv_a", "conv_b"]
    assert world.store.chats["conv_b"] == {}


async def test_move_all_passes_the_checks_reason_and_ignores_other_owners(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.owned.discard("conv_b")
    world.refusals["conv_a"] = OmnigentError(
        "Claude account selection requires a connected host.", code=ErrorCode.CONFLICT
    )
    response = await client.post(
        "/v1/ae/claude-account/move-all", json={"account": "Backup", "host_id": "host"}
    )
    body = response.json()
    assert body["moved"] == []
    assert body["skipped"] == [
        {
            "id": "conv_a",
            "title": "Chat conv_a",
            "reason": "Claude account selection requires a connected host.",
        }
    ]


async def test_move_all_switches_a_few_chats_at_a_time(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    for n in range(6):
        world.store.chats[f"conv_x{n}"] = {}
        world.owned.add(f"conv_x{n}")
        world.runner.delays[f"conv_x{n}"] = 0.02
    body = (await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})).json()
    assert len(body["moved"]) == 8 and body["pending"] == []
    assert 1 < world.runner.peak <= claude_account.MOVE_ALL_CONCURRENCY


async def test_move_all_reports_a_slow_chat_as_pending_and_it_finishes_alone(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_account, "MOVE_ALL_CAP_S", 0.05)
    world.runner.delays["conv_b"] = 0.3
    body = (await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})).json()
    assert body["moved"] == ["conv_a"]
    assert body["pending"] == [
        {"id": "conv_b", "title": "Chat conv_b", "reason": "still switching"}
    ]
    await asyncio.sleep(0.5)
    assert world.store.chats["conv_b"][KEY] == "Backup"
