"""The Home security card's server read: off, unreachable, stale, links (2026-11-08)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from ae_omni_policies import admin_cards, home_security

OWNER = {"X-Forwarded-Email": "owner@example.com", "Sec-Fetch-Site": "same-origin"}
MEMBER = {"X-Forwarded-Email": "member@example.com", "Sec-Fetch-Site": "same-origin"}
BASE = "http://home.example.com:8080"
ENV = {"AE_HOME_SECURITY_URL": BASE, "AE_HOME_SECURITY_LINK": "https://home.example.com/security"}
PORTAL = "https://fw.firewalla.net/box/abc"


def _body(**extra: Any) -> dict[str, Any]:
    health = {
        "firewalla": {
            "api": "up",
            "boxOnline": True,
            "checkedAt": "2026-11-08T10:00:00Z",
            "alarms24h": 3,
            "portalUrl": PORTAL,
        }
    }
    body: dict[str, Any] = {
        "ok": True,
        "stale": False,
        "summary": {
            "lastScan": "2026-11-08T09:00:00Z",
            "alerts": [{"id": "a1", "text": "secret detail"}, {"id": "a2"}],
            "health": health,
            "deviceList": [{"mac": "00:00:5e:00:53:01"}],
        },
    }
    body.update(extra)
    return body


@pytest.fixture(autouse=True)
def _fresh() -> None:
    home_security.reset()


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(home_security, "_make_transport", lambda: httpx.MockTransport(respond))
    return seen


def _json(body: Any, status: int = 200) -> Any:
    return lambda request: httpx.Response(status, json=body)


async def test_off_without_a_base_url() -> None:
    assert await home_security.overview({}) == {"off": True, "card": "home-security"}
    assert await home_security.overview({"AE_HOME_SECURITY_URL": "ftp://x.example.com"}) == {
        "off": True,
        "card": "home-security",
    }


async def test_a_good_read_is_compact_and_asks_the_lite_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _serve(monkeypatch, _json(_body()))
    out = await home_security.overview(ENV)
    assert str(seen[0].url) == f"{BASE}/api/security?lite=1"
    assert out["reachable"] is True and out["stale"] is False
    assert out["lastScan"] == "2026-11-08T09:00:00Z"
    assert out["alerts"] == 2
    assert out["firewalla"] == {
        "api": "up",
        "boxOnline": True,
        "checkedAt": "2026-11-08T10:00:00Z",
    }
    assert out["portalUrl"] == PORTAL
    assert out["link"] == "https://home.example.com/security"
    assert out["readAt"].endswith("Z")
    # Nothing of an alert's text or a device list is passed on.
    assert "secret detail" not in str(out) and "00:00:5e" not in str(out)


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(500, text="boom"),
        lambda request: httpx.Response(200, text="not json"),
        lambda request: httpx.Response(200, json=["not", "an", "object"]),
    ],
)
async def test_an_unusable_answer_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> None:
    _serve(monkeypatch, handler)
    out = await home_security.overview(ENV)
    assert out["reachable"] is False and out["alerts"] == 0 and out["firewalla"] is None
    assert out["link"] == "https://home.example.com/security"


async def test_a_refused_connection_is_unreachable_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _serve(monkeypatch, refuse)
    assert (await home_security.overview(ENV))["reachable"] is False


async def test_stale_flag_and_a_missing_summary_read_as_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, _json(_body(stale=True)))
    assert (await home_security.overview(ENV))["stale"] is True
    home_security.reset()
    _serve(monkeypatch, _json({"ok": True}))
    out = await home_security.overview(ENV)
    assert out["reachable"] is True and out["stale"] is True and out["lastScan"] is None


async def test_firewall_absent_gives_null(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _body()
    del body["summary"]["health"]["firewalla"]
    _serve(monkeypatch, _json(body))
    out = await home_security.overview(ENV)
    assert out["reachable"] is True and out["firewalla"] is None and out["portalUrl"] is None


@pytest.mark.parametrize(
    "portal",
    [
        "http://fw.firewalla.net/box",
        "https://evil.example.com/box",
        "https://firewalla.net.evil.example.com/box",
        "https://" + "name" + "@fw.firewalla.net/box",
        "javascript:alert(1)",
        42,
        None,
    ],
)
async def test_a_bad_portal_url_is_dropped(monkeypatch: pytest.MonkeyPatch, portal: Any) -> None:
    body = _body()
    body["summary"]["health"]["firewalla"]["portalUrl"] = portal
    _serve(monkeypatch, _json(body))
    out = await home_security.overview(ENV)
    assert out["portalUrl"] is None
    assert out["firewalla"] is not None


async def test_a_bad_link_is_dropped_and_odd_firewall_fields_are_tamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body()
    body["summary"]["health"]["firewalla"].update({"api": "weird", "boxOnline": "yes"})
    _serve(monkeypatch, _json(body))
    out = await home_security.overview({**ENV, "AE_HOME_SECURITY_LINK": "javascript:alert(1)"})
    assert out["link"] is None
    assert out["firewalla"]["api"] == "unknown" and out["firewalla"]["boxOnline"] is None


async def test_the_read_is_cached_for_a_minute(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, _json(_body()))
    first = await home_security.overview(ENV)
    second = await home_security.overview(ENV)
    assert first is second and len(seen) == 1
    later = home_security.time.monotonic() + home_security.CACHE_S + 1
    monkeypatch.setattr(home_security.time, "monotonic", lambda: later)
    await home_security.overview(ENV)
    assert len(seen) == 2


# ── the route ─────────────────────────────────────────────────────────────────────────────────


async def test_route_is_admin_only(client: httpx.AsyncClient, ae_admins: set[str]) -> None:
    refused = await client.get("/v1/ae/home-security", headers=MEMBER)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "admin_only"


async def test_route_answers_off_when_unset_and_when_switched_off(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_HOME_SECURITY_URL", raising=False)
    got = await client.get("/v1/ae/home-security", headers=OWNER)
    assert got.status_code == 200 and got.json() == {"off": True, "card": "home-security"}
    monkeypatch.setenv("AE_HOME_SECURITY_URL", BASE)
    seen = _serve(monkeypatch, _json(_body()))
    done = await client.post(
        "/v1/ae/cards/state",
        json={"id": "home-security", "on": False, "revision": 0},
        headers=OWNER,
    )
    assert done.status_code == 200, done.text
    got = await client.get("/v1/ae/home-security", headers=OWNER)
    assert got.json() == {"off": True, "card": "home-security"} and seen == []


async def test_route_returns_the_compact_object(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_HOME_SECURITY_URL", BASE)
    _serve(monkeypatch, _json(_body()))
    got = await client.get("/v1/ae/home-security", headers=OWNER)
    assert got.status_code == 200, got.text
    out = got.json()
    assert out["reachable"] is True and out["alerts"] == 2 and out["portalUrl"] == PORTAL


def test_the_catalog_names_both_settings_with_short_hints() -> None:
    card = next(c for c in admin_cards.CATALOG if c.id == "home-security")
    assert [f.env for f in card.fields] == ["AE_HOME_SECURITY_URL", "AE_HOME_SECURITY_LINK"]
    for field in card.fields:
        assert 0 < len(field.hint) <= admin_cards.HINT_MAX and field.env_only


# ── review fixes (2026-11-08) ─────────────────────────────────────────────────────────────────


def _alert(alert_id: str, **requests: Any) -> dict[str, Any]:
    return {"id": alert_id, "requests": requests}


def _count(alerts: list[Any], **extra: Any) -> int:
    body = _body(**extra)
    body["summary"]["alerts"] = alerts
    return home_security.shape(body, None, "2026-11-08T10:00:00Z")["alerts"]


def test_alerts_follow_the_home_apps_own_hiding_rule() -> None:
    received = "2026-11-08T10:00:00Z"
    plain = {"id": "a0"}
    waiting = _alert("a1", reviewed={"status": "requested", "finishedAt": None})
    failed = _alert("a2", reviewed={"status": "failed", "finishedAt": "2026-11-08T09:00:00Z"})
    done_after = _alert("a3", reviewed={"status": "done", "finishedAt": "2026-11-08T10:30:00Z"})
    done_before = _alert("a4", name={"status": "done", "finishedAt": "2026-11-08T09:00:00Z"})
    no_id = {"requests": {"reviewed": {"status": "requested"}}}
    latest_only = {
        "id": "a5",
        "request": {
            "action": "revoke-key",
            "status": "done",
            "finishedAt": "2026-11-08T10:30:00Z",
        },
    }
    assert _count([plain], receivedAt=received) == 1
    # A reviewed request that is waiting hides the alert at once.
    assert _count([waiting], receivedAt=received) == 0
    # A failed request hides nothing.
    assert _count([failed], receivedAt=received) == 1
    # A done request hides it until a summary newer than the request arrives.
    assert _count([done_after], receivedAt=received) == 0
    assert _count([done_before], receivedAt=received) == 1
    assert _count([done_after], receivedAt="2026-11-08T11:00:00Z") == 1
    # An alert with no id is never hidden; a lone latest request counts like a mapped one.
    assert _count([no_id], receivedAt=received) == 1
    assert _count([latest_only], receivedAt=received) == 0
    assert (
        _count([plain, waiting, failed, done_after, done_before, no_id], receivedAt=received) == 4
    )


# The parser-confusion case: a browser reads a backslash as a slash, Python does not.
CONFUSED = "https://evil.example\\sub.firewalla.net/x"


@pytest.mark.parametrize(
    "bad",
    [
        CONFUSED,
        "https://evil.example/\tx.firewalla.net/",
        "https://fw.firewalla.net/ok path",
        "https://fw.firewalla.net/\x00",
        "https://name" + "@fw.firewalla.net/x",
        "https://fw.firewalla.net:notaport/x",
        "https://fе.firewalla.net/x",
    ],
)
def test_portal_and_web_links_refuse_parser_confusion(bad: str) -> None:
    assert home_security.portal_link(bad) is None
    assert home_security.web_link(bad) is None


def test_portal_link_is_canonical_and_accepts_the_bare_host() -> None:
    assert home_security.portal_link("https://FW.Firewalla.NET/box?a=1") == (
        "https://fw.firewalla.net/box?a=1"
    )
    assert home_security.portal_link("https://firewalla.net/") == "https://firewalla.net/"
    assert home_security.portal_link("https://notfirewalla.net/") is None
    assert home_security.portal_link("http://fw.firewalla.net/") is None


async def test_a_confused_portal_url_never_reaches_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = _body()
    body["summary"]["health"]["firewalla"]["portalUrl"] = CONFUSED
    _serve(monkeypatch, _json(body))
    assert (await home_security.overview(ENV))["portalUrl"] is None


async def test_an_oversized_chunked_answer_is_stopped_and_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[int] = []

    async def chunks() -> Any:
        for _ in range(100):
            sent.append(1)
            yield b"x" * (home_security.MAX_BODY // 10)

    _serve(monkeypatch, lambda request: httpx.Response(200, content=chunks()))
    out = await home_security.overview(ENV)
    assert out["reachable"] is False
    # It stopped reading soon after the cap instead of buffering the whole stream.
    assert len(sent) <= 12


@pytest.mark.parametrize("ok", [False, None, "true", 1])
async def test_reachable_only_when_ok_is_true(monkeypatch: pytest.MonkeyPatch, ok: Any) -> None:
    body = _body()
    body["ok"] = ok
    _serve(monkeypatch, _json(body))
    assert (await home_security.overview(ENV))["reachable"] is False


async def test_a_body_with_no_ok_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _body()
    del body["ok"]
    _serve(monkeypatch, _json(body))
    assert (await home_security.overview(ENV))["reachable"] is False


@pytest.mark.parametrize(
    "base", [BASE + "?x=1", BASE + "#frag", BASE + "/a?x=1", "http://user" + "@home.example.com"]
)
async def test_a_base_with_a_query_fragment_or_userinfo_is_not_set_up(base: str) -> None:
    assert await home_security.overview({"AE_HOME_SECURITY_URL": base}) == {
        "off": True,
        "card": "home-security",
    }


def test_the_request_url_is_built_from_parts() -> None:
    assert home_security.request_url("http://home.example.com:8080") == (
        "http://home.example.com:8080/api/security?lite=1"
    )
    assert home_security.request_url("http://home.example.com/app/") == (
        "http://home.example.com/app/api/security?lite=1"
    )


async def test_concurrent_callers_share_one_read(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    seen = _serve(monkeypatch, _json(_body()))
    answers = await asyncio.gather(*(home_security.overview(ENV) for _ in range(5)))
    assert len(seen) == 1
    assert all(answer is answers[0] for answer in answers)
