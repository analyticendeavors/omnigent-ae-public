"""merge_watch.py: one note per merge or close, polled with ETags, kept across restarts.

GitHub is an ``httpx.MockTransport`` that answers from a queue per pull request
and records what was asked; the scan and the poster are stubs; the clock is a
list the tests move. Added 2026-10-21 with the watcher.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import auth, github, merge_watch
from ae_omni_policies.continuation import is_system_continuation
from ae_omni_policies.merge_watch import MergeWatcher, Scan, StateFile, Watched

REPO = "octo/repo"
URL = f"https://github.com/{REPO}/pull/196"
NOW = 1_800_000_000.0
MERGED_AT = "2027-01-15T08:00:00Z"  # NOW is 2027-01-15T08:00:00Z


def _pull(state: str = "open", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "number": 196,
        "title": "Add an Accounts card",
        "html_url": URL,
        "state": "closed" if state != "open" else "open",
        "merged": state == "merged",
        "merged_at": MERGED_AT if state == "merged" else None,
        "closed_at": MERGED_AT if state != "open" else None,
    }
    if state == "merged":
        body |= {"merge_commit_sha": "11c89f51" + "0" * 32, "merged_by": {"login": "octocat"}}
    return body | extra


class FakeGitHub:
    """Answers ``GET /repos/octo/repo/pulls/196`` from a queue; repeats the last answer."""

    def __init__(self) -> None:
        self.answers: list[httpx.Response] = []
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        assert request.url.path == f"/repos/{REPO}/pulls/196"
        if len(self.answers) > 1:
            return self.answers.pop(0)
        return self.answers[0]

    def ok(self, state: str, etag: str, headers: dict[str, str] | None = None) -> httpx.Response:
        return httpx.Response(200, json=_pull(state), headers={"etag": etag, **(headers or {})})


class Harness:
    """One watcher with its fakes; :meth:`restart` builds a new one on the same file."""

    def __init__(self, path: Path | None, gh: FakeGitHub) -> None:
        self.path = path
        self.gh = gh
        self.clock = [NOW]
        self.tracked: dict[str, list[str]] | None = {URL: ["conv_a"]}
        self.unread: set[str] = set()
        self.asked_for: list[set[str]] = []
        self.posted: list[tuple[str, str]] = []
        self.outcomes: list[merge_watch.PostOutcome] = []
        self.store = StateFile(path)
        self.watcher = self._build()

    def _build(self) -> MergeWatcher:
        async def discover(watched: set[str]) -> Scan | None:
            self.asked_for.append(set(watched))
            if self.tracked is None:
                return None
            read = {sid for ids in self.tracked.values() for sid in ids} | watched
            return Scan(self.tracked, frozenset(read - self.unread))

        async def post(session_id: str, text: str) -> merge_watch.PostOutcome:
            self.posted.append((session_id, text))
            return self.outcomes.pop(0) if self.outcomes else "posted"

        return MergeWatcher(
            github.GitHubSettings(token="test-token"),
            discover=discover,
            post=post,
            store=self.store,
            clock=lambda: self.clock[0],
            transport=httpx.MockTransport(self.gh.handler),
        )

    async def restart(self) -> None:
        await self.watcher.aclose()
        if self.path is None:
            self.store = StateFile(None)
        self.watcher = self._build()

    async def tick(self, advance: float = merge_watch.POLL_INTERVAL_S) -> None:
        await self.watcher.tick()
        self.clock[0] += advance


@pytest.fixture()
def gh() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture()
async def harness(tmp_path: Path, gh: FakeGitHub):
    made = Harness(tmp_path / "ae-merge-watch.json", gh)
    yield made
    await made.watcher.aclose()


async def test_a_merge_posts_one_note_and_leaves_the_poll_set(
    harness: Harness, gh: FakeGitHub
) -> None:
    gh.answers = [gh.ok("open", '"e1"'), httpx.Response(304), gh.ok("merged", '"e2"')]
    await harness.tick()  # first read: open, no note
    await harness.tick()  # 304: nothing changed
    assert harness.posted == []
    await harness.tick()  # merged
    assert len(harness.posted) == 1
    session_id, text = harness.posted[0]
    assert session_id == "conv_a"
    assert text == (
        "Merge watcher: PR #196 (Add an Accounts card) merged at 08:00Z as 11c89f51 by "
        f"octocat. {URL} Continue: delete its branch and worktree, then check the deploy."
    )
    assert is_system_continuation(text)
    calls = len(gh.seen)
    for _ in range(5):
        await harness.tick()
    assert len(harness.posted) == 1
    assert len(gh.seen) == calls, "a merged pull request is no longer polled"
    assert harness.watcher.pollable(harness.clock[0]) == []


async def test_a_close_without_merging_says_so(harness: Harness, gh: FakeGitHub) -> None:
    gh.answers = [gh.ok("open", '"e1"'), gh.ok("closed", '"e2"')]
    await harness.tick()
    await harness.tick()
    assert len(harness.posted) == 1
    text = harness.posted[0][1]
    assert text.startswith(
        "Merge watcher: PR #196 (Add an Accounts card) was closed without merging at 08:00Z. "
    )
    assert "merged at" not in text
    assert is_system_continuation(text)


async def test_the_304_path_sends_the_etag_and_changes_nothing(
    harness: Harness, gh: FakeGitHub
) -> None:
    gh.answers = [gh.ok("open", '"e1"'), httpx.Response(304)]
    await harness.tick()
    assert "if-none-match" not in gh.seen[0].headers
    for _ in range(3):
        await harness.tick()
    assert [request.headers.get("if-none-match") for request in gh.seen[1:]] == ['"e1"'] * 3
    record = harness.watcher.records[f"{REPO}#196"]
    assert record.state == "open" and record.etag == '"e1"' and record.errors == 0
    assert harness.posted == []


async def test_a_restart_catches_a_merge_made_while_down_and_never_reposts(
    harness: Harness, gh: FakeGitHub
) -> None:
    gh.answers = [gh.ok("open", '"e1"')]
    await harness.tick()
    assert harness.watcher.records[f"{REPO}#196"].state == "open"
    # The server goes down; the merge lands an hour later; the scan is slow to answer.
    harness.clock[0] += 3600
    harness.tracked = None
    gh.answers = [gh.ok("merged", '"e2"')]
    await harness.restart()
    await harness.tick()
    assert len(harness.posted) == 1 and "merged at 08:00Z" in harness.posted[0][1]
    assert gh.seen[-1].headers.get("if-none-match") == '"e1"', "the ETag survives a restart"
    # Another restart after the note went out: no second note, no poll.
    calls = len(gh.seen)
    await harness.restart()
    for _ in range(3):
        await harness.tick()
    assert len(harness.posted) == 1
    assert len(gh.seen) == calls


async def test_a_note_written_before_a_crash_is_delivered_after_it(
    harness: Harness, gh: FakeGitHub
) -> None:
    gh.answers = [gh.ok("open", '"e1"'), gh.ok("merged", '"e2"')]
    await harness.tick()
    harness.outcomes = ["retry"]
    await harness.tick()  # merged; the session's host is offline
    assert len(harness.posted) == 1
    await harness.restart()
    harness.clock[0] += 60
    await harness.tick()
    assert len(harness.posted) == 2, "the undelivered note is tried again after the restart"
    await harness.tick()
    assert len(harness.posted) == 2


async def test_a_pull_request_first_seen_merged_long_ago_stays_quiet(
    harness: Harness, gh: FakeGitHub
) -> None:
    harness.clock[0] = NOW + merge_watch.FIRST_SIGHT_WINDOW_S + 60
    gh.answers = [gh.ok("merged", '"e1"')]
    await harness.tick()
    assert harness.posted == []
    assert harness.watcher.records[f"{REPO}#196"].state == "merged"


async def test_a_merge_before_the_first_scan_listed_it_is_still_announced(
    harness: Harness, gh: FakeGitHub
) -> None:
    harness.clock[0] = NOW + 120
    gh.answers = [gh.ok("merged", '"e1"')]
    await harness.tick()
    assert len(harness.posted) == 1


@pytest.mark.parametrize(
    ("answer", "wait"),
    [
        (
            httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(NOW) + 300)},
            ),
            300,
        ),
        (httpx.Response(429, headers={"retry-after": "45"}), 45),
        (httpx.Response(429), merge_watch.RATE_LIMIT_DEFAULT_S),
    ],
)
async def test_a_rate_limit_pauses_every_poll(
    harness: Harness, gh: FakeGitHub, answer: httpx.Response, wait: float
) -> None:
    gh.answers = [answer, gh.ok("open", '"e1"')]
    await harness.tick(advance=0)
    assert harness.watcher.paused_until == NOW + wait
    calls = len(gh.seen)
    harness.clock[0] = NOW + wait - 1
    await harness.tick(advance=0)
    assert len(gh.seen) == calls, "no GitHub call while paused"
    harness.clock[0] = NOW + wait
    await harness.tick(advance=0)
    assert len(gh.seen) == calls + 1
    assert harness.watcher.records[f"{REPO}#196"].state == "open"


async def test_repeated_rate_limits_without_headers_double_the_pause(
    harness: Harness, gh: FakeGitHub
) -> None:
    gh.answers = [httpx.Response(429)]
    await harness.tick(advance=0)
    first = harness.watcher.paused_until - harness.clock[0]
    harness.clock[0] = harness.watcher.paused_until
    await harness.tick(advance=0)
    assert harness.watcher.paused_until - harness.clock[0] == 2 * first


async def test_an_ordinary_failure_backs_off_that_pull_request_only(
    harness: Harness, gh: FakeGitHub
) -> None:
    gh.answers = [httpx.Response(404, json={"message": "Not Found"}), gh.ok("open", '"e1"')]
    await harness.tick(advance=0)
    record = harness.watcher.records[f"{REPO}#196"]
    assert record.errors == 1 and record.next_poll == NOW + 20
    assert harness.watcher.paused_until == 0.0
    await harness.tick(advance=0)
    assert len(gh.seen) == 1
    harness.clock[0] = NOW + 20
    await harness.tick(advance=0)
    assert record.state == "open" and record.errors == 0


async def test_every_session_that_tracks_it_is_told(harness: Harness, gh: FakeGitHub) -> None:
    harness.tracked = {URL: ["conv_b", "conv_a"]}
    gh.answers = [gh.ok("open", '"e1"'), gh.ok("merged", '"e2"')]
    await harness.tick()
    await harness.tick()
    assert sorted(session for session, _ in harness.posted) == ["conv_a", "conv_b"]


async def test_a_refused_note_is_dropped_and_a_failing_one_given_up(
    harness: Harness, gh: FakeGitHub
) -> None:
    harness.tracked = {URL: ["conv_a", "conv_b"]}
    gh.answers = [gh.ok("open", '"e1"'), gh.ok("merged", '"e2"')]
    harness.outcomes = ["drop", *["retry"] * merge_watch.MAX_POST_ATTEMPTS]
    await harness.tick()
    for _ in range(merge_watch.MAX_POST_ATTEMPTS + 2):
        await harness.tick(advance=merge_watch.MAX_POST_BACKOFF_S)
    assert [session for session, _ in harness.posted].count("conv_a") == 1
    assert [session for session, _ in harness.posted].count("conv_b") == (
        merge_watch.MAX_POST_ATTEMPTS
    )
    assert harness.watcher.records[f"{REPO}#196"].pending == {}


def test_the_state_file_round_trips_and_survives_garbage(tmp_path: Path) -> None:
    path = tmp_path / "watch.json"
    store = StateFile(path)
    record = Watched(repo=REPO, number=196, url=URL, state="open", etag='"e1"')
    store.save({record.key: record})
    assert store.load() == {record.key: record}
    path.write_text("{not json", encoding="utf-8")
    assert store.load() == {}
    path.write_text('{"prs": {"x": {"repo": 1}, "y": {"state": "bogus"}}}', encoding="utf-8")
    assert store.load() == {}


def test_the_state_path_and_the_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(merge_watch, "DATA_DIR", tmp_path / "missing")
    assert merge_watch.state_path({}) is None
    monkeypatch.setattr(merge_watch, "DATA_DIR", tmp_path)
    assert merge_watch.state_path({}) == tmp_path / "ae-merge-watch.json"
    assert merge_watch.state_path({"AE_MERGE_WATCH_FILE": "/x/y.json"}) == Path("/x/y.json")
    assert merge_watch.enabled({}) is True
    assert merge_watch.enabled({"AE_MERGE_WATCH": "0"}) is False


async def test_start_stays_off_without_a_token_or_when_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AE_MERGE_WATCH", "0")
    monkeypatch.setenv("AE_GITHUB_TOKEN", "test-token")
    assert merge_watch.start(object()) is None  # type: ignore[arg-type]
    monkeypatch.setenv("AE_MERGE_WATCH", "1")
    monkeypatch.delenv("AE_GITHUB_TOKEN")
    assert merge_watch.start(object()) is None  # type: ignore[arg-type]


class _Provider:
    def __init__(self, source: str, mint: Callable[[str, int], str | None]) -> None:
        self.source = source
        self.mint = mint

    def mint_runner_token(self, user_id: str, ttl_seconds: int) -> str | None:
        return self.mint(user_id, ttl_seconds)


async def test_the_owner_identity_per_auth_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.server.auth as upstream_auth

    identity = merge_watch.OwnerIdentity()
    owners = {"conv_a": "owner@example.com"}
    monkeypatch.setattr(identity, "owner", owners.get)
    provider = _Provider("oidc", lambda user, ttl: f"jwt-for-{user}-{ttl}")
    monkeypatch.setattr(auth, "provider", lambda: provider)
    monkeypatch.setattr(upstream_auth, "auth_mode", lambda p: p.source)
    assert await identity.headers("conv_a") == {
        "authorization": f"Bearer jwt-for-owner@example.com-{merge_watch.TOKEN_TTL_S}"
    }
    assert await identity.headers("conv_none") is None
    provider.mint = lambda user, ttl: None
    assert await identity.headers("conv_a") is None
    provider.source = "header"
    monkeypatch.setattr(upstream_auth, "resolve_auth_header", lambda: "X-Forwarded-Email")
    monkeypatch.setattr(upstream_auth, "authentication_requires_identity", lambda p: True)
    assert await identity.headers("conv_a") == {"X-Forwarded-Email": "owner@example.com"}
    assert await identity.headers("conv_none") is None
    monkeypatch.setattr(upstream_auth, "authentication_requires_identity", lambda p: False)
    assert await identity.headers("conv_none") == {}
    provider.source = "none"
    assert await identity.headers("conv_none") == {}
    provider.source = "custom"
    assert await identity.headers("conv_a") is None


async def test_the_note_reaches_the_events_route_as_the_owner(
    client: httpx.AsyncClient, app: Any, ae_permissions: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The live path on the AE app: the owner grant from the permission store, then
    # the in-process POST /v1/sessions/{id}/events with the owner's header. The
    # test server has no runner, so the route answers 503 after auth passed (a
    # retry); as someone else the same post is refused and dropped.
    from tests.server.helpers import create_test_session

    client.headers.update({"X-Forwarded-Email": "reid@example.com"})
    sid = (await create_test_session(client, title="Ship the card"))["id"]
    del client.headers["X-Forwarded-Email"]
    identity = merge_watch.OwnerIdentity()
    assert identity.owner(sid) == "reid@example.com"
    note = "Merge watcher: PR #196 (Add an Accounts card) merged at 08:00Z as 11c89f51."
    assert await merge_watch.post_live(app, identity, sid, note) == "retry"
    monkeypatch.setattr(identity, "owner", lambda _sid: "steve@example.com")
    assert await merge_watch.post_live(app, identity, sid, note) == "drop"
    monkeypatch.setattr(identity, "owner", lambda _sid: None)
    assert await merge_watch.post_live(app, identity, sid, note) == "drop"


async def test_the_app_lifespan_starts_and_stops_the_watcher(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mounted through DEBUG_ROUTERS: FastAPI merges the router's lifespan into the app's.
    import asyncio

    monkeypatch.setenv("AE_MERGE_WATCH", "1")
    monkeypatch.setenv("AE_GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(merge_watch, "state_path", lambda: None)
    events: list[str] = []

    async def run(self: MergeWatcher) -> None:
        events.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stopped")

    monkeypatch.setattr(MergeWatcher, "run", run)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert events == ["started"]
    assert events == ["started", "stopped"]


async def test_a_session_that_attaches_after_the_merge_is_told_once(
    harness: Harness, gh: FakeGitHub
) -> None:
    # Codex review, 2026-10-21: a second session attached between two scans.
    gh.answers = [gh.ok("open", '"e1"'), gh.ok("merged", '"e2"')]
    await harness.tick()
    await harness.tick()
    assert [sid for sid, _ in harness.posted] == ["conv_a"]
    harness.tracked = {URL: ["conv_a", "conv_b"]}
    harness.clock[0] += merge_watch.DISCOVERY_INTERVAL_S
    await harness.tick()
    harness.clock[0] += merge_watch.DISCOVERY_INTERVAL_S
    await harness.tick()
    assert [sid for sid, _ in harness.posted] == ["conv_a", "conv_b"]


async def test_a_session_the_scan_could_not_read_keeps_its_place(
    harness: Harness, gh: FakeGitHub
) -> None:
    # Review, 2026-10-21: a host offline at the last scan must not cost its session the note.
    harness.tracked = {URL: ["conv_a", "conv_b"]}
    gh.answers = [gh.ok("open", '"e1"')]
    await harness.tick()
    assert harness.asked_for[0] == set()
    harness.tracked, harness.unread = {URL: ["conv_b"]}, {"conv_a"}
    harness.clock[0] += merge_watch.DISCOVERY_INTERVAL_S
    await harness.tick()
    assert harness.asked_for[-1] == {"conv_a", "conv_b"}, "watched sessions are always read"
    assert harness.watcher.records[f"{REPO}#196"].sessions == ["conv_a", "conv_b"]


async def test_a_closed_then_reopened_pull_request_is_announced_when_it_merges(
    harness: Harness, gh: FakeGitHub
) -> None:
    # Review, 2026-10-21: closing and reopening to rerun CI.
    gh.answers = [
        gh.ok("open", '"e1"'),
        gh.ok("closed", '"e2"'),
        httpx.Response(304),
        gh.ok("open", '"e3"'),
        gh.ok("merged", '"e4"'),
    ]
    for _ in range(5):
        await harness.tick()
    notes = [text for _, text in harness.posted]
    assert len(notes) == 2
    assert "closed without merging" in notes[0] and "merged at" in notes[1]


async def test_a_post_that_hangs_is_given_up_without_a_second_try(
    harness: Harness, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Review, 2026-10-21: a post held on an approval must not stall the loop or repeat.
    import asyncio

    monkeypatch.setattr(merge_watch, "POST_TIMEOUT_S", 0.05)
    calls: list[str] = []

    async def hang(session_id: str, text: str) -> merge_watch.PostOutcome:
        calls.append(session_id)
        await asyncio.sleep(10)
        return "posted"

    harness.watcher._post = hang  # type: ignore[method-assign]
    gh.answers = [gh.ok("open", '"e1"'), gh.ok("merged", '"e2"')]
    await harness.tick()
    await harness.tick()
    await harness.tick()
    assert calls == ["conv_a"]
    assert harness.watcher.records[f"{REPO}#196"].pending == {}


class _FixedIdentity(merge_watch.OwnerIdentity):
    async def headers(self, session_id: str) -> dict[str, str] | None:
        return {"x-test": "owner"}


def _fake_call(
    monkeypatch: pytest.MonkeyPatch,
    *,
    get: httpx.Response | Exception,
    post: httpx.Response | None = None,
) -> list[str]:
    calls: list[str] = []

    async def fake(app: Any, method: str, path: str, headers: Any, body: Any = None):
        calls.append(method)
        if method == "GET":
            if isinstance(get, Exception):
                raise get
            return get
        return post or httpx.Response(200)

    monkeypatch.setattr(merge_watch, "_call", fake)
    return calls


async def _post() -> str:
    return await merge_watch.post_live(None, _FixedIdentity(), "conv_a", "note")  # type: ignore[arg-type]


async def test_a_gone_workspace_is_dropped_without_a_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    gone = httpx.Response(200, json={"workspace": str(tmp_path / "deleted-worktree")})
    calls = _fake_call(monkeypatch, get=gone)
    assert await _post() == "drop"
    assert calls == ["GET"]


async def test_an_existing_workspace_still_posts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _fake_call(monkeypatch, get=httpx.Response(200, json={"workspace": str(tmp_path)}))
    assert await _post() == "posted"
    assert calls == ["GET", "POST"]


@pytest.mark.parametrize(
    "get",
    [
        httpx.ConnectError("down"),
        httpx.Response(500),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"workspace": ""}),
    ],
)
async def test_an_unknown_workspace_still_posts(
    monkeypatch: pytest.MonkeyPatch, get: httpx.Response | Exception
) -> None:
    calls = _fake_call(monkeypatch, get=get)
    assert await _post() == "posted"
    assert calls == ["GET", "POST"]


async def test_a_workspace_missing_refusal_is_dropped_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = {"error": {"code": "WORKSPACE_MISSING", "message": "The workspace no longer exists"}}
    _fake_call(
        monkeypatch,
        get=httpx.Response(200, json={"workspace": str(tmp_path)}),
        post=httpx.Response(409, json=body),
    )
    assert await _post() == "drop"
