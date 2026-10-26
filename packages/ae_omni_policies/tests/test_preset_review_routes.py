"""The preset review routes: roles toggle, the reviewer calls, suggestion actions."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from test_preset_review import Server, _row

from ae_omni_policies import github, preset_runs, preset_suggestions, role_prefs
from ae_omni_policies import router as router_module
from ae_omni_policies.role_prefs import RolePrefsStore

REID = {"X-Forwarded-Email": "reid@example.com"}
STEVE = {"X-Forwarded-Email": "steve@example.com"}
INSIGHTS = {"enabled": True, "weeks": 2, "scope": "all"}
SONNET_HIGH = {"mode": "fixed", "model": role_prefs.SONNET, "effort": "high"}


class World:
    """Session rows, ownership, the loopback task API and the rename, in memory."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.agents: dict[str, str | None] = {}
        self.owners: dict[str, str] = {}
        self.may_write = True
        self.titles: dict[str, str] = {}
        self.server = Server()
        self.run_status = 202
        role_prefs.reset_store(RolePrefsStore(tmp_path / "roles.json"))
        preset_suggestions.reset_store(preset_suggestions.SuggestionStore(tmp_path / "sugs.json"))
        monkeypatch.setenv(preset_runs.FILE_ENV, str(tmp_path / "runs.jsonl"))
        monkeypatch.setenv("AE_TASK_STORE_TZ", "America/Los_Angeles")

        async def _conversation(session_id: str) -> Any:
            if session_id not in self.agents:
                return None
            return SimpleNamespace(id=session_id, labels={})

        async def _may_write(_user: str | None, _session_id: str) -> bool:
            return self.may_write

        async def _agent_name(conv: Any) -> str | None:
            return self.agents.get(conv.id)

        @asynccontextmanager
        async def _client(_request: Any) -> AsyncIterator[httpx.AsyncClient]:
            inner = self.server

            def handler(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/run"):
                    inner.calls.append((request.method, request.url.path, None))
                    return httpx.Response(self.run_status, json={})
                return inner(request)

            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), base_url="http://box"
            ) as http:
                yield http

        class Store:
            def update_conversation(_self, session_id: str, title: str) -> None:
                self.titles[session_id] = title

        monkeypatch.setattr(router_module, "_conversation", _conversation)
        monkeypatch.setattr(router_module, "_may_write_session", _may_write)
        monkeypatch.setattr(router_module, "_agent_name", _agent_name)
        monkeypatch.setattr(router_module, "_loopback_client", _client)
        monkeypatch.setattr(router_module, "get_conversation_store", lambda: Store())
        monkeypatch.setattr(
            router_module._owner_identity, "owner", lambda sid: self.owners.get(sid)
        )

    def reviewer(self, sid: str = "conv_rev", owner: str | None = None, due: bool = True) -> str:
        self.agents[sid] = preset_runs.REVIEWER_AGENT
        if owner:
            self.owners[sid] = owner
        if due:
            # The session was told its read is due, so it may submit.
            preset_suggestions.store().note_due_read(sid, 1.0)
        return sid


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> World:
    router_module._repo_private.clear()
    yield World(monkeypatch, tmp_path)
    role_prefs.reset_store()
    preset_suggestions.reset_store()


def _roles_body(revision: int = 0, **extra: Any) -> dict[str, Any]:
    starters = role_prefs.starters()
    mine = copy.deepcopy(starters[1]) | {"id": "mine", "title": "Mine", "pinned": False}
    return {
        "revision": revision,
        "default_preset": "mine",
        "apply_start_on": True,
        "presets": [mine],
        **extra,
    }


async def _enable(client: httpx.AsyncClient, headers: dict[str, str], **insights: Any) -> dict:
    current = (await client.get("/v1/ae/roles", headers=headers)).json()
    body = _roles_body(current["revision"], insights=INSIGHTS | insights)
    response = await client.put("/v1/ae/roles", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _own(**kw: Any) -> dict[str, Any]:
    return {
        "kind": "own",
        "preset_id": "mine",
        "title": "Plan on Sonnet",
        "why": "12 runs, median $9 against $14.",
        "saving": "about $5 per run",
        "changes": [{"path": "roles.planner", "value": SONNET_HIGH}],
    } | kw


def _submit_body(*items: dict[str, Any]) -> dict[str, Any]:
    return {"summary": "Checked.", "suggestions": list(items)}


# -- the roles toggle


async def test_saving_with_insights_on_creates_the_task_and_off_deletes_it(
    client: httpx.AsyncClient, world: World
) -> None:
    on = await client.put("/v1/ae/roles", json=_roles_body(insights=INSIGHTS))
    assert on.status_code == 200, on.text
    assert on.json()["insights_task"] == {"ok": True, "action": "created"}
    assert world.server.writes()[0][:2] == ("POST", "/v1/scheduled-tasks")
    world.server.rows = [_row(id="task_1")]
    world.server.calls.clear()
    # Every save with insights on keeps the model in step with the default preset.
    again = await client.put("/v1/ae/roles", json=_roles_body(1, insights=INSIGHTS))
    assert again.json()["insights_task"]["action"] == "unchanged"
    off = await client.put(
        "/v1/ae/roles", json=_roles_body(2, insights={**INSIGHTS, "enabled": False})
    )
    assert off.json()["insights_task"] == {"ok": True, "action": "deleted"}
    assert ("DELETE", "/v1/scheduled-tasks/task_1", None) in world.server.writes()
    world.server.calls.clear()
    # Off to off lists the rows, finds none and writes nothing.
    world.server.rows = []
    still = await client.put(
        "/v1/ae/roles", json=_roles_body(3, insights={**INSIGHTS, "enabled": False})
    )
    assert still.json()["insights_task"] == {"ok": True, "action": "none"}
    assert world.server.writes() == []


async def test_a_failed_delete_is_retried_by_the_next_save_with_insights_off(
    client: httpx.AsyncClient, world: World
) -> None:
    await client.put("/v1/ae/roles", json=_roles_body(insights=INSIGHTS))
    world.server.rows = [_row(id="task_1")]
    world.server.fail_deletes = 1
    off = {**INSIGHTS, "enabled": False}
    first = await client.put("/v1/ae/roles", json=_roles_body(1, insights=off))
    assert first.status_code == 200 and first.json()["insights_task"]["ok"] is False
    second = await client.put("/v1/ae/roles", json=_roles_body(2, insights=off))
    assert second.json()["insights_task"] == {"ok": True, "action": "deleted"}


async def test_a_task_failure_never_fails_the_save(
    client: httpx.AsyncClient, world: World
) -> None:
    world.server.agent = False
    saved = await client.put("/v1/ae/roles", json=_roles_body(insights=INSIGHTS))
    assert saved.status_code == 200
    task = saved.json()["insights_task"]
    assert task["ok"] is False and "preset-reviewer" in task["error"]
    assert (await client.get("/v1/ae/roles")).json()["revision"] == 1


# -- the reviewer's two calls


async def test_the_read_checks_the_session_in_order(
    client: httpx.AsyncClient, world: World
) -> None:
    missing = await client.post("/v1/ae/sessions/conv_none/preset-insights", json={})
    assert missing.status_code == 404
    world.agents["conv_plain"] = "steward"
    plain = await client.post("/v1/ae/sessions/conv_plain/preset-insights", json={})
    assert plain.status_code == 403 and plain.json()["error"]["code"] == "not_reviewer"
    world.may_write = False
    sid = world.reviewer()
    foreign = await client.post(f"/v1/ae/sessions/{sid}/preset-insights", json={})
    assert foreign.status_code == 403 and foreign.json()["error"]["code"] != "not_reviewer"
    world.may_write = True
    extra = await client.post(f"/v1/ae/sessions/{sid}/preset-insights", json={"x": 1})
    assert extra.status_code == 422


async def test_the_read_answers_the_owner_own_scope_and_admin_all(
    client: httpx.AsyncClient, world: World, ae_admins: set[str]
) -> None:
    await _enable(client, REID)
    await _enable(client, STEVE)
    sid = world.reviewer(owner="reid@example.com")
    read = await client.post(f"/v1/ae/sessions/{sid}/preset-insights", json={}, headers=REID)
    body = read.json()
    assert read.status_code == 200 and body["due"] is True and body["scope"] == "admin"
    assert sorted(body["presets"]["users"]) == ["reid@example.com", "steve@example.com"]
    # An own-scope setting, or a non-admin owner, reads only their own presets.
    await _enable(client, REID, scope="own")
    own = (
        await client.post(f"/v1/ae/sessions/{sid}/preset-insights", json={}, headers=REID)
    ).json()
    assert own["due"] is True and own["scope"] == "own"
    assert list(own["presets"]["users"]) == ["reid@example.com"]
    sid2 = world.reviewer("conv_steve", owner="steve@example.com")
    steve = await client.post(f"/v1/ae/sessions/{sid2}/preset-insights", json={}, headers=STEVE)
    assert steve.json()["scope"] == "own"
    assert list(steve.json()["presets"]["users"]) == ["steve@example.com"]


async def test_a_caller_who_is_not_the_owner_is_never_admin(
    client: httpx.AsyncClient, world: World, ae_admins: set[str]
) -> None:
    await _enable(client, STEVE)
    sid = world.reviewer(owner="steve@example.com")
    read = await client.post(f"/v1/ae/sessions/{sid}/preset-insights", json={}, headers=REID)
    assert read.json()["scope"] == "own"


async def test_submit_stores_renames_and_refuses_a_second_call(
    client: httpx.AsyncClient, world: World
) -> None:
    await _enable(client, {})
    sid = world.reviewer()
    ok = await client.post(f"/v1/ae/sessions/{sid}/preset-suggestions", json=_submit_body(_own()))
    assert ok.status_code == 200, ok.text
    answer = ok.json()
    assert answer["ok"] is True and answer["count"] == 1 and answer["review_id"].startswith("rv_")
    assert world.titles[sid].startswith("Preset review ")
    again = await client.post(
        f"/v1/ae/sessions/{sid}/preset-suggestions", json=_submit_body(_own())
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_submitted"
    listed = (await client.get("/v1/ae/preset-suggestions")).json()
    assert [s["title"] for s in listed["suggestions"]] == ["Plan on Sonnet"]
    assert listed["suggestions"][0]["can_apply"] is True
    assert "before" not in listed["suggestions"][0]


async def test_submit_validation_and_auth(client: httpx.AsyncClient, world: World) -> None:
    await _enable(client, {})
    sid = world.reviewer()
    url = f"/v1/ae/sessions/{sid}/preset-suggestions"
    assert (await client.post(url, json={"summary": "x", "suggestions": [{}]})).status_code == 422
    bad_path = _own(changes=[{"path": "title", "value": "x"}])
    assert (await client.post(url, json=_submit_body(bad_path))).status_code == 422
    unknown = await client.post(url, json=_submit_body(_own(preset_id="nope")))
    assert unknown.status_code == 422 and "suggestions[0]" in unknown.json()["error"]["message"]
    world.agents["conv_x"] = "steward"
    other = await client.post("/v1/ae/sessions/conv_x/preset-suggestions", json=_submit_body())
    assert other.status_code == 403 and other.json()["error"]["code"] == "not_reviewer"
    assert (
        await client.post("/v1/ae/sessions/none/preset-suggestions", json=_submit_body())
    ).status_code == 404


# -- the suggestion actions


async def _filed(
    client: httpx.AsyncClient, world: World, headers: dict[str, str], sid: str = "conv_rev"
) -> dict:
    await _enable(client, headers)
    world.reviewer(sid, owner=headers.get("X-Forwarded-Email"))
    starter = {"kind": "starter", "title": "Raise timeout", "why": "w", "saving": "20 min"}
    response = await client.post(
        f"/v1/ae/sessions/{sid}/preset-suggestions",
        json=_submit_body(_own(), starter),
        headers=headers,
    )
    assert response.status_code == 200, response.text
    listed = (await client.get("/v1/ae/preset-suggestions", headers=headers)).json()
    return {s["kind"]: s for s in listed["suggestions"] if s["review_session"] == sid}


async def test_apply_then_undo_through_the_routes(client: httpx.AsyncClient, world: World) -> None:
    sugs = await _filed(client, world, {})
    sid = sugs["own"]["id"]
    applied = await client.post(f"/v1/ae/preset-suggestions/{sid}/apply", json={})
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["suggestion"]["status"] == "applied" and body["suggestion"]["can_undo"] is True
    mine = next(p for p in body["roles"]["presets"] if p["id"] == "mine")
    assert mine["roles"]["planner"]["model"] == role_prefs.SONNET
    assert body["roles"]["insights"]["enabled"] is True  # the setting survives the apply
    undone = await client.post(f"/v1/ae/preset-suggestions/{sid}/undo", json={})
    assert undone.json()["suggestion"]["status"] == "undone"
    again = await client.post(f"/v1/ae/preset-suggestions/{sid}/undo", json={})
    assert again.status_code == 409


async def test_apply_after_the_preset_changed_is_a_409(
    client: httpx.AsyncClient, world: World
) -> None:
    sugs = await _filed(client, world, {})
    state = (await client.get("/v1/ae/roles")).json()
    state.pop("starters")
    state["presets"][0]["description"] = "edited by hand"
    assert (await client.put("/v1/ae/roles", json=state)).status_code == 200
    refused = await client.post(f"/v1/ae/preset-suggestions/{sugs['own']['id']}/apply", json={})
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "preset_changed"
    assert "Dismiss it or edit the preset yourself" in refused.json()["error"]["message"]


async def test_the_actions_need_the_right_user(
    client: httpx.AsyncClient, world: World, ae_admins: set[str]
) -> None:
    sugs = await _filed(client, world, STEVE)
    own_id, starter_id = sugs["own"]["id"], sugs["starter"]["id"]
    wrong = await client.post(f"/v1/ae/preset-suggestions/{own_id}/apply", json={}, headers=REID)
    assert wrong.status_code == 403 and wrong.json()["error"]["code"] == "not_owner"
    undo = await client.post(f"/v1/ae/preset-suggestions/{own_id}/undo", json={}, headers=REID)
    assert undo.status_code == 403
    # The admin sees everyone's with all=true; Steve may not.
    everyone = await client.get("/v1/ae/preset-suggestions?all=true", headers=REID)
    assert len(everyone.json()["suggestions"]) == 2
    assert everyone.json()["suggestions"][0]["can_apply"] is False
    denied = await client.get("/v1/ae/preset-suggestions?all=true", headers=STEVE)
    assert denied.status_code == 403
    # Reid has none of his own.
    assert (await client.get("/v1/ae/preset-suggestions", headers=REID)).json()[
        "suggestions"
    ] == []
    # A non-owner non-admin cannot dismiss; the admin can.
    nope = await client.post(
        f"/v1/ae/preset-suggestions/{starter_id}/dismiss",
        json={},
        headers={"X-Forwarded-Email": "x@example.com"},
    )
    assert nope.status_code == 403
    gone = await client.post(
        f"/v1/ae/preset-suggestions/{starter_id}/dismiss", json={}, headers=REID
    )
    assert gone.status_code == 200 and gone.json()["suggestion"]["status"] == "dismissed"
    again = await client.post(
        f"/v1/ae/preset-suggestions/{starter_id}/dismiss", json={}, headers=STEVE
    )
    assert again.status_code == 409
    missing = await client.post("/v1/ae/preset-suggestions/sg_x/dismiss", json={}, headers=REID)
    assert missing.status_code == 404


async def test_the_issue_route_is_admin_only_and_posts_to_the_private_repo(
    client: httpx.AsyncClient,
    world: World,
    ae_admins: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sugs = await _filed(client, world, STEVE)
    sid = sugs["starter"]["id"]
    url = f"/v1/ae/preset-suggestions/{sid}/issue"
    assert (await client.post(url, json={}, headers=STEVE)).status_code == 403
    monkeypatch.delenv("AE_GITHUB_TOKEN", raising=False)
    off = await client.post(url, json={}, headers=REID)
    assert off.status_code == 503 and off.json()["error"]["code"] == "github_off"
    monkeypatch.setenv("AE_GITHUB_TOKEN", "gh-test")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    monkeypatch.delenv("AE_PRESET_ISSUE_REPO", raising=False)
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://api.github.com/repos/analyticendeavors/omnigent-ae").respond(
            200, json={"private": True}
        )
        route = mock.post("https://api.github.com/repos/analyticendeavors/omnigent-ae/issues")
        route.respond(
            201, json={"html_url": "https://github.com/analyticendeavors/omnigent-ae/issues/9"}
        )
        ok = await client.post(url, json={}, headers=REID)
        sent = json.loads(route.calls[0].request.content)
    assert ok.status_code == 200, ok.text
    assert ok.json()["suggestion"]["status"] == "issue_opened"
    assert ok.json()["suggestion"]["issue_url"].endswith("/issues/9")
    assert sent["title"] == "Starter preset suggestion: Raise timeout"
    lines = sent["body"].splitlines()
    assert lines[0] == "```" and lines[1] == "Preset: several starters"
    assert lines[2] == "Why: w" and lines[3] == "Expected saving: 20 min"
    assert lines[4] == "Changes:" and lines[5] == "none (no field)" and lines[6] == "```"
    assert lines[7].startswith("From: preset review ") and "session conv_rev" in lines[7]
    # A second try on the now-issued suggestion is refused; an own one is too.
    assert (await client.post(url, json={}, headers=REID)).status_code == 409
    own_url = f"/v1/ae/preset-suggestions/{sugs['own']['id']}/issue"
    assert (await client.post(own_url, json={}, headers=REID)).status_code == 409


async def test_the_issue_route_reports_a_github_failure_and_a_custom_repo(
    client: httpx.AsyncClient,
    world: World,
    ae_admins: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sugs = await _filed(client, world, REID)
    url = f"/v1/ae/preset-suggestions/{sugs['starter']['id']}/issue"
    monkeypatch.setenv("AE_GITHUB_TOKEN", "gh-test")
    monkeypatch.setenv("AE_PRESET_ISSUE_REPO", "acme/private")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    with respx.mock() as mock:
        mock.get("https://api.github.com/repos/acme/private").respond(200, json={"private": True})
        mock.post("https://api.github.com/repos/acme/private/issues").respond(
            403, json={"message": "no"}
        )
        failed = await client.post(url, json={}, headers=REID)
    assert failed.status_code == 502 and failed.json()["error"]["code"] == "github_failed"
    still = (await client.get("/v1/ae/preset-suggestions", headers=REID)).json()
    assert {s["status"] for s in still["suggestions"] if s["kind"] == "starter"} == {"open"}


async def test_create_issue_returns_the_html_url_and_sends_no_labels() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json={"html_url": "https://github.com/a/b/issues/1"})

    settings = github.GitHubSettings(token="t")
    async with github.GitHubClient(settings, transport=httpx.MockTransport(handler)) as gh:
        assert await gh.create_issue("a/b", "T", "B") == "https://github.com/a/b/issues/1"
    assert seen == [{"title": "T", "body": "B"}]
    for status in (403, 404):
        transport = httpx.MockTransport(lambda _r, s=status: httpx.Response(s, json={}))
        async with github.GitHubClient(settings, transport=transport) as gh:
            with pytest.raises(github.GitHubError, match="Issues: write") as err:
                await gh.create_issue("a/b", "T", "B")
        assert "Pull requests" not in str(err.value)
    for status, body in ((422, {"message": "bad"}), (201, {})):
        transport = httpx.MockTransport(lambda _r, s=status, b=body: httpx.Response(s, json=b))
        async with github.GitHubClient(settings, transport=transport) as gh:
            with pytest.raises(github.GitHubError):
                await gh.create_issue("a/b", "T", "B")


# -- run now and status


async def test_run_now_needs_insights_then_starts_the_task(
    client: httpx.AsyncClient, world: World
) -> None:
    off = await client.post("/v1/ae/preset-review/run", json={})
    assert off.status_code == 409 and off.json()["error"]["code"] == "insights_off"
    await _enable(client, {})
    world.server.rows = [_row(id="task_1")]
    world.server.calls.clear()
    started = await client.post("/v1/ae/preset-review/run", json={})
    assert started.status_code == 202 and started.json() == {"started": True}
    assert ("POST", "/v1/scheduled-tasks/task_1/run", None) in world.server.calls
    # The flag makes the reviewer's next read due.
    assert preset_suggestions.store().peek_forced("local", __import__("time").time())


async def test_run_now_answers_409_while_a_review_is_running(
    client: httpx.AsyncClient, world: World
) -> None:
    await _enable(client, {})
    world.server.rows = [_row(id="task_1")]
    world.run_status = 409
    busy = await client.post("/v1/ae/preset-review/run", json={})
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "review_running"
    assert not preset_suggestions.store().peek_forced("local", __import__("time").time())
    world.run_status = 500
    assert (await client.post("/v1/ae/preset-review/run", json={})).status_code == 502
    world.server.agent = False
    world.run_status = 202
    assert (await client.post("/v1/ae/preset-review/run", json={})).status_code == 502


async def test_status_reports_the_task_the_reviewer_and_the_next_review(
    client: httpx.AsyncClient, world: World
) -> None:
    quiet = (await client.get("/v1/ae/preset-review")).json()
    assert quiet["enabled"] is False and quiet["task"] is None and quiet["open"] == 0
    await _enable(client, {})
    world.server.rows = [
        _row(id="task_1", next_run_at="2026-10-26T14:00:00+00:00", state="active")
    ]
    sid = world.reviewer()
    await client.post(f"/v1/ae/sessions/{sid}/preset-suggestions", json=_submit_body(_own()))
    status = (await client.get("/v1/ae/preset-review")).json()
    assert status["enabled"] is True and status["scope"] == "all" and status["open"] == 1
    assert status["task"] == {
        "id": "task_1",
        "state": "active",
        "next_run_at": "2026-10-26T14:00:00+00:00",
    }
    assert status["reviewer"]["preset_title"] == "Mine"
    assert status["reviewer"]["model"].startswith("claude-")
    # Reviewed just now: the next weekly fire is inside 13 days, so it moves out in 7-day steps.
    assert status["last_review"]
    assert status["next_review"] is not None and status["next_review"] > "2026-10-26T14:00:00"


# -- review round 1 fixes (2026-10-24)


async def test_apply_and_undo_sync_the_task_and_show_the_changes(
    client: httpx.AsyncClient, world: World
) -> None:
    sugs = await _filed(client, world, {})
    sid = sugs["own"]["id"]
    assert sugs["own"]["changes_text"].startswith("Planner: ")
    world.server.rows = [_row(id="task_1")]
    world.server.calls.clear()
    applied = await client.post(f"/v1/ae/preset-suggestions/{sid}/apply", json={})
    assert applied.status_code == 200, applied.text
    assert applied.json()["insights_task"]["ok"] is True
    assert any(c[:2] == ("GET", "/v1/scheduled-tasks") for c in world.server.calls)
    world.server.calls.clear()
    undone = await client.post(f"/v1/ae/preset-suggestions/{sid}/undo", json={})
    assert undone.json()["insights_task"]["ok"] is True
    assert any(c[:2] == ("GET", "/v1/scheduled-tasks") for c in world.server.calls)


def _starter_issue_mocks(mock: respx.MockRouter, *, private: bool = True) -> respx.Route:
    mock.get("https://api.github.com/repos/analyticendeavors/omnigent-ae").respond(
        200, json={"private": private}
    )
    return mock.post("https://api.github.com/repos/analyticendeavors/omnigent-ae/issues")


async def test_a_public_issue_repo_is_refused_and_the_answer_is_cached(
    client: httpx.AsyncClient,
    world: World,
    ae_admins: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sugs = await _filed(client, world, REID)
    url = f"/v1/ae/preset-suggestions/{sugs['starter']['id']}/issue"
    monkeypatch.setenv("AE_GITHUB_TOKEN", "gh-test")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    monkeypatch.delenv("AE_PRESET_ISSUE_REPO", raising=False)
    with respx.mock(assert_all_called=False) as mock:
        create = _starter_issue_mocks(mock, private=False)
        refused = await client.post(url, json={}, headers=REID)
        again = await client.post(url, json={}, headers=REID)
        gets = [c for c in mock.calls if c.request.method == "GET"]
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "repo_not_private"
    assert again.status_code == 409 and len(gets) == 1  # cached for 10 minutes
    assert not create.called
    listed = (await client.get("/v1/ae/preset-suggestions", headers=REID)).json()
    assert {s["status"] for s in listed["suggestions"] if s["kind"] == "starter"} == {"open"}


async def test_a_second_click_during_an_open_issue_is_409_in_progress(
    client: httpx.AsyncClient,
    world: World,
    ae_admins: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sugs = await _filed(client, world, REID)
    sid = sugs["starter"]["id"]
    url = f"/v1/ae/preset-suggestions/{sid}/issue"
    monkeypatch.setenv("AE_GITHUB_TOKEN", "gh-test")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    monkeypatch.delenv("AE_PRESET_ISSUE_REPO", raising=False)
    preset_suggestions.store().reserve_issue(sid)
    second = await client.post(url, json={}, headers=REID)
    assert second.status_code == 409 and second.json()["error"]["code"] == "issue_in_progress"
    preset_suggestions.store().release_issue(sid)


async def test_the_issue_body_fences_the_reviewer_text_and_defuses_mentions() -> None:
    record = {
        "preset_title": "Mine @team",
        "why": "ask @reid ```` now [x](http://e.com) ![i](http://e.com/i.png) org/repo#7",
        "saving": "20 min [link](http://e.com)",
        "created_at": "2026-10-24T00:00:00Z",
        "review_session": "conv_rev",
        "changes": [{"path": "rules.helpers", "value": "``` @bob"}],
    }
    body = router_module._issue_body(record)
    assert "@reid" not in body and "@\u200breid" in body and "@\u200bteam" in body
    lines = body.splitlines()
    fence = lines[0]
    assert set(fence) == {"`"} and len(fence) >= 5  # longer than the ```` in the why
    assert lines.count(fence) == 2
    closing = lines.index(fence, 1)
    inside = "\n".join(lines[1:closing])
    # Links, images and issue references sit inside the fence, so none of them renders.
    for text in ("[x](http://e.com)", "![i](http://e.com/i.png)", "org/repo#7", "[link]("):
        assert text in inside
    assert "[x](" not in "\n".join(lines[closing + 1 :])
    record["why"] = "w" * 30000
    assert len(router_module._issue_body(record)) <= router_module.ISSUE_BODY_MAX


async def test_a_failed_issue_call_puts_the_suggestion_back_to_open(
    client: httpx.AsyncClient,
    world: World,
    ae_admins: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sugs = await _filed(client, world, REID)
    url = f"/v1/ae/preset-suggestions/{sugs['starter']['id']}/issue"
    monkeypatch.setenv("AE_GITHUB_TOKEN", "gh-test")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    monkeypatch.delenv("AE_PRESET_ISSUE_REPO", raising=False)
    with respx.mock() as mock:
        create = _starter_issue_mocks(mock)
        create.respond(500, json={"message": "down"})
        failed = await client.post(url, json={}, headers=REID)
    assert failed.status_code == 502
    listed = (await client.get("/v1/ae/preset-suggestions", headers=REID)).json()
    assert {s["status"] for s in listed["suggestions"] if s["kind"] == "starter"} == {"open"}


async def test_run_now_answers_502_on_a_transport_error_and_clears_its_own_flag(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(client, {})
    world.server.rows = [_row(id="task_1")]

    @asynccontextmanager
    async def broken(_request: Any) -> AsyncIterator[httpx.AsyncClient]:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/run"):
                raise httpx.ConnectError("refused")
            return world.server(request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://box"
        ) as http:
            yield http

    monkeypatch.setattr(router_module, "_loopback_client", broken)
    failed = await client.post("/v1/ae/preset-review/run", json={})
    assert failed.status_code == 502 and failed.json()["error"]["code"] == "run_failed"
    assert not preset_suggestions.store().peek_forced("local", __import__("time").time())
    # A flag somebody else set stays when this request's start fails.
    preset_suggestions.store().force("local", __import__("time").time())
    await client.post("/v1/ae/preset-review/run", json={})
    assert preset_suggestions.store().peek_forced("local", __import__("time").time())


async def test_run_now_on_a_paused_task_says_so(client: httpx.AsyncClient, world: World) -> None:
    await _enable(client, {})
    world.server.rows = [_row(id="task_1", state="paused")]
    paused = await client.post("/v1/ae/preset-review/run", json={})
    assert paused.status_code == 409 and paused.json()["error"]["code"] == "task_paused"
    assert not preset_suggestions.store().peek_forced("local", __import__("time").time())


async def test_a_flag_store_failure_in_the_read_is_a_json_error(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(client, {})
    sid = world.reviewer()

    def boom(*_a: Any, **_k: Any) -> bool:
        raise preset_suggestions.SuggestionError(503, "suggestions_not_saved", "disk")

    monkeypatch.setattr(preset_suggestions.SuggestionStore, "take_forced", boom)
    out = await client.post(f"/v1/ae/sessions/{sid}/preset-insights", json={})
    assert out.status_code == 503 and out.json()["error"]["code"] == "suggestions_not_saved"


async def test_a_bad_zone_name_still_stores_and_renames(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(client, {})
    sid = world.reviewer()
    monkeypatch.setenv("AE_TASK_STORE_TZ", "Not/AZone")
    done = await client.post(
        f"/v1/ae/sessions/{sid}/preset-suggestions", json=_submit_body(_own())
    )
    assert done.status_code == 200, done.text
    assert world.titles[sid].startswith("Preset review ")


# -- round 2 fixes


async def test_a_stale_issue_reservation_counts_as_free(
    client: httpx.AsyncClient,
    world: World,
    ae_admins: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sugs = await _filed(client, world, REID)
    sid = sugs["starter"]["id"]
    url = f"/v1/ae/preset-suggestions/{sid}/issue"
    monkeypatch.setenv("AE_GITHUB_TOKEN", "gh-test")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    monkeypatch.delenv("AE_PRESET_ISSUE_REPO", raising=False)
    store = preset_suggestions.store()
    held = datetime.now(UTC) - timedelta(minutes=5)
    store.reserve_issue(sid, now=held)
    with respx.mock() as mock:
        create = _starter_issue_mocks(mock)
        create.respond(201, json={"html_url": "https://github.com/a/b/issues/3"})
        done = await client.post(url, json={}, headers=REID)
    assert done.status_code == 200, done.text
    assert done.json()["suggestion"]["status"] == "issue_opened"


async def test_a_mark_failure_after_the_issue_is_retried_and_logs_the_url(
    client: httpx.AsyncClient,
    world: World,
    ae_admins: set[str],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sugs = await _filed(client, world, REID)
    sid = sugs["starter"]["id"]
    url = f"/v1/ae/preset-suggestions/{sid}/issue"
    monkeypatch.setenv("AE_GITHUB_TOKEN", "gh-test")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    monkeypatch.delenv("AE_PRESET_ISSUE_REPO", raising=False)
    real = preset_suggestions.SuggestionStore.mark_issue
    fails = {"left": 1}

    def flaky(self: Any, *args: Any, **kwargs: Any) -> Any:
        if fails["left"] > 0:
            fails["left"] -= 1
            raise preset_suggestions.SuggestionError(503, "suggestions_not_saved", "disk")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(preset_suggestions.SuggestionStore, "mark_issue", flaky)
    issue = "https://github.com/a/b/issues/4"
    with respx.mock() as mock, caplog.at_level("WARNING"):
        _starter_issue_mocks(mock).respond(201, json={"html_url": issue})
        retried = await client.post(url, json={}, headers=REID)
        assert retried.status_code == 200, retried.text
        assert retried.json()["suggestion"]["issue_url"] == issue
    assert issue in caplog.text
    # Two failures in a row: the answer is the failure, and the URL is in the log.
    sugs2 = await _filed(client, world, REID, sid="conv_rev2")
    fails["left"] = 2
    caplog.clear()
    with respx.mock(assert_all_called=False) as mock, caplog.at_level("WARNING"):
        _starter_issue_mocks(mock).respond(201, json={"html_url": issue})
        failed = await client.post(
            f"/v1/ae/preset-suggestions/{sugs2['starter']['id']}/issue", json={}, headers=REID
        )
    assert failed.status_code == 503
    assert issue in caplog.text and "could not be recorded" in caplog.text


async def test_run_now_rereads_the_roles_inside_the_lock(
    client: httpx.AsyncClient, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enable(client, {})
    world.server.rows = [_row(id="task_1")]
    real = role_prefs.RolePrefsStore.get
    world.server.calls.clear()

    def switched_off(self: Any, user: str) -> Any:
        # A save that switched insights off landed while the request waited for the lock.
        return real(self, user).model_copy(update={"insights": None})

    monkeypatch.setattr(role_prefs.RolePrefsStore, "get", switched_off)
    refused = await client.post("/v1/ae/preset-review/run", json={})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "insights_off"
    assert world.server.calls == []


async def test_submit_needs_a_due_read_for_that_session(
    client: httpx.AsyncClient, world: World
) -> None:
    await _enable(client, {})
    sid = world.reviewer(due=False)
    url = f"/v1/ae/sessions/{sid}/preset-suggestions"
    refused = await client.post(url, json=_submit_body(_own()))
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "no_due_read"
    # A read that answers due=false does not unlock it; one that answers due=true does.
    read = await client.post(f"/v1/ae/sessions/{sid}/preset-insights", json={})
    assert read.json()["due"] is True
    assert (await client.post(url, json=_submit_body(_own()))).status_code == 200
    # Not due: the last review is fresh, so a second reviewer session gets no unlock.
    sid2 = world.reviewer("conv_rev2", due=False)
    not_due = await client.post(f"/v1/ae/sessions/{sid2}/preset-insights", json={})
    assert not_due.json()["due"] is False
    again = await client.post(f"/v1/ae/sessions/{sid2}/preset-suggestions", json=_submit_body())
    assert again.status_code == 409 and again.json()["error"]["code"] == "no_due_read"
