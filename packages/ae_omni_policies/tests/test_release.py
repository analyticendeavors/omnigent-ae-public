"""release.py: the Session Dashboard's release row (2026-10-17).

GitHub is a respx mock at ``https://api.github.com``; the session list is a
stub, so each test sets which tag runs, which tags exist and who is mid-turn.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx

from ae_omni_policies import release

GH = "https://api.github.com"
TAGS = f"{GH}/repos/analyticendeavors/omnigent-ae/git/matching-refs/tags/ae-v"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    release.reset_cache()
    monkeypatch.setenv("AE_GITHUB_TOKEN", "test-token")
    # AE's box sets its private repo; the default is the public one (2026-10-22).
    monkeypatch.setenv("AE_RELEASE_REPO", "analyticendeavors/omnigent-ae")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    monkeypatch.setenv("OMNIGENT_IMAGE_TAG", "ae-v0.16.0.34")
    monkeypatch.setenv("AE_SERVER_ENV_EXAMPLE", "/nonexistent-ae-config/server.env.example")
    now = int(time.time())
    rows = [
        SimpleNamespace(live_status=status, runner_last_seen=seen)
        for status, seen in (
            ("running", now - 10),
            ("waiting", now - 200),
            ("running", now - 900),  # its runner died: auto-update ignores it
            ("idle", now),
            (None, None),
            ("failed", now),
        )
    ]
    monkeypatch.setattr(release.sessions, "list_top_level", lambda: rows)
    yield
    release.reset_cache()


def _tags(*names: str) -> httpx.Response:
    return httpx.Response(200, json=[{"ref": f"refs/tags/{name}"} for name in names])


def test_newest_sorts_by_version_and_ignores_other_tags() -> None:
    names = ["ae-v0.16.0.9", "ae-v0.16.0.35", "v0.16.0", "ae-v0.16.0.10", "ae-vX"]
    assert release.newest(names) == "ae-v0.16.0.35"
    assert release.newest(["v1.0"]) is None


async def test_a_newer_tag_is_waiting_on_the_sessions_mid_turn() -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(TAGS).mock(return_value=_tags("ae-v0.16.0.35", "ae-v0.16.0.34"))
        answer = await release.status()
        again = await release.status()
    assert answer == {
        "running": "ae-v0.16.0.34",
        "latest": "ae-v0.16.0.35",
        "state": "waiting",
        "mid_turn": 2,
        "error": None,
        "env_missing": None,
    }
    assert again == answer
    assert route.call_count == 1, "the tag list is cached"
    assert route.calls[0].request.headers["Authorization"] == "Bearer test-token"


async def test_the_running_tag_is_current() -> None:
    with respx.mock() as mock:
        mock.get(TAGS).mock(return_value=_tags("ae-v0.16.0.34", "ae-v0.16.0.33"))
        answer = await release.status()
    assert answer["state"] == "current"


async def test_github_off_or_failing_reads_as_unknown_with_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AE_GITHUB_TOKEN")
    off = await release.status()
    assert off["state"] == "unknown" and off["latest"] is None
    assert "AE_GITHUB_TOKEN" in off["error"]

    release.reset_cache()
    monkeypatch.setenv("AE_GITHUB_TOKEN", "test-token")
    with respx.mock() as mock:
        mock.get(TAGS).mock(return_value=httpx.Response(401, json={"message": "Bad credentials"}))
        failed = await release.status()
    assert failed["state"] == "unknown"
    assert "401" in failed["error"] and "test-token" not in failed["error"]


async def test_no_running_tag_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_IMAGE_TAG")
    with respx.mock() as mock:
        mock.get(TAGS).mock(return_value=_tags("ae-v0.16.0.35"))
        answer = await release.status()
    assert answer["running"] is None and answer["state"] == "unknown"


async def test_an_answer_that_is_not_a_list_of_refs_is_an_error_not_a_500() -> None:
    with respx.mock() as mock:
        mock.get(TAGS).mock(return_value=httpx.Response(200, text="<html>proxy</html>"))
        broken = await release.status()
    assert broken["state"] == "unknown" and "invalid JSON" in broken["error"]
    release.reset_cache()
    with respx.mock() as mock:
        mock.get(TAGS).mock(return_value=httpx.Response(200, json={"ref": "x"}))
        odd = await release.status()
    assert odd["state"] == "unknown" and "no list" in odd["error"]


def test_there_is_no_default_release_repo() -> None:
    assert release.DEFAULT_REPO == ""


async def test_without_a_release_repo_the_update_check_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AE_RELEASE_REPO", raising=False)
    release.reset_cache()
    with respx.mock():
        answer = await release.status()
    assert answer["state"] == "unknown" and "AE_RELEASE_REPO" in answer["error"]


async def test_settings_the_example_sets_and_the_env_lacks_are_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = tmp_path / "server.env.example"
    example.write_text("OMNIGENT_IMAGE_TAG=ae-v0\nAE_NEW_SETTING=1\n", encoding="utf-8")
    monkeypatch.setenv("AE_SERVER_ENV_EXAMPLE", str(example))
    monkeypatch.delenv("AE_NEW_SETTING", raising=False)
    with respx.mock() as mock:
        mock.get(TAGS).mock(return_value=_tags("ae-v0.16.0.34"))
        answer = await release.status()
    assert answer["env_missing"] == ["AE_NEW_SETTING"]
