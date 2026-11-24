"""The judge route and the ask_judge tool: guard refusals, roles, rate limit, log, failures."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from test_mcp import _payload, _stub

from ae_omni_policies import agent_judge
from ae_omni_policies import decision_advisor as da
from ae_omni_policies import decision_shadow as ds
from ae_omni_policies import router as router_module
from ae_omni_policies.mcp import TOOLS, WRITING_TOOLS, create_server

URL = "/v1/ae/sessions/conv_a/judge"
YES = {"kind": "yes_no", "question": "Should I run the full test suite now?"}
USER_MESSAGE = "please check why the nightly refresh keeps failing for everyone"


class World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.messages = [USER_MESSAGE]
        self.lead_messages = ["please look into the quarterly report for the whole team"]
        self.child_labels: dict[str, str] = {}
        self.sub_labels: dict[str, dict[str, str]] = {}  # child id to extra labels on its row
        self.sub_kind: dict[str, str] = {}  # child id to its kind, default "default"
        self.lead_sent: list[dict[str, Any]] = []  # arguments of the lead's own tool calls
        self.lead_readable = True
        self.meta: set[str] = set()  # texts the bridge stamped is_meta
        self.children: dict[str, list[str]] = {}  # child id to its user messages
        self.child_orch: dict[str, str] = {}  # child id to its ae.orch label, default conv_a
        self.scan_too_long = False
        self.child_parent: dict[str, str] = {}  # sub-agent child id to its parent id
        self.role: str | None = "helper"
        self.calls: list[dict[str, Any]] = []
        self.status = 200
        self.answer: dict[str, Any] = {"choice": "yes", "probabilities": {"yes": 0.9, "no": 0.1}}
        self.log = tmp_path / "shadow.jsonl"
        deny = tmp_path / "deny.txt"
        deny.write_text("Zorbex\n", encoding="utf-8")
        for k, v in {
            "AE_DECISION_ENABLED": "1",
            "AE_DECISION_ENDPOINT": "https://decision.example.com",
            "AE_DECISION_API_KEY": "test-key-value",
            "AE_DECISION_USERS": "local",
            "AE_DECISION_DENYLIST_FILE": str(deny),
            "AE_DECISION_SHADOW_FILE": str(self.log),
            "AE_DECISION_POINTS_FILE": str(tmp_path / "points.json"),
        }.items():
            monkeypatch.setenv(k, v)
        monkeypatch.delenv("AE_DECISION_POINTS", raising=False)
        world = self

        class Store:
            def get_conversation(_self, sid: str) -> Any:
                return SimpleNamespace(id=sid, created_at=0)

            def list_items(_self, sid: str, **kw: Any) -> Any:
                if kw.get("type") == "function_call":
                    calls = world.lead_sent if sid == "conv_a" else []
                    data = [
                        SimpleNamespace(
                            data=SimpleNamespace(
                                name=a.pop("_name", "Agent") if "_name" in a else "Agent",
                                arguments=json.dumps(a),
                            )
                        )
                        for a in (dict(x) for x in calls)
                    ]
                    return SimpleNamespace(data=data, has_more=False, last_id=None)
                if sid == "conv_lead":
                    if not world.lead_readable:
                        raise RuntimeError("unreadable")
                    msgs = world.lead_messages
                elif sid in world.children:
                    msgs = world.children[sid]
                else:
                    msgs = world.messages
                data = [
                    SimpleNamespace(
                        data=SimpleNamespace(
                            role="user",
                            content=[{"type": "input_text", "text": t}],
                            is_meta=t in world.meta,
                            subagent_return_id=None,
                        )
                    )
                    for t in msgs
                ]
                return SimpleNamespace(data=data, has_more=False, last_id=None)

        async def _conversation(sid: str) -> Any:
            if sid == "conv_lead":
                return SimpleNamespace(id=sid, labels={}, parent_conversation_id=None)
            if sid != "conv_a":
                return None
            labels = {"ae.role": world.role} if world.role else {}
            labels.update(world.child_labels)
            return SimpleNamespace(id=sid, labels=labels, parent_conversation_id=None)

        async def _may_write(_user: str | None, _sid: str) -> bool:
            return True

        def handler(req: httpx.Request) -> httpx.Response:
            world.calls.append(json.loads(req.content))
            if world.status != 200:
                return httpx.Response(world.status, text="secret body")
            return httpx.Response(200, content=json.dumps({"answers": {"q": world.answer}}))

        monkeypatch.setattr(router_module, "_conversation", _conversation)
        monkeypatch.setattr(router_module, "_may_write_session", _may_write)
        monkeypatch.setattr(router_module, "get_conversation_store", lambda: Store())
        monkeypatch.setattr(router_module._owner_identity, "owner", lambda sid: "local")
        monkeypatch.setattr(router_module.pending_inputs, "snapshot_for", lambda _s: [])
        monkeypatch.setattr(da, "_shared_transport", lambda: httpx.MockTransport(handler))

        def _list_since(*_a: Any, **_k: Any) -> list[Any]:
            if self.scan_too_long:
                raise router_module.sessions.ScanTooLong
            return [
                SimpleNamespace(
                    id=cid,
                    labels={
                        "ae.orch": self.child_orch.get(cid, "conv_a"),
                        **self.sub_labels.get(cid, {}),
                    },
                    parent_conversation_id=self.child_parent.get(cid),
                    kind=self.sub_kind.get(cid, "default"),
                )
                for cid in self.children
            ]

        monkeypatch.setattr(router_module.sessions, "list_since", _list_since)
        agent_judge._calls.clear()
        agent_judge._burst.clear()
        agent_judge._questions.clear()
        ds.reset()

    def shadow(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines()]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    return World(monkeypatch, tmp_path)


async def test_a_helper_gets_an_answer(client: httpx.AsyncClient, world: World) -> None:
    response = await client.post(URL, json=YES)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["answer"] is True and body["probs"] == {"yes": 0.9, "no": 0.1}
    assert body["low_confidence"] is False
    assert world.calls[0]["questions"]["q"]["instructions"] == YES["question"]


@pytest.mark.parametrize("role", ["lead", "helper", "builder", "reviewer", "planner", None])
async def test_every_role_is_allowed(client: httpx.AsyncClient, world: World, role: Any) -> None:
    world.role = role
    assert (await client.post(URL, json=YES)).json()["answer"] is True
    assert world.shadow()[-1]["features"]["role"] == (role or "unknown")


async def test_choice_and_score(client: httpx.AsyncClient, world: World) -> None:
    world.answer = {"choice": "scoped", "probabilities": {"scoped": 0.8, "full": 0.1}}
    body = (
        await client.post(
            URL,
            json={
                "kind": "choice",
                "question": "Which tests should I run first?",
                "options": [
                    {"key": "scoped", "description": "Only the touched modules"},
                    {"key": "full", "description": "The whole suite"},
                ],
                "facts": {"files_changed": 3, "touches_schema": False, "size": "small"},
            },
        )
    ).json()
    assert body["answer"] == "scoped" and body["low_confidence"] is False
    assert world.calls[-1]["state"] == {
        "files_changed": 3,
        "touches_schema": False,
        "size": "small",
    }
    world.answer = {
        "legend": {"0": "low", "1": "high"},
        "probabilities": {"0": 0.2, "1": 0.8},
    }
    body = (
        await client.post(
            URL,
            json={
                "kind": "score",
                "question": "How risky is this change?",
                "levels": ["low", "high"],
            },
        )
    ).json()
    assert body["answer"] == "high"


async def test_low_confidence_flag(client: httpx.AsyncClient, world: World) -> None:
    world.answer = {"choice": "yes", "probabilities": {"yes": 0.55, "no": 0.45}}
    assert (await client.post(URL, json=YES)).json()["low_confidence"] is True


@pytest.mark.parametrize(
    ("question", "code"),
    [
        (
            "check why the nightly refresh keeps failing for everyone",
            "refused_passthrough",
        ),
        ("Should I tell Zorbex about the change?", "refused_denylist"),
        ("Should I open https://example.com/page now?", "refused_url"),
        ("Should I edit the file in src/app/handlers?", "refused_path"),
        ("Should I ask Marguerite about this change?", "refused_proper_noun"),
        ("Should I use the key in `config`?", "refused_backtick"),
    ],
)
async def test_guard_refusals_name_the_code_only(
    client: httpx.AsyncClient, world: World, question: str, code: str
) -> None:
    body = (await client.post(URL, json={"kind": "yes_no", "question": question})).json()
    assert body["error"] == code and body["low_confidence"] is True and body["answer"] is None
    assert "Zorbex" not in json.dumps(body) and world.calls == []


async def test_options_levels_and_facts_are_guarded_too(
    client: httpx.AsyncClient, world: World
) -> None:
    bad_option = {
        "kind": "choice",
        "question": "Which tests should I run first?",
        "options": [
            {"key": "a", "description": "Ask Zorbex first"},
            {"key": "b", "description": "Skip it"},
        ],
    }
    assert (await client.post(URL, json=bad_option)).json()["error"] == "refused_denylist"
    bad_key = {**bad_option, "options": [{"key": "A b", "description": "x"}] * 2}
    assert (await client.post(URL, json=bad_key)).json()["error"] == "refused_key"
    bad_level = {
        "kind": "score",
        "question": "How risky is this?",
        "levels": ["low", "see docs.example.com"],
    }
    assert (await client.post(URL, json=bad_level)).json()["error"] == "refused_url"
    bad_fact = {**YES, "facts": {"who": "Zorbex"}}
    assert (await client.post(URL, json=bad_fact)).json()["error"] == "refused_denylist"
    assert (await client.post(URL, json={**YES, "facts": {"Bad Key": 1}})).json()[
        "error"
    ] == "refused_key"
    assert (await client.post(URL, json={"kind": "choice", "question": "Which one fits?"})).json()[
        "error"
    ] == "bad_request"
    assert world.calls == []


async def test_bodies_that_break_the_schema_are_422(
    client: httpx.AsyncClient, world: World
) -> None:
    assert (await client.post(URL, json={**YES, "extra": 1})).status_code == 422
    many = {str(i): i for i in range(13)}
    assert (await client.post(URL, json={**YES, "facts": many})).status_code == 422
    assert (await client.post("/v1/ae/sessions/nope/judge", json=YES)).status_code == 404


async def test_session_caps_are_120_an_hour_and_10_a_minute(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_MINUTE_CAP", "1000")
    for _ in range(10):
        assert (await client.post(URL, json=YES)).json()["answer"] is True
    body = (await client.post(URL, json=YES)).json()
    assert body["error"] == "rate_limited" and 1 <= body["retry_after_s"] <= 60
    assert len(world.calls) == 10
    monkeypatch.setenv("AE_DECISION_SESSION_MINUTE", "1000")
    monkeypatch.setenv("AE_DECISION_SESSION_HOURLY", "12")
    for _ in range(2):
        assert (await client.post(URL, json=YES)).json()["answer"] is True
    body = (await client.post(URL, json=YES)).json()
    assert body["error"] == "rate_limited" and body["retry_after_s"] > 60
    assert len(world.calls) == 12


async def test_no_text_in_the_shadow_line(client: httpx.AsyncClient, world: World) -> None:
    body = {
        "kind": "choice",
        "question": "Which tests should I run first?",
        "options": [
            {"key": "scoped", "description": "Only the touched modules"},
            {"key": "full", "description": "The whole suite"},
        ],
        "facts": {"size": "small", "files_changed": 3},
    }
    world.answer = {"choice": "scoped", "probabilities": {"scoped": 0.9, "full": 0.1}}
    await client.post(URL, json=body)
    (row,) = world.shadow()
    assert row["point"] == "agent_ask" and row["mode"] == "live" and row["acted"] is True
    assert row["features"] == {
        "kind": "choice",
        "n_options": 2,
        "n_facts": 2,
        "has_context": False,
        "batch": 1,
        "low_confidence": False,
        "role": "helper",
    }
    text = json.dumps(row)
    for secret in ("tests should", "touched modules", "whole suite", "small", "files_changed"):
        assert secret not in text


async def test_decision_failure_is_an_error_not_a_raise(
    client: httpx.AsyncClient, world: World
) -> None:
    world.status = 503
    response = await client.post(URL, json=YES)
    assert response.status_code == 200
    body = response.json()
    assert body["error"] == "http_503" and body["low_confidence"] is True
    assert "secret" not in json.dumps(body)


async def test_off_when_the_owner_is_not_allowed_or_killed(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_USERS", "someone@example.com")
    assert (await client.post(URL, json=YES)).json()["error"] == "judge_off"
    monkeypatch.setenv("AE_DECISION_USERS", "local")
    monkeypatch.setenv("AE_DECISION_POINTS", "agent_ask:off")
    assert (await client.post(URL, json=YES)).json()["error"] == "judge_off"
    assert world.calls == []


async def test_shadow_mode_does_not_hand_back_an_answer(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_POINTS", "agent_ask:shadow")
    body = (await client.post(URL, json=YES)).json()
    assert body["error"] == "shadow_mode" and body["answer"] is None
    assert world.shadow()[-1]["acted"] is False


async def test_a_broken_promotions_file_does_not_switch_it_off(
    client: httpx.AsyncClient, world: World, tmp_path: Path
) -> None:
    (tmp_path / "points.json").write_text("{broken")
    assert (await client.post(URL, json=YES)).json()["answer"] is True


# -- the MCP tool


def test_the_tool_is_listed_and_counts_as_a_writer() -> None:
    tool = next(t for t in TOOLS if t.name == "ask_judge")
    assert "required" not in tool.inputSchema  # a single question or a questions batch
    assert tool.inputSchema["properties"]["questions"]["maxItems"] == 5
    assert tool.inputSchema["properties"]["questions"]["items"]["required"] == ["kind", "question"]
    assert "ask_judge" in WRITING_TOOLS
    for phrase in ("full suite", "yes/no", "probabilities", "decide yourself", "never paste"):
        assert phrase in (tool.description or "")


async def test_the_tool_posts_as_the_calling_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "conv_a")
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == URL
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"answer": True, "probs": {}, "low_confidence": False})

    async with create_connected_server_and_client_session(create_server(_stub(handler))) as s:
        answered = _payload(await s.call_tool("ask_judge", YES))
        monkeypatch.delenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID")
        monkeypatch.delenv("OMNIGENT_TERMINAL_LAUNCH_SESSION_ID", raising=False)
        missing = _payload(await s.call_tool("ask_judge", YES))
    assert answered["answer"] is True and seen == [YES]
    assert missing["error"] == "no_session_id" and missing["low_confidence"] is True


async def test_the_tool_turns_a_server_error_into_an_error_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "conv_a")
    async with create_connected_server_and_client_session(
        create_server(_stub(lambda r: httpx.Response(500, json={})))
    ) as s:
        out = _payload(await s.call_tool("ask_judge", YES))
    assert out["error"] == "error" and out["low_confidence"] is True


# -- review round 2 (2026-11-06)

LEAD_QUESTION = "check the quarterly report for the whole team"


@pytest.mark.parametrize(
    "body",
    [
        {
            "kind": "choice",
            "question": "Which one fits?",
            "options": [
                {"key": "rebill_contoso", "description": "Rebill it"},
                {"key": "other", "description": "Skip it"},
            ],
        },
        {"kind": "yes_no", "question": "Should I go on?", "facts": {"who": "contoso_ltd"}},
        {
            "kind": "choice",
            "question": "Which one fits?",
            "options": [
                {"key": "a", "description": "rebill the contoso_inc account"},
                {"key": "b", "description": "Skip it"},
            ],
        },
        {"kind": "yes_no", "question": "Should I go on?", "facts": {"contoso_q4": 1}},
    ],
)
async def test_denylist_sees_through_underscores_and_hyphens(
    client: httpx.AsyncClient, world: World, tmp_path: Path, body: dict[str, Any]
) -> None:
    (tmp_path / "deny.txt").write_text("contoso\n", encoding="utf-8")
    out = (await client.post(URL, json=body)).json()
    assert out["error"] in {"refused_denylist", "refused_key"} and world.calls == []


async def test_option_keys_and_fact_keys_get_the_content_guard(
    client: httpx.AsyncClient, world: World
) -> None:
    world.messages = ["why does the nightly refresh keeps failing badly today"]
    opts = [{"key": "k1", "description": "x"}, {"key": "k2", "description": "y"}]
    cases = [
        {
            "kind": "choice",
            "question": "Which one fits?",
            "options": [
                {"key": "nightly_refresh_keeps_failing", "description": "x"},
                opts[1],
            ],
        },
        {"kind": "yes_no", "question": "Should I go on?", "facts": {"zorbex_flag": True}},
        {"kind": "yes_no", "question": "Should I go on?", "facts": {"id_12345": 1}},
    ]
    for body in cases:
        assert (await client.post(URL, json=body)).json()["error"] == "refused_key"
    # a one-word key that is also in the user text is a label, not a copy
    world.messages = ["please check the scoped tests"]
    ok = {
        "kind": "choice",
        "question": "Which one fits?",
        "options": [
            {"key": "scoped", "description": "Only these"},
            {"key": "full", "description": "All"},
        ],
    }
    world.answer = {"choice": "scoped", "probabilities": {"scoped": 0.9, "full": 0.1}}
    assert (await client.post(URL, json=ok)).json()["answer"] == "scoped"


@pytest.mark.parametrize(
    ("message", "question"),
    [
        ("nightly refresh keeps failing", "Should nightly refresh keeps failing?"),  # (a)
        (
            "run the whole suite and then report back to me about every failure",
            "run the whole suite",
        ),  # (b)
        (
            "please check the nightly refresh job in the new pipeline setup",
            "Is the nightly refresh job in the new pipeline setup ok?",
        ),  # (c)
        ("fix the bug now", "fix the bug now"),  # short copy, no 11 character skip
    ],
)
async def test_passthrough_is_not_bypassable(
    client: httpx.AsyncClient, world: World, message: str, question: str
) -> None:
    world.messages = [message]
    out = (await client.post(URL, json={"kind": "yes_no", "question": question})).json()
    assert out["error"] == "refused_passthrough" and world.calls == []


async def test_a_neutral_question_sharing_common_words_is_allowed(
    client: httpx.AsyncClient, world: World
) -> None:
    world.messages = ["please check why the nightly refresh keeps failing for everyone"]
    out = await client.post(
        URL, json={"kind": "yes_no", "question": "Should I run the full test suite now?"}
    )
    assert out.json()["answer"] is True


def test_check_summary_uses_the_same_passthrough_rule() -> None:
    allow = da.DEFAULT_TERM_ALLOW
    msgs = ["nightly refresh keeps failing"]
    assert (
        da.check_summary("Fix: nightly refresh keeps failing today", ["x"], allow, msgs)
        == "passthrough"
    )
    assert da.check_summary("Fix a failing test in the builder", ["x"], allow, msgs) is None
    assert da.check_summary("Fix the rebill_contoso job", ["contoso"], allow, []) == "denylist"
    assert da.check_summary("Fix the contoso-q4 job", ["contoso"], allow, []) == "denylist"


async def test_shadow_line_never_holds_option_keys_or_level_names(
    client: httpx.AsyncClient, world: World
) -> None:
    world.answer = {"choice": "zeta_key", "probabilities": {"zeta_key": 0.8, "omega_key": 0.2}}
    body = {
        "kind": "choice",
        "question": "Which one fits?",
        "options": [
            {"key": "omega_key", "description": "the first"},
            {"key": "zeta_key", "description": "the second"},
        ],
    }
    await client.post(URL, json=body)
    world.answer = {"legend": {"0": "tiny", "1": "huge"}, "probabilities": {"0": 0.1, "1": 0.9}}
    await client.post(
        URL, json={"kind": "score", "question": "How big is it?", "levels": ["tiny", "huge"]}
    )
    first, second = world.shadow()
    assert first["answer"] == 1 and first["probs"] == [0.2, 0.8]
    assert second["answer"] == 1 and second["probs"] == [0.1, 0.9]
    text = json.dumps(world.shadow())
    for name in ("omega_key", "zeta_key", "tiny", "huge"):
        assert name not in text


async def test_a_helper_also_checks_its_leads_transcript(
    client: httpx.AsyncClient, world: World
) -> None:
    world.child_labels = {"ae.orch": "conv_lead"}
    body = {"kind": "yes_no", "question": LEAD_QUESTION}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_passthrough"
    world.lead_readable = False
    assert (await client.post(URL, json=YES)).json()["error"] == "no_transcript"
    assert world.calls == []
    world.lead_readable = True
    assert (await client.post(URL, json=YES)).json()["answer"] is True


async def test_levels_and_string_facts_have_a_length_cap(
    client: httpx.AsyncClient, world: World
) -> None:
    long = "a" * 41
    assert (
        await client.post(
            URL, json={"kind": "score", "question": "How big is it?", "levels": ["lo", long]}
        )
    ).status_code == 422
    assert (await client.post(URL, json={**YES, "facts": {"k": long}})).status_code == 422
    assert (await client.post(URL, json={**YES, "question": "q" * 161})).status_code == 422


def test_rate_limit_map_prunes_only_expired_and_refuses_new_sessions() -> None:
    agent_judge._calls.clear()
    for n in range(agent_judge.MAX_SESSIONS):
        assert agent_judge.allow_call(f"s{n}", now=100.0)
    assert agent_judge.allow_call("s0", now=101.0)  # an existing session still counts
    assert agent_judge.allow_call("new", now=200.0) is False  # full of live counters: refuse
    assert agent_judge._calls["s1"] == (1, 100.0)  # nobody was evicted
    assert agent_judge.allow_call(
        "new", now=100.0 + agent_judge.RATE_WINDOW_S + 1
    )  # expired pruned
    assert agent_judge.allow_call("s0", now=100.0 + agent_judge.RATE_WINDOW_S + 1)


async def test_agent_ask_named_live_stays_live(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_POINTS", "agent_ask:live")
    assert (await client.post(URL, json=YES)).json()["answer"] is True
    assert world.shadow()[-1]["mode"] == "live"


async def test_bad_probabilities_are_an_error_not_an_answer(
    client: httpx.AsyncClient, world: World
) -> None:
    for bad in ({"yes": float("nan"), "no": 0.1}, {"yes": 1.5, "no": 0.1}, {}):
        world.answer = {"choice": "yes", "probabilities": bad}
        out = (await client.post(URL, json=YES)).json()
        assert out["error"] == "bad_shape" and out["answer"] is None
    assert "NaN" not in world.log.read_text()


HAND_BACK = "Another Claude session sent a message: 12 files changed and the build has tests"


async def test_a_meta_hand_back_does_not_block_a_fact_key(
    client: httpx.AsyncClient, world: World
) -> None:
    world.messages = [USER_MESSAGE, HAND_BACK]
    world.meta = {HAND_BACK}
    body = {**YES, "facts": {"files_changed_and_the": 12}}
    assert (await client.post(URL, json=body)).json()["answer"] is True


async def test_a_forged_prefix_without_the_stamp_is_still_counted(
    client: httpx.AsyncClient, world: World
) -> None:
    world.messages = [USER_MESSAGE, HAND_BACK]
    body = {**YES, "facts": {"files_changed_and_the": 12}}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_key"


async def test_owner_text_pasted_after_a_watcher_note_is_caught(
    client: httpx.AsyncClient, world: World
) -> None:
    world.messages = ["Merge watcher: PR green\n\nnightly refresh keeps failing"]
    body = {"kind": "yes_no", "question": "nightly refresh keeps failing"}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_passthrough"


async def test_a_session_of_only_meta_messages_fails_closed(
    client: httpx.AsyncClient, world: World
) -> None:
    world.messages = [HAND_BACK, "<task-notification>done</task-notification>"]
    world.meta = set(world.messages)
    out = (await client.post(URL, json=YES)).json()
    assert out["error"] == "no_transcript" and world.calls == []


def test_meta_and_return_id_items_are_skipped_and_pending_is_always_counted(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.messages = ["owner words", "stamped words"]
    world.meta = {"stamped words"}
    pending = [{"content": [{"text": "Merge watcher: queued"}, {"text": "queued owner"}]}]
    monkeypatch.setattr(router_module.pending_inputs, "snapshot_for", lambda _s: pending)
    assert router_module._user_texts("conv_a") == [
        "owner words",
        "Merge watcher: queued",
        "queued owner",
    ]
    item = SimpleNamespace(is_meta=False, subagent_return_id="task_1")
    assert router_module._is_injected(item)


async def test_a_childs_owner_message_is_caught_from_the_lead(
    client: httpx.AsyncClient, world: World
) -> None:
    world.role = "lead"
    world.children = {"conv_kid": ["the quarterly numbers look wrong in region nine"]}
    body = {"kind": "yes_no", "question": "Are the quarterly numbers look wrong in region nine?"}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_passthrough"
    assert (await client.post(URL, json=YES)).json()["answer"] is True


async def test_more_than_three_hundred_children_fails_closed(
    client: httpx.AsyncClient, world: World
) -> None:
    world.children = {f"conv_k{i}": ["hello there"] for i in range(301)}
    assert (await client.post(URL, json=YES)).json()["error"] == "no_transcript"
    world.children = {f"conv_k{i}": ["hello there"] for i in range(300)}
    assert (await client.post(URL, json=YES)).json()["answer"] is True


async def test_a_child_labelled_without_the_conv_prefix_is_still_read(
    client: httpx.AsyncClient, world: World
) -> None:
    world.children = {"conv_kid": ["the quarterly numbers look wrong in region nine"]}
    world.child_orch = {"conv_kid": "a"}
    body = {"kind": "yes_no", "question": "Are the quarterly numbers look wrong in region nine?"}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_passthrough"


async def test_a_child_scan_past_its_cap_fails_closed(
    client: httpx.AsyncClient, world: World
) -> None:
    world.scan_too_long = True
    assert (await client.post(URL, json=YES)).json()["error"] == "no_transcript"


async def test_no_children_still_answers(client: httpx.AsyncClient, world: World) -> None:
    world.children = {}
    assert (await client.post(URL, json=YES)).json()["answer"] is True


def test_list_since_keeps_archived_rows_and_stops_at_the_cutoff() -> None:
    seen: dict[str, Any] = {}

    class Store:
        def list_conversations(self, **kw: Any) -> Any:
            seen.update(kw)
            rows = [SimpleNamespace(id=f"c{t}", created_at=t) for t in (30, 20, 10, 5)]
            return SimpleNamespace(data=rows, has_more=True, last_id="c5")

    rows = router_module.sessions.list_since(10, Store())
    assert [r.id for r in rows] == ["c30", "c20", "c10"]
    assert seen["include_archived"] is True and seen["accessible_by"] is None
    assert seen["kind"] is None
    with pytest.raises(router_module.sessions.ScanTooLong):
        router_module.sessions.list_since(0, Store(), cap=2)


async def test_a_sub_agent_child_is_read(client: httpx.AsyncClient, world: World) -> None:
    world.children = {"conv_side": ["the quarterly numbers look wrong in region nine"]}
    world.child_orch = {"conv_side": ""}
    world.child_parent = {"conv_side": "conv_a"}
    body = {"kind": "yes_no", "question": "Are the quarterly numbers look wrong in region nine?"}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_passthrough"


PROMPT = "Role: Builder. Report the files changed and whether it has tests, then push."
MIRROR = {"omnigent.wrapper": "claude-code-native-ui-subagent"}


def _mirror(world: World, cid: str, texts: list[str]) -> None:
    world.children[cid] = texts
    world.child_orch[cid] = ""
    world.child_parent[cid] = "conv_a"
    world.sub_labels[cid] = MIRROR
    world.sub_kind[cid] = "sub_agent"


async def test_a_sub_agent_prompt_the_lead_sent_does_not_count(
    client: httpx.AsyncClient, world: World
) -> None:
    _mirror(world, "conv_sub", [PROMPT])
    world.lead_sent = [{"description": "Builder", "prompt": PROMPT}]
    body = {
        "kind": "yes_no",
        "question": "Ready?",
        "facts": {"files_changed": 3, "has_tests": True},
    }
    assert (await client.post(URL, json=body)).json()["answer"] is True


async def test_owner_text_posted_into_a_mirror_still_counts(
    client: httpx.AsyncClient, world: World
) -> None:
    _mirror(world, "conv_sub", [PROMPT, "the quarterly numbers look wrong in region nine"])
    world.lead_sent = [{"prompt": PROMPT}]
    body = {"kind": "yes_no", "question": "Are the quarterly numbers look wrong in region nine?"}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_passthrough"


async def test_a_forged_mirror_label_reads_the_child_anyway(
    client: httpx.AsyncClient, world: World
) -> None:
    world.children = {"conv_kid": ["the quarterly numbers look wrong in region nine"]}
    world.sub_labels = {"conv_kid": MIRROR}
    body = {"kind": "yes_no", "question": "Are the quarterly numbers look wrong in region nine?"}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_passthrough"


async def test_a_sub_agent_prompt_the_lead_did_not_send_counts(
    client: httpx.AsyncClient, world: World
) -> None:
    _mirror(world, "conv_sub", [PROMPT])
    body = {"kind": "yes_no", "question": "Ready?", "facts": {"report_the_files_changed": 3}}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_key"


async def test_mirrors_have_their_own_cap(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "MAX_JUDGE_MIRRORS", 400)
    for i in range(150):
        _mirror(world, f"conv_m{i}", [PROMPT])
    world.lead_sent = [{"prompt": PROMPT}]
    assert (await client.post(URL, json=YES)).json()["answer"] is True
    for i in range(150, 401):
        _mirror(world, f"conv_m{i}", [PROMPT])
    assert (await client.post(URL, json=YES)).json()["error"] == "no_transcript"


async def test_a_codex_sub_agent_still_counts(client: httpx.AsyncClient, world: World) -> None:
    world.children = {"conv_side": ["the quarterly numbers look wrong in region nine"]}
    world.child_orch = {"conv_side": ""}
    world.child_parent = {"conv_side": "conv_a"}
    world.sub_labels = {"conv_side": {"omnigent.wrapper": "codex-native-ui-subagent"}}
    world.sub_kind = {"conv_side": "sub_agent"}
    body = {"kind": "yes_no", "question": "Are the quarterly numbers look wrong in region nine?"}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_passthrough"


OWNER = "the quarterly numbers look wrong in region nine and we need it fixed"


async def test_an_owner_child_message_the_lead_echoes_still_counts(
    client: httpx.AsyncClient, world: World
) -> None:
    world.children = {"conv_kid": [OWNER]}
    world.lead_sent = [{"_name": "Bash", "command": OWNER}]  # not a delivery
    body = {"kind": "yes_no", "question": "Are the quarterly numbers look wrong in region nine?"}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_passthrough"


async def test_a_delivered_echo_cancels_only_its_own_copy(
    client: httpx.AsyncClient, world: World
) -> None:
    world.children = {"conv_kid": [OWNER, OWNER]}  # the owner's copy and the lead's
    world.lead_sent = [{"_name": "mcp__ae__post_message", "text": OWNER}]
    body = {"kind": "yes_no", "question": "Are the quarterly numbers look wrong in region nine?"}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_passthrough"


def test_delivery_tool_names_match_any_prefix() -> None:
    for name in ("Agent", "mcp__ae__create_session", "ae.post_message", "SendMessage"):
        assert router_module._delivers(name)
    for name in ("Bash", "mcp__ae__ask_judge", None, "agent"):
        assert not router_module._delivers(name)


async def test_whitespace_differences_still_match_a_delivery(
    client: httpx.AsyncClient, world: World
) -> None:
    _mirror(world, "conv_sub", ["  " + PROMPT.replace(" ", "\n", 2) + "  "])
    world.lead_sent = [{"prompt": PROMPT}]
    body = {"kind": "yes_no", "question": "Ready?", "facts": {"files_changed": 3}}
    assert (await client.post(URL, json=body)).json()["answer"] is True


# -- micro-decisions (2026-11-13): context, batches, 429


async def test_context_is_guarded_sent_as_situation_and_never_logged(
    client: httpx.AsyncClient, world: World
) -> None:
    context = "third check run failed on the same assertion after two fixes"
    body = (await client.post(URL, json={**YES, "context": context})).json()
    assert body["answer"] is True
    assert world.calls[-1]["state"] == {"situation": context}
    row = world.shadow()[-1]
    assert row["features"]["has_context"] is True
    assert context not in json.dumps(row)


@pytest.mark.parametrize(
    ("context", "code"),
    [
        ("tell Zorbex about the change", "refused_denylist"),
        ("see https://example.com/page for it", "refused_url"),
        ("the nightly refresh keeps failing for everyone", "refused_passthrough"),
        ("edit the file in src/app/handlers", "refused_path"),
    ],
)
async def test_context_gets_the_question_guard_and_copy_check(
    client: httpx.AsyncClient, world: World, context: str, code: str
) -> None:
    body = (await client.post(URL, json={**YES, "context": context})).json()
    assert body["error"] == code and body["answer"] is None
    assert world.calls == []


async def test_context_length_is_bounded(client: httpx.AsyncClient, world: World) -> None:
    assert (await client.post(URL, json={**YES, "context": "short"})).status_code == 422
    assert (await client.post(URL, json={**YES, "context": "a" * 161})).status_code == 422


async def test_a_batch_answers_in_order_and_counts_one_call(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_SESSION_MINUTE", "1")
    batch = {"questions": [YES, {"kind": "yes_no", "question": "Should I ask for review now?"}]}
    answers = (await client.post(URL, json=batch)).json()["answers"]
    assert [a.get("error") for a in answers] == [None, None]
    assert answers[0]["answer"] is True
    assert len(world.calls) == 2
    assert (await client.post(URL, json=YES)).json()["error"] == "rate_limited"  # one call used
    rows = world.shadow()
    assert [r["features"]["batch"] for r in rows if "batch" in r["features"]] == [2, 2]
    # the rate-limited call wrote one code-only line (with its session id)
    assert [r["error_class"] for r in rows if "batch" not in r["features"]] == ["rate_limited"]


async def test_a_bad_question_is_refused_alone_and_the_others_are_answered(
    client: httpx.AsyncClient, world: World
) -> None:
    """Q1 and Q2 are fine, Q3 names a denylisted client: only Q3 is refused, with its index."""
    batch = {
        "questions": [
            YES,
            {"kind": "yes_no", "question": "Should I ask for review now?"},
            {"kind": "yes_no", "question": "Should I tell Zorbex about it?"},
        ]
    }
    answers = (await client.post(URL, json=batch)).json()["answers"]
    assert len(world.calls) == 2  # the two clean questions were sent
    assert answers[0]["answer"] is True and answers[1]["answer"] is True
    assert answers[2]["error"] == "refused_denylist" and answers[2]["index"] == 2
    assert answers[2]["answer"] is None and answers[2]["low_confidence"] is True
    assert "Zorbex" not in answers[2]["hint"] and answers[2]["hint"]
    # only the refusal wrote a code-only line, with the session id
    refused = [r for r in world.shadow() if r.get("error_class")]
    assert [r["error_class"] for r in refused] == ["refused_denylist"]
    assert refused[0]["session"] == "conv_a"


async def test_a_bad_fact_or_context_in_a_later_question_is_refused_alone(
    client: httpx.AsyncClient, world: World
) -> None:
    later = {
        "kind": "yes_no",
        "question": "Should I ask for review now?",
        "facts": {"note": "ask Zorbex today"},
    }
    answers = (await client.post(URL, json={"questions": [YES, later]})).json()["answers"]
    assert len(world.calls) == 1 and answers[1]["error"].startswith("refused_")
    assert answers[0]["answer"] is True and "error" not in answers[0]


async def test_a_batch_counts_as_one_failure_toward_the_breaker(
    client: httpx.AsyncClient, world: World
) -> None:
    world.status = 503
    batch = {"questions": [YES] * 5}
    answers = (await client.post(URL, json=batch)).json()["answers"]
    assert len(world.calls) == 5 and all(a["error"] for a in answers)
    assert ds._limits.errors == 1 and ds._limits.open_until == 0.0


def test_the_concurrency_lets_a_full_batch_run_at_once() -> None:
    assert ds.CONCURRENCY >= agent_judge.MAX_BATCH


async def test_a_session_may_not_send_more_questions_than_its_minute_cap(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_SESSION_QUESTIONS_MINUTE", "6")
    assert len((await client.post(URL, json={"questions": [YES] * 5})).json()["answers"]) == 5
    body = (await client.post(URL, json={"questions": [YES] * 2})).json()
    assert all(a["error"] == "rate_limited" and a["retry_after_s"] >= 1 for a in body["answers"])
    assert len(world.calls) == 5
    assert (await client.post(URL, json=YES)).json()["answer"] is True  # the sixth still fits
    # the refusal wrote one line per question in the refused batch
    refused = [r for r in world.shadow() if r.get("error_class") == "rate_limited"]
    assert len(refused) == 2 and all(r["session"] == "conv_a" for r in refused)


def test_the_question_cap_counts_questions_not_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_judge._calls.clear()
    agent_judge._burst.clear()
    agent_judge._questions.clear()
    env = {"AE_DECISION_SESSION_QUESTIONS_MINUTE": "20"}
    for i in range(4):
        assert agent_judge.check_call("s", now=100.0 + i, environ=env, questions=5) is None
    wait = agent_judge.check_call("s", now=105.0, environ=env, questions=1)
    assert wait is not None and 50 <= wait <= 60
    assert agent_judge.check_call("s", now=161.0, environ=env, questions=5) is None


async def test_the_shadow_line_logs_the_final_low_confidence(
    client: httpx.AsyncClient, world: World
) -> None:
    await client.post(URL, json=YES)
    world.answer = {"choice": "yes", "probabilities": {"yes": 0.55, "no": 0.45}}
    body = (await client.post(URL, json=YES)).json()
    rows = world.shadow()
    assert [r["features"]["low_confidence"] for r in rows] == [False, body["low_confidence"]]
    assert body["low_confidence"] is True


async def test_a_batch_is_one_to_five_questions(client: httpx.AsyncClient, world: World) -> None:
    assert (await client.post(URL, json={"questions": []})).status_code == 422
    assert (await client.post(URL, json={"questions": [YES] * 6})).status_code == 422
    assert (await client.post(URL, json={"questions": [YES] * 5})).status_code == 200
    assert (await client.post(URL, json={"questions": [YES], **YES})).status_code == 422


async def test_every_question_in_a_batch_is_copy_checked(
    client: httpx.AsyncClient, world: World
) -> None:
    batch = {
        "questions": [
            YES,
            {
                "kind": "yes_no",
                "question": "check why the nightly refresh keeps failing for everyone",
            },
        ]
    }
    answers = (await client.post(URL, json=batch)).json()["answers"]
    assert answers[0]["answer"] is True and answers[1]["error"] == "refused_passthrough"
    assert len(world.calls) == 1


async def test_a_429_answers_rate_limited_with_retry_after_and_spares_the_breaker(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(da, "_throttle_until", 0.0)

    def limited(req: httpx.Request) -> httpx.Response:
        world.calls.append(json.loads(req.content))
        return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")

    monkeypatch.setattr(da, "_shared_transport", lambda: httpx.MockTransport(limited))
    body = (await client.post(URL, json=YES)).json()
    assert body["error"] == "rate_limited" and 1 <= body["retry_after_s"] <= 7
    assert len(world.calls) == 1
    # the wait holds the next calls back without a request, and never opens the breaker
    for _ in range(6):
        again = (await client.post(URL, json=YES)).json()
        assert again["error"] == "rate_limited"
    assert len(world.calls) == 1
    assert ds._limits.errors == 0 and ds._limits.open_until == 0.0
    monkeypatch.setattr(da, "_throttle_until", 0.0)


def test_a_missing_or_bad_retry_after_uses_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(da, "_throttle_until", 0.0)
    da._note_rate_limit("soon")
    assert 1 <= da.retry_after_s() <= da.DEFAULT_RETRY_S
    monkeypatch.setattr(da, "_throttle_until", 0.0)
    da._note_rate_limit("9999")
    assert da.retry_after_s() <= da.MAX_RETRY_S
    monkeypatch.setattr(da, "_throttle_until", 0.0)


async def test_a_refusal_writes_one_line_with_the_code_and_role_only(
    client: httpx.AsyncClient, world: World
) -> None:
    await client.post(
        URL, json={"kind": "yes_no", "question": "Should I use the key in `config`?"}
    )
    (row,) = world.shadow()
    assert row["point"] == "agent_ask" and row["error_class"] == "refused_backtick"
    assert set(row) == {"at", "point", "session", "features", "error_class"}
    assert row["session"] == "conv_a"
    assert set(row["features"]) == {"role"}


# --- 2026-11-18: neutral questions are answered; risky ones stay refused ---------------------


@pytest.mark.parametrize(
    "question",
    [
        "Retry the flaky test once? It failed on a timeout.",
        "Pick the wall widget layout: grid or list?",
        "Where should the fix be built?",
        "Use the API or SSH to check the device?",
        "Should I skip it? It's a small change and we don't need a review.",
    ],
)
async def test_neutral_questions_are_answered(
    client: httpx.AsyncClient, world: World, question: str
) -> None:
    body = {"kind": "yes_no", "question": question}
    assert (await client.post(URL, json=body)).json()["answer"] is True


@pytest.mark.parametrize(
    "question",
    [
        "Deploy now. Northwind. Fabrikam. Tailspin?",
        "Ship it now? e.g. Fabrikam is waiting.",
        "Is it ready... Fabrikam next?",
        "Should I ask Dom before merge?",
        "Should I ask O'Brien about it?",
        "Should I read pass/fail from the log?",
        "Should I open package.json now?",
    ],
)
async def test_risky_questions_stay_refused(
    client: httpx.AsyncClient, world: World, question: str
) -> None:
    body = {"kind": "yes_no", "question": question}
    out = (await client.post(URL, json=body)).json()
    assert out["error"].startswith("refused_") and out["hint"]
    assert world.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "yes_no", "question": "Rerun the slow suite now? it stalled on a network wait."},
        {"kind": "yes_no", "question": "Bump the pin first, or rebase?"},
        {
            "kind": "yes_no",
            "question": "Should I go on?",
            "context": "Revert the last change before a retry",
        },
    ],
)
async def test_the_first_word_of_a_question_or_context_is_free(
    client: httpx.AsyncClient, world: World, body: dict[str, Any]
) -> None:
    assert (await client.post(URL, json=body)).json()["answer"] is True


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (
            {"kind": "yes_no", "question": "Rerun now? Fabrikam asked for it."},
            "refused_proper_noun",
        ),
        ({"kind": "yes_no", "question": "Ship it? Northwind."}, "refused_proper_noun"),
        ({"kind": "yes_no", "question": "Is Northwind ready?"}, "refused_proper_noun"),
        ({"kind": "yes_no", "question": "Zorbex needs a retry?"}, "refused_denylist"),
        (
            {"kind": "yes_no", "question": "Should I go on?", "context": "Rerun now? Fabrikam."},
            "refused_proper_noun",
        ),
        (
            {
                "kind": "choice",
                "question": "Which one fits?",
                "options": [
                    {"key": "a", "description": "Fabrikam"},
                    {"key": "b", "description": "Skip it"},
                ],
            },
            "refused_proper_noun",
        ),
        (
            {"kind": "yes_no", "question": "Should I go on?", "facts": {"vendor": "Fabrikam"}},
            "refused_proper_noun",
        ),
    ],
)
async def test_only_the_opening_word_is_free(
    client: httpx.AsyncClient, world: World, body: dict[str, Any], error: str
) -> None:
    assert (await client.post(URL, json=body)).json()["error"] == error
    assert world.calls == []


async def test_a_level_of_a_name_stays_refused(client: httpx.AsyncClient, world: World) -> None:
    body = {"kind": "score", "question": "How big is the risk?", "levels": ["low", "Fabrikam"]}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_proper_noun"


async def test_a_contraction_passes_but_a_denylisted_possessive_does_not(
    client: httpx.AsyncClient, world: World
) -> None:
    ok = {"kind": "yes_no", "question": "Is it safe? It doesn't need the full suite."}
    assert (await client.post(URL, json=ok)).json()["answer"] is True
    bad = {"kind": "yes_no", "question": "Should I rebill zorbex's account?"}
    assert (await client.post(URL, json=bad)).json()["error"] == "refused_denylist"


async def test_a_key_inside_an_owner_message_passes_but_a_long_copy_does_not(
    client: httpx.AsyncClient, world: World
) -> None:
    world.messages = ["use the best model (opus, high) for the builder and keep going now"]
    options = [
        {"key": "opus_high", "description": "The strongest model"},
        {"key": "sonnet_medium", "description": "The cheaper model"},
    ]
    body = {
        "kind": "choice",
        "question": "Which model should the builder use?",
        "options": options,
    }
    assert not (await client.post(URL, json=body)).json().get("error", "").startswith("refused_")
    copied = [
        {"key": "use_the_best_model", "description": "Strong"},
        {"key": "other", "description": "Cheaper"},
    ]
    body = {**body, "options": copied}
    assert (await client.post(URL, json=body)).json()["error"] == "refused_key"


async def test_a_long_transcript_is_read_and_answered(
    client: httpx.AsyncClient, world: World
) -> None:
    world.messages = [f"owner message number {i}" for i in range(1500)]
    assert (await client.post(URL, json=YES)).json()["answer"] is True


async def test_a_read_past_the_budget_answers_no_transcript_and_logs_it(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "JUDGE_READ_BUDGET_S", -1.0)
    out = (await client.post(URL, json=YES)).json()
    assert out["error"] == "no_transcript" and world.calls == []
    assert [r["error_class"] for r in world.shadow()] == ["refused_no_transcript"]


async def test_a_rate_limited_call_never_reads_the_transcript(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_SESSION_MINUTE", "1")
    assert (await client.post(URL, json=YES)).json()["answer"] is True
    reads: list[str] = []

    def spy(*_a: Any, **_k: Any) -> list[str]:
        reads.append("read")
        return ["x"]

    monkeypatch.setattr(router_module, "_user_texts", spy)
    assert (await client.post(URL, json=YES)).json()["error"] == "rate_limited"
    assert reads == []


async def test_a_judge_off_call_never_reads_the_transcript_or_logs(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_USERS", "someone-else")
    reads: list[str] = []
    monkeypatch.setattr(router_module, "_user_texts", lambda *_a, **_k: reads.append("r") or [])
    assert (await client.post(URL, json=YES)).json()["error"] == "judge_off"
    assert reads == [] and world.shadow() == []


def test_the_owner_index_matches_the_old_rule_and_the_key_minimum() -> None:
    index = da.OwnerIndex(["use the best model (opus, high) for it"])
    assert da.passthrough("opus high", index)  # old rule (b)
    assert not da.passthrough("opus high", index, inside_min=4)
    assert da.passthrough("use the best model", index, inside_min=4)


# --- review round 1 (2026-11-18) ---------------------------------------------------------


class _Clock:
    """A stand-in for ``router.time`` whose monotonic clock only moves when a test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        import time as real

        return getattr(real, name)


async def test_a_failed_read_in_a_batch_answers_in_the_batch_shape(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "JUDGE_READ_BUDGET_S", -1.0)
    out = (await client.post(URL, json={"questions": [YES, YES]})).json()
    assert [a["error"] for a in out["answers"]] == ["no_transcript", "no_transcript"]
    assert world.calls == []


async def test_the_clock_passing_the_deadline_during_the_last_page_read_fails_closed(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(router_module, "time", clock)
    real = router_module.get_conversation_store()

    class Slow:
        def __getattr__(self, name: str) -> Any:
            return getattr(real, name)

        def list_items(self, sid: str, **kw: Any) -> Any:
            page = real.list_items(sid, **kw)
            clock.now += 100.0  # the read itself ran past the 8 second budget
            return page

    monkeypatch.setattr(router_module, "get_conversation_store", lambda: Slow())
    out = (await client.post(URL, json=YES)).json()
    assert out["error"] == "no_transcript" and world.calls == []


async def test_the_clock_passing_the_deadline_during_an_ancestor_lookup_fails_closed(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    monkeypatch.setattr(router_module, "time", clock)
    world.child_labels = {"ae.orch": "conv_lead"}
    inner = router_module._conversation

    async def slow(sid: str) -> Any:
        conv = await inner(sid)
        if sid == "conv_lead":
            clock.now += 100.0
        return conv

    monkeypatch.setattr(router_module, "_conversation", slow)
    out = (await client.post(URL, json=YES)).json()
    assert out["error"] == "no_transcript" and world.calls == []


async def test_repeated_failed_reads_never_become_rate_limited(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "JUDGE_READ_BUDGET_S", -1.0)
    monkeypatch.setenv("AE_DECISION_SESSION_MINUTE", "2")
    monkeypatch.setenv("AE_DECISION_SESSION_QUESTIONS_MINUTE", "2")
    for _ in range(6):
        out = (await client.post(URL, json={"questions": [YES, YES]})).json()
        assert out["answers"][0]["error"] == "no_transcript"
    monkeypatch.setattr(router_module, "JUDGE_READ_BUDGET_S", 8.0)
    assert (await client.post(URL, json=YES)).json()["answer"] is True


def test_refund_call_undoes_one_check_call() -> None:
    agent_judge._calls.clear()
    agent_judge._burst.clear()
    agent_judge._questions.clear()
    env = {"AE_DECISION_SESSION_MINUTE": "1"}
    assert agent_judge.check_call("s", now=1.0, environ=env, questions=3) is None
    assert agent_judge.check_call("s", now=2.0, environ=env) is not None
    agent_judge.refund_call("s", questions=3)
    assert agent_judge.check_call("s", now=3.0, environ=env, questions=3) is None
    agent_judge.refund_call("nobody")  # an unknown session is a no-op


async def test_the_read_lock_table_is_bounded_and_busy_entries_stay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router_module, "MAX_JUDGE_READ_LOCKS", 2)
    router_module._JUDGE_READ_LOCKS.clear()
    async with router_module._judge_read_slot("a"):
        async with router_module._judge_read_slot("b"):
            assert set(router_module._JUDGE_READ_LOCKS) == {"a", "b"}
            async with router_module._judge_read_slot("c"):  # full of busy entries: no lock
                assert set(router_module._JUDGE_READ_LOCKS) == {"a", "b"}
    assert {k: v[1] for k, v in router_module._JUDGE_READ_LOCKS.items()} == {"a": 0, "b": 0}
    async with router_module._judge_read_slot("c"):  # idle entries are evicted to make room
        assert "c" in router_module._JUDGE_READ_LOCKS and len(router_module._JUDGE_READ_LOCKS) <= 2
    router_module._JUDGE_READ_LOCKS.clear()


def test_guard_summary_is_safe_from_many_threads() -> None:
    import concurrent.futures

    allow = da.DEFAULT_TERM_ALLOW

    def work(i: int) -> bool:
        return (
            da.guard_summary(f"fix the {'x' * (i % 7)} thing", ["zorbex"], allow, []) is not None
        )

    old = da.MINTED_MAX
    da.MINTED_MAX = 3
    try:
        with concurrent.futures.ThreadPoolExecutor(16) as pool:
            assert all(pool.map(work, range(400)))
    finally:
        da.MINTED_MAX = old
