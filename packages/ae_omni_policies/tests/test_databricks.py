"""Admin Center Databricks (2026-10-19): ``/v1/ae/databricks`` on the real app, Databricks mocked.

Nothing here reaches a real workspace: the routes are driven through respx
and the module's own calls through ``httpx.MockTransport``. The client id, the
client secret and every token are secrets, so every test that touches them
also asserts they never reach a response body, an error message, the state
file or a log line, including when Databricks answers 500 with them echoed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx
import pytest
import respx

from ae_omni_policies import auth, databricks

# A credit for the tests that need one; the server has no built-in credit.
CREDIT_ENV = {
    "AE_DATABRICKS_CREDIT_USD": "500",
    "AE_DATABRICKS_CREDIT_CADENCE": "once",
    "AE_DATABRICKS_CREDIT_START": "2030-01-01",
    "AE_DATABRICKS_CREDIT_END": "2030-12-31",
}
HOST = "https://dbc-test0000-abcd.cloud.databricks.com"
CLIENT_ID = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
# '+', '/' and '=' so the percent-encoded spellings differ from the raw one.
CLIENT_SECRET = "dose+Secret/value=never_logged_0123456789"
TOKEN = "eyJ-token-value-never-logged-0123456789abcdef"
TOKEN2 = "eyJ-second-token-never-logged-9876543210fedcba"
BASIC = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
SECRETS = (
    CLIENT_ID,
    CLIENT_SECRET,
    quote_plus(CLIENT_SECRET),
    quote(CLIENT_SECRET, safe=""),
    TOKEN,
    TOKEN2,
    BASIC,
)
WID = "a1b2c3d4e5f60718"
OTHER = "0011223344556677"
TOKEN_URL = f"{HOST}/oidc/v1/token"
LIST_URL = f"{HOST}/api/2.0/sql/warehouses"
STATEMENTS_URL = f"{HOST}/api/2.0/sql/statements"
STATEMENT_ID = "01ef-aaaa-bbbb-cccc"
ECHO = (
    f"internal error for {CLIENT_ID} secret {CLIENT_SECRET} ({quote_plus(CLIENT_SECRET)}) "
    f"Authorization: Bearer {TOKEN} Basic {BASIC}"
)
# What the token endpoint could echo: no token has been issued yet.
CREDENTIAL_ECHO = (
    f"invalid client {CLIENT_ID} secret {CLIENT_SECRET} ({quote_plus(CLIENT_SECRET)}) "
    f"Authorization: Basic {BASIC}"
)


@pytest.fixture(autouse=True)
def _databricks_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    import os

    for key in list(os.environ):
        if key.startswith("AE_DATABRICKS_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("AE_DATABRICKS_STATE_FILE", str(tmp_path / "ae-databricks.json"))
    for key, value in CREDIT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(databricks, "POLL_INTERVAL_S", 0)
    databricks.reset()
    yield
    databricks.reset()


def _configure(monkeypatch: pytest.MonkeyPatch, host: str = HOST + "/") -> None:
    monkeypatch.setenv("AE_DATABRICKS_HOST", host)
    monkeypatch.setenv("AE_DATABRICKS_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("AE_DATABRICKS_CLIENT_SECRET", CLIENT_SECRET)


def _warehouse(
    state: str = "STOPPED", warehouse_id: str = WID, serverless: bool = True
) -> dict[str, object]:
    return {
        "id": warehouse_id,
        "name": "Serverless Starter Warehouse",
        "state": state,
        "cluster_size": "2X-Small",
        "auto_stop_mins": 5,
        "enable_serverless_compute": serverless,
        "warehouse_type": "PRO",
    }


def _row(state: str = "STOPPED", warehouse_id: str = WID) -> dict[str, object]:
    return {
        "id": warehouse_id,
        "name": "Serverless Starter Warehouse",
        "state": state,
        "size": "2X-Small",
        "autoStopMins": 5,
        "serverless": True,
        "url": f"{HOST}/sql/warehouses/{warehouse_id}",
    }


def _token_answer(token: str = TOKEN, expires_in: int = 3600) -> dict[str, object]:
    return {
        "access_token": token,
        "token_type": "Bearer",
        "scope": "sql",
        "expires_in": expires_in,
    }


def _statement(state: str, **extra: object) -> dict[str, object]:
    return {"statement_id": STATEMENT_ID, "status": {"state": state, **extra}}


def _succeeded(month: str | None = "12.5", total: str | None = "40.25") -> dict[str, object]:
    return {
        **_statement("SUCCEEDED"),
        "manifest": {"total_row_count": 1},
        "result": {"data_array": [[month, total, "2026-10-19T03:00:00.000Z"]]},
    }


def _breakdown(*rows: list[object]) -> dict[str, object]:
    """A finished breakdown statement with *rows* of (kind, item, usd)."""
    return {
        **_statement("SUCCEEDED"),
        "manifest": {"total_row_count": len(rows)},
        "result": {"data_array": [list(row) for row in rows]},
    }


def _sql_of(request: httpx.Request) -> str:
    return json.loads(request.content)["statement"]


def _routed(spend: httpx.Response | Exception, breakdown: httpx.Response | Exception) -> object:
    """A side effect for the statements route: each query gets its own answer."""

    def answer(request: httpx.Request) -> httpx.Response:
        reply = spend if _sql_of(request) == databricks.SPEND_SQL else breakdown
        if isinstance(reply, Exception):
            raise reply
        return reply

    return answer


def _days_back(n: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


def _mock(
    mock: respx.MockRouter, *warehouses: dict[str, object]
) -> tuple[respx.Route, respx.Route]:
    token = mock.post(TOKEN_URL).respond(200, json=_token_answer())
    listing = mock.get(LIST_URL).respond(200, json={"warehouses": list(warehouses)})
    return token, listing


def _clean(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


async def _settle() -> None:
    task = databricks.reading_task()
    if task is not None:
        await task


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ── configuration ────────────────────────────────────────────────────────


def test_configuration_needs_a_good_host_and_both_secrets() -> None:
    full = {
        "AE_DATABRICKS_HOST": HOST + "/",
        "AE_DATABRICKS_CLIENT_ID": f" {CLIENT_ID} ",
        "AE_DATABRICKS_CLIENT_SECRET": CLIENT_SECRET,
    }
    ws = databricks.workspace(full)
    assert ws is not None
    assert ws.host == HOST  # the trailing slash is dropped
    assert ws.client_id == CLIENT_ID
    # The dataclass repr is what a stray log line or traceback would print.
    _clean(repr(ws))
    for missing in full:
        assert databricks.workspace({**full, missing: " "}) is None
    for bad in ("http://dbc.cloud.databricks.com", "https://dbc", f"{HOST}/sql", "dbc.test"):
        env = {**full, "AE_DATABRICKS_HOST": bad}
        assert databricks.workspace(env) is None
        assert databricks.not_configured_note(env) == databricks.BAD_HOST
    assert databricks.not_configured_note({}) == databricks.HOW_TO_ENABLE


def test_credit_settings_read_and_a_bad_value_means_no_credit() -> None:
    full = databricks.credit(CREDIT_ENV)
    assert full == databricks.Credit(500.0, "once", date(2030, 1, 1), date(2030, 12, 31))
    custom = databricks.credit(
        {
            "AE_DATABRICKS_CREDIT_USD": "150",
            "AE_DATABRICKS_CREDIT_CADENCE": "Monthly",
            "AE_DATABRICKS_CREDIT_START": "2030-02-01",
            "AE_DATABRICKS_CREDIT_END": "2031-02-01",
        }
    )
    assert custom == databricks.Credit(150.0, "monthly", date(2030, 2, 1), date(2031, 2, 1))
    # An unreadable cadence alone falls back to once; the credit stays.
    assert databricks.credit({**CREDIT_ENV, "AE_DATABRICKS_CREDIT_CADENCE": "weekly"}) == full
    # Any other unreadable or missing value leaves no credit at all.
    none = databricks.Credit(None, "once", None, None)
    for bad in (
        {"AE_DATABRICKS_CREDIT_USD": "-5"},
        {"AE_DATABRICKS_CREDIT_USD": "0"},
        {"AE_DATABRICKS_CREDIT_USD": "nan"},
        {"AE_DATABRICKS_CREDIT_USD": ""},
        {"AE_DATABRICKS_CREDIT_START": "soon"},
        {"AE_DATABRICKS_CREDIT_END": ""},
    ):
        assert databricks.credit({**CREDIT_ENV, **bad}) == none, bad


def test_with_no_credit_vars_there_is_no_credit_and_no_error() -> None:
    assert databricks.credit({}) == databricks.Credit(None, "once", None, None)
    spend = databricks.spend_view(databricks.credit({}))
    assert (spend["creditUsd"], spend["creditStart"], spend["creditEnd"]) == (None, None, None)
    assert spend["status"] == "never"


def test_with_no_credit_the_spend_window_starts_on_the_months_first_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(databricks, "_now", lambda: datetime(2026, 10, 19, 12, 0, tzinfo=UTC))
    assert databricks.credit({}).since == date(2026, 10, 1)
    assert databricks.credit(CREDIT_ENV).since == date(2030, 1, 1)


def test_a_credit_that_starts_after_it_ends_is_no_credit() -> None:
    backwards = {
        **CREDIT_ENV,
        "AE_DATABRICKS_CREDIT_START": "2030-12-31",
        "AE_DATABRICKS_CREDIT_END": "2030-01-01",
    }
    assert databricks.credit(backwards) == databricks.Credit(None, "once", None, None)


def test_switching_from_a_credit_to_none_keeps_the_months_reading(tmp_path: Path) -> None:
    _seed(
        tmp_path / "ae-databricks.json",
        status="ok",
        monthUsd=3.5,
        totalUsd=9.0,
        readAt="2026-10-18T08:00:00Z",
        creditStart="2030-01-01",
    )
    spend = databricks.spend_view(databricks.credit({}))
    assert (spend["monthUsd"], spend["totalUsd"]) == (3.5, None)
    assert spend["readAt"] == "2026-10-18T08:00:00Z"


def test_a_partial_credit_config_warns_once_naming_variables_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    partial = {"AE_DATABRICKS_CREDIT_USD": "123456", "AE_DATABRICKS_CREDIT_START": "2030-01-01"}
    databricks.credit(partial)
    databricks.credit(partial)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "AE_DATABRICKS_CREDIT_END" in warnings[0].getMessage()
    assert "123456" not in caplog.text and "2030-01-01" not in caplog.text
    caplog.clear()
    databricks.credit({})
    databricks.credit(CREDIT_ENV)
    assert not caplog.records


def test_secrets_are_scrubbed_in_encoded_forms_and_with_tokens_held() -> None:
    ws = databricks.Workspace(HOST, CLIENT_ID, CLIENT_SECRET)
    found = ws.secrets()
    for expected in (CLIENT_ID, CLIENT_SECRET, BASIC, quote_plus(CLIENT_SECRET)):
        assert expected in found
    assert TOKEN not in found
    databricks._tokens[ws.key()] = (TOKEN, 0.0)
    assert TOKEN in ws.secrets()
    _clean(databricks._scrub(ECHO, ws))


# ── GET /v1/ae/databricks ────────────────────────────────────────────────


async def test_none_configured_says_how_to_turn_it_on(client: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        anything = mock.route().respond(500)
        response = await client.get("/v1/ae/databricks")
        assert not anything.called
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["host"] is None
    assert body["error"] is None
    assert body["warehouses"] == []
    note = body["note"]
    for fragment in (
        "AE_DATABRICKS_CLIENT_ID",
        "AE_DATABRICKS_CLIENT_SECRET",
        "AE_DATABRICKS_HOST",
        "The Admin Center's Databricks secrets (optional)",
    ):
        assert fragment in note
    assert body["spend"] == {
        "status": "never",
        "message": None,
        "readAt": None,
        "asOf": None,
        "monthUsd": None,
        "totalUsd": None,
        "creditUsd": 500.0,
        "cadence": "once",
        "creditStart": "2030-01-01",
        "creditEnd": "2030-12-31",
        "warehouseId": None,
        "products": None,
        "days": None,
        "note": None,
    }


async def test_the_listing_has_the_contracts_shape(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    odd = [
        {**_warehouse(), "id": "not-hex"},
        "not a row",
        {
            "id": OTHER,
            "state": "HIBERNATING",
            "cluster_size": "<script>",
            "auto_stop_mins": True,
            "enable_serverless_compute": "yes",
        },
    ]
    with respx.mock(assert_all_called=False) as mock:
        token, listing = _mock(mock, _warehouse(), *odd)
        statements = mock.post(STATEMENTS_URL).respond(200, json=_succeeded())
        response = await client.get("/v1/ae/databricks")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    assert body["note"] is None
    assert body["host"] == HOST
    assert body["error"] is None
    assert body["warehouses"] == [
        _row(),
        {
            "id": OTHER,
            "name": OTHER,
            "state": databricks.UNKNOWN_STATE,
            "size": "",
            "autoStopMins": None,
            "serverless": False,
            "url": f"{HOST}/sql/warehouses/{OTHER}",
        },
    ]
    assert body["spend"]["status"] == "never"
    assert body["spend"]["warehouseId"] == WID
    # The token: client credentials, scope sql only, HTTP Basic.
    sent = token.calls.last.request
    assert sent.content == b"grant_type=client_credentials&scope=sql"
    assert sent.headers["Authorization"] == f"Basic {BASIC}"
    assert sent.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert listing.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
    # The warehouse is stopped: GET never reads spend.
    assert not statements.called
    _clean(response.text, caplog.text)


async def test_zero_warehouses_is_an_empty_list(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).respond(200, json=_token_answer())
        mock.get(LIST_URL).respond(200, json={})
        response = await client.get("/v1/ae/databricks")
    body = response.json()
    assert body["warehouses"] == []
    assert body["error"] is None
    assert body["spend"]["warehouseId"] is None


async def test_the_token_is_cached_and_refreshed_before_it_expires(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    monkeypatch.setattr(databricks, "_monotonic", clock)
    with respx.mock(assert_all_called=False) as mock:
        token = mock.post(TOKEN_URL)
        token.side_effect = [
            httpx.Response(200, json=_token_answer(TOKEN)),
            httpx.Response(200, json=_token_answer(TOKEN2)),
        ]
        listing = mock.get(LIST_URL).respond(200, json={"warehouses": [_warehouse()]})
        await client.get("/v1/ae/databricks")
        await client.get("/v1/ae/databricks")
        # The listing is held LIST_TTL_S; the second GET asked nothing.
        assert (token.call_count, listing.call_count) == (1, 1)
        clock.now += databricks.LIST_TTL_S + 1
        await client.get("/v1/ae/databricks")
        assert (token.call_count, listing.call_count) == (1, 2)
        assert listing.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
        # Five minutes before the hour is up, a new token is asked for.
        clock.now = 1000.0 + 3600 - databricks.TOKEN_REFRESH_MARGIN_S + 1
        last = await client.get("/v1/ae/databricks")
        assert (token.call_count, listing.call_count) == (2, 3)
        assert listing.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN2}"
    _clean(last.text, caplog.text)


async def test_a_refused_token_is_dropped_and_asked_for_again(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        token = mock.post(TOKEN_URL).respond(200, json=_token_answer())
        listing = mock.get(LIST_URL)
        listing.side_effect = [
            httpx.Response(401, json={"error_code": "UNAUTHENTICATED", "message": ECHO}),
            httpx.Response(200, json={"warehouses": [_warehouse()]}),
        ]
        first = await client.get("/v1/ae/databricks")
        second = await client.get("/v1/ae/databricks")
    assert first.json()["error"]["code"] == databricks.CODE_AUTH
    assert "refused the token (401)" in first.json()["error"]["message"]
    assert second.json()["error"] is None
    assert token.call_count == 2
    _clean(first.text, second.text, caplog.text)


@pytest.mark.parametrize(
    ("token_reply", "list_reply", "code", "fragment"),
    [
        (
            httpx.Response(
                401, json={"error": "invalid_client", "error_description": CREDENTIAL_ECHO}
            ),
            None,
            databricks.CODE_AUTH,
            "AE_DATABRICKS_CLIENT_SECRET",
        ),
        (
            httpx.Response(500, text=f"<html>{CREDENTIAL_ECHO}</html>"),
            None,
            databricks.CODE_FAILED,
            "500",
        ),
        (httpx.Response(200, text="not json"), None, databricks.CODE_FAILED, "expected JSON"),
        (
            httpx.ConnectError(CREDENTIAL_ECHO),
            None,
            databricks.CODE_FAILED,
            "could not be reached",
        ),
        (httpx.ReadTimeout(CREDENTIAL_ECHO), None, databricks.CODE_TIMEOUT, "within 8 s"),
        (
            None,
            httpx.Response(500, json={"error_code": "INTERNAL_ERROR", "message": ECHO}),
            databricks.CODE_FAILED,
            "[redacted]",
        ),
        (None, httpx.ConnectTimeout(ECHO), databricks.CODE_TIMEOUT, "within 8 s"),
    ],
)
async def test_a_failed_listing_rides_inside_a_200_naming_no_secret(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    token_reply: httpx.Response | Exception | None,
    list_reply: httpx.Response | Exception | None,
    code: str,
    fragment: str,
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        token = mock.post(TOKEN_URL)
        listing = mock.get(LIST_URL)
        for route, reply in ((token, token_reply), (listing, list_reply)):
            if reply is None:
                route.respond(200, json=_token_answer() if route is token else {"warehouses": []})
            elif isinstance(reply, Exception):
                route.mock(side_effect=reply)
            else:
                route.mock(return_value=reply)
        response = await client.get("/v1/ae/databricks")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["warehouses"] == []
    assert body["error"]["code"] == code
    assert fragment in body["error"]["message"]
    assert body["spend"]["status"] == "never"
    _clean(response.text, caplog.text)


# ── POST start and stop ──────────────────────────────────────────────────


@pytest.mark.parametrize(("action", "after"), [("start", "STARTING"), ("stop", "STOPPING")])
async def test_start_and_stop_answer_the_row_and_drop_the_listing(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    action: str,
    after: str,
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        _token, listing = _mock(mock, _warehouse("STOPPED"))
        acted = mock.post(f"{LIST_URL}/{WID}/{action}").respond(200, json={})
        one = mock.get(f"{LIST_URL}/{WID}").respond(200, json=_warehouse(after))
        statements = mock.post(STATEMENTS_URL).respond(200, json=_succeeded())
        await client.get("/v1/ae/databricks")
        response = await client.post(f"/v1/ae/databricks/warehouses/{WID}/{action}", json={})
        await client.get("/v1/ae/databricks")
        assert acted.call_count == 1
        assert one.call_count == 1
        # The listing went with the action: the next GET asked again.
        assert listing.call_count == 2
        # Neither a Start nor a GET read spend.
        assert not statements.called
    assert response.status_code == 200, response.text
    assert response.json() == _row(after)
    sent = acted.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["Content-Type"] == "application/json"
    assert f"databricks {action} on {WID}: {after}" in caplog.text
    _clean(response.text, caplog.text)


async def test_a_start_whose_row_cannot_be_read_back_answers_starting(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("STOPPED"))
        mock.post(f"{LIST_URL}/{WID}/start").respond(200, json={})
        mock.get(f"{LIST_URL}/{WID}").respond(500, json={"message": "busy"})
        response = await client.post(f"/v1/ae/databricks/warehouses/{WID}/start", json={})
    assert response.status_code == 200
    assert response.json() == _row("STARTING")


async def test_an_unknown_warehouse_is_a_404(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        token, listing = _mock(mock, _warehouse())
        acted = mock.post(url__startswith=f"{LIST_URL}/").respond(200, json={})
        listed_out = await client.post(f"/v1/ae/databricks/warehouses/{OTHER}/start", json={})
        assert listing.call_count == 1
        before = token.call_count
        # A malformed id is refused before any call at all.
        malformed = await client.post("/v1/ae/databricks/warehouses/NOT-AN-ID/stop", json={})
        assert token.call_count == before
        assert listing.call_count == 1
        assert not acted.called
    assert listed_out.status_code == 404
    assert listed_out.json()["error"]["code"] == databricks.CODE_NOT_FOUND
    assert malformed.status_code == 404


async def test_actions_and_refresh_need_a_credential(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with respx.mock(assert_all_called=False) as mock:
        anything = mock.route().respond(200, json={})
        for path in (
            f"/v1/ae/databricks/warehouses/{WID}/start",
            f"/v1/ae/databricks/warehouses/{WID}/stop",
            "/v1/ae/databricks/spend/refresh",
        ):
            response = await client.post(path, json={})
            assert response.status_code == 409, path
            error = response.json()["error"]
            assert error["code"] == databricks.CODE_NOT_CONFIGURED
            assert "AE_DATABRICKS_CLIENT_ID" in error["message"]
        assert not anything.called


async def test_actions_and_refresh_need_an_authenticated_user(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    with respx.mock(assert_all_called=False) as mock:
        anything = mock.route().respond(200, json={})
        for path in (
            f"/v1/ae/databricks/warehouses/{WID}/start",
            f"/v1/ae/databricks/warehouses/{WID}/stop",
            "/v1/ae/databricks/spend/refresh",
        ):
            response = await client.post(path, json={})
            assert response.status_code == 401, path
            _clean(response.text)
        assert (await client.get("/v1/ae/databricks")).status_code == 401
        assert not anything.called


async def test_actions_are_json_only_and_origin_checked(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        anything = mock.route().respond(200, json={})
        assert (await client.get(f"/v1/ae/databricks/warehouses/{WID}/start")).status_code in (
            404,
            405,
        )
        for path in (
            f"/v1/ae/databricks/warehouses/{WID}/start",
            "/v1/ae/databricks/spend/refresh",
        ):
            form = await client.post(
                path, content="x=1", headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            assert form.status_code == 415, path
            foreign = await client.post(path, json={}, headers={"Origin": "https://evil.example"})
            assert foreign.status_code == 403, path
        assert not anything.called


@pytest.mark.parametrize(
    ("reply", "status", "code", "fragment"),
    [
        (
            httpx.Response(500, json={"error_code": "INTERNAL_ERROR", "message": ECHO}),
            502,
            databricks.CODE_FAILED,
            "[redacted]",
        ),
        (
            httpx.Response(403, json={"error_code": "PERMISSION_DENIED", "message": ECHO}),
            502,
            databricks.CODE_FAILED,
            "answered 403",
        ),
        (httpx.Response(200, text="<html>"), 502, databricks.CODE_FAILED, "expected JSON"),
        (httpx.ConnectError(ECHO), 502, databricks.CODE_FAILED, "could not be reached"),
        (httpx.ConnectTimeout(ECHO), 504, databricks.CODE_TIMEOUT, "look again in a minute"),
    ],
)
async def test_a_failed_start_names_no_secret_and_is_never_retried(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reply: httpx.Response | Exception,
    status: int,
    code: str,
    fragment: str,
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse())
        route = mock.post(f"{LIST_URL}/{WID}/start")
        if isinstance(reply, Exception):
            route.mock(side_effect=reply)
        else:
            route.mock(return_value=reply)
        response = await client.post(f"/v1/ae/databricks/warehouses/{WID}/start", json={})
        assert route.call_count == 1
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == code
    assert fragment in error["message"]
    assert f"databricks start on {WID} failed for local" in caplog.text
    _clean(response.text, caplog.text)


async def test_an_action_past_the_deadline_may_still_be_under_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(databricks, "REQUEST_TIMEOUT_S", 0.05)
    ws = databricks.Workspace(HOST, CLIENT_ID, CLIENT_SECRET)
    paths: list[str] = []

    async def slow(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/oidc/v1/token":
            return httpx.Response(200, json=_token_answer())
        if request.url.path == "/api/2.0/sql/warehouses":
            return httpx.Response(200, json={"warehouses": [_warehouse()]})
        await asyncio.sleep(0.5)
        return httpx.Response(200, json={})

    with pytest.raises(databricks.DatabricksError) as caught:
        await databricks.act(ws, WID, "start", transport=httpx.MockTransport(slow))
    assert caught.value.status == 504
    assert "may still be under way" in str(caught.value)
    # Never retried, and no read back after a timeout.
    assert paths.count(f"/api/2.0/sql/warehouses/{WID}/start") == 1
    assert f"/api/2.0/sql/warehouses/{WID}" not in paths


async def test_a_listing_past_the_deadline_is_a_timeout_without_the_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(databricks, "REQUEST_TIMEOUT_S", 0.05)
    ws = databricks.Workspace(HOST, CLIENT_ID, CLIENT_SECRET)

    async def slow(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oidc/v1/token":
            return httpx.Response(200, json=_token_answer())
        await asyncio.sleep(0.5)
        return httpx.Response(200, json={"warehouses": [_warehouse()]})

    with pytest.raises(databricks.DatabricksError) as caught:
        await databricks.act(ws, WID, "stop", transport=httpx.MockTransport(slow))
    assert caught.value.status == 504
    assert "may still be under way" not in str(caught.value)


async def test_a_token_timeout_before_the_call_is_sent_has_no_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(databricks, "_monotonic", clock)
    ws = databricks.Workspace(HOST, CLIENT_ID, CLIENT_SECRET)
    paths: list[str] = []
    tokens = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal tokens
        paths.append(request.url.path)
        if request.url.path == "/oidc/v1/token":
            tokens += 1
            if tokens > 1:
                await asyncio.sleep(0.5)
            # Good for one second past the refresh margin.
            return httpx.Response(
                200, json=_token_answer(expires_in=int(databricks.TOKEN_REFRESH_MARGIN_S) + 1)
            )
        return httpx.Response(200, json={"warehouses": [_warehouse()]})

    transport = httpx.MockTransport(handler)
    await databricks.warehouses(ws, transport=transport)
    # The listing is still cached; the token is due for a refresh, which hangs.
    clock.now += 5
    monkeypatch.setattr(databricks, "REQUEST_TIMEOUT_S", 0.05)
    with pytest.raises(databricks.DatabricksError) as caught:
        await databricks.act(ws, WID, "start", transport=transport)
    assert caught.value.status == 504
    assert "may still be under way" not in str(caught.value)
    assert f"/api/2.0/sql/warehouses/{WID}/start" not in paths


# ── spend: the cost guard ────────────────────────────────────────────────


def _seed(path: Path, **fields: object) -> None:
    path.write_text(json.dumps(fields), encoding="utf-8")


@pytest.mark.parametrize(
    ("state", "serverless", "attempted_minutes_ago", "reads"),
    [
        ("STOPPED", True, None, False),
        ("STARTING", True, None, False),
        ("RUNNING", False, None, False),
        ("RUNNING", True, 10, False),
        ("RUNNING", True, 31, False),
        ("RUNNING", True, 59, False),
        ("RUNNING", True, None, True),
        ("RUNNING", True, 61, True),
    ],
)
async def test_get_reads_spend_only_off_a_running_warehouse_when_stale(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
    serverless: bool,
    attempted_minutes_ago: int | None,
    reads: bool,
) -> None:
    _configure(monkeypatch)
    if attempted_minutes_ago is not None:
        attempted = datetime.now(UTC) - timedelta(minutes=attempted_minutes_ago)
        _seed(tmp_path / "ae-databricks.json", status="ok", attemptedAt=databricks.iso(attempted))
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse(state, serverless=serverless))
        one = mock.get(f"{LIST_URL}/{WID}").respond(200, json=_warehouse(state))
        statements = mock.post(STATEMENTS_URL).respond(200, json=_succeeded())
        response = await client.get("/v1/ae/databricks")
        await _settle()
        assert statements.called is reads
        # The uncached state read comes only on the way to an automatic reading.
        assert one.called is reads
    spend = response.json()["spend"]
    assert spend["status"] == ("reading" if reads else spend["status"])
    if not reads:
        assert spend["status"] != "reading"
    assert spend["warehouseId"] == (WID if serverless else None)


async def test_a_reading_on_get_is_reported_and_then_fresh(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    monkeypatch.setattr(databricks, "_monotonic", clock)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("RUNNING"))
        mock.get(f"{LIST_URL}/{WID}").respond(200, json=_warehouse("RUNNING"))
        statements = mock.post(STATEMENTS_URL).respond(200, json=_succeeded())
        first = await client.get("/v1/ae/databricks")
        await _settle()
        clock.now += databricks.LIST_TTL_S + 1
        second = await client.get("/v1/ae/databricks")
        await _settle()
        # Fresh now: the second GET did not read again (the totals, then the breakdown).
        assert [_sql_of(call.request) for call in statements.calls] == [
            databricks.SPEND_SQL,
            databricks.BREAKDOWN_SQL,
        ]
    assert first.json()["spend"]["status"] == "reading"
    spend = second.json()["spend"]
    assert spend["status"] == "ok"
    assert (spend["monthUsd"], spend["totalUsd"]) == (12.5, 40.25)
    _clean(first.text, second.text, caplog.text)


@pytest.mark.parametrize(
    ("uncached", "reads"),
    [
        (httpx.Response(200, json=_warehouse("STOPPED")), False),
        (httpx.Response(200, json=_warehouse("STOPPING")), False),
        (httpx.Response(500, json={"message": ECHO}), False),
        (httpx.Response(200, json=_warehouse("RUNNING")), True),
    ],
)
async def test_an_automatic_reading_rereads_the_state_uncached_first(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    uncached: httpx.Response,
    reads: bool,
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        # The cached listing still says RUNNING; the warehouse may have stopped since.
        _token, listing = _mock(mock, _warehouse("RUNNING"))
        one = mock.get(f"{LIST_URL}/{WID}").mock(return_value=uncached)
        statements = mock.post(STATEMENTS_URL).respond(200, json=_succeeded())
        response = await client.get("/v1/ae/databricks")
        await _settle()
        assert listing.call_count == 1
        assert one.call_count == 1
        assert statements.called is reads
    assert response.json()["spend"]["status"] == "reading"
    spend = databricks.spend_view(databricks.credit())
    if reads:
        assert spend["status"] == "ok"
    else:
        # A skip records nothing: no attempt, no file, the next GET decides again.
        assert spend["status"] == "never"
        assert databricks._state()["attemptedAt"] is None
        assert not (tmp_path / "ae-databricks.json").exists()
    _clean(response.text, caplog.text)


@pytest.mark.parametrize(
    ("auto_stop", "reads"),
    [
        (None, False),
        (60, False),
        (90, False),
        (58, False),
        (0, True),
        (55, True),
        (45, True),
        (5, True),
    ],
)
async def test_get_never_reads_on_a_warehouse_reads_could_hold_awake(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    auto_stop: int | None,
    reads: bool,
) -> None:
    _configure(monkeypatch)
    raw = {**_warehouse("RUNNING"), "auto_stop_mins": auto_stop}
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, raw)
        mock.get(f"{LIST_URL}/{WID}").respond(200, json=raw)
        statements = mock.post(STATEMENTS_URL).respond(200, json=_succeeded())
        await client.get("/v1/ae/databricks")
        await _settle()
        assert statements.called is reads


async def test_an_automatic_try_after_an_error_waits_fifteen_minutes_not_an_hour(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch)
    for minutes, reads in ((10, False), (16, True)):
        databricks.reset()
        attempted = datetime.now(UTC) - timedelta(minutes=minutes)
        _seed(
            tmp_path / "ae-databricks.json", status="error", attemptedAt=databricks.iso(attempted)
        )
        with respx.mock(assert_all_called=False) as mock:
            _mock(mock, _warehouse("RUNNING"))
            mock.get(f"{LIST_URL}/{WID}").respond(200, json=_warehouse("RUNNING"))
            statements = mock.post(STATEMENTS_URL).respond(200, json=_succeeded())
            await client.get("/v1/ae/databricks")
            await _settle()
            assert statements.called is reads


def test_a_reading_prefers_a_running_then_a_starting_serverless_warehouse() -> None:
    def row(warehouse_id: str, state: str, serverless: bool = True) -> dict[str, object]:
        return {"id": warehouse_id, "state": state, "serverless": serverless}

    pick = databricks._reading_warehouse
    assert pick([]) is None
    assert pick([row("a", "RUNNING", serverless=False), row("b", "DELETED")]) is None
    assert pick([row("a", "STOPPED"), row("b", "STARTING"), row("c", "RUNNING")])["id"] == "c"
    assert pick([row("a", "STOPPED"), row("b", "STARTING")])["id"] == "b"
    assert pick([row("a", "DELETING"), row("b", "STOPPED"), row("c", "STOPPED")])["id"] == "b"
    assert (
        pick([row("a", "RUNNING", serverless=False), row("b", "STOPPED"), row("c", "RUNNING")])[
            "id"
        ]
        == "c"
    )


def test_spend_is_priced_at_the_effective_list_price() -> None:
    assert databricks.SPEND_SQL.count("p.pricing.effective_list.default") == 2
    assert "p.pricing.default" not in databricks.SPEND_SQL


# ── spend: the background reading ────────────────────────────────────────


async def test_refresh_reads_in_the_background_and_polls_to_the_end(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch)
    monkeypatch.setenv("AE_DATABRICKS_CREDIT_CADENCE", "monthly")
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("STOPPED"))
        posted = mock.post(STATEMENTS_URL).mock(
            side_effect=_routed(
                httpx.Response(200, json=_statement("PENDING")),
                httpx.Response(200, json=_breakdown()),
            )
        )
        polled = mock.get(f"{STATEMENTS_URL}/{STATEMENT_ID}")
        polled.side_effect = [
            httpx.Response(200, json=_statement("RUNNING")),
            httpx.Response(200, json=_succeeded()),
        ]
        response = await client.post("/v1/ae/databricks/spend/refresh", json={})
        assert response.status_code == 200, response.text
        await _settle()
        after = await client.get("/v1/ae/databricks")
    started = response.json()
    assert started["status"] == "reading"
    assert started["warehouseId"] == WID
    assert started["cadence"] == "monthly"
    body = json.loads(posted.calls[0].request.content)
    assert body == {
        "warehouse_id": WID,
        "statement": databricks.SPEND_SQL,
        "parameters": [{"name": "credit_start", "value": "2030-01-01", "type": "DATE"}],
        "wait_timeout": "30s",
        "on_wait_timeout": "CONTINUE",
        "format": "JSON_ARRAY",
        "disposition": "INLINE",
    }
    assert ":credit_start" in databricks.SPEND_SQL
    assert polled.call_count == 2
    spend = after.json()["spend"]
    assert spend["status"] == "ok"
    assert spend["message"] is None
    assert (spend["monthUsd"], spend["totalUsd"]) == (12.5, 40.25)
    assert spend["asOf"] == "2026-10-19T03:00:00Z"
    assert spend["readAt"] is not None
    saved = (tmp_path / "ae-databricks.json").read_text(encoding="utf-8")
    _clean(response.text, after.text, caplog.text, saved)


async def test_a_sum_over_no_rows_reads_as_zero(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("RUNNING"))
        answer = {**_succeeded(None, None), "result": {"data_array": [[None, None, None]]}}
        mock.post(STATEMENTS_URL).respond(200, json=answer)
        await client.post("/v1/ae/databricks/spend/refresh", json={})
        await _settle()
    spend = databricks.spend_view(databricks.credit())
    assert (spend["status"], spend["monthUsd"], spend["totalUsd"], spend["asOf"]) == (
        "ok",
        0.0,
        0.0,
        None,
    )


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(
            200,
            json=_statement(
                "FAILED",
                error={
                    "error_code": "BAD_REQUEST",
                    "message": "[INSUFFICIENT_PERMISSIONS] User does not have USE CATALOG on "
                    f"Catalog 'system'. {ECHO}",
                },
            ),
        ),
        httpx.Response(
            200,
            json=_statement("FAILED", error={"error_code": "PERMISSION_DENIED", "message": "no"}),
        ),
        httpx.Response(403, json={"error_code": "PERMISSION_DENIED", "message": ECHO}),
    ],
)
async def test_missing_grants_read_as_no_access(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reply: httpx.Response,
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("RUNNING"))
        mock.post(STATEMENTS_URL).mock(return_value=reply)
        await client.post("/v1/ae/databricks/spend/refresh", json={})
        await _settle()
        after = await client.get("/v1/ae/databricks")
    spend = after.json()["spend"]
    assert spend["status"] == "no_access"
    assert spend["message"] == databricks.NO_ACCESS
    for grant in ("USE CATALOG", "USE SCHEMA", "SELECT", "system.billing"):
        assert grant in spend["message"]
    _clean(after.text, caplog.text)


@pytest.mark.parametrize(
    ("reply", "fragment"),
    [
        (
            httpx.Response(
                200,
                json=_statement("FAILED", error={"error_code": "INTERNAL_ERROR", "message": ECHO}),
            ),
            "The spend query failed: ",
        ),
        (
            httpx.Response(500, json={"error_code": "INTERNAL_ERROR", "message": ECHO}),
            "answered 500",
        ),
        (httpx.Response(200, json=_statement("CANCELED")), "The spend query canceled"),
        (httpx.Response(200, json={"status": {"state": "SUCCEEDED"}}), "no statement id"),
        (
            httpx.Response(
                200, json={**_succeeded(), "result": {"data_array": [["x", "1", None]]}}
            ),
            "expected shape",
        ),
        (RuntimeError(ECHO), "(RuntimeError)"),
    ],
)
async def test_a_failed_reading_keeps_the_last_numbers_and_names_no_secret(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    reply: httpx.Response | Exception,
    fragment: str,
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    _seed(
        tmp_path / "ae-databricks.json",
        status="ok",
        readAt="2026-10-18T08:00:00Z",
        asOf="2026-10-18T05:00:00Z",
        monthUsd=3.5,
        totalUsd=9.0,
        warehouseId=WID,
        creditStart="2030-01-01",
    )
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("STOPPED"))
        route = mock.post(STATEMENTS_URL)
        if isinstance(reply, Exception):
            route.mock(side_effect=reply)
        else:
            route.mock(return_value=reply)
        await client.post("/v1/ae/databricks/spend/refresh", json={})
        await _settle()
        after = await client.get("/v1/ae/databricks")
    spend = after.json()["spend"]
    assert spend["status"] == "error"
    assert fragment in spend["message"]
    # The last good reading stays on the strip.
    assert (spend["monthUsd"], spend["totalUsd"], spend["readAt"]) == (
        3.5,
        9.0,
        "2026-10-18T08:00:00Z",
    )
    saved = (tmp_path / "ae-databricks.json").read_text(encoding="utf-8")
    _clean(after.text, caplog.text, saved)


async def test_a_reading_past_its_deadline_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(databricks, "READING_DEADLINE_S", 0)
    ws = databricks.Workspace(HOST, CLIENT_ID, CLIENT_SECRET)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/oidc/v1/token":
            return httpx.Response(200, json=_token_answer())
        return httpx.Response(200, json=_statement("RUNNING"))

    await databricks._read_spend(
        ws, databricks.credit(CREDIT_ENV), WID, transport=httpx.MockTransport(handler)
    )
    assert f"POST /api/2.0/sql/statements/{STATEMENT_ID}/cancel" in seen
    spend = databricks.spend_view(databricks.credit(CREDIT_ENV))
    assert spend["status"] == "error"
    assert "did not finish within" in spend["message"]
    assert "was cancelled" in spend["message"]


def _statement_handler(poll: httpx.Response | Exception, seen: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/oidc/v1/token":
            return httpx.Response(200, json=_token_answer())
        if request.method == "POST" and request.url.path == "/api/2.0/sql/statements":
            return httpx.Response(200, json=_statement("PENDING"))
        if request.url.path.endswith("/cancel"):
            # The cancel's own failure is swallowed.
            return httpx.Response(500, json={"message": ECHO})
        if isinstance(poll, Exception):
            raise poll
        return poll

    return httpx.MockTransport(handler)


CANCEL = f"POST /api/2.0/sql/statements/{STATEMENT_ID}/cancel"


@pytest.mark.parametrize(
    ("poll", "cancels", "status"),
    [
        (httpx.Response(500, json={"message": ECHO}), True, "error"),
        (httpx.ConnectError(ECHO), True, "error"),
        (RuntimeError(ECHO), True, "error"),
        # A statement that ended on its own needs no cancel.
        (
            httpx.Response(200, json=_statement("FAILED", error={"message": "boom"})),
            False,
            "error",
        ),
        (httpx.Response(200, json=_statement("CANCELED")), False, "error"),
        (httpx.Response(200, json=_succeeded()), False, "ok"),
    ],
)
async def test_a_statement_left_running_is_cancelled_on_any_exit(
    caplog: pytest.LogCaptureFixture,
    poll: httpx.Response | Exception,
    cancels: bool,
    status: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    ws = databricks.Workspace(HOST, CLIENT_ID, CLIENT_SECRET)
    seen: list[str] = []
    await databricks._read_spend(
        ws, databricks.credit(CREDIT_ENV), WID, transport=_statement_handler(poll, seen)
    )
    assert (CANCEL in seen) is cancels
    spend = databricks.spend_view(databricks.credit(CREDIT_ENV))
    assert spend["status"] == status
    _clean(json.dumps(spend), caplog.text)


async def test_a_reading_cancelled_mid_poll_cancels_the_statement_and_records_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ws = databricks.Workspace(HOST, CLIENT_ID, CLIENT_SECRET)
    seen: list[str] = []
    polling = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/oidc/v1/token":
            return httpx.Response(200, json=_token_answer())
        if request.method == "POST" and request.url.path == "/api/2.0/sql/statements":
            return httpx.Response(200, json=_statement("PENDING"))
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={})
        polling.set()
        await asyncio.sleep(60)  # a poll that never answers
        return httpx.Response(200, json=_succeeded())

    task = asyncio.create_task(
        databricks._read_spend(
            ws, databricks.credit(CREDIT_ENV), WID, transport=httpx.MockTransport(handler)
        )
    )
    await polling.wait()
    task.cancel()  # what shutdown or databricks.reset() does
    with pytest.raises(asyncio.CancelledError):
        await task
    assert CANCEL in seen
    spend = databricks.spend_view(databricks.credit(CREDIT_ENV))
    assert spend["status"] == "error"
    assert "stopped before it finished" in spend["message"]
    saved = json.loads((tmp_path / "ae-databricks.json").read_text(encoding="utf-8"))
    assert saved["attemptedAt"] is not None
    assert saved["status"] == "error"
    _clean(caplog.text)


async def test_one_reading_at_a_time(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    release = asyncio.Event()
    runs: list[str] = []

    async def held(
        _ws: databricks.Workspace,
        _settings: databricks.Credit,
        warehouse_id: str,
        _transport: httpx.AsyncBaseTransport | None,
    ) -> dict[str, object]:
        runs.append(warehouse_id)
        await release.wait()
        return {"monthUsd": 1.0, "totalUsd": 2.0, "asOf": None}

    monkeypatch.setattr(databricks, "_run_statement", held)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("RUNNING"))
        first = await client.post("/v1/ae/databricks/spend/refresh", json={})
        await asyncio.sleep(0)
        second = await client.post("/v1/ae/databricks/spend/refresh", json={})
        listed = await client.get("/v1/ae/databricks")
        release.set()
        await _settle()
    assert runs == [WID]
    assert first.json()["status"] == "reading"
    assert second.json()["status"] == "reading"
    assert listed.json()["spend"]["status"] == "reading"
    assert databricks.spend_view(databricks.credit())["status"] == "ok"


async def test_refresh_with_no_serverless_warehouse_is_a_409(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("RUNNING", serverless=False), _warehouse("DELETED", OTHER))
        statements = mock.post(STATEMENTS_URL).respond(200, json=_succeeded())
        response = await client.post("/v1/ae/databricks/spend/refresh", json={})
        assert not statements.called
    assert response.status_code == 409
    assert response.json()["error"]["code"] == databricks.CODE_NO_WAREHOUSE


async def test_a_refresh_whose_listing_fails_is_a_502_naming_no_secret(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(TOKEN_URL).respond(200, json=_token_answer())
        mock.get(LIST_URL).respond(500, json={"message": ECHO})
        response = await client.post("/v1/ae/databricks/spend/refresh", json={})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == databricks.CODE_FAILED
    _clean(response.text, caplog.text)


# ── spend: the state file ────────────────────────────────────────────────


async def test_the_reading_survives_a_restart_through_the_state_file(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("STOPPED"))
        mock.post(STATEMENTS_URL).respond(200, json=_succeeded())
        await client.post("/v1/ae/databricks/spend/refresh", json={})
        await _settle()
    saved = json.loads((tmp_path / "ae-databricks.json").read_text(encoding="utf-8"))
    assert saved["status"] == "ok"
    assert (saved["monthUsd"], saved["totalUsd"]) == (12.5, 40.25)
    assert saved["creditStart"] == "2030-01-01"
    assert saved["warehouseId"] == WID
    assert list(tmp_path.glob(".ae-databricks-*")) == []  # no temp file left behind
    # A restart: memory gone, the file read back, nothing asked of Databricks.
    databricks.reset()
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("STOPPED"))
        statements = mock.post(STATEMENTS_URL).respond(200, json=_succeeded())
        after = await client.get("/v1/ae/databricks")
        assert not statements.called
    spend = after.json()["spend"]
    assert spend["status"] == "ok"
    assert (spend["monthUsd"], spend["totalUsd"]) == (12.5, 40.25)
    assert spend["asOf"] == "2026-10-19T03:00:00Z"
    assert spend["readAt"] == saved["readAt"]


def test_an_unreadable_or_odd_state_file_reads_as_never(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "ae-databricks.json"
    path.write_text("{not json", encoding="utf-8")
    assert databricks.spend_view(databricks.credit(CREDIT_ENV))["status"] == "never"
    assert "ignoring unreadable" in caplog.text
    databricks.reset()
    _seed(path, status="reading", monthUsd="lots", warehouseId="../etc", readAt="yesterday")
    spend = databricks.spend_view(databricks.credit(CREDIT_ENV))
    assert (spend["status"], spend["monthUsd"], spend["warehouseId"], spend["readAt"]) == (
        "never",
        None,
        None,
        None,
    )


def test_totals_from_another_credit_start_are_not_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(
        tmp_path / "ae-databricks.json",
        status="ok",
        monthUsd=3.5,
        totalUsd=9.0,
        readAt="2026-10-18T08:00:00Z",
        creditStart="2030-01-01",
    )
    assert databricks.spend_view(databricks.credit(CREDIT_ENV))["totalUsd"] == 9.0
    moved = databricks.credit({**CREDIT_ENV, "AE_DATABRICKS_CREDIT_START": "2030-06-01"})
    spend = databricks.spend_view(moved)
    assert (spend["monthUsd"], spend["totalUsd"], spend["readAt"]) == (None, None, None)
    assert spend["creditStart"] == "2030-06-01"


async def test_the_shared_transport_is_reused_across_calls() -> None:
    first = databricks._shared_transport()
    assert databricks._shared_transport() is first


# ── spend: the breakdown (2026-10-22) ────────────────────────────────────


def test_the_breakdown_is_priced_and_joined_as_the_totals() -> None:
    sql = databricks.BREAKDOWN_SQL
    assert "p.pricing.effective_list.default" in sql
    assert "p.pricing.default" not in sql
    assert ":credit_start" in sql
    assert ":since" in sql
    assert "billing_origin_product" in sql
    # The same join, line for line (whitespace aside), as SPEND_SQL.
    start = databricks.SPEND_SQL.index("FROM system.billing.usage u")
    end = databricks.SPEND_SQL.index("WHERE u.usage_date")
    join = " ".join(databricks.SPEND_SQL[start:end].split())
    assert join in " ".join(sql.split())


async def test_the_breakdown_rides_the_reading_and_is_parsed(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    rows = _breakdown(
        ["product", "JOBS", "1.5"],
        ["product", "SQL", "6.25"],
        ["product", "DLT", "0"],
        ["product", "OTHER", "0.75"],
        ["product", "AI.FUNCTIONS", "0.25"],  # not a code the page shows: counted as OTHER
        ["product", "APPS", "-2"],  # a net credit: left out
        ["day", _days_back(0), "2.5"],
        ["day", _days_back(3), "4"],
        ["day", _days_back(30), "9"],  # outside the window: left out
    )
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("STOPPED"))
        posted = mock.post(STATEMENTS_URL).mock(
            side_effect=_routed(
                httpx.Response(200, json=_succeeded()), httpx.Response(200, json=rows)
            )
        )
        await client.post("/v1/ae/databricks/spend/refresh", json={})
        await _settle()
        after = await client.get("/v1/ae/databricks")
    second = json.loads(posted.calls[1].request.content)
    assert second["statement"] == databricks.BREAKDOWN_SQL
    assert second["warehouse_id"] == WID
    assert second["parameters"] == [
        {"name": "credit_start", "value": "2030-01-01", "type": "DATE"},
        {"name": "since", "value": _days_back(20), "type": "DATE"},
    ]
    spend = after.json()["spend"]
    assert (spend["status"], spend["monthUsd"], spend["note"]) == ("ok", 12.5, None)
    # Largest first; a product at $0 is left out.
    assert spend["products"] == [
        {"product": "SQL", "usd": 6.25},
        {"product": "JOBS", "usd": 1.5},
        {"product": "OTHER", "usd": 1.0},
    ]
    days = spend["days"]
    assert len(days) == databricks.BREAKDOWN_DAYS
    assert days[0] == {"date": _days_back(20), "usd": 0.0}
    assert days[-1] == {"date": _days_back(0), "usd": 2.5}
    assert days[-4] == {"date": _days_back(3), "usd": 4.0}
    assert sum(day["usd"] for day in days) == 6.5
    _clean(after.text, caplog.text)


async def test_no_spend_rows_is_an_empty_breakdown_of_zero_days(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("RUNNING"))
        # No rows comes back with no data_array at all.
        empty = {**_statement("SUCCEEDED"), "manifest": {"total_row_count": 0}}
        mock.post(STATEMENTS_URL).mock(
            side_effect=_routed(
                httpx.Response(200, json=_succeeded("0", "0")), httpx.Response(200, json=empty)
            )
        )
        await client.post("/v1/ae/databricks/spend/refresh", json={})
        await _settle()
    spend = databricks.spend_view(databricks.credit())
    assert spend["products"] == []
    assert [day["usd"] for day in spend["days"]] == [0.0] * databricks.BREAKDOWN_DAYS
    assert spend["note"] is None


@pytest.mark.parametrize(
    ("reply", "fragment"),
    [
        (
            httpx.Response(
                200,
                json=_statement("FAILED", error={"error_code": "INTERNAL_ERROR", "message": ECHO}),
            ),
            "The spend breakdown query failed: ",
        ),
        (httpx.Response(500, json={"message": ECHO}), "answered 500"),
        (httpx.Response(200, json=_breakdown(["region", "us-west-2", "1"])), "expected shape"),
        (httpx.Response(200, json=_breakdown(["day", "2030-06-01", "lots"])), "expected shape"),
        (RuntimeError(ECHO), "(RuntimeError)"),
    ],
)
async def test_a_failed_breakdown_keeps_the_totals_and_carries_a_note(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    reply: httpx.Response | Exception,
    fragment: str,
) -> None:
    _configure(monkeypatch)
    caplog.set_level(logging.DEBUG)
    _seed(
        tmp_path / "ae-databricks.json",
        status="ok",
        monthUsd=3.5,
        totalUsd=9.0,
        creditStart="2030-01-01",
        products=[{"product": "SQL", "usd": 3.5}],
        days=[{"date": "2030-06-01", "usd": 3.5}],
    )
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("RUNNING"))
        mock.post(STATEMENTS_URL).mock(
            side_effect=_routed(httpx.Response(200, json=_succeeded()), reply)
        )
        await client.post("/v1/ae/databricks/spend/refresh", json={})
        await _settle()
        after = await client.get("/v1/ae/databricks")
    spend = after.json()["spend"]
    # The totals are the new reading's, and the status is not an error.
    assert (spend["status"], spend["message"], spend["monthUsd"]) == ("ok", None, 12.5)
    # The old rows are not shown beside the newer totals.
    assert (spend["products"], spend["days"]) == (None, None)
    assert spend["note"].startswith("Breakdown unavailable")
    assert "The totals are complete." in spend["note"]
    assert fragment in spend["note"]
    saved = (tmp_path / "ae-databricks.json").read_text(encoding="utf-8")
    assert json.loads(saved)["breakdownNote"] == spend["note"]
    _clean(after.text, caplog.text, saved)


async def test_failed_totals_run_no_breakdown_and_keep_the_last_one(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch)
    products = [{"product": "SQL", "usd": 3.5}]
    _seed(
        tmp_path / "ae-databricks.json",
        status="ok",
        monthUsd=3.5,
        totalUsd=9.0,
        creditStart="2030-01-01",
        products=products,
    )
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("RUNNING"))
        statements = mock.post(STATEMENTS_URL).respond(500, json={"message": "boom"})
        await client.post("/v1/ae/databricks/spend/refresh", json={})
        await _settle()
        assert statements.call_count == 1
    spend = databricks.spend_view(databricks.credit())
    assert spend["status"] == "error"
    # The last good reading stays whole: its totals and its breakdown.
    assert (spend["monthUsd"], spend["products"]) == (3.5, products)


@pytest.mark.parametrize(("state", "reads"), [("STOPPED", False), ("RUNNING", True)])
async def test_the_breakdown_never_wakes_a_warehouse_on_its_own(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, state: str, reads: bool
) -> None:
    _configure(monkeypatch)
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse(state))
        one = mock.get(f"{LIST_URL}/{WID}").respond(200, json=_warehouse(state))
        statements = mock.post(STATEMENTS_URL).mock(
            side_effect=_routed(
                httpx.Response(200, json=_succeeded()), httpx.Response(200, json=_breakdown())
            )
        )
        await client.get("/v1/ae/databricks")
        await _settle()
        # GET on a stopped warehouse posts nothing; on a running one, the two
        # statements of one reading, after one uncached state check.
        assert [_sql_of(call.request) for call in statements.calls] == (
            [databricks.SPEND_SQL, databricks.BREAKDOWN_SQL] if reads else []
        )
        assert {json.loads(call.request.content)["warehouse_id"] for call in statements.calls} <= {
            WID
        }
        assert one.call_count == (1 if reads else 0)


async def test_the_breakdown_shares_the_deadline_and_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    monkeypatch.setattr(databricks, "_monotonic", clock)
    ws = databricks.Workspace(HOST, CLIENT_ID, CLIENT_SECRET)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/oidc/v1/token":
            return httpx.Response(200, json=_token_answer())
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={})
        if request.method == "POST" and _sql_of(request) == databricks.SPEND_SQL:
            # The totals took most of the reading's time.
            clock.now += databricks.READING_DEADLINE_S - 1
            return httpx.Response(200, json=_succeeded())
        clock.now += 2
        return httpx.Response(200, json=_statement("RUNNING"))

    await databricks._read_spend(
        ws, databricks.credit(CREDIT_ENV), WID, transport=httpx.MockTransport(handler)
    )
    assert seen.count(CANCEL) == 1
    spend = databricks.spend_view(databricks.credit(CREDIT_ENV))
    assert (spend["status"], spend["monthUsd"], spend["products"]) == ("ok", 12.5, None)
    assert "spend breakdown query did not finish within" in spend["note"]
    _clean(json.dumps(spend), caplog.text)


async def test_a_reading_cancelled_in_the_breakdown_saves_the_totals(tmp_path: Path) -> None:
    ws = databricks.Workspace(HOST, CLIENT_ID, CLIENT_SECRET)
    seen: list[str] = []
    polling = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/oidc/v1/token":
            return httpx.Response(200, json=_token_answer())
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={})
        if request.method == "POST":
            if _sql_of(request) == databricks.SPEND_SQL:
                return httpx.Response(200, json=_succeeded())
            return httpx.Response(200, json=_statement("PENDING"))
        polling.set()
        await asyncio.sleep(60)  # a poll that never answers
        return httpx.Response(200, json=_breakdown())

    task = asyncio.create_task(
        databricks._read_spend(
            ws, databricks.credit(CREDIT_ENV), WID, transport=httpx.MockTransport(handler)
        )
    )
    await polling.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert CANCEL in seen
    saved = json.loads((tmp_path / "ae-databricks.json").read_text(encoding="utf-8"))
    assert (saved["status"], saved["monthUsd"], saved["products"]) == ("ok", 12.5, None)
    assert "stopped before it finished" in saved["breakdownNote"]
    assert saved["attemptedAt"] is not None


async def test_the_breakdown_survives_a_restart_through_the_state_file(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(monkeypatch)
    rows = _breakdown(["product", "SQL", "6.25"], ["day", _days_back(1), "6.25"])
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("STOPPED"))
        mock.post(STATEMENTS_URL).mock(
            side_effect=_routed(
                httpx.Response(200, json=_succeeded()), httpx.Response(200, json=rows)
            )
        )
        await client.post("/v1/ae/databricks/spend/refresh", json={})
        await _settle()
    before = databricks.spend_view(databricks.credit())
    saved = json.loads((tmp_path / "ae-databricks.json").read_text(encoding="utf-8"))
    assert saved["products"] == [{"product": "SQL", "usd": 6.25}]
    assert len(saved["days"]) == databricks.BREAKDOWN_DAYS
    databricks.reset()
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("STOPPED"))
        statements = mock.post(STATEMENTS_URL).respond(200, json=_succeeded())
        after = await client.get("/v1/ae/databricks")
        assert not statements.called
    spend = after.json()["spend"]
    assert (spend["products"], spend["days"], spend["note"]) == (
        before["products"],
        before["days"],
        None,
    )


def test_an_odd_saved_breakdown_reads_as_none(tmp_path: Path) -> None:
    _seed(
        tmp_path / "ae-databricks.json",
        status="ok",
        products=[{"product": "SQL", "usd": "lots"}],
        days=[{"date": "../etc", "usd": 1}],
        breakdownNote=7,
    )
    spend = databricks.spend_view(databricks.credit(CREDIT_ENV))
    assert (spend["products"], spend["days"], spend["note"]) == (None, None, None)
    databricks.reset()
    _seed(
        tmp_path / "ae-databricks.json", status="ok", days=[{"date": "2030-06-01", "usd": 1}] * 22
    )
    assert databricks.spend_view(databricks.credit(CREDIT_ENV))["days"] is None


async def test_a_credit_change_drops_the_breakdown(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed(
        tmp_path / "ae-databricks.json",
        status="ok",
        monthUsd=3.5,
        totalUsd=9.0,
        creditStart="2030-01-01",
        products=[{"product": "SQL", "usd": 3.5}],
        days=[{"date": "2030-06-01", "usd": 3.5}],
        breakdownNote="Breakdown unavailable: old. The totals are complete.",
    )
    assert databricks.spend_view(databricks.credit(CREDIT_ENV))["products"] is not None
    moved = databricks.credit({**CREDIT_ENV, "AE_DATABRICKS_CREDIT_START": "2030-06-01"})
    spend = databricks.spend_view(moved)
    assert (spend["products"], spend["days"], spend["note"]) == (None, None, None)
    # A reading under the new credit whose breakdown fails leaves none from the old one.
    _configure(monkeypatch)
    monkeypatch.setenv("AE_DATABRICKS_CREDIT_START", "2030-06-01")
    with respx.mock(assert_all_called=False) as mock:
        _mock(mock, _warehouse("RUNNING"))
        mock.post(STATEMENTS_URL).mock(
            side_effect=_routed(
                httpx.Response(200, json=_succeeded()), httpx.Response(500, json={"message": "x"})
            )
        )
        await client.post("/v1/ae/databricks/spend/refresh", json={})
        await _settle()
    state = databricks._state()
    assert (state["creditStart"], state["products"], state["days"]) == ("2030-06-01", None, None)


async def test_a_reading_out_of_time_posts_no_breakdown(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    monkeypatch.setattr(databricks, "_monotonic", clock)
    ws = databricks.Workspace(HOST, CLIENT_ID, CLIENT_SECRET)
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oidc/v1/token":
            return httpx.Response(200, json=_token_answer())
        posted.append(_sql_of(request))
        # The totals used more than the whole reading's time.
        clock.now += databricks.READING_DEADLINE_S + 1
        return httpx.Response(200, json=_succeeded())

    await databricks._read_spend(
        ws, databricks.credit(CREDIT_ENV), WID, transport=httpx.MockTransport(handler)
    )
    assert posted == [databricks.SPEND_SQL]
    spend = databricks.spend_view(databricks.credit(CREDIT_ENV))
    assert (spend["status"], spend["monthUsd"], spend["products"]) == ("ok", 12.5, None)
    assert "was not started" in spend["note"]


async def test_new_totals_never_show_beside_the_last_readings_rows(tmp_path: Path) -> None:
    _seed(
        tmp_path / "ae-databricks.json",
        status="ok",
        monthUsd=1.0,
        totalUsd=1.0,
        creditStart="2030-01-01",
        products=[{"product": "SQL", "usd": 1.0}],
        days=[{"date": "2030-06-01", "usd": 1.0}],
    )
    ws = databricks.Workspace(HOST, CLIENT_ID, CLIENT_SECRET)
    polling = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oidc/v1/token":
            return httpx.Response(200, json=_token_answer())
        if request.method == "POST":
            if _sql_of(request) == databricks.SPEND_SQL:
                return httpx.Response(200, json=_succeeded("100", "100"))
            return httpx.Response(200, json=_statement("PENDING"))
        polling.set()
        await release.wait()
        return httpx.Response(200, json=_breakdown(["product", "SQL", "100"]))

    task = asyncio.create_task(
        databricks._read_spend(
            ws, databricks.credit(CREDIT_ENV), WID, transport=httpx.MockTransport(handler)
        )
    )
    await polling.wait()
    # Mid-breakdown: the new totals, no rows, and the totals already on disk.
    during = databricks.spend_view(databricks.credit(CREDIT_ENV))
    assert (during["monthUsd"], during["products"], during["days"]) == (100.0, None, None)
    saved = json.loads((tmp_path / "ae-databricks.json").read_text(encoding="utf-8"))
    assert (saved["monthUsd"], saved["products"]) == (100.0, None)
    release.set()
    await task
    after = databricks.spend_view(databricks.credit(CREDIT_ENV))
    assert after["products"] == [{"product": "SQL", "usd": 100.0}]
