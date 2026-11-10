"""The advise route and the advise_preset tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from test_decision_advisor import GOOD, KEY, PRESETS, answer
from test_mcp import _payload, _stub

from ae_omni_policies import decision_advisor as da
from ae_omni_policies import labels, preset_runs
from ae_omni_policies import router as router_module
from ae_omni_policies.mcp import TOOLS, WRITING_TOOLS, create_server

URL = "/v1/ae/sessions/conv_lead/advise"


class World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.messages: list[str] = ["please look at the report builder"]
        self.resolved: dict[str, Any] = {"lead_picks": True, "presets": PRESETS, "more": 0}
        self.may_write = True
        self.calls: list[httpx.Request] = []
        self.insights = True
        self.labels: dict[str, str] = {}
        self.endless = False  # the store always says there are more items
        self.pending: list[str] = []
        deny = tmp_path / "deny.txt"
        deny.write_text("Zorbex\n", encoding="utf-8")
        monkeypatch.setenv("AE_DECISION_ENABLED", "1")
        monkeypatch.setenv("AE_DECISION_ENDPOINT", "https://decision.example.com")
        monkeypatch.setenv("AE_DECISION_API_KEY", KEY)
        monkeypatch.setenv("AE_DECISION_USERS", "local")
        monkeypatch.setenv("AE_DECISION_DENYLIST_FILE", str(deny))
        monkeypatch.setenv(preset_runs.STATE_FILE_ENV, str(tmp_path / "state.json"))
        world = self

        class Store:
            def get_conversation(_self, sid: str) -> Any:
                return SimpleNamespace(id=sid, created_at=0)

            def list_items(_self, sid: str, **kw: Any) -> Any:
                data = [
                    SimpleNamespace(
                        data=SimpleNamespace(
                            role="user", content=[{"type": "input_text", "text": t}]
                        )
                    )
                    for t in world.messages
                ]
                data.append(
                    SimpleNamespace(
                        data=SimpleNamespace(role="assistant", content=[{"text": GOOD}])
                    )
                )
                if world.endless:
                    return SimpleNamespace(data=data, has_more=True, last_id="item_x")
                return SimpleNamespace(data=data, has_more=False, last_id=None)

        async def _conversation(sid: str) -> Any:
            if sid != "conv_lead":
                return None
            return SimpleNamespace(id=sid, labels=dict(world.labels), parent_conversation_id=None)

        async def _may_write(_user: str | None, _sid: str) -> bool:
            return world.may_write

        def handler(req: httpx.Request) -> httpx.Response:
            world.calls.append(req)
            return answer("deep", {"deep": 0.9, "balanced": 0.05})

        monkeypatch.setattr(router_module, "_conversation", _conversation)
        monkeypatch.setattr(router_module, "_may_write_session", _may_write)
        monkeypatch.setattr(router_module, "get_conversation_store", lambda: Store())
        monkeypatch.setattr(router_module.sessions, "list_since", lambda *a, **k: [])
        monkeypatch.setattr(router_module._owner_identity, "owner", lambda sid: "local")
        monkeypatch.setattr(
            router_module.ae_roles, "resolve_roles", lambda conv, store: world.resolved
        )
        monkeypatch.setattr(da, "_shared_transport", lambda: httpx.MockTransport(handler))
        monkeypatch.setattr(preset_runs, "insights_enabled", lambda _u: world.insights)
        monkeypatch.setattr(
            router_module.pending_inputs,
            "snapshot_for",
            lambda _sid: [
                {"pending_id": f"p{n}", "content": [{"type": "input_text", "text": t}]}
                for n, t in enumerate(world.pending)
            ],
        )
        da._calls.clear()

    async def advice(self) -> list[dict[str, Any]]:
        await asyncio.gather(*router_module._ADVISE_TASKS)  # the log write runs after the reply
        state = preset_runs.shared_state()
        with state.locked() as data:
            return list((data["reports"].get("conv_lead") or {}).get("advisor") or [])


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    return World(monkeypatch, tmp_path)


async def test_a_lead_picks_owner_gets_a_verdict_and_a_log_entry(
    client: httpx.AsyncClient, world: World
) -> None:
    response = await client.post(URL, json={"summary": GOOD, "files": 4, "kind": "code"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdict"] == "use" and body["preset"] == "deep" and body["title"] == "Deep"
    assert len(world.calls) == 1
    (entry,) = await world.advice()
    assert entry["verdict"] == "use" and entry["files"] == 4
    assert GOOD not in json.dumps(entry) and entry["summary_sha"] == da.summary_hash(GOOD)


async def test_insights_off_keeps_no_entry(client: httpx.AsyncClient, world: World) -> None:
    world.insights = False
    assert (await client.post(URL, json={"summary": GOOD})).json()["verdict"] == "use"
    assert await world.advice() == []


async def test_a_session_that_is_not_lead_picks_gets_decide_yourself(
    client: httpx.AsyncClient, world: World
) -> None:
    world.resolved = {"preset": PRESETS[0], "custom": False}
    body = (await client.post(URL, json={"summary": GOOD})).json()
    assert body == {"verdict": "decide_yourself", "reason": "not_lead_picks"}
    assert world.calls == []
    world.resolved = {"lead_picks": True, "presets": PRESETS, "more": 0}
    assert (await client.post(URL, json={"summary": GOOD})).json()["verdict"] == "use"


async def test_a_summary_copied_from_a_user_message_is_refused(
    client: httpx.AsyncClient, world: World
) -> None:
    world.messages.append(f"ok so {GOOD.lower()} and then ship it")
    body = (await client.post(URL, json={"summary": GOOD})).json()
    assert body == {"verdict": "decide_yourself", "reason": "refused_passthrough"}
    assert world.calls == []
    world.messages.pop()
    assert (await client.post(URL, json={"summary": GOOD})).json()["verdict"] == "use"


async def test_the_assistant_messages_do_not_count_as_passthrough(
    client: httpx.AsyncClient, world: World
) -> None:
    # The fake store always adds an assistant message equal to GOOD.
    assert (await client.post(URL, json={"summary": GOOD})).json()["verdict"] == "use"


async def test_not_the_owner_is_403_and_nothing_is_sent(
    client: httpx.AsyncClient, world: World
) -> None:
    world.may_write = False
    assert (await client.post(URL, json={"summary": GOOD})).status_code == 403
    assert world.calls == []
    world.may_write = True
    assert (await client.post(URL, json={"summary": GOOD})).status_code == 200


async def test_an_owner_not_on_the_allow_list_gets_decide_yourself(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_USERS", "someone@example.com")
    body = (await client.post(URL, json={"summary": GOOD})).json()
    assert body == {"verdict": "decide_yourself", "reason": "advisor_off"}
    assert world.calls == []
    monkeypatch.setenv("AE_DECISION_USERS", "local")
    assert (await client.post(URL, json={"summary": GOOD})).json()["verdict"] == "use"


async def test_unknown_session_and_bad_bodies(client: httpx.AsyncClient, world: World) -> None:
    assert (
        await client.post("/v1/ae/sessions/nope/advise", json={"summary": GOOD})
    ).status_code == 404
    assert (await client.post(URL, json={"summary": "short"})).status_code == 422
    assert (await client.post(URL, json={"summary": GOOD, "repo": "x"})).status_code == 422
    assert world.calls == []


async def test_a_failing_endpoint_is_still_200_decide_yourself(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        da, "_shared_transport", lambda: httpx.MockTransport(lambda r: httpx.Response(503))
    )
    response = await client.post(URL, json={"summary": GOOD})
    assert response.status_code == 200
    assert response.json() == {"verdict": "decide_yourself", "reason": "error"}
    assert (await world.advice())[0]["error_class"] == "http_503"


# -- the MCP tool


def test_the_tool_is_listed_and_counts_as_a_writer() -> None:
    tool = next(t for t in TOOLS if t.name == "advise_preset")
    assert tool.inputSchema["required"] == ["summary"]
    assert "advise_preset" in WRITING_TOOLS


async def test_the_tool_posts_as_the_calling_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "conv_lead")
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == URL
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"verdict": "use", "preset": "deep"})

    args = {"summary": GOOD, "files": 3}
    async with create_connected_server_and_client_session(create_server(_stub(handler))) as s:
        answered = _payload(await s.call_tool("advise_preset", args))
        monkeypatch.delenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID")
        monkeypatch.delenv("OMNIGENT_TERMINAL_LAUNCH_SESSION_ID", raising=False)
        missing = _payload(await s.call_tool("advise_preset", args))
    assert answered["verdict"] == "use" and seen == [args]
    assert missing["verdict"] == "decide_yourself"


async def test_the_tool_turns_a_server_error_into_decide_yourself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "conv_lead")
    async with create_connected_server_and_client_session(
        create_server(_stub(lambda r: httpx.Response(500, json={})))
    ) as s:
        out = _payload(await s.call_tool("advise_preset", {"summary": GOOD}))
    assert out == {"verdict": "decide_yourself", "reason": "error"}


# -- review round 1


async def test_a_long_transcript_fails_closed_instead_of_passing(
    client: httpx.AsyncClient, world: World
) -> None:
    world.endless = True  # more items than the page cap can read
    body = (await client.post(URL, json={"summary": GOOD})).json()
    assert body == {"verdict": "decide_yourself", "reason": "no_transcript"}
    assert world.calls == []
    world.endless = False
    assert (await client.post(URL, json={"summary": GOOD})).json()["verdict"] == "use"


async def test_a_lead_with_no_user_text_at_all_fails_closed(
    client: httpx.AsyncClient, world: World
) -> None:
    world.messages = []
    body = (await client.post(URL, json={"summary": GOOD})).json()
    assert body == {"verdict": "decide_yourself", "reason": "no_transcript"}
    assert world.calls == []
    world.messages = ["please look at the report builder"]
    assert (await client.post(URL, json={"summary": GOOD})).json()["verdict"] == "use"


async def test_a_message_still_queued_for_the_harness_counts_for_passthrough(
    client: httpx.AsyncClient, world: World
) -> None:
    # Typed input lives in pending_inputs until the harness takes it; only then is it an item.
    world.pending = [f"quick one: {GOOD.lower()}"]
    body = (await client.post(URL, json={"summary": GOOD})).json()
    assert body == {"verdict": "decide_yourself", "reason": "refused_passthrough"}
    assert world.calls == []
    world.pending = ["something else entirely"]
    assert (await client.post(URL, json={"summary": GOOD})).json()["verdict"] == "use"


async def test_a_helper_labelled_lead_picks_gets_decide_yourself(
    client: httpx.AsyncClient, world: World
) -> None:
    world.labels = {labels.ORCH: "conv_parent"}  # the real child check, not a mock
    body = (await client.post(URL, json={"summary": GOOD})).json()
    assert body == {"verdict": "decide_yourself", "reason": "helper"}
    assert world.calls == []
    world.labels = {}
    assert (await client.post(URL, json={"summary": GOOD})).json()["verdict"] == "use"


async def test_the_reply_does_not_wait_for_a_busy_run_log(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    release = threading.Event()
    real = preset_runs.record_advice

    def stuck(*args: Any, **kwargs: Any) -> bool:
        release.wait(timeout=10)
        return real(*args, **kwargs)

    monkeypatch.setattr(preset_runs, "record_advice", stuck)
    response = await asyncio.wait_for(client.post(URL, json={"summary": GOOD}), timeout=5)
    assert response.json()["verdict"] == "use"  # answered while the log write is still stuck
    assert router_module._ADVISE_TASKS  # and the write is still pending
    release.set()
    assert len(await world.advice()) == 1


async def test_a_looping_lead_is_cut_off_after_ten_calls(
    client: httpx.AsyncClient, world: World
) -> None:
    for _ in range(da.MAX_PER_RUN):
        assert (await client.post(URL, json={"summary": GOOD})).json()["verdict"] == "use"
    body = (await client.post(URL, json={"summary": GOOD})).json()
    assert body == {"verdict": "decide_yourself", "reason": "rate_limited"}
    assert len(world.calls) == da.MAX_PER_RUN
