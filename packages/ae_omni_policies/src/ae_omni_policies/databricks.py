"""Databricks warehouses and spend for the Admin Center's Databricks section.

Added 2026-10-19, from Reid's Admin Center roadmap: after the Fabric
capacities, Databricks compute and a spend strip, built on the AWS MVP
account and its prepaid credit. The workspace
is serverless only, so the section lists its SQL warehouses with Start and
Stop and reads what the credit has paid for so far. The account console
shows one credit with an expiry date, while Reid believes it resets
monthly, so the strip shows both this month's spend and the spend since the
credit started, each against the credit; the cadence setting only picks which
one is the headline.

Configuration, from the environment:

- ``AE_DATABRICKS_HOST``: the workspace URL, ``https://`` and no trailing
  slash (a trailing one is dropped). Not a secret; the page links to it.
- ``AE_DATABRICKS_CLIENT_ID`` and ``AE_DATABRICKS_CLIENT_SECRET``: a service
  principal's OAuth M2M credential. Secrets: ``deploy/pull-secrets.sh`` writes
  them from Key Vault ``databricks-mvp-client-id`` and
  ``databricks-mvp-client-secret``. The token is asked for with ``scope=sql``
  only, which covers ``/api/2.0/sql/warehouses*`` and
  ``/api/2.0/sql/statements*`` and nothing else (not Unity Catalog's APIs), so
  a leaked token could at most start a warehouse or run a query.
- ``AE_DATABRICKS_CREDIT_USD`` (default 200), ``AE_DATABRICKS_CREDIT_CADENCE``
  (``monthly`` or ``once``, default ``once``), ``AE_DATABRICKS_CREDIT_START``
  and ``AE_DATABRICKS_CREDIT_END`` (ISO dates, default 2026-08-19 and
  2027-08-19). A value that does not read falls back to its default.
- ``AE_DATABRICKS_STATE_FILE`` (default ``/data/ae-databricks.json``): the
  last spend reading, written atomically the way ``limits.py`` writes its
  state, so a restart shows it without waking a warehouse.

Configured means the host and both secrets are present. The client id, the
secret and every token are never logged, returned, or put in an error
message. The traps are fabric.py's: httpx's ``AsyncClient`` logs each request,
so calls go straight to the transport (:func:`_send`); an exception's text or
an upstream body can echo a credential, so errors name the exception's class
only, raised ``from None``, and any body text is scrubbed of every secret
first (:func:`_scrub`), the Basic header's base64 and the tokens held
included.

Cost guard: ``GET`` never starts anything. A spend reading runs a query on a
warehouse, and a query on a stopped serverless warehouse starts it and bills
until it auto stops, so ``GET`` starts a reading only when the chosen
warehouse (a serverless one, RUNNING preferred, then STARTING) is already
RUNNING, auto stops in under :data:`SPEND_STALE_S` (so periodic reads can never
hold it awake), and the last attempt is older than that (or there was none);
the background task then reads the warehouse's state again, uncached, and
posts the query only if it is still RUNNING. Otherwise only
``POST /v1/ae/databricks/spend/refresh`` reads, and the page labels that
button as waking the warehouse.

A reading runs in a background task (:func:`_read_spend`), because the
extension host gives one call ten seconds and a cold start plus the query can
take longer. It posts the statement with a 30 s wait and polls until it
settles, bounded by :data:`READING_DEADLINE_S`, then cancels it; one reading at
a time, and a refresh while one runs answers its "reading" state. A query the
service principal may not run (no ``USE CATALOG`` on ``system``, and so on)
reads as ``no_access`` with the grants to ask for.

The breakdown (2026-10-22): the same reading runs a second statement,
:data:`BREAKDOWN_SQL`, right after the totals succeed: this month's spend by
product (``billing_origin_product``) and the last :data:`BREAKDOWN_DAYS` days'
spend by day, priced exactly as :data:`SPEND_SQL` is (the same join, the
effective list price, the same credit start bound). It never wakes a warehouse
on its own: it rides a reading the cost guard already allowed, on the
warehouse the totals query just used, and shares that reading's
:data:`READING_DEADLINE_S` deadline and its cancel on any exit. It is a bonus on
top of the totals, the way azure_cost.py treats its spike check: when it fails
the totals stay, the rows are dropped rather than shown stale (the last
reading's rows go the moment new totals land, and the totals are saved before
the breakdown starts), and the
``spend`` object carries a ``note``, not an error status. The rows live in the
state file beside the totals, and a credit start change hides them with the
totals.

Caches: the token in process, refreshed :data:`TOKEN_REFRESH_MARGIN_S` before
it expires, behind one lock; the warehouse listing for :data:`LIST_TTL_S`,
dropped after a Start or a Stop (a listing in flight then does not write its
stale answer back); the spend reading in memory and in the state file.
Request-path work (token, listing, Start or Stop) shares one
:data:`REQUEST_TIMEOUT_S` deadline, under the host's ten seconds. The transport
is one per event loop for the life of the process, as in fabric.py.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, quote_plus, urlencode

import httpx

from ae_omni_policies import __version__

_logger = logging.getLogger(__name__)

HOST_ENV = "AE_DATABRICKS_HOST"
CLIENT_ID_ENV = "AE_DATABRICKS_CLIENT_ID"
CLIENT_SECRET_ENV = "AE_DATABRICKS_CLIENT_SECRET"
CREDIT_USD_ENV = "AE_DATABRICKS_CREDIT_USD"
CREDIT_CADENCE_ENV = "AE_DATABRICKS_CREDIT_CADENCE"
CREDIT_START_ENV = "AE_DATABRICKS_CREDIT_START"
CREDIT_END_ENV = "AE_DATABRICKS_CREDIT_END"
STATE_FILE_ENV = "AE_DATABRICKS_STATE_FILE"
DEFAULT_STATE_FILE = "/data/ae-databricks.json"
DEFAULT_CREDIT_USD = 200.0
DEFAULT_CREDIT_START = date(2026, 8, 19)
DEFAULT_CREDIT_END = date(2027, 8, 19)

# Under the SDK's ten-second budget for one host call, token and listing (or
# token, Start and the row read back) together.
REQUEST_TIMEOUT_S = 8.0
LIST_TTL_S = 10.0
TOKEN_REFRESH_MARGIN_S = 300.0
# The statement POST waits up to 30 s for the answer (wait_timeout), so its own
# request bound sits above that.
STATEMENT_WAIT = "30s"
STATEMENT_REQUEST_TIMEOUT_S = 45.0
POLL_INTERVAL_S = 5.0
READING_DEADLINE_S = 240.0
CANCEL_TIMEOUT_S = 5.0
# GET starts a reading on a RUNNING warehouse only when the last attempt is older.
SPEND_STALE_S = 30 * 60.0
USER_AGENT = f"omnigent-ae/{__version__} (+https://github.com/analyticendeavors/omnigent-ae)"

# An https origin: a dotted host name and an optional port, nothing after it.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST = re.compile(rf"^https://{_LABEL}(?:\.{_LABEL})+(?::\d{{1,5}})?$")
# A warehouse id is 16 hex characters; the router 404s anything else unasked.
WAREHOUSE_ID = re.compile(r"^[0-9a-f]{16}$")
_STATEMENT_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")
_SIZE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{0,23}$")
_NAME_MAX = 100
# Error text from a Databricks body, after scrubbing, is cut to this.
_ERROR_TEXT_MAX = 200

States = ("RUNNING", "STARTING", "STOPPED", "STOPPING", "DELETING", "DELETED")
# A state outside the documented six reads as this, so nothing odd is echoed.
UNKNOWN_STATE = "UNKNOWN"
# Which state to show when the row cannot be read back after an action.
_AFTER = {"start": "STARTING", "stop": "STOPPING"}
_UNUSABLE = ("DELETING", "DELETED")
_RUNNING_STATEMENT = ("PENDING", "RUNNING")

Cadence = Literal["monthly", "once"]
Action = Literal["start", "stop"]
SpendStatus = Literal["ok", "reading", "never", "no_access", "error"]

CODE_FAILED = "databricks_failed"
CODE_TIMEOUT = "databricks_timeout"
CODE_AUTH = "databricks_auth"
CODE_NOT_CONFIGURED = "databricks_not_configured"
CODE_NOT_FOUND = "databricks_not_found"
CODE_NO_WAREHOUSE = "databricks_no_warehouse"
CODE_NO_ACCESS = "databricks_no_access"

HOW_TO_ENABLE = (
    "Databricks is not configured: the server has no workspace credential. "
    "Set AE_DATABRICKS_CLIENT_ID and AE_DATABRICKS_CLIENT_SECRET in the server's "
    "environment to the service principal's OAuth client id and secret, set "
    "AE_DATABRICKS_HOST to the workspace URL, then restart the stack "
    '(deploy/README.md, "The Admin Center\'s Databricks secrets (optional)").'
)
BAD_HOST = (
    "Databricks is not configured: AE_DATABRICKS_HOST is not an https workspace URL "
    "(https://<workspace>.cloud.databricks.com, no path). Fix it in the server's "
    "environment and restart the stack."
)
NO_ACCESS = (
    "The service principal cannot read the billing tables. An account or metastore admin "
    "grants it, in Catalog Explorer, USE CATALOG on catalog system, then USE SCHEMA and "
    "SELECT on schema system.billing; then Refresh."
)
# What a statement's error says when the grants above are missing.
_NO_ACCESS_CODES = ("PERMISSION_DENIED", "INSUFFICIENT_PERMISSIONS")
_NO_ACCESS_TEXT = re.compile(
    r"USE CATALOG|USE SCHEMA|PERMISSION_DENIED|INSUFFICIENT_PERMISSIONS"
    r"|does not have \S+ (?:privilege|permission)",
    re.IGNORECASE,
)

# The spend query (UTC). The credit is drawn at the effective list price
# (pricing.effective_list.default: the list price with any promotion applied,
# the rate Databricks bills at), not the plain list price. The credit start is
# a named parameter, never formatted into the text.
SPEND_SQL = """\
SELECT
  SUM(CASE WHEN u.usage_date >= date_trunc('MONTH', current_date())
           THEN u.usage_quantity * p.pricing.effective_list.default ELSE 0 END) AS month_usd,
  SUM(u.usage_quantity * p.pricing.effective_list.default) AS total_usd,
  MAX(u.usage_end_time) AS as_of
FROM system.billing.usage u
JOIN system.billing.list_prices p
  ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
 AND u.usage_end_time >= p.price_start_time
 AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
WHERE u.usage_date >= :credit_start"""

# The breakdown (module note): one row per product this month and one per day
# since :since, as (kind, item, usd). Priced and joined as SPEND_SQL, and bound
# by the credit start too, so the products add up to its month_usd. The month
# start comes from the warehouse's clock, as in SPEND_SQL; :since from ours.
BREAKDOWN_SQL = """\
WITH priced AS (
  SELECT
    u.usage_date,
    COALESCE(NULLIF(TRIM(u.billing_origin_product), ''), 'OTHER') AS product,
    u.usage_quantity * p.pricing.effective_list.default AS usd
  FROM system.billing.usage u
  JOIN system.billing.list_prices p
    ON u.sku_name = p.sku_name AND u.cloud = p.cloud AND u.usage_unit = p.usage_unit
   AND u.usage_end_time >= p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time < p.price_end_time)
  WHERE u.usage_date >= :credit_start
    AND u.usage_date >= LEAST(:since, CAST(date_trunc('MONTH', current_date()) AS DATE))
)
SELECT 'product' AS kind, product AS item, SUM(usd) AS usd
FROM priced
WHERE usage_date >= date_trunc('MONTH', current_date())
GROUP BY product
UNION ALL
SELECT 'day' AS kind, CAST(usage_date AS STRING) AS item, SUM(usd) AS usd
FROM priced
WHERE usage_date >= :since
GROUP BY usage_date"""
BREAKDOWN_DAYS = 21
# The most product rows kept: the docs list about 35 codes.
PRODUCTS_MAX = 50
_PRODUCT = re.compile(r"^[A-Za-z0-9_ -]{1,64}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Workspace:
    """The workspace and its service principal; the secrets stay out of ``repr``."""

    host: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def basic(self) -> str:
        """The HTTP Basic credential for the token endpoint (itself a secret)."""
        pair = f"{self.client_id}:{self.client_secret}".encode()
        return base64.b64encode(pair).decode("ascii")

    def key(self) -> str:
        """A digest naming this credential, so the token cache follows a change."""
        raw = f"{self.host}\n{self.client_id}\n{self.client_secret}".encode()
        return hashlib.sha256(raw).hexdigest()

    def secrets(self) -> list[str]:
        """Every string an answer must never contain, longest first.

        The client id, the secret, the Basic credential, every token held and
        the last few dropped (:data:`_retired`),
        each also percent-encoded (``quote`` and ``quote_plus``), because an
        echoed form body or URL carries that spelling.
        """
        found = [self.client_id, self.client_secret, self.basic(), *_held_tokens()]
        for item in list(found):
            found += [quote(item, safe=""), quote(item), quote_plus(item)]
        return sorted({item for item in found if item}, key=len, reverse=True)


@dataclass(frozen=True)
class Credit:
    """The credit the strip measures against (module note)."""

    usd: float
    cadence: Cadence
    start: date
    end: date


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _host(raw: str) -> str | None:
    host = raw.strip().rstrip("/")
    return host if _HOST.match(host) else None


def workspace(environ: Mapping[str, str] | None = None) -> Workspace | None:
    """The configured workspace, or ``None`` without a good host and both secrets."""
    env = _env(environ)
    host = _host(env.get(HOST_ENV) or "")
    client_id = (env.get(CLIENT_ID_ENV) or "").strip()
    secret = (env.get(CLIENT_SECRET_ENV) or "").strip()
    if host is None or not client_id or not secret:
        return None
    return Workspace(host=host, client_id=client_id, client_secret=secret)


def not_configured_note(environ: Mapping[str, str] | None = None) -> str:
    """Why :func:`workspace` is ``None``: a bad host, or the how-to sentence."""
    env = _env(environ)
    raw = (env.get(HOST_ENV) or "").strip()
    return BAD_HOST if raw and _host(raw) is None else HOW_TO_ENABLE


def _date(raw: str | None, default: date) -> date:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        return default


def credit(environ: Mapping[str, str] | None = None) -> Credit:
    """The credit settings, each falling back to its default when it does not read."""
    env = _env(environ)
    try:
        usd = float((env.get(CREDIT_USD_ENV) or "").strip())
    except ValueError:
        usd = DEFAULT_CREDIT_USD
    if not math.isfinite(usd) or usd <= 0:
        usd = DEFAULT_CREDIT_USD
    cadence = (env.get(CREDIT_CADENCE_ENV) or "").strip().lower()
    return Credit(
        usd=usd,
        cadence="monthly" if cadence == "monthly" else "once",
        start=_date(env.get(CREDIT_START_ENV), DEFAULT_CREDIT_START),
        end=_date(env.get(CREDIT_END_ENV), DEFAULT_CREDIT_END),
    )


class DatabricksError(Exception):
    """A Databricks call that failed; the message never carries a secret.

    ``upstream`` is Databricks' own ``error_code`` when the body had one (a
    fixed word such as ``PERMISSION_DENIED``), ``detail`` its scrubbed text.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = CODE_FAILED,
        status: int = 502,
        upstream: str | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.upstream = upstream
        self.detail = detail


def _scrub(text: str, ws: Workspace) -> str:
    for secret in ws.secrets():
        text = text.replace(secret, "[redacted]")
    return text


async def _send(transport: httpx.AsyncBaseTransport, request: httpx.Request) -> tuple[int, bytes]:
    """One request through *transport*, with no client in between (no request log)."""
    response = await transport.handle_async_request(request)
    try:
        body = await response.aread()
    finally:
        await response.aclose()
    return response.status_code, body


_transport: httpx.AsyncHTTPTransport | None = None
_transport_loop: asyncio.AbstractEventLoop | None = None


def _shared_transport() -> httpx.AsyncHTTPTransport:
    """The process-lifetime transport (module note); rebuilt on a new event loop."""
    global _transport, _transport_loop
    loop = asyncio.get_running_loop()
    if _transport is None or _transport_loop is not loop:
        _transport = httpx.AsyncHTTPTransport()
        _transport_loop = loop
    return _transport


def _monotonic() -> float:
    return time.monotonic()


def _now() -> datetime:
    return datetime.now(UTC)


def iso(moment: datetime | None) -> str | None:
    """*moment* as ``2026-10-19T08:00:00Z``, or ``None``."""
    return None if moment is None else moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _instant(value: object) -> datetime | None:
    """An ISO 8601 time (``Z``, an offset, or none for UTC) as aware UTC, or ``None``."""
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", value.strip().replace(" ", "T", 1))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _body_error(body: bytes, ws: Workspace) -> tuple[str | None, str | None]:
    """``(error_code, scrubbed message)`` from a Databricks or token-endpoint body."""
    try:
        payload = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    upstream = payload.get("error_code")
    if not isinstance(upstream, str) or not re.fullmatch(r"[A-Z_]{1,64}", upstream):
        upstream = None
    text = payload.get("message") or payload.get("error_description") or payload.get("error")
    if not isinstance(text, str) or not text.strip():
        return upstream, None
    return upstream, _scrub(text.strip(), ws)[:_ERROR_TEXT_MAX]


# ── the token ─────────────────────────────────────────────────────────────

# Workspace.key() -> (access token, refresh after, monotonic clock).
_tokens: dict[str, tuple[str, float]] = {}
_token_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
# The last few tokens dropped or replaced, still scrubbed: a late error body can
# echo a request sent with one of them.
_retired: collections.deque[str] = collections.deque(maxlen=4)


def _held_tokens() -> list[str]:
    return [token for token, _ in _tokens.values()] + list(_retired)


def _retire(key: str) -> None:
    held = _tokens.pop(key, None)
    if held is not None:
        _retired.append(held[0])


def _token_lock(key: str) -> asyncio.Lock:
    """The credential's token lock; rebuilt on a new event loop, like the transport."""
    loop = asyncio.get_running_loop()
    held = _token_locks.get(key)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _token_locks[key] = held
    return held[1]


def drop_token(ws: Workspace | None = None) -> None:
    """Forget the cached token for *ws* (after a 401), or every one."""
    if ws is None:
        for key in list(_tokens):
            _retire(key)
    else:
        _retire(ws.key())


async def _token(ws: Workspace, transport: httpx.AsyncBaseTransport) -> str:
    """A ``scope=sql`` token, from the cache until five minutes before it expires."""
    key = ws.key()
    async with _token_lock(key):
        held = _tokens.get(key)
        if held is not None and _monotonic() < held[1]:
            return held[0]
        request = httpx.Request(
            "POST",
            f"{ws.host}/oidc/v1/token",
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {ws.basic()}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            content=urlencode({"grant_type": "client_credentials", "scope": "sql"}).encode(),
            extensions={"timeout": httpx.Timeout(REQUEST_TIMEOUT_S).as_dict()},
        )
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                status, body = await _send(transport, request)
        except (httpx.TimeoutException, TimeoutError):
            raise DatabricksError(
                f"The Databricks token endpoint did not answer within {REQUEST_TIMEOUT_S:g} s.",
                code=CODE_TIMEOUT,
                status=504,
            ) from None
        except httpx.HTTPError as exc:
            raise DatabricksError(
                f"The Databricks token endpoint could not be reached ({type(exc).__name__})."
            ) from None
        if status != 200:
            _upstream, detail = _body_error(body, ws)
            if status in (400, 401, 403):
                message = (
                    f"Databricks refused the service principal's credential ({status})"
                    + (f": {detail.rstrip('.')}." if detail else ".")
                    + " Check the server's AE_DATABRICKS_CLIENT_ID and "
                    "AE_DATABRICKS_CLIENT_SECRET and restart the stack."
                )
                raise DatabricksError(message, code=CODE_AUTH)
            raise DatabricksError(
                f"The Databricks token endpoint answered {status}"
                + (f": {detail}" if detail else ".")
            )
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise DatabricksError("The Databricks token answer was not the expected JSON.")
        expires = payload.get("expires_in")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= 0:
            expires = 3600
        _retire(key)
        _tokens[key] = (token, _monotonic() + float(expires) - TOKEN_REFRESH_MARGIN_S)
        return token


async def _api(
    ws: Workspace,
    method: Literal["GET", "POST"],
    path: str,
    *,
    what: str,
    body: Mapping[str, Any] | None = None,
    timeout: float = REQUEST_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """One workspace API call with the token; its JSON object, or :class:`DatabricksError`."""
    sender = transport if transport is not None else _shared_transport()
    token = await _token(ws, sender)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    }
    content = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        content = json.dumps(body).encode()
    request = httpx.Request(
        method,
        f"{ws.host}{path}",
        headers=headers,
        content=content,
        extensions={"timeout": httpx.Timeout(timeout).as_dict()},
    )
    try:
        # One deadline over the whole exchange: httpx's own timeout is per phase.
        async with asyncio.timeout(timeout):
            status, raw = await _send(sender, request)
    except (httpx.TimeoutException, TimeoutError):
        raise DatabricksError(
            f"Databricks did not answer the {what} call within {timeout:g} s.",
            code=CODE_TIMEOUT,
            status=504,
        ) from None
    except httpx.HTTPError as exc:
        raise DatabricksError(
            f"Databricks could not be reached for the {what} call ({type(exc).__name__})."
        ) from None
    if status != 200:
        upstream, detail = _body_error(raw, ws)
        suffix = f": {detail}" if detail else "."
        if status == 401:
            # The next call asks for a new token; this one stays scrubbed (_retired).
            drop_token(ws)
            message = f"Databricks refused the token (401) on the {what} call{suffix}"
            raise DatabricksError(message, code=CODE_AUTH, upstream=upstream, detail=detail)
        if status == 404:
            raise DatabricksError(
                f"Databricks has no such resource for the {what} call (404){suffix}",
                code=CODE_NOT_FOUND,
                status=404,
                upstream=upstream,
                detail=detail,
            )
        raise DatabricksError(
            f"Databricks answered {status} to the {what} call{suffix}",
            upstream=upstream,
            detail=detail,
        )
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise DatabricksError(f"Databricks' {what} answer was not the expected JSON.")
    return payload


# ── warehouses ────────────────────────────────────────────────────────────


def _row(ws: Workspace, raw: object) -> dict[str, Any] | None:
    """One warehouse as ``GET /v1/ae/databricks`` shows it, or ``None`` for an odd id."""
    if not isinstance(raw, dict):
        return None
    warehouse_id = raw.get("id")
    if not isinstance(warehouse_id, str) or not WAREHOUSE_ID.match(warehouse_id):
        return None
    name = raw.get("name")
    name = name.strip()[:_NAME_MAX] if isinstance(name, str) and name.strip() else warehouse_id
    state = raw.get("state")
    size = raw.get("cluster_size")
    stop = raw.get("auto_stop_mins")
    return {
        "id": warehouse_id,
        "name": name,
        "state": state if state in States else UNKNOWN_STATE,
        "size": size.strip() if isinstance(size, str) and _SIZE.match(size.strip()) else "",
        # type() is int, not isinstance: a JSON true is not a number of minutes.
        "autoStopMins": stop if type(stop) is int and 0 <= stop <= 10_000 else None,
        "serverless": raw.get("enable_serverless_compute") is True,
        "url": f"{ws.host}/sql/warehouses/{warehouse_id}",
    }


# host -> (expires at, rows), monotonic clock.
_list_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
# host -> how many times forget() ran; a listing that began before a bump is stale.
_generation: dict[str, int] = {}


def forget(ws: Workspace | None = None) -> None:
    """Drop the cached listing (after a Start or a Stop), or every one."""
    if ws is None:
        for host in set(_list_cache) | set(_generation):
            _generation[host] = _generation.get(host, 0) + 1
        _list_cache.clear()
    else:
        _generation[ws.host] = _generation.get(ws.host, 0) + 1
        _list_cache.pop(ws.host, None)


async def warehouses(
    ws: Workspace, *, transport: httpx.AsyncBaseTransport | None = None
) -> list[dict[str, Any]]:
    """Every warehouse in the workspace, cached :data:`LIST_TTL_S`; zero or more."""
    held = _list_cache.get(ws.host)
    if held is not None and held[0] > _monotonic():
        return [dict(row) for row in held[1]]
    generation = _generation.get(ws.host, 0)
    payload = await _api(
        ws, "GET", "/api/2.0/sql/warehouses", what="warehouse listing", transport=transport
    )
    found = payload.get("warehouses")
    # Databricks leaves the key out when the workspace has none.
    listed = found if isinstance(found, list) else []
    rows = [row for row in (_row(ws, raw) for raw in listed) if row is not None]
    # A Start or a Stop landed while this listing was out: its answer predates it.
    if _generation.get(ws.host, 0) == generation:
        _list_cache[ws.host] = (_monotonic() + LIST_TTL_S, rows)
    return [dict(row) for row in rows]


def _reading_warehouse(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Where a spend reading runs: a serverless warehouse not being deleted.

    One already RUNNING first, then one STARTING, then the first listed, so a
    reading lands on compute that is billing anyway before it wakes another.
    """
    usable = [row for row in rows if row["serverless"] and row["state"] not in _UNUSABLE]
    for wanted in ("RUNNING", "STARTING"):
        for row in usable:
            if row["state"] == wanted:
                return row
    return usable[0] if usable else None


async def act(
    ws: Workspace,
    warehouse_id: str,
    action: Action,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Start or stop one warehouse; never retried. Answers its row after the call.

    The listing, the token and the call share one :data:`REQUEST_TIMEOUT_S`
    deadline. The row is read back within what is left of it; when that read
    fails the listed row is answered with ``STARTING`` or ``STOPPING``, since
    the action itself went through.

    :raises DatabricksError: 404 for an id the listing does not have, 504 on
        the deadline (the action may still land), 502 otherwise.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REQUEST_TIMEOUT_S
    listed: dict[str, Any] | None = None
    sent = False
    try:
        async with asyncio.timeout_at(deadline):
            rows = await warehouses(ws, transport=transport)
            listed = next((row for row in rows if row["id"] == warehouse_id), None)
            if listed is None:
                raise DatabricksError(
                    f"No Databricks warehouse has the id {warehouse_id} here; the page lists "
                    "the ones the workspace has.",
                    code=CODE_NOT_FOUND,
                    status=404,
                )
            # The token first, so a slow token endpoint is not mistaken for a
            # call that may have landed; _api then finds it cached.
            await _token(ws, transport if transport is not None else _shared_transport())
            sent = True
            await _api(
                ws,
                "POST",
                f"/api/2.0/sql/warehouses/{warehouse_id}/{action}",
                what=f"warehouse {action}",
                body={},
                transport=transport,
            )
    except (TimeoutError, DatabricksError) as exc:
        if isinstance(exc, DatabricksError) and exc.code != CODE_TIMEOUT:
            raise
        # Once the start or stop call was on its way it may have landed: say so,
        # so nobody starts the warehouse twice over a slow answer.
        hint = (
            "; it may still be under way, so look again in a minute before trying again"
            if sent
            else ""
        )
        raise DatabricksError(
            f"Databricks did not answer the warehouse {action} call within "
            f"{REQUEST_TIMEOUT_S:g} s{hint}.",
            code=CODE_TIMEOUT,
            status=504,
        ) from None
    finally:
        if listed is not None:
            forget(ws)
    try:
        async with asyncio.timeout_at(deadline):
            payload = await _api(
                ws,
                "GET",
                f"/api/2.0/sql/warehouses/{warehouse_id}",
                what="warehouse status",
                transport=transport,
            )
        row = _row(ws, payload)
    except (DatabricksError, TimeoutError):
        row = None
    if row is None or row["id"] != warehouse_id:
        row = {**listed, "state": _AFTER[action]}
    return row


# ── spend ─────────────────────────────────────────────────────────────────

_MISSING = object()
# The last reading's outcome; the state file mirrors it (module note).
_spend: dict[str, Any] = {}
_spend_path: Path | None | object = _MISSING
_reading: asyncio.Task[None] | None = None


def _blank_spend() -> dict[str, Any]:
    return {
        "status": "never",
        "message": None,
        "readAt": None,
        "asOf": None,
        "monthUsd": None,
        "totalUsd": None,
        "warehouseId": None,
        "attemptedAt": None,
        "creditStart": None,
        "products": None,
        "days": None,
        "breakdownNote": None,
    }


def _state_path() -> Path | None:
    raw = os.environ.get(STATE_FILE_ENV)
    raw = DEFAULT_STATE_FILE if raw is None else raw.strip()
    return Path(raw) if raw else None


def _money(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _products(raw: object) -> list[dict[str, Any]] | None:
    """Saved product rows, each ``{"product", "usd"}``; None when the field does not read."""
    if not isinstance(raw, list) or len(raw) > PRODUCTS_MAX:
        return None
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        product, usd = item.get("product"), _money(item.get("usd"))
        if not isinstance(product, str) or not _PRODUCT.match(product) or usd is None:
            return None
        rows.append({"product": product, "usd": usd})
    return rows


def _days(raw: object) -> list[dict[str, Any]] | None:
    """Saved day rows, each ``{"date", "usd"}``; None when the field does not read."""
    if not isinstance(raw, list) or len(raw) > BREAKDOWN_DAYS:
        return None
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        day, usd = item.get("date"), _money(item.get("usd"))
        if not isinstance(day, str) or not _DAY.match(day) or usd is None:
            return None
        rows.append({"date": day, "usd": usd})
    return rows


def _load(path: Path | None) -> dict[str, Any]:
    state = _blank_spend()
    if path is None:
        return state
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return state
    except (OSError, ValueError) as exc:
        _logger.warning("ae databricks: ignoring unreadable %s (%s)", path, type(exc).__name__)
        return state
    if not isinstance(raw, dict):
        return state
    status = raw.get("status")
    if status in ("ok", "no_access", "error"):
        state["status"] = status
    message = raw.get("message")
    state["message"] = message[: _ERROR_TEXT_MAX * 3] if isinstance(message, str) else None
    for key in ("readAt", "asOf", "attemptedAt"):
        state[key] = _instant(raw.get(key))
    for key in ("monthUsd", "totalUsd"):
        state[key] = _money(raw.get(key))
    warehouse_id = raw.get("warehouseId")
    if isinstance(warehouse_id, str) and WAREHOUSE_ID.match(warehouse_id):
        state["warehouseId"] = warehouse_id
    start = raw.get("creditStart")
    state["creditStart"] = start if isinstance(start, str) and len(start) == 10 else None
    state["products"] = _products(raw.get("products"))
    state["days"] = _days(raw.get("days"))
    note = raw.get("breakdownNote")
    state["breakdownNote"] = note[: _ERROR_TEXT_MAX * 3] if isinstance(note, str) else None
    return state


def _save(path: Path | None, state: Mapping[str, Any]) -> None:
    """Write *state* atomically (a temp file, then ``os.replace``), like limits.py."""
    if path is None:
        return
    body = {
        key: iso(value) if isinstance(value, datetime) else value for key, value in state.items()
    }
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ae-databricks-", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as exc:
        _logger.warning("ae databricks: could not write %s (%s)", path, type(exc).__name__)
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _state() -> dict[str, Any]:
    """The spend state, loaded from the state file on first use (or when it moves)."""
    global _spend, _spend_path
    path = _state_path()
    if _spend_path is _MISSING or _spend_path != path:
        _spend = _load(path)
        _spend_path = path
    return _spend


def reading_task() -> asyncio.Task[None] | None:
    """The spend reading in flight on this event loop, or ``None``."""
    task = _reading
    if task is None or task.done():
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return task
    return task if task.get_loop() is loop else None


def spend_view(settings: Credit, warehouse_id: str | None = None) -> dict[str, Any]:
    """The ``spend`` object of ``GET /v1/ae/databricks`` (and of Refresh)."""
    state = _state()
    # Totals read from another credit start measure something else: not shown.
    same_credit = state["creditStart"] in (None, settings.start.isoformat())
    return {
        "status": "reading" if reading_task() is not None else state["status"],
        "message": state["message"],
        "readAt": iso(state["readAt"]) if same_credit else None,
        "asOf": iso(state["asOf"]) if same_credit else None,
        "monthUsd": state["monthUsd"] if same_credit else None,
        "totalUsd": state["totalUsd"] if same_credit else None,
        "creditUsd": settings.usd,
        "cadence": settings.cadence,
        "creditStart": settings.start.isoformat(),
        "creditEnd": settings.end.isoformat(),
        "warehouseId": warehouse_id if warehouse_id is not None else state["warehouseId"],
        # The breakdown (module note): None until a reading has one.
        "products": state["products"] if same_credit else None,
        "days": state["days"] if same_credit else None,
        "note": state["breakdownNote"] if same_credit else None,
    }


def _no_access(upstream: str | None, text: str | None) -> bool:
    return upstream in _NO_ACCESS_CODES or bool(text and _NO_ACCESS_TEXT.search(text))


def _statement_state(payload: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    """``(state, error_code, scrubbed message)`` of a statement answer."""
    status = payload.get("status")
    status = status if isinstance(status, dict) else {}
    state = status.get("state")
    error = status.get("error")
    error = error if isinstance(error, dict) else {}
    code = error.get("error_code")
    text = error.get("message")
    return (
        state if isinstance(state, str) else "",
        code if isinstance(code, str) else None,
        text.strip() if isinstance(text, str) and text.strip() else None,
    )


def _amount(value: object) -> float:
    """A JSON_ARRAY cell (a string, or null for a sum over no rows) as dollars."""
    if value is None:
        return 0.0
    try:
        number = float(value) if isinstance(value, (str, int, float)) else math.nan
    except ValueError:
        number = math.nan
    if isinstance(value, bool) or not math.isfinite(number):
        raise DatabricksError("The spend query's answer was not the expected shape.")
    return round(number, 2)


async def _cancel(
    ws: Workspace, statement_id: str, transport: httpx.AsyncBaseTransport | None
) -> None:
    """Best effort: a statement left behind should stop billing. Swallows its own errors.

    A ``CancelledError`` is not an ``Exception``, so it still propagates.
    """
    with contextlib.suppress(Exception):
        await _api(
            ws,
            "POST",
            f"/api/2.0/sql/statements/{statement_id}/cancel",
            what="spend query cancel",
            body={},
            timeout=CANCEL_TIMEOUT_S,
            transport=transport,
        )


async def _statement(
    ws: Workspace,
    warehouse_id: str,
    sql: str,
    parameters: list[dict[str, str]],
    transport: httpx.AsyncBaseTransport | None,
    *,
    what: str,
    started: float,
) -> list[Any]:
    """Run *sql* and answer its ``data_array``, bounded by the reading's deadline.

    *started* is when the reading began (:func:`_monotonic`), so every
    statement of one reading shares :data:`READING_DEADLINE_S`. *what* names
    the statement in its errors ("spend query").

    :raises DatabricksError: ``CODE_NO_ACCESS`` when the grants are missing,
        ``CODE_TIMEOUT`` past the deadline, otherwise failed.
    """
    try:
        payload = await _api(
            ws,
            "POST",
            "/api/2.0/sql/statements",
            what=what,
            body={
                "warehouse_id": warehouse_id,
                "statement": sql,
                "parameters": parameters,
                "wait_timeout": STATEMENT_WAIT,
                "on_wait_timeout": "CONTINUE",
                "format": "JSON_ARRAY",
                "disposition": "INLINE",
            },
            timeout=STATEMENT_REQUEST_TIMEOUT_S,
            transport=transport,
        )
    except DatabricksError as exc:
        if _no_access(exc.upstream, exc.detail):
            raise DatabricksError(NO_ACCESS, code=CODE_NO_ACCESS) from None
        raise
    statement_id = payload.get("statement_id")
    if not isinstance(statement_id, str) or not _STATEMENT_ID.match(statement_id):
        raise DatabricksError(f"The {what}'s answer carried no statement id.")
    state, upstream, text = _statement_state(payload)
    try:
        while state in _RUNNING_STATEMENT:
            if _monotonic() - started >= READING_DEADLINE_S:
                raise DatabricksError(
                    f"The {what} did not finish within {READING_DEADLINE_S / 60:g} "
                    "minutes and was cancelled; Refresh to try again.",
                    code=CODE_TIMEOUT,
                    status=504,
                )
            await asyncio.sleep(POLL_INTERVAL_S)
            payload = await _api(
                ws,
                "GET",
                f"/api/2.0/sql/statements/{statement_id}",
                what=f"{what} status",
                transport=transport,
            )
            state, upstream, text = _statement_state(payload)
    finally:
        # Any exit while the statement may still run (the deadline, a failed
        # poll, a CancelledError at shutdown or reset) cancels it, so a reading
        # nobody waits for does not keep the warehouse billing. A statement
        # that ended on its own (SUCCEEDED, FAILED, CANCELED) needs no cancel.
        if state in _RUNNING_STATEMENT:
            await _cancel(ws, statement_id, transport)
    if state != "SUCCEEDED":
        if _no_access(upstream, text):
            raise DatabricksError(NO_ACCESS, code=CODE_NO_ACCESS)
        detail = _scrub(text, ws)[:_ERROR_TEXT_MAX] if text else None
        word = state.lower() if state in ("FAILED", "CANCELED", "CLOSED") else "not read"
        raise DatabricksError(f"The {what} {word}" + (f": {detail}" if detail else "."))
    result = payload.get("result")
    data = result.get("data_array") if isinstance(result, dict) else None
    # No rows comes back with no data_array at all.
    return data if isinstance(data, list) else []


async def _run_statement(
    ws: Workspace,
    settings: Credit,
    warehouse_id: str,
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any]:
    """Run :data:`SPEND_SQL` and answer ``{"monthUsd", "totalUsd", "asOf"}``.

    :raises DatabricksError: as :func:`_statement` does.
    """
    data = await _statement(
        ws,
        warehouse_id,
        SPEND_SQL,
        [{"name": "credit_start", "value": settings.start.isoformat(), "type": "DATE"}],
        transport,
        what="spend query",
        started=_monotonic(),
    )
    first = data[0] if data else [None, None, None]
    if not isinstance(first, list) or len(first) < 3:
        raise DatabricksError("The spend query's answer was not the expected shape.")
    return {
        "monthUsd": _amount(first[0]),
        "totalUsd": _amount(first[1]),
        "asOf": _instant(first[2]),
    }


async def _run_breakdown(
    ws: Workspace,
    settings: Credit,
    warehouse_id: str,
    transport: httpx.AsyncBaseTransport | None,
    started: float,
) -> dict[str, Any]:
    """Run :data:`BREAKDOWN_SQL` and answer ``{"products", "days"}`` (module note).

    ``products`` is ``[{"product", "usd"}]``, the largest first, at most
    :data:`PRODUCTS_MAX`; ``days`` is one ``{"date", "usd"}`` per day of the
    last :data:`BREAKDOWN_DAYS` (UTC), oldest first, a day with no rows at 0.

    :raises DatabricksError: as :func:`_statement` does, or on an odd answer.
    """
    # A reading out of time posts nothing more: a new statement would bill past the deadline.
    if _monotonic() - started >= READING_DEADLINE_S:
        raise DatabricksError(
            f"The spend breakdown query was not started: the reading ran past "
            f"{READING_DEADLINE_S / 60:g} minutes. Refresh to try again.",
            code=CODE_TIMEOUT,
            status=504,
        )
    today = _now().date()
    since = today - timedelta(days=BREAKDOWN_DAYS - 1)
    data = await _statement(
        ws,
        warehouse_id,
        BREAKDOWN_SQL,
        [
            {"name": "credit_start", "value": settings.start.isoformat(), "type": "DATE"},
            {"name": "since", "value": since.isoformat(), "type": "DATE"},
        ],
        transport,
        what="spend breakdown query",
        started=started,
    )
    window = {(since + timedelta(days=i)).isoformat(): 0.0 for i in range(BREAKDOWN_DAYS)}
    products: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, list) or len(row) < 3 or not isinstance(row[1], str):
            raise DatabricksError("The spend breakdown's answer was not the expected shape.")
        kind, item, usd = row[0], row[1].strip(), _amount(row[2])
        if kind == "product":
            # A code this page cannot show safely counts as OTHER rather than
            # failing the whole breakdown; a credit (net negative) is left out.
            code = item if _PRODUCT.match(item) else "OTHER"
            if usd > 0:
                products.append({"product": code, "usd": usd})
        elif kind == "day" and _DAY.match(item):
            # A day outside the window (the warehouse's clock ahead of ours) is left out.
            if item in window:
                window[item] = usd
        else:
            raise DatabricksError("The spend breakdown's answer was not the expected shape.")
    merged: dict[str, float] = {}
    for row in products:
        merged[row["product"]] = round(merged.get(row["product"], 0.0) + row["usd"], 2)
    ranked = sorted(merged.items(), key=lambda pair: (-pair[1], pair[0]))
    return {
        "products": [{"product": code, "usd": usd} for code, usd in ranked[:PRODUCTS_MAX]],
        "days": [{"date": day, "usd": usd} for day, usd in window.items()],
    }


async def _still_running(
    ws: Workspace, warehouse_id: str, transport: httpx.AsyncBaseTransport | None
) -> bool:
    """Whether the warehouse is RUNNING now, read uncached (the listing may be 10 s old)."""
    try:
        payload = await _api(
            ws,
            "GET",
            f"/api/2.0/sql/warehouses/{warehouse_id}",
            what="warehouse status",
            transport=transport,
        )
    except DatabricksError as exc:
        _logger.warning("databricks automatic spend reading skipped: %s", exc)
        return False
    row = _row(ws, payload)
    return row is not None and row["id"] == warehouse_id and row["state"] == "RUNNING"


async def _read_spend(
    ws: Workspace,
    settings: Credit,
    warehouse_id: str,
    transport: httpx.AsyncBaseTransport | None = None,
    *,
    only_if_running: bool = False,
) -> None:
    """The background reading (module note): run the query, keep and save the outcome.

    :param only_if_running: For the reading ``GET`` starts: post the statement
        only when the warehouse is RUNNING right now, read uncached, so a
        warehouse that stopped since the cached listing is never woken. A skip
        records nothing, and the next ``GET`` decides again.
    """
    state = _state()
    if only_if_running and not await _still_running(ws, warehouse_id, transport):
        return
    good = False
    started = _monotonic()
    try:
        outcome = await _run_statement(ws, settings, warehouse_id, transport)
    except asyncio.CancelledError:
        # Shutdown or reset: record the attempt so the strip does not wait on
        # a reading that is gone, then let the cancellation through.
        state.update(
            status="error",
            message="The spend reading was stopped before it finished; Refresh to try again.",
            attemptedAt=_now(),
            warehouseId=warehouse_id,
        )
        _save(_spend_path if isinstance(_spend_path, Path) else None, state)
        raise
    except DatabricksError as exc:
        status: SpendStatus = "no_access" if exc.code == CODE_NO_ACCESS else "error"
        _logger.warning("databricks spend reading failed: %s", exc)
        state.update(status=status, message=str(exc))
    except Exception as exc:  # noqa: BLE001 - a background task must record, not vanish
        # The exception's text could carry anything, so only its class is named.
        _logger.warning("databricks spend reading failed (%s)", type(exc).__name__)
        state.update(status="error", message=f"The spend reading failed ({type(exc).__name__}).")
    else:
        good = True
        if state["creditStart"] not in (None, settings.start.isoformat()):
            state.update(readAt=None, asOf=None, monthUsd=None, totalUsd=None)
        state.update(
            # The last reading's rows never sit beside these totals, not even
            # while the breakdown runs: they go now, and the breakdown fills them.
            products=None,
            days=None,
            breakdownNote=None,
            status="ok",
            message=None,
            readAt=_now(),
            asOf=outcome["asOf"],
            monthUsd=outcome["monthUsd"],
            totalUsd=outcome["totalUsd"],
            creditStart=settings.start.isoformat(),
        )
        _logger.info(
            "databricks spend: month %.2f, total %.2f USD",
            outcome["monthUsd"],
            outcome["totalUsd"],
        )
    state.update(attemptedAt=_now(), warehouseId=warehouse_id)
    # Saved before the breakdown, so a good (billed) totals reading survives a kill during it.
    _save(_spend_path if isinstance(_spend_path, Path) else None, state)
    if good:
        await _read_breakdown(ws, settings, warehouse_id, transport, started, state)


async def _read_breakdown(
    ws: Workspace,
    settings: Credit,
    warehouse_id: str,
    transport: httpx.AsyncBaseTransport | None,
    started: float,
    state: dict[str, Any],
) -> None:
    """The breakdown after good totals (module note): rows, or none and a ``note``; saved."""
    path = _spend_path if isinstance(_spend_path, Path) else None
    try:
        rows = await _run_breakdown(ws, settings, warehouse_id, transport, started)
    except asyncio.CancelledError:
        # The totals are already saved: record why the rows are missing, then let it through.
        state.update(
            breakdownNote="The breakdown was stopped before it finished; Refresh to try again."
        )
        _save(path, state)
        raise
    except DatabricksError as exc:
        _logger.warning("databricks spend breakdown failed: %s", exc)
        state.update(breakdownNote=f"Breakdown unavailable: {exc} The totals are complete.")
    except Exception as exc:  # noqa: BLE001 - the totals must still be kept
        _logger.warning("databricks spend breakdown failed (%s)", type(exc).__name__)
        state.update(
            breakdownNote=f"Breakdown unavailable ({type(exc).__name__}). The totals are complete."
        )
    else:
        state.update(products=rows["products"], days=rows["days"])
    _save(path, state)


def _begin_reading(
    ws: Workspace,
    settings: Credit,
    warehouse_id: str,
    transport: httpx.AsyncBaseTransport | None,
    *,
    only_if_running: bool = False,
) -> None:
    """Start the background reading unless one is in flight (single flight)."""
    global _reading
    if reading_task() is not None:
        return
    _state()
    _reading = asyncio.get_running_loop().create_task(
        _read_spend(ws, settings, warehouse_id, transport, only_if_running=only_if_running),
        name="ae-databricks-spend",
    )


def _auto_read_allowed(row: Mapping[str, Any]) -> bool:
    """Whether ``GET`` may read on *row* unasked (module note, cost guard).

    Only a RUNNING warehouse that auto stops sooner than :data:`SPEND_STALE_S`:
    with no auto stop (``None`` or 0), or one at least that long, a read every
    :data:`SPEND_STALE_S` could keep it from ever going idle.
    """
    stop = row["autoStopMins"]
    return row["state"] == "RUNNING" and bool(stop) and stop * 60 < SPEND_STALE_S


def _stale() -> bool:
    attempted = _state()["attemptedAt"]
    return attempted is None or (_now() - attempted).total_seconds() >= SPEND_STALE_S


async def overview(
    environ: Mapping[str, str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """``GET /v1/ae/databricks``: always an answer; a failed listing rides in ``error``.

    Starts a background spend reading only under the cost guard (module note).
    """
    settings = credit(environ)
    ws = workspace(environ)
    if ws is None:
        return {
            "configured": False,
            "note": not_configured_note(environ),
            "host": None,
            "error": None,
            "warehouses": [],
            "spend": spend_view(settings),
        }
    error: dict[str, str] | None = None
    rows: list[dict[str, Any]] = []
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_S):
            rows = await warehouses(ws, transport=transport)
    except TimeoutError:
        error = {
            "code": CODE_TIMEOUT,
            "message": f"Databricks did not answer the warehouse listing within "
            f"{REQUEST_TIMEOUT_S:g} s.",
        }
    except DatabricksError as exc:
        error = {"code": exc.code, "message": str(exc)}
    if error is not None:
        _logger.warning("databricks listing failed: %s", error["message"])
    chosen = _reading_warehouse(rows)
    if chosen is not None and _auto_read_allowed(chosen) and _stale():
        _begin_reading(ws, settings, chosen["id"], transport, only_if_running=True)
    return {
        "configured": True,
        "note": None,
        "host": ws.host,
        "error": error,
        "warehouses": rows,
        "spend": spend_view(settings, chosen["id"] if chosen is not None else None),
    }


async def refresh(
    ws: Workspace,
    settings: Credit,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """``POST /v1/ae/databricks/spend/refresh``: start a reading (or join the one running).

    A query on a stopped warehouse starts it, which is why only this reads
    unasked. Answers the ``spend`` object, ``reading`` while it runs.

    :raises DatabricksError: 409 with no serverless warehouse to read on; the
        listing's own failure (502, 504) otherwise.
    """
    if reading_task() is not None:
        return spend_view(settings)
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_S):
            rows = await warehouses(ws, transport=transport)
    except TimeoutError:
        raise DatabricksError(
            f"Databricks did not answer the warehouse listing within {REQUEST_TIMEOUT_S:g} s.",
            code=CODE_TIMEOUT,
            status=504,
        ) from None
    chosen = _reading_warehouse(rows)
    if chosen is None:
        raise DatabricksError(
            "No serverless SQL warehouse to read spend on: the workspace lists none that is "
            "not being deleted. Create one, then Refresh.",
            code=CODE_NO_WAREHOUSE,
            status=409,
        )
    _begin_reading(ws, settings, chosen["id"], transport)
    return spend_view(settings, chosen["id"])


def reset() -> None:
    """Forget every cache, token and reading (tests change the environment between cases)."""
    global _spend, _spend_path, _reading
    task = _reading
    if task is not None and not task.done():
        task.cancel()
    _reading = None
    _spend = {}
    _spend_path = _MISSING
    forget()
    _tokens.clear()
    _retired.clear()


__all__ = [
    "BAD_HOST",
    "BREAKDOWN_DAYS",
    "BREAKDOWN_SQL",
    "CODE_AUTH",
    "CODE_FAILED",
    "CODE_NOT_CONFIGURED",
    "CODE_NOT_FOUND",
    "CODE_NO_ACCESS",
    "CODE_NO_WAREHOUSE",
    "CODE_TIMEOUT",
    "HOW_TO_ENABLE",
    "LIST_TTL_S",
    "NO_ACCESS",
    "READING_DEADLINE_S",
    "REQUEST_TIMEOUT_S",
    "SPEND_SQL",
    "SPEND_STALE_S",
    "TOKEN_REFRESH_MARGIN_S",
    "WAREHOUSE_ID",
    "Credit",
    "DatabricksError",
    "Workspace",
    "act",
    "credit",
    "drop_token",
    "forget",
    "iso",
    "not_configured_note",
    "overview",
    "reading_task",
    "refresh",
    "reset",
    "spend_view",
    "warehouses",
    "workspace",
]
