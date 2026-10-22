"""The box host's reconnect: the store, the tile's state, the CLI deploy/host-relink.sh runs,
and the two routes (2026-10-20)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import Request

from ae_omni_policies import host_relink
from ae_omni_policies import router as router_module
from ae_omni_policies.host_relink import HostRelinkStore, Status
from ae_omni_policies.host_relink import Request as Ask

NOW = 1_792_520_000  # 2026-10-20T18:13:20Z
BOX = {"host_id": "host_box", "name": "ae-llm-box", "status": "online", "sandbox_provider": None}
OFFLINE = {**BOX, "status": "offline"}


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # No folder: the store stays in memory, so nothing writes to /data.
    monkeypatch.delenv(host_relink.DIR_ENV, raising=False)
    monkeypatch.delenv("AE_RESUME_HOST", raising=False)
    monkeypatch.setattr(host_relink, "DATA_DIR", Path("/nonexistent-ae-data"))
    host_relink.reset_store()
    yield
    host_relink.reset_store()


# ── the store ───────────────────────────────────────────────────────────


def test_status_and_request_round_trip_through_the_files(tmp_path: Path) -> None:
    store = HostRelinkStore(tmp_path / "data")
    assert store.read_status() is None and store.read_request() is None
    store.write_status(
        Status(phase="sent", updated_at=NOW, attempt=2, attempts=5, expires_at=NOW + 300)
    )
    again = HostRelinkStore(tmp_path / "data")
    status = again.read_status()
    assert status is not None and (status.phase, status.attempt, status.expires_at) == (
        "sent",
        2,
        NOW + 300,
    )
    store.request("reid", now=NOW)
    assert again.read_request() == Ask(requested_at=NOW, requested_by="reid")
    assert again.take() == Ask(requested_at=NOW, requested_by="reid")
    assert again.take() is None
    assert not (tmp_path / "data" / host_relink.REQUEST_FILE_NAME).exists()
    assert [p.name for p in (tmp_path / "data").iterdir()] == [host_relink.STATUS_FILE_NAME]


def test_the_status_file_never_holds_a_link(tmp_path: Path) -> None:
    store = HostRelinkStore(tmp_path)
    store.write_status(Status(phase="sent", updated_at=NOW, expires_at=NOW + 300))
    body = json.loads((tmp_path / host_relink.STATUS_FILE_NAME).read_text())
    assert set(body) == {
        "version",
        "phase",
        "updated_at",
        "streak",
        "since",
        "attempt",
        "attempts",
        "expires_at",
        "message",
        "reason",
    }


@pytest.mark.parametrize(
    "body", ["{", "[]", '{"version": 2}', '{"version": 1, "phase": "nope", "updated_at": 1}']
)
def test_a_corrupt_status_file_is_a_reason(tmp_path: Path, body: str) -> None:
    (tmp_path / host_relink.STATUS_FILE_NAME).write_text(body)
    with pytest.raises(ValueError, match=host_relink.STATUS_FILE_NAME):
        HostRelinkStore(tmp_path).read_status()


def test_a_corrupt_request_is_still_taken_once(tmp_path: Path) -> None:
    (tmp_path / host_relink.REQUEST_FILE_NAME).write_text("not json")
    store = HostRelinkStore(tmp_path)
    taken = store.take()
    assert taken is not None and taken.requested_by == "unknown"
    assert store.take() is None


def test_default_folder_follows_env_then_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert host_relink.store().folder is None
    monkeypatch.setattr(host_relink, "DATA_DIR", tmp_path)
    host_relink.reset_store()
    assert host_relink.store().folder == tmp_path
    monkeypatch.setenv(host_relink.DIR_ENV, str(tmp_path / "elsewhere"))
    host_relink.reset_store()
    assert host_relink.store().folder == tmp_path / "elsewhere"


# ── the tile's state ────────────────────────────────────────────────────


def test_pick_host_prefers_the_named_then_the_only_real_one() -> None:
    sandbox = {"host_id": "sb", "name": "modal", "status": "online", "sandbox_provider": "modal"}
    assert host_relink.pick_host([sandbox, OFFLINE], None) == OFFLINE
    other = {**BOX, "host_id": "h2", "name": "laptop"}
    assert host_relink.pick_host([OFFLINE, other], None) == other
    assert host_relink.pick_host([OFFLINE, other], "ae-llm-box") == OFFLINE
    assert host_relink.pick_host([OFFLINE], "missing") is None
    assert host_relink.pick_host([sandbox], None) is None
    # Two offline hosts and no AE_RESUME_HOST: ambiguous, as Resume finds it.
    assert host_relink.pick_host([OFFLINE, {**OFFLINE, "host_id": "h3"}], None) is None
    assert host_relink.pick_host([OFFLINE, other], "h2") == other


def test_old_rejected_and_failed_reports_are_not_shown_as_current() -> None:
    rejected = Status(phase="rejected", updated_at=NOW, streak=60)
    assert host_relink.view(OFFLINE, rejected, None, now=NOW + 60).state == "rejected"
    stale = host_relink.view(OFFLINE, rejected, None, now=NOW + host_relink.STALE_STEP_S + 1)
    assert stale.state == "offline" and stale.streak == 0
    failed = Status(phase="failed", updated_at=NOW, attempts=5, reason="lapsed")
    assert host_relink.view(OFFLINE, failed, None, now=NOW + 3600).state == "failed"
    old = host_relink.view(OFFLINE, failed, None, now=NOW + host_relink.STALE_FAILED_S + 1)
    assert old.state == "offline" and old.reason is None


def test_a_failed_report_says_why() -> None:
    for reason in host_relink.REASONS:
        status = Status(phase="failed", updated_at=NOW, attempts=5, reason=reason)
        assert host_relink.view(OFFLINE, status, None, now=NOW).reason == reason
    rejected = Status(phase="rejected", updated_at=NOW, reason="lapsed")
    assert host_relink.view(OFFLINE, rejected, None, now=NOW).reason is None


def test_a_connected_host_hides_the_button_whatever_the_helper_said() -> None:
    old = Status(phase="sent", updated_at=NOW - 10, expires_at=NOW + 200)
    shown = host_relink.view(BOX, old, Ask(NOW - 5, "reid"), now=NOW)
    assert shown.state == "connected"
    assert shown.can_request is False and shown.requested is False
    assert shown.expires_at is None and shown.streak == 0
    assert shown.host == {"id": "host_box", "name": "ae-llm-box", "status": "online"}


def test_a_rejected_host_offers_the_button_with_the_streak() -> None:
    status = Status(phase="rejected", updated_at=NOW - 3, streak=41, since=NOW - 120)
    shown = host_relink.view(OFFLINE, status, None, now=NOW)
    assert shown.state == "rejected" and shown.can_request is True
    assert shown.streak == 41 and shown.since == "2026-10-20T18:11:20Z"


def test_a_sent_link_counts_down_and_then_lapses() -> None:
    status = Status(phase="sent", updated_at=NOW, attempt=1, attempts=5, expires_at=NOW + 252)
    shown = host_relink.view(OFFLINE, status, None, now=NOW)
    assert shown.state == "sent" and shown.expires_in_s == 252 and shown.attempt == 1
    # The loop is still running: a second login would race it, so no button.
    assert shown.can_request is False
    lapsed = host_relink.view(OFFLINE, status, None, now=NOW + 300)
    assert lapsed.state == "offline" and lapsed.expires_in_s == 0


def test_a_step_in_progress_disables_the_button_until_it_goes_stale() -> None:
    status = Status(phase="sending", updated_at=NOW, attempt=2, attempts=5)
    assert host_relink.view(OFFLINE, status, None, now=NOW).can_request is False
    stale = host_relink.view(OFFLINE, status, None, now=NOW + host_relink.STALE_STEP_S + 1)
    assert stale.state == "offline" and stale.can_request is True


def test_a_pending_request_disables_the_button_and_says_when_it_is_untaken() -> None:
    shown = host_relink.view(OFFLINE, None, Ask(NOW - 30, "reid"), now=NOW)
    assert shown.requested is True and shown.can_request is False
    assert shown.request_untaken is False
    late = host_relink.view(OFFLINE, None, Ask(NOW - 600, "reid"), now=NOW)
    assert late.request_untaken is True


def test_no_host_row_is_unknown_and_offers_nothing() -> None:
    shown = host_relink.view(None, None, None, error="listing the hosts failed: 500", now=NOW)
    assert shown.state == "unknown" and shown.can_request is False
    assert shown.error == "listing the hosts failed: 500"


def test_public_link_moves_the_ticket_to_the_callback_host() -> None:
    env = {"OMNIGENT_OIDC_REDIRECT_URI": "https://omnigent.example.org/auth/callback"}
    link = "https://box.tailnet.ts.net:8010/auth/login?ticket=abc"
    assert host_relink.public_link(link, env) == (
        "https://omnigent.example.org/auth/login?ticket=abc"
    )
    assert host_relink.public_link(link, {}) == link
    assert host_relink.public_link(link, {"OMNIGENT_OIDC_REDIRECT_URI": "nonsense"}) == link


# ── the CLI ─────────────────────────────────────────────────────────────


def test_cli_status_writes_the_report(capsys: pytest.CaptureFixture[str]) -> None:
    code = host_relink.main(
        ["status", "sent", "attempt=2", "attempts=5", f"expires_at={NOW + 300}"], now=NOW
    )
    assert code == 0 and capsys.readouterr().out == "ok sent\n"
    status = host_relink.store().read_status()
    assert status == Status(
        phase="sent", updated_at=NOW, attempt=2, attempts=5, expires_at=NOW + 300
    )
    assert host_relink.main(["status", "rejected", "message=The reminder failed."], now=NOW) == 0
    again = host_relink.store().read_status()
    assert again is not None and again.message == "The reminder failed." and again.attempt == 0
    assert host_relink.main(["status", "failed", "attempts=5", "reason=restart"], now=NOW) == 0
    failed = host_relink.store().read_status()
    assert failed is not None and failed.reason == "restart"


def test_the_reason_survives_the_file(tmp_path: Path) -> None:
    store = HostRelinkStore(tmp_path)
    store.write_status(Status(phase="failed", updated_at=NOW, reason="push"))
    again = HostRelinkStore(tmp_path).read_status()
    assert again is not None and again.reason == "push"
    (tmp_path / host_relink.STATUS_FILE_NAME).write_text(
        '{"version": 1, "phase": "failed", "updated_at": 1, "reason": "nope"}'
    )
    with pytest.raises(ValueError, match="unknown reason"):
        HostRelinkStore(tmp_path).read_status()


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["status"],
        ["status", "nope"],
        ["status", "sent", "attempt"],
        ["status", "sent", "x=1"],
        ["status", "sent", "attempt=-1"],
        ["status", "failed", "reason=nope"],
        ["take", "extra"],
        ["push", "http://x", "1"],
        ["push", "https://x"],
        ["giveup", "five"],
        ["what"],
    ],
)
def test_cli_refuses_bad_input_with_exit_2(argv: list[str]) -> None:
    assert host_relink.main(argv, now=NOW) == 2


def test_cli_take_prints_and_removes_the_request(capsys: pytest.CaptureFixture[str]) -> None:
    assert host_relink.main(["take"]) == 0
    assert capsys.readouterr().out == "none\n"
    host_relink.store().request("Reid Havens", now=NOW)
    assert host_relink.main(["take"]) == 0
    assert capsys.readouterr().out == f"request {NOW} Reid_Havens\n"
    assert host_relink.store().read_request() is None


def test_cli_push_sends_the_public_link_as_a_reminder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OMNIGENT_OIDC_REDIRECT_URI", "https://omnigent.example.org/auth/callback")
    sent: list[tuple[str, str]] = []

    async def remind(title: str, notes: str, now: int) -> bool:
        sent.append((title, notes))
        return True

    monkeypatch.setattr(host_relink, "_remind", remind)
    link = "https://box.tailnet.ts.net:8010/auth/login?ticket=t1"
    assert host_relink.main(["push", link, str(NOW + 300)], now=NOW) == 0
    assert capsys.readouterr().out == "ok\n"
    title, notes = sent[0]
    assert "reconnect" in title.lower()
    assert "https://omnigent.example.org/auth/login?ticket=t1" in notes
    assert "18:18Z" in notes
    assert host_relink.main(["giveup", "5"], now=NOW) == 0
    assert "still disconnected" in sent[1][0]


def test_cli_push_exits_1_when_the_store_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    async def refuse(title: str, notes: str, now: int) -> bool:
        return False

    monkeypatch.setattr(host_relink, "_remind", refuse)
    assert host_relink.main(["push", "https://x/auth/login?ticket=t", str(NOW)], now=NOW) == 1


def test_remind_without_a_service_token_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AE_CF_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("AE_CF_ACCESS_CLIENT_SECRET", raising=False)
    assert asyncio.run(host_relink._remind("t", "n", NOW)) is False


def test_cli_runs_as_a_module(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", host_relink.DIR_ENV: str(tmp_path)}
    run = subprocess.run(
        [sys.executable, "-m", "ae_omni_policies.host_relink", "status", "rejected", "streak=30"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip().splitlines()[-1] == "ok rejected"
    assert json.loads((tmp_path / host_relink.STATUS_FILE_NAME).read_text())["streak"] == 30


# ── the routes ──────────────────────────────────────────────────────────


class _Hosts:
    """Stands in for the loopback: answers ``GET /v1/hosts`` only."""

    def __init__(self, hosts: list[dict[str, Any]] | None, status: int = 200) -> None:
        self.hosts = hosts
        self.status = status

    async def __call__(
        self, request: Request, method: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        assert (method, path) == ("GET", "/v1/hosts")
        return httpx.Response(self.status, json={"hosts": self.hosts or []})


async def test_get_host_says_connected(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "_loopback", _Hosts([BOX]))
    response = await client.get("/v1/ae/host")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "connected" and body["can_request"] is False
    assert body["host"]["name"] == "ae-llm-box" and body["error"] is None


async def test_get_host_shows_the_helpers_report_while_rejected(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "_loopback", _Hosts([OFFLINE]))
    host_relink.main(["status", "rejected", "streak=41"])
    body = (await client.get("/v1/ae/host")).json()
    assert body["state"] == "rejected" and body["streak"] == 41 and body["can_request"] is True


async def test_get_host_reports_a_failed_host_list(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "_loopback", _Hosts(None, status=503))
    body = (await client.get("/v1/ae/host")).json()
    assert body["state"] == "unknown" and body["error"] == "listing the hosts failed: 503"


async def test_post_relink_records_a_request_for_a_rejected_host(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "_loopback", _Hosts([OFFLINE]))
    response = await client.post("/v1/ae/host/relink", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requested"] is True and body["can_request"] is False
    asked = host_relink.store().read_request()
    assert asked is not None and asked.requested_by == "local"


async def test_post_relink_refuses_a_connected_host(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "_loopback", _Hosts([BOX]))
    response = await client.post("/v1/ae/host/relink", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == host_relink.CODE_CONNECTED
    assert host_relink.store().read_request() is None


async def test_post_relink_refuses_while_a_login_loop_runs(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stale tab's button would otherwise queue a second loop behind the first.
    monkeypatch.setattr(router_module, "_loopback", _Hosts([OFFLINE]))
    assert host_relink.main(["status", "sending", "attempt=1", "attempts=5"]) == 0
    response = await client.post("/v1/ae/host/relink", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == host_relink.CODE_BUSY
    assert host_relink.store().read_request() is None


async def test_the_card_and_resume_pick_the_same_host(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    laptop = {**BOX, "host_id": "h2", "name": "laptop"}
    box_offline = {**OFFLINE}
    monkeypatch.setattr(router_module, "_loopback", _Hosts([box_offline, laptop]))
    # Resume picks the only online host; so does the card, unless AE_RESUME_HOST names one.
    resumed = await router_module._resume_host(Request({"type": "http"}))
    body = (await client.get("/v1/ae/host")).json()
    assert resumed["host_id"] == "h2" and body["host"]["id"] == "h2"
    monkeypatch.setenv("AE_RESUME_HOST", "ae-llm-box")
    body = (await client.get("/v1/ae/host")).json()
    assert body["host"]["id"] == "host_box" and body["state"] == "offline"
    with pytest.raises(router_module._NoTarget, match="ae-llm-box"):
        await router_module._resume_host(Request({"type": "http"}))
    monkeypatch.delenv("AE_RESUME_HOST")
    monkeypatch.setattr(
        router_module, "_loopback", _Hosts([box_offline, {**laptop, "status": "offline"}])
    )
    body = (await client.get("/v1/ae/host")).json()
    assert body["host"] is None and "AE_RESUME_HOST" in body["error"]


async def test_post_relink_without_a_host_is_502(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "_loopback", _Hosts([]))
    response = await client.post("/v1/ae/host/relink", json={})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == host_relink.CODE_NO_HOST


async def test_post_relink_needs_json(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "_loopback", _Hosts([OFFLINE]))
    response = await client.post(
        "/v1/ae/host/relink", content="x", headers={"content-type": "text/plain"}
    )
    assert response.status_code == 415
    assert host_relink.store().read_request() is None


async def test_post_relink_reports_a_save_failure(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(router_module, "_loopback", _Hosts([OFFLINE]))
    blocker = tmp_path / "file"
    blocker.write_text("")
    host_relink.reset_store(HostRelinkStore(blocker / "sub"))
    response = await client.post("/v1/ae/host/relink", json={})
    assert response.status_code == 500
    assert response.json()["error"]["code"] == host_relink.CODE_NOT_SAVED
