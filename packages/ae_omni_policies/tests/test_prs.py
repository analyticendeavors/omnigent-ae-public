"""The PR card routes on the real app: ``/v1/ae/prs``, one card, and the Merge button.

GitHub and the sessions' tracked-PR route are respx mocks: GitHub at
``https://api.github.com``, and the loopback at ``AE_SELF_URL`` so the scan's
``GET /v1/sessions/{id}/resources/github`` (upstream's per-session tracking,
served by a runner or host no test has) answers from here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from tests.server.helpers import create_test_session

from ae_omni_policies import auth, labels, pr_compliance, prs, your_move

GH = "https://api.github.com"
SELF = "https://self.test"
TOKEN = "github_pat_test_value_never_returned"
REPO = "analyticendeavors/omnigent-ae"
SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
NEW_SHA = "ffffffffffffffffffffffffffffffffffffffff"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AE_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("AE_SELF_URL", SELF)
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    monkeypatch.setattr(prs, "MERGEABLE_RETRY_S", 0.0)
    prs.reset_cache()
    your_move.reset_cache()
    yield
    prs.reset_cache()
    your_move.reset_cache()


def pr_url(number: int, repo: str = REPO) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def pull(number: int, **extra: Any) -> dict[str, Any]:
    """A pull as ``GET /repos/{repo}/pulls/{n}`` answers it: open, clean, one commit."""
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "Review summary\n\nChanges in this PR.",
        "additions": 12,
        "deletions": 3,
        "changed_files": 2,
        "node_id": f"PR_node_{number}",
        "html_url": pr_url(number),
        "state": "open",
        "merged": False,
        "merged_at": None,
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "ae-owner"},
        "head": {"ref": f"claude/branch-{number}", "sha": SHA},
        "base": {"ref": "main"},
        "requested_reviewers": [],
        "requested_teams": [],
        "updated_at": f"2026-09-2{number % 10}T10:00:00Z",
        **extra,
    }


def mock_card(
    mock: respx.MockRouter,
    number: int,
    *,
    pull_extra: dict[str, Any] | None = None,
    runs: list[dict[str, Any]] | None = None,
    statuses: list[dict[str, Any]] | None = None,
    reviews: list[dict[str, Any]] | None = None,
    repo_settings: dict[str, Any] | None = None,
    repo: str = REPO,
) -> dict[str, respx.Route]:
    """Mock the five reads a card makes; returns the routes to assert call counts on."""
    body = pull(number, **(pull_extra or {}))
    sha = body["head"]["sha"]
    if runs is None:
        runs = [{"status": "completed", "conclusion": "success"}] * 3
    return {
        "pull": mock.get(f"{GH}/repos/{repo}/pulls/{number}").respond(200, json=body),
        "reviews": mock.get(f"{GH}/repos/{repo}/pulls/{number}/reviews").respond(
            200, json=reviews or []
        ),
        "runs": mock.get(f"{GH}/repos/{repo}/commits/{sha}/check-runs").respond(
            200, json={"total_count": len(runs), "check_runs": runs}
        ),
        "statuses": mock.get(f"{GH}/repos/{repo}/commits/{sha}/status").respond(
            200, json={"state": "success", "statuses": statuses or []}
        ),
        "repo": mock.get(f"{GH}/repos/{repo}").respond(
            200,
            json=repo_settings
            if repo_settings is not None
            else {"allow_squash_merge": True, "allow_merge_commit": True},
        ),
    }


def mock_tracked(mock: respx.MockRouter, session_id: str, urls: list[str]) -> respx.Route:
    return mock.get(f"{SELF}/v1/sessions/{session_id}/resources/github").respond(
        200,
        json={
            "object": "session.github.info",
            "tracking_available": True,
            "prs": [{"url": url, "relationship": "created", "source": "gh"} for url in urls],
        },
    )


async def session(client: httpx.AsyncClient, title: str, status: str | None = None) -> str:
    seed = {labels.STATUS: status} if status else None
    snapshot = await create_test_session(client, title=title, labels=seed)
    return snapshot["id"]


# ── GET /v1/ae/prs ──────────────────────────────────────────────────────


async def test_every_open_pr_a_session_tracks_is_a_card(client: httpx.AsyncClient) -> None:
    first = await session(client, "Merge button", "working")
    second = await session(client, "Also on PR 5", "review")
    offline = await session(client, "Runner asleep", "parked")
    with respx.mock(assert_all_called=False) as mock:
        mock_tracked(mock, first, [pr_url(5), pr_url(6), "https://gitlab.com/a/b/pull/1"])
        mock_tracked(mock, second, [pr_url(5)])
        mock.get(f"{SELF}/v1/sessions/{offline}/resources/github").respond(503, json={})
        mock_card(mock, 5)
        mock_card(mock, 6, pull_extra={"state": "closed", "merged": True, "merged_at": "x"})
        response = await client.get("/v1/ae/prs")
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["configured"] is True
    assert answer["sessions_scanned"] == 3
    # Only the open one; the merged PR 6 and the non-GitHub URL are not cards.
    [card] = answer["items"]
    assert card["number"] == 5
    assert card["body"] == "Review summary\n\nChanges in this PR."
    assert (card["additions"], card["deletions"], card["changed_files"]) == (12, 3, 2)
    assert card["repo"] == REPO
    assert card["head_ref"] == "claude/branch-5"
    assert card["ci"] == {"state": "passing", "passing": 3, "failing": 0, "pending": 0, "total": 3}
    assert card["mergeable"]["state"] == "clean"
    assert card["merge"] == {
        "methods": ["squash", "merge"],
        "default_method": "squash",
        "recommended_method": "squash",
        "recommended_reason": "Squash lands this change as one commit on main.",
        "allowed": True,
        "reason": None,
    }
    assert sorted(ref["title"] for ref in card["sessions"]) == ["Also on PR 5", "Merge button"]
    assert {ref["status"] for ref in card["sessions"]} == {"working", "review"}
    assert answer["errors"] == [{"session_id": offline, "message": "session not read: 503"}]
    assert TOKEN not in response.text


async def test_a_pr_github_cannot_read_is_an_error_beside_the_others(
    client: httpx.AsyncClient,
) -> None:
    sid = await session(client, "Two PRs", "working")
    with respx.mock(assert_all_called=False) as mock:
        mock_tracked(mock, sid, [pr_url(5), pr_url(7, "analyticendeavors/private")])
        mock_card(mock, 5)
        mock.get(f"{GH}/repos/analyticendeavors/private/pulls/7").respond(
            404, json={"message": "Not Found"}
        )
        answer = (await client.get("/v1/ae/prs")).json()
    assert [card["number"] for card in answer["items"]] == [5]
    [error] = answer["errors"]
    assert error["url"] == pr_url(7, "analyticendeavors/private")
    assert "404 Not Found" in error["message"]


async def test_the_scan_and_the_cards_are_cached(client: httpx.AsyncClient) -> None:
    sid = await session(client, "Cached", "working")
    with respx.mock(assert_all_called=False) as mock:
        tracked = mock_tracked(mock, sid, [pr_url(5)])
        routes = mock_card(mock, 5)
        await client.get("/v1/ae/prs")
        await client.get("/v1/ae/prs")
        assert tracked.call_count == 1
        assert routes["pull"].call_count == 1
        # fresh re-reads which sessions track what, but not GitHub.
        await client.get("/v1/ae/prs?fresh=true")
        assert tracked.call_count == 2
        assert routes["pull"].call_count == 1


async def _until(check: Any, timeout: float = 2.0) -> None:
    """Yield to the loop until *check()* holds; background reads finish this way."""
    for _ in range(int(timeout / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the background read never finished")


# Stale-while-revalidate (2026-09-26): the Dashboard waited on the cold scan
# (2321 ms on the box) on every open after a minute away.


async def test_a_stale_scan_is_served_at_once_and_one_background_scan_replaces_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = await session(client, "Stale scan", "working")
    gate = asyncio.Event()
    real = prs.tracked_by_session
    scans = 0

    async def held_open(rows: Any, fetch: Any) -> Any:
        nonlocal scans
        scans += 1
        if scans > 1:
            await gate.wait()
        return await real(rows, fetch)

    monkeypatch.setattr(prs, "tracked_by_session", held_open)
    with respx.mock(assert_all_called=False) as mock:
        tracked = mock_tracked(mock, sid, [pr_url(5)])
        mock_card(mock, 5)
        mock_card(mock, 6)
        first = (await client.get("/v1/ae/prs")).json()
        assert [card["number"] for card in first["items"]] == [5]
        monkeypatch.setattr(prs._scan, "ttl_s", 0.0)
        tracked.respond(
            200, json={"prs": [{"url": pr_url(5)}, {"url": pr_url(6)}], "tracking_available": True}
        )
        # Three polls while the refresh is held open: each answers from the
        # held scan, and they share the one background scan.
        for _ in range(3):
            answer = (await asyncio.wait_for(client.get("/v1/ae/prs"), 1.0)).json()
            assert [card["number"] for card in answer["items"]] == [5]
        assert scans == 2
        gate.set()
        await _until(lambda: not prs._scan.refreshing(None))
        assert tracked.call_count == 2
        monkeypatch.setattr(prs._scan, "ttl_s", 60.0)
        after = (await client.get("/v1/ae/prs")).json()
    assert sorted(card["number"] for card in after["items"]) == [5, 6]


async def test_fresh_awaits_a_new_scan(client: httpx.AsyncClient) -> None:
    sid = await session(client, "Fresh scan", "working")
    with respx.mock(assert_all_called=False) as mock:
        tracked = mock_tracked(mock, sid, [pr_url(5)])
        mock_card(mock, 5)
        mock_card(mock, 6)
        await client.get("/v1/ae/prs")
        tracked.respond(200, json={"prs": [{"url": pr_url(5)}, {"url": pr_url(6)}]})
        answer = (await client.get("/v1/ae/prs?fresh=true")).json()
    assert tracked.call_count == 2
    assert sorted(card["number"] for card in answer["items"]) == [5, 6]


async def test_a_scan_that_fails_keeps_the_last_cards_and_says_so(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = await session(client, "Scan fails", "working")
    with respx.mock(assert_all_called=False) as mock:
        mock_tracked(mock, sid, [pr_url(5)])
        mock_card(mock, 5)
        await client.get("/v1/ae/prs")
        monkeypatch.setattr(prs._scan, "ttl_s", 0.0)

        def broken(**_kwargs: Any) -> list[Any]:
            raise RuntimeError("store gone")

        monkeypatch.setattr(prs.sessions, "list_top_level", broken)
        await client.get("/v1/ae/prs")
        await _until(lambda: prs._scan.last_error(None) is not None)
        answer = (await client.get("/v1/ae/prs")).json()
    assert [card["number"] for card in answer["items"]] == [5]
    said = "which sessions track what was not re-read (RuntimeError); showing the last scan"
    assert {"message": said} in answer["errors"]


async def test_a_stale_card_is_served_and_re_read_in_the_background(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = await session(client, "Stale card", "working")
    with respx.mock(assert_all_called=False) as mock:
        mock_tracked(mock, sid, [pr_url(5)])
        routes = mock_card(mock, 5)
        await client.get("/v1/ae/prs")
        monkeypatch.setattr(prs, "CARD_TTL_S", 0.0)
        routes["pull"].respond(200, json=pull(5, title="Renamed"))
        held = (await client.get("/v1/ae/prs")).json()
        assert held["items"][0]["title"] == "PR 5"
        await _until(lambda: routes["pull"].call_count == 2 and not prs._card_refreshes)
        monkeypatch.setattr(prs, "CARD_TTL_S", 60.0)
        after = (await client.get("/v1/ae/prs")).json()
        assert after["items"][0]["title"] == "Renamed"
        # A failed re-read keeps the card and names the failure beside it.
        monkeypatch.setattr(prs, "CARD_TTL_S", 0.0)
        routes["pull"].respond(502, json={"message": "Bad gateway"})
        await client.get("/v1/ae/prs")
        await _until(lambda: not prs._card_refreshes)
        kept = (await client.get("/v1/ae/prs")).json()
    assert kept["items"][0]["title"] == "Renamed"
    [error] = kept["errors"]
    assert error["url"] == pr_url(5)
    assert error["message"].startswith("showing the last read; re-reading failed:")


async def test_a_card_read_that_started_before_a_merge_stores_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()

    async def slow_open_card(client: Any, repo: str, number: int) -> dict[str, Any]:
        await gate.wait()
        return {"repo": repo, "number": number, "state": "open"}

    monkeypatch.setattr(prs, "_read_card_now", slow_open_card)
    settings = prs.github.GitHubSettings.from_env()
    prs._refresh_card_later(settings, REPO, 5)
    await asyncio.sleep(0)
    prs.forget(REPO, 5)
    gate.set()
    await _until(lambda: not prs._card_refreshes)
    assert (REPO, 5) not in prs._cards


async def test_without_a_token_the_list_says_how_to_turn_it_on(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_GITHUB_TOKEN")
    with respx.mock(assert_all_called=False) as mock:
        response = await client.get("/v1/ae/prs")
        assert not mock.calls
    answer = response.json()
    assert answer["configured"] is False
    assert "AE_GITHUB_TOKEN" in answer["note"]
    card = await client.get(f"/v1/ae/prs/{REPO}/5")
    assert card.status_code == 503
    assert card.json()["error"]["code"] == "github_not_configured"
    merge = await client.post(f"/v1/ae/prs/{REPO}/5/merge", json={"sha": SHA})
    assert merge.status_code == 503


# ── GET /v1/ae/prs/{owner}/{repo}/{n} ──────────────────────────────────


async def test_one_card_with_review_state_and_the_repo_merge_method(
    client: httpx.AsyncClient,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(
            mock,
            8,
            pull_extra={"requested_reviewers": [{"login": "someone"}]},
            reviews=[
                {"user": {"login": "a"}, "state": "CHANGES_REQUESTED"},
                {"user": {"login": "a"}, "state": "APPROVED"},
                {"user": {"login": "b"}, "state": "COMMENTED"},
            ],
            repo_settings={"allow_squash_merge": False, "allow_merge_commit": True},
        )
        response = await client.get(f"/v1/ae/prs/{REPO}/8")
    assert response.status_code == 200, response.text
    card = response.json()
    assert card["review"] == {
        "state": "approved",
        "approvals": 1,
        "changes_requested": 0,
        "requested": ["someone"],
    }
    assert card["merge"]["methods"] == ["merge"]
    assert card["merge"]["default_method"] == "merge"
    assert card["merge"]["recommended_method"] == "merge"
    assert card["merge"]["recommended_reason"] == "The only method this repo allows."
    assert card["merge"]["allowed"] is True


async def test_a_promotion_card_recommends_and_defaults_to_a_merge_commit(
    client: httpx.AsyncClient,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 9, pull_extra={"head": {"ref": "dev", "sha": SHA}})
        response = await client.get(f"/v1/ae/prs/{REPO}/9")
    assert response.status_code == 200, response.text
    merge = response.json()["merge"]
    assert merge["methods"] == ["squash", "merge"]
    assert merge["default_method"] == "merge"
    assert merge["recommended_method"] == "merge"
    assert merge["recommended_reason"] == (
        "Merge commit keeps dev and main in step; a squash would make the next promotion conflict."
    )


GATES: list[tuple[str, dict[str, Any], str]] = [
    (
        "ci failing",
        {"runs": [{"status": "completed", "conclusion": "failure"}] * 2},
        "CI is failing: 2 of 2 checks.",
    ),
    (
        "ci running",
        {
            "runs": [
                {"status": "completed", "conclusion": "success"},
                {"status": "in_progress", "conclusion": None},
            ]
        },
        "CI is still running: 1 of 2 checks passed, 1 to go.",
    ),
    (
        "a failed commit status",
        {"statuses": [{"state": "error", "context": "deploy"}]},
        "CI is failing: 1 of 4 checks.",
    ),
    (
        "conflicted",
        {"pull_extra": {"mergeable": False, "mergeable_state": "dirty"}},
        "Conflicts with the base branch.",
    ),
    (
        "blocked",
        {"pull_extra": {"mergeable_state": "blocked"}},
        "Blocked by branch protection (a required review or check).",
    ),
    (
        "behind",
        {"pull_extra": {"mergeable_state": "behind"}},
        "Behind the base branch, which protection requires to be merged in.",
    ),
    (
        "draft",
        {"pull_extra": {"draft": True, "mergeable_state": "draft"}},
        "Draft: review details and mark ready for review.",
    ),
    (
        "still computing",
        {"pull_extra": {"mergeable": None, "mergeable_state": "unknown"}},
        "GitHub is still checking mergeability.",
    ),
    (
        "changes requested",
        {"reviews": [{"user": {"login": "x"}, "state": "CHANGES_REQUESTED"}]},
        "A reviewer requested changes.",
    ),
]


@pytest.mark.parametrize(("case", "setup", "reason"), GATES, ids=[g[0] for g in GATES])
async def test_the_merge_button_is_off_with_the_reason(
    client: httpx.AsyncClient, case: str, setup: dict[str, Any], reason: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 9, **setup)
        card = (await client.get(f"/v1/ae/prs/{REPO}/9")).json()
    assert card["merge"]["allowed"] is False, case
    assert card["merge"]["reason"] == reason


async def test_a_card_is_cached_until_fresh(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_card(mock, 5)
        await client.get(f"/v1/ae/prs/{REPO}/5")
        await client.get(f"/v1/ae/prs/{REPO}/5")
        assert routes["pull"].call_count == 1
        await client.get(f"/v1/ae/prs/{REPO}/5?fresh=true")
        assert routes["pull"].call_count == 2
        # The repo's merge settings keep their own, longer cache.
        assert routes["repo"].call_count == 1


async def test_a_check_read_that_fails_keeps_the_button_off(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5)
        mock.get(f"{GH}/repos/{REPO}/commits/{SHA}/check-runs").respond(403, json={})
        card = (await client.get(f"/v1/ae/prs/{REPO}/5")).json()
    assert card["ci"]["state"] == "unknown"
    assert card["merge"]["allowed"] is False
    assert card["merge"]["reason"] == "CI could not be read; refresh to try again."


async def test_a_bad_repo_name_never_reaches_github(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        response = await client.get("/v1/ae/prs/bad$owner/repo/5")
        assert not mock.calls
    assert response.status_code == 400


# ── POST /v1/ae/prs/{owner}/{repo}/{n}/merge ───────────────────────────


async def test_ready_marks_draft_and_returns_refreshed_card(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_card(mock, 5, pull_extra={"draft": True, "mergeable_state": "draft"})
        routes["pull"].mock(
            side_effect=[
                httpx.Response(200, json=pull(5, draft=True, mergeable_state="draft")),
                httpx.Response(200, json=pull(5)),
            ]
        )
        ready = mock.post(f"{GH}/graphql").respond(
            200,
            json={"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}},
        )
        merge = mock.put(f"{GH}/repos/{REPO}/pulls/5/merge").respond(200, json={})
        response = await client.post(f"/v1/ae/prs/{REPO}/5/ready", json={"sha": SHA})
        assert not merge.called
    assert response.status_code == 200, response.text
    assert routes["pull"].call_count == 2
    assert response.json()["draft"] is False
    assert response.json()["head_sha"] == SHA
    assert response.json()["body"] == "Review summary\n\nChanges in this PR."
    payload = json.loads(ready.calls.last.request.content)
    assert "markPullRequestReadyForReview" in payload["query"]
    assert payload["variables"] == {"pullRequestId": "PR_node_5"}
    assert ready.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in response.text
    assert TOKEN not in caplog.text


async def test_ready_is_idempotent_for_open_non_draft(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_card(mock, 5)
        ready = mock.post(f"{GH}/graphql").respond(200, json={})
        response = await client.post(f"/v1/ae/prs/{REPO}/5/ready", json={"sha": SHA})
        assert not ready.called
    assert response.status_code == 200, response.text
    assert response.json()["draft"] is False
    assert routes["pull"].call_count == 2


async def test_ready_unknown_pull_reports_github_error(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GH}/repos/{REPO}/pulls/5").respond(404, json={"message": "Not Found"})
        ready = mock.post(f"{GH}/graphql").respond(200, json={})
        response = await client.post(f"/v1/ae/prs/{REPO}/5/ready", json={"sha": SHA})
        assert not ready.called
    assert response.status_code == 502
    assert response.json()["error"]["code"] == prs.CODE_GITHUB_FAILED
    assert TOKEN not in response.text


async def test_ready_refuses_stale_head(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5, pull_extra={"draft": True, "head": {"ref": "b", "sha": NEW_SHA}})
        ready = mock.post(f"{GH}/graphql").respond(200, json={})
        response = await client.post(f"/v1/ae/prs/{REPO}/5/ready", json={"sha": SHA})
        assert not ready.called
    assert response.status_code == 409
    assert response.json()["error"]["code"] == prs.CODE_HEAD_MOVED
    assert response.json()["error"]["card"]["head_sha"] == NEW_SHA


async def test_ready_refuses_closed_pull(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5, pull_extra={"draft": True, "state": "closed"})
        ready = mock.post(f"{GH}/graphql").respond(200, json={})
        response = await client.post(f"/v1/ae/prs/{REPO}/5/ready", json={"sha": SHA})
        assert not ready.called
    assert response.status_code == 409
    assert response.json()["error"]["code"] == prs.CODE_READY_BLOCKED


async def test_ready_requires_credentials(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_GITHUB_TOKEN")
    with respx.mock(assert_all_called=False) as mock:
        response = await client.post(f"/v1/ae/prs/{REPO}/5/ready", json={"sha": SHA})
        assert not mock.calls
    assert response.status_code == 503
    assert response.json()["error"]["code"] == prs.CODE_NOT_CONFIGURED


@pytest.mark.parametrize("failure", ["http", "graphql", "malformed", "still_draft"])
async def test_ready_reports_upstream_failure(client: httpx.AsyncClient, failure: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5, pull_extra={"draft": True})
        route = mock.post(f"{GH}/graphql")
        if failure == "http":
            route.respond(403, json={"message": "Resource not accessible"})
        elif failure == "graphql":
            route.respond(200, json={"errors": [{"message": "Resource not accessible"}]})
        elif failure == "malformed":
            route.respond(200, json={"data": {}})
        else:
            route.respond(
                200,
                json={
                    "data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": True}}}
                },
            )
        merge = mock.put(f"{GH}/repos/{REPO}/pulls/5/merge").respond(200, json={})
        response = await client.post(f"/v1/ae/prs/{REPO}/5/ready", json={"sha": SHA})
        assert not merge.called
    assert response.status_code == 502
    assert response.json()["error"]["code"] == prs.CODE_READY_REFUSED
    assert TOKEN not in response.text


async def test_ready_has_merge_routes_safety_checks(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        assert (await client.get(f"/v1/ae/prs/{REPO}/5/ready")).status_code in (404, 405)
        form = await client.post(
            f"/v1/ae/prs/{REPO}/5/ready",
            content=f"sha={SHA}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        foreign = await client.post(
            f"/v1/ae/prs/{REPO}/5/ready",
            json={"sha": SHA},
            headers={"Origin": "https://evil.example"},
        )
        missing = await client.post(f"/v1/ae/prs/{REPO}/5/ready", json={})
        invalid_repo = await client.post("/v1/ae/prs/bad$owner/repo/5/ready", json={"sha": SHA})
        assert not mock.calls
    statuses = (
        form.status_code,
        foreign.status_code,
        missing.status_code,
        invalid_repo.status_code,
    )
    assert statuses == (415, 403, 422, 400)


async def test_ready_needs_an_authenticated_user(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    with respx.mock(assert_all_called=False) as mock:
        response = await client.post(f"/v1/ae/prs/{REPO}/5/ready", json={"sha": SHA})
        assert not mock.calls
    assert response.status_code == 401


async def test_merge_pins_the_sha_and_offers_review_for_the_session(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    sid = await session(client, "Shipping it", "working")
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_card(mock, 5)
        merge = mock.put(f"{GH}/repos/{REPO}/pulls/5/merge").respond(
            200,
            json={"sha": "m3rg3d", "merged": True, "message": "Pull Request successfully merged"},
        )
        await client.get(f"/v1/ae/prs/{REPO}/5")
        response = await client.post(
            f"/v1/ae/prs/{REPO}/5/merge", json={"sha": SHA, "session_id": sid}
        )
        # The gate is re-read fresh, not from the card the GET cached.
        assert routes["pull"].call_count == 2
    assert response.status_code == 200, response.text
    assert json.loads(merge.calls.last.request.content) == {"merge_method": "squash", "sha": SHA}
    assert merge.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    answer = response.json()
    assert answer == {
        "merged": True,
        "repo": REPO,
        "number": 5,
        "method": "squash",
        "sha": "m3rg3d",
        "message": "Pull Request successfully merged",
        "session": {"id": sid, "status": "working", "offer_review": True},
    }
    assert TOKEN not in response.text
    assert TOKEN not in caplog.text
    assert f"merged {REPO}#5 (squash)" in caplog.text


async def test_merge_with_a_chosen_method_and_a_session_already_in_review(
    client: httpx.AsyncClient,
) -> None:
    sid = await session(client, "Done already", "review")
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5)
        merge = mock.put(f"{GH}/repos/{REPO}/pulls/5/merge").respond(200, json={"sha": "m"})
        response = await client.post(
            f"/v1/ae/prs/{REPO}/5/merge", json={"sha": SHA, "method": "merge", "session_id": sid}
        )
    assert response.status_code == 200, response.text
    assert json.loads(merge.calls.last.request.content)["merge_method"] == "merge"
    assert response.json()["session"]["offer_review"] is False


async def test_merge_is_refused_when_the_gate_is_shut(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5, runs=[{"status": "completed", "conclusion": "failure"}])
        merge = mock.put(f"{GH}/repos/{REPO}/pulls/5/merge").respond(200, json={})
        response = await client.post(f"/v1/ae/prs/{REPO}/5/merge", json={"sha": SHA})
        assert not merge.called
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "merge_blocked"
    assert error["message"] == "CI is failing: 1 of 1 checks."
    assert error["card"]["ci"]["state"] == "failing"


async def test_merge_is_refused_when_the_head_moved(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5, pull_extra={"head": {"ref": "b", "sha": NEW_SHA}})
        merge = mock.put(f"{GH}/repos/{REPO}/pulls/5/merge").respond(200, json={})
        response = await client.post(f"/v1/ae/prs/{REPO}/5/merge", json={"sha": SHA})
        assert not merge.called
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "merge_head_moved"
    assert error["card"]["head_sha"] == NEW_SHA


async def test_a_method_the_repo_disallows_is_a_400(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5, repo_settings={"allow_squash_merge": True, "allow_merge_commit": False})
        response = await client.post(
            f"/v1/ae/prs/{REPO}/5/merge", json={"sha": SHA, "method": "merge"}
        )
    assert response.status_code == 400
    assert "allows squash" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("status", "code", "fragment"),
    [
        (405, 409, "Base branch was modified"),
        (409, 409, "Head branch was modified"),
        (403, 502, "Contents and Pull requests: read and write"),
    ],
)
async def test_githubs_refusal_is_reported_with_its_sentence(
    client: httpx.AsyncClient, status: int, code: int, fragment: str
) -> None:
    messages = {405: "Base branch was modified", 409: "Head branch was modified", 403: "Nope"}
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5)
        mock.put(f"{GH}/repos/{REPO}/pulls/5/merge").respond(
            status, json={"message": messages[status]}
        )
        response = await client.post(f"/v1/ae/prs/{REPO}/5/merge", json={"sha": SHA})
    assert response.status_code == code
    error = response.json()["error"]
    assert error["code"] == "merge_refused"
    assert fragment in error["message"]
    assert TOKEN not in response.text


async def test_merge_is_post_json_only_and_origin_checked(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5)
        merge = mock.put(f"{GH}/repos/{REPO}/pulls/5/merge").respond(200, json={})
        # 405 from the router, or 404 once a built SPA's catch-all takes the GET.
        assert (await client.get(f"/v1/ae/prs/{REPO}/5/merge")).status_code in (404, 405)
        form = await client.post(
            f"/v1/ae/prs/{REPO}/5/merge",
            content=f"sha={SHA}",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert form.status_code == 415
        foreign = await client.post(
            f"/v1/ae/prs/{REPO}/5/merge",
            json={"sha": SHA},
            headers={"Origin": "https://evil.example"},
        )
        assert foreign.status_code == 403
        missing = await client.post(f"/v1/ae/prs/{REPO}/5/merge", json={})
        assert missing.status_code == 422
        assert not merge.called


async def test_merge_needs_an_authenticated_user(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    with respx.mock(assert_all_called=False) as mock:
        merge = mock.put(f"{GH}/repos/{REPO}/pulls/5/merge").respond(200, json={})
        response = await client.post(f"/v1/ae/prs/{REPO}/5/merge", json={"sha": SHA})
        assert not merge.called
    assert response.status_code == 401


@pytest.mark.parametrize("endpoint", ["merge", "ready", "update-branch"])
async def test_pull_request_writes_are_admin_only(
    client: httpx.AsyncClient, ae_admins: set[str], endpoint: str
) -> None:
    path = f"/v1/ae/prs/{REPO}/5/{endpoint}"
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="api.github.com").respond(404, json={"message": "Not Found"})
        refused = await client.post(
            path, json={"sha": SHA}, headers={"X-Forwarded-Email": "member@example.com"}
        )
        assert not mock.calls
        # An admin gets past the gate to GitHub (here a 404, so a 502).
        admitted = await client.post(
            path, json={"sha": SHA}, headers={"X-Forwarded-Email": "owner@example.com"}
        )
        assert mock.calls
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "admin_only"
    assert admitted.status_code == 502, admitted.text


# ── the pieces ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "parsed"),
    [
        (pr_url(12), (REPO, 12)),
        ("https://github.com/Owner/Repo/pull/3/", ("owner/repo", 3)),
        ("https://github.com/o/r/issues/3", None),
        ("http://github.com/o/r/pull/3", None),
        ("https://ghe.example/o/r/pull/3", None),
        ("https://github.com/../r/pull/3", None),
    ],
)
def test_parse_pull_url(url: str, parsed: tuple[str, int] | None) -> None:
    assert prs.parse_pull_url(url) == parsed


def test_ci_summary_counts_skipped_as_passing() -> None:
    runs = [
        {"status": "completed", "conclusion": "skipped"},
        {"status": "completed", "conclusion": "neutral"},
        {"status": "queued", "conclusion": None},
    ]
    assert prs.ci_summary(runs, [{"state": "success"}]) == {
        "state": "pending",
        "passing": 3,
        "failing": 0,
        "pending": 1,
        "total": 4,
    }
    assert prs.ci_summary([], [])["state"] == "none"


def test_a_dismissed_review_clears_a_change_request() -> None:
    reviews = [
        {"user": {"login": "a"}, "state": "CHANGES_REQUESTED"},
        {"user": {"login": "A"}, "state": "DISMISSED"},
    ]
    assert prs.review_summary(reviews, {})["state"] == "none"


def test_merge_methods_without_the_fields_offers_both() -> None:
    assert prs.merge_methods(None) == (["squash", "merge"], "squash")
    assert prs.merge_methods({"name": "r"}) == (["squash", "merge"], "squash")
    assert prs.merge_methods({"allow_squash_merge": False, "allow_merge_commit": False}) == (
        [],
        None,
    )


SQUASH_WHY = "Squash lands this change as one commit on {base}."
MERGE_WHY = (
    "Merge commit keeps {head} and {base} in step; "
    "a squash would make the next promotion conflict."
)
ONLY_WHY = "The only method this repo allows."


@pytest.mark.parametrize(
    ("methods", "head", "base", "expected"),
    [
        (["squash", "merge"], "claude/merge-method", "main", ("squash", SQUASH_WHY)),
        (["squash", "merge"], "dev", "main", ("merge", MERGE_WHY)),
        (["squash", "merge"], "develop", "main", ("merge", MERGE_WHY)),
        (["squash", "merge"], "master", "release", ("merge", MERGE_WHY)),
        (["squash", "merge"], "staging", "main", ("merge", MERGE_WHY)),
        (["squash", "merge"], "release/v1.2", "main", ("merge", MERGE_WHY)),
        (["squash", "merge"], "hotfix/login", "main", ("merge", MERGE_WHY)),
        (["squash", "merge"], "main", "dev", ("merge", MERGE_WHY)),
        # A name that only contains a long-lived one is still a feature branch.
        (["squash", "merge"], "feature/main-menu", "main", ("squash", SQUASH_WHY)),
        (["squash", "merge"], "dev-tools", "main", ("squash", SQUASH_WHY)),
        # Only squash allowed: a promotion still gets squash, the only choice.
        (["squash"], "dev", "main", ("squash", ONLY_WHY)),
        (["squash"], "claude/x", "main", ("squash", SQUASH_WHY)),
        # Only merge allowed: a feature branch gets merge, the only choice.
        (["merge"], "claude/x", "main", ("merge", ONLY_WHY)),
        (["merge"], "dev", "main", ("merge", MERGE_WHY)),
        ([], "dev", "main", (None, None)),
    ],
)
def test_recommend_merge_method(
    methods: list[str], head: str, base: str, expected: tuple[str | None, str | None]
) -> None:
    method, why = expected
    if why is not None:
        why = why.format(head=head, base=base)
    assert prs.recommend_merge_method(methods, head, base) == (method, why)


def test_recommend_merge_method_when_the_repo_hides_its_settings() -> None:
    # A token GitHub hides the fields from offers both, so the branch rule decides.
    methods, _ = prs.merge_methods({"name": "r"})
    assert prs.recommend_merge_method(methods, "claude/x", "main") == (
        "squash",
        SQUASH_WHY.format(base="main"),
    )
    assert prs.recommend_merge_method(methods, "dev", "main") == (
        "merge",
        MERGE_WHY.format(head="dev", base="main"),
    )


def test_recommend_merge_method_without_branch_names() -> None:
    assert prs.recommend_merge_method(["squash", "merge"], None, None) == (
        "squash",
        "Squash lands this change as one commit on the base branch.",
    )


@pytest.mark.parametrize("draft", [False, True])
async def test_update_branch_is_pinned_and_pending(client: httpx.AsyncClient, draft: bool) -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_card(mock, 5, pull_extra={"draft": draft, "mergeable_state": "behind"})
        card = (await client.get(f"/v1/ae/prs/{REPO}/5")).json()
        assert card["update_branch"] == {"allowed": True, "reason": None}
        write = mock.put(f"{GH}/repos/{REPO}/pulls/5/update-branch").respond(
            202, json={"message": "Updating pull request branch."}
        )
        comments = mock.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(200, json=[])
        await client.get(f"/v1/ae/prs/{REPO}/5/compliance")
        response = await client.post(f"/v1/ae/prs/{REPO}/5/update-branch", json={"sha": SHA})
        assert routes["pull"].call_count == 2  # Cached card never replaces preflight.
        assert write.call_count == 1
        assert json.loads(write.calls.last.request.content) == {"expected_head_sha": SHA}
        assert write.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
        assert (REPO.lower(), 5) not in prs._cards
        await client.get(f"/v1/ae/prs/{REPO}/5/compliance")
        assert comments.call_count == 2
    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "message": "Branch update accepted; GitHub is processing it.",
        "head_sha": SHA,
    }


@pytest.mark.parametrize(
    ("extra", "code", "reason"),
    [
        ({"state": "closed"}, prs.CODE_UPDATE_BLOCKED, "open"),
        ({"merged": True}, prs.CODE_UPDATE_BLOCKED, "open"),
        ({"mergeable_state": "dirty"}, prs.CODE_UPDATE_BLOCKED, "conflicts"),
        ({"mergeable": False, "draft": True}, prs.CODE_UPDATE_BLOCKED, "conflicts"),
        ({"mergeable_state": "unknown"}, prs.CODE_UPDATE_BLOCKED, "confirmed"),
        ({"mergeable_state": "clean"}, prs.CODE_UPDATE_BLOCKED, "confirmed"),
        ({"head": {"sha": NEW_SHA}}, prs.CODE_HEAD_MOVED, "New commits"),
    ],
)
async def test_update_branch_preflight_refuses_without_writing(
    client: httpx.AsyncClient, extra: dict[str, Any], code: str, reason: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5, pull_extra={"mergeable_state": "behind", **extra})
        write = mock.put(f"{GH}/repos/{REPO}/pulls/5/update-branch").respond(202, json={})
        response = await client.post(f"/v1/ae/prs/{REPO}/5/update-branch", json={"sha": SHA})
        assert not write.called
    assert response.status_code == 409
    assert response.json()["error"]["code"] == code
    assert reason in response.json()["error"]["message"]


@pytest.mark.parametrize("status", [403, 422, 409, 500])
async def test_update_branch_upstream_refusal(client: httpx.AsyncClient, status: int) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5, pull_extra={"mergeable_state": "behind"})
        write = mock.put(f"{GH}/repos/{REPO}/pulls/5/update-branch").respond(
            status, json={"message": "Cannot update this branch"}
        )
        response = await client.post(f"/v1/ae/prs/{REPO}/5/update-branch", json={"sha": SHA})
        assert write.call_count == 1
    assert response.status_code == (409 if status in (409, 422) else 502)
    assert response.json()["error"]["code"] == prs.CODE_UPDATE_REFUSED
    assert "Cannot update this branch" in response.json()["error"]["message"]
    if status == 403:
        assert "read and write" in response.json()["error"]["message"]
    assert TOKEN not in response.text


async def test_update_branch_detects_race_at_github(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_card(mock, 5, pull_extra={"head": {"sha": NEW_SHA}})
        routes["pull"].side_effect = [
            httpx.Response(200, json=pull(5, mergeable_state="behind")),
            httpx.Response(200, json=pull(5, head={"sha": NEW_SHA})),
        ]
        write = mock.put(f"{GH}/repos/{REPO}/pulls/5/update-branch").respond(
            422, json={"message": "Expected head sha didn't match current head ref."}
        )
        response = await client.post(f"/v1/ae/prs/{REPO}/5/update-branch", json={"sha": SHA})
        assert write.call_count == 1
    assert response.status_code == 409
    assert response.json()["error"]["code"] == prs.CODE_HEAD_MOVED
    assert response.json()["error"]["card"]["head_sha"] == NEW_SHA


async def test_update_timeout_invalidates_without_retry(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5, pull_extra={"mergeable_state": "behind"})
        await client.get(f"/v1/ae/prs/{REPO}/5")
        write = mock.put(f"{GH}/repos/{REPO}/pulls/5/update-branch").mock(
            side_effect=httpx.ReadTimeout("Timeout")
        )
        response = await client.post(f"/v1/ae/prs/{REPO}/5/update-branch", json={"sha": SHA})
        assert write.call_count == 1
        assert (REPO.lower(), 5) not in prs._cards
    assert response.status_code == 502
    assert "outcome is unknown" in response.json()["error"]["message"]


async def test_update_branch_security(client: httpx.AsyncClient) -> None:
    path = f"/v1/ae/prs/{REPO}/5/update-branch"
    with respx.mock(assert_all_called=False) as mock:
        assert (await client.get(path)).status_code in (404, 405)
        assert (await client.post(path, content="sha=x")).status_code == 415
        assert (
            await client.post(path, json={"sha": SHA}, headers={"Origin": "https://evil.test"})
        ).status_code == 403
        for body in ({}, {"sha": "invalid"}, {"sha": SHA, "extra": True}):
            assert (await client.post(path, json=body)).status_code == 422
        assert (
            await client.post("/v1/ae/prs/bad$owner/repo/5/update-branch", json={"sha": SHA})
        ).status_code == 400
        assert not mock.calls


@pytest.mark.parametrize("endpoint", ["update-branch", "compliance"])
async def test_review_routes_require_identity(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    with respx.mock(assert_all_called=False) as mock:
        path = f"/v1/ae/prs/{REPO}/5/{endpoint}"
        response = (
            await client.post(path, json={"sha": SHA})
            if endpoint == "update-branch"
            else await client.get(path)
        )
        assert not mock.calls
    assert response.status_code == 401


@pytest.mark.parametrize("draft", [True, False])
async def test_conflict_card_exposes_disabled_update(
    client: httpx.AsyncClient, draft: bool
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5, pull_extra={"draft": draft, "mergeable_state": "dirty"})
        card = (await client.get(f"/v1/ae/prs/{REPO}/5")).json()
    assert card["update_branch"]["allowed"] is False
    assert "conflicts" in card["update_branch"]["reason"]


def compliance_comment(**extra: Any) -> dict[str, Any]:
    return {
        "id": 42,
        "body": "## 🔍 AE Compliance Review\n\n**APPROVED**\n\nFull findings here.\n",
        "user": {"login": "github-actions[bot]", "type": "Bot"},
        "updated_at": "2026-10-17T15:00:00Z",
        "html_url": f"https://github.com/{REPO}/pull/5#issuecomment-42",
        **extra,
    }


@pytest.mark.parametrize(
    ("verdict_text", "verdict"),
    [
        ("**APPROVED**", "approved"),
        ("### BLOCKED", "blocked"),
        ("CHANGES REQUIRED: findings", "blocked"),
        ("**CHANGES REQUESTED**", "blocked"),
        ("Verdict: **APPROVED**", "approved"),
        ("NOT REVIEWED\n\nAPPROVED", "unknown"),
        ("Example: APPROVED", "unknown"),
        ("```\nAPPROVED\n```", "unknown"),
        ("> APPROVED", "unknown"),
        ("    APPROVED", "unknown"),
        ("APPROVEDISH", "unknown"),
    ],
)
async def test_compliance_verdict_and_full_body(
    client: httpx.AsyncClient, verdict_text: str, verdict: str
) -> None:
    body = f"## 🔍 AE Compliance Review\n\n{verdict_text}\n\nFull findings.\n"
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(
            200, json=[compliance_comment(body=body, html_url="javascript:alert(1)")]
        )
        response = await client.get(f"/v1/ae/prs/{REPO}/5/compliance?fresh=true")
    assert response.status_code == 200
    assert response.json()["report"] == {
        "body": body,
        "verdict": verdict,
        "url": f"https://github.com/{REPO}/pull/5#issuecomment-42",
        "updated_at": "2026-10-17T15:00:00Z",
        "reviewed_sha": None,
        "revision": "unknown",
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"user": {"login": "github-actions[bot]", "type": "User"}},
        {"user": {"login": "other[bot]", "type": "Bot"}},
        {"user": None},
        {"body": "Discussion mentioning AE Compliance Review\nAPPROVED"},
        {"body": "> ## AE Compliance Review\nAPPROVED"},
        {"body": "```\n## AE Compliance Review\nAPPROVED\n```"},
        {"body": "## AE Compliance Review spoof\nAPPROVED"},
        {"body": "    ## AE Compliance Review\nAPPROVED"},
    ],
)
async def test_compliance_ignores_spoofed_comments(
    client: httpx.AsyncClient, extra: dict[str, Any]
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(
            200, json=[compliance_comment(**extra)]
        )
        response = await client.get(f"/v1/ae/prs/{REPO}/5/compliance?fresh=true")
    assert response.status_code == 200
    assert response.json() == {"report": None}


@pytest.mark.parametrize(("marker_sha", "revision"), [(SHA, "current"), (NEW_SHA, "stale")])
async def test_compliance_revision_uses_explicit_marker_and_fresh_head(
    client: httpx.AsyncClient, marker_sha: str, revision: str
) -> None:
    body = (
        f"## AE Compliance Review\n\n<!-- ae-compliance-reviewed-sha: {marker_sha} -->\n"
        "## APPROVED\nDetails"
    )
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5, pull_extra={"head": {"sha": NEW_SHA}})
        await client.get(f"/v1/ae/prs/{REPO}/5")
        head = mock.get(f"{GH}/repos/{REPO}/pulls/5").respond(200, json=pull(5))
        mock.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(
            200, json=[compliance_comment(body=body)]
        )
        report = (await client.get(f"/v1/ae/prs/{REPO}/5/compliance?fresh=true")).json()["report"]
        assert head.called
    assert report["revision"] == revision
    assert report["reviewed_sha"] == marker_sha


async def test_compliance_reads_all_pages_and_chooses_latest_update(
    client: httpx.AsyncClient,
) -> None:
    old = compliance_comment(updated_at="2026-10-15T10:00:00Z")
    latest = compliance_comment(id=1, body="## AE Compliance Review\nBLOCKED")
    with respx.mock(assert_all_called=False) as mock:
        first = mock.get(
            f"{GH}/repos/{REPO}/issues/5/comments", params={"per_page": 100, "page": 1}
        ).respond(200, json=[old] + [{"user": {"type": "User"}}] * 99)
        second = mock.get(
            f"{GH}/repos/{REPO}/issues/5/comments", params={"per_page": 100, "page": 2}
        ).respond(200, json=[latest])
        response = await client.get(f"/v1/ae/prs/{REPO}/5/compliance?fresh=true")
        assert first.called and second.called
    assert response.json()["report"]["body"] == latest["body"]


@pytest.mark.parametrize("failure", ["http", "timeout", "json", "shape", "pagination"])
async def test_compliance_failures_are_retryable_not_missing(
    client: httpx.AsyncClient, failure: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{GH}/repos/{REPO}/issues/5/comments")
        if failure == "http":
            route.respond(403, json={"message": "Forbidden"})
        elif failure == "timeout":
            route.mock(side_effect=httpx.ReadTimeout("Timeout"))
        elif failure == "json":
            route.respond(200, text="not json")
        elif failure == "shape":
            route.respond(200, json={"unexpected": []})
        else:
            route.respond(200, json=[compliance_comment()] * 100)
        response = await client.get(f"/v1/ae/prs/{REPO}/5/compliance?fresh=true")
        assert route.call_count == (10 if failure == "pagination" else 1)
        route.respond(200, json=[])
        retry = await client.get(f"/v1/ae/prs/{REPO}/5/compliance?fresh=true")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == prs.CODE_GITHUB_FAILED
    assert "report" not in response.json()
    assert retry.json() == {"report": None}


async def test_compliance_cache_refresh_failure_does_not_serve_old_report(
    client: httpx.AsyncClient,
) -> None:
    path = f"/v1/ae/prs/{REPO}/5/compliance"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(
            200, json=[compliance_comment()]
        )
        await client.get(path)
        await client.get(path)
        assert route.call_count == 1
        route.respond(500, json={"message": "Temporary failure"})
        assert (await client.get(f"{path}?fresh=true")).status_code == 502
        assert (await client.get(path)).status_code == 502
        route.respond(200, json=[])
        assert (await client.get(path)).json() == {"report": None}


async def test_compliance_never_changes_merge_gate(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock_card(mock, 5)
        before = (await client.get(f"/v1/ae/prs/{REPO}/5")).json()
        comments = mock.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(
            200, json=[compliance_comment(body="## AE Compliance Review\nBLOCKED")]
        )
        await client.get(f"/v1/ae/prs/{REPO}/5/compliance?fresh=true")
        after = (await client.get(f"/v1/ae/prs/{REPO}/5?fresh=true")).json()
        assert comments.call_count == 1  # Card reads never fetch reports.
    assert before["merge"]["allowed"] is True
    assert after["merge"] == before["merge"]


async def test_compliance_cache_is_bounded(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pr_compliance, "MAX_REPORTS", 2)
    with respx.mock(assert_all_called=False) as mock:
        for number in (1, 2, 3):
            mock.get(f"{GH}/repos/{REPO}/issues/{number}/comments").respond(200, json=[])
            await client.get(f"/v1/ae/prs/{REPO}/{number}/compliance")
    assert len(pr_compliance._cache) == 2
    assert (REPO, 1) not in pr_compliance._cache


async def test_compliance_tied_update_time_prefers_newer_id(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(
            200,
            json=[
                compliance_comment(),
                compliance_comment(id=43, body="## AE Compliance Review\nBLOCKED"),
            ],
        )
        response = await client.get(f"/v1/ae/prs/{REPO}/5/compliance?fresh=true")
    assert response.json()["report"]["verdict"] == "blocked"
    assert response.json()["report"]["url"].endswith("issuecomment-43")


async def test_compliance_page_failure_never_returns_earlier_report(
    client: httpx.AsyncClient,
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(
            f"{GH}/repos/{REPO}/issues/5/comments", params={"per_page": 100, "page": 1}
        ).respond(200, json=[compliance_comment()] + [{}] * 99)
        mock.get(
            f"{GH}/repos/{REPO}/issues/5/comments", params={"per_page": 100, "page": 2}
        ).respond(500, json={"message": "Failed page"})
        response = await client.get(f"/v1/ae/prs/{REPO}/5/compliance?fresh=true")
    assert response.status_code == 502
    assert "report" not in response.json()


@pytest.mark.parametrize("head_response", [500, 200])
async def test_compliance_head_fetch_failure_is_retryable(
    client: httpx.AsyncClient, head_response: int
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(
            200,
            json=[
                compliance_comment(
                    body=f"## AE Compliance Review\n<!-- ae-compliance-reviewed-sha: {SHA} -->\n"
                    "APPROVED"
                )
            ],
        )
        mock.get(f"{GH}/repos/{REPO}/pulls/5").respond(head_response, json={})
        response = await client.get(f"/v1/ae/prs/{REPO}/5/compliance?fresh=true")
    assert response.status_code == 502
    assert "report" not in response.json()


async def test_sha_in_findings_is_not_revision_evidence(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(
            200,
            json=[
                compliance_comment(
                    body=f"## AE Compliance Review\nAPPROVED\n\nReviewed {SHA}\n"
                    f"<!-- ae-compliance-reviewed-sha: {SHA} -->"
                )
            ],
        )
        response = await client.get(f"/v1/ae/prs/{REPO}/5/compliance?fresh=true")
    assert response.json()["report"]["reviewed_sha"] is None
    assert response.json()["report"]["revision"] == "unknown"


@pytest.mark.parametrize("endpoint", ["compliance", "card"])
async def test_inflight_read_cannot_repopulate_cache_after_write(
    client: httpx.AsyncClient, endpoint: str
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_card(mock, 5)

        def invalidate_during_read(request: httpx.Request) -> httpx.Response:
            prs.forget(REPO, 5)
            body = [compliance_comment()] if endpoint == "compliance" else pull(5)
            return httpx.Response(200, json=body)

        if endpoint == "compliance":
            mock.get(f"{GH}/repos/{REPO}/issues/5/comments").mock(
                side_effect=invalidate_during_read
            )
            response = await client.get(f"/v1/ae/prs/{REPO}/5/compliance")
            assert (REPO, 5) not in pr_compliance._cache
        else:
            routes["pull"].mock(side_effect=invalidate_during_read)
            response = await client.get(f"/v1/ae/prs/{REPO}/5")
            assert (REPO, 5) not in prs._cards
    assert response.status_code == 200


@pytest.mark.parametrize("endpoint", ["compliance", "update-branch"])
async def test_new_routes_report_missing_credentials(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.delenv("AE_GITHUB_TOKEN")
    with respx.mock(assert_all_called=False) as mock:
        path = f"/v1/ae/prs/{REPO}/5/{endpoint}"
        response = (
            await client.get(path)
            if endpoint == "compliance"
            else await client.post(path, json={"sha": SHA})
        )
        assert not mock.calls
    assert response.status_code == 503
    assert response.json()["error"]["code"] == prs.CODE_NOT_CONFIGURED


# ── a non-admin and the server's GitHub token (2026-10-28) ─────────────

OWNER = {"X-Forwarded-Email": "owner@example.com"}
MEMBER = {"X-Forwarded-Email": "member@example.com"}


def mock_access(mock: respx.MockRouter, session_id: str, *, allowed: bool = True) -> None:
    """The access check a non-admin's PR read makes of a session (``GET /v1/sessions/{id}``)."""
    mock.get(url__startswith=f"{SELF}/v1/sessions/{session_id}?").respond(
        200 if allowed else 403, json={}
    )


async def _session_of(client: httpx.AsyncClient, who: dict[str, str], title: str) -> str:
    client.headers.update(who)
    try:
        return await session(client, title, "working")
    finally:
        del client.headers["X-Forwarded-Email"]


async def test_a_non_admin_reads_only_the_pull_requests_their_sessions_track(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    mine = await _session_of(client, MEMBER, "Member's PR")
    theirs = await _session_of(client, OWNER, "Owner's PR")
    with respx.mock(assert_all_called=False) as mock:
        mock_tracked(mock, mine, [pr_url(5)])
        mock_tracked(mock, theirs, [pr_url(6)])
        mock_access(mock, mine)
        mock_access(mock, theirs, allowed=False)
        mock_card(mock, 5)
        mock_card(mock, 6)
        mock.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(200, json=[])
        listed = (await client.get("/v1/ae/prs", headers=MEMBER)).json()
        own = await client.get(f"/v1/ae/prs/{REPO}/5", headers=MEMBER)
        own_compliance = await client.get(f"/v1/ae/prs/{REPO}/5/compliance", headers=MEMBER)
        other = await client.get(f"/v1/ae/prs/{REPO}/6", headers=MEMBER)
        other_compliance = await client.get(f"/v1/ae/prs/{REPO}/6/compliance", headers=MEMBER)
        admin = await client.get(f"/v1/ae/prs/{REPO}/6", headers=OWNER)
    assert [card["number"] for card in listed["items"]] == [5]
    assert [error.get("session_id") for error in listed["errors"]] == []
    assert own.status_code == 200, own.text
    assert own_compliance.status_code == 200, own_compliance.text
    for refused in (other, other_compliance):
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "forbidden"
    assert admin.status_code == 200, admin.text


async def test_one_users_scan_never_answers_another(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    theirs = await _session_of(client, OWNER, "Owner's PR")
    with respx.mock(assert_all_called=False) as mock:
        mock_tracked(mock, theirs, [pr_url(6)])
        mock_card(mock, 6)
        assert [
            c["number"] for c in (await client.get("/v1/ae/prs", headers=OWNER)).json()["items"]
        ] == [6]
        assert (await client.get("/v1/ae/prs", headers=MEMBER)).json()["items"] == []


async def test_the_asking_chat_vouches_for_its_own_pull_request(
    client: httpx.AsyncClient, ae_permissions: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # An older chat falls outside the newest sessions a scan reads (2026-10-28).
    monkeypatch.setattr(prs, "MAX_SESSIONS", 0)
    mine = await _session_of(client, MEMBER, "Old chat")
    theirs = await _session_of(client, OWNER, "Owner's chat")
    with respx.mock(assert_all_called=False) as mock:
        mock_tracked(mock, mine, [pr_url(5)])
        mock_tracked(mock, theirs, [pr_url(5)])
        mock_access(mock, mine)
        mock_access(mock, theirs, allowed=False)
        mock_card(mock, 5)
        url = f"/v1/ae/prs/{REPO}/5"
        assert (await client.get(url, headers=MEMBER)).status_code == 403
        vouched = await client.get(url, params={"session_id": mine}, headers=MEMBER)
        borrowed = await client.get(url, params={"session_id": theirs}, headers=MEMBER)
    assert vouched.status_code == 200, vouched.text
    assert borrowed.status_code == 403, borrowed.text


async def test_a_miss_rescans_at_most_once_in_thirty_seconds(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    mine = await _session_of(client, MEMBER, "Member's PR")
    with respx.mock(assert_all_called=False) as mock:
        route = mock_tracked(mock, mine, [pr_url(5)])
        mock_card(mock, 9)
        first = await client.get(f"/v1/ae/prs/{REPO}/9", headers=MEMBER)
        after_first = route.call_count
        second = await client.get(f"/v1/ae/prs/{REPO}/9", headers=MEMBER)
        third = await client.get(f"/v1/ae/prs/{REPO}/8", headers=MEMBER)
    assert first.status_code == second.status_code == third.status_code == 403
    assert after_first == 2  # the scan, then one fresh scan
    assert route.call_count == after_first  # later misses answer from the last scan


async def test_a_revoked_session_no_longer_vouches_for_a_stale_scan(
    client: httpx.AsyncClient, ae_permissions: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ae_omni_policies import router as router_module

    mine = await _session_of(client, MEMBER, "Shared then revoked")
    with respx.mock(assert_all_called=False) as mock:
        mock_tracked(mock, mine, [pr_url(5)])
        mock_access(mock, mine)
        mock_card(mock, 5)
        mock.get(f"{GH}/repos/{REPO}/issues/5/comments").respond(200, json=[])
        for route in ("", "/compliance"):
            assert (
                await client.get(f"/v1/ae/prs/{REPO}/5{route}", headers=MEMBER)
            ).status_code == 200
        real = router_module._may_open_session

        async def revoked(request: Any, user: str | None, session_id: str) -> bool:
            return False if session_id == mine else await real(request, user, session_id)

        monkeypatch.setattr(router_module, "_may_open_session", revoked)
        for route in ("", "/compliance"):
            gone = await client.get(f"/v1/ae/prs/{REPO}/5{route}", headers=MEMBER)
            assert gone.status_code == 403, gone.text


async def test_the_deploy_check_is_scoped_like_the_card(
    client: httpx.AsyncClient, ae_permissions: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ae_omni_policies import router as router_module

    async def status(repo: str, number: int) -> dict[str, Any]:
        return {"state": "unknown", "repo": repo, "number": number}

    monkeypatch.setattr(router_module.pr_deploy, "status", status)
    mine = await _session_of(client, MEMBER, "Member's PR")
    with respx.mock(assert_all_called=False) as mock:
        mock_tracked(mock, mine, [pr_url(5)])
        mock_access(mock, mine)
        own = await client.get(f"/v1/ae/prs/{REPO}/5/deploy", headers=MEMBER)
        other = await client.get(f"/v1/ae/prs/{REPO}/6/deploy", headers=MEMBER)
        admin = await client.get(f"/v1/ae/prs/{REPO}/6/deploy", headers=OWNER)
    assert own.status_code == 200 and admin.status_code == 200
    assert other.status_code == 403


async def test_a_failed_scan_does_not_use_up_the_thirty_seconds(
    client: httpx.AsyncClient, ae_permissions: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = await _session_of(client, MEMBER, "Member's PR")
    real = prs.tracking_sessions
    calls: list[bool] = []

    async def flaky(fetch: Any, repo: str, number: int, **kwargs: Any) -> list[str]:
        calls.append(bool(kwargs.get("fresh")))
        if kwargs.get("fresh") and calls.count(True) == 1:
            raise RuntimeError("scan failed")
        return await real(fetch, repo, number, **kwargs)

    monkeypatch.setattr(prs, "tracking_sessions", flaky)
    with respx.mock(assert_all_called=False) as mock:
        mock_tracked(mock, mine, [pr_url(5)])
        mock_access(mock, mine)
        mock_card(mock, 9)
        with pytest.raises(RuntimeError):
            await client.get(f"/v1/ae/prs/{REPO}/9", headers=MEMBER)
        assert prs.fresh_scan_allowed("member@example.com")
        again = await client.get(f"/v1/ae/prs/{REPO}/9", headers=MEMBER)
    assert again.status_code == 403
    assert calls.count(True) == 2
    assert not prs.fresh_scan_allowed("member@example.com")


async def test_a_scan_whose_sessions_are_all_closed_to_the_caller_gets_one_rescan(
    client: httpx.AsyncClient, ae_permissions: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ae_omni_policies import router as router_module

    old = await _session_of(client, MEMBER, "Revoked")
    new = await _session_of(client, MEMBER, "Replacement")
    with respx.mock(assert_all_called=False) as mock:
        route_old = mock_tracked(mock, old, [pr_url(5)])
        route_new = mock_tracked(mock, new, [])
        mock_access(mock, old)
        mock_access(mock, new)
        mock_card(mock, 5)
        assert (await client.get(f"/v1/ae/prs/{REPO}/5", headers=MEMBER)).status_code == 200
        real = router_module._may_open_session

        async def only_new(request: Any, user: str | None, session_id: str) -> bool:
            return False if session_id == old else await real(request, user, session_id)

        monkeypatch.setattr(router_module, "_may_open_session", only_new)
        route_old.respond(200, json={"prs": []})
        route_new.respond(200, json={"prs": [{"url": pr_url(5), "relationship": "created"}]})
        # The listed session is closed now; the re-scan finds the one that is still open.
        moved = await client.get(f"/v1/ae/prs/{REPO}/5", headers=MEMBER)
    assert moved.status_code == 200, moved.text
