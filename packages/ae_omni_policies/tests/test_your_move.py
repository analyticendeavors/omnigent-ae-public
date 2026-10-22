"""``GET /v1/ae/your-move`` on the real app: upstream's stores, respx for task-store and GitHub."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.runtime import get_conversation_store
from tests.server.helpers import create_test_session

from ae_omni_policies import github, labels, your_move

TASKS = "https://tasks.test"
GH = "https://api.github.com"
LOT = json.loads((Path(__file__).parent / "fixtures" / "lot.json").read_text(encoding="utf-8"))
TZ = "America/Los_Angeles"
TOKEN = "github_pat_test_value_never_logged"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AE_TASK_STORE_TZ", TZ)
    monkeypatch.delenv("AE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("AE_GITHUB_REPOS", raising=False)
    monkeypatch.delenv("AE_GITHUB_LOGIN", raising=False)
    monkeypatch.delenv("AE_STEWARD_AGENT", raising=False)
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    your_move.reset_cache()
    yield
    your_move.reset_cache()


def _day(offset: int) -> str:
    return (datetime.now(ZoneInfo(TZ)).date() + timedelta(days=offset)).isoformat()


def _lot_tasks() -> dict[str, Any]:
    """The fixture lot with resurface dates yesterday, today and next week."""
    tasks = copy.deepcopy(LOT["tasks"])
    rows = tasks["tasks"]
    rows[0]["due_at"] = f"{_day(-1)}T00:00:00Z"
    rows[1]["due_at"] = f"{_day(7)}T00:00:00Z"
    today = copy.deepcopy(rows[1])
    today.update(id="01M0TODAY00000000000000000", title="due-today", due_at=f"{_day(0)}T00:00:00Z")
    rows.insert(1, today)
    return tasks


async def _session(
    client: httpx.AsyncClient, title: str, seed: dict[str, str] | None = None, **kwargs: Any
) -> str:
    snapshot = await create_test_session(client, title=title, labels=seed, **kwargs)
    return snapshot["id"]


def _mock_store(mock: respx.MockRouter) -> None:
    mock.get(f"{TASKS}/api/buckets").respond(200, json=LOT["buckets"])
    mock.get(f"{TASKS}/api/tasks", params={"status": "open"}).respond(200, json=_lot_tasks())


def _pull(number: int, author: str, reviewers: list[str], sha: str, **extra: Any) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "html_url": f"https://github.com/analyticendeavors/omnigent-ae/pull/{number}",
        "user": {"login": author},
        "requested_reviewers": [{"login": login} for login in reviewers],
        "draft": False,
        "updated_at": f"2026-09-2{number % 10}T10:00:00Z",
        "head": {"sha": sha},
        **extra,
    }


async def test_sessions_that_need_reid_are_grouped_by_kind(client: httpx.AsyncClient) -> None:
    store = get_conversation_store()
    gate = await _session(
        client,
        "Gate session",
        {labels.STATUS: "working", labels.GATE: "open", labels.FORK: "EUR formatting"},
    )
    awaiting = await _session(client, "Needs approval", {labels.STATUS: "working"})
    store.set_pending_elicitation_count(awaiting, 2)
    blocked = await _session(client, "Blocked one", {labels.STATUS: "blocked"})
    review = await _session(client, "Review one", {labels.STATUS: "review", labels.NEXT: "merge"})
    await _session(client, "Plain working", {labels.STATUS: "working"})
    await _session(client, "Answered gate", {labels.STATUS: "working", labels.GATE: "answered"})
    await _session(client, "Parked", {labels.STATUS: "parked"})

    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        response = await client.get("/v1/ae/your-move")
    assert response.status_code == 200, response.text
    part = response.json()["sessions"]
    assert part["ok"] is True
    assert [(row["id"], row["kind"]) for row in part["items"]] == [
        (gate, "gate"),
        (awaiting, "awaiting"),
        (blocked, "blocked"),
        (review, "review"),
    ]
    assert part["counts"] == {"gate": 1, "awaiting": 1, "blocked": 1, "review": 1}
    first = part["items"][0]
    assert first["fork"] == "EUR formatting"
    assert first["title"] == "Gate session"
    assert part["items"][1]["pending"] == 2
    assert part["items"][3]["next"] == "merge"


async def test_a_gate_with_an_approval_lists_once_with_both_reasons(
    client: httpx.AsyncClient,
) -> None:
    both = await _session(client, "Both", {labels.STATUS: "working", labels.GATE: "open"})
    get_conversation_store().set_pending_elicitation_count(both, 1)
    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        body = (await client.get("/v1/ae/your-move")).json()
    rows = body["sessions"]["items"]
    assert len(rows) == 1
    assert rows[0]["kinds"] == ["gate", "awaiting"]


async def test_lot_keeps_today_and_overdue_only(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        body = (await client.get("/v1/ae/your-move")).json()
    lot = body["lot"]
    assert lot["ok"] is True
    assert body["today"] == _day(0)
    assert body["timezone"] == TZ
    assert [(task["title"], task["overdue"]) for task in lot["items"]] == [
        ("contoso-vendor-category-check", True),
        ("due-today", False),
    ]
    assert lot["items"][0]["next"].startswith("Ask Alex in Teams:")


async def test_one_failing_source_does_not_blank_the_others(client: httpx.AsyncClient) -> None:
    review = await _session(client, "Review one", {labels.STATUS: "review"})
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{TASKS}/api/buckets").respond(302, headers={"location": "https://login/"})
        response = await client.get("/v1/ae/your-move")
    assert response.status_code == 200
    body = response.json()
    assert body["lot"]["ok"] is False
    assert body["lot"]["error"]["code"] == "task_store_unavailable"
    assert "Cloudflare Access redirected" in body["lot"]["error"]["message"]
    assert [row["id"] for row in body["sessions"]["items"]] == [review]
    assert body["steward"]["ok"] is True


async def test_a_source_that_raises_is_reported_in_its_slot(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_user: str | None = None) -> dict[str, Any]:
        raise RuntimeError("secret-bearing message")

    monkeypatch.setattr(your_move, "steward_source", boom)
    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        body = (await client.get("/v1/ae/your-move")).json()
    assert body["steward"] == {
        "ok": False,
        "error": {"code": "source_failed", "message": "steward could not be read (RuntimeError)."},
    }
    assert body["lot"]["ok"] is True


async def test_unconfigured_task_store_is_a_state(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_CF_ACCESS_CLIENT_SECRET")
    body = (await client.get("/v1/ae/your-move")).json()
    assert body["lot"]["ok"] is False
    assert body["lot"]["error"]["code"] == "task_store_not_configured"


async def test_steward_brief_is_the_newest_run_and_its_last_reply(
    client: httpx.AsyncClient,
) -> None:
    store = get_conversation_store()
    await _session(client, "AE steward (old)", {labels.STATUS: "reference"}, name="steward")
    newest = await _session(client, "AE steward", {labels.STATUS: "reference"}, name="steward")
    await _session(client, "Other agent", {labels.STATUS: "reference"}, name="shutdown")

    def message(role: str, text: str) -> NewConversationItem:
        return NewConversationItem(
            type="message",
            response_id="resp_1",
            data=MessageData(
                role=role,
                content=[
                    {"type": "output_text" if role == "assistant" else "input_text", "text": text}
                ],
                agent="steward" if role == "assistant" else None,
            ),
        )

    store.append(
        newest,
        [
            message("user", "Write the brief."),
            message("assistant", "Working on it."),
            message("assistant", "Brief: two lot items resurface today."),
        ],
    )
    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        body = (await client.get("/v1/ae/your-move")).json()
    steward = body["steward"]
    assert steward["ok"] is True
    assert steward["agent"] == "steward"
    brief = steward["brief"]
    assert brief["session_id"] == newest
    assert brief["text"] == "Brief: two lot items resurface today."
    assert brief["truncated"] is False


async def test_no_steward_run_says_how_it_starts(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        body = (await client.get("/v1/ae/your-move")).json()
    assert body["steward"]["brief"] is None
    assert "schedules-seed" in body["steward"]["note"]


async def test_pulls_without_a_token_say_how_to_turn_them_on(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        github_route = mock.get(url__startswith=GH)
        body = (await client.get("/v1/ae/your-move")).json()
    pulls = body["pulls"]
    assert pulls["ok"] is True
    assert pulls["configured"] is False
    assert pulls["items"] == []
    assert "AE_GITHUB_TOKEN" in pulls["note"]
    assert pulls["repos"] == list(github.DEFAULT_REPOS)
    assert not github_route.called


async def test_pulls_by_or_awaiting_reid_with_ci_state(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AE_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("AE_GITHUB_REPOS", "analyticendeavors/omnigent-ae, not a repo")
    repo = "/repos/analyticendeavors/omnigent-ae"
    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        listed = mock.get(f"{GH}{repo}/pulls").respond(
            200,
            json=[
                _pull(21, "ae-reid-havens", [], "a" * 40),
                _pull(22, "someone", ["ae-reid-havens"], "b" * 40),
                _pull(23, "someone", ["another"], "c" * 40),
                _pull(24, "AE-Reid-Havens", [], "d" * 40, draft=True),
            ],
        )
        mock.get(f"{GH}{repo}/commits/{'a' * 40}/check-runs").respond(
            200, json={"check_runs": [{"status": "completed", "conclusion": "success"}]}
        )
        mock.get(f"{GH}{repo}/commits/{'b' * 40}/check-runs").respond(
            200,
            json={
                "check_runs": [
                    {"status": "completed", "conclusion": "success"},
                    {"status": "completed", "conclusion": "failure"},
                ]
            },
        )
        mock.get(f"{GH}{repo}/commits/{'d' * 40}/check-runs").respond(
            200, json={"check_runs": [{"status": "in_progress", "conclusion": None}]}
        )
        body = (await client.get("/v1/ae/your-move")).json()
        sent = listed.calls[0].request
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent.url.params["state"] == "open"
    pulls = body["pulls"]
    assert pulls["configured"] is True
    assert pulls["repos"] == ["analyticendeavors/omnigent-ae"]
    assert [(p["number"], p["why"], p["ci"], p["draft"]) for p in pulls["items"]] == [
        (24, ["author"], "pending", True),
        (22, ["review_requested"], "failing", False),
        (21, ["author"], "passing", False),
    ]
    assert pulls["items"][0]["url"].endswith("/pull/24")
    assert body["total"] == 3 + len(body["lot"]["items"])
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps(body)


async def test_one_repo_failing_keeps_the_other(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_GITHUB_TOKEN", TOKEN)
    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        mock.get(f"{GH}/repos/analyticendeavors/omnigent-ae/pulls").respond(
            200, json=[_pull(5, "ae-reid-havens", [], "e" * 40)]
        )
        mock.get(
            f"{GH}/repos/analyticendeavors/omnigent-ae/commits/{'e' * 40}/check-runs"
        ).respond(500)
        mock.get(f"{GH}/repos/analyticendeavors/agents-and-automation/pulls").respond(
            401, json={"message": "Bad credentials"}
        )
        body = (await client.get("/v1/ae/your-move")).json()
    pulls = body["pulls"]
    assert [(p["number"], p["ci"]) for p in pulls["items"]] == [(5, "unknown")]
    assert len(pulls["errors"]) == 1
    error = pulls["errors"][0]
    assert error["repo"] == "analyticendeavors/agents-and-automation"
    assert "401 Bad credentials" in error["message"]
    assert "invalid or expired" in error["message"]
    assert TOKEN not in json.dumps(body)


async def test_the_answer_is_cached_until_fresh_is_asked(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        first = (await client.get("/v1/ae/your-move")).json()
        await _session(client, "New review", {labels.STATUS: "review"})
        cached = (await client.get("/v1/ae/your-move")).json()
        fresh = (await client.get("/v1/ae/your-move?fresh=true")).json()
    assert first["cached"] is False
    assert cached["cached"] is True
    assert cached["sessions"]["items"] == first["sessions"]["items"] == []
    assert [row["title"] for row in fresh["sessions"]["items"]] == ["New review"]


@pytest.mark.parametrize(
    ("runs", "state"),
    [
        ([], "none"),
        ([{"status": "completed", "conclusion": "success"}], "passing"),
        ([{"status": "completed", "conclusion": "skipped"}], "passing"),
        ([{"status": "queued", "conclusion": None}], "pending"),
        (
            [
                {"status": "queued", "conclusion": None},
                {"status": "completed", "conclusion": "timed_out"},
            ],
            "failing",
        ),
    ],
)
def test_ci_state(runs: list[dict[str, Any]], state: str) -> None:
    assert github.ci_state(runs) == state


def test_settings_default_and_filter() -> None:
    settings = github.GitHubSettings.from_env({"AE_GITHUB_REPOS": "../etc, a/b"})
    assert settings.repos == ("a/b",)
    assert settings.configured is False
    assert github.GitHubSettings.from_env({}).login == "ae-reid-havens"
    assert github.GitHubSettings.from_env({}).api_url == "https://api.github.com"
    local = github.GitHubSettings.from_env({"AE_GITHUB_API_URL": "http://127.0.0.1:8788/"})
    assert local.api_url == "http://127.0.0.1:8788"


async def test_pulls_keep_their_own_cache_through_fresh(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("AE_GITHUB_REPOS", "analyticendeavors/omnigent-ae")
    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        listed = mock.get(f"{GH}/repos/analyticendeavors/omnigent-ae/pulls").respond(200, json=[])
        await client.get("/v1/ae/your-move?fresh=true")
        await client.get("/v1/ae/your-move?fresh=true")
    assert listed.call_count == 1


async def test_each_user_sees_only_their_own_sessions(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    reid = {"X-Forwarded-Email": "reid@example.com"}
    steve = {"X-Forwarded-Email": "steve@example.com"}
    client.headers.update(reid)
    mine = await _session(client, "Reid review", {labels.STATUS: "review"})
    await _session(client, "Reid steward", {labels.STATUS: "reference"}, name="steward")
    client.headers.update(steve)
    theirs = await _session(client, "Steve review", {labels.STATUS: "blocked"})
    del client.headers["X-Forwarded-Email"]

    with respx.mock(assert_all_called=False) as mock:
        _mock_store(mock)
        # Back to back, inside the cache's few seconds: one caller's answer
        # is never served to the other.
        for_reid = (await client.get("/v1/ae/your-move", headers=reid)).json()
        for_steve = (await client.get("/v1/ae/your-move", headers=steve)).json()
        again = (await client.get("/v1/ae/your-move", headers=reid)).json()
    assert [row["id"] for row in for_reid["sessions"]["items"]] == [mine]
    assert [row["id"] for row in for_steve["sessions"]["items"]] == [theirs]
    assert again["cached"] is True
    assert [row["id"] for row in again["sessions"]["items"]] == [mine]
    assert for_reid["steward"]["brief"]["title"] == "Reid steward"
    assert for_steve["steward"]["brief"] is None
