"""GET /v1/ae/decision, its reason order, its cache, and the judge tools it hides."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from test_mcp import _stub

from ae_omni_policies import decision_shadow as ds
from ae_omni_policies import decision_status as st
from ae_omni_policies import mcp as mcp_module
from ae_omni_policies.mcp import JUDGE_TOOLS, TOOL_NAMES, OmniClient, create_server

KEY = "decision-test-key-0123456789"
ENDPOINT = "https://decision.example.com"
URL = "/v1/ae/decision"


@pytest.fixture
def on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ds.reset()
    deny = tmp_path / "deny.txt"
    deny.write_text("Zorbex\n", encoding="utf-8")
    for k, v in {
        "AE_DECISION_ENABLED": "1",
        "AE_DECISION_ENDPOINT": ENDPOINT,
        "AE_DECISION_API_KEY": KEY,
        "AE_DECISION_USERS": "local",
        "AE_DECISION_DENYLIST_FILE": str(deny),
        "AE_DECISION_DEPLOYMENT": "decision-1",
        "AE_DECISION_SHADOW_FILE": str(tmp_path / "shadow.jsonl"),
    }.items():
        monkeypatch.setenv(k, v)
    return deny


async def get(client: httpx.AsyncClient) -> dict[str, Any]:
    st.reset()
    resp = await client.get(URL)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_available(client: httpx.AsyncClient, on: Path) -> None:
    body = await get(client)
    assert body["available"] is True and body["configured"] is True
    assert body["enabled"] is True and "reason" not in body
    assert body["model"] == "decision-1" and body["retires"] is None
    assert body["set_up"] is True
    assert body["calls_today"] == 0 and body["daily_cap"] == ds.DEFAULT_DAILY_CAP
    assert body["paused_until"] is None
    names = [p["name"] for p in body["points"]]
    assert "agent_ask" in names and "advisor" in names and "step_down" not in names
    # Both answer now, before anyone has asked (the card read Off for both on 2026-11-07).
    modes = {p["name"]: p["mode"] for p in body["points"]}
    assert modes["agent_ask"] == ds.LIVE and modes["advisor"] == ds.LIVE
    assert body["shadow"] == {"would_act_7d": 0, "lines_7d": 0}


async def test_agent_ask_kill_switch(
    client: httpx.AsyncClient, on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_POINTS", "agent_ask:off")
    modes = {p["name"]: p["mode"] for p in (await get(client))["points"]}
    assert modes["agent_ask"] == ds.OFF and modes["advisor"] == ds.LIVE


@pytest.mark.parametrize(
    ("drop", "reason"),
    [
        ("AE_DECISION_ENABLED", "not_enabled"),
        ("AE_DECISION_ENDPOINT", "no_endpoint"),
        ("AE_DECISION_API_KEY", "no_key"),
        ("AE_DECISION_DENYLIST_FILE", "no_denylist"),
        ("AE_DECISION_USERS", "no_users"),
    ],
)
async def test_each_reason(
    client: httpx.AsyncClient, on: Path, monkeypatch: pytest.MonkeyPatch, drop: str, reason: str
) -> None:
    monkeypatch.delenv(drop)
    body = await get(client)
    assert body["available"] is False and body["reason"] == reason


async def test_not_allowed_and_precedence(
    client: httpx.AsyncClient, on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_USERS", "someone-else")
    assert (await get(client))["reason"] == "not_allowed"
    # Everything missing: the first reason in the fixed order wins.
    for k in ("AE_DECISION_ENDPOINT", "AE_DECISION_API_KEY", "AE_DECISION_USERS"):
        monkeypatch.delenv(k)
    assert (await get(client))["reason"] == "no_endpoint"
    monkeypatch.setenv("AE_DECISION_ENDPOINT", ENDPOINT)
    assert (await get(client))["reason"] == "no_key"
    monkeypatch.setenv("AE_DECISION_API_KEY", KEY)
    on.write_text("# nothing\n", encoding="utf-8")
    body = await get(client)
    assert body["reason"] == "no_denylist" and body["configured"] is False
    on.write_text("Zorbex\n", encoding="utf-8")
    assert (await get(client))["reason"] == "no_users"


async def test_model_defaults_when_the_deployment_is_unset(
    client: httpx.AsyncClient, on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_DECISION_DEPLOYMENT")
    assert (await get(client))["model"] == "decision-1"


async def test_set_up_is_true_when_anything_is_set_even_if_disabled(
    client: httpx.AsyncClient, on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_DECISION_ENABLED")
    monkeypatch.delenv("AE_DECISION_ENDPOINT")
    monkeypatch.delenv("AE_DECISION_API_KEY")
    body = await get(client)
    assert body["set_up"] is True and body["configured"] is False and body["enabled"] is False
    for k in ("AE_DECISION_USERS", "AE_DECISION_DENYLIST_FILE", "AE_DECISION_DEPLOYMENT"):
        monkeypatch.delenv(k)
    body = await get(client)
    assert body["set_up"] is False and body["reason"] == "not_enabled"


async def test_an_invalid_utf8_denylist_fails_closed(client: httpx.AsyncClient, on: Path) -> None:
    on.write_bytes(b"\xff\xfe\x00bad\n")
    body = await get(client)
    assert body["reason"] == "no_denylist" and body["configured"] is False


def test_two_callers_do_not_share_a_cached_answer(
    on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_USERS", "a")
    st.reset()
    assert st.status("a")["available"] is True
    assert st.status("b")["reason"] == "not_allowed"
    assert st.status("a")["available"] is True


async def test_no_secret_in_the_answer(client: httpx.AsyncClient, on: Path) -> None:
    resp = await client.get(URL)
    for secret in (KEY, ENDPOINT, "decision.example.com", "Zorbex", str(on)):
        assert secret not in resp.text


async def test_retires_and_points(
    client: httpx.AsyncClient, on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_DECISION_RETIRES", "2026-12-31")
    monkeypatch.setenv("AE_DECISION_POINTS", "step_down:shadow,custom_x:live")
    body = await get(client)
    assert body["retires"] == "2026-12-31"
    modes = {p["name"]: p["mode"] for p in body["points"]}
    assert modes["step_down"] == "shadow" and "custom_x" in modes
    monkeypatch.setenv("AE_DECISION_RETIRES", "soon")
    assert (await get(client))["retires"] is None


async def test_shadow_counts_and_usage(
    client: httpx.AsyncClient, on: Path, tmp_path: Path
) -> None:
    now = ds._iso(ds._now())
    old = ds._iso(ds._now() - 10 * 86400)
    rows = [
        {"at": now, "would_act": True},
        {"at": now, "would_act": False},
        {"at": old, "would_act": True},
    ]
    (tmp_path / "shadow.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8"
    )
    ds._limits.day_count = 3
    ds._limits.day = ds.datetime.fromtimestamp(ds._now(), ds.UTC).strftime("%Y-%m-%d")
    ds._limits.open_until = ds._mono() + 120
    body = await get(client)
    assert body["shadow"] == {"would_act_7d": 1, "lines_7d": 2}
    assert body["calls_today"] == 3
    assert body["paused_until"] and body["paused_until"].endswith("Z")


async def test_cache_holds_for_sixty_seconds(
    client: httpx.AsyncClient, on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await get(client))["available"] is True
    monkeypatch.delenv("AE_DECISION_ENABLED")
    assert (await client.get(URL)).json()["available"] is True  # cached
    st.reset()
    assert (await client.get(URL)).json()["reason"] == "not_enabled"


def test_a_read_error_keeps_the_fields_it_can(on: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_env: Any) -> Any:
        raise OSError("disk")

    monkeypatch.setattr(ds, "usage", boom)
    body = st.build("local")
    assert body["error"] == "OSError" and body["available"] is True


def test_a_failure_before_the_end_still_has_the_full_shape(
    on: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_caller: Any, _env: Any) -> Any:
        raise RuntimeError("x")

    monkeypatch.setattr(st, "reason", boom)
    body = st.build("local")
    assert body["error"] == "RuntimeError"
    assert body["points"] == [] and body["shadow"] == {"would_act_7d": 0, "lines_7d": 0}
    assert body["calls_today"] == 0 and body["paused_until"] is None


# ── tool hiding ──


def _server(answer: dict[str, Any] | int, seen: list[str]) -> OmniClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if isinstance(answer, int):
            return httpx.Response(answer, json={})
        return httpx.Response(200, json=answer)

    return _stub(handler)


async def _names(omni: OmniClient) -> list[str]:
    async with create_connected_server_and_client_session(create_server(omni)) as s:
        return [t.name for t in (await s.list_tools()).tools]


async def test_tools_shown_when_available() -> None:
    seen: list[str] = []
    names = await _names(_server({"available": True}, seen))
    assert names == list(TOOL_NAMES) and seen == [URL]


@pytest.mark.parametrize("answer", [{"available": False, "reason": "not_enabled"}, {}, 500, 403])
async def test_tools_hidden_when_not_available_or_unreadable(answer: Any) -> None:
    names = await _names(_server(answer, []))
    assert not set(names) & JUDGE_TOOLS
    assert "park_fork" in names


async def test_tools_hidden_when_the_server_never_answers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    assert not set(await _names(_stub(handler))) & JUDGE_TOOLS


async def test_the_answer_is_cached() -> None:
    seen: list[str] = []
    omni = _server({"available": True}, seen)
    await _names(omni)
    await _names(omni)
    assert seen == [URL]
    omni.judge_seen = (0.0, True)  # expired
    await _names(omni)
    assert seen == [URL, URL]


async def test_a_failed_read_is_cached_for_five_seconds_a_good_one_for_sixty() -> None:
    import time

    bad = _server(500, [])
    await _names(bad)
    assert bad.judge_seen is not None and bad.judge_seen[1] is False
    assert bad.judge_seen[0] - time.monotonic() <= mcp_module.JUDGE_FAIL_CACHE_S
    good = _server({"available": True}, [])
    await _names(good)
    assert good.judge_seen is not None
    assert good.judge_seen[0] - time.monotonic() > mcp_module.JUDGE_FAIL_CACHE_S


async def test_a_slow_read_times_out_and_hides_the_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    monkeypatch.setattr(mcp_module, "JUDGE_TIMEOUT_S", 0.05)

    async def slow(*_a: Any, **_k: Any) -> Any:
        await asyncio.sleep(1)
        return 200, {"available": True}

    omni = _server({"available": True}, [])
    monkeypatch.setattr(omni, "call", slow)
    assert not set(await _names(omni)) & JUDGE_TOOLS
    assert omni.judge_seen is not None and omni.judge_seen[1] is False
