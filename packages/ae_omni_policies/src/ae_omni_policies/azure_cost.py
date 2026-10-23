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
one 429 does not blank the card. A reading with a transient failure is tried again
after :data:`RETRY_TTL_S` (two minutes); one whose every failure is permanent (no Cost
Management Reader role, a refused principal, too many pages) waits the hour like a good
reading, because retrying cannot fix it and spends the shared rate limit (2026-10-22). A 429
stops the pass and waits Azure's own retry time (one minute to one hour), or
:data:`RATE_LIMIT_TTL_S` (15 minutes) without one, because every query made while limited
only extends the limit. The newest day with data
is ``through``, and the pace is computed up to it.

The spike check (the by-resource query) is separate from the totals (2026-10-22): when it
fails, the totals stay, the previous reading's resources stay (stale flags beat none, and
:data:`FLAG_FRESH_DAYS` ages them out), and the row carries a ``note``, not an ``error``. A
transient spike failure is retried in two minutes, a permanent one waits the hour.

Four guards from the 2026-10-22 review: a query with more pages than
:data:`MAX_PAGES` is an error, never a quietly partial total; amounts are
never converted, so a subscription billed in another currency than the first
read one gets its own line in its own currency and stays out of the totals, while a
subscription with no rows has no currency and does not vote for one; and a flag needs a
finished day within :data:`FLAG_FRESH_DAYS` days and at least
:data:`FLAG_MIN_PRIOR_DAYS` earlier days, so a new resource or an old spike
does not raise one.

Persisted since 2026-10-22: the cache lived in memory only, so after every deploy the card
said "Reading..." until the first ``GET`` started a reading and two queries per subscription
came back (eleven minutes after a restart that day). Each pass now writes the view, the
readings, the errors, ``at`` and the wall-clock time the next reading is due (a 429's wait
included) to ``AE_AZURE_COST_STATE_FILE`` (default ``/data/ae-azure-cost.json``; empty turns
it off), atomically the way databricks.py writes its state. The first ``overview()`` after a
start loads it and answers with the saved view and its real ``asOf``; due is judged by wall
clock, because the monotonic clock restarts with the process, so a saved rate limit is still
honored after a deploy. The file names the subscriptions it was read for and a one-way hash
of the tenant and client id, never the ids, the secret or a token; a file for another
subscription list or principal is ignored, as is one that is missing, unreadable or
malformed (the log names the exception's class only). :data:`lifespan_router`, in
``router.DEBUG_ROUTERS`` beside merge_watch's, loads it at boot and starts a reading then
when the saved one is due, so the first visit after a deploy rarely waits.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, FastAPI

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
STATE_FILE_ENV = "AE_AZURE_COST_STATE_FILE"
DEFAULT_STATE_FILE = "/data/ae-azure-cost.json"
STATE_VERSION = 1
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

    def principal(self) -> str:
        """A one-way hash of the tenant and client id, safe to write to the state file."""
        return hashlib.sha256(f"ae-azure-cost:{self.key()}".encode()).hexdigest()


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
    """A failure whose text is safe to show: it never carries a secret.

    *permanent* marks one that retrying will not fix (a principal without the role, a
    refused principal, a subscription too big to page); the card then waits the hour
    instead of asking again every two minutes and spending the shared rate limit.
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


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


_REFUSED_CODES = {"invalid_client", "unauthorized_client", "invalid_grant"}


def _refused_principal(body: bytes | str) -> bool:
    """True when a 400 from the token endpoint names a bad principal, not a bad request.

    Only the error code is read; the body is never put in a message.
    """
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("error") in _REFUSED_CODES


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
            if status == 401 or (status == 400 and _refused_principal(body)):
                raise AzureCostError(
                    f"Microsoft refused the Azure cost service principal ({status}). Check the "
                    "AE_AZURE_COST_* secrets and restart the stack.",
                    permanent=True,
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
        if status == 401:
            # An invalid or expired bearer token, not a missing role: sign in again next try.
            _token_cache.pop(cfg.key(), None)
            raise AzureCostError(
                "Cost Management refused the sign-in token (401); the next try signs in again."
            )
        if status == 403:
            raise AzureCostError(
                f"The service principal may not read this subscription's costs ({status}). "
                "Give it Cost Management Reader on it.",
                permanent=True,
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
            "show partial costs.",
            permanent=True,
        )
    return rows


def _cost(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _resource_name(resource_id: str) -> str:
    return resource_id.rstrip("/").rsplit("/", 1)[-1] or resource_id


async def _read_subscription(
    cfg: Config,
    transport: httpx.AsyncBaseTransport,
    token: str,
    sub: str,
    today: date,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    first_last = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    by_service = await _query(
        cfg, transport, token, sub, _body(first_last, today, "ServiceName", "Daily")
    )
    by_resource: list[dict[str, Any]] = []
    note = None
    spike = None
    try:
        by_resource = await _query(
            cfg,
            transport,
            token,
            sub,
            _body(today - timedelta(days=RESOURCE_DAYS), today, "ResourceId", "Daily"),
        )
    except RateLimitedError:
        raise
    except AzureCostError as exc:
        # The spike check is a bonus on top of the totals: losing it must not lose them.
        note = f"Spike check unavailable: {_scrub(str(exc), cfg)} Totals are complete."
        spike = "permanent" if exc.permanent else "transient"
    daily: dict[str, float] = {}
    services: dict[str, dict[str, float]] = {}
    currency: str | None = None
    for row in by_service:
        day = _day(row.get("UsageDate"))
        if day is None:
            continue
        amount = _cost(row.get("Cost"))
        daily[str(day)] = daily.get(str(day), 0.0) + amount
        name = str(row.get("ServiceName") or "Other")
        month = services.setdefault(str(day)[:7], {})
        month[name] = month.get(name, 0.0) + amount
        currency = str(row.get("Currency") or currency or "USD")
    resources: dict[str, dict[str, float]] = {}
    if spike is not None and previous is not None:
        # Stale flags beat none; FLAG_FRESH_DAYS ages them out. Days past the query's window
        # are dropped, so a carried copy, now saved across restarts, cannot grow for good.
        oldest = str(today - timedelta(days=RESOURCE_DAYS))
        for rid, days in (previous.get("resources") or {}).items():
            recent = {day: cost for day, cost in days.items() if day >= oldest}
            if recent:
                resources[rid] = recent
    for row in by_resource:
        day = _day(row.get("UsageDate"))
        rid = str(row.get("ResourceId") or "")
        if day is None or not rid:
            continue
        resources.setdefault(rid, {})[str(day)] = resources.get(rid, {}).get(
            str(day), 0.0
        ) + _cost(row.get("Cost"))
    return {
        "daily": daily,
        "services": services,
        "resources": resources,
        "currency": currency,
        "note": note,
        # How the spike check failed, for _read to pick the retry time; it pops this.
        "spike": spike,
    }


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
    # A subscription with no rows has no currency (None) and does not vote; it adds nothing
    # to the totals, so it counts in them whatever the currency.
    seen = [
        kept[sid]["currency"]
        for sid, _ in cfg.subscriptions
        if sid in kept and kept[sid]["currency"] is not None
    ]
    currency = max(dict.fromkeys(seen), key=seen.count, default="USD")
    for sid, name in cfg.subscriptions:
        reading = kept.get(sid)
        error = errors.get(sid)
        spent = 0.0
        counted = True
        row_currency = currency
        if reading is not None and reading["currency"] not in (None, currency):
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
                # A stale note beside a new error would contradict it.
                "note": None if reading is None or error else reading.get("note"),
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
# Whether this process has looked at the state file yet (once, on the first overview or at boot).
_loaded = False
# The saved view's parts and their types, as _assemble builds them: the page reads them as is.
_VIEW_SHAPE: dict[str, type] = {
    "currency": str,
    "month": dict,
    "daily": list,
    "subscriptions": list,
    "services": list,
    "flags": list,
}


def _state_path() -> Path | None:
    raw = os.environ.get(STATE_FILE_ENV)
    raw = DEFAULT_STATE_FILE if raw is None else raw.strip()
    return Path(raw) if raw else None


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def _amounts(value: object) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, float] = {}
    for key, amount in value.items():
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            return None
        out[str(key)] = float(amount)
    return out


def _nested(value: object) -> dict[str, dict[str, float]] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, dict[str, float]] = {}
    for key, inner in value.items():
        amounts = _amounts(inner)
        if amounts is None:
            return None
        out[str(key)] = amounts
    return out


def _saved_reading(value: object) -> dict[str, Any] | None:
    """A reading as _read_subscription returns it, or None when the saved one is not that."""
    if not isinstance(value, dict):
        return None
    daily = _amounts(value.get("daily"))
    services = _nested(value.get("services"))
    resources = _nested(value.get("resources"))
    currency = value.get("currency")
    note = value.get("note")
    if daily is None or services is None or resources is None:
        return None
    if not isinstance(currency, (str, type(None))) or not isinstance(note, (str, type(None))):
        return None
    return {
        "daily": daily,
        "services": services,
        "resources": resources,
        "currency": currency,
        "note": note,
    }


def _subscription_list(cfg: Config) -> list[list[str]]:
    return [[sid, name] for sid, name in cfg.subscriptions]


def _load(cfg: Config, path: Path | None) -> dict[str, Any] | None:
    """The saved reading for *cfg*, or None when there is none that applies."""
    if path is None:
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _logger.warning("azure cost: ignoring unreadable %s (%s)", path, type(exc).__name__)
        return None
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        _logger.warning("azure cost: ignoring malformed %s", path)
        return None
    if raw.get("subscriptions") != _subscription_list(cfg) or raw.get("principal") != (
        cfg.principal()
    ):
        # Read for another subscription list or principal: none of it applies now.
        _logger.info("azure cost: discarding %s, read for another configuration", path)
        return None
    at, due = _instant(raw.get("at")), _instant(raw.get("due"))
    view, readings, errors = raw.get("view"), raw.get("readings"), raw.get("errors")
    configured = {sid for sid, _ in cfg.subscriptions}
    if (
        at is None
        or due is None
        or not isinstance(view, dict)
        or not all(isinstance(view.get(key), kind) for key, kind in _VIEW_SHAPE.items())
        or not all(
            isinstance(row, dict)
            for key in ("daily", "subscriptions", "services", "flags")
            for row in view[key]
        )
        or not isinstance(readings, dict)
        or not isinstance(errors, dict)
        or not all(sid in configured and isinstance(text, str) for sid, text in errors.items())
    ):
        _logger.warning("azure cost: ignoring malformed %s", path)
        return None
    kept: dict[str, dict[str, Any]] = {}
    for sid, value in readings.items():
        reading = _saved_reading(value)
        if sid not in configured or reading is None:
            _logger.warning("azure cost: ignoring malformed %s", path)
            return None
        kept[sid] = reading
    return {"view": view, "readings": kept, "errors": dict(errors), "at": at, "due": due}


def _save(cfg: Config, path: Path | None) -> None:
    """Write the reading atomically (a temp file, then ``os.replace``), like databricks.py.

    Only the subscription list and :meth:`Config.principal` say what it was read for:
    never the tenant id, the client id, the secret or a token.
    """
    if path is None or _state["at"] is None:
        return
    body = {
        "version": STATE_VERSION,
        "subscriptions": _subscription_list(cfg),
        "principal": cfg.principal(),
        "at": iso(_state["at"]),
        "due": iso(_state["at"] + timedelta(seconds=_state["ttl"])),
        "view": _state["view"],
        "readings": _state["readings"],
        "errors": _state["errors"],
    }
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ae-azure-cost-", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, separators=(",", ":"))
        os.replace(tmp, path)
        tmp = None
    except (OSError, TypeError, ValueError) as exc:
        _logger.warning("azure cost: could not write %s (%s)", path, type(exc).__name__)
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _restore(cfg: Config) -> None:
    """Load the saved reading once per process, so a restart answers with it at once.

    Due is judged by wall clock (the monotonic clock restarts with the process): the time
    left becomes this process's ttl, capped at the hour, so a saved 429 wait still holds.
    """
    global _loaded
    if _loaded:
        return
    _loaded = True
    if _state["at"] is not None:
        return
    saved = _load(cfg, _state_path())
    if saved is None:
        return
    left = (saved["due"] - _now()).total_seconds()
    _state.update(
        {
            "view": saved["view"],
            "readings": saved["readings"],
            "errors": saved["errors"],
            "at": saved["at"],
            "mono": _monotonic(),
            "ttl": min(max(left, 0.0), CACHE_TTL_S),
            "key": (cfg.key(), cfg.subscriptions),
        }
    )


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
    permanent: set[str] = set()
    spike_retry = False

    def unread(message: str) -> dict[str, str]:
        # Only the subscriptions this pass did not finish; the others keep their new reading.
        return {sid: errors.get(sid, message) for sid, _ in cfg.subscriptions if sid not in done}

    try:
        async with asyncio.timeout(READING_DEADLINE_S):
            token = await _token(cfg, transport)
            for sid, _ in cfg.subscriptions:
                try:
                    reading = await _read_subscription(
                        cfg, transport, token, sid, today, _state["readings"].get(sid)
                    )
                    spike_retry = spike_retry or reading.pop("spike") == "transient"
                    _state["readings"][sid] = reading
                    done.add(sid)
                except RateLimitedError as exc:
                    # Stop asking: more queries while limited only extend the limit.
                    backoff = exc.wait_s
                    errors = unread(_scrub(str(exc), cfg))
                    break
                except AzureCostError as exc:
                    errors[sid] = _scrub(str(exc), cfg)
                    if exc.permanent:
                        permanent.add(sid)
    except AzureCostError as exc:
        errors = unread(_scrub(str(exc), cfg))
        if exc.permanent:
            permanent.update(errors)
    except TimeoutError:
        errors = unread("The reading took too long.")
    except Exception as exc:  # noqa: BLE001  a bug must show on the card, not leave it reading for good
        _logger.warning("azure cost reading failed unexpectedly: %s", type(exc).__name__)
        errors = unread("The reading failed unexpectedly.")
    _state["errors"] = errors
    _state["mono"] = _monotonic()
    # A transient failure is tried again in two minutes, a rate limit after its wait, a good
    # reading after the hour. So is a pass whose every error is permanent (no role, a refused
    # principal, too many pages): retrying cannot fix it and each try spends the shared
    # Cost Management rate limit.
    # A transient spike-check failure counts too: its note is not an error, but it is worth
    # asking again soon.
    retry = spike_retry or any(sid not in permanent for sid in errors)
    _state["ttl"] = backoff or (RETRY_TTL_S if retry else CACHE_TTL_S)
    _state["at"] = _now()
    _state["view"] = _assemble(cfg, _state["readings"], errors, today)
    _save(cfg, _state_path())
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
    _restore(cfg)
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
    global _task, _transport, _transport_loop, _loaded
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
    _loaded = False
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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Load the saved reading at boot, and start a reading then when it is due."""
    try:
        cfg = config()
        if cfg is not None:
            _restore(cfg)
            if _stale():
                _begin(cfg)
    except Exception as exc:  # noqa: BLE001  the card must never stop the server starting
        _logger.warning("azure cost: boot reading not started (%s)", type(exc).__name__)
    try:
        yield
    finally:
        task = _task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


#: No routes; included for its lifespan, like merge_watch's (see the module docstring).
lifespan_router = APIRouter(lifespan=lifespan)
