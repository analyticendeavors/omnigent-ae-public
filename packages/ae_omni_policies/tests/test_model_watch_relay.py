"""The new-model watch's relay: the scan, the candidate rule, the probe, the pass."""

from __future__ import annotations

import io
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import model_watch, model_watch_relay, omni

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def test_the_scan_reads_ids_and_never_a_date_suffix(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    binary.write_bytes(
        b"\x00claude-opus-4-20250514\x00claude-fable-5-2[1m]\x00claude-mythos-5\x00"
        b"claude-sonnet-5-5 and claude-haiku-4-5-20251001"
    )
    assert model_watch_relay.scan_file(binary) == {
        "claude-opus-4",
        "claude-fable-5-2",
        "claude-mythos-5",
        "claude-sonnet-5-5",
        "claude-haiku-4-5",
    }


def test_a_match_cut_by_a_chunk_boundary_is_read_whole(tmp_path: Path) -> None:
    binary = tmp_path / "claude"
    # The boundary falls inside "-20250514" and inside "claude-fable-5-2".
    payload = b"x" * 10 + b"claude-opus-4-20250514" + b"y" * 5 + b"claude-fable-5-2" + b"z"
    binary.write_bytes(payload)
    for chunk in range(1, len(payload)):
        assert model_watch_relay.scan_file(binary, chunk) == {
            "claude-opus-4",
            "claude-fable-5-2",
        }, chunk


def test_a_candidate_is_newer_than_the_newest_known_of_its_family() -> None:
    known = {"claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5"}
    scanned = {
        "claude-fable-5-2",  # known
        "claude-fable-5-3",
        "claude-fable-5-4",  # newest of fable wins
        "claude-opus-5-5",  # known
        "claude-opus-4-8",  # older
        "claude-mythos-5-2",  # a family with nothing known
        "claude-mythos-5",
    }
    assert model_watch_relay.candidates(scanned, known) == [
        "claude-fable-5-4",
        "claude-mythos-5-2",
    ]
    assert model_watch_relay.candidates({"claude-fable-5-2"}, known) == []


@pytest.mark.parametrize(
    ("reply", "served"),
    [
        ({"is_error": False, "result": "ok", "modelUsage": {"claude-fable-5-2": {}}}, True),
        ({"is_error": False, "result": "OK.", "modelUsage": {"claude-fable-5-2[1m]": {}}}, True),
        ({"is_error": True, "result": "model not found", "modelUsage": {}}, False),
        ({"is_error": False, "result": "ok", "modelUsage": {"claude-haiku-4-5": {}}}, False),
        ({"is_error": False, "result": "I cannot", "modelUsage": {"claude-fable-5-2": {}}}, True),
        ({"is_error": False, "result": "ok", "modelUsage": {}}, False),
    ],
)
def test_a_reply_serves_when_the_model_answered(reply: dict[str, Any], served: bool) -> None:
    assert model_watch_relay.served_reply(json.dumps(reply), "claude-fable-5-2")[0] is served


@pytest.mark.parametrize(
    "text",
    [
        "There's an issue with the selected model (claude-mythos-5-2). It may not exist or "
        "you may not have access to it. Run --model to pick a different model.",
        "there's an issue. IT MAY NOT EXIST OR YOU MAY NOT HAVE ACCESS to it.",
    ],
)
def test_a_reply_that_says_the_model_is_unavailable_is_a_verdict(text: str) -> None:
    reply = json.dumps({"is_error": True, "result": text, "modelUsage": {}})
    assert model_watch_relay.served_reply(reply, "claude-fable-5-2", 1) == (
        False,
        f"the plan refused it: {text[:120]}",
        False,
    )


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 429 rate limit",
        "Invalid API key. Please run /login",
        "fetch failed ECONNRESET",
        "model not found",
    ],
)
def test_any_other_error_reply_is_inconclusive(text: str) -> None:
    reply = json.dumps({"is_error": True, "result": text, "modelUsage": {}})
    served, _detail, error = model_watch_relay.served_reply(reply, "claude-fable-5-2", 1)
    assert (served, error) == (False, True)
    ok = json.dumps({"is_error": False, "result": "ok", "modelUsage": {"claude-fable-5-2": {}}})
    assert model_watch_relay.served_reply(ok, "claude-fable-5-2", 2)[2] is True


def test_model_usage_decides_whatever_the_reply_says() -> None:
    usage = {"claude-fable-5-2": {}}
    for text in ["ok", " OK. ", "Okay, here you go!", "book"]:
        reply = json.dumps({"is_error": False, "result": text, "modelUsage": usage})
        answer = model_watch_relay.served_reply(reply, "claude-fable-5-2")
        assert answer[::2] == (True, False), text
    other = json.dumps({"is_error": False, "result": "ok", "modelUsage": {"claude-opus-5-5": {}}})
    assert model_watch_relay.served_reply(other, "claude-fable-5-2")[::2] == (False, False)
    unnamed = json.dumps({"is_error": False, "result": "ok", "modelUsage": {}})
    assert model_watch_relay.served_reply(unnamed, "claude-fable-5-2")[::2] == (False, True)


def test_a_reply_that_is_not_json_is_not_served() -> None:
    assert model_watch_relay.served_reply("boom", "claude-fable-5-2")[::2] == (False, True)


def _fake_claude(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-claude"
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


async def test_the_probe_runs_the_cli_and_reads_its_json(tmp_path: Path) -> None:
    reply = json.dumps({"is_error": False, "result": "ok", "modelUsage": {"claude-fable-5-2": {}}})
    # The model id and the flags reach the CLI; the word is read from stdin.
    claude = _fake_claude(
        tmp_path,
        f'for a in "$@"; do echo "$a" >> "$0.args"; done; cat > "$0.stdin"\necho \'{reply}\'',
    )
    served, _detail, error = await model_watch_relay.probe(claude, "claude-fable-5-2")
    assert (served, error) == (True, False)
    args = Path(claude + ".args").read_text().split("\n")
    assert {"-p", "--model", "claude-fable-5-2", "--tools", "--strict-mcp-config"} <= set(args)
    assert Path(claude + ".stdin").read_text() == model_watch_relay.PROMPT


async def test_the_probe_calls_a_refusal_a_verdict_and_a_crash_an_error(tmp_path: Path) -> None:
    refused = json.dumps(
        {
            "is_error": True,
            "result": "It may not exist or you may not have access to it.",
            "modelUsage": {},
        }
    )
    claude = _fake_claude(tmp_path, f"cat >/dev/null; echo '{refused}'; exit 1")
    assert (await model_watch_relay.probe(claude, "claude-fable-5-2"))[::2] == (False, False)
    limited = json.dumps({"is_error": True, "result": "429 rate limit", "modelUsage": {}})
    claude = _fake_claude(tmp_path, f"cat >/dev/null; echo '{limited}'; exit 1")
    assert (await model_watch_relay.probe(claude, "claude-fable-5-2"))[::2] == (False, True)
    missing = await model_watch_relay.probe(str(tmp_path / "nope"), "claude-fable-5-2")
    assert (missing[0], missing[2]) == (False, True)


async def test_the_probe_times_out(tmp_path: Path) -> None:
    claude = _fake_claude(tmp_path, "sleep 30")
    served, detail, error = await model_watch_relay.probe(claude, "claude-fable-5-2", timeout=0.3)
    assert (served, error) == (False, True) and "no answer" in detail


class Server:
    """The model-watch routes, in a list."""

    def __init__(self, served: list[str] | None = None, status: int = 200) -> None:
        self.served = served or []
        self.status = status
        self.posts: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"served_ids": self.served, "models": []})
        self.posts.append(json.loads(request.content))
        return httpx.Response(self.status, json={"kept": True, "new": []})


def _settings() -> omni.Settings:
    return omni.Settings(server_url="http://box", token="t", problem="")


def _binary(tmp_path: Path, *ids: str) -> str:
    path = tmp_path / "claude.bin"
    path.write_bytes(b"\x00" + b"\x00".join(i.encode() for i in ids))
    return str(path)


def _prober(served: set[str], calls: list[str]) -> Any:
    async def ask(_bin: str, model_id: str) -> tuple[bool, str, bool]:
        calls.append(model_id)
        return model_id in served, "answered ok" if model_id in served else "refused", False

    return ask


async def _run(
    tmp_path: Path, server: Server, calls: list[str], served: set[str], **kw: Any
) -> int:
    return await model_watch_relay.run(
        claude_bin=kw.pop("claude_bin"),
        settings=_settings(),
        state_path=tmp_path / "state.json",
        transport=httpx.MockTransport(server),
        prober=_prober(served, calls),
        now=kw.pop("now", NOW),
        out=io.StringIO(),
        **kw,
    )


async def test_a_pass_asks_the_newest_new_id_and_posts_it(tmp_path: Path) -> None:
    binary = _binary(
        tmp_path, "claude-fable-5-2", "claude-fable-5-3", "claude-fable-5-4", "claude-opus-5-5"
    )
    server, calls = Server(), []
    assert await _run(tmp_path, server, calls, {"claude-fable-5-4"}, claude_bin=binary) == 0
    assert calls == ["claude-fable-5-4"]
    (post,) = server.posts
    assert [p["id"] for p in post["probes"]] == ["claude-fable-5-4"]
    assert post["probes"][0]["served"] is True and post["probes"][0]["error"] is False
    model_watch.ReportPost.model_validate(post)


async def test_an_unchanged_binary_is_not_scanned_but_still_reports(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "claude-fable-5-4")
    server, calls = Server(), []
    await _run(tmp_path, server, calls, {"claude-fable-5-4"}, claude_bin=binary)
    calls.clear()
    assert await _run(tmp_path, server, calls, set(), claude_bin=binary) == 0
    assert calls == [] and server.posts[-1]["probes"] == []
    assert len(server.posts) == 2


async def test_an_id_that_did_not_answer_waits_24_hours(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "claude-mythos-5-2")
    server, calls = Server(), []
    await _run(tmp_path, server, calls, set(), claude_bin=binary)
    assert calls == ["claude-mythos-5-2"]
    await _run(tmp_path, server, calls, set(), claude_bin=binary, now=NOW + timedelta(hours=6))
    assert calls == ["claude-mythos-5-2"]
    await _run(tmp_path, server, calls, set(), claude_bin=binary, now=NOW + timedelta(hours=25))
    assert calls == ["claude-mythos-5-2"] * 2


async def test_a_pass_asks_at_most_three_ids(tmp_path: Path) -> None:
    binary = _binary(
        tmp_path,
        "claude-fable-9-1",
        "claude-opus-9-1",
        "claude-sonnet-9-1",
        "claude-haiku-9-1",
        "claude-mythos-9-1",
    )
    calls: list[str] = []
    await _run(tmp_path, Server(), calls, set(), claude_bin=binary)
    assert len(calls) == model_watch_relay.MAX_PROBES


async def test_a_served_id_the_server_holds_is_known(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "claude-fable-5-4")
    calls: list[str] = []
    await _run(tmp_path, Server(served=["claude-fable-5-4"]), calls, set(), claude_bin=binary)
    assert calls == []


async def test_an_id_a_host_catalog_lists_is_still_a_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Claude Code's cached catalog lists a release when it ships; that must not make it known.
    home = tmp_path / "home"
    catalog_dir = home / ".omnigent" / "cache" / "model-catalogs"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "claude-native-x.json").write_text(
        json.dumps({"models": [{"id": "claude-fable-5-4", "model": "claude-fable-5-4"}]})
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    binary = _binary(tmp_path, "claude-fable-5-4")
    server, calls = Server(), []
    code = await _run(tmp_path, server, calls, {"claude-fable-5-4"}, claude_bin=binary)
    assert code == 0
    assert calls == ["claude-fable-5-4"]
    assert [p["id"] for p in server.posts[0]["probes"]] == ["claude-fable-5-4"]


async def test_a_stamp_without_the_current_scan_rules_forces_a_rescan(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "claude-fable-5-4")
    info = Path(binary).stat()
    for old in ([info.st_size, int(info.st_mtime)], [info.st_size, int(info.st_mtime), 1]):
        (tmp_path / "state.json").write_text(json.dumps({"binary": old}))
        server, calls = Server(), []
        await _run(tmp_path, server, calls, set(), claude_bin=binary)
        assert calls == ["claude-fable-5-4"], old
    # The stamp now carries the current rules, so the next pass skips the scan.
    calls.clear()
    await _run(tmp_path, Server(), calls, set(), claude_bin=binary)
    assert calls == []


async def test_seed_asks_a_known_id(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "claude-fable-5-2")
    server, calls = Server(), []
    code = await _run(
        tmp_path, server, calls, {"claude-fable-5-2"}, claude_bin=binary, seed="claude-fable-5-2"
    )
    assert code == 0 and calls == ["claude-fable-5-2"]
    assert server.posts[0]["probes"][0]["id"] == "claude-fable-5-2"


async def test_seed_must_be_a_model_id(tmp_path: Path) -> None:
    code = await _run(tmp_path, Server(), [], set(), claude_bin=_binary(tmp_path), seed="gpt-6")
    assert code == 1


async def test_a_refused_post_fails_the_pass_and_keeps_the_ids_for_next_time(
    tmp_path: Path,
) -> None:
    binary = _binary(tmp_path, "claude-fable-5-4")
    server, calls = Server(status=403), []
    assert await _run(tmp_path, server, calls, {"claude-fable-5-4"}, claude_bin=binary) == 1
    assert not (tmp_path / "state.json").exists()
    assert await _run(tmp_path, Server(), calls, {"claude-fable-5-4"}, claude_bin=binary) == 0
    assert calls == ["claude-fable-5-4"] * 2


async def test_dry_run_posts_nothing(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "claude-fable-5-4")
    server, calls = Server(), []
    assert await _run(tmp_path, server, calls, set(), claude_bin=binary, dry_run=True) == 0
    assert server.posts == []


async def test_a_scan_that_asked_nothing_still_saves_the_binary_stamp(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "claude-fable-5-2")  # known: no candidate
    server, calls = Server(), []
    await _run(tmp_path, server, calls, set(), claude_bin=binary)
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["binary"] is not None


async def test_an_id_stops_being_asked_after_ten_refusals_until_the_binary_changes(
    tmp_path: Path,
) -> None:
    binary = _binary(tmp_path, "claude-mythos-5-2")
    server, calls = Server(), []
    for hours in range(0, 14 * 25, 25):
        await _run(
            tmp_path, server, calls, set(), claude_bin=binary, now=NOW + timedelta(hours=hours)
        )
    assert calls == ["claude-mythos-5-2"] * model_watch_relay.MAX_TRIES
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["candidates"] == [] and state["tries"] == {"claude-mythos-5-2": 10}
    binary = _binary(tmp_path, "claude-mythos-5-2", "claude-mythos-5-3")
    await _run(tmp_path, server, calls, set(), claude_bin=binary, now=NOW + timedelta(days=30))
    assert calls[-1] == "claude-mythos-5-3"
