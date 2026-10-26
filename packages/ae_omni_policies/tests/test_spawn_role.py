"""Spawn by role: create_session's role, model, effort and preset, and the preset route."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from omnigent.runtime import get_conversation_store
from test_router import START, _App, _labels, _session, home  # noqa: F401

from ae_omni_policies import labels, preset_log, role_prefs, spawn_role
from ae_omni_policies import router as router_module

pytestmark = pytest.mark.usefixtures("home")

NO_LEAD = {k: v for k, v in START.items() if k != "orchestrator"}


def _fixed(model: str, effort: str) -> dict[str, str]:
    return {"mode": "fixed", "model": model, "effort": effort}


def _preset(pid: str = "balanced", **roles: Any) -> dict[str, Any]:
    base = {
        "lead": {"mode": "session"},
        "planner": _fixed("claude-opus-5-5", "high"),
        "reviewer": {"mode": "auto"},
        "builder": _fixed("claude-sonnet-5-5", "medium"),
        "helper": _fixed("claude-haiku-5-5", "low"),
    }
    return {"preset": {"id": pid, "title": pid, "roles": base | roles, "rules": {}}}


@pytest.fixture(autouse=True)
def _prefs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.delenv(role_prefs.STATE_FILE_ENV, raising=False)
    monkeypatch.setattr(role_prefs, "DATA_DIR", Path("/nonexistent-ae-data"))
    role_prefs.reset_store()
    log = tmp_path / "preset-log.jsonl"
    monkeypatch.setenv(preset_log.FILE_ENV, str(log))
    yield log
    role_prefs.reset_store()


def _use(monkeypatch: pytest.MonkeyPatch, effective: dict[str, Any]) -> None:
    monkeypatch.setattr(spawn_role, "_effective", lambda parent, user: effective)


def _create(app: _App) -> dict[str, Any]:
    body = app.calls[2][2]
    assert body is not None
    return body


async def _start(client: httpx.AsyncClient, **extra: Any) -> httpx.Response:
    return await client.post("/v1/ae/sessions", json=NO_LEAD | extra)


async def test_fixed_role_sets_model_effort_and_labels(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    _use(monkeypatch, _preset("deep-debug"))
    response = await _start(client, role="builder")
    assert response.status_code == 200, response.text
    create = _create(app)
    assert (create["model_override"], create["reasoning_effort"]) == (
        "claude-sonnet-5-5",
        "medium",
    )
    assert create["labels"][labels.ROLE] == "builder"
    assert create["labels"][labels.PRESET] == "balanced", "the owner default, not the lead preset"
    assert "ae.team" not in create["labels"], "no preset asked, so the child keeps the default"
    assert response.json()["model"] == "claude-sonnet-5-5"


async def test_auto_role_and_no_role_leave_model_and_effort_unset(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    _use(monkeypatch, _preset())
    await _start(client, role="reviewer")
    create = _create(app)
    assert "model_override" not in create and "reasoning_effort" not in create
    app.calls.clear()
    await _start(client, key="other")
    create = _create(app)
    assert labels.ROLE not in create["labels"]
    assert create["labels"][labels.PRESET] == "balanced"
    assert "model_override" not in create


async def test_lead_picks_leaves_both_unset(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    _use(monkeypatch, {"lead_picks": True, "presets": [], "more": 0})
    await _start(client, role="builder")
    create = _create(app)
    assert "model_override" not in create
    assert create["labels"][labels.PRESET] == "balanced", "the owner's default, not the lead's"


async def test_multi_reviewer_uses_slot_one_and_codex_switches_the_harness(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    claude_first = {
        "mode": "multi",
        "reviewers": [_fixed("claude-opus-5-5", "xhigh"), {"mode": "auto"}],
    }
    _use(monkeypatch, _preset(reviewer=claude_first))
    await _start(client, role="reviewer")
    create = _create(app)
    assert (create["model_override"], create["reasoning_effort"]) == ("claude-opus-5-5", "xhigh")
    assert create["agent_id"] == "ag_native"

    app.calls.clear()
    codex = {
        "mode": "agent",
        "agent": "x",
        "agent_name": role_prefs.CODEX_AGENT_NAME,
        "name": "Codex",
        "model": "gpt-5.6-sol",
        "effort": "high",
    }
    _use(monkeypatch, _preset(reviewer={"mode": "multi", "reviewers": [codex]}))
    response = await _start(client, role="reviewer", key="codex-1")
    assert response.status_code == 200, response.text
    create = _create(app)
    assert create["agent_id"] == "ag_codex"
    assert (create["model_override"], create["reasoning_effort"]) == ("gpt-5.6-sol", "high")


async def test_explicit_model_and_effort_beat_the_role(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    _use(monkeypatch, _preset())
    await _start(client, role="builder", model="claude-opus-5-5", effort="max")
    create = _create(app)
    assert (create["model_override"], create["reasoning_effort"]) == ("claude-opus-5-5", "max")


def test_session_role_uses_the_parents_own_model_and_effort() -> None:
    parent = SimpleNamespace(model_override="claude-opus-5-5", reasoning_effort="xhigh")
    out = spawn_role._from_role("lead", _preset(), parent)
    assert (out.model, out.effort) == ("claude-opus-5-5", "xhigh")
    out = spawn_role._from_role("lead", _preset(), None)
    assert (out.model, out.effort) == (None, None)


async def test_preset_sets_the_childs_team_label(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    response = await _start(client, role="builder", preset="yolo")
    assert response.status_code == 200, response.text
    create = _create(app)
    assert create["labels"][labels.PRESET] == "yolo"
    assert json.loads(create["labels"]["ae.team"]) == {
        "version": 1,
        "mode": "auto",
        "members": [],
        "roles": {"preset": "yolo"},
    }


async def test_bad_role_effort_and_preset_are_rejected_with_422(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    assert (await _start(client, role="boss")).status_code == 422
    assert (await _start(client, effort="turbo")).status_code == 422
    assert (await _start(client, preset="Not An Id")).status_code == 422
    unknown = await _start(client, preset="nope-not-saved")
    assert unknown.status_code == 422
    assert "Settings > Roles" in unknown.json()["error"]["message"]
    assert app.calls == [], "nothing was created"


async def test_creating_logs_the_childs_preset(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, _prefs: Path
) -> None:
    monkeypatch.setattr(router_module, "_loopback", _App())
    monkeypatch.setattr(router_module.time, "time", lambda: 1_800_000_000.0)
    await _start(client, role="helper")
    line = json.loads(_prefs.read_text().splitlines()[0])
    assert line == {
        "session": "conv_resumed",
        "from": None,
        "to": "balanced",
        "at": 1_800_000_000,
        "by": line["by"],
    }


# ── PUT /v1/ae/sessions/{id}/preset ──


async def test_put_preset_sets_the_label_and_logs_once(
    client: httpx.AsyncClient, _prefs: Path
) -> None:
    sid = await _session(client, "Chat", {labels.PRESET: "balanced"})
    first = await client.put(f"/v1/ae/sessions/{sid}/preset", json={"preset": "yolo"})
    assert first.status_code == 200, first.text
    assert first.json() == {"ok": True, "preset": "yolo"}
    assert _labels(sid)[labels.PRESET] == "yolo"
    lines = [json.loads(x) for x in _prefs.read_text().splitlines()]
    assert [(x["session"], x["from"], x["to"]) for x in lines] == [(sid, "balanced", "yolo")]
    assert isinstance(lines[0]["at"], int) and "by" in lines[0]
    again = await client.put(f"/v1/ae/sessions/{sid}/preset", json={"preset": "yolo"})
    assert again.json() == {"ok": True, "preset": "yolo"}
    assert len(_prefs.read_text().splitlines()) == 1, "a no-op writes no line"


async def test_put_preset_from_nothing_logs_a_null_from(
    client: httpx.AsyncClient, _prefs: Path
) -> None:
    sid = await _session(client, "Chat")
    await client.put(f"/v1/ae/sessions/{sid}/preset", json={"preset": "balanced"})
    assert json.loads(_prefs.read_text())["from"] is None


async def test_put_preset_unknown_session_is_404_and_foreign_is_403(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, _prefs: Path
) -> None:
    gone = await client.put("/v1/ae/sessions/conv_gone/preset", json={"preset": "yolo"})
    assert gone.status_code == 404
    sid = await _session(client, "Not yours")

    async def _closed(user: str | None, session_id: str) -> bool:
        return False

    monkeypatch.setattr(router_module, "_may_write_session", _closed)
    denied = await client.put(f"/v1/ae/sessions/{sid}/preset", json={"preset": "yolo"})
    assert denied.status_code == 403
    assert labels.PRESET not in _labels(sid)
    assert not _prefs.exists()
    bad = await client.put(f"/v1/ae/sessions/{sid}/preset", json={"preset": "No Good"})
    assert bad.status_code == 422


async def test_a_failed_log_write_never_fails_the_change(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The log path is a directory, so opening it for append raises.
    monkeypatch.setenv(preset_log.FILE_ENV, str(tmp_path))
    sid = await _session(client, "Chat")
    response = await client.put(f"/v1/ae/sessions/{sid}/preset", json={"preset": "yolo"})
    assert response.status_code == 200, response.text
    assert _labels(sid)[labels.PRESET] == "yolo"
    assert preset_log.append("s", None, "x", None) is False


def test_the_log_rotates_past_five_megabytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv(preset_log.FILE_ENV, str(path))
    path.write_text("x" * (preset_log.MAX_BYTES + 1))
    assert preset_log.append("s1", "a", "b", "me", now=5) is True
    assert (tmp_path / "log.jsonl.1").stat().st_size == preset_log.MAX_BYTES + 1
    assert json.loads(path.read_text())["session"] == "s1"
    monkeypatch.setenv(preset_log.FILE_ENV, "")
    assert preset_log.append("s2", None, "b", None) is False


def test_the_store_default_preset_is_used_without_a_parent() -> None:
    state = role_prefs.store().get("local").model_dump()
    out = spawn_role.resolve(
        parent=None, user=None, role="builder", model=None, effort=None, preset=None
    )
    assert out.preset == state["default_preset"]
    assert get_conversation_store() is not None


class _ViewerStore:
    """A permission store where the caller holds a read grant only."""

    def is_admin(self, user_id: str) -> bool:
        return False

    def check_access(self, user_id: str | None, conversation_id: str, level: int) -> bool:
        return level <= 1


def _as_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_module, "_acl_user", lambda user: "viewer@example.com")
    monkeypatch.setattr(router_module, "_permission_store", lambda: _ViewerStore())

    async def _readable(request: Any, user: str | None, session_id: str) -> bool:
        return True

    monkeypatch.setattr(router_module, "_may_open_session", _readable)


async def test_a_view_only_user_cannot_change_the_preset(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, _prefs: Path
) -> None:
    sid = await _session(client, "Shared")
    _as_viewer(monkeypatch)
    denied = await client.put(f"/v1/ae/sessions/{sid}/preset", json={"preset": "yolo"})
    assert denied.status_code == 403, denied.text
    assert labels.PRESET not in _labels(sid)


async def test_put_preset_unknown_id_is_422_and_lead_picks_is_allowed(
    client: httpx.AsyncClient, _prefs: Path
) -> None:
    sid = await _session(client, "Chat")
    unknown = await client.put(f"/v1/ae/sessions/{sid}/preset", json={"preset": "nope-nope"})
    assert unknown.status_code == 422
    assert labels.PRESET not in _labels(sid)
    ok = await client.put(f"/v1/ae/sessions/{sid}/preset", json={"preset": "lead-picks"})
    assert ok.status_code == 200, ok.text


async def test_a_view_only_user_cannot_lead_a_family_under_a_session(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    lead = await _session(client, "Someone else's", {labels.STATUS: "working"})
    _as_viewer(monkeypatch)
    response = await _start(client, orchestrator=lead)
    assert response.status_code == 200, response.text
    assert _create(app)["labels"][labels.ORCH] == "1"


async def test_the_automatic_key_tells_roles_apart_and_still_dedupes(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    _use(monkeypatch, _preset())
    plain = {k: v for k, v in NO_LEAD.items() if k != "key"}

    async def spawn(**extra: Any) -> dict[str, Any]:
        response = await client.post("/v1/ae/sessions", json=plain | extra)
        assert response.status_code == 200, response.text
        return response.json()

    builder = await spawn(role="builder")
    reviewer = await spawn(role="reviewer")
    assert builder["key"] != reviewer["key"]
    assert reviewer["existing"] is False
    creates = [c for c in app.calls if c[:2] == ("POST", "/v1/sessions")]
    assert len(creates) == 2
    # Make the first live so an identical spawn finds it.
    get_conversation_store().set_labels(
        await _session(
            client, "live", {labels.STATUS: "working", labels.ORCH_KEY: builder["key"]}
        ),
        {},
    )
    again = await spawn(role="builder")
    assert (again["key"], again["existing"]) == (builder["key"], True)


async def test_a_named_harness_that_differs_from_the_slot_drops_the_role_values(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    codex = {
        "mode": "agent",
        "agent": "x",
        "agent_name": role_prefs.CODEX_AGENT_NAME,
        "name": "Codex",
        "model": "gpt-5.6-sol",
        "effort": "high",
    }
    _use(monkeypatch, _preset(reviewer={"mode": "multi", "reviewers": [codex]}))
    await _start(client, role="reviewer", harness="claude", key="a")
    create = _create(app)
    assert create["agent_id"] == "ag_native"
    assert "model_override" not in create and "reasoning_effort" not in create

    app.calls.clear()
    await _start(client, role="builder", harness="codex", key="b")
    create = _create(app)
    assert create["agent_id"] == "ag_codex"
    assert "model_override" not in create and "reasoning_effort" not in create

    app.calls.clear()
    await _start(client, role="builder", harness="codex", model="gpt-5.6-sol", key="c")
    create = _create(app)
    assert create["model_override"] == "gpt-5.6-sol"
    assert "reasoning_effort" not in create


async def test_a_retry_dedupes_after_the_leads_preset_changed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    plain = {k: v for k, v in NO_LEAD.items() if k != "key"}
    _use(monkeypatch, _preset())
    first = (await client.post("/v1/ae/sessions", json=plain | {"role": "builder"})).json()
    await _session(client, "live", {labels.STATUS: "working", labels.ORCH_KEY: first["key"]})
    _use(monkeypatch, _preset(builder=_fixed("claude-opus-5-5", "max")))
    again = (await client.post("/v1/ae/sessions", json=plain | {"role": "builder"})).json()
    assert (again["key"], again["existing"]) == (first["key"], True)


async def test_a_failing_role_resolution_still_creates_the_session(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)

    def broken() -> Any:
        raise OSError("roles store unreadable")

    monkeypatch.setattr(spawn_role, "_prefs", lambda user: broken())
    response = await _start(client, role="builder")
    assert response.status_code == 200, response.text
    create = _create(app)
    assert "model_override" not in create and "reasoning_effort" not in create
    assert create["labels"][labels.ROLE] == "builder"


async def test_reviewer_with_codex_harness_picks_the_codex_slot_not_slot_one(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    codex = {
        "mode": "agent",
        "agent": "x",
        "agent_name": role_prefs.CODEX_AGENT_NAME,
        "name": "Codex",
        "model": "gpt-5.6-sol",
        "effort": "high",
    }
    both = {"mode": "multi", "reviewers": [_fixed("claude-opus-5-5", "xhigh"), codex]}
    _use(monkeypatch, _preset(reviewer=both))
    response = await _start(client, role="reviewer", harness="codex", key="r1")
    assert response.status_code == 200, response.text
    create = _create(app)
    assert create["agent_id"] == "ag_codex"
    assert (create["model_override"], create["reasoning_effort"]) == ("gpt-5.6-sol", "high")

    app.calls.clear()
    await _start(client, role="reviewer", key="r2")
    create = _create(app)
    assert create["agent_id"] == "ag_native"
    assert (create["model_override"], create["reasoning_effort"]) == ("claude-opus-5-5", "xhigh")


def test_a_standalone_chat_takes_its_role_from_the_preset_it_opens_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep = _preset("deep", lead=_fixed("claude-opus-5-5", "xhigh"))["preset"]
    plain = _preset("balanced")["preset"]
    monkeypatch.setattr(spawn_role, "_prefs", lambda user: ("balanced", [plain, deep]))
    # The opener's own preset decides a helper's role, never a standalone chat's.
    _use(monkeypatch, _preset("opener", lead=_fixed("claude-haiku-5-5", "low")))
    opener = SimpleNamespace(model_override="claude-sonnet-5-5", reasoning_effort="medium")

    def resolve(preset: str | None, standalone: bool = True) -> spawn_role.Spawn:
        return spawn_role.resolve(
            parent=opener,
            user=None,
            role="lead",
            model=None,
            effort=None,
            preset=preset,
            standalone=standalone,
        )

    out = resolve("deep")
    assert (out.model, out.effort, out.preset) == ("claude-opus-5-5", "xhigh", "deep")
    # No preset: the owner's default, whose session lead runs on the opener's model.
    out = resolve(None)
    assert (out.model, out.effort, out.preset) == ("claude-sonnet-5-5", "medium", "balanced")
    out = resolve("lead-picks")
    assert (out.model, out.effort) == (None, None)
    out = resolve("deep", standalone=False)
    assert (out.model, out.effort) == ("claude-haiku-5-5", "low")


async def test_a_standalone_chat_leads_no_family_and_holds_its_own_slot(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    lead = await _session(client, "Opener", {labels.STATUS: "working"})
    # A Haiku lead here would show the route fell back to the opener's preset.
    _use(monkeypatch, _preset("opener", lead=_fixed("claude-haiku-5-5", "low")))
    opener = SimpleNamespace(model_override="claude-opus-5-5", reasoning_effort="high")

    async def _conv(session_id: str) -> Any:
        return opener

    monkeypatch.setattr(router_module, "_conversation", _conv)
    response = await _start(client, orchestrator=lead, standalone=True, preset="balanced")
    assert response.status_code == 200, response.text
    create = _create(app)
    assert create["labels"][labels.ORCH] == "1"
    assert create["labels"][labels.ROLE] == "lead"
    assert create["labels"][labels.PRESET] == "balanced"
    assert json.loads(create["labels"]["ae.team"])["roles"] == {"preset": "balanced"}
    assert (create["model_override"], create["reasoning_effort"]) == ("claude-opus-5-5", "high")
    # With every slot taken it waits like any lead, though the opener's family has room.
    for title in ("Two", "Three"):
        await _session(client, title, {labels.STATUS: "working"})
    full = await _start(
        client, orchestrator=lead, standalone=True, preset="balanced", key="handoff-2"
    )
    assert full.status_code == 409, full.text
    assert full.json()["error"]["code"] == "slots_full"
    unknown = await _start(client, orchestrator=lead, standalone=True, preset="nope-nope")
    assert unknown.status_code == 422, unknown.text


def test_a_session_lead_takes_the_parents_harness_with_its_model() -> None:
    codex = SimpleNamespace(
        model_override="gpt-5.5",
        reasoning_effort="medium",
        labels={"omnigent.wrapper": "codex-native-ui"},
    )

    def resolve(harness: str | None) -> spawn_role.Spawn:
        return spawn_role.resolve(
            parent=codex,
            user=None,
            role="lead",
            model=None,
            effort=None,
            preset="balanced",
            harness=harness,
            standalone=True,
        )

    out = resolve(None)
    assert (out.harness, out.model, out.effort) == ("codex", "gpt-5.5", "medium")
    assert (resolve("codex").model, resolve("codex").effort) == ("gpt-5.5", "medium")
    out = resolve("claude")
    assert (out.harness, out.model, out.effort) == ("claude", None, None)
