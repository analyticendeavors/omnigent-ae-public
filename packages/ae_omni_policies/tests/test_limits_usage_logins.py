"""Usage logins: idle accounts read through their own sign-in (2026-11-16).

docs/PLAN-LIMITS.md, "Idle accounts". The account names here are made up; the
point of several tests is that no name ever reaches the relay's output.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from ae_omni_policies import limits, limits_relay, shared_accounts, usage_login
from ae_omni_policies.limits import AccountProbe, LimitSnapshot, LimitWindow
from ae_omni_policies.omni import Settings

ACCOUNT = "shared:Zebra Team"
SECRET_WORDS = ("Zebra", "zebra")


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(limits.STATE_FILE_ENV, raising=False)
    monkeypatch.setattr(limits, "DATA_DIR", Path("/nonexistent-ae-data"))
    monkeypatch.setattr(
        "omnigent.onboarding.claude_accounts.account_descriptors", list, raising=False
    )
    limits.reset_store()
    yield
    limits.reset_store()


def _window(window_id: str, used: float, minutes: int = 10080) -> LimitWindow:
    return LimitWindow(
        id=window_id,
        label=window_id,
        used_percent=used,
        resets_at=datetime.now(UTC) + timedelta(days=2),
        window_minutes=minutes,
    )


def _host(observed: datetime, **update: Any) -> LimitSnapshot:
    snap = LimitSnapshot(
        harness="claude",
        source="statusline+usage",
        observed_at=observed,
        plan="Max",
        windows=[_window("seven_day", 50)],
    )
    return snap.model_copy(update=update)


def _probe(observed: datetime, used: float = 91, account: str = ACCOUNT) -> AccountProbe:
    return AccountProbe(
        account=account,
        observed_at=observed,
        windows=[_window("five_hour", 10, 300), _window("seven_day", used)],
    )


# ── the server side ─────────────────────────────────────────────────────


def test_a_probe_becomes_its_accounts_reading_with_no_plan() -> None:
    now = datetime.now(UTC)
    snap = _host(now, account_probes=[_probe(now), AccountProbe(account="Empty", observed_at=now)])
    readings = {r.account: r for r in limits.account_readings(snap)}
    assert set(readings) == {None, ACCOUNT}  # a probe without windows says nothing
    shared = readings[ACCOUNT]
    assert shared.label == "Zebra Team (shared)"
    assert shared.plan is None and shared.credits is None
    assert shared.observed_at == now
    assert readings[None].plan == "Max"


def test_a_probe_cannot_name_the_default_login() -> None:
    with pytest.raises(ValueError):
        AccountProbe(account="", observed_at=datetime.now(UTC))


def test_probes_only_post_is_noted_never_held() -> None:
    store = limits.store()
    old = datetime.now(UTC) - timedelta(hours=1)
    assert store.put(_host(old))
    before = store.view()["harnesses"]["claude"]
    probes_only = LimitSnapshot(
        harness="claude",
        source=limits.PROBES_SOURCE,
        observed_at=datetime.now(UTC),
        account_probes=[_probe(datetime.now(UTC))],
    )
    assert store.put(probes_only) is False
    after = store.view()["harnesses"]["claude"]
    assert after["source"] == before["source"] == "statusline+usage"
    assert after["windows"] == before["windows"]
    rows = {r.account: r for r in store.accounts()}
    assert rows[ACCOUNT].observed_at is not None


def test_probes_do_not_decide_which_host_snapshot_is_held() -> None:
    store = limits.store()
    t0 = datetime.now(UTC) - timedelta(minutes=30)
    # Held: an old host reading with a fresh probe beside it.
    store.put(_host(t0, account_probes=[_probe(datetime.now(UTC))]))
    # A newer host reading (older than the probe) still replaces it.
    newer = _host(t0 + timedelta(minutes=10), windows=[_window("seven_day", 55)])
    assert store.put(newer) is True
    assert store.view()["harnesses"]["claude"]["windows"][0]["used_percent"] == 55


def test_newest_reading_wins_between_probe_and_status_line() -> None:
    store = limits.store()
    now = datetime.now(UTC)
    store.put(_host(now - timedelta(minutes=5), account_probes=[_probe(now, used=92)]))
    stale_session = limits.SessionLimits(
        conversation_id="conv_1",
        observed_at=now - timedelta(hours=20),
        account=ACCOUNT,
        windows=[_window("seven_day", 80)],
    )
    store.put(_host(now - timedelta(minutes=1), sessions=[stale_session]))
    row = {r.account: r for r in store.accounts()}[ACCOUNT]
    assert row.observed_at == now
    assert limits.headline_windows(row.windows)[1].used_percent == 92  # type: ignore[union-attr]


def test_views_never_carry_probes() -> None:
    store = limits.store()
    now = datetime.now(UTC)
    store.put(_host(now, account_probes=[_probe(now)]))
    view = store.view()
    assert "account_probes" not in view["harnesses"]["claude"]
    assert "account_probes" not in store.view(account=ACCOUNT)["harnesses"]["claude"]
    leaky = {"harnesses": {"claude": {"account_probes": [{}], "contexts": [], "sessions": []}}}
    member = limits.member_view(leaky, session_id=None, relayed=False)
    assert "account_probes" not in member["harnesses"]["claude"]


async def test_route_shows_the_probe_reading_and_nothing_else_changes(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(shared_accounts.KEY_ENV, Fernet.generate_key().decode())
    shared = shared_accounts.SharedAccountsStore(tmp_path / "shared.json")
    shared.add("Zebra Team", "Zebra Team", "sk-ant-oat01-" + "a" * 40, "owner@example.com")
    shared_accounts.reset_store(shared)
    try:
        now = datetime.now(UTC)
        host = _host(now)
        assert (
            await client.post("/v1/ae/limits/claude", json=host.model_dump(mode="json"))
        ).status_code == 200
        before = (await client.get("/v1/ae/limits/accounts")).json()["accounts"]
        probed = host.model_copy(update={"account_probes": [_probe(now)]})
        posted = await client.post("/v1/ae/limits/claude", json=probed.model_dump(mode="json"))
        assert posted.status_code == 200, posted.text
        after = (await client.get("/v1/ae/limits/accounts")).json()["accounts"]
        assert [(r["harness"], r["account"]) for r in after] == [
            (r["harness"], r["account"]) for r in before
        ]
        shared_row = next(r for r in after if r["account"] == ACCOUNT)
        assert shared_row["weekly"]["used_percent"] == 91
        assert shared_row["plan"] is None and shared_row["credits"] is None
        assert after[0] == before[0]  # the default login's row is untouched
        limits_view = (await client.get("/v1/ae/limits")).json()
        assert "account_probes" not in json.dumps(limits_view)
    finally:
        shared_accounts.reset_store()


# ── the relay side ──────────────────────────────────────────────────────

# A CLI that answers get_usage from the login its CLAUDE_CONFIG_DIR holds: a file
# named "logged-in" stands for a full sign-in; anything else answers unavailable.
FAKE_CLAUDE = r"""#!{python}
import json, os, sys
config = os.environ.get("CLAUDE_CONFIG_DIR", "")
log = {log!r}
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"config": config,
        "token": "CLAUDE_CODE_OAUTH_TOKEN" in os.environ,
        "autoupdate_off": os.environ.get("DISABLE_AUTOUPDATER") == "1",
        "base_url": "ANTHROPIC_BASE_URL" in os.environ}}) + "\n")
if "-p" in sys.argv and sys.argv[sys.argv.index("-p") + 1].startswith("Reply"):
    ok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat01-chat"
    print("OK" if ok else "", flush=True)
    sys.exit(0 if ok else 1)
logged_in = bool(config) and os.path.exists(os.path.join(config, "logged-in"))
for line in sys.stdin:
    msg = json.loads(line)
    if logged_in and "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ:
        reply = json.loads({reply!r})
    else:
        reply = {{"subscription_type": None, "rate_limits_available": False, "rate_limits": None}}
    body = {{"subtype": "success", "request_id": msg["request_id"], "response": reply}}
    print(json.dumps({{"type": "control_response", "response": body}}), flush=True)
"""


def _iso(now: datetime, **ahead: float) -> str:
    return (now + timedelta(**ahead)).isoformat()


def _usage_reply(weekly: float = 91.0) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "subscription_type": "max",
        "rate_limits_available": True,
        "rate_limits": {
            "five_hour": {"utilization": 12.0, "resets_at": _iso(now, hours=2)},
            "seven_day": {"utilization": weekly, "resets_at": _iso(now, days=3)},
            "model_scoped": [],
            "extra_usage": {"is_enabled": True, "monthly_limit": 100, "used_credits": 5},
        },
    }


def _fake_claude(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "calls.jsonl"
    path = tmp_path / "fake-claude"
    path.write_text(
        FAKE_CLAUDE.format(python=sys.executable, log=str(log), reply=json.dumps(_usage_reply())),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path), log


def _slot(root: Path, name: str, account: str, *, logged_in: bool = True) -> Path:
    slot = root / name
    (slot / "config").mkdir(parents=True)
    (slot / "account").write_text(account + "\n", encoding="utf-8")
    if logged_in:
        (slot / "config" / "logged-in").write_text("", encoding="utf-8")
    return slot


def _calls(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_usage_login_env_reads_only_the_slot(tmp_path: Path) -> None:
    env = limits_relay.usage_login_env(
        tmp_path / "slot-1",
        {
            "CLAUDE_CODE_OAUTH_TOKEN": "x",
            "ANTHROPIC_API_KEY": "y",
            "CLAUDE_CONFIG_DIR": "/elsewhere/.claude",
        },
    )
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "slot-1" / "config")
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env


def test_scan_skips_anything_that_is_not_a_slot(tmp_path: Path) -> None:
    root = tmp_path / "logins"
    _slot(root, "slot-1", ACCOUNT)
    _slot(root, "slot-2", ACCOUNT)  # the same account twice: first slot only
    _slot(root, "slot-3", "bad/name")
    _slot(root, "notaslot", "Other")
    _slot(root, "slot-4", "")
    (root / "slot-9").symlink_to(root / "slot-1")
    assert [(s.name, a) for s, a in limits_relay.scan_usage_logins(root)] == [("slot-1", ACCOUNT)]
    assert limits_relay.scan_usage_logins(tmp_path / "missing") == []


async def test_due_slots_are_read_stamped_and_then_left_alone(tmp_path: Path) -> None:
    claude, log = _fake_claude(tmp_path)
    root = tmp_path / "logins"
    slot = _slot(root, "slot-1", ACCOUNT)
    probes, line, stamps = await limits_relay.read_usage_logins(claude, root)
    assert [p.account for p in probes] == [ACCOUNT]
    # Not stamped until the reading is delivered.
    assert not (slot / "last-read").exists()
    assert stamps == [(slot, probes[0].observed_at)]
    limits_relay.stamp_usage_logins(stamps)
    stamp = (slot / "last-read").stat().st_mtime
    assert stamp == pytest.approx(probes[0].observed_at.timestamp(), abs=0.01)
    assert line == "claude: usage logins: 1 read, 0 read under 12 min ago, 0 unreadable"
    [call] = _calls(log)
    assert call == {
        "config": str(slot / "config"),
        "token": False,
        "autoupdate_off": True,
        "base_url": False,
    }
    again, line2, _ = await limits_relay.read_usage_logins(claude, root)
    assert again == [] and "0 read, 1 read under 12 min ago" in str(line2)
    assert len(_calls(log)) == 1
    # Twelve minutes on, it is due again.
    old = stamp - limits_relay.USAGE_LOGIN_EVERY_S - 1
    os.utime(slot / "last-read", (old, old))
    third, _, _ = await limits_relay.read_usage_logins(claude, root)
    assert len(third) == 1
    # A stamp in the future (the clock stepped back) counts as due.
    ahead = datetime.now(UTC).timestamp() + 3600
    os.utime(slot / "last-read", (ahead, ahead))
    fourth, _, _ = await limits_relay.read_usage_logins(claude, root)
    assert len(fourth) == 1


async def test_an_unreadable_slot_retries_next_tick_and_names_nobody(tmp_path: Path) -> None:
    claude, log = _fake_claude(tmp_path)
    root = tmp_path / "logins"
    slot = _slot(root, "slot-1", ACCOUNT, logged_in=False)
    probes, line, stamps = await limits_relay.read_usage_logins(claude, root)
    assert probes == [] and stamps == []
    assert not (slot / "last-read").exists()
    assert line is not None and "1 unreadable" in line and "ae-usage-login.sh" in line
    assert not any(word in line for word in SECRET_WORDS)
    await limits_relay.read_usage_logins(claude, root)
    assert len(_calls(log)) == 2


async def test_a_held_lock_skips_the_slot(tmp_path: Path) -> None:
    import fcntl

    claude, log = _fake_claude(tmp_path)
    root = tmp_path / "logins"
    slot = _slot(root, "slot-1", ACCOUNT)
    fd = os.open(slot / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        probes, line, _ = await limits_relay.read_usage_logins(claude, root)
    finally:
        os.close(fd)
    assert probes == [] and "1 read under" in str(line)
    assert _calls(log) == []


async def test_a_host_login_that_left_the_host_is_skipped(tmp_path: Path) -> None:
    claude, _log = _fake_claude(tmp_path)
    root = tmp_path / "logins"
    _slot(root, "slot-1", "Gone")
    _slot(root, "slot-2", ACCOUNT)
    probes, line, _ = await limits_relay.read_usage_logins(claude, root, known={None, "Other"})
    assert [p.account for p in probes] == [ACCOUNT]
    assert "1 for a login no longer on this host" in str(line)


async def test_relay_posts_probes_with_the_claude_reading(app: FastAPI, tmp_path: Path) -> None:
    claude, _log = _fake_claude(tmp_path)
    root = tmp_path / "logins"
    _slot(root, "slot-1", ACCOUNT)
    statusline = tmp_path / "statusline.json"
    reset = int((datetime.now(UTC) + timedelta(hours=2)).timestamp())
    statusline.write_text(
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": reset}}}),
        encoding="utf-8",
    )
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=statusline,
        codex_bin=None,
        claude_bin=claude,
        usage_logins=root,
        settings=Settings(server_url="http://test"),
        transport=httpx.ASGITransport(app=app),
        out=out,
    )
    assert status == 0, out.getvalue()
    assert "claude: usage logins: 1 read" in out.getvalue()
    assert not any(word in out.getvalue() for word in SECRET_WORDS)
    rows = {r.account: r for r in limits.store().accounts()}
    assert limits.headline_windows(rows[ACCOUNT].windows)[1].used_percent == 91  # type: ignore[union-attr]
    held = limits.store().view()["harnesses"]["claude"]
    assert held["source"] == "statusline"


async def test_relay_posts_probes_alone_when_there_is_no_claude_reading(
    app: FastAPI, tmp_path: Path
) -> None:
    claude, _log = _fake_claude(tmp_path)
    root = tmp_path / "logins"
    _slot(root, "slot-1", ACCOUNT)
    # No status line, and the fake answers the default login "unavailable":
    # nothing but the probe has a reading.
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=tmp_path / "missing.json",
        codex_bin=None,
        claude_bin=claude,
        usage_logins=root,
        settings=Settings(server_url="http://test"),
        transport=httpx.ASGITransport(app=app),
        out=out,
    )
    assert status == 0, out.getvalue()
    assert "claude: posted (1 usage-login readings only)" in out.getvalue()
    assert limits.store().view()["harnesses"]["claude"] is None
    assert {r.account for r in limits.store().accounts()} >= {ACCOUNT}


def test_post_body_leaves_out_an_empty_probe_list() -> None:
    snap = _host(datetime.now(UTC))
    assert "account_probes" not in limits_relay._post_body(snap)
    probed = snap.model_copy(update={"account_probes": [_probe(datetime.now(UTC))]})
    assert limits_relay._post_body(probed)["account_probes"][0]["account"] == ACCOUNT


async def test_a_422_never_echoes_the_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"input": ACCOUNT}]})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(limits_relay.RelayError) as caught:
            await limits_relay.post(client, _host(datetime.now(UTC)))
    assert "422" in str(caught.value)
    assert not any(word in str(caught.value) for word in SECRET_WORDS)


async def test_dry_run_reads_no_slot_and_names_nobody(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, log = _fake_claude(tmp_path)
    root = tmp_path / "logins"
    slot = _slot(root, "slot-1", ACCOUNT)
    monkeypatch.setattr(
        limits_relay,
        "known_accounts",
        lambda: [limits.KnownAccount(account="Zebra", label="Zebra login")],
    )
    out = io.StringIO()
    await limits_relay.run(
        claude_path=tmp_path / "missing.json",
        codex_bin=None,
        claude_bin=claude,
        usage_logins=root,
        settings=Settings(server_url="http://test"),
        dry_run=True,
        out=out,
    )
    assert "claude: usage logins: 1 found, not read in a dry run" in out.getvalue()
    assert not any(word in out.getvalue() for word in SECRET_WORDS)
    # Only the default login's get_usage ran; the slot was never opened or stamped.
    assert [c["config"] for c in _calls(log)] == [""]
    assert not (slot / "last-read").exists()


# ── the one-time sign-in helper ─────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(limits_relay.USAGE_LOGINS_ENV, raising=False)
    return home


@pytest.mark.parametrize("inside", [".claude", ".claude/sub", ".claude-acct2", ""])
def test_root_never_overlaps_a_chat_login(
    home: Path, monkeypatch: pytest.MonkeyPatch, inside: str
) -> None:
    monkeypatch.setenv(limits_relay.USAGE_LOGINS_ENV, str(home / inside) if inside else str(home))
    with pytest.raises(usage_login.UsageLoginError, match="overlaps"):
        usage_login.safe_root()


def test_new_slot_is_private_and_found_again(home: Path) -> None:
    slot = usage_login.new_slot(ACCOUNT)
    assert slot == home / ".omnigent" / "ae" / "usage-logins" / "slot-1"
    assert stat.S_IMODE(slot.stat().st_mode) == 0o700
    assert stat.S_IMODE((slot / "account").stat().st_mode) == 0o600
    assert (slot / "account").read_text(encoding="utf-8").strip() == ACCOUNT
    assert usage_login.new_slot(ACCOUNT) == slot
    assert usage_login.new_slot("Other").name == "slot-2"
    for bad in ("", "  ", "a/b", "shared:", "../x"):
        with pytest.raises(usage_login.UsageLoginError):
            usage_login.new_slot(bad)


def test_new_slot_takes_the_only_shared_account(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def one(settings: Any) -> list[str]:
        return [ACCOUNT]

    async def two(settings: Any) -> list[str]:
        return [ACCOUNT, "shared:Other"]

    monkeypatch.setattr(usage_login, "_shared_accounts", two)
    with pytest.raises(usage_login.UsageLoginError, match="2 shared accounts") as caught:
        usage_login.new_slot(None, settings=Settings(server_url="http://test"))
    assert not any(word in str(caught.value) for word in SECRET_WORDS)
    monkeypatch.setattr(usage_login, "_shared_accounts", one)
    slot = usage_login.new_slot(None, settings=Settings(server_url="http://test"))
    assert usage_login.slot_account(slot) == ACCOUNT


def test_slot_path_refuses_anything_but_a_slot(home: Path) -> None:
    slot = usage_login.new_slot(ACCOUNT)
    assert usage_login.slot_path(str(slot)) == slot
    for bad in (str(home / ".claude"), str(slot.parent), str(slot / "config"), "/tmp/slot-1"):
        with pytest.raises(usage_login.UsageLoginError):
            usage_login.slot_path(bad)


def test_check_read_passes_only_on_a_full_sign_in(home: Path, tmp_path: Path) -> None:
    claude, _log = _fake_claude(tmp_path)
    slot = usage_login.new_slot(ACCOUNT)
    passed, line = usage_login.check_read(slot, claude)
    assert not passed and line.startswith("FAIL") and "setup token" in line
    (slot / "config" / "logged-in").write_text("", encoding="utf-8")
    passed, line = usage_login.check_read(slot, claude)
    assert passed and line == "PASS: the usage read works for that account (weekly 91%)."


def test_check_chat_uses_the_chat_token_in_a_fresh_folder(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, log = _fake_claude(tmp_path)
    slot = usage_login.new_slot(ACCOUNT)
    monkeypatch.setattr(usage_login, "_chat_token", lambda account: "sk-ant-oat01-chat")
    passed, line = usage_login.check_chat(slot, claude)
    assert passed and line == "PASS: a chat on that account still replies."
    [call] = _calls(log)
    assert call["token"] is True
    assert call["config"] and not call["config"].startswith(str(home))
    monkeypatch.setattr(usage_login, "_chat_token", lambda account: "sk-ant-oat01-revoked")
    passed, line = usage_login.check_chat(slot, claude)
    assert not passed and line == "FAIL: a chat on that account did not reply (exit 1)."


def test_main_removes_a_slot_and_prints_no_name(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    slot = usage_login.new_slot(ACCOUNT)
    assert usage_login.main(["remove", str(slot)]) == 0
    assert not slot.exists()
    assert usage_login.main(["remove", str(home / ".claude")]) == 1
    printed = capsys.readouterr()
    assert not any(word in printed.out + printed.err for word in SECRET_WORDS)


# ── review fixes (2026-11-16) ───────────────────────────────────────────


def test_scan_skips_a_symlinked_config(tmp_path: Path) -> None:
    root = tmp_path / "logins"
    slot = root / "slot-1"
    slot.mkdir(parents=True)
    (slot / "account").write_text(ACCOUNT, encoding="utf-8")
    (slot / "config").symlink_to(tmp_path)
    assert limits_relay.scan_usage_logins(root) == []


def test_the_server_checks_the_account_alphabet_too() -> None:
    for bad in ("a/b", "../x", "shared:", " lead", "x" * 70):
        with pytest.raises(ValueError):
            AccountProbe(account=bad, observed_at=datetime.now(UTC))
    assert AccountProbe(account=ACCOUNT, observed_at=datetime.now(UTC)).account == ACCOUNT


async def test_one_slots_surprise_never_costs_the_other_readings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, _log = _fake_claude(tmp_path)
    root = tmp_path / "logins"
    _slot(root, "slot-1", "Broken")
    _slot(root, "slot-2", ACCOUNT)
    real = limits_relay._read_slot

    async def flaky(claude_bin: str, slot: Path, account: str, now: datetime) -> Any:
        if account == "Broken":
            raise ValueError("odd reply")
        return await real(claude_bin, slot, account, now)

    monkeypatch.setattr(limits_relay, "_read_slot", flaky)
    probes, line, _ = await limits_relay.read_usage_logins(claude, root)
    assert [p.account for p in probes] == [ACCOUNT]
    assert "1 read" in str(line) and "1 unreadable" in str(line)
    assert "odd reply" not in str(line)


async def test_a_probes_only_post_never_makes_the_run_pass(app: FastAPI, tmp_path: Path) -> None:
    claude, _log = _fake_claude(tmp_path)
    root = tmp_path / "logins"
    _slot(root, "slot-1", ACCOUNT)
    codex = tmp_path / "codex-broken"
    codex.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n", encoding="utf-8")
    codex.chmod(0o700)
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=tmp_path / "missing.json",
        codex_bin=str(codex),
        claude_bin=claude,
        usage_logins=root,
        settings=Settings(server_url="http://test"),
        transport=httpx.ASGITransport(app=app),
        out=out,
    )
    # Codex missed and nothing but the probe posted: the unit still fails, as before.
    assert status == 1, out.getvalue()
    assert "usage-login readings only" in out.getvalue()


def _old_server(seen: list[dict[str, Any]], status_without: int = 200) -> httpx.MockTransport:
    """A server from before 2026-11-16: it refuses account_probes with 422."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "account_probes" in body:
            return httpx.Response(422, json={"detail": [{"input": ACCOUNT}]})
        return httpx.Response(status_without, json={"kept": True})

    return httpx.MockTransport(handler)


async def test_an_older_server_still_gets_the_default_reading_and_no_stamp(
    tmp_path: Path,
) -> None:
    claude, _log = _fake_claude(tmp_path)
    root = tmp_path / "logins"
    slot = _slot(root, "slot-1", ACCOUNT)
    statusline = tmp_path / "statusline.json"
    reset = int((datetime.now(UTC) + timedelta(hours=2)).timestamp())
    statusline.write_text(
        json.dumps({"rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": reset}}}),
        encoding="utf-8",
    )
    seen: list[dict[str, Any]] = []
    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=statusline,
        codex_bin=None,
        claude_bin=claude,
        usage_logins=root,
        settings=Settings(server_url="http://test"),
        transport=_old_server(seen),
        out=out,
    )
    assert status == 0, out.getvalue()
    assert ["account_probes" in body for body in seen] == [True, False]
    assert "claude: posted (5-hour limit 5%)" in out.getvalue()
    assert "refused the usage-login readings (422)" in out.getvalue()
    assert not any(word in out.getvalue() for word in SECRET_WORDS)
    assert not (slot / "last-read").exists()


async def test_a_failed_post_leaves_the_slot_due(tmp_path: Path) -> None:
    claude, _log = _fake_claude(tmp_path)
    root = tmp_path / "logins"
    slot = _slot(root, "slot-1", ACCOUNT)

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="maintenance")

    out = io.StringIO()
    status = await limits_relay.run(
        claude_path=tmp_path / "missing.json",
        codex_bin=None,
        claude_bin=claude,
        usage_logins=root,
        settings=Settings(server_url="http://test"),
        transport=httpx.MockTransport(down),
        out=out,
    )
    assert status == 1
    assert not (slot / "last-read").exists()


def test_new_says_whether_it_made_the_slot(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert usage_login.main(["new", ACCOUNT]) == 0
    first = capsys.readouterr().out.splitlines()
    assert first[0].endswith("slot-1") and first[1] == "created"
    usage_login._CREATED.clear()
    assert usage_login.main(["new", ACCOUNT]) == 0
    assert capsys.readouterr().out.splitlines()[1] == "existing"


def test_slot_path_refuses_a_symlinked_config(home: Path, tmp_path: Path) -> None:
    slot = usage_login.new_slot(ACCOUNT)
    (slot / "config").rmdir()
    (slot / "config").symlink_to(home)
    with pytest.raises(usage_login.UsageLoginError, match="symbolic link"):
        usage_login.slot_path(str(slot))


def test_check_chat_never_passes_a_gateway_url(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude, log = _fake_claude(tmp_path)
    slot = usage_login.new_slot(ACCOUNT)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.invalid")
    monkeypatch.setattr(usage_login, "_chat_token", lambda account: "sk-ant-oat01-chat")
    passed, _line = usage_login.check_chat(slot, claude)
    assert passed
    assert _calls(log)[-1]["base_url"] is False
    env = limits_relay.usage_login_env(slot, {"ANTHROPIC_BASE_URL": "https://gateway.invalid"})
    assert "ANTHROPIC_BASE_URL" not in env


def test_the_state_file_holds_probes_only_in_the_account_rows(tmp_path: Path) -> None:
    path = tmp_path / "limits.json"
    store = limits.LimitStore(path)
    now = datetime.now(UTC)
    store.put(_host(now, account_probes=[_probe(now)]))
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "account_probes" not in raw["claude"]["snapshot"]
    assert any(row["account"] == ACCOUNT for row in raw["accounts"])
    reloaded = {r.account: r for r in limits.LimitStore(path).accounts()}
    assert reloaded[ACCOUNT].observed_at == now
