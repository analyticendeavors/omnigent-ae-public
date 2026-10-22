"""pr_deploy.py: whether a merged omnigent-ae pull request runs on the box (2026-10-20).

GitHub is a respx mock at ``https://api.github.com``; the session list is a
stub and the deploy hold lives in memory, so each test sets which tag runs,
what the compare answers and whether a hold is on.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from ae_omni_policies import deploy_hold, pr_deploy, release

GH = "https://api.github.com"
REPO = "analyticendeavors/omnigent-ae"
TAGS = f"{GH}/repos/{REPO}/git/matching-refs/tags/ae-v"
PULL = f"{GH}/repos/{REPO}/pulls/179"
SHA = "a" * 40
COMPARE = f"{GH}/repos/{REPO}/compare/{SHA}...ae-v0.18.0.31"
MERGED_AT = "2026-10-20T21:40:00Z"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    release.reset_cache()
    pr_deploy.reset_cache()
    monkeypatch.setenv("AE_GITHUB_TOKEN", "test-token")
    # AE's box sets its private repo; the default is the public one (2026-10-22).
    monkeypatch.setenv("AE_RELEASE_REPO", "analyticendeavors/omnigent-ae")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    monkeypatch.setenv("OMNIGENT_IMAGE_TAG", "ae-v0.18.0.31")
    monkeypatch.setenv("AE_SERVER_ENV_EXAMPLE", "/nonexistent-ae-config/server.env.example")
    monkeypatch.delenv(deploy_hold.STATE_FILE_ENV, raising=False)
    monkeypatch.setattr(deploy_hold, "DATA_DIR", Path("/nonexistent-ae-data"))
    deploy_hold.reset_store()
    now = int(time.time())
    rows = [SimpleNamespace(live_status="running", runner_last_seen=now - 5)] * 2
    monkeypatch.setattr(release.sessions, "list_top_level", lambda: rows)
    yield
    release.reset_cache()
    pr_deploy.reset_cache()
    deploy_hold.reset_store()


def _github(mock: respx.MockRouter, compare: str = "behind") -> respx.Route:
    mock.get(TAGS).mock(
        return_value=httpx.Response(
            200,
            json=[{"ref": f"refs/tags/{t}"} for t in ("ae-v0.18.0.31", "ae-v0.18.0.32")],
        )
    )
    mock.get(PULL).mock(
        return_value=httpx.Response(
            200,
            json={
                "merged_at": MERGED_AT,
                "merge_commit_sha": SHA,
                "state": "closed",
                "base": {"ref": "main", "repo": {"default_branch": "main"}},
            },
        )
    )
    return mock.get(COMPARE).mock(return_value=httpx.Response(200, json={"status": compare}))


async def test_a_merge_the_running_tag_lacks_is_waiting_with_the_busy_sessions() -> None:
    with respx.mock(assert_all_called=True) as mock:
        compare = _github(mock, "diverged")
        answer = await pr_deploy.status(REPO, 179)
    assert answer == {
        "state": "waiting",
        "running": "ae-v0.18.0.31",
        "latest": "ae-v0.18.0.32",
        "mid_turn": 2,
        "hold_until": None,
        "hold_by": None,
        "merged_at": MERGED_AT,
        "error": None,
    }
    assert compare.calls[0].request.headers["Authorization"] == "Bearer test-token"


@pytest.mark.parametrize("compare", ["ahead", "identical"])
async def test_a_running_tag_that_contains_the_merge_is_live(compare: str) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _github(mock, compare)
        answer = await pr_deploy.status(REPO, 179)
    assert answer["state"] == "live"


async def test_the_hold_in_force_is_named() -> None:
    deploy_hold.store().set(2, "you@example.com")
    with respx.mock(assert_all_called=False) as mock:
        _github(mock)
        answer = await pr_deploy.status(REPO, 179)
    assert answer["state"] == "waiting"
    assert answer["hold_by"] == "you@example.com"
    assert answer["hold_until"] is not None


async def test_the_compare_and_the_merge_commit_are_cached() -> None:
    with respx.mock(assert_all_called=False) as mock:
        compare = _github(mock)
        first = await pr_deploy.status(REPO, 179)
        again = await pr_deploy.status(REPO, 179)
        pull_calls = mock.routes[1].call_count
    assert first == again
    assert compare.call_count == 1, "the compare is cached per (tag, sha)"
    assert pull_calls == 1, "a merged pull request's merge commit is kept"


async def test_a_new_running_tag_compares_again(monkeypatch: pytest.MonkeyPatch) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _github(mock)
        assert (await pr_deploy.status(REPO, 179))["state"] == "waiting"
        monkeypatch.setenv("OMNIGENT_IMAGE_TAG", "ae-v0.18.0.32")
        mock.get(f"{GH}/repos/{REPO}/compare/{SHA}...ae-v0.18.0.32").mock(
            return_value=httpx.Response(200, json={"status": "ahead"})
        )
        assert (await pr_deploy.status(REPO, 179))["state"] == "live"


async def test_a_github_failure_is_unknown_not_an_error() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _github(mock)
        mock.get(COMPARE).mock(return_value=httpx.Response(502, json={"message": "down"}))
        answer = await pr_deploy.status(REPO, 179)
    assert answer["state"] == "unknown"
    assert "compare" in answer["error"]


async def test_no_token_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AE_GITHUB_TOKEN")
    answer = await pr_deploy.status(REPO, 179)
    assert answer["state"] == "unknown"
    assert "AE_GITHUB_TOKEN" in answer["error"]


async def test_an_unmerged_pull_request_is_unknown_and_not_kept() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _github(mock)
        mock.get(PULL).mock(
            return_value=httpx.Response(200, json={"merged_at": None, "merge_commit_sha": SHA})
        )
        answer = await pr_deploy.status(REPO, 179)
    assert answer["state"] == "unknown"
    assert answer["error"] == f"{REPO} #179 is not merged"


async def test_another_repo_gets_no_deploy_state() -> None:
    with respx.mock(assert_all_called=False) as mock:
        _github(mock)
        answer = await pr_deploy.status("analyticendeavors/agents-and-automation", 5)
    assert answer["state"] == "unknown"
    assert "release repository" in answer["error"]


async def test_the_route_answers_the_state(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        _github(mock, "ahead")
        response = await client.get(f"/v1/ae/prs/{REPO}/179/deploy")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "live"


async def test_the_route_refuses_a_bad_repo_name(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/ae/prs/bad..owner/../179/deploy")
    assert response.status_code in (400, 404)


async def test_a_stacked_pull_request_merged_off_main_is_unknown() -> None:
    with respx.mock(assert_all_called=False) as mock:
        compare = _github(mock)
        mock.get(PULL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "merged_at": MERGED_AT,
                    "merge_commit_sha": SHA,
                    "base": {"ref": "feat/a", "repo": {"default_branch": "main"}},
                },
            )
        )
        answer = await pr_deploy.status(REPO, 179)
    assert answer["state"] == "unknown"
    assert "feat/a" in answer["error"]
    assert compare.call_count == 0


async def test_another_repo_skips_the_release_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object) -> None:
        raise AssertionError("release.status must not run for another repo")

    monkeypatch.setattr(pr_deploy.release, "status", boom)
    answer = await pr_deploy.status("o/other", 1)
    assert answer["state"] == "unknown"
