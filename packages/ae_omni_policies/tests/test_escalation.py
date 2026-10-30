"""Escalation (PR 4): the rules, the model route, the log and the set_session_model tool."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session
from test_mcp import _payload, _stub

from ae_omni_policies import escalation, labels, model_log, role_prefs, spawn_role
from ae_omni_policies import router as router_module
from ae_omni_policies.mcp import create_server

OPUS, SONNET, HAIKU = "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"
TELL = {"raise_or_lower": "tell", "lead_steps_down": True, "auto_raises": 1}


# ── the rules ──


@pytest.mark.parametrize(
    ("old", "new", "kind"),
    [
        ((SONNET, "medium"), (OPUS, "medium"), "raise"),
        ((SONNET, "medium"), (SONNET, "high"), "raise"),
        ((OPUS, "high"), (SONNET, "high"), "lower"),
        ((OPUS, "high"), (OPUS, "low"), "lower"),
        ((SONNET, "high"), (SONNET, "high"), "same"),
        # A bigger family at a lower effort is still a raise; the reverse is a lower move.
        ((SONNET, "high"), (OPUS, "low"), "raise"),
        ((OPUS, "low"), (SONNET, "max"), "lower"),
        ((SONNET, None), (SONNET, "low"), "lower"),
        ((OPUS, "high"), ("claude-opus-5-6[1m]", "high"), "raise"),
        # No model reported yet: a known family is never "lower", and a lead's start value
        # with no model is only matched by an unchanged (still unknown) model.
        ((None, "high"), (SONNET, "high"), "raise"),
        ((None, "high"), (HAIKU, "low"), "raise"),
        ((None, "low"), (None, "high"), "raise"),
        ((None, "high"), (None, "low"), "lower"),
        ((None, "high"), (None, "high"), "same"),
    ],
)
def test_classify(old: tuple[str, str | None], new: tuple[str, str], kind: str) -> None:
    assert escalation.classify(*old, *new) == kind


def _decide(rules: dict[str, Any] | None = None, **kw: Any) -> escalation.Decision:
    args: dict[str, Any] = {
        "kind": "raise",
        "is_lead": False,
        "raises": 0,
        "start_kind": None,
        "codex": False,
        "model_changes": True,
        "confirmed": False,
    }
    return escalation.decide(rules or TELL, **(args | kw))


def test_the_off_rule_refuses_every_move() -> None:
    off = TELL | {"raise_or_lower": "off"}
    for kind in ("raise", "lower"):
        assert _decide(off, kind=kind).status == "refused"
    assert _decide(off, kind="same").status == "unchanged"


def test_the_ask_rule_needs_approval_for_raise_and_lower() -> None:
    ask = TELL | {"raise_or_lower": "ask"}
    assert _decide(ask).status == "needs_approval"
    assert _decide(ask, kind="lower").status == "needs_approval"
    done = _decide(ask, confirmed=True)
    assert (done.status, done.approved, done.counted) == ("changed", "owner", True)


def test_the_tell_rule_applies_and_counts_raises_only() -> None:
    raised = _decide()
    assert (raised.status, raised.approved, raised.counted) == ("changed", "auto", True)
    lowered = _decide(kind="lower")
    assert (lowered.status, lowered.counted) == ("changed", False)


def test_the_cap_reached_needs_approval_unless_confirmed() -> None:
    assert _decide(raises=1).status == "needs_approval"
    assert _decide(raises=1, confirmed=True).approved == "owner"
    # A lower move never meets the cap.
    assert _decide(raises=5, kind="lower").status == "changed"


def test_the_lead_lowering_itself_follows_steps_down() -> None:
    assert _decide(is_lead=True, kind="lower").status == "changed"
    no = _decide(TELL | {"lead_steps_down": False}, is_lead=True, kind="lower")
    assert (no.status, no.refusal is not None) == ("refused", True)
    ask = TELL | {"raise_or_lower": "ask"}
    assert _decide(ask, is_lead=True, kind="lower").status == "needs_approval"


def test_the_lead_returns_to_its_start_for_free_and_pays_above_it() -> None:
    # Climbing back to (or under) the start value, even with the cap spent and "ask" on.
    ask = TELL | {"raise_or_lower": "ask"}
    back = _decide(ask, is_lead=True, raises=3, start_kind="same")
    assert (back.status, back.counted, back.approved) == ("changed", False, "auto")
    assert _decide(is_lead=True, start_kind="lower").counted is False
    above = _decide(is_lead=True, start_kind="raise")
    assert (above.status, above.counted) == ("changed", True)
    assert _decide(is_lead=True, start_kind="raise", raises=1).status == "needs_approval"


def test_a_codex_helper_takes_effort_only() -> None:
    refused = _decide(codex=True, model_changes=True)
    assert refused.status == "refused" and "Codex" in (refused.refusal or "")
    effort = _decide(codex=True, model_changes=False)
    assert (effort.status, effort.counted) == ("changed", True)


def test_question_and_options_read_like_the_contract() -> None:
    assert escalation.question("raise", "builder", "Fix CI", OPUS, "high") == (
        'Raise the builder "Fix CI" to Opus high?'
    )
    assert escalation.options("raise", OPUS, "high") == [
        "Raise it to Opus high",
        "Keep it as it is",
    ]
    assert escalation.parse_start(escalation.start_value(SONNET, None)) == (SONNET, None)
    assert escalation.parse_start("") is None


# ── the route ──


class _World:
    """Conversations, labels, the loopback and the log, in memory."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.convs: dict[str, Any] = {}
        self.patches: list[tuple[str, dict[str, Any] | None]] = []
        self.owned: set[str] | None = None
        self.agents: dict[str, str] = {}
        self.rules = dict(TELL)
        self.rules_by: dict[str, dict[str, Any]] = {}
        self.delay = 0.0
        self.log = tmp_path / "model-log.jsonl"
        monkeypatch.setenv(model_log.FILE_ENV, str(self.log))

        async def _conversation(session_id: str) -> Any:
            return self.convs.get(session_id)

        async def _set_labels(session_id: str, updates: dict[str, str]) -> None:
            self.convs[session_id].labels.update(updates)

        async def _may_write(user: str | None, session_id: str) -> bool:
            return self.owned is None or session_id in self.owned

        async def _loopback(request: Any, method: str, path: str, body: Any = None) -> Any:
            self.patches.append((path, body))
            await asyncio.sleep(self.delay)
            return httpx.Response(200, json={})

        async def _agent_name(conv: Any) -> str | None:
            return self.agents.get(conv.id)

        monkeypatch.setattr(router_module, "_conversation", _conversation)
        monkeypatch.setattr(router_module, "_set_labels", _set_labels)
        monkeypatch.setattr(router_module, "_may_write_session", _may_write)
        monkeypatch.setattr(router_module, "_loopback", _loopback)
        monkeypatch.setattr(router_module, "_agent_name", _agent_name)
        monkeypatch.setattr(
            spawn_role,
            "_effective",
            lambda parent, user: {"preset": {"rules": self.rules_by.get(parent.id, self.rules)}},
        )

    def add(self, sid: str, model: str | None, effort: str | None, **lbl: str) -> None:
        self.convs[sid] = SimpleNamespace(
            id=sid,
            title=f"Session {sid}",
            task_summary=None,
            labels=dict(lbl),
            model_override=model,
            reported_model=None,
            reasoning_effort=effort,
            agent_id=None,
        )

    def lines(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _World:
    return _World(monkeypatch, tmp_path)


async def _post(client: httpx.AsyncClient, sid: str, **body: Any) -> httpx.Response:
    return await client.post(f"/v1/ae/sessions/{sid}/model", json=body)


async def test_the_browser_path_applies_and_is_logged_as_user(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.add("conv_a", SONNET, "medium")
    world.rules = TELL | {"raise_or_lower": "off"}  # rules do not apply to the browser
    response = await _post(client, "conv_a", model=OPUS, effort="high", undo=True)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["approved"], body["counted"]) == ("changed", "user", False)
    assert body["undo"] == {"model": SONNET, "effort": "medium"}
    assert world.patches == [
        ("/v1/sessions/conv_a", {"model_override": OPUS, "reasoning_effort": "high"})
    ]
    (line,) = world.lines()
    assert line["approved"] == "user" and line["caller"] is None and line["session"] == "conv_a"
    assert line["from"] == {"model": SONNET, "effort": "medium"} and line["kind"] == "raise"
    assert world.convs["conv_a"].labels[labels.MODEL_START] == f"{SONNET}|medium"
    assert labels.RAISES not in world.convs["conv_a"].labels


async def test_a_bad_body_is_422(client: httpx.AsyncClient, world: _World) -> None:
    world.add("conv_a", SONNET, "medium")
    assert (await _post(client, "conv_a", reason="why")).status_code == 422
    assert (await _post(client, "conv_a", model=OPUS)).status_code == 422
    assert (await _post(client, "conv_gone", model=OPUS, undo=True)).status_code == 404


async def test_a_lead_path_caller_that_is_neither_target_nor_parent_gets_403(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.add("conv_lead", OPUS, "high")
    world.add("conv_other", OPUS, "high")
    world.add("conv_kid", SONNET, "medium", **{labels.ORCH: "conv_lead"})
    denied = await _post(client, "conv_kid", model=OPUS, reason="r", caller="conv_other")
    assert denied.status_code == 403, denied.text
    assert world.patches == []
    world.owned = {"conv_kid"}  # the user does not own the caller
    unowned = await _post(client, "conv_kid", model=OPUS, reason="r", caller="conv_lead")
    assert unowned.status_code == 403
    world.owned = None
    ok = await _post(client, "conv_kid", model=OPUS, reason="r", caller="conv_lead")
    assert ok.json()["status"] == "changed"


async def test_a_raise_under_tell_stamps_the_labels_once_and_counts(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.add("conv_lead", OPUS, "high")
    world.add("conv_kid", SONNET, "medium", **{labels.ORCH: "conv_lead", labels.ROLE: "builder"})
    first = await _post(
        client, "conv_kid", effort="high", reason="CI keeps failing", caller="conv_lead"
    )
    body = first.json()
    assert (body["status"], body["counted"], body["raises"], body["cap"]) == (
        "changed",
        True,
        1,
        1,
    )
    assert (body["approved"], body["is_lead"], body["role"]) == ("auto", False, "builder")
    stamped = world.convs["conv_kid"].labels
    assert stamped[labels.MODEL_START] == f"{SONNET}|medium"
    assert (stamped[labels.RAISES], stamped[labels.RAISE_CAP]) == ("1", "1")
    assert world.patches[-1] == ("/v1/sessions/conv_kid", {"reasoning_effort": "high"})
    (line,) = world.lines()
    assert (line["caller"], line["reason"], line["approved"]) == (
        "conv_lead",
        "CI keeps failing",
        "auto",
    )
    # The cap of one is spent: the next raise asks, and the start value stays the first one.
    world.convs["conv_kid"].reasoning_effort = "high"
    second = (await _post(client, "conv_kid", model=OPUS, reason="r", caller="conv_lead")).json()
    assert second["status"] == "needs_approval"
    assert second["question"] == 'Raise the builder "Session conv_kid" to Opus high?'
    assert second["options"] == ["Raise it to Opus high", "Keep it as it is"]
    assert second["undo"] is None and len(world.patches) == 1
    # A lower move changes it without counting; the start value is not rewritten.
    world.convs["conv_kid"].model_override = OPUS
    lower = (await _post(client, "conv_kid", model=SONNET, reason="r", caller="conv_lead")).json()
    assert (lower["status"], lower["counted"], lower["raises"]) == ("changed", False, 1)
    assert world.convs["conv_kid"].labels[labels.MODEL_START] == f"{SONNET}|medium"


async def test_confirmed_bypasses_approval(client: httpx.AsyncClient, world: _World) -> None:
    world.rules = TELL | {"raise_or_lower": "ask"}
    world.add("conv_lead", OPUS, "high")
    world.add("conv_kid", SONNET, "medium", **{labels.ORCH: "conv_lead"})
    asked = (await _post(client, "conv_kid", model=OPUS, reason="r", caller="conv_lead")).json()
    assert asked["status"] == "needs_approval" and world.patches == []
    done = (
        await _post(client, "conv_kid", model=OPUS, reason="r", caller="conv_lead", confirmed=True)
    ).json()
    assert (done["status"], done["approved"], done["counted"]) == ("changed", "owner", True)
    assert world.lines()[0]["approved"] == "owner"


async def test_the_lead_steps_down_and_climbs_back_free(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.add("conv_lead", OPUS, "high")
    down = (await _post(client, "conv_lead", model=SONNET, reason="r", caller="conv_lead")).json()
    assert (down["status"], down["is_lead"], down["counted"]) == ("changed", True, False)
    world.convs["conv_lead"].model_override = SONNET
    up = (await _post(client, "conv_lead", model=OPUS, reason="r", caller="conv_lead")).json()
    assert (up["status"], up["counted"], up["approved"]) == ("changed", False, "auto")
    world.convs["conv_lead"].model_override = OPUS
    above = (
        await _post(client, "conv_lead", model="claude-fable-5-1", reason="r", caller="conv_lead")
    ).json()
    assert (above["status"], above["counted"]) == ("changed", True)
    assert labels.RAISES in world.convs["conv_lead"].labels


async def test_codex_children_take_effort_only_and_the_model_is_resolved(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.add("conv_lead", OPUS, "high")
    world.add("conv_cx", None, "low", **{labels.ORCH: "conv_lead"})
    # model_override is empty, so the model the harness reported stands in.
    world.convs["conv_cx"].reported_model = "gpt-5.6-sol"
    world.agents["conv_cx"] = role_prefs.CODEX_AGENT_NAME
    refused = (await _post(client, "conv_cx", model=OPUS, reason="r", caller="conv_lead")).json()
    assert refused["status"] == "refused" and refused["from"]["model"] == "gpt-5.6-sol"
    ok = (await _post(client, "conv_cx", effort="high", reason="r", caller="conv_lead")).json()
    assert (ok["status"], ok["counted"], ok["from"]["model"]) == ("changed", True, "gpt-5.6-sol")
    assert world.patches == [("/v1/sessions/conv_cx", {"reasoning_effort": "high"})]


async def test_unchanged_and_a_failed_log_write(
    client: httpx.AsyncClient, world: _World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world.add("conv_lead", OPUS, "high")
    same = (await _post(client, "conv_lead", model=OPUS, reason="r", caller="conv_lead")).json()
    assert same["status"] == "unchanged" and world.patches == []
    monkeypatch.setenv(model_log.FILE_ENV, str(tmp_path))  # a directory: the append fails
    changed = await _post(client, "conv_lead", effort="low", reason="r", caller="conv_lead")
    assert changed.json()["status"] == "changed"
    assert model_log.append({"session": "s"}) is False


def test_the_log_rotates_past_five_megabytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv(model_log.FILE_ENV, str(path))
    path.write_text("x" * (model_log.MAX_BYTES + 1))
    assert model_log.append({"session": "s1"}, now=5) is True
    assert (tmp_path / "log.jsonl.1").stat().st_size == model_log.MAX_BYTES + 1
    assert json.loads(path.read_text()) == {"session": "s1", "at": 5}
    monkeypatch.setenv(model_log.FILE_ENV, "")
    assert model_log.append({"session": "s2"}) is False


# ── the MCP tool ──

ASKED = {
    "status": "needs_approval",
    "question": 'Raise the builder "Fix CI" to Opus high?',
    "options": ["Raise it to Opus high", "Keep it as it is"],
}


def _model_server(seen: list[dict[str, Any]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/ae/sessions/conv_kid/model"
        body = json.loads(request.content)
        seen.append(body)
        if body.get("confirmed"):
            return httpx.Response(200, json={"status": "changed", "approved": "auto"})
        return httpx.Response(200, json=ASKED)

    return _stub(handler)


ARGS = {"session_id": "conv_kid", "model": OPUS, "effort": "high", "reason": "CI keeps failing"}


async def test_set_session_model_confirms_after_owner_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "conv_lead")
    seen: list[dict[str, Any]] = []
    asked: list[Any] = []

    async def accept(context: Any, params: Any) -> types.ElicitResult:
        asked.append(params)
        return types.ElicitResult(
            action="accept",
            content={"choice": params.requestedSchema["properties"]["choice"]["enum"][0]},
        )

    async with create_connected_server_and_client_session(
        create_server(_model_server(seen)), elicitation_callback=accept
    ) as session:
        answer = _payload(await session.call_tool("set_session_model", ARGS))
    assert (answer["status"], answer["approved"]) == ("changed", "owner")
    assert [bool(b.get("confirmed")) for b in seen] == [False, True]
    assert seen[0]["caller"] == "conv_lead" and seen[0]["model"] == OPUS
    assert asked[0].requestedSchema["properties"]["choice"]["enum"][0].endswith("(Recommended)")


async def test_set_session_model_declined_and_without_pop_ups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "conv_lead")
    seen: list[dict[str, Any]] = []

    async def keep(context: Any, params: Any) -> types.ElicitResult:
        enum = params.requestedSchema["properties"]["choice"]["enum"]
        return types.ElicitResult(action="accept", content={"choice": enum[1]})

    async with create_connected_server_and_client_session(
        create_server(_model_server(seen)), elicitation_callback=keep
    ) as session:
        declined = _payload(await session.call_tool("set_session_model", ARGS))
    assert declined["status"] == "declined" and len(seen) == 1
    async with create_connected_server_and_client_session(
        create_server(_model_server([]))
    ) as session:
        plain = _payload(await session.call_tool("set_session_model", ARGS))
    assert plain["status"] == "needs_approval"
    assert plain["instruction"].startswith("Ask the user in chat")


# ── review fixes ──


async def test_parallel_raises_cannot_both_pass_the_cap(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.delay = 0.05
    world.add("conv_lead", OPUS, "high")
    world.add("conv_kid", SONNET, "low", **{labels.ORCH: "conv_lead"})
    results = await asyncio.gather(
        _post(client, "conv_kid", effort="medium", reason="r", caller="conv_lead"),
        _post(client, "conv_kid", effort="high", reason="r", caller="conv_lead"),
    )
    statuses = sorted(r.json()["status"] for r in results)
    assert statuses == ["changed", "needs_approval"]
    assert world.convs["conv_kid"].labels[labels.RAISES] == "1"
    assert len(world.patches) == 1


async def test_a_helper_changing_itself_uses_its_parents_rules(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.add("conv_lead", OPUS, "high")
    world.add("conv_kid", SONNET, "medium", **{labels.ORCH: "conv_lead"})
    world.rules = TELL | {"raise_or_lower": "off"}  # the helper's own default preset
    world.rules_by["conv_lead"] = TELL
    first = (await _post(client, "conv_kid", effort="high", reason="r", caller="conv_kid")).json()
    assert (first["status"], first["is_lead"], first["counted"]) == ("changed", False, True)
    assert world.convs["conv_kid"].labels[labels.RAISES] == "1"
    world.convs["conv_kid"].reasoning_effort = "high"
    # Not the lead: stepping down then climbing back is not free, it is a counted raise.
    world.convs["conv_kid"].reasoning_effort = "low"
    again = (await _post(client, "conv_kid", effort="high", reason="r", caller="conv_kid")).json()
    assert again["status"] == "needs_approval"


async def test_partial_rules_fill_from_the_defaults(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.rules = {"raise_or_lower": "tell"}  # no auto_raises: the default cap is 0
    world.add("conv_lead", OPUS, "high")
    world.add("conv_kid", SONNET, "medium", **{labels.ORCH: "conv_lead"})
    asked = (await _post(client, "conv_kid", effort="high", reason="r", caller="conv_lead")).json()
    assert asked["status"] == "needs_approval" and asked["cap"] == 0


async def test_undo_restores_null_and_clears_with_default(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.add("conv_a", OPUS, "high")
    response = await _post(client, "conv_a", model=None, effort=None, undo=True)
    assert response.json()["status"] == "changed", response.text
    assert world.patches == [
        ("/v1/sessions/conv_a", {"model_override": "default", "reasoning_effort": "default"})
    ]
    assert response.json()["to"] == {"model": None, "effort": None}


async def test_an_unreadable_agent_refuses_a_model_change_but_not_effort(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.add("conv_lead", OPUS, "high")
    world.add("conv_kid", SONNET, "medium", **{labels.ORCH: "conv_lead"})
    world.agents["conv_kid"] = router_module.AGENT_UNKNOWN
    refused = (await _post(client, "conv_kid", model=OPUS, reason="r", caller="conv_lead")).json()
    assert refused["status"] == "refused"
    ok = (await _post(client, "conv_kid", effort="high", reason="r", caller="conv_lead")).json()
    assert ok["status"] == "changed"


async def test_a_failed_label_write_still_logs_and_warns(
    client: httpx.AsyncClient, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add("conv_lead", OPUS, "high")
    world.add("conv_kid", SONNET, "medium", **{labels.ORCH: "conv_lead"})

    async def _boom(session_id: str, updates: dict[str, str]) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(router_module, "_set_labels", _boom)
    body = (await _post(client, "conv_kid", effort="high", reason="r", caller="conv_lead")).json()
    assert body["status"] == "changed" and "not saved" in body["warning"]
    assert len(world.lines()) == 1


async def test_set_session_model_without_a_caller_id_posts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", raising=False)
    monkeypatch.delenv("OMNIGENT_TERMINAL_LAUNCH_SESSION_ID", raising=False)
    seen: list[dict[str, Any]] = []
    async with create_connected_server_and_client_session(
        create_server(_model_server(seen))
    ) as session:
        answer = _payload(await session.call_tool("set_session_model", ARGS))
    assert "own id" in answer["error"] and seen == []


async def test_undo_of_a_session_with_no_override_clears_instead_of_pinning(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.add("conv_a", None, None)
    world.convs["conv_a"].reported_model = SONNET
    raised = (await _post(client, "conv_a", model=OPUS, effort="high", undo=True)).json()
    assert raised["from"] == {"model": SONNET, "effort": None}
    assert raised["undo"] == {"model": None, "effort": None}
    # The PATCH applied: the session now carries the override Undo must clear.
    world.convs["conv_a"].model_override, world.convs["conv_a"].reasoning_effort = OPUS, "high"
    world.patches.clear()
    undone = await _post(client, "conv_a", undo=True, **raised["undo"])
    assert undone.json()["status"] == "changed", undone.text
    assert world.patches == [
        ("/v1/sessions/conv_a", {"model_override": "default", "reasoning_effort": "default"})
    ]


def _last(world: _World, sid: str) -> dict[str, Any]:
    return escalation.parse_last(world.convs[sid].labels[labels.MODEL_LAST]) or {}


async def test_every_change_stamps_model_last(client: httpx.AsyncClient, world: _World) -> None:
    world.add("conv_lead", OPUS, "high")
    world.add("conv_kid", SONNET, "medium", **{labels.ORCH: "conv_lead"})
    await _post(client, "conv_kid", effort="high", reason="CI fails", caller="conv_lead")
    raised = _last(world, "conv_kid")
    assert raised["kind"] == "raise" and raised["undone"] is False
    assert raised["from"] == {"model": SONNET, "effort": "medium"}
    assert raised["to"] == {"model": SONNET, "effort": "high"}
    assert (raised["reason"], raised["caller"], raised["approved"]) == (
        "CI fails",
        "conv_lead",
        "auto",
    )
    assert raised["undo"] == {"model": SONNET, "effort": "medium"}
    assert isinstance(raised["at"], int)
    world.convs["conv_kid"].reasoning_effort = "high"
    await _post(client, "conv_kid", effort="low", reason="done", caller="conv_lead")
    assert _last(world, "conv_kid")["kind"] == "lower"
    await _post(client, "conv_lead", model=SONNET, reason="r", caller="conv_lead")
    stepped = _last(world, "conv_lead")
    assert stepped["kind"] == "lower"
    world.convs["conv_lead"].model_override = SONNET
    await _post(client, "conv_lead", model=OPUS, effort="high", undo=True)
    undone = _last(world, "conv_lead")
    assert (undone["undone"], undone["approved"], undone["caller"]) == (True, "user", None)


def test_model_last_stays_under_the_cap_with_a_long_reason() -> None:
    value = escalation.last_change_label(
        old=(SONNET, "medium"),
        new=(OPUS, "high"),
        kind="raise",
        reason="x" * 5000,
        at=1_800_000_000,
        caller="conv_" + "a" * 40,
        approved="owner",
        undo=(SONNET, "medium"),
        undone=False,
    )
    assert len(value) <= 256
    assert escalation.parse_last(value)["to"] == {"model": OPUS, "effort": "high"}


def test_model_last_real_length_values_lose_nothing() -> None:
    caller, reason = "conv_" + "a" * 32, "r" * 80
    value = escalation.last_change_label(
        old=(SONNET, "medium"),
        new=(OPUS, "high"),
        kind="raise",
        reason=reason,
        at=1_800_000_000,
        caller=caller,
        approved="owner",
        undo=(SONNET, "medium"),
        undone=True,
    )
    assert len(value) <= 256
    assert escalation.parse_last(value) == {
        "from": {"model": SONNET, "effort": "medium"},
        "to": {"model": OPUS, "effort": "high"},
        "kind": "raise",
        "reason": reason,
        "at": 1_800_000_000,
        "caller": caller,
        "approved": "owner",
        "undo": {"model": SONNET, "effort": "medium"},
        "undone": True,
    }


def test_parse_last_accepts_the_long_key_form() -> None:
    old = json.dumps(
        {
            "from": {"model": SONNET, "effort": "low"},
            "to": {"model": OPUS, "effort": "low"},
            "kind": "raise",
            "reason": "r",
            "at": 5,
            "caller": None,
            "approved": "auto",
            "undo": {"model": None, "effort": None},
            "undone": False,
        }
    )
    parsed = escalation.parse_last(old)
    assert parsed is not None and parsed["kind"] == "raise" and parsed["at"] == 5


def _stamp(world: _World, sid: str, age: float, **extra: Any) -> None:
    import time

    value = escalation.last_change_label(
        old=(SONNET, "low"),
        new=(OPUS, "high"),
        kind="raise",
        reason="r",
        at=int(time.time() - age),
        caller=None,
        approved="auto",
        undo=(SONNET, "low"),
        undone=False,
    )
    world.convs[sid].labels[labels.MODEL_LAST] = extra.get("raw", value)


async def _changes(client: httpx.AsyncClient, sid: str) -> httpx.Response:
    return await client.get(f"/v1/ae/sessions/{sid}/model-changes")


async def test_model_changes_lists_the_family_newest_first(
    client: httpx.AsyncClient, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add("conv_lead", OPUS, "high")
    world.add("conv_new", SONNET, "low", **{labels.ORCH: "conv_lead", labels.ROLE: "builder"})
    world.add("conv_old", SONNET, "low", **{labels.ORCH: "conv_lead"})
    world.add("conv_bad", SONNET, "low", **{labels.ORCH: "conv_lead"})
    world.add("conv_quiet", SONNET, "low", **{labels.ORCH: "conv_lead"})
    world.add("conv_foreign", SONNET, "low", **{labels.ORCH: "conv_other"})
    _stamp(world, "conv_lead", 3600)
    _stamp(world, "conv_new", 60)
    _stamp(world, "conv_old", 25 * 3600)
    _stamp(world, "conv_bad", 10, raw="{not json")
    _stamp(world, "conv_foreign", 5)
    monkeypatch.setattr(
        router_module.sessions, "list_top_level", lambda *a, **k: list(world.convs.values())
    )
    response = await _changes(client, "conv_lead")
    assert response.status_code == 200, response.text
    items = response.json()["changes"]
    assert [item["session_id"] for item in items] == ["conv_new", "conv_lead"]
    assert (items[0]["role"], items[0]["is_lead"], items[1]["is_lead"]) == ("builder", False, True)
    assert items[0]["title"] and items[0]["change"]["to"] == {"model": OPUS, "effort": "high"}


async def test_model_changes_checks_owner_and_existence(
    client: httpx.AsyncClient, world: _World
) -> None:
    world.add("conv_lead", OPUS, "high")
    world.owned = set()
    assert (await _changes(client, "conv_lead")).status_code == 403
    world.owned = None
    assert (await _changes(client, "conv_gone")).status_code == 404


def test_model_last_that_cannot_fit_is_none_and_never_over_the_cap() -> None:
    kw: dict[str, Any] = {
        "old": ("m" * 200, "low"),
        "new": (OPUS, "high"),
        "kind": "raise",
        "reason": "r",
        "at": 1_800_000_000,
        "caller": None,
        "approved": "auto",
        "undo": ("m" * 200, "low"),
        "undone": False,
    }
    # The old pair is never dropped to make room: an Undo must restore it, not clear it.
    assert escalation.last_change_label(**kw) is None
    fits = escalation.last_change_label(
        **kw | {"old": ("m" * 150, "low"), "undo": ("m" * 150, "low")}
    )
    assert fits is not None and len(fits) <= 256
    parsed = escalation.parse_last(fits)
    assert parsed is not None and parsed["undo"] == {"model": "m" * 150, "effort": "low"}
    assert escalation.last_change_label(**kw | {"new": ("n" * 300, "high")}) is None


async def test_model_changes_skips_another_owners_helper(
    client: httpx.AsyncClient, world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.add("conv_lead", OPUS, "high")
    world.add("conv_mine", SONNET, "low", **{labels.ORCH: "conv_lead"})
    world.add("conv_theirs", SONNET, "low", **{labels.ORCH: "conv_lead"})
    _stamp(world, "conv_mine", 10)
    _stamp(world, "conv_theirs", 5)
    world.owned = {"conv_lead", "conv_mine"}
    seen: list[Any] = []

    def _list(*args: Any, **kwargs: Any) -> list[Any]:
        seen.append(kwargs.get("accessible_by", "unset"))
        return list(world.convs.values())

    monkeypatch.setattr(router_module.sessions, "list_top_level", _list)
    response = await _changes(client, "conv_lead")
    assert [i["session_id"] for i in response.json()["changes"]] == ["conv_mine"]
    assert seen and seen[0] != "unset"


async def test_report_review_posts_the_counts_as_the_calling_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "conv_lead")
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/ae/sessions/conv_lead/review-report"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "totals": {"calls": 1}})

    args = {"reviewers_run": 2, "findings_returned": 5, "findings_fixed": 4}
    async with create_connected_server_and_client_session(create_server(_stub(handler))) as s:
        answer = _payload(await s.call_tool("report_review", args))
        monkeypatch.delenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID")
        monkeypatch.delenv("OMNIGENT_TERMINAL_LAUNCH_SESSION_ID", raising=False)
        missing = _payload(await s.call_tool("report_review", args))
    assert answer["ok"] is True and seen == [args]
    assert "own id" in missing["error"]


async def test_report_review_tool_passes_its_call_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_PRIMARY_SESSION_ID", "conv_lead")
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "status": "repeat"})

    args = {"reviewers_run": 2, "findings_returned": 5, "findings_fixed": 4, "call_id": "final"}
    async with create_connected_server_and_client_session(create_server(_stub(handler))) as s:
        answer = _payload(await s.call_tool("report_review", args))
    assert answer["status"] == "repeat" and seen == [args]


async def test_review_report_route_adds_up_and_follows_a_child_to_its_lead(
    client: httpx.AsyncClient, world: _World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ae_omni_policies import preset_runs

    monkeypatch.setenv(preset_runs.STATE_FILE_ENV, str(tmp_path / "runs-state.json"))
    monkeypatch.setattr(preset_runs, "insights_enabled", lambda _user: True)
    world.add("conv_lead", OPUS, "high")
    world.add("conv_kid", SONNET, "medium", **{labels.ORCH: "conv_lead"})
    body = {"reviewers_run": 2, "findings_returned": 5, "findings_fixed": 4}
    first = await client.post("/v1/ae/sessions/conv_lead/review-report", json=body)
    assert first.status_code == 200, first.text
    second = await client.post("/v1/ae/sessions/conv_kid/review-report", json=body)
    answer = second.json()
    assert answer["session_id"] == "conv_lead"
    assert answer["totals"] == {
        "reviewers_run": 4,
        "findings_returned": 10,
        "findings_fixed": 8,
        "calls": 2,
    }
    missing = await client.post("/v1/ae/sessions/nope/review-report", json=body)
    assert missing.status_code == 404
    bad = await client.post("/v1/ae/sessions/conv_lead/review-report", json=body | {"x": 1})
    assert bad.status_code == 422
    empty_id = await client.post(
        "/v1/ae/sessions/conv_lead/review-report", json=body | {"call_id": ""}
    )
    assert empty_id.status_code == 422


async def test_review_report_route_ignores_a_repeated_call_id(
    client: httpx.AsyncClient, world: _World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ae_omni_policies import preset_runs

    monkeypatch.setenv(preset_runs.STATE_FILE_ENV, str(tmp_path / "runs-state.json"))
    monkeypatch.setattr(preset_runs, "insights_enabled", lambda _user: True)
    world.add("conv_lead", OPUS, "high")
    world.add("conv_kid", SONNET, "medium", **{labels.ORCH: "conv_lead"})
    body = {"reviewers_run": 5, "findings_returned": 38, "findings_fixed": 29, "call_id": "final"}
    first = await client.post("/v1/ae/sessions/conv_lead/review-report", json=body)
    assert first.json()["status"] == "stored", first.text
    # The same round again, even from a child of the lead, adds nothing.
    again = await client.post("/v1/ae/sessions/conv_kid/review-report", json=body)
    answer = again.json()
    assert again.status_code == 200 and answer["status"] == "repeat"
    assert answer["totals"] == {
        "reviewers_run": 5,
        "findings_returned": 38,
        "findings_fixed": 29,
        "calls": 1,
    }


async def test_review_report_needs_ownership_of_the_resolved_lead(
    client: httpx.AsyncClient, world: _World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ae_omni_policies import preset_runs

    monkeypatch.setenv(preset_runs.STATE_FILE_ENV, str(tmp_path / "runs-state.json"))
    monkeypatch.setattr(preset_runs, "insights_enabled", lambda _user: True)
    world.add("conv_lead", OPUS, "high")
    world.add("conv_kid", SONNET, "medium", **{labels.ORCH: "conv_lead"})
    body = {"reviewers_run": 1, "findings_returned": 1, "findings_fixed": 1}
    # The caller owns the kid but not the lead its ae.orch points at (a foreign or view-only lead).
    world.owned = {"conv_kid"}
    denied = await client.post("/v1/ae/sessions/conv_kid/review-report", json=body)
    assert denied.status_code == 403, denied.text
    world.owned = {"conv_kid", "conv_lead"}
    allowed = await client.post("/v1/ae/sessions/conv_kid/review-report", json=body)
    assert allowed.status_code == 200, allowed.text


async def test_review_report_checks_insights_for_the_leads_owner(
    client: httpx.AsyncClient, world: _World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ae_omni_policies import preset_runs

    monkeypatch.setenv(preset_runs.STATE_FILE_ENV, str(tmp_path / "runs-state.json"))
    asked: list[Any] = []

    def _on(user: Any) -> bool:
        asked.append(user)
        return user == "the-owner"

    monkeypatch.setattr(preset_runs, "insights_enabled", _on)
    monkeypatch.setattr(router_module._owner_identity, "owner", lambda _id: "the-owner")
    world.add("conv_lead", OPUS, "high")
    body = {"reviewers_run": 1, "findings_returned": 1, "findings_fixed": 1}
    answer = await client.post("/v1/ae/sessions/conv_lead/review-report", json=body)
    assert answer.json()["status"] == "stored" and asked == ["the-owner"]


def test_legacy_approver_value_reads_as_owner() -> None:
    legacy = "".join(("re", "id"))  # what records carried before 2026-10-28
    assert model_log.normalize_approved(legacy) == "owner"
    assert model_log.normalize_approved("auto") == "auto"
    assert model_log.normalize({"approved": legacy, "kind": "raise"}) == {
        "approved": "owner",
        "kind": "raise",
    }
    assert model_log.normalize({"kind": "raise"}) == {"kind": "raise"}
    assert (
        escalation.parse_last(
            json.dumps({"f": [SONNET, "low"], "t": [OPUS, "low"], "a": 1, "p": legacy})
        )["approved"]
        == "owner"
    )
