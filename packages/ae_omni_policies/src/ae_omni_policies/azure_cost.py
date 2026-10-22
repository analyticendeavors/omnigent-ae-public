"""Azure spend for the Admin Center's Azure card.

Added 2026-10-22, for a glance at what the Azure tenant spends across its
subscriptions and on what: a cost spike on one resource can go unseen until
someone opens Cost Management. The card shows this month so far,
the pace against last month, the last 21 days, the top services, and a flag
for a resource whose latest day costs far more than its recent average.

Configuration, from the environment:

- ``AE_AZURE_COST_SUBSCRIPTIONS``: the subscriptions to read, comma separated
  ``<guid>=<name>`` pairs (the name is what the card shows; a bare guid shows
  as itself). Not a secret.
- ``AE_AZURE_COST_TENANT_ID``, ``AE_AZURE_COST_CLIENT_ID`` and
  ``AE_AZURE_COST_CLIENT_SECRET``: a service principal with the Cost
  Management Reader role on each subscription. ``deploy/pull-secrets.sh``
  writes them from Key Vault ``azure-cost-tenant-id``, ``azure-cost-client-id``
  and ``azure-cost-client-secret``.

Configured means the subscriptions and all three secrets are present. The
tenant id, the client id, the secret and every token are never logged,
returned, or put in an error message: calls go straight to the transport (no
httpx client, so no request log), errors name the exception's class only,
raised ``from None``, and any upstream text is scrubbed of every secret first,
as in databricks.py and fabric.py.

Cost Management is rate limited and posts costs 8 to 24 hours late, so a
reading is two queries per subscription (daily by service, and the last nine
days by resource) behind a one hour cache. ``GET`` never waits on Azure: it
answers the cache and, when the cache is older than :data:`CACHE_TTL_S`,
starts a reading in the background (the extension host gives a call ten
seconds). A subscription that fails keeps its previous answer and says why, so
one 429 does not blank the card. A reading with a failure is tried again after
:data:`RETRY_TTL_S` (two minutes); a 429 stops the pass and waits Azure's own retry
time (one minute to one hour), or :data:`RATE_LIMIT_TTL_S` (15 minutes) without one,
because every query made while limited only extends the limit. The newest day with data
is ``through``, and the pace is computed up to it.

Three guards from the 2026-10-22 review: a query with more pages than
:data:`MAX_PAGES` is an error, never a quietly partial total; amounts are
never converted, so a subscription billed in another currency than the first
read one gets its own line in its own currency and stays out of the totals; and a flag needs a
finished day within :data:`FLAG_FRESH_DAYS` days and at least
:data:`FLAG_MIN_PRIOR_DAYS` earlier days, so a new resource or an old spike
does not raise one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

_logger = logging.getLogger(__name__)

CACHE_TTL_S = 3600.0
REQUEST_TIMEOUT_S = 25.0
READING_DEADLINE_S = 120.0
TOKEN_REFRESH_MARGIN_S = 300.0
MAX_PAGES = 5
SPARK_DAYS = 21
RESOURCE_DAYS = 9
FLAG_MIN_USD = 2.0
FLAG_RATIO = 3.0
FLAG_FRESH_DAYS = 3
FLAG_MIN_PRIOR_DAYS = 3
RETRY_TTL_S = 120.0
RATE_LIMIT_TTL_S = 900.0
RATE_LIMIT_MIN_S = 60.0
TOP_SERVICES = 5
MAX_FLAGS = 5
API_VERSION = "2023-11-01"
USER_AGENT = "ae-omni-policies/azure-cost"
CODE_NOT_CONFIGURED = "azure_cost_not_configured"
_GUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


@dataclass(frozen=True)
class Config:
    tenant: str
    client_id: str
    client_secret: str
    subscriptions: tuple[tuple[str, str], ...]

    def secrets(self) -> list[str]:
        return [self.client_secret, self.client_id, self.tenant]

    def key(self) -> str:
        return f"{self.tenant}:{self.client_id}"


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _subscriptions(raw: str) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for part in raw.split(","):
        sid, _, name = part.strip().partition("=")
        sid = sid.strip().lower()
        if _GUID.match(sid) and all(sid != seen for seen, _ in rows):
            rows.append((sid, name.strip() or sid))
    return tuple(rows)


def config(environ: Mapping[str, str] | None = None) -> Config | None:
    env = _env(environ)
    values = [(env.get(key) or "").strip() for key in _KEYS]
    subs = _subscriptions(values[0])
    if not subs or not all(values[1:]):
        return None
    return Config(values[1], values[2], values[3], subs)


_KEYS = (
    "AE_AZURE_COST_SUBSCRIPTIONS",
    "AE_AZURE_COST_TENANT_ID",
    "AE_AZURE_COST_CLIENT_ID",
    "AE_AZURE_COST_CLIENT_SECRET",
)


def not_configured_note() -> str:
    return (
        "Azure spend is not configured: put azure-cost-tenant-id, azure-cost-client-id and "
        "azure-cost-client-secret in Key Vault, set AE_AZURE_COST_SUBSCRIPTIONS in "
        "config/server.env, and run deploy/pull-secrets.sh."
    )


class AzureCostError(Exception):
    """A failure whose text is safe to show: it never carries a secret."""


class RateLimitedError(AzureCostError):
    """Cost Management answered 429; *wait_s* is how long to leave it alone."""

    def __init__(self, message: str, wait_s: float) -> None:
        super().__init__(message)
        self.wait_s = wait_s


def _backoff(retry_after: str | None) -> float:
    """Seconds to wait after a 429: Azure's figure within a minute to an hour, else 15 minutes."""
    if retry_after and retry_after.isdigit():
        return min(max(float(retry_after), RATE_LIMIT_MIN_S), CACHE_TTL_S)
    return RATE_LIMIT_TTL_S


def _scrub(text: str, cfg: Config) -> str:
    for secret in [*cfg.secrets(), *_tokens_held()]:
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


async def _send(
    transport: httpx.AsyncBaseTransport, request: httpx.Request
) -> tuple[int, bytes, httpx.Headers]:
    response = await transport.handle_async_request(request)
    try:
        body = await response.aread()
    finally:
        await response.aclose()
    return response.status_code, body, response.headers


_transport: httpx.AsyncHTTPTransport | None = None
_transport_loop: asyncio.AbstractEventLoop | None = None


def _shared_transport() -> httpx.AsyncHTTPTransport:
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
    return None if moment is None else moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_token_cache: dict[str, tuple[str, float]] = {}
_token_locks: dict[str, asyncio.Lock] = {}


def _tokens_held() -> list[str]:
    return [token for token, _ in _token_cache.values()]


async def _token(cfg: Config, transport: httpx.AsyncBaseTransport) -> str:
    lock = _token_locks.setdefault(cfg.key(), asyncio.Lock())
    async with lock:
        held = _token_cache.get(cfg.key())
        if held is not None and _monotonic() < held[1]:
            return held[0]
        request = httpx.Request(
            "POST",
            f"https://login.microsoftonline.com/{cfg.tenant}/oauth2/v2.0/token",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            content=urlencode(
                {
                    "grant_type": "client_credentials",
                    "client_id": cfg.client_id,
                    "client_secret": cfg.client_secret,
                    "scope": "https://management.azure.com/.default",
                }
            ).encode(),
            extensions={"timeout": httpx.Timeout(REQUEST_TIMEOUT_S).as_dict()},
        )
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                status, body, _ = await _send(transport, request)
        except (httpx.TimeoutException, TimeoutError):
            raise AzureCostError("Microsoft sign-in did not answer in time.") from None
        except httpx.HTTPError as exc:
            raise AzureCostError(
                f"Microsoft sign-in could not be reached ({type(exc).__name__})."
            ) from None
        if status != 200:
            if status in (400, 401):
                raise AzureCostError(
                    f"Microsoft refused the Azure cost service principal ({status}). Check the "
                    "AE_AZURE_COST_* secrets and restart the stack."
                )
            raise AzureCostError(f"Microsoft sign-in answered {status}.")
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise AzureCostError("Microsoft sign-in did not return a token.")
        expires = payload.get("expires_in")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or expires <= 0:
            expires = 3600
        _token_cache[cfg.key()] = (token, _monotonic() + float(expires) - TOKEN_REFRESH_MARGIN_S)
        return token


def _body(day_from: date, day_to: date, grouping: str, granularity: str) -> dict[str, Any]:
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{day_from}T00:00:00Z", "to": f"{day_to}T23:59:59Z"},
        "dataset": {
            "granularity": granularity,
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": grouping}],
        },
    }


def _day(value: object) -> date | None:
    text = str(value)
    if len(text) == 8 and text.isdigit():
        with contextlib.suppress(ValueError):
            return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    with contextlib.suppress(ValueError):
        return date.fromisoformat(text[:10])
    return None


async def _query(
    cfg: Config,
    transport: httpx.AsyncBaseTransport,
    token: str,
    sub: str,
    body: dict[str, Any],
) -> list[dict[str, Any]]:
    """Every row of one Cost Management query, as dicts keyed by column name."""
    url: str | None = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/providers/Microsoft.CostManagement/query?api-version={API_VERSION}"
    )
    rows: list[dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        if url is None:
            break
        request = httpx.Request(
            "POST",
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            content=json.dumps(body).encode(),
            extensions={"timeout": httpx.Timeout(REQUEST_TIMEOUT_S).as_dict()},
        )
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_S):
                status, raw, headers = await _send(transport, request)
        except (httpx.TimeoutException, TimeoutError):
            raise AzureCostError("Cost Management did not answer in time.") from None
        except httpx.HTTPError as exc:
            raise AzureCostError(
                f"Cost Management could not be reached ({type(exc).__name__})."
            ) from None
        if status == 429:
            wait = headers.get(
                "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after"
            ) or headers.get("retry-after")
            raise RateLimitedError(
                "Cost Management is rate limiting"
                + (f"; retry in {_scrub(wait, cfg)} s." if wait and wait.isdigit() else "."),
                _backoff(wait),
            )
        if status in (401, 403):
            raise AzureCostError(
                f"The service principal may not read this subscription's costs ({status}). "
                "Give it Cost Management Reader on it."
            )
        if status != 200:
            raise AzureCostError(f"Cost Management answered {status}.")
        try:
            payload = json.loads(raw)
            props = payload["properties"]
            names = [str(col["name"]) for col in props["columns"]]
            for row in props["rows"]:
                rows.append(dict(zip(names, row, strict=False)))
            link = props.get("nextLink")
        except (ValueError, KeyError, TypeError):
            raise AzureCostError("Cost Management's answer was not the expected JSON.") from None
        url = (
            link
            if isinstance(link, str) and link.startswith("https://management.azure.com/")
            else None
        )
    if url is not None:
        raise AzureCostError(
            f"Cost Management returned more than {MAX_PAGES} pages of rows; the card does not "
            "show partial costs."
        )
    return rows


def _cost(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _resource_name(resource_id: str) -> str:
    return resource_id.rstrip("/").rsplit("/", 1)[-1] or resource_id


async def _read_subscription(
    cfg: Config, transport: httpx.AsyncBaseTransport, token: str, sub: str, today: date
) -> dict[str, Any]:
    first_last = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    by_service = await _query(
        cfg, transport, token, sub, _body(first_last, today, "ServiceName", "Daily")
    )
    by_resource = await _query(
        cfg,
        transport,
        token,
        sub,
        _body(today - timedelta(days=RESOURCE_DAYS), today, "ResourceId", "Daily"),
    )
    daily: dict[str, float] = {}
    services: dict[str, dict[str, float]] = {}
    currency = "USD"
    for row in by_service:
        day = _day(row.get("UsageDate"))
        if day is None:
            continue
        amount = _cost(row.get("Cost"))
        daily[str(day)] = daily.get(str(day), 0.0) + amount
        name = str(row.get("ServiceName") or "Other")
        month = services.setdefault(str(day)[:7], {})
        month[name] = month.get(name, 0.0) + amount
        currency = str(row.get("Currency") or currency)
    resources: dict[str, dict[str, float]] = {}
    for row in by_resource:
        day = _day(row.get("UsageDate"))
        rid = str(row.get("ResourceId") or "")
        if day is None or not rid:
            continue
        resources.setdefault(rid, {})[str(day)] = resources.get(rid, {}).get(
            str(day), 0.0
        ) + _cost(row.get("Cost"))
    return {"daily": daily, "services": services, "resources": resources, "currency": currency}


def _flags(
    readings: Mapping[str, Mapping[str, Any]], names: Mapping[str, str], today: date
) -> list[dict[str, Any]]:
    """Resources whose newest finished day costs over :data:`FLAG_RATIO` times their prior week."""
    found: list[dict[str, Any]] = []
    for sub, reading in readings.items():
        for rid, days in reading["resources"].items():
            finished = sorted(day for day in days if day < str(today))
            # Azure posts a day late, so the newest finished day may be two or three days
            # back; an older one is history, not news.
            if not finished or finished[-1] < str(today - timedelta(days=FLAG_FRESH_DAYS)):
                continue
            newest = finished[-1]
            prior = [days[day] for day in finished[:-1]][-7:]
            if len(prior) < FLAG_MIN_PRIOR_DAYS:
                continue
            latest = days[newest]
            average = sum(prior) / len(prior)
            if latest < FLAG_MIN_USD or not average or latest < FLAG_RATIO * average:
                continue
            found.append(
                {
                    "resource": _resource_name(rid),
                    "subscription": names.get(sub, sub),
                    "day": newest,
                    "cost": round(latest, 2),
                    "average": round(average, 2),
                    "ratio": round(latest / average, 1),
                }
            )
    found.sort(key=lambda row: row["cost"], reverse=True)
    return found[:MAX_FLAGS]


def _assemble(
    cfg: Config, readings: Mapping[str, Mapping[str, Any]], errors: Mapping[str, str], today: date
) -> dict[str, Any]:
    names = dict(cfg.subscriptions)
    month = str(today.replace(day=1))[:7]
    last_month = str(today.replace(day=1) - timedelta(days=1))[:7]
    daily: dict[str, float] = {}
    services: dict[str, float] = {}
    subs: list[dict[str, Any]] = []
    # Only the configured subscriptions count (a removed one may still be cached), and only
    # in one currency: amounts are never converted, so a subscription billed in another
    # currency gets its own line in that currency and stays out of the totals. The total's
    # currency is the one most subscriptions bill in (the first in config order on a tie), so
    # one rate-limited subscription cannot swap which currency is "apart" between polls.
    configured = {sid for sid, _ in cfg.subscriptions}
    kept = {sid: reading for sid, reading in readings.items() if sid in configured}
    seen = [kept[sid]["currency"] for sid, _ in cfg.subscriptions if sid in kept]
    currency = max(dict.fromkeys(seen), key=seen.count, default="USD")
    for sid, name in cfg.subscriptions:
        reading = kept.get(sid)
        error = errors.get(sid)
        spent = 0.0
        counted = True
        row_currency = currency
        if reading is not None and reading["currency"] != currency:
            # Never converted: it gets its own line in its own currency, outside every total.
            counted = False
            row_currency = reading["currency"]
            spent = sum(a for d, a in reading["daily"].items() if d.startswith(month))
            kept.pop(sid)
        elif reading is not None:
            for day, amount in reading["daily"].items():
                daily[day] = daily.get(day, 0.0) + amount
                if day.startswith(month):
                    spent += amount
            for service, amount in reading["services"].get(month, {}).items():
                services[service] = services.get(service, 0.0) + amount
        subs.append(
            {
                "id": sid,
                "name": name,
                "spent": round(spent, 2),
                "currency": row_currency,
                "counted": counted,
                "error": error,
            }
        )
    spent = sum(amount for day, amount in daily.items() if day.startswith(month))
    previous = sum(amount for day, amount in daily.items() if day.startswith(last_month))
    through_days = [day for day in daily if day.startswith(month) and daily[day] > 0]
    through = max(through_days) if through_days else None
    days_in_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    pace = None
    if through is not None:
        pace = round(spent / int(through[-2:]) * days_in_month.day, 2)
    spark = [
        {
            "date": str(today - timedelta(days=offset)),
            "cost": round(daily.get(str(today - timedelta(days=offset)), 0.0), 2),
        }
        for offset in range(SPARK_DAYS - 1, -1, -1)
    ]
    top = sorted(services.items(), key=lambda item: item[1], reverse=True)[:TOP_SERVICES]
    return {
        "currency": currency,
        "month": {
            "spent": round(spent, 2),
            "pace": pace,
            "lastMonth": round(previous, 2),
            "through": through,
        },
        "daily": spark,
        "subscriptions": subs,
        "services": [{"name": name, "cost": round(cost, 2)} for name, cost in top],
        "flags": _flags(kept, names, today),
    }


_state: dict[str, Any] = {
    "view": None,
    "readings": {},
    "errors": {},
    "at": None,
    "mono": None,
    "ttl": CACHE_TTL_S,
    "key": None,
}
_task: asyncio.Task[None] | None = None


async def _read(cfg: Config) -> None:
    transport = _shared_transport()
    today = _now().date()
    signature = (cfg.key(), cfg.subscriptions)
    if _state["key"] != signature:
        # A new principal or subscription list: nothing cached under the old one applies.
        _state["readings"] = {}
        _state["key"] = signature
    errors: dict[str, str] = {}
    done: set[str] = set()
    backoff = 0.0

    def unread(message: str) -> dict[str, str]:
        # Only the subscriptions this pass did not finish; the others keep their new reading.
        return {sid: errors.get(sid, message) for sid, _ in cfg.subscriptions if sid not in done}

    try:
        async with asyncio.timeout(READING_DEADLINE_S):
            token = await _token(cfg, transport)
            for sid, _ in cfg.subscriptions:
                try:
                    _state["readings"][sid] = await _read_subscription(
                        cfg, transport, token, sid, today
                    )
                    done.add(sid)
                except RateLimitedError as exc:
                    # Stop asking: more queries while limited only extend the limit.
                    backoff = exc.wait_s
                    errors = unread(_scrub(str(exc), cfg))
                    break
                except AzureCostError as exc:
                    errors[sid] = _scrub(str(exc), cfg)
    except AzureCostError as exc:
        errors = unread(_scrub(str(exc), cfg))
    except TimeoutError:
        errors = unread("The reading took too long.")
    except Exception as exc:  # noqa: BLE001  a bug must show on the card, not leave it reading for good
        _logger.warning("azure cost reading failed unexpectedly: %s", type(exc).__name__)
        errors = unread("The reading failed unexpectedly.")
    _state["errors"] = errors
    _state["mono"] = _monotonic()
    # A failure is tried again in two minutes, a rate limit after its wait, a good reading
    # after the hour.
    _state["ttl"] = backoff or (RETRY_TTL_S if errors else CACHE_TTL_S)
    _state["at"] = _now()
    _state["view"] = _assemble(cfg, _state["readings"], errors, today)
    if errors:
        _logger.warning("azure cost reading had %d failing subscription(s)", len(errors))


def _stale() -> bool:
    mono = _state["mono"]
    return mono is None or _monotonic() - mono >= _state["ttl"]


def reading_task() -> asyncio.Task[None] | None:
    return _task


def _begin(cfg: Config) -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_read(cfg))


async def overview(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The card's data: the cache now, and a background reading when it is old."""
    cfg = config(environ)
    if cfg is None:
        return {"configured": False, "note": not_configured_note()}
    if _stale():
        _begin(cfg)
    view = _state["view"]
    return {
        "configured": True,
        "note": None,
        "reading": _task is not None and not _task.done(),
        "asOf": iso(_state["at"]),
        "view": view,
    }


def reset() -> None:
    global _task, _transport, _transport_loop
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
    _transport = None
    _transport_loop = None
    _token_cache.clear()
    _token_locks.clear()
    _state.update(
        {
            "view": None,
            "readings": {},
            "errors": {},
            "at": None,
            "mono": None,
            "ttl": CACHE_TTL_S,
            "key": None,
        }
    )
