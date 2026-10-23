"""Admin Center Azure card (2026-10-22): ``/v1/ae/azure-cost`` with Azure mocked.

Nothing here reaches Microsoft: the module's calls go through
``httpx.MockTransport``. The tenant id, the client id, the client secret and
every token are secrets, so the tests that touch them also assert they never
reach a response body, an error message or a log line, including when Azure
answers with them echoed.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from ae_omni_policies import azure_cost

SUB_A = "11111111-1111-4111-8111-111111111111"
SUB_B = "22222222-2222-4222-8222-222222222222"
TENANT = "33333333-3333-4333-8333-333333333333"
CLIENT_ID = "44444444-4444-4444-8444-444444444444"
CLIENT_SECRET = "azure~Secret.value_never_logged_0123456789"
TOKEN = "eyJ-azure-token-never-logged-0123456789abcdef"
SECRETS = (TENANT, CLIENT_ID, CLIENT_SECRET, TOKEN)
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
ECHO = f"bad {CLIENT_SECRET} {CLIENT_ID} {TENANT} Bearer {TOKEN}"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _azure_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    for key in list(os.environ):
        if key.startswith("AE_AZURE_COST_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("AE_AZURE_COST_STATE_FILE", str(tmp_path / "ae-azure-cost.json"))
    monkeypatch.setattr(azure_cost, "_now", lambda: NOW)
    azure_cost.reset()
    yield
    azure_cost.reset()


def _configure(monkeypatch: pytest.MonkeyPatch, subs: str | None = None) -> None:
    monkeypatch.setenv("AE_AZURE_COST_SUBSCRIPTIONS", subs or f"{SUB_A}=Production,{SUB_B}=Dev")
    monkeypatch.setenv("AE_AZURE_COST_TENANT_ID", TENANT)
    monkeypatch.setenv("AE_AZURE_COST_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AE_AZURE_COST_CLIENT_SECRET", CLIENT_SECRET)


def _answer(rows: list[list[object]], columns: list[str]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "properties": {
                "columns": [{"name": name, "type": "String"} for name in columns],
                "rows": rows,
            }
        },
    )


SERVICE_COLUMNS = ["Cost", "UsageDate", "ServiceName", "Currency"]
RESOURCE_COLUMNS = ["Cost", "UsageDate", "ResourceId", "Currency"]
SQL_ID = f"/subscriptions/{SUB_A}/resourceGroups/rg/providers/Microsoft.Sql/servers/sql-prod-01"


def _service_rows() -> list[list[object]]:
    rows: list[list[object]] = [[10.0, 20260915, "SQL Database", "USD"]]
    rows += [[10.0, 20261000 + day, "SQL Database", "USD"] for day in range(1, 21)]
    rows += [[5.0, 20261000 + day, "App Service", "USD"] for day in range(1, 21)]
    return rows


def _resource_rows() -> list[list[object]]:
    rows: list[list[object]] = [[1.0, 20261000 + day, SQL_ID, "USD"] for day in range(12, 20)]
    rows += [[10.12, 20261020, SQL_ID, "USD"], [3.0, 20261021, SQL_ID, "USD"]]
    return rows


def _azure(calls: list[httpx.Request], *, subscription: Handler | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "login.microsoftonline.com":
            form = parse_qs(request.content.decode())
            assert form["scope"] == ["https://management.azure.com/.default"]
            assert form["client_secret"] == [CLIENT_SECRET]
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if subscription is not None:
            return subscription(request)
        grouping = json.loads(request.content)["dataset"]["grouping"][0]["name"]
        if grouping == "ServiceName":
            return _answer(_service_rows(), SERVICE_COLUMNS)
        return _answer(_resource_rows(), RESOURCE_COLUMNS)

    return handler


def _mock(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    monkeypatch.setattr(azure_cost, "_shared_transport", lambda: httpx.MockTransport(handler))


async def _read(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    first = await azure_cost.overview()
    assert first["configured"] is True
    task = azure_cost.reading_task()
    assert task is not None
    await task
    return await azure_cost.overview()


def _no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text


async def test_not_configured_says_how_to_turn_it_on() -> None:
    answer = await azure_cost.overview()
    assert answer["configured"] is False
    assert "azure-cost-client-secret" in str(answer["note"])
    assert "AE_AZURE_COST_SUBSCRIPTIONS" in str(answer["note"])


@pytest.mark.parametrize("missing", ["TENANT_ID", "CLIENT_ID", "CLIENT_SECRET", "SUBSCRIPTIONS"])
async def test_each_setting_is_required(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _configure(monkeypatch)
    monkeypatch.delenv(f"AE_AZURE_COST_{missing}")
    assert (await azure_cost.overview())["configured"] is False


def test_subscriptions_keep_names_and_drop_junk() -> None:
    parsed = azure_cost._subscriptions(f"{SUB_A.upper()}=Prod, not-a-guid=x,{SUB_B},{SUB_A}=Again")
    assert parsed == ((SUB_A, "Prod"), (SUB_B, SUB_B))


async def test_a_reading_gives_the_month_the_pace_the_services_and_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    calls: list[httpx.Request] = []
    _mock(monkeypatch, _azure(calls))
    answer = await _read(monkeypatch)
    view = answer["view"]
    assert isinstance(view, dict)
    # Two subscriptions, each 20 days at $15, so $600 so far; $20 (September) for last month.
    assert view["month"] == {
        "spent": 600.0,
        "pace": 930.0,
        "lastMonth": 20.0,
        "through": "2026-10-20",
    }
    assert [s["name"] for s in view["subscriptions"]] == ["Production", "Dev"]
    assert view["services"] == [
        {"name": "SQL Database", "cost": 400.0},
        {"name": "App Service", "cost": 200.0},
    ]
    assert len(view["daily"]) == azure_cost.SPARK_DAYS
    assert view["daily"][-1]["date"] == "2026-10-21"
    flag = view["flags"][0]
    assert flag["resource"] == "sql-prod-01"
    assert flag["subscription"] == "Production"
    assert flag["cost"] == 10.12
    assert flag["average"] == 1.0
    assert flag["ratio"] == 10.1
    # One token, then two queries per subscription.
    assert len(calls) == 1 + 2 * 2
    assert answer["asOf"] == "2026-10-21T12:00:00Z"
    _no_secrets(json.dumps(answer))


async def test_a_steady_resource_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")

    def steady(request: httpx.Request) -> httpx.Response:
        grouping = json.loads(request.content)["dataset"]["grouping"][0]["name"]
        if grouping == "ServiceName":
            return _answer(_service_rows(), SERVICE_COLUMNS)
        rows: list[list[object]] = [[9.5, 20261000 + day, SQL_ID, "USD"] for day in range(12, 21)]
        return _answer(rows, RESOURCE_COLUMNS)

    _mock(monkeypatch, _azure([], subscription=steady))
    view = (await _read(monkeypatch))["view"]
    assert isinstance(view, dict)
    assert view["flags"] == []


async def test_the_cache_answers_without_calling_azure_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    calls: list[httpx.Request] = []
    _mock(monkeypatch, _azure(calls))
    await _read(monkeypatch)
    seen = len(calls)
    again = await azure_cost.overview()
    assert again["reading"] is False
    assert len(calls) == seen


async def test_one_rate_limited_subscription_does_not_blank_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)

    def limited(request: httpx.Request) -> httpx.Response:
        if SUB_B in request.url.path:
            return httpx.Response(429, headers={"retry-after": "30"})
        grouping = json.loads(request.content)["dataset"]["grouping"][0]["name"]
        if grouping == "ServiceName":
            return _answer(_service_rows(), SERVICE_COLUMNS)
        return _answer(_resource_rows(), RESOURCE_COLUMNS)

    _mock(monkeypatch, _azure([], subscription=limited))
    view = (await _read(monkeypatch))["view"]
    assert isinstance(view, dict)
    by_name = {s["name"]: s for s in view["subscriptions"]}
    assert by_name["Production"]["error"] is None
    assert by_name["Production"]["spent"] == 300.0
    assert "rate limiting" in by_name["Dev"]["error"]
    assert "retry in 30 s" in by_name["Dev"]["error"]


async def test_no_access_says_which_role_to_give(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    _mock(monkeypatch, _azure([], subscription=lambda r: httpx.Response(403, text=ECHO)))
    view = (await _read(monkeypatch))["view"]
    assert isinstance(view, dict)
    error = view["subscriptions"][0]["error"]
    assert "Cost Management Reader" in error
    _no_secrets(error)


async def test_a_refused_credential_never_echoes_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    _mock(monkeypatch, lambda request: httpx.Response(401, text=ECHO))
    caplog.set_level(logging.DEBUG)
    answer = await _read(monkeypatch)
    view = answer["view"]
    assert isinstance(view, dict)
    assert all(
        "refused the Azure cost service principal" in s["error"] for s in view["subscriptions"]
    )
    _no_secrets(json.dumps(answer))
    _no_secrets(caplog.text)


async def test_an_upstream_error_body_is_not_passed_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    _mock(monkeypatch, _azure([], subscription=lambda r: httpx.Response(500, text=ECHO)))
    answer = await _read(monkeypatch)
    assert answer["view"]["subscriptions"][0]["error"] == "Cost Management answered 500."
    _no_secrets(json.dumps(answer))


async def test_the_route_answers_a_signed_in_user(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = await client.get("/v1/ae/azure-cost")
    assert response.status_code == 200, response.text
    assert response.json()["configured"] is False
    _configure(monkeypatch)
    _mock(monkeypatch, _azure([]))
    response = await client.get("/v1/ae/azure-cost")
    assert response.status_code == 200
    assert response.json()["configured"] is True
    _no_secrets(response.text)


async def test_more_pages_than_the_card_reads_is_an_error_not_a_partial_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")

    def endless(request: httpx.Request) -> httpx.Response:
        body = {
            "properties": {
                "columns": [{"name": "Cost"}, {"name": "UsageDate"}, {"name": "ServiceName"}],
                "rows": [[1.0, 20261001, "SQL Database"]],
                "nextLink": "https://management.azure.com/next?page=2",
            }
        }
        return httpx.Response(200, json=body)

    _mock(monkeypatch, _azure([], subscription=endless))
    view = (await _read(monkeypatch))["view"]
    assert isinstance(view, dict)
    assert "pages of rows" in view["subscriptions"][0]["error"]
    assert view["month"]["spent"] == 0.0


async def test_a_subscription_in_another_currency_is_not_added_to_the_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)

    def mixed(request: httpx.Request) -> httpx.Response:
        grouping = json.loads(request.content)["dataset"]["grouping"][0]["name"]
        euro = SUB_B in request.url.path
        if grouping == "ServiceName":
            rows = [[c, d, s, "EUR" if euro else "USD"] for c, d, s, _ in _service_rows()]
            return _answer(rows, SERVICE_COLUMNS)
        return _answer(_resource_rows(), RESOURCE_COLUMNS)

    _mock(monkeypatch, _azure([], subscription=mixed))
    view = (await _read(monkeypatch))["view"]
    assert isinstance(view, dict)
    assert view["currency"] == "USD"
    assert view["month"]["spent"] == 300.0
    by_name = {s["name"]: s for s in view["subscriptions"]}
    # Its own line, in euros, outside the dollar total.
    assert by_name["Dev"]["currency"] == "EUR"
    assert by_name["Dev"]["counted"] is False
    assert by_name["Dev"]["spent"] == 300.0
    assert by_name["Dev"]["error"] is None
    assert by_name["Production"]["counted"] is True


def test_a_retained_reading_does_not_bring_last_months_services_into_this_month() -> None:
    cfg = azure_cost.Config(TENANT, CLIENT_ID, CLIENT_SECRET, ((SUB_A, "Production"),))
    old = {
        "daily": {"2026-09-15": 10.0},
        "services": {"2026-09": {"SQL Database": 10.0}},
        "resources": {},
        "currency": "USD",
    }
    view = azure_cost._assemble(cfg, {SUB_A: old}, {SUB_A: "rate limiting"}, NOW.date())
    assert view["services"] == []
    assert view["month"]["spent"] == 0.0
    assert view["month"]["lastMonth"] == 10.0


def test_a_removed_subscription_does_not_keep_flagging() -> None:
    cfg = azure_cost.Config(TENANT, CLIENT_ID, CLIENT_SECRET, ((SUB_A, "Production"),))
    days = {f"2026-10-{d}": 1.0 for d in range(12, 20)} | {"2026-10-20": 10.0}
    reading = {"daily": {}, "services": {}, "resources": {SQL_ID: days}, "currency": "USD"}
    view = azure_cost._assemble(cfg, {SUB_A: reading, SUB_B: reading}, {}, NOW.date())
    assert [flag["subscription"] for flag in view["flags"]] == ["Production"]


def test_a_new_resource_or_an_old_spike_is_not_flagged() -> None:
    names = {SUB_A: "Production"}
    today = NOW.date()

    def flags(days: dict[str, float]) -> list[dict[str, object]]:
        reading = {"resources": {SQL_ID: days}}
        return azure_cost._flags({SUB_A: reading}, names, today)

    # Two days old: $0.10 then $3 would read as 60x its "average" of one prior day.
    assert flags({"2026-10-19": 0.1, "2026-10-20": 3.0}) == []
    # A spike eight days ago and nothing since is history.
    old = {f"2026-10-{d}": 1.0 for d in range(5, 12)} | {"2026-10-12": 10.0}
    assert flags(old) == []
    # A steady week then a jump yesterday is the case the card exists for.
    jump = {f"2026-10-{d}": 1.0 for d in range(13, 20)} | {"2026-10-20": 10.0}
    assert len(flags(jump)) == 1


async def test_a_timeout_names_only_the_subscriptions_it_did_not_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    _configure(monkeypatch)
    monkeypatch.setattr(azure_cost, "READING_DEADLINE_S", 0.3)
    fast = _azure([])

    async def slow_second(request: httpx.Request) -> httpx.Response:
        if SUB_B in request.url.path:
            await asyncio.sleep(5)
        return fast(request)

    _mock(monkeypatch, slow_second)  # type: ignore[arg-type]
    view = (await _read(monkeypatch))["view"]
    assert isinstance(view, dict)
    by_name = {s["name"]: s for s in view["subscriptions"]}
    assert by_name["Production"]["error"] is None
    assert by_name["Production"]["spent"] == 300.0
    assert by_name["Dev"]["error"] == "The reading took too long."


async def test_an_unexpected_failure_shows_on_the_card_and_never_echoes_a_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    _mock(monkeypatch, _azure([]))

    async def boom(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError(ECHO)

    monkeypatch.setattr(azure_cost, "_read_subscription", boom)
    caplog.set_level(logging.DEBUG)
    answer = await _read(monkeypatch)
    view = answer["view"]
    assert isinstance(view, dict)
    assert view["subscriptions"][0]["error"] == "The reading failed unexpectedly."
    _no_secrets(json.dumps(answer))
    _no_secrets(caplog.text)


async def test_a_failed_reading_is_tried_again_in_two_minutes_not_an_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    _mock(monkeypatch, _azure([], subscription=lambda r: httpx.Response(500)))
    clock = [1000.0]
    monkeypatch.setattr(azure_cost, "_monotonic", lambda: clock[0])
    await _read(monkeypatch)
    clock[0] += azure_cost.RETRY_TTL_S - 1
    assert (await azure_cost.overview())["reading"] is False
    clock[0] += 2
    assert (await azure_cost.overview())["reading"] is True


def test_the_total_is_in_the_currency_most_subscriptions_bill_in() -> None:
    sub_c = "55555555-5555-4555-8555-555555555555"
    cfg = azure_cost.Config(
        TENANT, CLIENT_ID, CLIENT_SECRET, ((sub_c, "UK"), (SUB_A, "Prod"), (SUB_B, "Dev"))
    )

    def reading(currency: str) -> dict[str, object]:
        return {
            "daily": {"2026-10-20": 10.0},
            "services": {},
            "resources": {},
            "currency": currency,
        }

    # The GBP subscription is first in config order, yet the two USD ones set the total.
    both = {sub_c: reading("GBP"), SUB_A: reading("USD"), SUB_B: reading("USD")}
    view = azure_cost._assemble(cfg, both, {}, NOW.date())
    assert view["currency"] == "USD"
    assert {s["name"]: s["counted"] for s in view["subscriptions"]} == {
        "UK": False,
        "Prod": True,
        "Dev": True,
    }
    # With the GBP subscription unread the answer is the same, so nothing swaps between polls.
    rest = {SUB_A: reading("USD"), SUB_B: reading("USD")}
    assert azure_cost._assemble(cfg, rest, {sub_c: "limited"}, NOW.date())["currency"] == "USD"


def _limited(retry_after: str | None) -> Handler:
    headers = {"retry-after": retry_after} if retry_after else {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers=headers)

    return handler


@pytest.mark.parametrize(
    ("header", "wait"),
    [("300", 300.0), ("5", 60.0), ("7200", 3600.0), (None, 900.0), ("soon", 900.0)],
)
async def test_a_rate_limit_waits_azures_time_within_a_minute_and_an_hour_else_fifteen(
    monkeypatch: pytest.MonkeyPatch, header: str | None, wait: float
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    _mock(monkeypatch, _azure([], subscription=_limited(header)))
    clock = [1000.0]
    monkeypatch.setattr(azure_cost, "_monotonic", lambda: clock[0])
    await _read(monkeypatch)
    clock[0] += wait - 1
    assert (await azure_cost.overview())["reading"] is False
    clock[0] += 2
    assert (await azure_cost.overview())["reading"] is True


async def test_a_rate_limit_stops_the_pass_so_no_more_queries_extend_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    calls: list[httpx.Request] = []
    _mock(monkeypatch, _azure(calls, subscription=_limited("30")))
    view = (await _read(monkeypatch))["view"]
    assert isinstance(view, dict)
    # One token call and one query: the second subscription is never asked.
    assert len(calls) == 2
    assert all("rate limiting" in s["error"] for s in view["subscriptions"])


async def test_another_failure_still_retries_in_two_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    _mock(monkeypatch, _azure([], subscription=lambda r: httpx.Response(500)))
    clock = [1000.0]
    monkeypatch.setattr(azure_cost, "_monotonic", lambda: clock[0])
    await _read(monkeypatch)
    clock[0] += azure_cost.RETRY_TTL_S + 1
    assert (await azure_cost.overview())["reading"] is True


async def test_a_failed_spike_check_keeps_the_totals_and_says_so_in_a_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")

    def sub(request: httpx.Request) -> httpx.Response:
        grouping = json.loads(request.content)["dataset"]["grouping"][0]["name"]
        if grouping == "ServiceName":
            return _answer(_service_rows(), SERVICE_COLUMNS)
        return httpx.Response(500, text=ECHO)

    _mock(monkeypatch, _azure([], subscription=sub))
    view = (await _read(monkeypatch))["view"]
    assert isinstance(view, dict)
    row = view["subscriptions"][0]
    # The row is not a failed one (the card counts "error"), and its totals are all there.
    assert row["error"] is None
    assert row["spent"] == 300.0
    assert view["month"]["spent"] == 300.0
    assert view["flags"] == []
    assert row["note"] == (
        "Spike check unavailable: Cost Management answered 500. Totals are complete."
    )
    _no_secrets(row["note"])


async def test_only_permanent_errors_wait_the_hour_before_asking_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    _mock(monkeypatch, _azure([], subscription=lambda r: httpx.Response(403, text=ECHO)))
    clock = [1000.0]
    monkeypatch.setattr(azure_cost, "_monotonic", lambda: clock[0])
    await _read(monkeypatch)
    # No role is not going to fix itself: two minutes later the card still answers from cache.
    clock[0] += azure_cost.RETRY_TTL_S + 1
    assert (await azure_cost.overview())["reading"] is False
    clock[0] += azure_cost.CACHE_TTL_S
    assert (await azure_cost.overview())["reading"] is True


async def test_a_permanent_error_beside_a_transient_one_still_retries_in_two_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)

    def sub(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403 if SUB_A in str(request.url) else 500)

    _mock(monkeypatch, _azure([], subscription=sub))
    clock = [1000.0]
    monkeypatch.setattr(azure_cost, "_monotonic", lambda: clock[0])
    await _read(monkeypatch)
    clock[0] += azure_cost.RETRY_TTL_S + 1
    assert (await azure_cost.overview())["reading"] is True


def test_a_subscription_with_no_rows_does_not_vote_for_usd() -> None:
    cfg = azure_cost.Config(TENANT, CLIENT_ID, CLIENT_SECRET, ((SUB_A, "Prod"), (SUB_B, "Dev")))
    empty: dict[str, object] = {"daily": {}, "services": {}, "resources": {}, "currency": None}
    euro = {
        "daily": {"2026-10-20": 10.0},
        "services": {},
        "resources": {},
        "currency": "EUR",
    }
    view = azure_cost._assemble(cfg, {SUB_A: empty, SUB_B: euro}, {}, NOW.date())
    assert view["currency"] == "EUR"
    assert view["month"]["spent"] == 10.0
    rows = {s["name"]: s for s in view["subscriptions"]}
    assert rows["Prod"]["counted"] is True
    assert rows["Prod"]["currency"] == "EUR"
    # Nothing voted at all: the default stands.
    assert azure_cost._assemble(cfg, {SUB_A: empty}, {}, NOW.date())["currency"] == "USD"
    # A reading cached before the change always carried a string currency and has no note.
    old = {"daily": {}, "services": {}, "resources": {}, "currency": "USD"}
    kept = azure_cost._assemble(cfg, {SUB_A: old}, {}, NOW.date())
    assert kept["subscriptions"][0]["note"] is None


def _spike_fails(status: int) -> Handler:
    def sub(request: httpx.Request) -> httpx.Response:
        grouping = json.loads(request.content)["dataset"]["grouping"][0]["name"]
        if grouping == "ServiceName":
            return _answer(_service_rows(), SERVICE_COLUMNS)
        return httpx.Response(status)

    return sub


async def test_a_transient_spike_failure_retries_in_two_minutes_a_permanent_one_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    clock = [1000.0]
    monkeypatch.setattr(azure_cost, "_monotonic", lambda: clock[0])
    _mock(monkeypatch, _azure([], subscription=_spike_fails(500)))
    await _read(monkeypatch)
    clock[0] += azure_cost.RETRY_TTL_S + 1
    assert (await azure_cost.overview())["reading"] is True
    await azure_cost.reading_task()
    # A 403 on the spike query will not fix itself.
    _mock(monkeypatch, _azure([], subscription=_spike_fails(403)))
    clock[0] += azure_cost.CACHE_TTL_S + 1
    await _read(monkeypatch)
    clock[0] += azure_cost.RETRY_TTL_S + 1
    assert (await azure_cost.overview())["reading"] is False


async def test_a_failed_spike_check_keeps_the_flags_of_the_last_good_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    clock = [1000.0]
    monkeypatch.setattr(azure_cost, "_monotonic", lambda: clock[0])
    _mock(monkeypatch, _azure([]))
    first = (await _read(monkeypatch))["view"]
    assert isinstance(first, dict)
    assert first["flags"]
    _mock(monkeypatch, _azure([], subscription=_spike_fails(500)))
    clock[0] += azure_cost.CACHE_TTL_S + 1
    second = (await _read(monkeypatch))["view"]
    assert isinstance(second, dict)
    assert second["subscriptions"][0]["note"]
    assert second["flags"] == first["flags"]


async def test_a_401_from_cost_management_drops_the_token_and_is_not_a_missing_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    calls: list[httpx.Request] = []
    _mock(monkeypatch, _azure(calls, subscription=lambda r: httpx.Response(401, text=ECHO)))
    clock = [1000.0]
    monkeypatch.setattr(azure_cost, "_monotonic", lambda: clock[0])
    view = (await _read(monkeypatch))["view"]
    assert isinstance(view, dict)
    error = view["subscriptions"][0]["error"]
    assert "Cost Management Reader" not in error
    assert "signs in again" in error
    _no_secrets(error)
    assert azure_cost._token_cache == {}
    clock[0] += azure_cost.RETRY_TTL_S + 1
    assert (await azure_cost.overview())["reading"] is True


@pytest.mark.parametrize(
    ("body", "permanent"),
    [
        ('{"error": "invalid_client"}', True),
        ('{"error": "invalid_grant"}', True),
        ('{"error": "temporarily_unavailable"}', False),
        ("not json", False),
    ],
)
async def test_only_a_400_that_names_the_principal_is_permanent(
    monkeypatch: pytest.MonkeyPatch, body: str, permanent: bool
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    _mock(monkeypatch, lambda request: httpx.Response(400, text=body))
    clock = [1000.0]
    monkeypatch.setattr(azure_cost, "_monotonic", lambda: clock[0])
    view = (await _read(monkeypatch))["view"]
    assert isinstance(view, dict)
    error = view["subscriptions"][0]["error"]
    if permanent:
        assert "refused the Azure cost service principal" in error
    else:
        assert error == "Microsoft sign-in answered 400."
    clock[0] += azure_cost.RETRY_TTL_S + 1
    assert (await azure_cost.overview())["reading"] is not permanent


def test_a_stale_note_is_not_shown_beside_a_new_error() -> None:
    cfg = azure_cost.Config(TENANT, CLIENT_ID, CLIENT_SECRET, ((SUB_A, "Prod"),))
    reading = {
        "daily": {"2026-10-20": 10.0},
        "services": {},
        "resources": {},
        "currency": "USD",
        "note": "Spike check unavailable: x. Totals are complete.",
    }
    clean = azure_cost._assemble(cfg, {SUB_A: reading}, {}, NOW.date())
    assert clean["subscriptions"][0]["note"]
    failed = azure_cost._assemble(cfg, {SUB_A: reading}, {SUB_A: "limited"}, NOW.date())
    assert failed["subscriptions"][0]["note"] is None


# The saved reading (2026-10-22): a deploy restarts the server, and the card used to say
# "Reading..." until a first GET started a fresh reading.


def _state_file() -> Path:
    return Path(os.environ["AE_AZURE_COST_STATE_FILE"])


def _clock(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    wall = [NOW]
    monkeypatch.setattr(azure_cost, "_now", lambda: wall[0])
    return wall


_GOOD_VIEW: dict[str, object] = {
    "currency": "USD",
    "month": {"spent": 10.0, "pace": None, "lastMonth": 0.0, "through": "2026-10-20"},
    "daily": [{"date": "2026-10-20", "cost": 10.0}],
    "subscriptions": [],
    "services": [],
    "flags": [],
}
_GOOD_READING: dict[str, object] = {
    "daily": {"2026-10-20": 10.0},
    "services": {"2026-10": {"SQL Database": 10.0}},
    "resources": {},
    "currency": "USD",
    "note": None,
}


def _saved_file(**parts: object) -> str:
    body: dict[str, object] = {
        "version": 1,
        "subscriptions": [[SUB_A, "Production"]],
        "principal": azure_cost.Config(
            TENANT, CLIENT_ID, CLIENT_SECRET, ((SUB_A, "Production"),)
        ).principal(),
        "at": "2026-10-21T12:00:00Z",
        "due": "2026-10-21T13:00:00Z",
        "view": _GOOD_VIEW,
        "readings": {SUB_A: _GOOD_READING},
        "errors": {},
    }
    body.update(parts)
    return json.dumps(body)


async def test_a_carried_spike_reading_drops_days_past_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Readings now outlive a restart, so the copy kept when the spike check fails is trimmed.
    cfg = azure_cost.Config(TENANT, CLIENT_ID, CLIENT_SECRET, ((SUB_A, "Production"),))

    def no_spike(request: httpx.Request) -> httpx.Response:
        grouping = json.loads(request.content)["dataset"]["grouping"][0]["name"]
        if grouping == "ServiceName":
            return _answer(_service_rows(), SERVICE_COLUMNS)
        return httpx.Response(500)

    previous = {
        "resources": {SQL_ID: {"2026-09-01": 9.0, "2026-10-19": 1.0}, "old": {"2026-09-02": 1.0}}
    }
    reading = await azure_cost._read_subscription(
        cfg, httpx.MockTransport(no_spike), TOKEN, SUB_A, NOW.date(), previous
    )
    assert reading["resources"] == {SQL_ID: {"2026-10-19": 1.0}}
    assert previous["resources"][SQL_ID] == {"2026-09-01": 9.0, "2026-10-19": 1.0}


def _restart() -> None:
    # What a deploy does to the process: every in-memory cache is gone.
    azure_cost.reset()


async def test_a_reading_is_saved_with_its_time_and_when_the_next_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    _mock(monkeypatch, _azure([]))
    answer = await _read(monkeypatch)
    saved = json.loads(_state_file().read_text(encoding="utf-8"))
    assert saved["at"] == answer["asOf"] == "2026-10-21T12:00:00Z"
    assert saved["due"] == "2026-10-21T13:00:00Z"
    assert saved["view"] == answer["view"]
    assert saved["subscriptions"] == [[SUB_A, "Production"], [SUB_B, "Dev"]]
    assert set(saved["readings"]) == {SUB_A, SUB_B}
    assert _state_file().stat().st_mode & 0o077 == 0


async def test_a_restart_answers_from_the_file_and_reads_nothing_when_not_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    _mock(monkeypatch, _azure([]))
    before = await _read(monkeypatch)
    _restart()
    wall = _clock(monkeypatch)
    wall[0] = NOW + timedelta(minutes=50)
    calls: list[httpx.Request] = []
    _mock(monkeypatch, _azure(calls))
    after = await azure_cost.overview()
    assert after["reading"] is False
    assert after["asOf"] == before["asOf"]
    assert after["view"] == before["view"]
    assert azure_cost.reading_task() is None
    assert calls == []


async def test_a_restart_with_a_due_file_shows_it_and_starts_one_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    _mock(monkeypatch, _azure([]))
    before = await _read(monkeypatch)
    _restart()
    wall = _clock(monkeypatch)
    wall[0] = NOW + timedelta(seconds=azure_cost.CACHE_TTL_S + 1)
    calls: list[httpx.Request] = []
    _mock(monkeypatch, _azure(calls))
    first = await azure_cost.overview()
    assert first["reading"] is True
    assert first["view"] == before["view"]
    task = azure_cost.reading_task()
    assert (await azure_cost.overview())["reading"] is True
    assert azure_cost.reading_task() is task
    assert task is not None
    await task
    # One sign-in and two queries per subscription: one reading, not two.
    assert len(calls) == 5
    assert (await azure_cost.overview())["asOf"] == "2026-10-21T13:00:01Z"


async def test_a_saved_rate_limit_is_still_honored_after_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    _mock(monkeypatch, _azure([], subscription=_limited("300")))
    await _read(monkeypatch)
    assert json.loads(_state_file().read_text())["due"] == "2026-10-21T12:05:00Z"
    _restart()
    wall = _clock(monkeypatch)
    wall[0] = NOW + timedelta(seconds=299)
    clock = [5.0]
    monkeypatch.setattr(azure_cost, "_monotonic", lambda: clock[0])
    calls: list[httpx.Request] = []
    _mock(monkeypatch, _azure(calls))
    assert (await azure_cost.overview())["reading"] is False
    # Only one second of the wait is left; this process's own clock now runs it out.
    clock[0] += 0.5
    assert (await azure_cost.overview())["reading"] is False
    clock[0] += 1
    assert (await azure_cost.overview())["reading"] is True


@pytest.mark.parametrize(
    "change",
    [
        {"AE_AZURE_COST_SUBSCRIPTIONS": f"{SUB_A}=Production"},
        {"AE_AZURE_COST_SUBSCRIPTIONS": f"{SUB_B}=Dev,{SUB_A}=Production"},
        {"AE_AZURE_COST_CLIENT_ID": "66666666-6666-4666-8666-666666666666"},
    ],
)
async def test_a_file_read_for_another_configuration_is_discarded(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, str]
) -> None:
    _configure(monkeypatch)
    _mock(monkeypatch, _azure([]))
    await _read(monkeypatch)
    _restart()
    for key, value in change.items():
        monkeypatch.setenv(key, value)
    answer = await azure_cost.overview()
    assert answer["view"] is None
    assert answer["asOf"] is None
    assert answer["reading"] is True


async def test_a_well_formed_saved_file_is_accepted() -> None:
    # The baseline for the corrupt cases below: each one breaks a single part of this file.
    cfg = azure_cost.Config(TENANT, CLIENT_ID, CLIENT_SECRET, ((SUB_A, "Production"),))
    path = Path(os.environ["AE_AZURE_COST_STATE_FILE"])
    path.write_text(_saved_file(), encoding="utf-8")
    saved = azure_cost._load(cfg, path)
    assert saved is not None
    assert saved["readings"][SUB_A]["daily"] == {"2026-10-20": 10.0}


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"version": 99}),
        _saved_file(readings={SUB_A: {**_GOOD_READING, "daily": {"2026-10-20": "ten"}}}),
        _saved_file(view={**_GOOD_VIEW, "month": None, "daily": "x"}),
        _saved_file(view={**_GOOD_VIEW, "flags": [0]}),
    ],
)
async def test_a_corrupt_file_is_ignored_and_reads_as_today(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    _configure(monkeypatch, f"{SUB_A}=Production")
    _state_file().write_text(content, encoding="utf-8")
    _mock(monkeypatch, _azure([]))
    with caplog.at_level(logging.WARNING, logger="ae_omni_policies.azure_cost"):
        answer = await azure_cost.overview()
    assert answer["view"] is None
    assert answer["reading"] is True
    assert "ignoring" in caplog.text
    assert "not json" not in caplog.text
    task = azure_cost.reading_task()
    assert task is not None
    await task
    assert (await azure_cost.overview())["view"] is not None


async def test_the_file_never_holds_a_secret_or_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)

    def echoing(request: httpx.Request) -> httpx.Response:
        if SUB_B in request.url.path:
            return httpx.Response(403, text=ECHO)
        return _azure([])(request)

    _mock(monkeypatch, _azure([], subscription=echoing))
    await _read(monkeypatch)
    text = _state_file().read_text(encoding="utf-8")
    _no_secrets(text)
    assert f"{TENANT}:{CLIENT_ID}" not in text
    assert json.loads(text)["errors"][SUB_B]


async def test_an_empty_state_file_setting_keeps_the_reading_in_memory_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AE_AZURE_COST_STATE_FILE", "")
    _configure(monkeypatch)
    _mock(monkeypatch, _azure([]))
    assert (await _read(monkeypatch))["view"] is not None
    assert list(tmp_path.iterdir()) == []


async def test_boot_loads_the_file_and_reads_only_when_it_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    from ae_omni_policies.router import DEBUG_ROUTERS

    assert any(entry[0] is azure_cost.lifespan_router for entry in DEBUG_ROUTERS)
    app = FastAPI()
    app.include_router(azure_cost.lifespan_router)
    _configure(monkeypatch)
    calls: list[httpx.Request] = []
    _mock(monkeypatch, _azure(calls))
    # No file yet: boot starts the first reading, so the first visit may find it done.
    async with app.router.lifespan_context(app):
        task = azure_cost.reading_task()
        assert task is not None
        await task
    assert _state_file().exists()
    _restart()
    calls.clear()
    # A fresh file: boot answers from it and asks Azure nothing.
    async with app.router.lifespan_context(app):
        assert azure_cost.reading_task() is None
        assert (await azure_cost.overview())["asOf"] == "2026-10-21T12:00:00Z"
    assert calls == []
