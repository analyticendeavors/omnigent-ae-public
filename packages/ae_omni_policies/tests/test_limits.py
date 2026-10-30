"""Plan limits: the two translations, the store, the /v1/ae/limits routes and the host relay."""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from tests.server.helpers import create_test_session

from ae_omni_policies import auth, limits, limits_relay, shared_accounts
from ae_omni_policies.limits import LimitSnapshot, LimitStore, claude_snapshot, codex_snapshot
from ae_omni_policies.omni import Settings

NOW = datetime(2026, 9, 24, 16, 0, tzinfo=UTC)
FIVE_H_RESET = int((NOW + timedelta(hours=2)).timestamp())
WEEK_RESET = int((NOW + timedelta(days=3)).timestamp())

# Claude Code's statusLine stdin, trimmed to what matters (the documented
# `rate_limits` block of the 2.1.281 CLI).
STATUSLINE: dict[str, Any] = {
    "context_window": {"context_window_size": 200000, "used_percentage": 12},
    "rate_limits": {
        "five_hour": {"used_percentage": 37.5, "resets_at": FIVE_H_RESET},
        "seven_day": {"used_percentage": 61, "resets_at": WEEK_RESET},
    },
}

# `account/rateLimits/read` as codex app-server answers it
# (codex-rs/app-server-protocol v2/account.rs, GetAccountRateLimitsResponse).
CODEX_RESULT: dict[str, Any] = {
    "ordinaryUsageAllowed": True,
    "rateLimits": {
        "limitId": "codex",
        "limitName": None,
        "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": FIVE_H_RESET},
        "secondary": {"usedPercent": 44, "windowDurationMins": 10080, "resetsAt": WEEK_RESET},
        "planType": "pro",
    },
    "rateLimitsByLimitId": {
        "codex_spark": {
            "limitId": "codex_spark",
            "limitName": "GPT-5.3-Codex-Spark",
            "primary": {"usedPercent": 3, "windowDurationMins": 300, "resetsAt": FIVE_H_RESET},
            "secondary": None,
            "planType": "pro",
        },
        "codex": {
            "limitId": "codex",
            "limitName": None,
            "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": FIVE_H_RESET},
            "secondary": {
                "usedPercent": 44,
                "windowDurationMins": 10080,
                "resetsAt": WEEK_RESET,
            },
            "planType": "pro",
        },
    },
}


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # No state file: the store stays in memory, so nothing writes to /data.
    monkeypatch.delenv(limits.STATE_FILE_ENV, raising=False)
    monkeypatch.setattr(limits, "DATA_DIR", Path("/nonexistent-ae-data"))
    # Nor does the relay read this machine's own Claude accounts or secrets.
    monkeypatch.setattr(
        "omnigent.onboarding.claude_accounts.account_descriptors", list, raising=False
    )
    limits.reset_store()
    yield
    limits.reset_store()


# ── translations ────────────────────────────────────────────────────────


def test_claude_statusline_becomes_two_windows() -> None:
    snap = claude_snapshot(STATUSLINE, NOW, now=NOW)
    assert snap.harness == "claude"
    assert snap.source == "statusline"
    assert [(w.id, w.label, w.used_percent, w.window_minutes) for w in snap.windows] == [
        ("five_hour", "5-hour limit", 37.5, 300),
        ("seven_day", "Weekly, all models", 61.0, 10080),
    ]
    assert snap.windows[0].resets_at == datetime.fromtimestamp(FIVE_H_RESET, tz=UTC)
    assert snap.note is None


def test_claude_drops_a_window_whose_reset_has_passed() -> None:
    payload = json.loads(json.dumps(STATUSLINE))
    payload["rate_limits"]["five_hour"]["resets_at"] = int(
        (NOW - timedelta(minutes=1)).timestamp()
    )
    snap = claude_snapshot(payload, NOW, now=NOW)
    assert [w.id for w in snap.windows] == ["seven_day"]


@pytest.mark.parametrize(
    "rate_limits",
    [None, {}, {"five_hour": {"used_percentage": "lots"}}, "not an object"],
)
def test_claude_without_limits_says_why(rate_limits: Any) -> None:
    payload: dict[str, Any] = {"context_window": {"context_window_size": 200000}}
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    snap = claude_snapshot(payload, NOW, now=NOW)
    assert snap.windows == []
    assert snap.note is not None and "API key" in snap.note


def test_codex_multi_bucket_puts_codex_first_and_names_the_rest() -> None:
    snap = codex_snapshot(CODEX_RESULT, NOW)
    assert snap.plan == "pro"
    assert [(w.id, w.label, w.used_percent) for w in snap.windows] == [
        ("codex.primary", "5-hour limit", 12.0),
        ("codex.secondary", "Weekly limit", 44.0),
        ("codex_spark.primary", "GPT-5.3-Codex-Spark: 5-hour limit", 3.0),
    ]
    assert snap.windows[1].resets_at == datetime.fromtimestamp(WEEK_RESET, tz=UTC)
    assert snap.note is None


def test_codex_single_bucket_fallback_and_odd_durations() -> None:
    result = {
        "rateLimits": {
            "primary": {"usedPercent": 5, "windowDurationMins": 90, "resetsAt": None},
            "secondary": {"usedPercent": 9, "windowDurationMins": 2880, "resetsAt": WEEK_RESET},
        }
    }
    snap = codex_snapshot(result, NOW)
    assert [(w.label, w.resets_at is None) for w in snap.windows] == [
        ("90-minute limit", True),
        ("2-day limit", False),
    ]


def test_codex_without_limits_or_usage_blocked() -> None:
    assert codex_snapshot({"rateLimits": {}}, NOW).note is not None
    blocked = dict(CODEX_RESULT, ordinaryUsageAllowed=False)
    assert "not allowed" in (codex_snapshot(blocked, NOW).note or "")


# ── the store ───────────────────────────────────────────────────────────


def test_store_keeps_the_newest_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "state" / "limits.json"
    store = LimitStore(path)
    assert store.view(now=NOW)["harnesses"] == {"claude": None, "codex": None}
    newer = claude_snapshot(STATUSLINE, NOW, now=NOW)
    older = claude_snapshot(STATUSLINE, NOW - timedelta(minutes=5), now=NOW)
    assert store.put(newer, now=NOW) is True
    assert store.put(older, now=NOW) is False
    view = store.view(now=NOW)
    assert view["harnesses"]["claude"]["observed_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert view["harnesses"]["claude"]["received_at"] == NOW.isoformat()
    # A new process reads the file back.
    reloaded = LimitStore(path).view(now=NOW)
    assert reloaded["harnesses"]["claude"]["windows"] == view["harnesses"]["claude"]["windows"]
    assert reloaded["harnesses"]["codex"] is None


def test_store_ignores_an_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "limits.json"
    path.write_text("{not json", encoding="utf-8")
    assert LimitStore(path).view(now=NOW)["harnesses"]["claude"] is None


def test_default_path_follows_env_then_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert limits._default_path() is None
    monkeypatch.setattr(limits, "DATA_DIR", tmp_path)
    assert limits._default_path() == tmp_path / "ae-limits.json"
    monkeypatch.setenv(limits.STATE_FILE_ENV, str(tmp_path / "x.json"))
    assert limits._default_path() == tmp_path / "x.json"


def test_store_survives_a_restart_without_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "DATA_DIR", tmp_path)
    limits.store().put(codex_snapshot(CODEX_RESULT, NOW), now=NOW)
    limits.reset_store()
    assert limits.store().view(now=NOW)["harnesses"]["codex"] is not None


# ── the routes ──────────────────────────────────────────────────────────


async def test_routes_round_trip(client: httpx.AsyncClient) -> None:
    empty = await client.get("/v1/ae/limits")
    assert empty.status_code == 200, empty.text
    assert empty.json()["harnesses"] == {"claude": None, "codex": None}

    body = codex_snapshot(CODEX_RESULT, NOW).model_dump(mode="json")
    posted = await client.post("/v1/ae/limits/codex", json=body)
    assert posted.status_code == 200, posted.text
    assert posted.json() == {"kept": True, "harness": "codex"}

    view = (await client.get("/v1/ae/limits")).json()["harnesses"]
    assert view["claude"] is None
    assert view["codex"]["plan"] == "pro"
    assert [w["used_percent"] for w in view["codex"]["windows"]] == [12.0, 44.0, 3.0]


async def test_route_rejects_a_mismatch_and_a_bad_body(client: httpx.AsyncClient) -> None:
    body = claude_snapshot(STATUSLINE, NOW, now=NOW).model_dump(mode="json")
    mismatch = await client.post("/v1/ae/limits/codex", json=body)
    assert mismatch.status_code == 400
    assert mismatch.json()["error"]["code"] == "invalid_input"
    bad = dict(body, windows=[{"id": "x", "label": "x", "used_percent": -1}])
    assert (await client.post("/v1/ae/limits/claude", json=bad)).status_code == 422
    assert (await client.post("/v1/ae/limits/gemini", json=body)).status_code == 422


async def test_routes_need_an_identity_off_single_user(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    assert (await client.get("/v1/ae/limits")).status_code == 401
    body = claude_snapshot(STATUSLINE, NOW, now=NOW).model_dump(mode="json")
    assert (await client.post("/v1/ae/limits/claude", json=body)).status_code == 401


# ── the relay ───────────────────────────────────────────────────────────

FAKE_CODEX = r"""#!{python}
import json, sys
assert sys.argv[1:] == ["app-server"], sys.argv
mode = {mode!r}
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "initialize":
        print(json.dumps({{"id": msg["id"], "result": {{"userAgent": "fake"}}}}), flush=True)
    elif msg.get("method") == "account/rateLimits/read":
        assert msg["params"] == {{"excludeResetCreditDetails": True}}
        # A notification first: the relay must skip it.
        print(json.dumps({{"method": "account/updated", "params": {{}}}}), flush=True)
        if mode == "error":
            error = {{"code": -32600, "message": "not logged in"}}
            print(json.dumps({{"id": msg["id"], "error": error}}), flush=True)
        else:
            print(json.dumps({{"id": msg["id"], "result": json.loads({result!r})}}), flush=True)
"""


def _fake_codex(tmp_path: Path, mode: str = "ok") -> str:
    path = tmp_path / f"codex-{mode}"
    path.write_text(
        FAKE_CODEX.format(python=sys.executable, mode=mode, result=json.dumps(CODEX_RESULT)),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _statusline_file(tmp_path: Path) -> Path:
    # The relay reads the real clock, so the resets are relative to it: a fixed
    # time would drop the 5-hour window once the wall clock passes it.
    now = int(datetime.now(UTC).timestamp())
    payload = json.loads(json.dumps(STATUSLINE))
    payload["rate_limits"]["five_hour"]["resets_at"] = now + 2 * 3600
    payload["rate_limits"]["seven_day"]["resets_at"] = now + 3 * 86400
    path = tmp_path / "claude-statusline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_read_claude_uses_the_file_time(tmp_path: Path) -> None:
    path = _statusline_file(tmp_path)
    stamp = FIVE_H_RESET - 3600
    os.utime(path, (stamp, stamp))
    snap = limits_relay.read_claude(path, now=NOW)
    assert snap is not None
    assert snap.observed_at == datetime.fromtimestamp(stamp, tz=UTC)
    assert limits_relay.read_claude(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("[1]", encoding="utf-8")
    with pytest.raises(limits_relay.RelayError):
        limits_relay.read_claude(tmp_path / "bad.json")


async def test_read_codex_speaks_app_server(tmp_path: Path) -> None:
    snap = await limits_relay.read_codex(_fake_codex(tmp_path))
    assert snap.harness == "codex"
    assert [w.id for w in snap.windows] == [
        "codex.primary",
        "codex.secondary",
        "codex_spark.primary",
    ]


async def test_read_codex_reports_a_refusal_and_a_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(limits_relay.RelayError, match="not logged in"):
        await limits_relay.read_codex(_fake_codex(tmp_path, "error"))
    with pytest.raises(limits_relay.RelayError, match="could not start"):
        await limits_relay.read_codex(str(tmp_path / "no-such-codex"))


async def test_relay_posts_both_to_the_app(app: FastAPI, tmp_path: Path) -> None:
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=_statusline_file(tmp_path),
        codex_bin=_fake_codex(tmp_path),
        settings=Settings(server_url="http://test"),
        transport=httpx.ASGITransport(app=app),
        out=out,
    )
    assert status == 0, out.getvalue()
    assert "claude: posted (5-hour limit 38%, Weekly, all models 61%)" in out.getvalue()
    view = limits.store().view()["harnesses"]
    assert view["claude"]["source"] == "statusline"
    assert view["codex"]["source"] == "app-server"


async def test_relay_keeps_going_when_codex_fails(app: FastAPI, tmp_path: Path) -> None:
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=_statusline_file(tmp_path),
        codex_bin=_fake_codex(tmp_path, "error"),
        settings=Settings(server_url="http://test"),
        transport=httpx.ASGITransport(app=app),
        out=out,
    )
    # A Codex miss is logged, not a failure, while the Claude reading posts.
    assert status == 0, out.getvalue()
    assert "codex: codex app-server refused" in out.getvalue()
    assert "claude: posted" in out.getvalue()
    assert limits.store().view()["harnesses"]["claude"] is not None


async def test_relay_fails_when_codex_misses_and_nothing_else_posts(tmp_path: Path) -> None:
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=tmp_path / "missing.json",
        codex_bin=_fake_codex(tmp_path, "error"),
        settings=Settings(server_url="http://test"),
        out=out,
    )
    assert status == 1
    assert "codex: codex app-server refused" in out.getvalue()


# A CLI that never answers. The launcher exits at once and leaves a child
# holding its stdio pipes: the shape that, after a Codex timeout on the box
# (2026-09-24), left the subprocess transport open until asyncio.run had closed
# the loop, so its __del__ raised "Event loop is closed" and the unit failed.
FAKE_SILENT = r"""#!{python}
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
with open({pidfile!r}, "w", encoding="utf-8") as f:
    f.write(str(child.pid))
"""

# Runs in a fresh interpreter so asyncio.run really closes its loop, as the
# unit's does; ResourceWarning is an error so an unclosed transport cannot pass.
RELAY_ONCE = r"""
import asyncio, sys
from pathlib import Path
import httpx
from ae_omni_policies import limits_relay
from ae_omni_policies.omni import Settings

limits_relay.CODEX_TIMEOUT_S = 0.5
if sys.argv[1] == "claude-probe":
    try:
        asyncio.run(limits_relay.read_claude_usage(sys.argv[2], timeout=0.5))
    except limits_relay.RelayError as exc:
        print(f"claude: {exc}")
    sys.exit(0)
status = asyncio.run(
    limits_relay.run(
        claude_path=Path(sys.argv[1]),
        codex_bin=sys.argv[2],
        claude_bin=None,
        settings=Settings(server_url="http://test", token="t"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
)
sys.exit(status)
"""


def _fake_silent(tmp_path: Path, name: str) -> tuple[str, Path]:
    pidfile = tmp_path / f"{name}.pid"
    path = tmp_path / name
    path.write_text(
        FAKE_SILENT.format(python=sys.executable, pidfile=str(pidfile)), encoding="utf-8"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path), pidfile


def _relay_once(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning", "-c", RELAY_ONCE, *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _assert_killed(pidfile: Path) -> None:
    """The launcher's child went with it (the whole process group is killed)."""
    proc_stat = Path("/proc") / pidfile.read_text(encoding="utf-8") / "stat"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            state = proc_stat.read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
        except OSError:
            return
        if state == "Z":
            return
        time.sleep(0.05)
    raise AssertionError(f"{proc_stat.parent} still runs after the relay exited")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_codex_timeout_is_a_soft_miss_with_no_closed_loop_traceback(tmp_path: Path) -> None:
    codex, pidfile = _fake_silent(tmp_path, "codex-silent")
    done = _relay_once(str(_statusline_file(tmp_path)), codex)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "codex: codex app-server did not answer within" in done.stdout
    assert "claude: posted" in done.stdout
    assert "Event loop is closed" not in done.stderr
    assert "Exception ignored" not in done.stderr
    assert done.stderr == ""
    _assert_killed(pidfile)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc")
def test_claude_probe_timeout_leaves_no_transport_open(tmp_path: Path) -> None:
    claude, pidfile = _fake_silent(tmp_path, "claude-silent")
    done = _relay_once("claude-probe", claude)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "claude: claude did not answer get_usage within" in done.stdout
    assert done.stderr == ""
    _assert_killed(pidfile)


async def test_relay_with_nothing_to_read_posts_nothing(tmp_path: Path) -> None:
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=tmp_path / "missing.json",
        codex_bin=None,
        settings=Settings(),
        out=out,
    )
    assert status == 0
    assert "no status line yet" in out.getvalue()
    assert "no codex on PATH" in out.getvalue()


async def test_relay_names_the_fix_without_a_server(tmp_path: Path) -> None:
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=_statusline_file(tmp_path), codex_bin=None, settings=Settings(), out=out
    )
    assert status == 1
    assert "omnigent login" in out.getvalue()


async def test_relay_reports_an_expired_login(tmp_path: Path) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "expired"})

    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=_statusline_file(tmp_path),
        codex_bin=None,
        settings=Settings(server_url="http://test", token="stale"),
        transport=httpx.MockTransport(answer),
        out=out,
    )
    assert status == 1
    assert "run `omnigent login http://test`" in out.getvalue()


def test_dry_run_prints_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    status = limits_relay.main(
        [
            "--claude-file",
            str(_statusline_file(tmp_path)),
            "--claude-bridges",
            str(tmp_path / "no-bridges"),
            "--claude-bin",
            _fake_claude(tmp_path, "noscope"),
            "--codex-bin",
            _fake_codex(tmp_path),
            "--dry-run",
        ]
    )
    assert status == 0
    printed = capsys.readouterr().out
    assert '"harness": "claude"' in printed and '"harness": "codex"' in printed
    LimitSnapshot.model_validate(
        json.loads(printed[printed.index("{") : printed.index("\n}") + 2])
    )


def test_statusline_script_saves_stdin_and_chains(tmp_path: Path) -> None:
    import subprocess

    script = Path(__file__).resolve().parents[3] / "deploy" / "ae-limits-statusline.sh"
    target = tmp_path / "ae" / "claude-statusline.json"
    env = {**os.environ, "AE_LIMITS_CLAUDE_FILE": str(target)}
    done = subprocess.run(
        [str(script)],
        input=json.dumps(STATUSLINE),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    assert done.stdout == ""
    assert json.loads(target.read_text(encoding="utf-8")) == STATUSLINE
    chained = subprocess.run(
        [str(script), "wc", "-c"], input="{}", text=True, capture_output=True, env=env, check=True
    )
    assert chained.stdout.strip() == "2"
    assert target.read_text(encoding="utf-8") == "{}"
    # An empty stdin never clobbers the last good reading.
    subprocess.run([str(script)], input="", text=True, env=env, check=True)
    assert target.read_text(encoding="utf-8") == "{}"


# ── second pass (2026-09-24): bridge captures, get_usage, the merged rows ──

# `get_usage` as the 2.1.282 CLI answers it for a Max login with the profile
# scope (the control response body; values illustrative, not the owner's).
USAGE_REPLY: dict[str, Any] = {
    "session": {"total_cost_usd": 0, "model_usage": {}},
    "subscription_type": "max",
    "rate_limits_available": True,
    "rate_limits": {
        "five_hour": {"utilization": 41.0, "resets_at": "2026-09-24T18:00:00Z"},
        "seven_day": {"utilization": 63.0, "resets_at": "2026-09-27T16:00:00+00:00"},
        "seven_day_opus": None,
        "model_scoped": [
            {"display_name": "Fable", "utilization": 22.0, "resets_at": "2026-09-27T16:00:00Z"},
            {"display_name": "Sonnet", "utilization": None, "resets_at": None},
        ],
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": 40000,
            "used_credits": 7881,
            "utilization": 19.7,
            "currency": "USD",
        },
    },
    "behaviors": None,
}

FAKE_CLAUDE = r"""#!{python}
import json, os, sys
assert sys.argv[1] == "-p" and "--no-session-persistence" in sys.argv, sys.argv
assert sys.argv[sys.argv.index("--setting-sources") + 1] == "", sys.argv
mode = {mode!r}
for line in sys.stdin:
    msg = json.loads(line)
    request = msg["request"]
    assert msg["type"] == "control_request" and request["subtype"] == "get_usage", msg
    assert request["skip_behaviors"] is True
    # Noise first: the relay must skip lines that are not its answer.
    print(json.dumps({{"type": "system", "subtype": "init"}}), flush=True)
    if "CLAUDE_CODE_OAUTH_TOKEN" in os.environ or "ANTHROPIC_API_KEY" in os.environ:
        body = {{"subtype": "error", "request_id": msg["request_id"], "error": "token leaked"}}
    elif mode == "error":
        body = {{"subtype": "error", "request_id": msg["request_id"], "error": "no"}}
    elif mode == "noscope":
        reply = {{"subscription_type": None, "rate_limits_available": False, "rate_limits": None}}
        body = {{"subtype": "success", "request_id": msg["request_id"], "response": reply}}
    else:
        reply = json.loads({reply!r})
        body = {{"subtype": "success", "request_id": msg["request_id"], "response": reply}}
    print(json.dumps({{"type": "control_response", "response": body}}), flush=True)
"""


def _live_usage_reply() -> dict[str, Any]:
    """USAGE_REPLY with its resets moved ahead of the real clock.

    For tests that run the relay, which reads the real clock: the fixed resets
    in USAGE_REPLY passed on 2026-09-27 16:00 UTC, the relay then dropped the
    Fable and weekly windows, and test_relay_posts_the_full_claude_panel failed
    on main from that day (2026-09-28).
    """
    now = datetime.now(UTC)
    reply = json.loads(json.dumps(USAGE_REPLY))
    rate = reply["rate_limits"]
    rate["five_hour"]["resets_at"] = (now + timedelta(hours=2)).isoformat()
    rate["seven_day"]["resets_at"] = (now + timedelta(days=3)).isoformat()
    rate["model_scoped"][0]["resets_at"] = (now + timedelta(days=3)).isoformat()
    return reply


def _fake_claude(tmp_path: Path, mode: str = "ok", reply: dict[str, Any] | None = None) -> str:
    path = tmp_path / f"claude-{mode}"
    body = json.dumps(USAGE_REPLY if reply is None else reply)
    path.write_text(
        FAKE_CLAUDE.format(python=sys.executable, mode=mode, reply=body),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _capture(
    root: Path,
    name: str,
    conversation: str | None,
    payload: dict[str, Any],
    age_s: int,
    *,
    account: str | None = None,
) -> Path:
    """A claude-native bridge directory as the harness leaves it."""
    bridge = root / name
    bridge.mkdir(parents=True)
    if account is not None:
        (bridge / "claude_account").write_text(account, encoding="utf-8")
    if conversation is not None:
        config = {"conversation_id": conversation, "token": "never-read-or-posted"}
        (bridge / "bridge.json").write_text(json.dumps(config), encoding="utf-8")
    raw = bridge / "context_raw.json"
    raw.write_text(json.dumps(payload), encoding="utf-8")
    stamp = datetime.now(UTC).timestamp() - age_s
    os.utime(raw, (stamp, stamp))
    return raw


def _session_payload(input_tokens: int, cache_read: int, *, limits: bool = True) -> dict[str, Any]:
    now = int(datetime.now(UTC).timestamp())
    payload: dict[str, Any] = {
        "model": {"id": "claude-x", "display_name": "Fable"},
        "context_window": {
            "context_window_size": 1_000_000,
            "current_usage": {
                "input_tokens": input_tokens,
                "output_tokens": 900,
                "cache_creation_input_tokens": 1_800,
                "cache_read_input_tokens": cache_read,
            },
        },
    }
    if limits:
        payload["rate_limits"] = {
            "five_hour": {"used_percentage": 12, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 50, "resets_at": now + 86400},
        }
    return payload


def test_session_context_sums_the_three_input_kinds() -> None:
    ctx = limits.session_context(_session_payload(5_000, 210_000), "conv_a", NOW)
    assert ctx is not None
    assert (ctx.used_tokens, ctx.input_tokens, ctx.cache_write_tokens, ctx.cache_read_tokens) == (
        216_800,
        5_000,
        1_800,
        210_000,
    )
    assert (ctx.window_size, ctx.model) == (1_000_000, "Fable")
    before_first_reply = {"context_window": {"context_window_size": 200000, "current_usage": None}}
    assert limits.session_context(before_first_reply, "conv_a", NOW) is None
    assert limits.session_context({}, "conv_a", NOW) is None


def test_read_claude_finds_a_session_that_never_chained(tmp_path: Path) -> None:
    # The box on 2026-09-24: the statusLine was installed after the session
    # started, so ~/.omnigent/ae/claude-statusline.json was never written, but
    # the harness's own capture was.
    bridges = tmp_path / "claude-native"
    _capture(bridges, "aaa", "conv_old", _session_payload(5_000, 210_000), age_s=30)
    _capture(bridges, "bbb", "conv_idle", _session_payload(100, 9_000, limits=False), age_s=5)
    _capture(bridges, "ccc", None, _session_payload(1, 1), age_s=1)  # no bridge.json
    (bridges / "ddd").mkdir()  # a bridge that never rendered
    (bridges / "eee").mkdir()
    (bridges / "eee" / "context_raw.json").write_text("{half", encoding="utf-8")
    snap = limits_relay.read_claude(tmp_path / "never-written.json", bridges=bridges)
    assert snap is not None
    # The newest capture WITH rate_limits gives the windows, not the newest overall.
    assert [(w.id, w.used_percent) for w in snap.windows] == [
        ("five_hour", 12.0),
        ("seven_day", 50.0),
    ]
    assert [(c.conversation_id, c.used_tokens) for c in snap.contexts] == [
        ("conv_idle", 10_900),
        ("conv_old", 216_800),
    ]
    assert "never-read-or-posted" not in snap.model_dump_json()
    assert limits_relay.read_claude(tmp_path / "x.json", bridges=tmp_path / "none") is None


def _running(bridges: Path, name: str, conversation: str | None, pid: int) -> Path:
    """A bridge directory of a running session that has not rendered a status line."""
    root = bridges / name
    root.mkdir(parents=True)
    if conversation is not None:
        (root / "bridge.json").write_text(
            json.dumps({"conversation_id": conversation}), encoding="utf-8"
        )
    (root / "owner.pid").write_text(str(pid), encoding="utf-8")
    return root


def test_scan_live_counts_running_sessions_without_a_capture(tmp_path: Path) -> None:
    bridges = tmp_path / "claude-native"
    _running(bridges, "aaa", "conv_new", os.getpid())
    backup = _running(bridges, "bbb", "conv_backup", os.getpid())
    (backup / "claude_account").write_text("work", encoding="utf-8")
    _running(bridges, "ccc", "conv_dead", 2**22 + 12345)  # no such process
    _running(bridges, "ddd", None, os.getpid())  # no bridge.json
    (bridges / "eee").mkdir()  # never owned
    live = limits_relay.scan_live(bridges)
    assert sorted((c, a) for c, _, a in live) == [("conv_backup", "work"), ("conv_new", None)]
    assert limits_relay.scan_live(tmp_path / "none") == []
    # A bridge the harness stamped at launch names its account before any render.
    snap = limits_relay.with_live(claude_snapshot({"rate_limits": {}}, NOW), live)
    assert snap is not None
    by_id = {s.conversation_id: s.account for s in snap.sessions}
    assert by_id == {"conv_backup": "work", "conv_new": None}


def test_with_live_makes_the_default_login_count_without_any_capture(tmp_path: Path) -> None:
    bridges = tmp_path / "claude-native"
    _running(bridges, "aaa", "conv_new", os.getpid())
    _capture(bridges, "bbb", "conv_seen", _session_payload(1, 1), age_s=5)
    snap = claude_snapshot({"rate_limits": {}}, NOW)
    snap = limits_relay.with_live(snap, limits_relay.scan_live(bridges))
    assert snap is not None
    assert [s.conversation_id for s in snap.sessions] == ["conv_new"]
    assert snap.sessions[0].windows == []
    assert snap.sessions[0].observed_at <= snap.observed_at
    store = LimitStore()
    store.put(snap)
    rows = {r.account: r for r in store.accounts({})}
    assert rows[None].live_sessions == 1
    assert limits_relay.with_live(None, [("c", NOW, None)]) is None


def test_claude_bridges_default_is_upstreams_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(limits_relay.CLAUDE_BRIDGES_ENV, raising=False)
    root = limits_relay.claude_bridges()
    assert root.parts[-2:] == (f"omnigent-{os.getuid()}", "claude-native")
    assert limits_relay.claude_bridges({"AE_LIMITS_CLAUDE_BRIDGES": "/x/y"}) == Path("/x/y")


def test_with_usage_adds_model_rows_credits_and_plan() -> None:
    base = claude_snapshot(STATUSLINE, NOW, now=NOW)
    merged = limits.with_usage(base, USAGE_REPLY, NOW, now=NOW)
    assert merged is not None
    assert merged.source == "statusline+usage"
    assert merged.plan == "Max"
    assert [(w.id, w.label, w.used_percent) for w in merged.windows] == [
        ("five_hour", "5-hour limit", 41.0),
        ("seven_day", "Weekly, all models", 63.0),
        ("seven_day.model.fable", "Weekly · Fable", 22.0),
    ]
    assert merged.credits == limits.Credits(
        enabled=True, used_minor=7881, limit_minor=40000, used_percent=19.7, currency="USD"
    )
    assert merged.hint is None
    labelled = limits.with_usage(None, USAGE_REPLY, NOW, plan_label="Max 20x", now=NOW)
    assert labelled is not None and labelled.plan == "Max 20x" and labelled.source == "usage"


def test_with_usage_falls_back_to_the_legacy_model_rows() -> None:
    reply = json.loads(json.dumps(USAGE_REPLY))
    del reply["rate_limits"]["model_scoped"]
    reply["rate_limits"]["seven_day_opus"] = {"utilization": 8, "resets_at": None}
    merged = limits.with_usage(None, reply, NOW, now=NOW)
    assert merged is not None
    assert [w.label for w in merged.windows][-1] == "Weekly · Opus"


def test_with_usage_without_the_profile_scope_keeps_the_statusline_and_hints() -> None:
    base = claude_snapshot(STATUSLINE, NOW, now=NOW)
    for reply in (None, {"rate_limits_available": False, "rate_limits": None}):
        merged = limits.with_usage(base, reply, NOW, now=NOW)
        assert merged is not None
        assert merged.windows == base.windows and merged.credits is None
        assert merged.hint == limits.PROFILE_HINT
    assert limits.with_usage(None, None, NOW, now=NOW) is None


async def test_read_claude_usage_strips_the_setup_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The host's env carries the inference-only setup token; the probe must
    # not pass it on, or the CLI would prefer it over the stored login.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-test-only")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only")
    reply = await limits_relay.read_claude_usage(_fake_claude(tmp_path))
    assert reply["rate_limits_available"] is True
    with pytest.raises(limits_relay.RelayError, match="refused get_usage: no"):
        await limits_relay.read_claude_usage(_fake_claude(tmp_path, "error"))
    with pytest.raises(limits_relay.RelayError, match="could not start"):
        await limits_relay.read_claude_usage(str(tmp_path / "no-such-claude"))


async def test_relay_posts_the_full_claude_panel(app: FastAPI, tmp_path: Path) -> None:
    bridges = tmp_path / "claude-native"
    _capture(bridges, "aaa", "conv_a", _session_payload(5_000, 210_000), age_s=10)
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=tmp_path / "never-written.json",
        codex_bin=None,
        bridges=bridges,
        claude_bin=_fake_claude(tmp_path, reply=_live_usage_reply()),
        plan_label="Max 20x",
        settings=Settings(server_url="http://test"),
        transport=httpx.ASGITransport(app=app),
        out=out,
    )
    assert status == 0, out.getvalue()
    assert "Weekly · Fable 22%" in out.getvalue()
    claude = limits.store().view()["harnesses"]["claude"]
    assert claude["plan"] == "Max 20x"
    assert claude["credits"]["used_minor"] == 7881
    assert claude["contexts"][0]["conversation_id"] == "conv_a"
    assert claude["hint"] is None


async def test_relay_without_a_profile_login_still_posts_with_a_hint(
    app: FastAPI, tmp_path: Path
) -> None:
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=_statusline_file(tmp_path),
        codex_bin=None,
        claude_bin=_fake_claude(tmp_path, "noscope"),
        settings=Settings(server_url="http://test"),
        transport=httpx.ASGITransport(app=app),
        out=out,
    )
    assert status == 0, out.getvalue()
    assert "claude auth login" in out.getvalue()
    claude = limits.store().view()["harnesses"]["claude"]
    assert [w["id"] for w in claude["windows"]] == ["five_hour", "seven_day"]
    assert claude["hint"] == limits.PROFILE_HINT


async def test_relay_probe_failure_is_reported_not_fatal(app: FastAPI, tmp_path: Path) -> None:
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=_statusline_file(tmp_path),
        codex_bin=None,
        claude_bin=_fake_claude(tmp_path, "error"),
        settings=Settings(server_url="http://test"),
        transport=httpx.ASGITransport(app=app),
        out=out,
    )
    assert status == 0, out.getvalue()
    assert "claude: claude refused get_usage" in out.getvalue()
    assert limits.store().view()["harnesses"]["claude"]["hint"] == limits.PROFILE_HINT


# ── per-session view (backup accounts) ──────────────────────────────────


def _window(window_id: str, used: float) -> limits.LimitWindow:
    return limits.LimitWindow(id=window_id, label=window_id, used_percent=used)


def _host(source: str, *sessions: limits.SessionLimits) -> LimitSnapshot:
    return LimitSnapshot(
        harness="claude",
        source=source,
        observed_at=NOW,
        plan="Max",
        windows=[
            _window("five_hour", 0),
            _window("seven_day", 100),
            _window("seven_day.model.fable", 9),
        ],
        credits=limits.Credits(enabled=True),
        note=None,
        hint=limits.PROFILE_HINT,
        sessions=list(sessions),
    )


def _own(conversation: str, *, account: str | None = None, windows: Any = ()) -> Any:
    return limits.SessionLimits(
        conversation_id=conversation,
        observed_at=NOW - timedelta(minutes=3),
        account=account,
        account_label=f"{account} label" if account else None,
        windows=list(windows),
    )


def test_new_fields_round_trip_and_an_old_relay_still_validates() -> None:
    own = _own("conv_b", account="Backup", windows=[_window("five_hour", 25)])
    snap = _host("statusline", own).model_copy(update={"account": "Backup"})
    again = LimitSnapshot.model_validate(snap.model_dump(mode="json"))
    assert again == snap
    assert again.sessions[0].windows[0].used_percent == 25
    old = claude_snapshot(STATUSLINE, NOW, now=NOW).model_dump(mode="json")
    for key in ("account", "account_label", "sessions"):
        old.pop(key)
    parsed = LimitSnapshot.model_validate(old)
    assert (parsed.account, parsed.sessions) == (None, [])
    five = [_window(f"w{i}", 1).model_dump() for i in range(5)]
    with pytest.raises(ValueError):
        limits.SessionLimits.model_validate(dict(own.model_dump(mode="json"), windows=five))


def test_session_view_never_borrows_the_other_accounts_numbers() -> None:
    host = _host("statusline+usage", _own("conv_quiet"), _own("conv_idle", account="Backup"))
    assert limits.session_view(host, "conv_missing") is host
    assert limits.session_view(host, "conv_quiet") is host
    # A backup session with no windows of its own gets none, not the default login's.
    idle = limits.session_view(host, "conv_idle")
    assert (idle.windows, idle.account, idle.note) == ([], "Backup", limits.NO_WINDOWS_NOTE)
    # Nor does a default-login session get a backup's when only a backup had a reading.
    backup_host = host.model_copy(update={"account": "Backup", "account_label": "Backup label"})
    quiet = limits.session_view(backup_host, "conv_quiet")
    assert (quiet.windows, quiet.account, quiet.note) == ([], None, limits.NO_WINDOWS_NOTE)


def test_session_view_backup_account_shows_its_own_windows_only() -> None:
    # The 2026-10-17 evidence: the session's status line said 25% and 4%, the
    # route answered the default login's 0% and 100%.
    windows = [_window("five_hour", 25), _window("seven_day", 4)]
    own = _own("conv_b", account="Backup", windows=windows)
    other = _own("conv_d", windows=[_window("five_hour", 1)])
    host = _host("statusline+usage", other, own)
    view = limits.session_view(host, "conv_b")
    assert [(w.id, w.used_percent) for w in view.windows] == [("five_hour", 25), ("seven_day", 4)]
    assert (view.source, view.observed_at) == ("statusline", own.observed_at)
    assert (view.plan, view.credits, view.note) == (None, None, None)
    assert view.hint == limits.BACKUP_HINT
    assert (view.account, view.account_label) == ("Backup", "Backup label")
    assert view.sessions == host.sessions
    assert host.windows[0].used_percent == 0  # the stored reading is untouched


def test_session_view_default_login_keeps_the_host_reading() -> None:
    # The host reading is the default login's own, probed or from its newest
    # status line, so an idle session's older windows never replace it (nor
    # borrow its newer time).
    own = _own("conv_d", windows=[_window("five_hour", 25), _window("seven_day", 4)])
    for source in ("statusline+usage", "usage", "statusline"):
        host = _host(source, own)
        assert limits.session_view(host, "conv_d") is host


def test_session_view_default_login_replaces_a_backup_host_reading() -> None:
    # Only a backup session had a reading for the host, so it names that
    # account; a default-login session shows its own windows at their own time.
    own = _own("conv_d", windows=[_window("five_hour", 25), _window("seven_day", 4)])
    host = _host("statusline", own).model_copy(
        update={"account": "Backup", "account_label": "Backup label", "plan": None}
    )
    view = limits.session_view(host, "conv_d")
    assert [(w.id, w.used_percent) for w in view.windows] == [("five_hour", 25), ("seven_day", 4)]
    assert (view.observed_at, view.account, view.account_label) == (own.observed_at, None, None)
    assert (view.source, view.hint) == ("statusline", limits.PROFILE_HINT)


def test_store_keeps_a_post_whose_sessions_moved_on() -> None:
    store = LimitStore(None)
    assert store.put(_host("statusline", _own("conv_b", account="Backup")))
    # The host reading falls back to an idle default session's older status
    # line, but the backup session has rendered since: the post is newer.
    moved = _own("conv_b", account="Backup").model_copy(
        update={"observed_at": NOW + timedelta(minutes=1)}
    )
    older_host = _host("statusline", moved).model_copy(
        update={"observed_at": NOW - timedelta(minutes=10)}
    )
    assert store.put(older_host)
    # A delayed retry, older in every part, still cannot roll the panel back.
    stale = _host("statusline", _own("conv_b", account="Backup")).model_copy(
        update={"observed_at": NOW - timedelta(minutes=20)}
    )
    assert not store.put(stale)


def _backup_payload() -> dict[str, Any]:
    payload = _session_payload(7, 70)
    payload["rate_limits"]["five_hour"]["used_percentage"] = 25
    payload["rate_limits"]["seven_day"]["used_percentage"] = 4
    return payload


def test_read_claude_records_each_sessions_account_and_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.claude_accounts.account_label",
        lambda name: {"Backup": "AE Backup"}.get(name, name),
    )
    bridges = tmp_path / "claude-native"
    _capture(bridges, "aaa", "conv_default", _session_payload(5_000, 210_000), age_s=60)
    _capture(bridges, "bbb", "conv_backup", _backup_payload(), age_s=5, account="Backup\n")
    _capture(bridges, "ccc", "conv_blank", _session_payload(1, 1), age_s=90, account="  ")
    # After /clear the status line keeps its limits but has no context yet.
    cleared = _backup_payload()
    cleared["context_window"]["current_usage"] = None
    _capture(bridges, "ddd", "conv_cleared", cleared, age_s=2, account="Backup")
    snap = limits_relay.read_claude(tmp_path / "never-written.json", bridges=bridges)
    assert snap is not None
    # The newer backup captures do not stand in for the default login's windows.
    assert [(w.id, w.used_percent) for w in snap.windows] == [
        ("five_hour", 12.0),
        ("seven_day", 50.0),
    ]
    assert snap.account is None
    by_id = {s.conversation_id: s for s in snap.sessions}
    backup = by_id["conv_backup"]
    assert (backup.account, backup.account_label) == ("Backup", "AE Backup")
    assert [(w.id, w.used_percent) for w in backup.windows] == [
        ("five_hour", 25.0),
        ("seven_day", 4.0),
    ]
    assert (by_id["conv_default"].account, by_id["conv_default"].account_label) == (None, None)
    assert by_id["conv_blank"].account is None
    assert [w.used_percent for w in by_id["conv_default"].windows] == [12.0, 50.0]
    assert "conv_cleared" not in {c.conversation_id for c in snap.contexts}
    for session in ("conv_backup", "conv_cleared"):
        view = limits.session_view(snap, session)
        assert ([w.used_percent for w in view.windows], view.account) == ([25.0, 4.0], "Backup")


def test_read_claude_falls_back_to_a_backup_reading_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An omnigent without account_label: the label is the name.
    monkeypatch.delattr("omnigent.onboarding.claude_accounts.account_label")
    bridges = tmp_path / "claude-native"
    _capture(bridges, "aaa", "conv_idle", _session_payload(1, 1, limits=False), age_s=1)
    _capture(bridges, "bbb", "conv_backup", _backup_payload(), age_s=30, account="B" * 200)
    snap = limits_relay.read_claude(tmp_path / "never-written.json", bridges=bridges)
    assert snap is not None
    assert [w.used_percent for w in snap.windows] == [25.0, 4.0]
    # The only reading is a backup account's, and the snapshot says whose.
    assert (snap.account, snap.account_label) == ("B" * 120, "B" * 120)
    by_id = {s.conversation_id: s for s in snap.sessions}
    assert by_id["conv_backup"].account == "B" * 120
    assert by_id["conv_idle"].windows == []


def test_read_claude_reads_the_chained_file_only_without_captures(tmp_path: Path) -> None:
    bridges = tmp_path / "claude-native"
    chained = tmp_path / "claude-statusline.json"
    outside = _session_payload(1, 1)
    outside["rate_limits"]["five_hour"]["used_percentage"] = 33
    chained.write_text(json.dumps(outside), encoding="utf-8")
    # No OmniGent session: the chained file (a CLI outside OmniGent) is the reading.
    snap = limits_relay.read_claude(chained, bridges=bridges)
    assert snap is not None
    assert ([w.used_percent for w in snap.windows], snap.account) == ([33.0, 50.0], None)

    # With captures it is ignored, even when it is the newest: it may hold a
    # backup session's numbers whose bridge is gone. Unreadable, it does not raise.
    _capture(bridges, "aaa", "conv_default", _session_payload(5_000, 210_000), age_s=60)
    chained.write_text(json.dumps(_backup_payload()), encoding="utf-8")
    snap = limits_relay.read_claude(chained, bridges=bridges)
    assert snap is not None
    assert [w.used_percent for w in snap.windows] == [12.0, 50.0]
    chained.write_text("not json", encoding="utf-8")
    assert limits_relay.read_claude(chained, bridges=bridges) is not None


def test_scan_bridges_reports_the_account_sidecar(tmp_path: Path) -> None:
    _capture(tmp_path, "aaa", "conv_a", _session_payload(1, 1), age_s=5, account="Backup")
    _capture(tmp_path, "bbb", "conv_b", _session_payload(1, 1), age_s=9)
    assert [(c[2], c[3]) for c in limits_relay.scan_bridges(tmp_path)] == [
        ("conv_a", "Backup"),
        ("conv_b", None),
    ]


async def test_route_answers_one_sessions_view(client: httpx.AsyncClient) -> None:
    own = [_window("five_hour", 25), _window("seven_day", 4)]
    host = _host("statusline+usage", _own("conv_b", account="Backup", windows=own))
    posted = await client.post("/v1/ae/limits/claude", json=host.model_dump(mode="json"))
    assert posted.status_code == 200, posted.text

    plain = (await client.get("/v1/ae/limits")).json()["harnesses"]["claude"]
    assert [w["used_percent"] for w in plain["windows"]] == [0, 100, 9]
    assert plain["account"] is None and plain["plan"] == "Max"

    mine = await client.get("/v1/ae/limits", params={"session_id": "conv_b"})
    assert mine.status_code == 200, mine.text
    body = mine.json()
    assert set(body) == {"now", "harnesses"} and body["harnesses"]["codex"] is None
    claude = body["harnesses"]["claude"]
    assert [w["used_percent"] for w in claude["windows"]] == [25, 4]
    assert (claude["account"], claude["account_label"]) == ("Backup", "Backup label")
    assert (claude["plan"], claude["credits"], claude["hint"]) == (None, None, limits.BACKUP_HINT)
    assert claude["received_at"] == plain["received_at"]

    unknown = await client.get("/v1/ae/limits", params={"session_id": "conv_nobody"})
    assert unknown.json()["harnesses"]["claude"] == plain
    too_long = await client.get("/v1/ae/limits", params={"session_id": "x" * 121})
    assert too_long.status_code == 422


async def test_route_answers_a_session_only_to_a_caller_who_may_open_it(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    owner = {"X-Forwarded-Email": "owner@example.com"}
    member = {"X-Forwarded-Email": "member@example.com"}
    client.headers.update(owner)
    sid = (await create_test_session(client, title="Owner on the backup"))["id"]
    del client.headers["X-Forwarded-Email"]
    own = [_window("five_hour", 25), _window("seven_day", 4)]
    host = _host("statusline+usage", _own(sid, account="Backup", windows=own))
    posted = await client.post(
        "/v1/ae/limits/claude", json=host.model_dump(mode="json"), headers=owner
    )
    assert posted.status_code == 200, posted.text

    plain = (await client.get("/v1/ae/limits", headers=member)).json()["harnesses"]["claude"]
    mine = await client.get("/v1/ae/limits", params={"session_id": sid}, headers=owner)
    assert mine.json()["harnesses"]["claude"]["account"] == "Backup"
    theirs = await client.get("/v1/ae/limits", params={"session_id": sid}, headers=member)
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["harnesses"]["claude"] == plain


# ── per account (the Admin Center's Accounts card, 2026-10-21) ─────────


def _reading(account: str | None, minutes_ago: int, five: float, week: float) -> Any:
    return limits.SessionLimits(
        conversation_id=f"conv_{account or 'default'}_{minutes_ago}",
        observed_at=NOW - timedelta(minutes=minutes_ago),
        account=account,
        account_label=f"{account} label" if account else None,
        windows=[
            limits.LimitWindow(
                id="five_hour", label="5-hour limit", used_percent=five, window_minutes=300
            ),
            limits.LimitWindow(
                id="seven_day", label="Weekly", used_percent=week, window_minutes=7 * 24 * 60
            ),
        ],
    )


def _rows(store: LimitStore, shared: dict[str, str] | None = None) -> dict[Any, Any]:
    return {(r.harness, r.account): r for r in store.accounts(shared)}


def test_store_keeps_each_accounts_newest_reading() -> None:
    store = LimitStore(None)
    host = _host("statusline+usage", _reading("Backup", 3, 25, 4), _reading(None, 9, 1, 2))
    assert store.put(host)
    rows = _rows(store)
    # The host reading fills the default login with its plan and credits; the
    # older default-login session does not replace it.
    default = rows[("claude", None)]
    assert (default.plan, default.credits, default.observed_at) == ("Max", host.credits, NOW)
    assert [w.used_percent for w in default.windows] == [0, 100, 9]
    backup = rows[("claude", "Backup")]
    assert ([w.used_percent for w in backup.windows], backup.plan, backup.credits) == (
        [25, 4],
        None,
        None,
    )
    assert backup.label == "Backup label"
    assert (default.live_sessions, backup.live_sessions) == (1, 1)

    # A newer backup reading wins; an older one in a later post does not.
    newer = _reading("Backup", 3, 60, 8).model_copy(
        update={"observed_at": NOW + timedelta(minutes=1)}
    )
    store.put(
        _host("statusline", newer).model_copy(update={"observed_at": NOW + timedelta(minutes=1)})
    )
    store.put(_host("statusline", _reading("Backup", 30, 99, 99)))
    assert [w.used_percent for w in _rows(store)[("claude", "Backup")].windows] == [60, 8]


def test_a_refused_post_still_records_a_newer_account_reading() -> None:
    store = LimitStore(None)
    assert store.put(_host("statusline", _reading(None, 1, 1, 1)))
    # A second host's post, older as a whole, carries the only reading for Team.
    late = _host("statusline", _reading("shared:Team", 2, 40, 41)).model_copy(
        update={"observed_at": NOW - timedelta(hours=1)}
    )
    assert store.put(late) is False
    assert _rows(store)[("claude", "shared:Team")].windows[0].used_percent == 40


def test_default_login_keeps_its_plan_when_a_session_reading_is_newer() -> None:
    store = LimitStore(None)
    store.put(_host("statusline+usage"))
    session = _reading(None, 0, 30, 31).model_copy(
        update={"observed_at": NOW + timedelta(minutes=2)}
    )
    store.put(_host("statusline+usage", session))
    default = _rows(store)[("claude", None)]
    assert [w.used_percent for w in default.windows] == [30, 31]
    # The plan carries over; the credits do not, or they would read as current.
    assert (default.plan, default.credits) == ("Max", None)


def test_a_host_reading_on_a_backup_fills_that_account_not_the_default() -> None:
    store = LimitStore(None)
    host = _host("statusline").model_copy(
        update={"account": "Backup", "account_label": "AE Backup", "plan": None, "credits": None}
    )
    store.put(host)
    rows = _rows(store)
    assert rows[("claude", None)].observed_at is None
    assert rows[("claude", None)].windows == []
    assert rows[("claude", "Backup")].label == "AE Backup"


def test_codex_fills_its_one_login() -> None:
    store = LimitStore(None)
    store.put(codex_snapshot(CODEX_RESULT, NOW))
    codex = _rows(store)[("codex", None)]
    assert (codex.label, codex.plan, codex.live_sessions) == (limits.CODEX_LABEL, "pro", None)
    five, week = limits.headline_windows(codex.windows)
    assert five is not None and week is not None
    assert (five.id, five.used_percent, week.id, week.used_percent) == (
        "codex.primary",
        12,
        "codex.secondary",
        44,
    )
    # Codex with no windows (an API key login) adds no row.
    empty = LimitStore(None)
    empty.put(codex_snapshot({"rateLimits": None}, NOW))
    assert ("codex", None) not in _rows(empty)


def test_accounts_persist_and_an_old_state_file_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "limits.json"
    store = LimitStore(path)
    store.put(_host("statusline+usage", _reading("Backup", 3, 25, 4)))
    body = json.loads(path.read_text(encoding="utf-8"))
    assert {row["account"] for row in body[limits.ACCOUNTS_KEY]} == {None, "Backup"}
    assert _rows(LimitStore(path))[("claude", "Backup")].windows[0].used_percent == 25

    # A file from before 2026-10-21 has no accounts key: it loads, and the
    # held snapshot's sessions fill the accounts.
    del body[limits.ACCOUNTS_KEY]
    body["claude"]["snapshot"].pop("known_accounts")
    path.write_text(json.dumps(body), encoding="utf-8")
    old = LimitStore(path)
    assert old.view(now=NOW)["harnesses"]["claude"]["plan"] == "Max"
    assert _rows(old)[("claude", "Backup")].windows[0].used_percent == 25
    # A bad row is skipped, not fatal.
    body[limits.ACCOUNTS_KEY] = [{"harness": "gemini"}, {"harness": "codex", "label": "Codex"}]
    path.write_text(json.dumps(body), encoding="utf-8")
    assert set(_rows(LimitStore(path))) == {("claude", None)}


def test_merge_lists_signed_in_accounts_without_a_reading_in_order() -> None:
    store = LimitStore(None)
    host = _host("statusline+usage", _reading("Zed", 3, 5, 5)).model_copy(
        update={
            "known_accounts": [
                limits.KnownAccount(account=None, label="Owner main"),
                limits.KnownAccount(account="Personal", label="Owner personal"),
                limits.KnownAccount(account="shared:Team", label="Team (shared)"),
            ]
        }
    )
    store.put(host)
    store.put(codex_snapshot(CODEX_RESULT, NOW))
    rows = store.accounts({"shared:Team": "AE Team", "shared:Ops": "Ops"})
    assert [(r.harness, r.account, r.label) for r in rows] == [
        ("claude", None, "Owner main"),
        ("claude", "Personal", "Owner personal"),
        ("claude", "shared:Team", "AE Team (shared)"),
        ("claude", "shared:Ops", "Ops (shared)"),
        ("claude", "Zed", "Zed label"),
        ("codex", None, "Codex"),
    ]
    personal = rows[1]
    assert (personal.observed_at, personal.windows, personal.live_sessions) == (None, [], 0)


def test_relay_lists_the_hosts_logins_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptors = [
        {"family": "claude-oauth-active", "source": "Default CLI account", "env_var": ""},
        {"family": "claude-oauth", "source": "AE Backup", "env_var": "Backup"},
        {"family": "claude-provider-env", "source": "ANTHROPIC_API_KEY", "env_var": "X"},
        {"family": "claude-oauth", "source": "", "env_var": "NoLabel"},
        {"family": "claude-oauth", "source": "AE Backup again", "env_var": "Backup"},
    ]
    monkeypatch.setattr(
        "omnigent.onboarding.claude_accounts.account_descriptors", lambda: descriptors
    )
    assert limits_relay.known_accounts() == [
        limits.KnownAccount(account=None, label="Default CLI account"),
        limits.KnownAccount(account="Backup", label="AE Backup"),
    ]

    def broken() -> list[Any]:
        raise RuntimeError("Invalid Claude accounts.")

    monkeypatch.setattr("omnigent.onboarding.claude_accounts.account_descriptors", broken)
    assert limits_relay.known_accounts() == []


async def test_relay_posts_the_known_accounts(
    app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.claude_accounts.account_descriptors",
        lambda: [{"family": "claude-oauth", "source": "AE Backup", "env_var": "Backup"}],
    )
    out = io.StringIO()
    code = await limits_relay.run(
        claude_path=_statusline_file(tmp_path),
        codex_bin=None,
        settings=Settings(server_url="http://test"),
        transport=httpx.ASGITransport(app=app),
        out=out,
    )
    assert code == 0, out.getvalue()
    rows = limits.store().accounts()
    assert [(r.account, r.label) for r in rows] == [
        (None, limits.DEFAULT_LABEL),
        ("Backup", "AE Backup"),
    ]
    assert rows[0].observed_at is not None and rows[1].observed_at is None


async def test_accounts_route_merges_and_says_nothing_secret(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv(shared_accounts.KEY_ENV, Fernet.generate_key().decode())
    token = "sk-ant-oat01-" + "a" * 40
    shared = shared_accounts.SharedAccountsStore(tmp_path / "shared.json")
    shared.add("Team", "AE Team", token, "owner@example.com")
    shared_accounts.reset_store(shared)
    try:
        empty = await client.get("/v1/ae/limits/accounts")
        assert empty.status_code == 200, empty.text
        assert [(r["account"], r["observed_at"]) for r in empty.json()["accounts"]] == [
            (None, None),
            ("shared:Team", None),
        ]

        host = _host("statusline+usage", _reading("Backup", 3, 25, 4)).model_copy(
            update={"known_accounts": [limits.KnownAccount(account="Personal", label="Mine")]}
        )
        posted = await client.post("/v1/ae/limits/claude", json=host.model_dump(mode="json"))
        assert posted.status_code == 200, posted.text
        await client.post(
            "/v1/ae/limits/codex", json=codex_snapshot(CODEX_RESULT, NOW).model_dump(mode="json")
        )

        answer = await client.get("/v1/ae/limits/accounts")
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert set(body) == {"now", "accounts"}
        rows = {(r["harness"], r["account"]): r for r in body["accounts"]}
        assert list(rows) == [
            ("claude", None),
            ("claude", "Personal"),
            ("claude", "shared:Team"),
            ("claude", "Backup"),
            ("codex", None),
        ]
        default = rows[("claude", None)]
        assert (default["five_hour"]["used_percent"], default["weekly"]["used_percent"]) == (
            0,
            100,
        )
        assert default["plan"] == "Max" and default["live_sessions"] == 0
        backup = rows[("claude", "Backup")]
        assert (backup["five_hour"]["used_percent"], backup["weekly"]["used_percent"]) == (25, 4)
        assert backup["live_sessions"] == 1 and backup["plan"] is None
        # Signed in, never read: a row with nothing in it yet.
        personal = rows[("claude", "Personal")]
        assert (personal["label"], personal["observed_at"], personal["five_hour"]) == (
            "Mine",
            None,
            None,
        )
        assert rows[("claude", "shared:Team")]["label"] == "AE Team (shared)"
        codex = rows[("codex", None)]
        assert (codex["five_hour"]["used_percent"], codex["weekly"]["used_percent"]) == (12, 44)
        assert set(default) == {
            "harness",
            "account",
            "label",
            "plan",
            "five_hour",
            "weekly",
            "credits",
            "observed_at",
            "live_sessions",
        }
        text = answer.text
        for secret in ("owner@example.com", token, "sk-ant", "created_by", "@"):
            assert secret not in text
    finally:
        shared_accounts.reset_store()


async def test_accounts_route_needs_an_identity_off_single_user(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    assert (await client.get("/v1/ae/limits/accounts")).status_code == 401


def test_a_fresh_store_lists_no_account() -> None:
    assert LimitStore(None).accounts() == []
    assert LimitStore(None).accounts({}) == []


def test_an_account_that_left_the_host_or_the_store_drops_out() -> None:
    store = LimitStore(None)
    store.put(_host("statusline", _reading("Gone", 3, 95, 95), _reading("shared:Old", 4, 1, 1)))
    # No list from the host (an older relay) and no shared store: every reading shows.
    assert set(_rows(store, None)) == {
        ("claude", None),
        ("claude", "Gone"),
        ("claude", "shared:Old"),
    }
    later = NOW + timedelta(minutes=5)
    listed = _host("statusline").model_copy(
        update={
            "observed_at": later,
            "known_accounts": [limits.KnownAccount(account=None, label="Main")],
        }
    )
    store.put(listed)
    assert set(_rows(store, {})) == {("claude", None)}
    # A session still running on an account keeps it listed.
    running = _reading("Gone", 0, 50, 50).model_copy(update={"observed_at": later})
    store.put(listed.model_copy(update={"sessions": [running]}))
    assert ("claude", "Gone") in _rows(store, {})


def test_codex_keeps_its_row_past_the_account_cap() -> None:
    readings = [
        limits.AccountUsage(
            harness="claude", account=f"a{i}", label=f"a{i}", observed_at=NOW, windows=[]
        )
        for i in range(limits.MAX_ACCOUNTS + 5)
    ]
    readings.append(
        limits.AccountUsage(harness="codex", label="Codex", observed_at=NOW, windows=[])
    )
    rows = limits.merge_accounts(readings, [], None, {})
    assert len(rows) == limits.MAX_ACCOUNTS
    assert rows[-1].harness == "codex"


def test_weekly_headline_is_never_a_per_model_or_legacy_row() -> None:
    def win(window_id: str) -> limits.LimitWindow:
        return limits.LimitWindow(
            id=window_id, label=window_id, used_percent=50, window_minutes=7 * 24 * 60
        )

    five, week = limits.headline_windows([win("seven_day_opus"), win("seven_day.model.fable")])
    assert (five, week) == (None, None)
    assert limits.headline_windows([win("seven_day_opus"), win("seven_day")])[1].id == "seven_day"


def test_with_usage_keeps_a_backup_status_line_off_the_default_login() -> None:
    backup = claude_snapshot(STATUSLINE, NOW, now=NOW).model_copy(
        update={"account": "Backup", "account_label": "AE Backup"}
    )
    reply = {"rate_limits_available": True, "rate_limits": {}}
    merged = limits.with_usage(backup, reply, NOW, now=NOW)
    assert merged is not None
    assert (merged.account, merged.windows) == (None, [])


def test_the_limits_view_leaves_the_login_list_out() -> None:
    store = LimitStore(None)
    store.put(
        _host("statusline").model_copy(
            update={"known_accounts": [limits.KnownAccount(account="Personal", label="Mine")]}
        )
    )
    assert "known_accounts" not in store.view(now=NOW)["harnesses"]["claude"]


_KNOWN = [
    limits.KnownAccount(account=None, label="AE Default"),
    limits.KnownAccount(account="Backup", label="AE Backup"),
]


def _claude_view(snapshot: LimitSnapshot, session_id: str | None = None) -> dict[str, Any]:
    store = LimitStore(None)
    store.put(snapshot)
    return store.view(now=NOW, session_id=session_id)["harnesses"]["claude"]


def test_the_limits_view_labels_the_default_login_from_the_host_list() -> None:
    host = _host("statusline", _own("conv_main")).model_copy(update={"known_accounts": _KNOWN})
    for session_id in (None, "conv_main"):
        body = _claude_view(host, session_id)
        assert (body["account"], body["account_label"]) == (None, "AE Default")
        assert "known_accounts" not in body


def test_the_limits_view_labels_a_backup_session_from_the_host_list() -> None:
    own = limits.SessionLimits(
        conversation_id="conv_b",
        observed_at=NOW,
        account="Backup",
        windows=[_window("five_hour", 25)],
    )
    host = _host("statusline", own).model_copy(update={"known_accounts": _KNOWN})
    body = _claude_view(host, "conv_b")
    assert (body["account"], body["account_label"]) == ("Backup", "AE Backup")


def test_the_limits_view_keeps_a_label_the_snapshot_already_has() -> None:
    host = _host("statusline").model_copy(
        update={"account_label": "Own label", "known_accounts": _KNOWN}
    )
    assert _claude_view(host)["account_label"] == "Own label"


def test_the_limits_view_leaves_the_label_null_without_a_host_list() -> None:
    body = _claude_view(_host("statusline"))
    assert (body["account"], body["account_label"]) == (None, None)
    assert "known_accounts" not in body


def test_the_limits_view_keeps_a_backup_sessions_own_label() -> None:
    own = _own("conv_b", account="Backup", windows=[_window("five_hour", 25)])
    host = _host("statusline", own).model_copy(update={"known_accounts": _KNOWN})
    assert _claude_view(host, "conv_b")["account_label"] == "Backup label"


# ── a session the relay has not posted yet (2026-10-26) ─────────────────


def _backup_reading(used: float = 21) -> limits.AccountUsage:
    return limits.AccountUsage(
        harness="claude",
        account="Backup",
        label="AE Backup",
        windows=[_window("five_hour", used), _window("seven_day", 4)],
        observed_at=NOW - timedelta(minutes=2),
    )


def _readings(*rows: limits.AccountUsage) -> dict[tuple[str, str], limits.AccountUsage]:
    return {(r.harness, r.account or ""): r for r in rows}


def _default_host(*sessions: limits.SessionLimits) -> LimitSnapshot:
    return _host("statusline+usage", *sessions).model_copy(update={"known_accounts": _KNOWN})


def test_an_unknown_session_on_backup_gets_the_backup_reading() -> None:
    host = _default_host()
    view = limits.session_view(
        host, "conv_new", account="Backup", readings=_readings(_backup_reading())
    )
    assert [(w.id, w.used_percent) for w in view.windows] == [("five_hour", 21), ("seven_day", 4)]
    assert (view.plan, view.credits, view.hint) == (None, None, limits.BACKUP_HINT)
    assert (view.account, view.account_label) == ("Backup", "AE Backup")
    assert view.observed_at == NOW - timedelta(minutes=2)
    assert view.note is not None and "AE Backup" in view.note and "another session" in view.note
    assert host.windows[0].used_percent == 0  # the stored reading is untouched


def test_an_unknown_backup_session_with_no_reading_never_gets_the_defaults_numbers() -> None:
    view = limits.session_view(_default_host(), "conv_new", account="Backup", readings={})
    assert view.windows == []
    assert view.note == limits.NO_ACCOUNT_READING_NOTE.format(account="AE Backup")
    assert (view.plan, view.credits, view.account) == (None, None, "Backup")


def test_a_session_in_the_snapshot_ignores_the_hint() -> None:
    own = _own("conv_d", windows=[_window("five_hour", 25)])
    host = _default_host(own)
    assert (
        limits.session_view(
            host, "conv_d", account="Backup", readings=_readings(_backup_reading())
        )
        is host
    )


def test_a_listed_backup_session_without_windows_gets_the_accounts_reading() -> None:
    host = _default_host(_own("conv_b", account="Backup"))
    view = limits.session_view(host, "conv_b", readings=_readings(_backup_reading()))
    assert [w.used_percent for w in view.windows] == [21, 4]
    assert view.observed_at == NOW - timedelta(minutes=2)
    assert view.note is not None and "another session" in view.note
    # Without a reading of its own account it still has none, as before.
    bare = limits.session_view(host, "conv_b", readings={})
    assert (bare.windows, bare.note) == ([], limits.NO_WINDOWS_NOTE)


def test_an_account_the_host_does_not_list_gives_the_host_answer() -> None:
    host = _default_host()
    assert limits.session_view(host, "conv_new", account="Gone", readings={}) is host


def test_a_default_login_hint_on_a_backup_host_reading_gets_the_default_reading() -> None:
    host = _default_host().model_copy(update={"account": "Backup", "plan": None})
    held = limits.AccountUsage(
        harness="claude",
        label="AE Default",
        plan="Max",
        windows=[_window("five_hour", 7)],
        observed_at=NOW,
    )
    view = limits.session_view(host, "conv_new", account=None, readings=_readings(held))
    assert (view.account, view.plan, [w.used_percent for w in view.windows]) == (None, "Max", [7])


def test_the_store_answers_a_new_backup_chat_from_its_account() -> None:
    store = LimitStore(None)
    store.put(_default_host(_own("conv_b", account="Backup", windows=[_window("five_hour", 33)])))
    body = store.view(now=NOW, session_id="conv_new", account="Backup")["harnesses"]["claude"]
    assert [w["used_percent"] for w in body["windows"]] == [33]
    assert body["account"] == "Backup"
    plain = store.view(now=NOW, session_id="conv_new")["harnesses"]["claude"]
    assert plain["account"] is None and plain["windows"][0]["used_percent"] == 0


# ── the route: label, hint and who may ask ──────────────────────────────


@pytest.fixture
def _two_logins(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A stored Claude post with a Backup reading, a settable label and open access."""
    from ae_omni_policies import router as router_module

    limits.reset_store()
    state: dict[str, Any] = {"labels": {}, "open": True, "age_s": 30}
    own = _own("conv_b", account="Backup", windows=[_window("five_hour", 33)])
    limits.store().put(_default_host(own))

    async def _may_open(request: Any, user: str | None, session_id: str) -> bool:
        return bool(state["open"])

    async def _conversation(session_id: str) -> Any:
        created = int(time.time()) - state["age_s"]
        return type("Conv", (), {"labels": state["labels"], "created_at": created})()

    monkeypatch.setattr(router_module, "_may_open_session", _may_open)
    monkeypatch.setattr(router_module, "_conversation", _conversation)
    yield state
    limits.reset_store()


async def _claude(client: httpx.AsyncClient, query: str) -> dict[str, Any]:
    response = await client.get(f"/v1/ae/limits?{query}")
    assert response.status_code == 200, response.text
    return response.json()["harnesses"]["claude"]


async def test_route_uses_the_account_hint_for_a_new_session(
    client: httpx.AsyncClient, _two_logins: dict[str, Any]
) -> None:
    body = await _claude(client, "session_id=conv_new&account=Backup")
    assert (body["account"], [w["used_percent"] for w in body["windows"]]) == ("Backup", [33])
    host = await _claude(client, "session_id=conv_new")
    assert host["account"] is None


async def test_route_label_beats_the_hint(
    client: httpx.AsyncClient, _two_logins: dict[str, Any]
) -> None:
    _two_logins["labels"] = {"omnigent.claude_native.account": "Backup"}
    body = await _claude(client, "session_id=conv_new&account=")
    assert body["account"] == "Backup"
    _two_logins["labels"] = {"omnigent.claude_native.account": ""}
    body = await _claude(client, "session_id=conv_new&account=Backup")
    assert body["account"] is None


async def test_route_ignores_labels_when_the_caller_cannot_open_the_session(
    client: httpx.AsyncClient, _two_logins: dict[str, Any]
) -> None:
    _two_logins["open"] = False
    _two_logins["labels"] = {
        "omnigent.claude_native.account": "Backup",
        limits.ACCOUNT_LIVE_LABEL: "Backup",
    }
    body = await _claude(client, "session_id=conv_new")
    assert body["account"] is None
    assert body["windows"][0]["used_percent"] == 0
    # The login the caller names is theirs to ask for, with or without the session.
    named = await _claude(client, "session_id=conv_new&account=")
    assert named["account"] is None


async def test_route_ignores_the_hint_for_a_session_older_than_the_window(
    client: httpx.AsyncClient, _two_logins: dict[str, Any]
) -> None:
    from ae_omni_policies import router as router_module

    inside = router_module.LIMITS_HINT_WINDOW_S - 60
    _two_logins["age_s"] = inside
    assert (await _claude(client, "session_id=conv_new&account=Backup"))["account"] == "Backup"
    _two_logins["age_s"] = router_module.LIMITS_HINT_WINDOW_S + 60
    old = await _claude(client, "session_id=conv_new&account=Backup")
    assert old["account"] is None and old["windows"][0]["used_percent"] == 0
    # A label has no age limit.
    _two_logins["labels"] = {"omnigent.claude_native.account": "Backup"}
    assert (await _claude(client, "session_id=conv_new"))["account"] == "Backup"


async def test_route_answers_account_alone_as_that_logins_view(
    client: httpx.AsyncClient, _two_logins: dict[str, Any]
) -> None:
    limits.reset_store(LimitStore(None))  # no Backup reading stored
    limits.store().put(_default_host())
    backup = await _claude(client, "account=Backup")
    assert (backup["account"], backup["account_label"]) == ("Backup", "AE Backup")
    assert backup["windows"] == [] and "AE Backup" in backup["note"]
    assert "first reply" not in backup["note"]
    default = await _claude(client, "account=")
    assert default["account"] is None and default["windows"][0]["used_percent"] == 0
    # An unlisted account gets no windows and a note naming it, not the box-wide view.
    gone = await _claude(client, "account=Gone")
    assert gone["account"] == "Gone" and gone["windows"] == [] and "Gone" in gone["note"]
    plain = await _claude(client, "")
    assert plain["account"] is None and plain["windows"][0]["used_percent"] == 0


async def test_route_answers_account_alone_from_the_stored_reading(
    client: httpx.AsyncClient, _two_logins: dict[str, Any]
) -> None:
    limits.store().put(
        _default_host(_own("conv_b", account="Backup", windows=[_window("five_hour", 33)]))
    )
    body = await _claude(client, "account=Backup")
    assert [w["used_percent"] for w in body["windows"]] == [33]
    assert body["account"] == "Backup"


async def test_route_treats_a_temp_session_id_as_account_alone(
    client: httpx.AsyncClient, _two_logins: dict[str, Any]
) -> None:
    _two_logins["labels"] = {limits.ACCOUNT_LIVE_LABEL: ""}
    body = await _claude(client, "session_id=temp:abc&account=Backup")
    assert body["account"] == "Backup"
    assert (await _claude(client, "session_id=temp:abc&account="))["account"] is None


async def test_route_live_label_beats_the_fork_label_and_the_hint(
    client: httpx.AsyncClient, _two_logins: dict[str, Any]
) -> None:
    _two_logins["labels"] = {
        limits.ACCOUNT_LIVE_LABEL: "Backup",
        "omnigent.claude_native.account": "",
    }
    body = await _claude(client, "session_id=conv_new&account=")
    assert body["account"] == "Backup"
    # "" is the default login, however old the session is.
    _two_logins["labels"] = {limits.ACCOUNT_LIVE_LABEL: ""}
    _two_logins["age_s"] = 3600
    body = await _claude(client, "session_id=conv_new&account=Backup")
    assert body["account"] is None and body["windows"][0]["used_percent"] == 0


async def test_route_ignores_a_live_label_for_a_login_the_host_does_not_list(
    client: httpx.AsyncClient, _two_logins: dict[str, Any]
) -> None:
    _two_logins["labels"] = {limits.ACCOUNT_LIVE_LABEL: "Gone"}
    body = await _claude(client, "session_id=conv_new&account=Backup")
    assert body["account"] == "Backup"


def test_a_stale_relay_entry_never_answers_for_another_live_login() -> None:
    # The relay saw conv_b on Backup; the terminal has since relaunched on the default login.
    host = _default_host(_own("conv_b", account="Backup", windows=[_window("five_hour", 33)]))
    on_default = limits.session_view(host, "conv_b", live=None, readings={})
    assert on_default is host
    # And the other way: the relay saw it on the default login, now it runs on Backup.
    seen_default = _default_host(_own("conv_d", windows=[_window("five_hour", 25)]))
    now_backup = limits.session_view(
        seen_default, "conv_d", live="Backup", readings=_readings(_backup_reading())
    )
    assert (now_backup.account, [w.used_percent for w in now_backup.windows]) == (
        "Backup",
        [21, 4],
    )
    bare = limits.session_view(seen_default, "conv_d", live="Backup", readings={})
    assert bare.windows == [] and bare.account == "Backup"
    assert bare.note == limits.NO_ACCOUNT_READING_NOTE.format(account="AE Backup")
    # The entry still answers when it names the live login.
    same = limits.session_view(host, "conv_b", live="Backup", readings={})
    assert [w.used_percent for w in same.windows] == [33]


def test_a_label_for_an_unlisted_shared_account_is_trusted() -> None:
    held = limits.AccountUsage(
        harness="claude",
        account="shared:Team",
        label="Team (shared)",
        windows=[_window("five_hour", 55)],
        observed_at=NOW,
    )
    for host in (_default_host(), _default_host().model_copy(update={"known_accounts": []})):
        view = limits.session_view(
            host, "conv_f", account="shared:Team", trusted=True, readings=_readings(held)
        )
        assert (view.account, [w.used_percent for w in view.windows]) == ("shared:Team", [55])
        bare = limits.session_view(
            host, "conv_f", account="shared:Team", trusted=True, readings={}
        )
        assert bare.windows == [] and bare.account == "shared:Team"
        assert bare.note == limits.NO_ACCOUNT_READING_NOTE.format(account="Team")
        assert (bare.plan, bare.credits, bare.hint) == (None, None, limits.BACKUP_HINT)
        # The same account as a hint is not on the host's list: the host answer.
        assert limits.session_view(host, "conv_f", account="shared:Team", readings={}) is host
