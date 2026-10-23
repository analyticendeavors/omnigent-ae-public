"""Fabric Admin (2026-10-16): ``/v1/ae/fabric`` on the real app, the Logic Apps mocked.

Nothing here reaches a real Logic App: the routes are driven through respx and
the module's ``call`` through ``httpx.MockTransport``. The wake URL and the
suspend key are secrets, so every test that touches them also asserts they
never reach a response body, an error message or a log line, including when
the Logic App raises or answers 500 with the URL echoed back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterator

import httpx
import pytest
import respx

from ae_omni_policies import auth, fabric

SIG = "sIgNaTuRe_value_never_logged_0123456789"
WAKE_URL = (
    "https://logic.test/workflows/abc123/triggers/manual/paths/invoke"
    f"?api-version=2016-10-01&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig={SIG}"
)
OTHER_SIG = "other_signature_value_never_logged_987"
OTHER_URL = (
    "https://logic.test/workflows/def456/triggers/manual/paths/invoke"
    f"?api-version=2016-10-01&sig={OTHER_SIG}"
)
KEY = "9f2c4e6a8b0d1f3a5c7e9b1d3f5a7c9e0b2d4f6a8c0e1b3d5f7a9c1e3b5d7f90"
INVOKE = "https://logic.test/workflows/abc123/triggers/manual/paths/invoke"
OTHER_INVOKE = "https://logic.test/workflows/def456/triggers/manual/paths/invoke"
SECRETS = (SIG, OTHER_SIG, KEY, "workflows/abc123", "workflows/def456")
CAPACITY_ID = (
    "/subscriptions/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0/resourceGroups/rg-ae-fabric"
    "/providers/Microsoft.Fabric/capacities/aetraining"
)
NO_DETAILS = {
    "sku": None,
    "region": None,
    "capacityId": None,
    "hourlyUsd": None,
    "resumedAt": None,
    "keepAwakeUntil": None,
    "maxHours": None,
    "sleepsAt": None,
    "keepAwakeOptions": [],
    # Unknown maxHours: the setup script's 6, plus the second quiet run.
    "wakeBilledHours": 7,
}


@pytest.fixture(autouse=True)
def _fabric_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import os

    for key in list(os.environ):
        if key.startswith(
            ("AE_FABRIC_WAKE_URL_", "AE_FABRIC_SUSPEND_KEY_", "AE_FABRIC_CAPACITIES_")
        ):
            monkeypatch.delenv(key)
    monkeypatch.delenv("AE_FABRIC_USD_PER_CU_HOUR", raising=False)
    fabric.forget()
    yield
    fabric.forget()


def _configure(monkeypatch: pytest.MonkeyPatch, *, key: bool = True, other: bool = False) -> None:
    monkeypatch.setenv("AE_FABRIC_WAKE_URL_AETRAINING", WAKE_URL)
    if key:
        monkeypatch.setenv("AE_FABRIC_SUSPEND_KEY_AETRAINING", KEY)
    if other:
        monkeypatch.setenv("AE_FABRIC_WAKE_URL_AEPROD", OTHER_URL)


def _answer(state: str, action: str = "none", changed: bool = False) -> dict[str, object]:
    return {"capacity": "aetraining", "state": state, "action": action, "changed": changed}


def _clean(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


def _params(request: httpx.Request) -> dict[str, str]:
    return dict(request.url.params)


# ── configuration ────────────────────────────────────────────────────────


def test_capacities_come_from_the_environment_by_suffix() -> None:
    found = fabric.capacities(
        {
            "AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL,
            "AE_FABRIC_SUSPEND_KEY_AETRAINING": KEY,
            "AE_FABRIC_WAKE_URL_AE_PROD_2": OTHER_URL,
            "AE_FABRIC_WAKE_URL_lower": OTHER_URL,
            "AE_FABRIC_WAKE_URL_EMPTY": "  ",
            "AE_FABRIC_SUSPEND_KEY_NOURL": KEY,
        }
    )
    assert list(found) == ["ae_prod_2", "aetraining"]
    assert found["aetraining"].can_suspend is True
    assert found["ae_prod_2"].can_suspend is False
    assert fabric.find("AETraining", {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL}) is not None
    # The dataclass repr is what a stray log line or traceback would print.
    _clean(repr(found), repr(list(found.values())))


async def test_call_merges_its_parameters_into_the_signed_query() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_answer("Paused"))

    capacity = fabric.capacities({"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL + "&action=stale"})[
        "aetraining"
    ]
    answer = await fabric.call(capacity, "probe", transport=httpx.MockTransport(handler))
    details = answer.pop("details")
    assert answer.pop("sent") == frozenset()
    assert answer == {"name": "aetraining", "state": "Paused", "action": "none", "changed": False}
    # The old-shape answer: every optional field is null.
    assert set(details.values()) == {None}
    params = _params(seen[0])
    assert params["sig"] == SIG
    assert params["sp"] == "/triggers/manual/run"
    assert params["api-version"] == "2016-10-01"
    assert params["format"] == "json"
    # The URL's own action is replaced, not doubled.
    assert params["action"] == "probe"
    assert seen[0].url.params.get_list("action") == ["probe"]
    assert "key" not in params
    assert fabric.SUSPEND_KEY_HEADER not in seen[0].headers


async def test_call_reads_an_odd_state_as_unknown() -> None:
    capacity = fabric.Capacity("aetraining", "AETRAINING", WAKE_URL)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"state": f"<b>{SIG}</b>"})
    )
    answer = await fabric.call(capacity, "probe", transport=transport)
    assert answer["state"] == "Unknown"


# ── GET /v1/ae/fabric ────────────────────────────────────────────────────


async def test_none_configured_says_how_to_turn_it_on(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/ae/fabric")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["capacities"] == []
    assert "AE_FABRIC_WAKE_URL_<NAME>" in body["note"]
    assert "deploy/README.md" in body["note"]


async def test_status_lists_each_capacity_isolated_and_cached(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch, other=True)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        ok = mock.get(url__startswith=INVOKE).respond(200, json=_answer("Paused"))
        # The failing one echoes its own URL back, the way an Azure error can.
        failing = mock.get(url__startswith=OTHER_INVOKE).respond(
            500, json={"error": f"InvalidTemplate at {OTHER_URL} (sig={OTHER_SIG})"}
        )
        first = await client.get("/v1/ae/fabric")
        second = await client.get("/v1/ae/fabric")
        # An answer with an error in it is stale at once: the second GET returned
        # it unchanged and started the refresh that asks the failing one again.
        refresh = fabric.refresh_task()
        assert refresh is not None
        await refresh
    assert first.status_code == 200
    rows = {row["name"]: row for row in first.json()["capacities"]}
    # The old-shape Logic App answer still reads, with every new field null.
    assert rows["aetraining"] == {
        "name": "aetraining",
        "can_suspend": True,
        "can_keep_awake": True,
        "state": "Paused",
        **NO_DETAILS,
    }
    assert rows["aeprod"]["state"] == "Unknown"
    assert rows["aeprod"]["can_suspend"] is False
    assert rows["aeprod"]["error"].startswith("aeprod: the Logic App answered 500")
    assert "[redacted]" in rows["aeprod"]["error"]
    assert _params(ok.calls.last.request)["action"] == "probe"
    assert _params(ok.calls.last.request)["sig"] == SIG
    # The good one is cached for STATUS_TTL_S; the failing one is asked again.
    assert ok.call_count == 1
    assert failing.call_count == 2
    _clean(first.text, second.text, caplog.text)


@pytest.mark.parametrize(
    ("raised", "fragment"),
    [
        (httpx.ConnectError(f"cannot connect to {WAKE_URL}"), "could not be reached"),
        (httpx.ReadTimeout(f"timed out reading {WAKE_URL}"), "within 8 s"),
    ],
)
async def test_a_raising_logic_app_is_a_row_error_without_the_url(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    raised: Exception,
    fragment: str,
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=INVOKE).mock(side_effect=raised)
        response = await client.get("/v1/ae/fabric")
    assert response.status_code == 200
    (row,) = response.json()["capacities"]
    assert row["state"] == "Unknown"
    assert fragment in row["error"]
    _clean(response.text, caplog.text)


# ── POST /v1/ae/fabric/{name}/wake and /suspend ──────────────────────────


async def test_wake_sends_wake_and_drops_the_cached_status(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE)
        route.side_effect = [
            httpx.Response(200, json=_answer("Paused")),
            httpx.Response(200, json=_answer("Waking", "wake", True)),
            httpx.Response(200, json=_answer("Resuming")),
        ]
        await client.get("/v1/ae/fabric")
        woke = await client.post("/v1/ae/fabric/aetraining/wake", json={})
        after = await client.get("/v1/ae/fabric")
    assert woke.status_code == 200, woke.text
    # The full row, as GET shows it, plus what the action did.
    assert woke.json() == {
        "name": "aetraining",
        "state": "Waking",
        "action": "wake",
        "changed": True,
        "can_suspend": True,
        "can_keep_awake": True,
        **NO_DETAILS,
    }
    wake_request = route.calls[1].request
    assert _params(wake_request)["action"] == "wake"
    assert _params(wake_request)["sig"] == SIG
    # A wake never carries the suspend key.
    assert "key" not in _params(wake_request)
    assert "x-suspend-key" not in wake_request.headers
    # The cache went with the wake: the next GET asked again.
    assert route.call_count == 3
    assert after.json()["capacities"][0]["state"] == "Resuming"
    _clean(woke.text, after.text, caplog.text)


async def test_suspend_sends_the_key_and_never_returns_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE).respond(
            200, json=_answer("Suspending", "suspend", True)
        )
        response = await client.post("/v1/ae/fabric/aetraining/suspend", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "Suspending"
    params = _params(route.calls.last.request)
    assert params["action"] == "suspend"
    assert params["sig"] == SIG
    # The key travels in a header, never in the URL.
    request = route.calls.last.request
    assert request.headers["x-suspend-key"] == KEY
    assert "key" not in params
    assert KEY not in str(request.url)
    _clean(response.text, caplog.text)


async def test_suspend_without_a_key_is_refused_before_any_call(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, key=False)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE).respond(200, json=_answer("Active"))
        response = await client.post("/v1/ae/fabric/aetraining/suspend", json={})
        listed = await client.get("/v1/ae/fabric")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == fabric.CODE_SUSPEND_NOT_CONFIGURED
    assert "AE_FABRIC_SUSPEND_KEY_AETRAINING" in error["message"]
    assert "deploy/README.md" in error["message"]
    assert route.call_count == 1  # the GET's probe only
    assert listed.json()["capacities"][0]["can_suspend"] is False
    _clean(response.text)


async def test_a_refused_key_is_a_502_that_never_echoes_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=INVOKE).respond(
            403, json={"error": f"suspend key missing or wrong: {KEY}"}
        )
        response = await client.post("/v1/ae/fabric/aetraining/suspend", json={})
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == fabric.CODE_FAILED
    assert "refused the suspend key" in error["message"]
    _clean(response.text, caplog.text)


@pytest.mark.parametrize(
    ("reply", "status", "code"),
    [
        (httpx.Response(500, json={"error": {"message": f"boom at {WAKE_URL}"}}), 502, "failed"),
        (httpx.Response(500, text=f"<html>{WAKE_URL}</html>"), 502, "failed"),
        (httpx.Response(200, text="not json"), 502, "failed"),
        (httpx.ConnectError(f"no route to {WAKE_URL}"), 502, "failed"),
        (httpx.ConnectTimeout(f"connect timeout {WAKE_URL}"), 504, "timeout"),
    ],
)
async def test_a_failed_wake_names_no_secret(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reply: httpx.Response | Exception,
    status: int,
    code: str,
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE)
        if isinstance(reply, Exception):
            route.mock(side_effect=reply)
        else:
            route.mock(return_value=reply)
        response = await client.post("/v1/ae/fabric/aetraining/wake", json={})
        # Never retried: one call, whatever the answer.
        assert route.call_count == 1
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == f"fabric_{code}"
    assert error["message"].startswith("aetraining: ")
    if code == "timeout":
        assert "look again in a minute" in error["message"]
    _clean(response.text, caplog.text)


async def test_an_unknown_capacity_is_a_404(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE).respond(200, json=_answer("Paused"))
        for action in ("wake", "suspend"):
            response = await client.post(f"/v1/ae/fabric/nosuch/{action}", json={})
            assert response.status_code == 404
        assert not route.called


async def test_wake_is_post_json_only_and_origin_checked(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE).respond(200, json=_answer("Waking", "wake"))
        assert (await client.get("/v1/ae/fabric/aetraining/wake")).status_code in (404, 405)
        form = await client.post(
            "/v1/ae/fabric/aetraining/wake",
            content="x=1",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert form.status_code == 415
        foreign = await client.post(
            "/v1/ae/fabric/aetraining/suspend",
            json={},
            headers={"Origin": "https://evil.example"},
        )
        assert foreign.status_code == 403
        assert not route.called


async def test_wake_needs_an_authenticated_user(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE).respond(200, json=_answer("Waking", "wake"))
        response = await client.post("/v1/ae/fabric/aetraining/wake", json={})
        listed = await client.get("/v1/ae/fabric")
        assert not route.called
    assert response.status_code == 401
    assert listed.status_code == 401
    assert json.dumps(response.json()).find(SIG) == -1


# ── total deadline, races, URL errors, scrubbing (review fixes, 2026-10-16) ──


async def test_the_deadline_covers_the_whole_exchange_not_each_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    monkeypatch.setattr(fabric, "REQUEST_TIMEOUT_S", 0.05)
    capacity = fabric.Capacity("aetraining", "AETRAINING", WAKE_URL, KEY)

    class Dribble(httpx.AsyncByteStream):
        # Every chunk arrives inside any per-phase timeout; the sum does not.
        async def __aiter__(self):
            for _ in range(20):
                await asyncio.sleep(0.02)
                yield b" "

    async def slow(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Dribble())

    with pytest.raises(fabric.FabricError) as caught:
        await fabric.call(capacity, "wake", transport=httpx.MockTransport(slow))
    assert caught.value.code == fabric.CODE_TIMEOUT
    assert caught.value.status == 504
    assert "may still be under way" in str(caught.value)
    _clean(str(caught.value))


async def test_a_probe_in_flight_during_a_wake_does_not_write_stale_state_back() -> None:
    import asyncio

    capacity = fabric.Capacity("aetraining", "AETRAINING", WAKE_URL, KEY)
    started = asyncio.Event()
    release = asyncio.Event()
    answers = iter(["Paused", "Waking"])

    async def handler(_request: httpx.Request) -> httpx.Response:
        state = next(answers)
        if state == "Paused":
            started.set()
            await release.wait()
        return httpx.Response(200, json=_answer(state))

    transport = httpx.MockTransport(handler)
    probe = asyncio.create_task(fabric.status(capacity, transport=transport))
    await started.wait()
    fabric.forget(capacity.name)  # what act() does after a wake
    release.set()
    first = await probe
    assert first["state"] == "Paused"  # the probe's own answer, returned once
    # But it was not cached: the next read asks again and sees the wake.
    second = await fabric.status(capacity, transport=transport)
    assert second["state"] == "Waking"


async def test_a_bad_url_is_one_rows_error_and_the_others_still_answer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bad = "https://logic.test/run?sig=secretsignature123456\x00"
    caplog.set_level(logging.DEBUG)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=_answer("Active")))
    env = {
        "AE_FABRIC_WAKE_URL_AEBAD": bad,
        "AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL,
    }
    answer = await fabric.overview(env, transport=transport)
    rows = {row["name"]: row for row in answer["capacities"]}
    assert rows["aetraining"]["state"] == "Active"
    assert rows["aebad"]["state"] == "Unknown"
    assert "could not be built" in rows["aebad"]["error"]
    assert "AE_FABRIC_WAKE_URL_AEBAD" in rows["aebad"]["error"]
    for text in (json.dumps(answer), caplog.text):
        assert "secretsignature123456" not in text
        assert "logic.test/run" not in text


def test_the_suspend_key_is_scrubbed_at_any_length_and_in_encoded_forms() -> None:
    from urllib.parse import quote, quote_plus

    short = "k 3/+"
    capacity = fabric.Capacity(
        "x",
        "X",
        "https://logic.test/run?sig=a%2Fb%2Bc%20dsignature&sv=1.0&v=a+b%2Fcdefgh",
        short,
    )
    found = capacity.secrets()
    for expected in (short, quote(short, safe=""), quote_plus(short), capacity.wake_url):
        assert expected in found
    # Encoded spellings of a query value are covered too.
    assert quote("a/b+c dsignature", safe="") in found
    assert quote_plus("a/b+c dsignature") in found
    # Incidental short values stay out so they cannot scrub ordinary words.
    assert "1.0" not in found
    tiny = fabric.Capacity("x", "X", WAKE_URL, "k")
    assert "k" in tiny.secrets()
    assert fabric._scrub("bad key k3 / " + quote_plus(short), capacity).count("[redacted]") == 1


async def test_the_shared_transport_is_reused_across_calls() -> None:
    first = fabric._shared_transport()
    assert fabric._shared_transport() is first


# ── second pass (2026-10-16): details, sleepsAt and keep awake ───────────

from datetime import UTC, datetime  # noqa: E402


def _at(text: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-16T{text}:00+00:00")


def _rule(**fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "resumedAt": _at("18:00"),
        "keepAwakeSince": None,
        "quietSince": None,
        "maxHours": 6,
        "nextCheckAt": _at("18:20"),
    }
    return {**base, **fields}


def _next_day(text: str) -> datetime:
    return datetime.fromisoformat(f"2026-10-17T{text}:00+00:00")


@pytest.mark.parametrize(
    ("fields", "now", "expected"),
    [
        # Window opens 00:00; first run then is 00:20 (quiet), the suspend an hour on.
        ({}, _at("18:05"), _next_day("01:20")),
        # A run lands exactly when the window opens.
        ({"nextCheckAt": _at("18:00")}, _at("17:59"), _next_day("01:00")),
        # A keep awake into the future moves the window start past resumedAt.
        ({"keepAwakeSince": _at("20:05")}, _at("18:05"), _next_day("03:20")),
        # An older keepAwakeSince changes nothing: the later of the two wins.
        ({"keepAwakeSince": _at("12:00")}, _at("18:05"), _next_day("01:20")),
        # Quiet already counted inside the open window: the next run suspends.
        (
            {"resumedAt": _at("10:00"), "quietSince": _at("16:20"), "nextCheckAt": _at("17:20")},
            _at("16:45"),
            _at("17:20"),
        ),
        # Quiet counted before a keep awake reopened the window: it starts over.
        (
            {
                "resumedAt": _at("10:00"),
                "keepAwakeSince": _at("14:00"),
                "quietSince": _at("16:20"),
                "nextCheckAt": _at("17:20"),
            },
            _at("16:45"),
            _at("21:20"),
        ),
        # Window long open and busy until now: next run is the first quiet one.
        (
            {"resumedAt": _at("08:00"), "nextCheckAt": _at("17:20")},
            _at("16:45"),
            _at("18:20"),
        ),
        # A stale nextCheckAt (in the past) still fixes the minute of the hour.
        (
            {"resumedAt": _at("08:00"), "quietSince": _at("16:20"), "nextCheckAt": _at("15:20")},
            _at("16:45"),
            _at("17:20"),
        ),
        # Third pass (2026-10-16). No resumedAt: keepAwakeSince alone starts the window.
        ({"resumedAt": None, "keepAwakeSince": _at("20:05")}, _at("18:05"), _next_day("03:20")),
        # Neither: the runbook starts the window 30 days back, so it is long open.
        ({"resumedAt": None}, _at("18:05"), _at("19:20")),
        # keepAwakeUntil in the future: nothing until then, then the usual two runs.
        ({"keepAwakeUntil": _next_day("02:30")}, _at("18:05"), _next_day("04:20")),
        # keepAwakeUntil earlier than the window's own end changes nothing.
        ({"keepAwakeUntil": _at("23:00")}, _at("18:05"), _next_day("01:20")),
        # A run up to 5 minutes before the decision time catches it (jobs start late)...
        ({"keepAwakeUntil": _next_day("02:22")}, _at("18:05"), _next_day("03:20")),
        (
            {"resumedAt": _at("18:03"), "nextCheckAt": _at("18:00")},
            _at("18:05"),
            _next_day("01:00"),
        ),
        # ...one more than 5 minutes before it does not.
        ({"keepAwakeUntil": _next_day("02:26")}, _at("18:05"), _next_day("04:20")),
        (
            {"resumedAt": _at("18:06"), "nextCheckAt": _at("18:00")},
            _at("18:05"),
            _next_day("02:00"),
        ),
        # A stale quiet mark (over 100 minutes before the run) starts the count over.
        (
            {"resumedAt": _at("08:00"), "quietSince": _at("15:00"), "nextCheckAt": _at("17:20")},
            _at("16:45"),
            _at("18:20"),
        ),
        # A mark too recent (under 45 minutes, a manual run): the run after counts it.
        (
            {"resumedAt": _at("08:00"), "quietSince": _at("17:00"), "nextCheckAt": _at("17:20")},
            _at("17:05"),
            _at("18:20"),
        ),
        # Too recent for one run and too old for the next: that next one marks afresh.
        (
            {"resumedAt": _at("08:00"), "quietSince": _at("17:08"), "nextCheckAt": _at("17:50")},
            _at("17:10"),
            _at("19:50"),
        ),
        # A mark from before keepAwakeUntil does not count.
        (
            {
                "resumedAt": _at("08:00"),
                "keepAwakeUntil": _at("16:30"),
                "quietSince": _at("16:20"),
                "nextCheckAt": _at("17:20"),
            },
            _at("16:45"),
            _at("18:20"),
        ),
    ],
)
def test_sleeps_at_follows_the_runbook_rule(
    fields: dict[str, object], now: datetime, expected: datetime
) -> None:
    assert fabric.sleeps_at(_rule(**fields), now) == expected


@pytest.mark.parametrize("missing", ["maxHours", "nextCheckAt"])
def test_sleeps_at_is_none_without_what_it_needs(missing: str) -> None:
    assert fabric.sleeps_at(_rule(**{missing: None}), _at("18:05")) is None
    assert fabric.sleeps_at({}, _at("18:05")) is None


def test_the_runbooks_gaps_are_mirrored() -> None:
    # Suspend-FabricCapacity.runbook.ps1 Get-SuspendDecision: MinQuietGapMinutes
    # 45, MaxQuietGapMinutes 100, and Get-WindowStart's 30-day lookback.
    assert fabric._MIN_QUIET_GAP.total_seconds() == 45 * 60
    assert fabric._MAX_QUIET_GAP.total_seconds() == 100 * 60
    assert fabric._NO_RESUME_LOOKBACK.days == 30
    assert fabric._RUN_SLACK.total_seconds() == 5 * 60


def test_keep_awake_options_come_from_the_same_rule() -> None:
    details = _rule()
    now = _at("18:05")
    # Sleeps at 01:20 now; 2 and 4 hours end before then, 8 hours (02:05) does not.
    assert fabric.sleeps_at(details, now) == _next_day("01:20")
    assert fabric.keep_awake_options(details, now) == [
        {"hours": 2, "sleepsAt": "2026-10-17T01:20:00Z", "addsTime": False},
        {"hours": 4, "sleepsAt": "2026-10-17T01:20:00Z", "addsTime": False},
        {"hours": 8, "sleepsAt": "2026-10-17T03:20:00Z", "addsTime": True},
    ]
    for option in fabric.keep_awake_options(details, now):
        until = now + fabric.timedelta(hours=option["hours"])
        projected = fabric.sleeps_at({**details, "keepAwakeUntil": until}, now)
        assert option["sleepsAt"] == fabric.iso(projected)
    # A later keepAwakeUntil already held is never shortened.
    held = fabric.keep_awake_options({**details, "keepAwakeUntil": _next_day("06:00")}, now)
    assert [option["sleepsAt"] for option in held] == ["2026-10-17T07:20:00Z"] * 3
    assert [option["addsTime"] for option in held] == [False] * 3
    # A counted quiet mark would suspend at 17:20; keep awake clears it, so 2 hours
    # (to 18:45) means the 19:20 run marks quiet and the 20:20 run suspends.
    marked = _rule(resumedAt=_at("10:00"), quietSince=_at("16:20"), nextCheckAt=_at("17:20"))
    assert fabric.sleeps_at(marked, _at("16:45")) == _at("17:20")
    first = fabric.keep_awake_options(marked, _at("16:45"))[0]
    assert first == {"hours": 2, "sleepsAt": "2026-10-16T20:20:00Z", "addsTime": True}
    # Nothing to compare with: each choice adds time and has no sleep time.
    assert fabric.keep_awake_options({}, now) == [
        {"hours": hours, "sleepsAt": None, "addsTime": True} for hours in (2, 4, 8)
    ]


FULL = {
    "capacity": "aetraining",
    "state": "Active",
    "action": "none",
    "changed": False,
    "sku": "F4",
    "region": "North Central US",
    "capacityId": CAPACITY_ID,
    "hourlyUsd": 0.72,
    "resumedAt": "2026-10-16T18:00:00Z",
    "keepAwakeSince": None,
    "quietSince": None,
    "maxHours": 6,
    "nextCheckAt": "2026-10-16T18:20:00.000Z",
}


async def test_status_passes_the_details_through_and_says_when_it_sleeps(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(fabric, "_now", lambda: _at("18:05"))
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE).respond(200, json=FULL)
        first = await client.get("/v1/ae/fabric")
        # Served from the cache, the time is still worked out.
        second = await client.get("/v1/ae/fabric")
    assert route.call_count == 1
    for response in (first, second):
        (row,) = response.json()["capacities"]
        assert row == {
            "name": "aetraining",
            "can_suspend": True,
            "can_keep_awake": True,
            "state": "Active",
            "sku": "F4",
            "region": "North Central US",
            "capacityId": CAPACITY_ID,
            "hourlyUsd": 0.72,
            "resumedAt": "2026-10-16T18:00:00Z",
            "keepAwakeUntil": None,
            "maxHours": 6,
            "sleepsAt": "2026-10-17T01:20:00Z",
            "keepAwakeOptions": [
                {"hours": 2, "sleepsAt": "2026-10-17T01:20:00Z", "addsTime": False},
                {"hours": 4, "sleepsAt": "2026-10-17T01:20:00Z", "addsTime": False},
                {"hours": 8, "sleepsAt": "2026-10-17T03:20:00Z", "addsTime": True},
            ],
            "wakeBilledHours": 7,
        }


async def test_sleeps_at_is_null_unless_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fabric, "_now", lambda: _at("18:05"))
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={**FULL, "state": "Paused"})
    )
    answer = await fabric.overview(
        {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL}, transport=transport
    )
    (row,) = answer["capacities"]
    assert row["sleepsAt"] is None
    assert row["keepAwakeOptions"] == []
    assert row["sku"] == "F4"
    # No suspend key: neither Off nor keep awake.
    assert row["can_suspend"] is False
    assert row["can_keep_awake"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sku", "<b>F4</b>"),
        ("sku", 4),
        ("region", f"see {WAKE_URL}"),
        ("capacityId", "/subscriptions/x/resourceGroups/rg/providers/Microsoft.Web/sites/a"),
        ("capacityId", CAPACITY_ID + "/../../evil"),
        ("hourlyUsd", "0.72"),
        ("hourlyUsd", True),
        ("hourlyUsd", -1),
        ("hourlyUsd", float("nan")),
        ("maxHours", 6.5),
        ("maxHours", 0),
        ("maxHours", True),
        ("resumedAt", "yesterday"),
        ("resumedAt", 1760637600),
    ],
)
async def test_an_odd_detail_is_null_never_echoed(field: str, value: object) -> None:
    body = json.dumps({**FULL, field: value}, allow_nan=True)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=body.encode()))
    answer = await fabric.overview(
        {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL}, transport=transport
    )
    (row,) = answer["capacities"]
    assert row[field] is None
    _clean(json.dumps(answer))


def test_a_lowercase_region_and_an_offset_time_read_as_given() -> None:
    details = fabric._details(
        {"region": "northcentralus", "resumedAt": "2026-10-16T11:00:00-07:00", "maxHours": 6.0}
    )
    assert details["region"] == "northcentralus"
    assert details["resumedAt"] == datetime(2026, 10, 16, 18, 0, tzinfo=UTC)
    assert details["maxHours"] == 6
    assert fabric.iso(details["resumedAt"]) == "2026-10-16T18:00:00Z"


async def test_keep_awake_sends_the_hours_and_the_key_in_the_header(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(fabric, "_now", lambda: _at("18:05"))
    caplog.set_level(logging.DEBUG)
    until = "2026-10-17T02:05:00.1234567Z"
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE)
        route.side_effect = [
            httpx.Response(200, json={**FULL, "quietSince": "2026-10-16T18:01:00Z"}),
            # The third pass's answer: keepAwakeUntil, seven fractional digits.
            httpx.Response(
                200, json={**_answer("Active", "keepawake", True), "keepAwakeUntil": until}
            ),
            httpx.Response(200, json={**FULL, "keepAwakeUntil": until}),
        ]
        await client.get("/v1/ae/fabric")
        kept = await client.post("/v1/ae/fabric/aetraining/keepawake", json={"hours": 8})
        await client.get("/v1/ae/fabric")
    assert kept.status_code == 200, kept.text
    # The full row, from the answer over the details GET read, with the new sleep time.
    assert kept.json() == {
        "name": "aetraining",
        "state": "Active",
        "action": "keepawake",
        "changed": True,
        "can_suspend": True,
        "can_keep_awake": True,
        "sku": "F4",
        "region": "North Central US",
        "capacityId": CAPACITY_ID,
        "hourlyUsd": 0.72,
        "resumedAt": "2026-10-16T18:00:00Z",
        "keepAwakeUntil": "2026-10-17T02:05:00Z",
        "maxHours": 6,
        # Nothing until 02:05; the 02:20 run marks quiet, the 03:20 one suspends.
        "sleepsAt": "2026-10-17T03:20:00Z",
        "keepAwakeOptions": [
            {"hours": hours, "sleepsAt": "2026-10-17T03:20:00Z", "addsTime": False}
            for hours in (2, 4, 8)
        ],
        "wakeBilledHours": 7,
    }
    # The quiet mark the Logic App cleared is gone from what the row was built on.
    assert fabric._known["aetraining"]["quietSince"] is None
    request = route.calls[1].request
    params = _params(request)
    assert params["action"] == "keepawake"
    assert params["hours"] == "8"
    assert params["sig"] == SIG
    assert request.headers["x-suspend-key"] == KEY
    assert KEY not in str(request.url)
    # The cached status went with it: the next GET asked again.
    assert route.call_count == 3
    _clean(kept.text, caplog.text)


@pytest.mark.parametrize(
    "body",
    [{"hours": 0}, {"hours": 13}, {"hours": "4"}, {"hours": 4.5}, {}, {"hours": 4, "x": 1}],
)
async def test_keep_awake_checks_the_hours_before_any_call(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE).respond(200, json=FULL)
        response = await client.post("/v1/ae/fabric/aetraining/keepawake", json=body)
        assert not route.called
    assert response.status_code == 422


async def test_call_refuses_bad_hours_itself() -> None:
    capacity = fabric.Capacity("aetraining", "AETRAINING", WAKE_URL, KEY)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=FULL))
    for hours in (None, 0, 13, True):
        with pytest.raises(fabric.FabricError) as caught:
            await fabric.call(capacity, "keepawake", hours=hours, transport=transport)  # type: ignore[arg-type]
        assert caught.value.status == 400
        assert caught.value.code == fabric.CODE_BAD_HOURS


async def test_keep_awake_without_a_key_is_refused_before_any_call(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch, key=False)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE).respond(200, json=FULL)
        response = await client.post("/v1/ae/fabric/aetraining/keepawake", json={"hours": 2})
        assert not route.called
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == fabric.CODE_SUSPEND_NOT_CONFIGURED
    assert error["message"].startswith("Keep awake is not set up for aetraining")


@pytest.mark.parametrize(
    ("reply", "fragment"),
    [
        (
            httpx.Response(403, json={"error": f"suspend key missing or wrong: {KEY}"}),
            "refused the suspend key (403) on the keep-awake call",
        ),
        (
            httpx.Response(400, json={"error": f"hours must be 1..12 ({WAKE_URL})"}),
            "refused the keep-awake hours (400): hours must be 1..12 ([redacted])",
        ),
    ],
)
async def test_a_refused_keep_awake_is_a_502_naming_no_secret(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reply: httpx.Response,
    fragment: str,
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE).mock(return_value=reply)
        response = await client.post("/v1/ae/fabric/aetraining/keepawake", json={"hours": 2})
        assert route.call_count == 1
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == fabric.CODE_FAILED
    assert fragment in error["message"]
    _clean(response.text, caplog.text)


async def test_keep_awake_is_json_only_origin_checked_and_signed_in(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE).respond(200, json=FULL)
        path = "/v1/ae/fabric/aetraining/keepawake"
        assert (await client.get(path)).status_code in (404, 405)
        form = await client.post(
            path, content="hours=2", headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        assert form.status_code == 415
        foreign = await client.post(
            path, json={"hours": 2}, headers={"Origin": "https://evil.example"}
        )
        assert foreign.status_code == 403
        assert (
            await client.post("/v1/ae/fabric/nosuch/keepawake", json={"hours": 2})
        ).status_code == 404
        monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
        auth.reset_provider_cache()
        unsigned = await client.post(path, json={"hours": 2})
        assert unsigned.status_code == 401
        assert not route.called


# The Logic App's real answer (agents-and-automation, 2026-10-16): keepAwakeSince
# as .NET round-trip text with seven fractional digits, the region in display form.
REAL = {
    "capacity": "aetraining",
    "state": "Active",
    "action": "none",
    "changed": False,
    "sku": "F4",
    "region": "North Central US",
    "capacityId": "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups"
    "/rg-trainings/providers/Microsoft.Fabric/capacities/aetraining",
    "hourlyUsd": 0.72,
    "resumedAt": "2026-10-16T19:15:04Z",
    "keepAwakeSince": "2026-10-17T01:20:05.2616188Z",
    "quietSince": None,
    "maxHours": 6,
    "nextCheckAt": "2026-10-17T02:19:00Z",
}


def test_seven_fractional_digits_read_as_a_time() -> None:
    details = fabric._details({**REAL, "quietSince": "2026-10-17T08:19:00.1234567Z"})
    assert details["keepAwakeSince"] == datetime(2026, 10, 17, 1, 20, 5, 261618, tzinfo=UTC)
    assert details["quietSince"] == datetime(2026, 10, 17, 8, 19, 0, 123456, tzinfo=UTC)
    assert fabric._instant("2026-10-17T01:20:05.261618812Z") is not None


async def test_the_logic_apps_real_answer_reads_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fabric, "_now", lambda: datetime(2026, 10, 17, 1, 30, tzinfo=UTC))
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=REAL))
    env = {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL, "AE_FABRIC_SUSPEND_KEY_AETRAINING": KEY}
    answer = await fabric.overview(env, transport=transport)
    (row,) = answer["capacities"]
    assert row == {
        "name": "aetraining",
        "can_suspend": True,
        "can_keep_awake": True,
        "state": "Active",
        "sku": "F4",
        "region": "North Central US",
        "capacityId": REAL["capacityId"],
        "hourlyUsd": 0.72,
        "resumedAt": "2026-10-16T19:15:04Z",
        "maxHours": 6,
        "keepAwakeUntil": None,
        # Window from the keep awake at 01:20:05 opens at 07:20:05; runs fall on
        # :19 and a job starts a minute or two late, so the 07:19 run decides
        # (the first quiet one) and 08:19 suspends.
        "sleepsAt": "2026-10-17T08:19:00Z",
        "keepAwakeOptions": [
            {"hours": 2, "sleepsAt": "2026-10-17T08:19:00Z", "addsTime": False},
            {"hours": 4, "sleepsAt": "2026-10-17T08:19:00Z", "addsTime": False},
            {"hours": 8, "sleepsAt": "2026-10-17T11:19:00Z", "addsTime": True},
        ],
        "wakeBilledHours": 7,
    }


async def test_no_linked_schedule_means_no_sleep_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fabric, "_now", lambda: datetime(2026, 10, 17, 1, 30, tzinfo=UTC))
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={**REAL, "nextCheckAt": None})
    )
    answer = await fabric.overview(
        {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL}, transport=transport
    )
    (row,) = answer["capacities"]
    assert row["sleepsAt"] is None
    assert row["resumedAt"] == "2026-10-16T19:15:04Z"


async def test_keep_awake_sends_hours_as_plain_digits() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={**_answer("Active", "keepawake", True)})

    capacity = fabric.Capacity("aetraining", "AETRAINING", WAKE_URL, KEY)
    for hours in (1, 12):
        await fabric.call(
            capacity, "keepawake", hours=hours, transport=httpx.MockTransport(handler)
        )
    assert [_params(request)["hours"] for request in seen] == ["1", "12"]


async def test_actions_on_one_capacity_never_overlap() -> None:
    # The Logic App runs requests in parallel, so the server keeps two keep-awake
    # calls from both reading KeepAwakeUntil before either writes.
    running = 0
    most = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.01)
        running -= 1
        return httpx.Response(200, json=_answer("Active", "keepawake", True))

    capacity = fabric.Capacity("aetraining", "AETRAINING", WAKE_URL, KEY)
    transport = httpx.MockTransport(handler)
    rows = await asyncio.gather(
        *(fabric.act(capacity, "keepawake", hours=h, transport=transport) for h in (12, 1, 4))
    )
    assert most == 1
    assert [row["changed"] for row in rows] == [True, True, True]


# ── third pass (2026-10-16): keepAwakeUntil, full rows from actions, id casing ──


async def test_the_new_shapes_keep_awake_until_reads_with_seven_digits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fabric, "_now", lambda: datetime(2026, 10, 17, 1, 30, tzinfo=UTC))
    body = {**REAL, "keepAwakeUntil": "2026-10-17T09:20:05.2616188Z"}
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=body))
    env = {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL, "AE_FABRIC_SUSPEND_KEY_AETRAINING": KEY}
    (row,) = (await fabric.overview(env, transport=transport))["capacities"]
    assert row["keepAwakeUntil"] == "2026-10-17T09:20:05Z"
    # Nothing before 09:20:05; the 09:19 run is within the slack, so 10:19 suspends.
    assert row["sleepsAt"] == "2026-10-17T10:19:00Z"


async def test_an_old_shape_keep_awake_answer_still_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fabric, "_now", lambda: _at("18:05"))
    capacity = fabric.Capacity("aetraining", "AETRAINING", WAKE_URL, KEY)
    replies = iter(
        [
            httpx.Response(200, json=FULL),
            # Before the third pass: keepAwakeSince moved into the future, no keepAwakeUntil.
            httpx.Response(
                200,
                json={
                    **_answer("Active", "keepawake", True),
                    "keepAwakeSince": "2026-10-16T20:05:00Z",
                },
            ),
        ]
    )
    transport = httpx.MockTransport(lambda _request: next(replies))
    await fabric.status(capacity, transport=transport)
    row = await fabric.act(capacity, "keepawake", hours=8, transport=transport)
    assert row["keepAwakeUntil"] is None
    assert row["sleepsAt"] == "2026-10-17T03:20:00Z"
    assert row["sku"] == "F4"


async def test_a_wake_answers_the_full_row_from_the_details_last_read(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    monkeypatch.setattr(fabric, "_now", lambda: _at("18:05"))
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE)
        route.side_effect = [
            httpx.Response(200, json={**FULL, "state": "Paused"}),
            httpx.Response(200, json=_answer("Waking", "wake", True)),
        ]
        await client.get("/v1/ae/fabric")
        # Past the cache's life: the row still comes from the details last read.
        fabric._status_cache.clear()
        woke = await client.post("/v1/ae/fabric/aetraining/wake", json={})
    row = woke.json()
    assert row["state"] == "Waking"
    assert row["sku"] == "F4"
    assert row["capacityId"] == CAPACITY_ID
    assert row["sleepsAt"] is None
    assert row["keepAwakeOptions"] == []
    assert row["wakeBilledHours"] == 7
    assert set(row) == set(NO_DETAILS) | {
        "name",
        "state",
        "action",
        "changed",
        "can_suspend",
        "can_keep_awake",
    }


async def test_a_failed_keep_awake_500_is_a_502_with_its_error_scrubbed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    reply = httpx.Response(
        500, json={"error": f"Set-AutomationVariable KeepAwakeUntil failed at {WAKE_URL} ({KEY})"}
    )
    with respx.mock(assert_all_called=False) as mock:
        mock.get(url__startswith=INVOKE).mock(return_value=reply)
        response = await client.post("/v1/ae/fabric/aetraining/keepawake", json={"hours": 2})
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == fabric.CODE_FAILED
    assert error["message"] == (
        "aetraining: the Logic App answered 500 to the keep-awake call: "
        "Set-AutomationVariable KeepAwakeUntil failed at [redacted] ([redacted])"
    )
    _clean(response.text, caplog.text)


# Copied from upstream/web/src/lib/aeExternalUrls.ts (isAeFabricAdminLinkUrl,
# patch 0064); keep the two in step. fabric.ts capacityLinks builds the link as
# "https://portal.azure.com/#resource" + capacityId + "/overview".
_TS_GUID = "[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_TS_AZURE_CAPACITY_LINK = re.compile(
    f"^https://portal\\.azure\\.com/#resource/subscriptions/{_TS_GUID}"
    "/resourceGroups/[A-Za-z0-9._()-]{1,90}"
    "/providers/Microsoft\\.Fabric/capacities/[A-Za-z0-9-]{1,63}/overview$"
)
_TS_DOTS_GROUP = re.compile(r"/resourceGroups/\.+/")


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (CAPACITY_ID, CAPACITY_ID),
        # ARM's own mixed casing, and an upper-case guid.
        (
            "/SUBSCRIPTIONS/0B1C2D3E-4F50-6172-8394-A5B6C7D8E9F0/resourcegroups/RG-AE-Fabric"
            "/providers/microsoft.fabric/Capacities/aetraining",
            "/subscriptions/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0/resourceGroups/RG-AE-Fabric"
            "/providers/Microsoft.Fabric/capacities/aetraining",
        ),
        (
            " /subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg_(x).1"
            "/providers/Microsoft.Fabric/capacities/ae-01 ",
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg_(x).1"
            "/providers/Microsoft.Fabric/capacities/ae-01",
        ),
        # Not a strict 8-4-4-4-12 guid: no id, so no links.
        (
            "/subscriptions/0b1c2d3e4f50-6172-8394-a5b6-c7d8e9f0/resourceGroups/rg"
            "/providers/Microsoft.Fabric/capacities/aetraining",
            None,
        ),
        (
            "/subscriptions/------------------------------------/resourceGroups/rg"
            "/providers/Microsoft.Fabric/capacities/aetraining",
            None,
        ),
        (
            "/subscriptions/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0/resourceGroups/.."
            "/providers/Microsoft.Fabric/capacities/aetraining",
            None,
        ),
        (CAPACITY_ID + "/", None),
        (CAPACITY_ID.replace("Microsoft.Fabric", "Microsoft.Web"), None),
        (None, None),
    ],
)
def test_capacity_ids_are_normalised_to_what_the_host_admits(
    given: object, expected: str | None
) -> None:
    normalised = fabric._details({"capacityId": given})["capacityId"]
    assert normalised == expected
    if normalised is not None:
        link = f"https://portal.azure.com/#resource{normalised}/overview"
        assert _TS_AZURE_CAPACITY_LINK.match(link)
        assert not _TS_DOTS_GROUP.search(link)


# ── the capacity fleet (2026-10-21) ──────────────────────────────────────

FLEET_SIG = "fleet_signature_value_never_logged_4567"
FLEET_INVOKE = "https://logic.test/workflows/fleet000/triggers/manual/paths/invoke"
FLEET_URL = f"{FLEET_INVOKE}?api-version=2016-10-01&sig={FLEET_SIG}"
FLEET_KEY = "0" * 63 + "1"
FLEET_SECRETS = (FLEET_SIG, FLEET_KEY, "workflows/fleet000")
ZERO_SUB = "00000000-0000-0000-0000-000000000000"


def _fleet_id(name: str, group: str = "rg-placeholder") -> str:
    return (
        f"/subscriptions/{ZERO_SUB}/resourceGroups/{group}"
        f"/providers/Microsoft.Fabric/capacities/{name}"
    )


def _fleet_capacity(name: str, sku: str = "F4", state: str = "Active") -> dict[str, object]:
    return {
        "name": name,
        "capacityId": _fleet_id(name),
        "subscriptionName": f"Subscription {name[-1].upper()}",
        "resourceGroup": "rg-placeholder",
        "region": "North Central US",
        "sku": sku,
        "state": state,
    }


FLEET_ROWS = [
    _fleet_capacity("teamlab", "F4", "Paused"),
    _fleet_capacity("aetraining", "F4", "Active"),
    _fleet_capacity("salesdemo", "F8", "Active"),
]


def _fleet_answer(
    action: str = "list",
    rows: list[dict[str, object]] | None = None,
    *,
    changed: bool = False,
    capacity: dict[str, object] | None = None,
    errors: list[str] | None = None,
) -> dict[str, object]:
    return {
        "action": action,
        "changed": changed,
        "capacities": FLEET_ROWS if rows is None else rows,
        "capacity": capacity,
        "errors": errors or [],
    }


def _fleet_write(
    action: str, row: dict[str, object], *, changed: bool = True
) -> dict[str, object]:
    """A write's answer since 2026-10-21: no list, only the target's row."""
    return _fleet_answer(action, rows=[row], changed=changed, capacity=row)


def _configure_fleet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AE_FABRIC_CAPACITIES_URL", FLEET_URL)
    monkeypatch.setenv("AE_FABRIC_CAPACITIES_KEY", FLEET_KEY)


def _fleet_clean(*texts: str) -> None:
    _clean(*texts)
    for text in texts:
        for secret in FLEET_SECRETS:
            assert secret not in text


def _sent(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content)


def _rich_probe() -> dict[str, object]:
    """aetraining's own Logic App, with the runbook details the fleet does not know."""
    return {
        **_answer("Active"),
        "sku": "F2",
        "maxHours": 6,
        "resumedAt": "2026-10-21T08:00:00Z",
        "nextCheckAt": "2026-10-21T09:10:00Z",
    }


def test_the_fleet_needs_both_its_url_and_its_key() -> None:
    assert fabric.fleet({"AE_FABRIC_CAPACITIES_URL": FLEET_URL}) is None
    assert fabric.fleet({"AE_FABRIC_CAPACITIES_KEY": FLEET_KEY}) is None
    shared = fabric.fleet(
        {"AE_FABRIC_CAPACITIES_URL": FLEET_URL, "AE_FABRIC_CAPACITIES_KEY": f" {FLEET_KEY} "}
    )
    assert shared is not None and shared.key == FLEET_KEY
    _fleet_clean(repr(shared))


@pytest.mark.parametrize(
    ("rate", "sku", "hourly"),
    [
        (None, "F4", 0.72),
        (None, "F8", 1.44),
        (None, "F64", 11.52),
        ("0.2", "F2", 0.4),
        ("nonsense", "F4", 0.72),
        ("-1", "F4", 0.72),
        (None, "P1", None),
        (None, None, None),
    ],
)
def test_the_hourly_price_is_capacity_units_times_the_rate(
    rate: str | None, sku: str | None, hourly: float | None
) -> None:
    env = {} if rate is None else {"AE_FABRIC_USD_PER_CU_HOUR": rate}
    assert fabric.hourly_for(sku, fabric.usd_per_cu_hour(env)) == hourly


async def test_the_fleet_lists_every_capacity_and_merges_the_wake_details(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    _configure_fleet(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        listed = mock.post(url__startswith=FLEET_INVOKE).respond(200, json=_fleet_answer())
        probe = mock.get(url__startswith=INVOKE).respond(200, json=_rich_probe())
        response = await client.get("/v1/ae/fabric")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True and body["fleet"] is True
    assert "fleetError" not in body
    rows = body["capacities"]
    # Sorted by name, one row per listed capacity, never a fixed count.
    assert [row["name"] for row in rows] == ["aetraining", "salesdemo", "teamlab"]
    by = {row["name"]: row for row in rows}
    for row in rows:
        assert row["can_suspend"] is True
        assert row["canScale"] is True
        assert row["skuChoices"] == list(fabric.SKU_CHOICES)
        assert row["capacityId"] == _fleet_id(row["name"])
        assert row["resourceGroup"] == "rg-placeholder"
        assert "error" not in row
    # The fleet's SKU wins over the probe's, and the price follows the SKU.
    assert by["aetraining"]["sku"] == "F4"
    assert by["aetraining"]["hourlyUsd"] == 0.72
    assert by["salesdemo"]["hourlyUsd"] == 1.44
    assert by["teamlab"]["state"] == "Paused"
    # aetraining keeps its runbook extras from its own Logic App.
    assert by["aetraining"]["can_keep_awake"] is True
    assert by["aetraining"]["resumedAt"] == "2026-10-21T08:00:00Z"
    assert by["aetraining"]["maxHours"] == 6
    assert by["aetraining"]["wakeBilledHours"] == 7
    assert [option["hours"] for option in by["aetraining"]["keepAwakeOptions"]] == [2, 4, 8]
    # The others have no runbook: no keep awake, no sleep time, no minimum bill.
    for name in ("salesdemo", "teamlab"):
        assert by[name]["can_keep_awake"] is False
        assert by[name]["keepAwakeOptions"] == []
        assert by[name]["sleepsAt"] is None
        assert by[name]["wakeBilledHours"] is None
    # One list call, POSTed as JSON with the key in its header, never in the URL.
    request = listed.calls.last.request
    assert request.method == "POST"
    assert _sent(request) == {"action": "list"}
    assert request.headers[fabric.CAPACITY_KEY_HEADER] == FLEET_KEY
    assert request.headers["content-type"] == "application/json"
    assert FLEET_KEY not in str(request.url)
    assert probe.call_count == 1
    _fleet_clean(response.text, caplog.text)


@pytest.mark.parametrize("failure", ["slow", "500", "raises"])
async def test_a_failing_detail_probe_leaves_the_fleet_row_and_its_state(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    monkeypatch.setattr(fabric, "REQUEST_TIMEOUT_S", 0.3)
    caplog.set_level(logging.DEBUG)
    env = {
        "AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL,
        "AE_FABRIC_SUSPEND_KEY_AETRAINING": KEY,
        "AE_FABRIC_CAPACITIES_URL": FLEET_URL,
        "AE_FABRIC_CAPACITIES_KEY": FLEET_KEY,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_fleet_answer())
        if failure == "slow":
            await asyncio.sleep(5)
        if failure == "raises":
            raise httpx.ConnectError(f"cannot connect to {WAKE_URL}")
        return httpx.Response(500, json={"error": f"stalled at {WAKE_URL}"})

    loop = asyncio.get_running_loop()
    started = loop.time()
    answer = await fabric.overview(env, transport=httpx.MockTransport(handler))
    # The probe and the list ran at once: the slow probe cost its own deadline only.
    assert loop.time() - started < 2
    by = {row["name"]: row for row in answer["capacities"]}
    row = by["aetraining"]
    assert row["state"] == "Active"
    assert "error" not in row
    assert row["sku"] == "F4" and row["hourlyUsd"] == 0.72
    assert row["can_suspend"] is True
    # Details left out: no keep awake to offer and no runbook times.
    assert row["keepAwakeOptions"] == []
    assert row["can_keep_awake"] is False
    assert row["resumedAt"] is None
    assert len(answer["capacities"]) == 3
    _fleet_clean(json.dumps(answer), caplog.text)


async def test_a_failing_detail_probe_keeps_the_last_good_details() -> None:
    env = {
        "AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL,
        "AE_FABRIC_SUSPEND_KEY_AETRAINING": KEY,
        "AE_FABRIC_CAPACITIES_URL": FLEET_URL,
        "AE_FABRIC_CAPACITIES_KEY": FLEET_KEY,
    }
    probe_ok = True

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=_fleet_answer())
        if probe_ok:
            return httpx.Response(200, json=_rich_probe())
        return httpx.Response(504, json={"error": "stalled"})

    transport = httpx.MockTransport(handler)
    first = await fabric.overview(env, transport=transport)
    assert {row["name"]: row for row in first["capacities"]}["aetraining"]["can_keep_awake"]
    # The status cache expires (forget one name keeps the last details), then the probe stalls.
    fabric.forget("aetraining")
    probe_ok = False
    answer = await fabric.overview(env, transport=transport)
    row = {row["name"]: row for row in answer["capacities"]}["aetraining"]
    assert row["state"] == "Active" and "error" not in row
    assert row["resumedAt"] == "2026-10-21T08:00:00Z"
    assert row["maxHours"] == 6
    # Stale details never offer keep awake.
    assert row["can_keep_awake"] is False
    assert row["keepAwakeOptions"] == []


async def test_a_failed_fleet_list_keeps_todays_rows_and_says_why(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    _configure_fleet(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        # The failing fleet echoes its own URL and key back, the way an Azure error can.
        mock.post(url__startswith=FLEET_INVOKE).respond(
            500, json={"error": f"InvalidTemplate at {FLEET_URL} with {FLEET_KEY}"}
        )
        mock.get(url__startswith=INVOKE).respond(200, json=_answer("Paused"))
        response = await client.get("/v1/ae/fabric")
    body = response.json()
    assert response.status_code == 200
    assert body["fleetError"].startswith("the capacities Logic App answered 500 to the list")
    assert "[redacted]" in body["fleetError"]
    (row,) = body["capacities"]
    assert row["name"] == "aetraining" and row["state"] == "Paused"
    assert "canScale" not in row
    _fleet_clean(response.text, caplog.text)


async def test_a_subscription_the_fleet_could_not_read_is_counted_never_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = {"AE_FABRIC_CAPACITIES_URL": FLEET_URL, "AE_FABRIC_CAPACITIES_KEY": FLEET_KEY}
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=_fleet_answer(errors=[ZERO_SUB]))
    )
    answer = await fabric.overview(env, transport=transport)
    assert answer["fleetError"].startswith("Azure did not answer for 1 subscription;")
    assert ZERO_SUB not in answer["fleetError"]
    assert len(answer["capacities"]) == 3


async def test_pause_and_start_go_through_the_fleet_for_every_row(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # aetraining has no suspend key here: the fleet pauses it anyway.
    _configure(monkeypatch, key=False)
    _configure_fleet(monkeypatch)
    caplog.set_level(logging.DEBUG)
    pausing = _fleet_capacity("aetraining", "F4", "Pausing")
    with respx.mock(assert_all_called=False) as mock:
        fleet_route = mock.post(url__startswith=FLEET_INVOKE)
        fleet_route.side_effect = [
            httpx.Response(200, json=_fleet_answer()),
            httpx.Response(200, json=_fleet_write("suspend", pausing)),
            httpx.Response(200, json=_fleet_write("resume", FLEET_ROWS[0], changed=False)),
        ]
        wake_route = mock.get(url__startswith=INVOKE).respond(200, json=_answer("Active"))
        await client.get("/v1/ae/fabric")
        paused = await client.post("/v1/ae/fabric/aetraining/suspend", json={})
        started = await client.post("/v1/ae/fabric/teamlab/wake", json={})
    assert paused.status_code == 200, paused.text
    assert paused.json()["state"] == "Pausing"
    assert paused.json()["action"] == "suspend" and paused.json()["changed"] is True
    assert _sent(fleet_route.calls[1].request) == {
        "action": "suspend",
        "capacityId": _fleet_id("aetraining"),
    }
    assert started.status_code == 200, started.text
    assert started.json()["action"] == "wake"
    assert _sent(fleet_route.calls[2].request) == {
        "action": "resume",
        "capacityId": _fleet_id("teamlab"),
    }
    # The wake Logic App was only probed by the GET, never asked to act.
    assert [_params(call.request)["action"] for call in wake_route.calls] == ["probe"]
    _fleet_clean(paused.text, started.text, caplog.text)


async def test_resize_checks_the_sku_against_the_allowlist_before_any_call(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_fleet(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__startswith=FLEET_INVOKE).respond(200, json=_fleet_answer())
        for sku in ("F128", "f8", "P1", "F8 "):
            response = await client.post("/v1/ae/fabric/teamlab/scale", json={"sku": sku})
            assert response.status_code == 400, (sku, response.text)
            error = response.json()["error"]
            assert error["code"] == fabric.CODE_BAD_SKU
            assert "F2, F4, F8, F16, F32, F64" in error["message"]
        not_a_string = await client.post("/v1/ae/fabric/teamlab/scale", json={"sku": 8})
        assert not_a_string.status_code == 422
        assert not route.called


async def test_resize_sends_the_sku_and_answers_the_new_row(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure_fleet(monkeypatch)
    caplog.set_level(logging.DEBUG)
    updating = _fleet_capacity("teamlab", "F8", "Updating")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__startswith=FLEET_INVOKE)
        route.side_effect = [
            # No list yet: the write lists first to find the capacity's id.
            httpx.Response(200, json=_fleet_answer()),
            httpx.Response(200, json=_fleet_write("scale", updating)),
        ]
        response = await client.post("/v1/ae/fabric/teamlab/scale", json={"sku": "F8"})
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["action"] == "scale" and row["changed"] is True
    assert row["sku"] == "F8" and row["state"] == "Updating"
    assert row["hourlyUsd"] == 1.44
    assert _sent(route.calls[0].request) == {"action": "list"}
    assert _sent(route.calls[1].request) == {
        "action": "scale",
        "capacityId": _fleet_id("teamlab"),
        "sku": "F8",
    }
    _fleet_clean(response.text, caplog.text)


async def test_resize_without_the_fleet_says_how_to_add_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=INVOKE).respond(200, json=_answer("Active"))
        response = await client.post("/v1/ae/fabric/aetraining/scale", json={"sku": "F8"})
        assert not route.called
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == fabric.CODE_FLEET_NOT_CONFIGURED
    assert "AE_FABRIC_CAPACITIES_URL" in error["message"]


async def test_a_name_two_capacities_share_is_never_guessed(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_fleet(monkeypatch)
    twins = [
        _fleet_capacity("twin", "F4"),
        {**_fleet_capacity("twin", "F2"), "capacityId": _fleet_id("twin", "rg-other")},
    ]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__startswith=FLEET_INVOKE).respond(
            200, json=_fleet_answer(rows=twins)
        )
        listed = await client.get("/v1/ae/fabric")
        response = await client.post("/v1/ae/fabric/twin/scale", json={"sku": "F8"})
        paused = await client.post("/v1/ae/fabric/twin/suspend", json={})
    assert len(listed.json()["capacities"]) == 2
    for refused in (response, paused):
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == fabric.CODE_AMBIGUOUS
    # Only the GET reached the fleet.
    assert route.call_count == 1


async def test_an_unknown_name_on_the_fleet_is_a_404(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_fleet(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__startswith=FLEET_INVOKE).respond(200, json=_fleet_answer())
        response = await client.post("/v1/ae/fabric/nosuch/scale", json={"sku": "F8"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("reply", "status", "fragment"),
    [
        (httpx.Response(403, json={"error": "capacity key missing or wrong"}), 502, "403"),
        (
            httpx.Response(502, json={"error": f"ARM said no to {FLEET_URL} key {FLEET_KEY}"}),
            502,
            "[redacted]",
        ),
        (httpx.ConnectError(f"cannot connect to {FLEET_URL}"), 502, "could not be reached"),
        (httpx.ReadTimeout(f"timed out reading {FLEET_URL}"), 504, "within 9.5 s"),
    ],
)
async def test_a_failed_resize_names_no_secret(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reply: httpx.Response | Exception,
    status: int,
    fragment: str,
) -> None:
    _configure_fleet(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__startswith=FLEET_INVOKE)
        route.side_effect = [httpx.Response(200, json=_fleet_answer()), reply]
        await client.get("/v1/ae/fabric")
        response = await client.post("/v1/ae/fabric/salesdemo/scale", json={"sku": "F16"})
    assert response.status_code == status, response.text
    message = response.json()["error"]["message"]
    assert message.startswith("salesdemo: ")
    assert fragment in message
    if status == 502 and "403" in fragment:
        assert "AE_FABRIC_CAPACITIES_KEY" in message
    _fleet_clean(response.text, caplog.text)


def test_the_fleet_key_is_scrubbed_at_any_length_and_in_encoded_forms() -> None:
    from urllib.parse import quote

    shared = fabric.Fleet(url=FLEET_URL, key="k")
    text = f"saw k and {FLEET_SIG} and {quote(FLEET_URL, safe='')}"
    scrubbed = fabric._scrub(text, shared)
    assert FLEET_SIG not in scrubbed and "k " not in scrubbed
    assert "workflows%2Ffleet000" not in scrubbed


async def test_an_odd_fleet_row_is_shape_checked_never_echoed() -> None:
    env = {"AE_FABRIC_CAPACITIES_URL": FLEET_URL, "AE_FABRIC_CAPACITIES_KEY": FLEET_KEY}
    rows = [
        {**_fleet_capacity("good"), "sku": "<b>F4</b>", "subscriptionName": "<script>"},
        {**_fleet_capacity("bad"), "name": "../etc"},
        "not a row",
    ]
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=_fleet_answer(rows=rows))
    )
    answer = await fabric.overview(env, transport=transport)
    (row,) = answer["capacities"]
    assert row["name"] == "good"
    assert row["sku"] is None and row["hourlyUsd"] is None
    assert row["subscriptionName"] is None


# ── review fixes (2026-10-21): fallbacks, deadlines, one-row writes ─────────


def _fleet_failure(kind: str) -> httpx.Response | Exception:
    """A fresh failing fleet reply; a Response is single use, so never shared."""
    if kind == "timeout":
        return httpx.ReadTimeout(f"timed out reading {FLEET_URL}")
    if kind == "unreachable":
        return httpx.ConnectError(f"cannot connect to {FLEET_URL}")
    if kind == "403":
        return httpx.Response(403, json={"error": "capacity key missing or wrong"})
    if kind == "404":
        return httpx.Response(404, json={"error": "capacity not found"})
    return httpx.Response(int(kind), json={"error": f"failed at {FLEET_URL}; retrying is safe"})


def _own_logic_app(action: str) -> object:
    """aetraining's wake Logic App: probes read Active, *action* answers changed."""
    moving = {"wake": "Resuming", "suspend": "Pausing"}

    def answer(request: httpx.Request) -> httpx.Response:
        asked = _params(request)["action"]
        if asked == "probe":
            return httpx.Response(200, json=_answer("Active"))
        return httpx.Response(200, json=_answer(moving[asked], asked, True))

    return answer


@pytest.mark.parametrize("kind", ["500", "403", "timeout", "unreachable"])
@pytest.mark.parametrize("action", ["wake", "suspend"])
async def test_start_and_pause_use_the_own_logic_app_when_the_list_fails(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    action: str,
    kind: str,
) -> None:
    # No list known yet, so fleet_act lists first, and that list fails: no id, no write.
    _configure(monkeypatch)
    _configure_fleet(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        fleet_route = mock.post(url__startswith=FLEET_INVOKE)
        fleet_route.side_effect = [_fleet_failure(kind)]
        own = mock.get(url__startswith=INVOKE)
        own.side_effect = _own_logic_app(action)
        response = await client.post(f"/v1/ae/fabric/aetraining/{action}", json={})
    assert response.status_code == 200, response.text
    assert response.json()["action"] == action and response.json()["changed"] is True
    assert [_sent(call.request) for call in fleet_route.calls] == [{"action": "list"}]
    assert [_params(call.request)["action"] for call in own.calls] == [action]
    _fleet_clean(response.text, caplog.text)


@pytest.mark.parametrize("kind", ["403", "404", "missing"])
async def test_a_write_the_fleet_refused_first_uses_the_own_logic_app(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # 403: the fleet checks the key before anything; 404: it found no such capacity;
    # missing: the list does not have aetraining at all (its subscription unread).
    _configure(monkeypatch)
    _configure_fleet(monkeypatch)
    listed = (
        [row for row in FLEET_ROWS if row["name"] != "aetraining"]
        if kind == "missing"
        else FLEET_ROWS
    )
    with respx.mock(assert_all_called=False) as mock:
        fleet_route = mock.post(url__startswith=FLEET_INVOKE)
        fleet_route.side_effect = [
            httpx.Response(200, json=_fleet_answer(rows=listed)),
            *([] if kind == "missing" else [_fleet_failure(kind)]),
        ]
        own = mock.get(url__startswith=INVOKE)
        own.side_effect = _own_logic_app("wake")
        await client.get("/v1/ae/fabric")
        response = await client.post("/v1/ae/fabric/aetraining/wake", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "Resuming"
    assert [_params(call.request)["action"] for call in own.calls] == ["probe", "wake"]


@pytest.mark.parametrize(
    ("kind", "status", "fragment"),
    [
        ("502", 502, "retrying is safe"),
        ("timeout", 504, "within 9.5 s; it may still be under way"),
        ("unreachable", 502, "could not be reached"),
    ],
)
@pytest.mark.parametrize("action", ["wake", "suspend"])
async def test_a_write_that_may_have_gone_out_never_falls_back(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    action: str,
    kind: str,
    status: int,
    fragment: str,
) -> None:
    _configure(monkeypatch)
    _configure_fleet(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        fleet_route = mock.post(url__startswith=FLEET_INVOKE)
        fleet_route.side_effect = [httpx.Response(200, json=_fleet_answer()), _fleet_failure(kind)]
        own = mock.get(url__startswith=INVOKE)
        own.side_effect = _own_logic_app(action)
        await client.get("/v1/ae/fabric")
        response = await client.post(f"/v1/ae/fabric/aetraining/{action}", json={})
    assert response.status_code == status, response.text
    assert fragment in response.json()["error"]["message"]
    # Only the GET's probe reached the wake Logic App: no second write.
    assert [_params(call.request)["action"] for call in own.calls] == ["probe"]
    _fleet_clean(response.text, caplog.text)


async def test_pause_without_a_suspend_key_reports_the_fleets_failure(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The own Logic App cannot pause without the key, so the fleet's sentence stands.
    _configure(monkeypatch, key=False)
    _configure_fleet(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__startswith=FLEET_INVOKE).side_effect = [_fleet_failure("500")]
        own = mock.get(url__startswith=INVOKE).respond(200, json=_answer("Active"))
        response = await client.post("/v1/ae/fabric/aetraining/suspend", json={})
    assert response.status_code == 502, response.text
    assert "answered 500 to the list" in response.json()["error"]["message"]
    assert not own.called


async def test_a_write_gets_its_own_deadline_and_the_list_keeps_the_read_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert fabric.REQUEST_TIMEOUT_S == 8.0
    assert fabric.FLEET_WRITE_TIMEOUT_S == 9.5
    monkeypatch.setattr(fabric, "REQUEST_TIMEOUT_S", 0.2)
    monkeypatch.setattr(fabric, "FLEET_WRITE_TIMEOUT_S", 0.8)
    shared = fabric.Fleet(url=FLEET_URL, key=FLEET_KEY)
    row = _fleet_capacity("teamlab", "F4", "Pausing")

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.4)
        if _sent(request)["action"] == "list":
            return httpx.Response(200, json=_fleet_answer())
        return httpx.Response(200, json=_fleet_write("suspend", row))

    transport = httpx.MockTransport(handler)
    # 0.4 s is past the read deadline but inside the write's.
    answer = await fabric.fleet_call(
        shared, "suspend", capacity_id=_fleet_id("teamlab"), transport=transport
    )
    assert answer["changed"] is True and answer["capacity"]["state"] == "Pausing"
    with pytest.raises(fabric.FabricError) as caught:
        await fabric.fleet_call(shared, "list", transport=transport)
    assert caught.value.status == 504 and caught.value.unsent is True
    assert "under way" not in str(caught.value)


async def test_a_write_updates_its_own_row_and_keeps_the_rest(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    _configure_fleet(monkeypatch)
    pausing = _fleet_capacity("aetraining", "F4", "Pausing")
    updating = _fleet_capacity("salesdemo", "F16", "Updating")
    with respx.mock(assert_all_called=False) as mock:
        fleet_route = mock.post(url__startswith=FLEET_INVOKE)
        fleet_route.side_effect = [
            httpx.Response(200, json=_fleet_answer()),
            httpx.Response(200, json=_fleet_write("suspend", pausing)),
            httpx.Response(200, json=_fleet_write("scale", updating)),
        ]
        mock.get(url__startswith=INVOKE).respond(200, json=_rich_probe())
        await client.get("/v1/ae/fabric")
        paused = await client.post("/v1/ae/fabric/aetraining/suspend", json={})
        resized = await client.post("/v1/ae/fabric/salesdemo/scale", json={"sku": "F16"})
    assert paused.json()["state"] == "Pausing"
    assert resized.status_code == 200, resized.text
    assert resized.json()["state"] == "Updating" and resized.json()["hourlyUsd"] == 2.88
    # The one-row write answers never replaced the list: the resize found its id
    # there without another list call, and every capacity is still known.
    assert [_sent(call.request)["action"] for call in fleet_route.calls] == [
        "list",
        "suspend",
        "scale",
    ]
    known = {row["name"]: row for row in fabric._fleet_known}
    assert set(known) == {"aetraining", "salesdemo", "teamlab"}
    assert known["aetraining"]["state"] == "Pausing"
    assert known["salesdemo"]["sku"] == "F16"
    assert known["teamlab"]["state"] == "Paused"


async def test_an_action_never_offers_keep_awake_from_old_details(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    _configure_fleet(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        fleet_route = mock.post(url__startswith=FLEET_INVOKE)
        fleet_route.side_effect = [
            httpx.Response(200, json=_fleet_answer()),
            # Start on an Active capacity: nothing changes and it stays Active.
            httpx.Response(200, json=_fleet_write("resume", FLEET_ROWS[1], changed=False)),
        ]
        mock.get(url__startswith=INVOKE).respond(200, json=_rich_probe())
        view = await client.get("/v1/ae/fabric")
        started = await client.post("/v1/ae/fabric/aetraining/wake", json={})
    before = {row["name"]: row for row in view.json()["capacities"]}["aetraining"]
    assert before["can_keep_awake"] is True
    row = started.json()
    assert row["state"] == "Active" and row["changed"] is False
    # The last details still ride along, but keep awake is not offered from them.
    assert row["resumedAt"] == "2026-10-21T08:00:00Z"
    assert row["can_keep_awake"] is False
    assert row["keepAwakeOptions"] == []


async def test_keep_awake_with_the_fleet_answers_the_fleets_row(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fleet says F8; the wake Logic App's own answer still says F2 and its price.
    _configure(monkeypatch)
    _configure_fleet(monkeypatch)
    rows = [_fleet_capacity("aetraining", "F8", "Active"), FLEET_ROWS[0]]

    def own(request: httpx.Request) -> httpx.Response:
        if _params(request)["action"] == "probe":
            return httpx.Response(200, json={**_rich_probe(), "hourlyUsd": 0.36})
        return httpx.Response(
            200,
            json={**_answer("Active", "keepawake", True), "sku": "F2", "hourlyUsd": 0.36},
        )

    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__startswith=FLEET_INVOKE).respond(200, json=_fleet_answer(rows=rows))
        mock.get(url__startswith=INVOKE).side_effect = own
        view = await client.get("/v1/ae/fabric")
        kept = await client.post("/v1/ae/fabric/aetraining/keepawake", json={"hours": 2})
    shown = {row["name"]: row for row in view.json()["capacities"]}["aetraining"]
    assert kept.status_code == 200, kept.text
    row = kept.json()
    assert row["action"] == "keepawake" and row["changed"] is True
    assert row["sku"] == "F8"
    assert row["hourlyUsd"] == 1.44 == shown["hourlyUsd"]
    assert row["capacityId"] == _fleet_id("aetraining")
    assert row["region"] == "North Central US"
    assert row["subscriptionName"] == shown["subscriptionName"]
    assert row["resourceGroup"] == "rg-placeholder"
    assert row["canScale"] is True
    assert row["skuChoices"] == list(fabric.SKU_CHOICES)
    # The keep awake's own details are fresh: it stays on offer.
    assert row["can_keep_awake"] is True
    assert row["maxHours"] == 6
    assert "error" not in row


async def test_a_capacity_in_an_unread_subscription_keeps_its_last_known_row() -> None:
    env = {"AE_FABRIC_CAPACITIES_URL": FLEET_URL, "AE_FABRIC_CAPACITIES_KEY": FLEET_KEY}
    other_sub = "00000000-0000-0000-0000-000000000002"
    far = {
        **_fleet_capacity("salesdemo", "F8", "Active"),
        "capacityId": (
            f"/subscriptions/{other_sub}/resourceGroups/rg-placeholder"
            "/providers/Microsoft.Fabric/capacities/salesdemo"
        ),
    }
    near = [FLEET_ROWS[0], FLEET_ROWS[1]]
    answers = [
        _fleet_answer(rows=[*near, far]),
        # Azure sends ids in any case; the match ignores it.
        _fleet_answer(rows=near, errors=[other_sub.upper()]),
    ]
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=answers.pop(0)))
    first = await fabric.overview(env, transport=transport)
    assert "fleetError" not in first
    # Drop the cached list (the known rows stay), then read again.
    fabric.forget("teamlab")
    answer = await fabric.overview(env, transport=transport)
    by = {row["name"]: row for row in answer["capacities"]}
    assert set(by) == {"aetraining", "salesdemo", "teamlab"}
    assert by["salesdemo"]["state"] == "Active" and by["salesdemo"]["sku"] == "F8"
    assert by["salesdemo"]["error"] == fabric.UNREAD_SUBSCRIPTION
    assert "error" not in by["teamlab"] and "error" not in by["aetraining"]
    assert answer["fleetError"].startswith("Azure did not answer for 1 subscription;")
    for message in (answer["fleetError"], by["salesdemo"]["error"]):
        assert other_sub not in message.lower()


# 2026-10-22: GET answers the last known result and refreshes in the background.


def _age_last(seconds: float) -> None:
    """Make the kept answer *seconds* older, as if that long had passed."""
    assert fabric._last is not None
    taken, answer = fabric._last
    fabric._last = (taken - seconds, answer)


def _counting(
    states: list[str], gate: asyncio.Event | None = None, reached: asyncio.Event | None = None
) -> tuple[httpx.MockTransport, list[int]]:
    """A transport answering *states* in turn; calls after the first wait on *gate*."""
    calls: list[int] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if gate is not None and len(calls) > 1:
            if reached is not None:
                reached.set()
            await gate.wait()
        return httpx.Response(200, json=_answer(states[min(len(calls), len(states)) - 1]))

    return httpx.MockTransport(handler), calls


async def test_a_warm_get_answers_without_calling_the_transport() -> None:
    env = {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL}
    transport, calls = _counting(["Active"])
    first = await fabric.overview(env, transport=transport)  # cold: waits live
    assert len(calls) == 1
    before = fabric.refresh_task()
    again = await fabric.overview(env, transport=transport)
    assert again == first
    assert len(calls) == 1
    assert fabric.refresh_task() is before  # no refresh started


async def test_a_stale_get_answers_the_cache_and_starts_exactly_one_refresh() -> None:
    env = {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL}
    gate = asyncio.Event()
    reached = asyncio.Event()
    transport, calls = _counting(["Paused", "Active"], gate, reached)
    first = await fabric.overview(env, transport=transport)
    assert first["capacities"][0]["state"] == "Paused"
    _age_last(fabric.STATUS_TTL_S + 1)
    fabric._status_cache.clear()  # the per-capacity cache has expired as well
    answers = [await fabric.overview(env, transport=transport) for _ in range(3)]
    assert all(answer["capacities"][0]["state"] == "Paused" for answer in answers)
    task = fabric.refresh_task()
    assert task is not None and not task.done()
    await reached.wait()
    assert len(calls) == 2  # the cold read plus ONE refresh, however many polls
    gate.set()
    await task
    fresh = await fabric.overview(env, transport=transport)
    assert fresh["capacities"][0]["state"] == "Active"
    assert len(calls) == 2


async def test_forget_makes_the_next_get_wait_for_a_live_answer() -> None:
    env = {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL}
    transport, calls = _counting(["Paused", "Waking"])
    assert (await fabric.overview(env, transport=transport))["capacities"][0]["state"] == "Paused"
    fabric.forget("aetraining")  # what act() does after a wake
    after = await fabric.overview(env, transport=transport)
    assert after["capacities"][0]["state"] == "Waking"
    assert len(calls) == 2


async def test_a_refresh_begun_before_an_action_does_not_write_back() -> None:
    env = {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL}
    gate = asyncio.Event()
    reached = asyncio.Event()
    transport, _calls = _counting(["Paused", "Paused", "Waking"], gate, reached)
    await fabric.overview(env, transport=transport)
    _age_last(fabric.STATUS_TTL_S + 1)
    fabric._status_cache.clear()
    await fabric.overview(env, transport=transport)  # starts the refresh
    task = fabric.refresh_task()
    assert task is not None
    await reached.wait()
    fabric.forget("aetraining")  # an action lands while the refresh is out
    gate.set()
    await task
    assert fabric._last is None  # the older answer was not kept
    live = await fabric.overview(env, transport=transport)
    assert live["capacities"][0]["state"] == "Waking"


async def test_a_failed_refresh_keeps_the_last_rows_and_shows_the_error() -> None:
    env = {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL}
    healthy = [True]

    def handler(_request: httpx.Request) -> httpx.Response:
        if healthy[0]:
            return httpx.Response(200, json=_answer("Active"))
        return httpx.Response(500, text="down")

    transport = httpx.MockTransport(handler)
    await fabric.overview(env, transport=transport)
    healthy[0] = False
    _age_last(fabric.STATUS_TTL_S + 1)
    fabric._status_cache.clear()
    await fabric.overview(env, transport=transport)
    task = fabric.refresh_task()
    assert task is not None
    await task
    row = (await fabric.overview(env, transport=transport))["capacities"][0]
    assert row["state"] == "Active"  # the last good state stays
    assert row["error"]  # and the failure is shown on the row


def test_failed_rows_keep_the_last_good_fields_by_name_and_across_failures() -> None:
    # A failed probe's row has no capacityId, so it matches the good row by name;
    # a second failure in a row still keeps the good fields (review of 2026-10-22).
    good = {"name": "aetraining", "capacityId": "/subscriptions/x/c", "state": "Active"}
    failed = {"name": "aetraining", "state": "Unknown", "error": "down"}
    once = fabric._keep_last_rows({"capacities": [good]}, {"capacities": [failed]})
    assert once["capacities"] == [{**good, "error": "down"}]
    again = fabric._keep_last_rows(once, {"capacities": [{**failed, "error": "still down"}]})
    assert again["capacities"] == [{**good, "error": "still down"}]


def test_a_failed_row_never_borrows_a_name_two_capacities_share() -> None:
    first = {"name": "dup", "capacityId": "/subscriptions/a/c", "state": "Active"}
    second = {"name": "dup", "capacityId": "/subscriptions/b/c", "state": "Paused"}
    failed = {"name": "dup", "state": "Unknown", "error": "down"}
    kept = fabric._keep_last_rows({"capacities": [first, second]}, {"capacities": [failed]})
    assert kept["capacities"][0] == failed


async def test_a_bug_in_the_refresh_is_logged_by_class_and_never_sticks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    env = {"AE_FABRIC_WAKE_URL_AETRAINING": WAKE_URL}
    transport, _calls = _counting(["Active"])
    await fabric.overview(env, transport=transport)
    _age_last(fabric.STATUS_TTL_S + 1)

    async def boom(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError(WAKE_URL)

    monkeypatch.setattr(fabric, "_live_overview", boom)
    caplog.set_level(logging.DEBUG)
    await fabric.overview(env, transport=transport)
    task = fabric.refresh_task()
    assert task is not None
    await task
    assert "RuntimeError" in caplog.text
    _clean(caplog.text)
    _age_last(fabric.STATUS_TTL_S + 1)
    await fabric.overview(env, transport=transport)  # a new refresh may start
    assert fabric.refresh_task() is not task
