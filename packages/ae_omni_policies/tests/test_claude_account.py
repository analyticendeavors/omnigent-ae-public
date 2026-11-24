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

from ae_omni_policies import claude_account, limits, sessions
from ae_omni_policies import router as router_module

# The real check, kept before the world fixture swaps it for a stub.
_REAL_STORED_CHECK = claude_account.validate_stored_claude_account
_BUSY = (409, {"error": "claude_account_busy", "detail": "Busy."})


class _Store:
    """Holds each chat's labels; ``set_labels`` and ``delete_label`` are the real surface used."""

    def __init__(self, chats: dict[str, dict[str, str]]) -> None:
        self.chats = chats
        self.owners: dict[str, str] = {}
        self.parents: dict[str, str] = {}
        self.archived: set[str] = set()
        self.hosts: dict[str, str] = {}

    def get_session_owner(self, session_id: str, *, owner_only: bool = False) -> str:
        return self.owners.get(session_id, "owner")

    def _conv(self, session_id: str) -> Any:
        return SimpleNamespace(
            id=session_id,
            parent_conversation_id=self.parents.get(session_id),
            agent_id="agent",
            host_id=self.hosts.get(session_id, "host"),
            title=f"Chat {session_id}",
            archived=session_id in self.archived,
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
        self.unbound: set[str] = set()
        self.answers: dict[str, tuple[int, dict[str, str]] | None] = {}
        self.delays: dict[str, float] = {}
        # Per-chat answers used one by one before ``answers``: a tuple, ``None`` or an exception.
        self.sequence: dict[str, list[Any]] = {}
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
    state = SimpleNamespace(
        store=store,
        runner=runner,
        refusals=refusals,
        owned=owned,
        pinned=set(),
        truncated=False,
        stored_checks=[],
    )

    async def validate(_request: Any, *, source: Any, **_kw: Any) -> None:
        if source.id in refusals:
            raise refusals[source.id]

    async def validate_stored(*_a: Any, conv: Any, **_kw: Any) -> None:
        state.stored_checks.append(conv.id)
        if conv.id in refusals:
            raise refusals[conv.id]

    async def pinned(_request: Any) -> set[str]:
        return set(state.pinned)

    async def bound(_session_id: str, _router: Any, **_kw: Any) -> Any:
        return object() if runner.bound and _session_id not in runner.unbound else None

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
        queue = runner.sequence.get(session_id)
        if queue:
            answer = queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
        else:
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
    monkeypatch.setattr(claude_account, "validate_stored_claude_account", validate_stored)
    monkeypatch.setattr(claude_account, "_pinned_run_ids", pinned)
    monkeypatch.setattr(facade, "_get_runner_client", bound)
    monkeypatch.setattr(facade, "_forward_session_change_to_runner", forward)
    monkeypatch.setattr(routes_events, "get_server_runner_router", lambda: None)
    monkeypatch.setattr(router_module, "_may_write_session", may_write)
    monkeypatch.setattr(router_module, "_conversation", conversation)
    monkeypatch.setattr(
        claude_account.sessions,
        "list_every_top_level",
        lambda **_kw: ([store._conv(c) for c in store.chats], state.truncated),
    )
    yield state
    for task in list(claude_account._retries.values()):
        task.cancel()


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
    assert response.json() == {"account": "Backup", "relaunched": False, "queued": True}
    assert world.store.chats["conv_a"][KEY] == "Backup"
    assert world.runner.sent == []


async def test_a_switch_with_no_terminal_stamps_the_live_label_and_a_repeat_finds_it_done(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    # 2026-11-13: the live label follows the stored login, so a closed chat shows what it will
    # start on and a repeat has nothing left to do.
    world.runner.bound = False
    await _put(client, "conv_a", "Backup")
    assert world.store.chats["conv_a"][limits.ACCOUNT_LIVE_LABEL] == "Backup"
    response = await _put(client, "conv_a", "Backup")
    assert response.json() == {"account": "Backup", "relaunched": False}


async def test_picking_the_live_login_over_another_stored_one_with_no_terminal_is_queued(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    # Move all stored Backup while the chat was idle; picking Work again cancels that.
    world.runner.bound = False
    world.store.chats["conv_a"].update({KEY: "Backup", limits.ACCOUNT_LIVE_LABEL: "Work"})
    response = await _put(client, "conv_a", "Work")
    assert response.json() == {"account": "Work", "relaunched": False, "queued": True}
    assert world.store.chats["conv_a"][KEY] == "Work"
    assert world.store.chats["conv_a"][limits.ACCOUNT_LIVE_LABEL] == "Work"


async def test_a_stale_live_label_with_another_login_stored_restarts_the_terminal(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    # The runner's own stamp failed after a restart onto Backup, so the live label still says Work.
    world.store.chats["conv_a"].update({KEY: "Backup", limits.ACCOUNT_LIVE_LABEL: "Work"})
    response = await _put(client, "conv_a", "Work")
    assert response.json() == {"account": "Work", "relaunched": True}
    assert [sent[0] for sent in world.runner.sent] == ["conv_a"]


async def test_a_relaunch_stamps_the_live_label(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.store.chats["conv_a"].update({KEY: "Work", limits.ACCOUNT_LIVE_LABEL: "Work"})
    response = await _put(client, "conv_a", "Backup")
    assert response.json() == {"account": "Backup", "relaunched": True}
    assert world.store.chats["conv_a"][limits.ACCOUNT_LIVE_LABEL] == "Backup"


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


async def test_move_all_moves_idle_chats_holds_busy_and_leaves_same_login(
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
        "skipped": [],
        "pending": [
            {"id": "conv_b", "title": "Chat conv_b", "reason": "moves when its reply ends"}
        ],
        "queued": [],
    }
    assert sorted(sent[0] for sent in world.runner.sent) == ["conv_a", "conv_b"]
    # The busy chat keeps the new login stored for its next launch.
    assert world.store.chats["conv_b"] == {KEY: "Backup"}


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


async def _put_family(
    client: httpx.AsyncClient, session_id: str, account: str = "Backup"
) -> httpx.Response:
    return await client.put(
        f"/v1/ae/sessions/{session_id}/claude-account",
        json={"account": account, "include_children": True},
    )


def _family(world: SimpleNamespace) -> None:
    """conv_a leads conv_c, which leads conv_d; all one owner and writable."""
    world.store.chats["conv_c"] = {"ae.orch": "conv_a"}
    world.store.chats["conv_d"] = {"ae.orch": "conv_c"}
    world.owned.update({"conv_c", "conv_d"})


async def test_the_flag_moves_children_and_grandchildren(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    _family(world)
    response = await _put_family(client, "conv_a")
    assert response.status_code == 200
    body = response.json()
    assert body["relaunched"] is True
    assert body["children"] == {
        "moved": [
            {"id": "conv_c", "title": "Chat conv_c"},
            {"id": "conv_d", "title": "Chat conv_d"},
        ],
        "skipped": [],
        "pending": [],
        "queued": [],
    }
    assert world.store.chats["conv_d"][KEY] == "Backup"
    assert world.store.chats["conv_b"] == {}


async def test_a_busy_child_is_listed_with_its_title_and_reason(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    _family(world)
    world.runner.answers["conv_c"] = (409, {"error": "claude_account_busy", "detail": "Busy."})
    body = (await _put_family(client, "conv_a")).json()
    assert body["children"]["skipped"] == []
    assert body["children"]["pending"] == [
        {"id": "conv_c", "title": "Chat conv_c", "reason": "moves when its reply ends"}
    ]
    assert [row["id"] for row in body["children"]["moved"]] == ["conv_d"]
    assert world.store.chats["conv_c"][KEY] == "Backup"


async def test_a_refused_parent_leaves_its_children_alone(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    _family(world)
    world.runner.answers["conv_a"] = (409, {"error": "claude_account_busy", "detail": "Busy."})
    assert (await _put_family(client, "conv_a")).status_code == 409
    assert world.store.chats["conv_c"] == {"ae.orch": "conv_a"}
    assert sorted(sent[0] for sent in world.runner.sent) == ["conv_a"]


async def test_a_pending_parent_still_moves_its_children_and_answers_202(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    _family(world)
    world.runner.answers["conv_a"] = None
    response = await _put_family(client, "conv_a")
    assert response.status_code == 202
    assert response.json()["pending"] is True
    assert [row["id"] for row in response.json()["children"]["moved"]] == ["conv_c", "conv_d"]


async def test_an_orch_loop_ends(client: httpx.AsyncClient, world: SimpleNamespace) -> None:
    world.store.chats["conv_c"] = {"ae.orch": "conv_a"}
    world.store.chats["conv_a"]["ae.orch"] = "conv_c"
    world.owned.add("conv_c")
    body = (await _put_family(client, "conv_a")).json()
    assert [row["id"] for row in body["children"]["moved"]] == ["conv_c"]


async def test_a_fork_and_a_sub_agent_of_a_child_stay_put(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    _family(world)
    # A fork copies its source's ae.orch; a sub-agent is a top-level row with a parent.
    world.store.chats["conv_f"] = {"ae.orch": "conv_a", "omnigent.fork.source_id": "conv_c"}
    world.store.chats["conv_s"] = {"ae.orch": "conv_a"}
    world.store.parents["conv_s"] = "conv_a"
    world.owned.update({"conv_f", "conv_s"})
    body = (await _put_family(client, "conv_a")).json()
    assert [row["id"] for row in body["children"]["moved"]] == ["conv_c", "conv_d"]
    assert KEY not in world.store.chats["conv_f"] and KEY not in world.store.chats["conv_s"]


async def test_another_users_session_naming_the_lead_is_never_moved(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    _family(world)
    world.store.owners["conv_c"] = "stranger"
    # An admin passes the write check on the stranger's session; the owner check stops it.
    body = (await _put_family(client, "conv_a")).json()
    assert body["children"] == {"moved": [], "skipped": [], "pending": [], "queued": []}
    assert world.store.chats["conv_c"] == {"ae.orch": "conv_a"}
    assert "conv_c" not in [sent[0] for sent in world.runner.sent]


async def test_children_the_caller_cannot_write_are_left_out(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    _family(world)
    world.owned.discard("conv_c")
    body = (await _put_family(client, "conv_a")).json()
    assert [row["id"] for row in body["children"]["moved"]] == ["conv_d"]
    assert body["children"]["skipped"] == []


async def test_non_claude_and_same_login_children_are_left_out(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    _family(world)
    world.refusals["conv_c"] = OmnigentError(
        "Claude account selection requires a Claude native target.", code=ErrorCode.INVALID_INPUT
    )
    world.store.chats["conv_d"][limits.ACCOUNT_LIVE_LABEL] = "Backup"
    body = (await _put_family(client, "conv_a")).json()
    assert body["children"] == {"moved": [], "skipped": [], "pending": [], "queued": []}


async def test_without_the_flag_the_answer_has_no_children_key(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    _family(world)
    response = await _put(client, "conv_a", "Backup")
    assert response.json() == {"account": "Backup", "relaunched": True}
    assert world.store.chats["conv_c"] == {"ae.orch": "conv_a"}
    assert [sent[0] for sent in world.runner.sent] == ["conv_a"]


async def test_a_cascade_failure_keeps_the_parents_answer(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _family(world)

    def broken(**_kw: Any) -> list[Any]:
        raise RuntimeError("store down")

    monkeypatch.setattr(claude_account.sessions, "list_every_top_level", broken)
    response = await _put_family(client, "conv_a")
    assert response.status_code == 200
    body = response.json()
    assert body["relaunched"] is True
    assert body["children"] == {
        "moved": [],
        "skipped": [],
        "pending": [],
        "queued": [],
        "error": "children not attempted",
    }
    assert world.store.chats["conv_a"][KEY] == "Backup"


async def test_access_revoked_after_the_list_is_built_writes_no_label(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    _family(world)
    real = claude_account.sessions.list_every_top_level

    def revoke_after_listing(**kw: Any) -> Any:
        rows = real(**kw)
        world.owned.discard("conv_c")
        return rows

    monkeypatch.setattr(claude_account.sessions, "list_every_top_level", revoke_after_listing)
    state = {"first": True}

    async def may_write(_user: Any, session_id: str) -> bool:
        # The queueing check still passes; only the check before the write fails.
        if session_id == "conv_c" and state["first"]:
            state["first"] = False
            return True
        return session_id in world.owned

    monkeypatch.setattr(router_module, "_may_write_session", may_write)
    body = (await _put_family(client, "conv_a")).json()
    assert [row["id"] for row in body["children"]["moved"]] == ["conv_d"]
    assert body["children"]["skipped"] == []
    assert KEY not in world.store.chats["conv_c"]
    assert "conv_c" not in [sent[0] for sent in world.runner.sent]


async def test_a_store_without_owners_is_single_user_and_an_ownerless_lookup_excludes() -> None:
    assert claude_account._session_owner(object(), "conv_x") == claude_account.LOCAL_OWNER

    class _NoOwner:
        def get_session_owner(self, _sid: str, *, owner_only: bool = False) -> None:
            return None

    assert claude_account._session_owner(_NoOwner(), "conv_x") is None


async def test_children_with_no_terminal_are_listed_queued_not_moved(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    _family(world)
    world.runner.unbound.add("conv_d")
    body = (await _put_family(client, "conv_a")).json()
    assert [row["id"] for row in body["children"]["moved"]] == ["conv_c"]
    assert body["children"]["queued"] == [
        {"id": "conv_d", "title": "Chat conv_d", "reason": "starts at its next message"}
    ]
    assert world.store.chats["conv_d"][KEY] == "Backup"


async def test_a_family_switch_resets_a_child_whose_stored_pick_differs(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    # A child runs on Work with Backup stored for its next start; switching the family to Work
    # must store Work on it too, or its next message launches on Backup.
    _family(world)
    world.runner.bound = False
    world.store.chats["conv_c"].update({KEY: "Backup", limits.ACCOUNT_LIVE_LABEL: "Work"})
    world.store.chats["conv_d"].update({KEY: "Work", limits.ACCOUNT_LIVE_LABEL: "Work"})
    body = (await _put_family(client, "conv_a", "Work")).json()
    assert [row["id"] for row in body["children"]["queued"]] == ["conv_c"]
    assert body["children"]["moved"] == []
    assert world.store.chats["conv_c"][KEY] == "Work"


async def test_move_all_lists_chats_with_no_terminal_as_queued(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.runner.unbound.add("conv_b")
    world.store.chats["conv_a"].update({KEY: "Backup", limits.ACCOUNT_LIVE_LABEL: "Work"})
    response = await client.post("/v1/ae/claude-account/move-all", json={"account": "Work"})
    body = response.json()
    # conv_a runs on Work but has Backup stored, so it is reset (with a restart) too.
    assert body["moved"] == ["conv_a"]
    assert body["queued"] == [
        {"id": "conv_b", "title": "Chat conv_b", "reason": "starts at its next message"}
    ]
    assert world.store.chats["conv_a"][KEY] == "Work"


async def test_move_all_stores_the_login_on_archived_chats(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.store.chats["conv_z"] = {KEY: "", limits.ACCOUNT_LIVE_LABEL: ""}
    world.store.archived.add("conv_z")
    world.owned.add("conv_z")
    world.runner.unbound.add("conv_z")
    body = (await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})).json()
    assert body["queued"] == [
        {"id": "conv_z", "title": "Chat conv_z", "reason": "starts at its next message"}
    ]
    assert world.store.chats["conv_z"] == {KEY: "Backup", limits.ACCOUNT_LIVE_LABEL: "Backup"}
    assert all(sent[0] != "conv_z" for sent in world.runner.sent)


async def test_move_all_asks_the_store_for_archived_chats(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[dict[str, Any]] = []

    def listing(**kw: Any) -> tuple[list[Any], bool]:
        asked.append(kw)
        return [], False

    monkeypatch.setattr(claude_account.sessions, "list_every_top_level", listing)
    await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})
    assert asked and asked[0]["include_archived"] is True


async def test_a_busy_chat_is_retried_until_its_reply_ends(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_account, "RETRY_EVERY_S", 0.02)
    world.runner.answers["conv_b"] = (409, {"error": "claude_account_busy", "detail": "Busy."})
    body = (await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})).json()
    assert [row["id"] for row in body["pending"]] == ["conv_b"]
    await asyncio.sleep(0.1)  # still busy: retried, never restored to the old login
    assert world.store.chats["conv_b"][KEY] == "Backup"
    assert limits.ACCOUNT_LIVE_LABEL not in world.store.chats["conv_b"]
    del world.runner.answers["conv_b"]  # the reply ended
    await asyncio.sleep(0.1)
    assert world.store.chats["conv_b"][limits.ACCOUNT_LIVE_LABEL] == "Backup"
    assert "conv_b" not in claude_account._retries


async def test_a_busy_retry_stops_when_the_chat_is_switched_elsewhere(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_account, "RETRY_EVERY_S", 0.1)
    world.runner.sequence["conv_b"] = [_BUSY]
    await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})
    assert (await _put(client, "conv_b", "Work")).status_code == 200  # a later "This chat" switch
    sent = len(world.runner.sent)
    await asyncio.sleep(0.4)
    assert world.store.chats["conv_b"][KEY] == "Work"
    assert world.store.chats["conv_b"][limits.ACCOUNT_LIVE_LABEL] == "Work"
    assert len(world.runner.sent) == sent  # the superseded retry never asked the runner again
    assert "conv_b" not in claude_account._retries


async def test_a_busy_retry_gives_up_after_its_window(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_account, "RETRY_EVERY_S", 0.01)
    monkeypatch.setattr(claude_account, "RETRY_FOR_S", 0.05)
    world.runner.answers["conv_b"] = (409, {"error": "claude_account_busy", "detail": "Busy."})
    await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})
    await asyncio.sleep(0.2)
    assert "conv_b" not in claude_account._retries
    assert world.store.chats["conv_b"][KEY] == "Backup"


async def test_a_late_busy_refusal_never_overwrites_a_newer_pick(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    # Move-all to Backup reaches the runner first and is refused as busy, slowly; the user
    # picks Work for the chat meanwhile. The pick waits on the chat's lock, then wins.
    world.runner.delays["conv_b"] = 0.15
    world.runner.sequence["conv_b"] = [_BUSY, (200, {"relaunched": True})]
    mover = asyncio.create_task(
        client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})
    )
    await asyncio.sleep(0.05)
    put = await _put(client, "conv_b", "Work")
    body = (await mover).json()
    assert put.status_code == 200 and put.json() == {"account": "Work", "relaunched": True}
    assert [row["id"] for row in body["pending"]] == ["conv_b"]
    assert world.store.chats["conv_b"][KEY] == "Work"
    assert world.store.chats["conv_b"][limits.ACCOUNT_LIVE_LABEL] == "Work"
    await asyncio.sleep(0.1)
    assert "conv_b" not in claude_account._retries


async def test_an_older_move_waiting_on_the_lock_does_nothing_once_superseded(
    world: SimpleNamespace,
) -> None:
    conv = world.store._conv("conv_a")
    first = claude_account._claim("conv_a", "Backup")
    claude_account._claim("conv_a", "Work")
    answer = await claude_account.switch(None, "owner", conv, "Backup", claim=first)  # type: ignore[arg-type]
    assert answer == {"account": "Backup", "relaunched": False, "superseded": True}
    assert world.store.chats["conv_a"] == {}


async def test_a_busy_chat_moves_once_its_reply_ends_with_a_stamped_live_label(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real busy-then-free sequence for a chat already stamped on another login, run with force.
    monkeypatch.setattr(claude_account, "RETRY_EVERY_S", 0.02)
    world.store.chats["conv_b"] = {KEY: "Work", limits.ACCOUNT_LIVE_LABEL: "Work"}
    world.runner.sequence["conv_b"] = [_BUSY, _BUSY]
    body = (await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})).json()
    assert [row["id"] for row in body["pending"]] == ["conv_b"]
    assert world.store.chats["conv_b"] == {KEY: "Backup", limits.ACCOUNT_LIVE_LABEL: "Work"}
    await asyncio.sleep(0.2)
    assert world.store.chats["conv_b"] == {KEY: "Backup", limits.ACCOUNT_LIVE_LABEL: "Backup"}
    assert [s[0] for s in world.runner.sent].count("conv_b") == 3
    assert "conv_b" not in claude_account._retries


async def test_an_unanswered_runner_and_a_transient_error_keep_the_retry_going(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_account, "RETRY_EVERY_S", 0.02)
    world.runner.sequence["conv_b"] = [_BUSY, None, RuntimeError("socket reset")]
    await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})
    await asyncio.sleep(0.3)
    sent = [s[0] for s in world.runner.sent]
    assert sent.count("conv_b") == 4  # busy, unanswered, error, then the relaunch
    assert world.store.chats["conv_b"][limits.ACCOUNT_LIVE_LABEL] == "Backup"
    assert "conv_b" not in claude_account._retries


async def test_a_permanent_refusal_ends_the_retry(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_account, "RETRY_EVERY_S", 0.02)
    world.runner.sequence["conv_b"] = [_BUSY, (400, {"error": "bad", "detail": "No."})]
    await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})
    await asyncio.sleep(0.2)
    assert [s[0] for s in world.runner.sent].count("conv_b") == 2
    assert "conv_b" not in claude_account._retries
    assert (
        world.store.chats["conv_b"][KEY] == "Old" or world.store.chats["conv_b"][KEY] == "Backup"
    )


async def test_cancelling_a_retry_mid_switch_leaves_the_stored_login_and_frees_the_lock(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_account, "RETRY_EVERY_S", 0.02)
    world.runner.sequence["conv_b"] = [_BUSY]
    await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})
    world.runner.delays["conv_b"] = 5.0
    for _ in range(100):
        if [s[0] for s in world.runner.sent].count("conv_b") >= 2:
            break
        await asyncio.sleep(0.01)
    task = claude_account._retries["conv_b"]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert "conv_b" not in claude_account._retries
    assert world.store.chats["conv_b"][KEY] == "Backup"
    world.runner.delays["conv_b"] = 0
    put = await asyncio.wait_for(_put(client, "conv_b", "Backup"), timeout=2)
    assert put.status_code == 200  # the chat's lock was released by the cancel


async def test_move_all_walks_past_five_hundred_chats(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    for n in range(620):
        world.store.chats[f"conv_x{n}"] = {limits.ACCOUNT_LIVE_LABEL: "Work"}
        world.owned.add(f"conv_x{n}")
        world.runner.unbound.add(f"conv_x{n}")
    body = (await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})).json()
    assert len(body["queued"]) == 620
    assert "truncated" not in body
    assert world.store.chats["conv_x0"][KEY] == "Backup"
    assert world.store.chats["conv_x619"][KEY] == "Backup"


async def test_move_all_says_when_the_list_was_cut_at_the_hard_cap(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.truncated = True
    body = (await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})).json()
    assert body["truncated"] is True


class _Page:
    def __init__(self, data: list[Any], has_more: bool) -> None:
        self.data = data
        self.has_more = has_more
        self.last_id = data[-1].id if data else None


class _PagedStore:
    def __init__(self, count: int) -> None:
        self.rows = [SimpleNamespace(id=f"c{n:05d}") for n in range(count)]
        self.calls = 0

    def list_conversations(self, *, limit: int, after: str | None, **_kw: Any) -> _Page:
        self.calls += 1
        start = 0 if after is None else int(after[1:]) + 1
        chunk = self.rows[start : start + limit]
        return _Page(chunk, start + limit < len(self.rows))


def test_the_every_row_listing_pages_past_the_top_level_cap() -> None:
    store = _PagedStore(1122)
    capped = sessions.list_top_level(store)
    assert len(capped) == sessions.MAX_ROWS
    rows, truncated = sessions.list_every_top_level(store)
    assert len(rows) == 1122 and truncated is False


def test_the_every_row_listing_reports_a_hard_cap() -> None:
    rows, truncated = sessions.list_every_top_level(_PagedStore(450), hard_cap=300)
    assert len(rows) == 300 and truncated is True
    rows, truncated = sessions.list_every_top_level(_PagedStore(300), hard_cap=300)
    assert len(rows) == 300 and truncated is False


async def test_a_pinned_tasks_run_chats_are_skipped_with_a_reason(
    client: httpx.AsyncClient, world: SimpleNamespace
) -> None:
    world.pinned = {"conv_a"}
    body = (await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})).json()
    assert body["skipped"] == [
        {"id": "conv_a", "title": "Chat conv_a", "reason": "pinned by its scheduled task"}
    ]
    assert body["moved"] == ["conv_b"]
    assert world.store.chats["conv_a"] == {}


def test_pinned_run_ids_come_from_the_runs_of_pinned_tasks_only() -> None:
    from omnigent.server.scheduled import task_account

    class TaskStore:
        def list(self) -> list[Any]:
            return [SimpleNamespace(id="t_pinned"), SimpleNamespace(id="t_free")]

        def list_runs(self, task_id: str, *, limit: int, after_id: str | None) -> Any:
            ids = {"t_pinned": ["conv_a", "conv_old"], "t_free": ["conv_b"]}[task_id]
            return [SimpleNamespace(conversation_id=i) for i in ids], None

    store = TaskStore()
    task_account.set_account(store, "t_pinned", "Main")
    try:
        assert claude_account._pinned_run_ids_sync(store) == {"conv_a", "conv_old"}
    finally:
        task_account.set_account(store, "t_pinned", None)


async def test_a_shared_login_moves_chats_on_every_host(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_account, "known_on_server", lambda a: a.startswith("shared:"))
    world.store.hosts["conv_b"] = "laptop"
    body = (
        await client.post(
            "/v1/ae/claude-account/move-all",
            json={"account": "shared:AE Backup", "host_id": "host"},
        )
    ).json()
    assert sorted(body["moved"]) == ["conv_a", "conv_b"]


async def test_a_host_login_moves_only_the_named_hosts_chats(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_account, "known_on_server", lambda _a: False)
    world.store.hosts["conv_b"] = "laptop"
    body = (
        await client.post(
            "/v1/ae/claude-account/move-all", json={"account": "Backup", "host_id": "host"}
        )
    ).json()
    assert body["moved"] == ["conv_a"]


async def test_cap_pending_chats_keep_their_own_reason(
    client: httpx.AsyncClient, world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_account, "MOVE_ALL_CAP_S", 0.05)
    world.runner.delays["conv_b"] = 0.3
    body = (await client.post("/v1/ae/claude-account/move-all", json={"account": "Backup"})).json()
    assert body["pending"][0]["reason"] == "still switching"


class _Hosts:
    def __init__(self, owner: str = "owner", sandbox: Any = None) -> None:
        self.calls = 0
        self.owner = owner
        self.sandbox = sandbox

    def get_host(self, _host_id: str) -> Any:
        self.calls += 1
        return SimpleNamespace(user_id=self.owner, sandbox_provider=self.sandbox, name="Laptop")


def _stored_request(hosts: _Hosts, *, online: bool) -> Any:
    registry = SimpleNamespace(get=lambda _h: object() if online else None)
    state = SimpleNamespace(host_store=hosts, host_registry=registry)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _no_agent_chat(**labels: str) -> Any:
    return SimpleNamespace(id="conv_z", host_id="host", labels=labels, inference_snapshot=None)


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from omnigent.server.routes import hosts

    calls = SimpleNamespace(n=0, credentials=[{"family": "claude-oauth", "env_var": "Backup"}])

    async def detect(**_kw: Any) -> dict[str, Any]:
        calls.n += 1
        return {"credentials": calls.credentials}

    monkeypatch.setattr(hosts, "_proxy_detect_credentials", detect)
    monkeypatch.setattr(claude_account, "known_on_server", lambda a: a.startswith("shared:"))
    return calls


async def _stored(request: Any, conv: Any, account: str, cache: Any = None) -> None:
    await _REAL_STORED_CHECK(
        request, account=account, agent=None, conv=conv, user="owner", host_checks=cache
    )


async def test_a_shared_login_on_an_offline_host_is_written_without_a_probe(
    probe: SimpleNamespace,
) -> None:
    conv = _no_agent_chat(**{KEY: ""})
    await _stored(_stored_request(_Hosts(), online=False), conv, "shared:AE Backup")
    assert probe.n == 0


async def test_a_host_login_on_an_offline_host_is_skipped_with_a_clear_reason(
    probe: SimpleNamespace,
) -> None:
    conv = _no_agent_chat(**{KEY: ""})
    with pytest.raises(
        OmnigentError, match="Backup is a login on the host Laptop, which is offline"
    ):
        await _stored(_stored_request(_Hosts(), online=False), conv, "Backup")


async def test_a_host_login_is_probed_once_per_host_across_chats(probe: SimpleNamespace) -> None:
    request = _stored_request(_Hosts(), online=True)
    cache: dict[Any, Any] = {}
    chats = [_no_agent_chat(**{limits.ACCOUNT_LIVE_LABEL: ""}) for _ in range(5)]
    await asyncio.gather(*(_stored(request, chat, "Backup", cache) for chat in chats))
    assert probe.n == 1
    probe.credentials = []
    with pytest.raises(OmnigentError, match="unavailable on this host"):
        await _stored(request, chats[0], "Backup")


async def test_the_stored_check_keeps_the_host_owner_check() -> None:
    conv = _no_agent_chat(**{KEY: ""})
    request = _stored_request(_Hosts(owner="someone_else"), online=True)
    with pytest.raises(OmnigentError) as caught:
        await _stored(request, conv, "shared:AE Backup")
    assert caught.value.code == ErrorCode.FORBIDDEN


async def test_a_chat_with_no_claude_login_label_and_no_agent_is_not_a_claude_chat(
    probe: SimpleNamespace,
) -> None:
    with pytest.raises(OmnigentError, match="Claude native target"):
        await _stored(_stored_request(_Hosts(), online=True), _no_agent_chat(), "Backup")
