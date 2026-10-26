"""Fabric capacities for the Admin Center's Fabric section: status, wake, suspend and keep awake.

Added 2026-10-16. Reid wanted the aetraining wake button inside omnigent-ae,
next to the Dashboard, with room for more capacities later; Off came at his
request the same day. Each capacity sits behind its own Azure Logic App
(built in agents-and-automation), which holds the Azure credential and does
the ARM call; this server only holds the Logic App's URL.

Configuration, from the environment, so the list grows without a code change:

- ``AE_FABRIC_WAKE_URL_<NAME>``: the Logic App's HTTP trigger URL for one
  capacity. ``NAME`` is upper case (letters, digits, underscores); the
  capacity's name is the suffix lower-cased, so
  ``AE_FABRIC_WAKE_URL_AETRAINING`` is ``aetraining``. A secret: the URL
  carries the trigger's signature, so anyone holding it can wake the
  capacity (that is the shared wake link). ``deploy/pull-secrets.sh`` writes
  it from Key Vault ``aetraining-wake-url``.
- ``AE_FABRIC_SUSPEND_KEY_<NAME>``: optional, a secret. The Logic App
  suspends only with this key, so holders of the shared wake link can wake
  but never turn a capacity off. Without it the page offers no Off for that
  capacity. ``AE_FABRIC_SUSPEND_KEY_AETRAINING`` comes from Key Vault
  ``aetraining-suspend-key``.

Neither value is ever logged, returned, or put in an error message. Two
traps make that more than a matter of care:

- httpx's ``AsyncClient`` logs every request at INFO with the full URL, the
  signature included. So calls go straight to the transport
  (:func:`_send`), which logs nothing; the client's redirects and cookies
  are not needed for one GET.
- an httpx exception's text and a Logic App's error body can carry the URL
  or the key. Errors name the exception's class only, raised ``from None``
  so no chained traceback carries the original, and any body text is
  scrubbed of every secret first (:func:`_scrub`).

The Logic App contract (agents-and-automation, 2026-10-16): a GET on the
wake URL with ``format=json`` and ``action=probe|wake|suspend`` answers 200
with ``{"capacity", "state", "action", "changed"}``. Suspend also sends the
key as the request header ``x-suspend-key`` (64 lowercase hex characters;
changed the same day from a ``key`` query parameter, so the key never sits in
a URL that a proxy or a log could keep). A wrong or missing key answers 403
with ``{"error": "suspend key missing or wrong"}``. The wake URL already
carries a query string (``api-version``, ``sig``, ...), so the parameters are
merged into it with ``httpx.URL.copy_merge_params``. Passing ``params=`` to
``httpx.Request`` instead would replace that query and drop the signature.

Every call is bounded by :data:`REQUEST_TIMEOUT_S` in total (connect, send,
wait and the body read together, not per phase), under the ten seconds the
extension SDK gives a host call, so a slow Logic App reads as this server's
timeout sentence, not the page's "timed out". Status is cached
:data:`STATUS_TTL_S` per capacity and dropped after a wake or a suspend (a
probe already in flight when that happens does not write its stale answer
back); one capacity failing is reported on its own row, never instead of the
others.

2026-10-22, instant GET (measured live: three ``GET /v1/ae/fabric`` took 4.6 s,
4.6 s and 8.0 s, the last with the capacities Logic App not answering within
8 s). The per-capacity caches above live ten seconds but the page polls every
15 s while busy and 60 s when idle, so every poll missed and waited on live
Logic App calls. :func:`overview` now keeps the last whole answer
(:data:`_last`) and returns it at once, however old; when it is older than
:data:`STATUS_TTL_S` (an answer with an error in it counts as stale at once) it
also starts one background refresh, never two, and the next poll gets the fresh
rows. The first ever GET still waits for the live answer. :func:`forget` drops
the kept answer, so after a wake, suspend, keep awake or scale the next GET
waits live, and a refresh begun before that action does not write its older
answer back. A refresh that fails keeps the last rows and shows the error the
way a live GET does (``fleetError``, or the row's own ``error``); a bug in the refresh is logged
by class name only.

2026-10-16, second pass (Reid approved the compact row design): the probe
answer may also carry ``sku``, ``region``, ``capacityId`` (the ARM resource
id), ``hourlyUsd``, ``resumedAt`` (the last successful resume),
``keepAwakeSince``, ``quietSince``, ``maxHours`` and ``nextCheckAt`` (the
hourly runbook's next run), each null when unknown. Every one is optional,
because the live Logic App does not send them yet; each is checked against a
narrow shape and dropped (null) when it does not fit, so nothing odd is
echoed to the page. ``GET /v1/ae/fabric`` passes ``sku``, ``region``,
``capacityId``, ``hourlyUsd``, ``resumedAt`` and ``maxHours`` through and adds
``sleepsAt`` (:func:`sleeps_at`), when an idle Active capacity is expected to
be suspended. ``action=keepawake&hours=N`` (1 to 12) pushes that back; it
needs the suspend key in the same header, so ``can_keep_awake`` follows
``can_suspend``.

The transport is one module-level ``httpx.AsyncHTTPTransport`` for the life of
the process (:func:`_shared_transport`), so connections are reused. The router
has no lifespan hook to close it on, and the process owns nothing else to
flush; the OS closes the sockets at exit. It is rebuilt if the running event
loop changes, because a pool is bound to the loop that opened it.

2026-10-16, third pass (review fixes; the Logic App changes in parallel):
keep awake now writes its own Automation variable. Every answer may carry
``keepAwakeUntil`` (ISO UTC or null); ``action=keepawake`` sets it to the
later of its value and now + N hours and clears ``QuietSince``, and answers
with ``keepAwakeUntil`` instead of ``keepAwakeSince``. While now is before it
the runbook does nothing; after it the old rule holds. The old shape (no
``keepAwakeUntil``) still reads. :func:`sleeps_at` is the one copy of the
runbook's rule on this side: ``GET`` also returns ``keepAwakeOptions`` (what 2,
4 or 8 hours would do, from the same function) and ``wakeBilledHours``, so the
page only formats what the server says. A wake, a suspend or a keep awake
answers the capacity's full row, built from the Logic App's answer over the
last details read, so the page can redraw the card at once. ``capacityId`` is
rebuilt in ARM's canonical casing (:func:`_capacity_id`) or dropped, so the
Azure link the page builds is one the host's allowlist admits.

2026-10-21 (the capacity fleet): the Compute card showed aetraining as Unknown
with "the Logic App did not answer the status call within 8 s". Root cause, in
the Logic App, not here: ``la-aetraining-wake`` holds its HTTP reply until five
Azure Automation and Activity Log reads finish, and those reads occasionally
stall (one ``Get_job_schedules`` took 85 s; 6 of 50 runs passed 8 s and ended
as a 504). agents-and-automation caps each read at 3 s, but that cap may not
bound a stalled read: the 85 s read already ran under a 20 s cap. So the guard
is this server: a row's state comes from the fleet's ARM read below, and a
failed or slow details probe keeps the last good details (never offering keep
awake from them). Reid also wanted every
Fabric capacity he has (three subscriptions today), each with its SKU, state,
Pause or Start, and a resize behind a confirm. Design he approved: this server
still holds no Azure credential; one shared Logic App, ``la-fabric-capacities``,
does the ARM calls. Two optional secrets turn it on:

- ``AE_FABRIC_CAPACITIES_URL``: its HTTP trigger URL (carries the signature;
  Key Vault ``fabric-capacities-url``).
- ``AE_FABRIC_CAPACITIES_KEY``: 64 lowercase hex, sent as the
  ``x-capacity-key`` header on every call (Key Vault ``fabric-capacities-key``).

Both are scrubbed and kept out of logs exactly like the wake URL and the
suspend key: the same transport (:func:`_send`), one deadline over each whole
call, the same :func:`_scrub`. The fleet takes a POSTed JSON body,
``{"action": "list" | "resume" | "suspend" | "scale", "capacityId", "sku"}``.
``list`` reads a fixed set of subscriptions and answers every capacity in
them; a subscription Azure did not answer for comes back in ``errors`` by id,
and its capacities keep their last known rows with ``error`` set
(:data:`UNREAD_SUBSCRIPTION`), never an id in any message. A write does not
list (changed the same day): the Logic App checks the id, GETs that capacity,
writes, and answers that one row (Resuming, Pausing or Updating when it
changed), so :data:`_fleet_known` is updated one row at a time. The list keeps
:data:`REQUEST_TIMEOUT_S`; a write, about 8 s at worst on Azure's side, gets
:data:`FLEET_WRITE_TIMEOUT_S` (9.5 s, under the extension SDK's 10 s), and a
write that runs out says it may still be under way. With both set, ``GET`` lists every capacity
the fleet returns, sorted by name, and for a name that also has an
``AE_FABRIC_WAKE_URL_<NAME>`` merges that Logic App's details (keep awake,
sleeps at, last resume) when its probe answers in time. The list call and the
probes run at once; a probe that fails or times out only leaves the details
out, never turns the row Unknown. ``hourlyUsd`` is the SKU's capacity units
times ``AE_FABRIC_USD_PER_CU_HOUR`` (default 0.18, pay as you go; F4 is
$0.72/h). Pause and Start go through the fleet for every row; keep awake stays
on the per-capacity Logic App, and with the fleet set up a keep awake answers
the fleet's row (SKU, price, id, subscription) over its own details, so the
page keeps the fleet's price. When the fleet fails before any write can have
gone out (the list failed or timed out, the capacity is not on it, or the
fleet refused the key or the capacity: 403, 404), Start and Pause fall back to
the capacity's own wake Logic App when it has one (``FabricError.unsent``); a
write that timed out or answered 502 may have reached Azure, so it never falls
back. Resize (:func:`fleet_act`) checks the SKU against
:data:`SKU_CHOICES` before any call. Rows are addressed by name, as before;
names are unique across the fleet today, and a write to a name two rows share
is refused rather than guessed. Without the fleet secrets nothing here changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote, quote_plus, urlsplit

import httpx

from ae_omni_policies import __version__, key_health

_logger = logging.getLogger(__name__)

WAKE_URL_PREFIX = "AE_FABRIC_WAKE_URL_"
SUSPEND_KEY_PREFIX = "AE_FABRIC_SUSPEND_KEY_"
# Under the SDK's ten-second budget for one host call (see the module note).
REQUEST_TIMEOUT_S = 8.0
# A fleet write (resume, suspend, scale) runs about 8 s at worst on the Logic
# App's side (it GETs the capacity, then writes), so it gets a little more than
# a read; still under the 10 s the extension SDK gives a host call, so the page
# reads this server's sentence, not its own "timed out".
FLEET_WRITE_TIMEOUT_S = 9.5
STATUS_TTL_S = 10.0
SUSPEND_KEY_HEADER = "x-suspend-key"
USER_AGENT = f"omnigent-ae/{__version__} (+https://github.com/analyticendeavors/omnigent-ae)"
# The env suffix: upper-case letters, digits and underscores.
_SUFFIX = re.compile(r"^[A-Z0-9][A-Z0-9_]*$")
# A state is a word or two of letters (ARM's Active, Paused, ...; the Logic
# App's Waking, WakeFailed, ...). Anything else reads as Unknown, so nothing
# odd from the Logic App is echoed to the page.
_STATE = re.compile(r"^[A-Za-z][A-Za-z ]{0,39}$")
# Query values shorter than this are incidental (api-version=1, sv=1.0), not
# secrets, and would scrub ordinary text such as a state word. The signature
# is far longer. The suspend key and the URL itself have no floor.
_QUERY_VALUE_MIN = 6
# Error text from a Logic App body, after scrubbing, is cut to this.
_ERROR_TEXT_MAX = 200

# The extra probe fields (module note, second pass). Each is null unless it
# fits its shape: a SKU word, a region as ARM writes it either way
# ("North Central US" or "northcentralus"), an ARM resource id for a Fabric
# capacity, a price under a thousand dollars an hour, a window of 1 to 48 hours.
_SKU = re.compile(r"^[A-Za-z0-9]{1,16}$")
_REGION = re.compile(r"^[A-Za-z][A-Za-z0-9 ()-]{0,47}$")
# The id is matched in any case and rebuilt by _capacity_id in the one shape
# upstream/web/src/lib/aeExternalUrls.ts isAeFabricAdminLinkUrl admits (patch
# 0064): a strict 8-4-4-4-12 guid, written lower case, and the structural
# segments in ARM's own casing.
_GUID = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_CAPACITY_ID = re.compile(
    rf"^/subscriptions/(?P<sub>{_GUID})/resourceGroups/(?P<rg>[A-Za-z0-9._()-]{{1,90}})"
    r"/providers/Microsoft\.Fabric/capacities/(?P<name>[A-Za-z0-9-]{1,63})$",
    re.IGNORECASE,
)
_HOURLY_MAX = 1000.0
_MAX_HOURS_RANGE = range(1, 49)
# keepawake's hours, inclusive (the Logic App answers 400 outside them).
KEEP_AWAKE_HOURS_MIN = 1
KEEP_AWAKE_HOURS_MAX = 12
# The choices the page offers, each projected by keep_awake_options.
KEEP_AWAKE_CHOICES = (2, 4, 8)
_HOUR = timedelta(hours=1)
# The runbook's constants (agents-and-automation
# fabric-capacity/Suspend-FabricCapacity.runbook.ps1, 2026-10-16): a quiet mark
# counts only when the previous hourly run set it, 45 to 100 minutes before;
# with no resume on record and no keep awake the window starts 30 days back;
# setup-capacity-autosuspend.ps1 sets MaxHours to 6.
_MIN_QUIET_GAP = timedelta(minutes=45)
_MAX_QUIET_GAP = timedelta(minutes=100)
_NO_RESUME_LOOKBACK = timedelta(days=30)
DEFAULT_MAX_HOURS = 6
# A job starts a minute or two after its scheduled minute, so a decision time
# up to this long after a scheduled run is taken as caught by that run.
_RUN_SLACK = timedelta(minutes=5)

Action = Literal["probe", "wake", "suspend", "keepawake"]

CODE_FAILED = "fabric_failed"
CODE_TIMEOUT = "fabric_timeout"
CODE_SUSPEND_NOT_CONFIGURED = "fabric_suspend_not_configured"
CODE_BAD_HOURS = "fabric_bad_hours"
CODE_BAD_SKU = "fabric_bad_sku"
CODE_FLEET_NOT_CONFIGURED = "fabric_fleet_not_configured"
CODE_AMBIGUOUS = "fabric_ambiguous_name"
CODE_NOT_FOUND = "fabric_not_found"

# The capacity fleet (module note, 2026-10-21).
FLEET_URL_ENV = "AE_FABRIC_CAPACITIES_URL"
FLEET_KEY_ENV = "AE_FABRIC_CAPACITIES_KEY"
USD_PER_CU_ENV = "AE_FABRIC_USD_PER_CU_HOUR"
DEFAULT_USD_PER_CU_HOUR = 0.18
CAPACITY_KEY_HEADER = "x-capacity-key"
# The sizes a resize may pick, the shared Logic App's own list (it refuses others).
SKU_CHOICES = ("F2", "F4", "F8", "F16", "F32", "F64")
_F_SKU = re.compile(r"^F(\d{1,4})$")
# ARM's names: a capacity, a subscription's display name, a resource group.
_CAPACITY_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,62}$")
_SUBSCRIPTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,63}$")
_RESOURCE_GROUP = re.compile(r"^[A-Za-z0-9._()-]{1,90}$")
_FLEET_WHAT = {"list": "list", "resume": "start", "suspend": "pause", "scale": "resize"}

FLEET_NOT_CONFIGURED = (
    "Resize is not set up: the server has no AE_FABRIC_CAPACITIES_URL and "
    "AE_FABRIC_CAPACITIES_KEY (Key Vault fabric-capacities-url and fabric-capacities-key). "
    "Run deploy/pull-secrets.sh and restart the stack; deploy/README.md has the steps."
)

HOW_TO_ENABLE = (
    "No Fabric capacity is configured: the server has none. "
    "Set AE_FABRIC_WAKE_URL_<NAME> in the server's environment to the capacity's "
    "Logic App wake URL (and, for Off, AE_FABRIC_SUSPEND_KEY_<NAME> to its suspend "
    "key), then restart the stack. Each AE_FABRIC_WAKE_URL_<NAME> variable adds one "
    "capacity; deploy/README.md has the steps."
)


@dataclass(frozen=True)
class Capacity:
    """One capacity and its Logic App; the secrets stay out of ``repr``."""

    name: str
    env_suffix: str
    wake_url: str = field(repr=False)
    suspend_key: str = field(default="", repr=False)

    @property
    def can_suspend(self) -> bool:
        return bool(self.suspend_key)

    @property
    def can_keep_awake(self) -> bool:
        """Keep awake needs the same key as suspend (the Logic App checks it)."""
        return bool(self.suspend_key)

    def secrets(self) -> list[str]:
        """Every string an answer must never contain (:func:`_url_secrets`)."""
        return _url_secrets(self.wake_url, self.suspend_key)


@dataclass(frozen=True)
class Fleet:
    """The shared capacities Logic App (module note, 2026-10-21); secrets out of ``repr``."""

    url: str = field(repr=False)
    key: str = field(repr=False)

    def secrets(self) -> list[str]:
        """Every string an answer must never contain (:func:`_url_secrets`)."""
        return _url_secrets(self.url, self.key)


def _url_secrets(url: str, key: str) -> list[str]:
    """Every string an answer must never contain.

    The URL, the key (whatever its length) and each query value of the URL,
    each also in its percent-encoded forms (``quote`` and ``quote_plus``),
    because an echoed error can carry either spelling. Only the incidental
    query values keep a length floor (:data:`_QUERY_VALUE_MIN`).
    """
    always = [url, key]
    incidental: list[str] = []
    try:
        query = httpx.URL(url).params
    except (httpx.InvalidURL, TypeError, ValueError):
        query = httpx.QueryParams()
    for _key, value in query.multi_items():
        incidental.append(value)
    incidental.extend(re.findall(r"=([^&#]+)", urlsplit(url).query))
    found = [item for item in always if item]
    found += [item for item in incidental if len(item) >= _QUERY_VALUE_MIN]
    for item in list(found):
        found += [quote(item, safe=""), quote(item), quote_plus(item)]
    return sorted({item for item in found if item}, key=len, reverse=True)


def capacities(environ: Mapping[str, str] | None = None) -> dict[str, Capacity]:
    """Every capacity the environment names, by name, in name order."""
    env = os.environ if environ is None else environ
    found: dict[str, Capacity] = {}
    for key in env:
        if not key.startswith(WAKE_URL_PREFIX):
            continue
        suffix = key[len(WAKE_URL_PREFIX) :]
        url = (env.get(key) or "").strip()
        if not _SUFFIX.match(suffix) or not url:
            continue
        found[suffix.lower()] = Capacity(
            name=suffix.lower(),
            env_suffix=suffix,
            wake_url=url,
            suspend_key=(env.get(f"{SUSPEND_KEY_PREFIX}{suffix}") or "").strip(),
        )
    return dict(sorted(found.items()))


def find(name: str, environ: Mapping[str, str] | None = None) -> Capacity | None:
    """The capacity called *name* (any case), or ``None``."""
    return capacities(environ).get(name.lower())


def fleet(environ: Mapping[str, str] | None = None) -> Fleet | None:
    """The shared capacities Logic App, when both its URL and its key are set."""
    env = os.environ if environ is None else environ
    url = (env.get(FLEET_URL_ENV) or "").strip()
    key = (env.get(FLEET_KEY_ENV) or "").strip()
    return Fleet(url=url, key=key) if url and key else None


def usd_per_cu_hour(environ: Mapping[str, str] | None = None) -> float:
    """``AE_FABRIC_USD_PER_CU_HOUR``, or the default when unset or not a sane price."""
    env = os.environ if environ is None else environ
    try:
        rate = float((env.get(USD_PER_CU_ENV) or "").strip())
    except ValueError:
        return DEFAULT_USD_PER_CU_HOUR
    return rate if math.isfinite(rate) and 0 < rate < 100 else DEFAULT_USD_PER_CU_HOUR


def hourly_for(sku: str | None, rate: float) -> float | None:
    """An F SKU's price an hour: its capacity units (F4 is 4) times *rate*; else ``None``."""
    found = _F_SKU.match(sku or "")
    if found is None:
        return None
    return _hourly(round(int(found[1]) * rate, 4))


def check_sku(name: str, sku: object) -> str:
    """*sku* when it is one of :data:`SKU_CHOICES`; else a 400 naming the choices."""
    if isinstance(sku, str) and sku in SKU_CHOICES:
        return sku
    raise FabricError(
        f"{name}: the size must be one of {', '.join(SKU_CHOICES)}.",
        code=CODE_BAD_SKU,
        status=400,
    )


def suspend_not_configured(capacity: Capacity, what: str = "Off") -> str:
    """The sentence for Off (or Keep awake) on a capacity with no suspend key."""
    return (
        f"{what} is not set up for {capacity.name}: the server has no "
        f"{SUSPEND_KEY_PREFIX}{capacity.env_suffix}. Set it in the server's environment "
        "to the Logic App's suspend key and restart the stack; deploy/README.md has the "
        "steps."
    )


class FabricError(Exception):
    """A Logic App call that failed; the message names the capacity, never a secret.

    ``unsent`` is true only when no write can have reached Azure (the fleet's
    list failed, or it refused the key or the capacity before writing), so a
    Start or Pause may safely go through the capacity's own wake Logic App
    instead. A write that timed out or answered 502 may have gone out: never.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = CODE_FAILED,
        status: int = 502,
        unsent: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.unsent = unsent


def _scrub(text: str, holder: Capacity | Fleet) -> str:
    for secret in holder.secrets():
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


def _note_capacity(capacity: Capacity, action: Action, status: int) -> None:
    """Tell the Admin Center's secrets list how the capacity's pair fared (``key_health.py``).

    The wake URL carries the Logic App's signature, so a 401 is that URL refused and
    any answer below 500 means it was taken. The suspend key rides only on suspend and
    keep-awake, where the Logic App answers 403 to a wrong one.
    """
    suffix = capacity.env_suffix
    if status == 401:
        key_health.record(f"fabric_wake:{suffix}", False, status)
        return
    if status < 500:
        key_health.record(f"fabric_wake:{suffix}", True)
    if action in ("suspend", "keepawake"):
        if status == 403:
            key_health.record(f"fabric_suspend:{suffix}", False, status)
        elif status == 200:
            key_health.record(f"fabric_suspend:{suffix}", True)


def _request(capacity: Capacity, action: Action, hours: int | None = None) -> httpx.Request:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if action in ("suspend", "keepawake"):
        headers[SUSPEND_KEY_HEADER] = capacity.suspend_key
    params: dict[str, str] = {"format": "json", "action": action}
    if action == "keepawake":
        params["hours"] = str(hours)
    url = httpx.URL(capacity.wake_url).copy_merge_params(params)
    return httpx.Request(
        "GET",
        url,
        headers=headers,
        extensions={"timeout": httpx.Timeout(REQUEST_TIMEOUT_S).as_dict()},
    )


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


# name -> (event loop, lock). The Logic App cannot serialize its runs (it refuses
# trigger concurrency on a workflow that answers with a synchronous Response), so
# two keepawake runs that overlap could both read KeepAwakeUntil before either
# writes and shorten the hold. This server is the only caller holding the key, so
# it sends one action per capacity at a time.
_action_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _action_lock(name: str) -> asyncio.Lock:
    """The capacity's action lock; rebuilt on a new event loop, like the transport."""
    loop = asyncio.get_running_loop()
    held = _action_locks.get(name)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _action_locks[name] = held
    return held[1]


def _state_of(payload: dict[str, Any]) -> str:
    state = payload.get("state")
    return state if isinstance(state, str) and _STATE.match(state) else "Unknown"


def _text(value: object, shape: re.Pattern[str]) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if shape.match(value) else None


def _hourly(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number < _HOURLY_MAX else None


def _whole_hours(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    number = int(value)
    return number if number in _MAX_HOURS_RANGE else None


# The Logic App writes keepAwakeSince and quietSince as .NET's round-trip text,
# seven fractional digits ("2026-10-17T01:20:05.2616188Z"); fromisoformat
# before Python 3.11 rejects more than six, so the tail is cut first.
_LONG_FRACTION = re.compile(r"(\.\d{6})\d+")


def _instant(value: object) -> datetime | None:
    """An ISO 8601 time as an aware UTC datetime; a time with no offset is UTC."""
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    try:
        parsed = datetime.fromisoformat(_LONG_FRACTION.sub(r"\1", value.strip()))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso(moment: datetime | None) -> str | None:
    """*moment* as ``2026-10-16T08:00:00Z``, or ``None``."""
    return None if moment is None else moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _capacity_id(value: object) -> str | None:
    """An ARM capacity id in canonical form (see :data:`_CAPACITY_ID`), or ``None``.

    Azure hands ids back in mixed case (``resourcegroups``, an upper-case
    guid), which the host's link allowlist would refuse; the structural
    segments are rewritten, the guid lower-cased, the resource group and the
    name kept as given. A resource group of dots is a path step: ``None``.
    """
    if not isinstance(value, str):
        return None
    found = _CAPACITY_ID.match(value.strip())
    if found is None or re.fullmatch(r"\.+", found["rg"]):
        return None
    return (
        f"/subscriptions/{found['sub'].lower()}/resourceGroups/{found['rg']}"
        f"/providers/Microsoft.Fabric/capacities/{found['name']}"
    )


# The optional fields of an answer, in the order _details reads them.
_DETAIL_KEYS = (
    "sku",
    "region",
    "capacityId",
    "hourlyUsd",
    "resumedAt",
    "keepAwakeSince",
    "keepAwakeUntil",
    "quietSince",
    "maxHours",
    "nextCheckAt",
)


def _details(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The optional probe fields (module note), each checked or null; times as datetimes."""
    return {
        "sku": _text(payload.get("sku"), _SKU),
        "region": _text(payload.get("region"), _REGION),
        "capacityId": _capacity_id(payload.get("capacityId")),
        "hourlyUsd": _hourly(payload.get("hourlyUsd")),
        "resumedAt": _instant(payload.get("resumedAt")),
        "keepAwakeSince": _instant(payload.get("keepAwakeSince")),
        "keepAwakeUntil": _instant(payload.get("keepAwakeUntil")),
        "quietSince": _instant(payload.get("quietSince")),
        "maxHours": _whole_hours(payload.get("maxHours")),
        "nextCheckAt": _instant(payload.get("nextCheckAt")),
    }


def _first_run(next_check: datetime, at: datetime) -> datetime:
    """The first hourly runbook run at or after *at*; *next_check* is the next one."""
    if at <= next_check:
        return next_check
    behind = (at - next_check) / _HOUR
    return next_check + math.ceil(behind) * _HOUR


def sleeps_at(details: Mapping[str, Any], now: datetime) -> datetime | None:
    """When the hourly runbook is expected to suspend an idle Active capacity.

    The runbook's rule (agents-and-automation, 2026-10-16, third pass): while
    now is before ``keepAwakeUntil`` it does nothing. Otherwise the window
    starts at the later of ``resumedAt`` and ``keepAwakeSince`` (either alone
    when the other is null; neither means 30 days back, as the runbook
    assumes), nothing happens until the window start plus ``maxHours``, and
    then it suspends on the second quiet run in a row, ``quietSince`` marking
    the first. So decisions start at the later of the window start plus
    ``maxHours`` and ``keepAwakeUntil``. Runs come hourly on ``nextCheckAt``'s
    minute, and a decision time up to :data:`_RUN_SLACK` after a run is caught
    by that run. The first deciding run marks quiet and the next one
    suspends, unless a mark is already there: it counts only when it is at or
    after the decision time and the run that reads it comes 45 to 100 minutes
    later (the runbook's gaps); a mark too recent waits for the run after, and
    one too old starts the count over. ``None`` without ``maxHours`` or
    ``nextCheckAt``. It assumes the capacity stays idle; use keeps it up.
    """
    max_hours = details.get("maxHours")
    next_check = details.get("nextCheckAt")
    if max_hours is None or next_check is None:
        return None
    starts = [
        moment
        for moment in (details.get("resumedAt"), details.get("keepAwakeSince"))
        if moment is not None
    ]
    window = max(starts) if starts else now - _NO_RESUME_LOOKBACK
    decides = window + timedelta(hours=max_hours)
    until = details.get("keepAwakeUntil")
    if until is not None and until > decides:
        decides = until
    first = _first_run(next_check, max(now, decides - _RUN_SLACK))
    # The run that marks the first quiet hour; the suspend comes a run later.
    marks = first
    quiet = details.get("quietSince")
    if quiet is not None and quiet >= decides:
        gap = first - quiet
        if _MIN_QUIET_GAP <= gap <= _MAX_QUIET_GAP:
            return first
        if gap < _MIN_QUIET_GAP:
            # Too recent for the first run (a manual start): that run keeps
            # the mark, and the run after counts it if it is not too old by then.
            if gap + _HOUR <= _MAX_QUIET_GAP:
                return first + _HOUR
            marks = first + _HOUR
    return marks + _HOUR


def keep_awake_options(details: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    """What each of :data:`KEEP_AWAKE_CHOICES` would do, from :func:`sleeps_at`.

    Each choice as if the Logic App had just set ``keepAwakeUntil`` to the
    later of its value and now plus the hours, and cleared ``quietSince``
    (its contract). ``addsTime`` is false only when the new sleep time is no
    later than the current one; with no sleep time to compare, it is true.
    """
    current = sleeps_at(details, now)
    held = details.get("keepAwakeUntil")
    options: list[dict[str, Any]] = []
    for hours in KEEP_AWAKE_CHOICES:
        until = now + timedelta(hours=hours)
        if held is not None and held > until:
            until = held
        sleeps = sleeps_at({**details, "keepAwakeUntil": until, "quietSince": None}, now)
        adds = sleeps is None or current is None or sleeps > current
        options.append({"hours": hours, "sleepsAt": iso(sleeps), "addsTime": adds})
    return options


def _now() -> datetime:
    return datetime.now(UTC)


def _body_error(body: bytes, holder: Capacity | Fleet) -> str | None:
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        error = error.get("message")
    if not isinstance(error, str) or not error.strip():
        return None
    return _scrub(error.strip(), holder)[:_ERROR_TEXT_MAX]


_WHAT = {"probe": "status", "keepawake": "keep-awake"}


async def call(
    capacity: Capacity,
    action: Action,
    *,
    hours: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Send *action* to the capacity's Logic App and read its answer.

    :param hours: For ``keepawake`` only, 1 to 12 (the router checks first).
    :returns: ``{"name", "state", "action", "changed", "details", "sent"}``;
        ``details`` holds the optional fields (:func:`_details`), times as
        datetimes, and ``sent`` names the ones the answer carried at all.
    :raises FabricError: On a timeout (504), an unreachable Logic App, a
        non-200 answer or a body that is not the contract's JSON (502).
    """
    sender = transport if transport is not None else _shared_transport()
    what = _WHAT.get(action, action)
    if action == "keepawake" and not (
        type(hours) is int and KEEP_AWAKE_HOURS_MIN <= hours <= KEEP_AWAKE_HOURS_MAX
    ):
        raise FabricError(
            f"{capacity.name}: keep awake takes whole hours from {KEEP_AWAKE_HOURS_MIN} to "
            f"{KEEP_AWAKE_HOURS_MAX}.",
            code=CODE_BAD_HOURS,
            status=400,
        )
    try:
        request = _request(capacity, action, hours)
    except Exception as exc:  # noqa: BLE001 - httpx.InvalidURL and anything else while building
        # The exception text can carry the URL, so only its class is named.
        raise FabricError(
            f"{capacity.name}: the {what} request could not be built "
            f"({type(exc).__name__}); check AE_FABRIC_WAKE_URL_{capacity.env_suffix}."
        ) from None
    try:
        # One deadline over the whole exchange: httpx's own timeout is per phase.
        async with asyncio.timeout(REQUEST_TIMEOUT_S):
            status, body = await _send(sender, request)
    except (httpx.TimeoutException, TimeoutError):
        hint = (
            "; it may still be under way, so look again in a minute before trying again"
            if action != "probe"
            else ""
        )
        raise FabricError(
            f"{capacity.name}: the Logic App did not answer the {what} call within "
            f"{REQUEST_TIMEOUT_S:g} s{hint}.",
            code=CODE_TIMEOUT,
            status=504,
        ) from None
    except httpx.HTTPError as exc:
        raise FabricError(
            f"{capacity.name}: the Logic App could not be reached for the {what} call "
            f"({type(exc).__name__})."
        ) from None
    _note_capacity(capacity, action, status)
    if status != 200:
        detail = _body_error(body, capacity)
        if status == 403 and action in ("suspend", "keepawake"):
            message = (
                f"{capacity.name}: the Logic App refused the suspend key (403) on the {what} "
                f"call. The server's {SUSPEND_KEY_PREFIX}{capacity.env_suffix} must match "
                "the key the Logic App checks; fix it and restart the stack."
            )
        elif status == 400 and action == "keepawake":
            message = (
                f"{capacity.name}: the Logic App refused the keep-awake hours (400)"
                + (f": {detail}" if detail else ".")
                + f" It takes whole hours from {KEEP_AWAKE_HOURS_MIN} to "
                f"{KEEP_AWAKE_HOURS_MAX}."
            )
        else:
            message = f"{capacity.name}: the Logic App answered {status} to the {what} call"
            message += f": {detail}" if detail else "."
        raise FabricError(message)
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise FabricError(
            f"{capacity.name}: the Logic App's {what} answer was not the expected JSON."
        )
    return {
        "name": capacity.name,
        "state": _state_of(payload),
        "action": "none" if action == "probe" else action,
        "changed": payload.get("changed") is True,
        "details": _details(payload),
        "sent": frozenset(key for key in _DETAIL_KEYS if key in payload),
    }


# What GET /v1/ae/fabric passes through per capacity (module note); times as ISO.
_PASSED = (
    "sku",
    "region",
    "capacityId",
    "hourlyUsd",
    "resumedAt",
    "keepAwakeUntil",
    "maxHours",
)


def _blank() -> dict[str, Any]:
    return dict.fromkeys(_DETAIL_KEYS)


def _public(capacity: Capacity, state: str, details: Mapping[str, Any]) -> dict[str, Any]:
    """One capacity's row as ``GET /v1/ae/fabric`` shows it (module note, third pass)."""
    return _row(
        capacity.name,
        state,
        details,
        can_suspend=capacity.can_suspend,
        can_keep_awake=capacity.can_keep_awake,
        runbook=True,
    )


def _row(
    name: str,
    state: str,
    details: Mapping[str, Any],
    *,
    can_suspend: bool,
    can_keep_awake: bool,
    runbook: bool,
) -> dict[str, Any]:
    """A row in ``GET``'s shape. Without *runbook* (a capacity nothing suspends on
    its own) there is no sleep time, keep awake or minimum wake bill."""
    now = _now()
    active = state == "Active"
    shown: dict[str, Any] = {
        "name": name,
        "can_suspend": can_suspend,
        "can_keep_awake": can_keep_awake,
        "state": state,
    }
    for key in _PASSED:
        value = details.get(key)
        shown[key] = iso(value) if isinstance(value, datetime) else value
    shown["sleepsAt"] = iso(sleeps_at(details, now)) if active and runbook else None
    shown["keepAwakeOptions"] = (
        keep_awake_options(details, now) if active and runbook and can_keep_awake else []
    )
    # A wake bills the window and the second quiet run at least.
    max_hours = details.get("maxHours")
    shown["wakeBilledHours"] = (
        (max_hours if max_hours is not None else DEFAULT_MAX_HOURS) + 1 if runbook else None
    )
    return shown


# name -> (expires at, state, details), monotonic clock.
_status_cache: dict[str, tuple[float, str, dict[str, Any]]] = {}
# name -> the last details read or acted on, kept past forget(): an action's
# answer carries little more than the state, and its row is drawn from these.
_known: dict[str, dict[str, Any]] = {}
# name -> how many times forget() ran; a probe that began before a bump is stale.
_generation: dict[str, int] = {}
# The fleet's last list: (expires at, rows, subscriptions it could not read).
_fleet_cache: tuple[float, list[dict[str, Any]], int] | None = None
# Bumped by every forget(); a list call that began before a bump is stale.
_fleet_generation = 0
# The last rows any fleet answer carried, kept past forget(name): a write finds
# its capacity's ARM id here instead of paying for one more list call.
_fleet_known: list[dict[str, Any]] = []


# The last whole GET answer: (taken at, answer), monotonic clock. Served at once
# at any age; forget() drops it, so an action is never followed by a stale card.
_last: tuple[float, dict[str, Any]] | None = None
# Bumped by every forget(); a refresh that began before a bump is stale.
_last_generation = 0
# The one background refresh in flight, if any.
_refresh_task: asyncio.Task[None] | None = None


def forget(name: str | None = None) -> None:
    """Drop one capacity's cached status (after a wake or a suspend), or all of them.

    The last known details stay (:data:`_known`) unless every capacity goes.
    """
    global _fleet_cache, _fleet_generation, _last, _last_generation, _refresh_task
    _last = None
    # An older refresh may finish, but its write is dropped (generation); let a new one start.
    _refresh_task = None
    _last_generation += 1
    # Any action changes the fleet's list, and forget() runs after every one.
    _fleet_generation += 1
    _fleet_cache = None
    if name is None:
        for known in set(_status_cache) | set(_generation):
            _generation[known] = _generation.get(known, 0) + 1
        _status_cache.clear()
        _known.clear()
        _fleet_known.clear()
    else:
        _generation[name] = _generation.get(name, 0) + 1
        _status_cache.pop(name, None)


async def _probe(
    capacity: Capacity, transport: httpx.AsyncBaseTransport | None
) -> tuple[str, dict[str, Any]]:
    """The capacity's state and details through its own Logic App, cached; raises FabricError."""
    held = _status_cache.get(capacity.name)
    if held is not None and held[0] > time.monotonic():
        return held[1], held[2]
    generation = _generation.get(capacity.name, 0)
    answer = await call(capacity, "probe", transport=transport)
    # A wake or suspend landed while this probe was out: its answer predates it.
    if _generation.get(capacity.name, 0) == generation:
        _status_cache[capacity.name] = (
            time.monotonic() + STATUS_TTL_S,
            answer["state"],
            answer["details"],
        )
        _known[capacity.name] = answer["details"]
    return answer["state"], answer["details"]


def _failed_row(capacity: Capacity, exc: FabricError) -> dict[str, Any]:
    return {
        "name": capacity.name,
        "can_suspend": capacity.can_suspend,
        "can_keep_awake": capacity.can_keep_awake,
        "state": "Unknown",
        "error": str(exc),
    }


async def status(
    capacity: Capacity, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """One capacity's row for ``GET /v1/ae/fabric``; a failure is the row's ``error``."""
    try:
        state, details = await _probe(capacity, transport)
    except FabricError as exc:
        _logger.warning("fabric status failed: %s", exc)
        return _failed_row(capacity, exc)
    return _public(capacity, state, details)


async def _live_overview(
    env: Mapping[str, str], transport: httpx.AsyncBaseTransport | None
) -> dict[str, Any]:
    shared = fleet(env)
    if shared is not None:
        return await _fleet_overview(shared, env, transport)
    found = list(capacities(env).values())
    rows = await asyncio.gather(*(status(item, transport=transport) for item in found))
    answer: dict[str, Any] = {"configured": bool(found), "capacities": list(rows)}
    if not found:
        answer["note"] = HOW_TO_ENABLE
    return answer


def _row_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return str(row.get("name", "")).lower(), str(row.get("capacityId") or "")


def _has_error(answer: Mapping[str, Any]) -> bool:
    """Whether a read failed in *answer*: asked again on the next poll, not after the TTL."""
    return "fleetError" in answer or any(row.get("error") for row in answer.get("capacities", []))


def _keep_last_rows(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """*new*, where a failed read keeps the rows the card already showed.

    A row that came back with an ``error`` keeps its last good fields (the
    error is shown on it), through any number of failed reads in a row; a fleet
    list that failed and left fewer rows than before keeps the old ones, with
    the new ``fleetError`` on top. A failed probe's row carries no
    ``capacityId`` (:func:`_failed_row`), so it falls back to the name when
    exactly one earlier row has it (review of 2026-10-22).
    """
    old_rows = old.get("capacities", [])
    before = {_row_key(row): row for row in old_rows}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in old_rows:
        by_name.setdefault(_row_key(row)[0], []).append(row)
    rows = []
    for row in new.get("capacities", []):
        last = before.get(_row_key(row))
        if last is None and not row.get("capacityId"):
            named = by_name.get(_row_key(row)[0], [])
            last = named[0] if len(named) == 1 else None
        if row.get("error") and last is not None:
            row = {**last, "error": row["error"]}
        rows.append(row)
    if "fleetError" in new and len(rows) < len(old.get("capacities", [])):
        rows = list(old["capacities"])
    return {**new, "capacities": rows}


def _store(generation: int, answer: dict[str, Any]) -> None:
    """Keep *answer* as the last one, unless an action landed since *generation*."""
    global _last
    if _last_generation != generation:
        return
    if _last is not None:
        answer = _keep_last_rows(_last[1], answer)
    _last = (time.monotonic(), answer)


async def _refresh(
    env: Mapping[str, str], transport: httpx.AsyncBaseTransport | None, generation: int
) -> None:
    try:
        _store(generation, await _live_overview(env, transport))
    except Exception as exc:  # noqa: BLE001 (a bug here must be logged, never stick the cache)
        # Never the message: it could echo a Logic App URL.
        _logger.warning("fabric background refresh failed unexpectedly: %s", type(exc).__name__)


def _begin_refresh(env: Mapping[str, str], transport: httpx.AsyncBaseTransport | None) -> None:
    global _refresh_task
    if _refresh_task is None or _refresh_task.done():
        _refresh_task = asyncio.get_running_loop().create_task(
            _refresh(dict(env), transport, _last_generation)
        )


def refresh_task() -> asyncio.Task[None] | None:
    return _refresh_task


async def overview(
    environ: Mapping[str, str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """``GET /v1/ae/fabric``: every configured capacity's state, each isolated.

    Answers the last known result at once; one background refresh starts when
    it is older than :data:`STATUS_TTL_S`. With nothing kept (first call, or
    after :func:`forget`) it waits for the live answer.
    """
    env = os.environ if environ is None else environ
    held = _last
    if held is not None:
        if time.monotonic() - held[0] >= STATUS_TTL_S or _has_error(held[1]):
            _begin_refresh(env, transport)
        return held[1]
    generation = _last_generation
    answer = await _live_overview(env, transport)
    _store(generation, answer)
    return answer


async def act(
    capacity: Capacity,
    action: Literal["wake", "suspend", "keepawake"],
    *,
    hours: int | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Wake, suspend or keep *capacity* awake, one action at a time; never retried.

    The cached status goes either way.

    Answers the capacity's full row, as ``GET`` shows it, plus ``action`` and
    ``changed``: the details the Logic App's answer carried, over the last
    ones read (:data:`_known`). Keep awake also clears ``quietSince``, as the
    Logic App does, so the row's sleep time and options are the new ones.
    With the fleet set up, a keep awake answers the fleet's row for the
    capacity (:func:`_fleet_shaped`), so the page keeps the fleet's SKU and price.
    """
    async with _action_lock(capacity.name):
        try:
            answer = await call(capacity, action, hours=hours, transport=transport)
        finally:
            forget(capacity.name)
    details = {**_blank(), **_known.get(capacity.name, {})}
    for key in answer["sent"]:
        details[key] = answer["details"][key]
    if action == "keepawake":
        details["quietSince"] = None
    _known[capacity.name] = details
    shown = (
        _fleet_shaped(capacity, answer["state"], details, environ)
        if action == "keepawake"
        else None
    )
    return {
        **(shown if shown is not None else _public(capacity, answer["state"], details)),
        "action": answer["action"],
        "changed": answer["changed"],
    }


def _fleet_shaped(
    capacity: Capacity,
    state: str,
    details: Mapping[str, Any],
    environ: Mapping[str, str] | None,
) -> dict[str, Any] | None:
    """*capacity*'s row as the fleet's ``GET`` shows it, over a keep awake's *details*.

    The fleet's SKU, region, id, price, subscription, resource group and resize
    choices win, so the page's row keeps them; the state is the keep awake's
    own (it is the newer read) unless that is Unknown. ``None`` without the
    fleet, or when the last list has no one row of that name.
    """
    env = os.environ if environ is None else environ
    if fleet(env) is None:
        return None
    rows = [row for row in _fleet_known if str(row["name"]).lower() == capacity.name]
    if len(rows) != 1:
        return None
    row = {**rows[0], "unread": False}
    if state != "Unknown":
        row["state"] = state
    return _fleet_public(row, capacity, details, usd_per_cu_hour(env))


# ── the capacity fleet (module note, 2026-10-21) ─────────────────────────────


def _fleet_row(item: object) -> dict[str, Any] | None:
    """One capacity from the fleet's answer, each field shape-checked; no name, no row."""
    if not isinstance(item, Mapping):
        return None
    name = _text(item.get("name"), _CAPACITY_NAME)
    if name is None:
        return None
    return {
        "name": name,
        "capacityId": _capacity_id(item.get("capacityId")),
        "subscriptionName": _text(item.get("subscriptionName"), _SUBSCRIPTION_NAME),
        "resourceGroup": _text(item.get("resourceGroup"), _RESOURCE_GROUP),
        "region": _text(item.get("region"), _REGION),
        "sku": _text(item.get("sku"), _SKU),
        "state": _state_of(dict(item)),
    }


async def fleet_call(
    shared: Fleet,
    action: Literal["list", "resume", "suspend", "scale"],
    *,
    capacity_id: str | None = None,
    sku: str | None = None,
    label: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """POST *action* to the shared capacities Logic App and read its answer.

    :param label: The capacity's name, for the error sentences of a write.
    :returns: ``{"action", "changed", "capacities", "capacity", "errors",
        "unread"}``: the capacities the answer carried (:func:`_fleet_row`;
        the full list for ``list``, only the target for a write), the one the
        action targeted, how many subscriptions the list could not read, and
        those subscriptions' ids, lower-cased (never shown; they only match
        rows to :func:`_fleet_list`'s last known ones).
    :raises FabricError: On a timeout (504), a capacity the fleet does not
        find (404), a size it refuses (400), anything else (502). No message
        carries the URL or the key. ``unsent`` is set when nothing can have
        been written: any failed ``list``, a refused key (403, checked before
        anything else) and a 404.
    """
    sender = transport if transport is not None else _shared_transport()
    what = _FLEET_WHAT[action]
    who = f"{label}: " if label else ""
    reading = action == "list"
    # The list keeps the read deadline; a write gets its own (FLEET_WRITE_TIMEOUT_S).
    deadline = REQUEST_TIMEOUT_S if reading else FLEET_WRITE_TIMEOUT_S
    body: dict[str, str] = {"action": action}
    if capacity_id is not None:
        body["capacityId"] = capacity_id
    if sku is not None:
        body["sku"] = sku
    try:
        request = httpx.Request(
            "POST",
            shared.url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                CAPACITY_KEY_HEADER: shared.key,
            },
            content=json.dumps(body).encode(),
            extensions={"timeout": httpx.Timeout(deadline).as_dict()},
        )
    except Exception as exc:  # noqa: BLE001 - httpx.InvalidURL and anything else while building
        raise FabricError(
            f"{who}the capacities {what} request could not be built "
            f"({type(exc).__name__}); check {FLEET_URL_ENV}.",
            unsent=True,
        ) from None
    try:
        async with asyncio.timeout(deadline):
            status_code, raw = await _send(sender, request)
    except (httpx.TimeoutException, TimeoutError):
        hint = (
            ""
            if reading
            else "; it may still be under way, so look again in a minute before trying again"
        )
        raise FabricError(
            f"{who}the capacities Logic App did not answer the {what} call within "
            f"{deadline:g} s{hint}.",
            code=CODE_TIMEOUT,
            status=504,
            unsent=reading,
        ) from None
    except httpx.HTTPError as exc:
        raise FabricError(
            f"{who}the capacities Logic App could not be reached for the {what} call "
            f"({type(exc).__name__}).",
            unsent=reading,
        ) from None
    if status_code == 403:
        key_health.record("fabric_fleet", False, status_code)
    elif status_code == 200:
        key_health.record("fabric_fleet", True)
    if status_code != 200:
        detail = _body_error(raw, shared)
        if status_code == 403:
            raise FabricError(
                f"{who}the capacities Logic App refused the capacity key (403) on the {what} "
                f"call. The server's {FLEET_KEY_ENV} must match the key the Logic App checks "
                "(Key Vault fabric-capacities-key); fix it and restart the stack.",
                unsent=True,
            )
        if status_code == 404 and not reading:
            raise FabricError(
                f"{who}the capacities Logic App could not find this capacity (404); "
                "read the page again.",
                code=CODE_NOT_FOUND,
                status=404,
                unsent=True,
            )
        if status_code == 400 and action == "scale":
            raise FabricError(
                f"{who}the capacities Logic App refused the size (400)"
                + (f": {detail}" if detail else "."),
                code=CODE_BAD_SKU,
                status=400,
            )
        message = f"{who}the capacities Logic App answered {status_code} to the {what} call"
        raise FabricError(message + (f": {detail}" if detail else "."), unsent=reading)
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    listed = payload.get("capacities") if isinstance(payload, dict) else None
    if not isinstance(payload, dict) or not isinstance(listed, list):
        raise FabricError(
            f"{who}the capacities Logic App's {what} answer was not the expected JSON.",
            unsent=reading,
        )
    rows = [row for row in map(_fleet_row, listed) if row is not None]
    errors = payload.get("errors")
    errors = errors if isinstance(errors, list) else []
    return {
        "action": action,
        "changed": payload.get("changed") is True,
        "capacities": rows,
        "capacity": _fleet_row(payload.get("capacity")),
        "errors": len(errors),
        "unread": frozenset(
            item.strip().lower()
            for item in errors
            if isinstance(item, str) and re.fullmatch(_GUID, item.strip())
        ),
    }


UNREAD_SUBSCRIPTION = (
    "Azure did not answer for this capacity's subscription; showing the last known state."
)


def _subscription_of(row: Mapping[str, Any]) -> str | None:
    """The lower-case subscription id in *row*'s canonical ``capacityId``, or ``None``."""
    found = _CAPACITY_ID.match(row.get("capacityId") or "")
    return found["sub"].lower() if found else None


def _remember_fleet(rows: list[dict[str, Any]]) -> None:
    _fleet_known[:] = rows


def _remember_fleet_row(row: dict[str, Any]) -> None:
    """Put one write's answered row in :data:`_fleet_known`, the others kept.

    A write answers only its own capacity (2026-10-21, the Logic App no longer
    lists on a write), so the known list is updated in place, never replaced.
    """
    for index, known in enumerate(_fleet_known):
        if known.get("capacityId") == row.get("capacityId"):
            _fleet_known[index] = row
            return
    _fleet_known.append(row)


async def _fleet_list(
    shared: Fleet, transport: httpx.AsyncBaseTransport | None
) -> tuple[list[dict[str, Any]], int]:
    """The fleet's list, cached :data:`STATUS_TTL_S`; raises FabricError.

    A capacity whose subscription Azure did not answer for this time keeps its
    last known row, marked ``unread``, instead of vanishing from the card.
    """
    global _fleet_cache
    held = _fleet_cache
    if held is not None and held[0] > time.monotonic():
        return held[1], held[2]
    generation = _fleet_generation
    answer = await fleet_call(shared, "list", transport=transport)
    rows = list(answer["capacities"])
    if answer["unread"]:
        listed = {row.get("capacityId") for row in rows}
        rows += [
            {**known, "unread": True}
            for known in _fleet_known
            if _subscription_of(known) in answer["unread"]
            and known.get("capacityId") not in listed
        ]
    # An action landed while this list was out: its answer predates it.
    if _fleet_generation == generation:
        _fleet_cache = (time.monotonic() + STATUS_TTL_S, rows, answer["errors"])
        _remember_fleet(rows)
    return rows, answer["errors"]


def _fleet_public(
    row: Mapping[str, Any],
    capacity: Capacity | None,
    details: Mapping[str, Any] | None,
    rate: float,
    *,
    fresh: bool = True,
) -> dict[str, Any]:
    """A fleet row as ``GET`` shows it, with its wake Logic App's *details* when it has them.

    The fleet's state, SKU, region and id win over the details; the price
    comes from the SKU. Pause and Start go through the fleet, so every row
    can suspend. A capacity with no wake Logic App has no runbook: no sleep
    time, keep awake or minimum wake bill. Without details, or with only the
    last good ones (*fresh* false: this probe failed or timed out), there is
    no keep awake either.
    """
    merged = {**_blank(), **(details or {})}
    merged["sku"] = row.get("sku")
    merged["region"] = row.get("region") or merged.get("region")
    merged["capacityId"] = row.get("capacityId")
    merged["hourlyUsd"] = hourly_for(row.get("sku"), rate)
    shown = _row(
        row["name"],
        row["state"],
        merged,
        can_suspend=True,
        can_keep_awake=(
            capacity is not None and capacity.can_keep_awake and details is not None and fresh
        ),
        runbook=capacity is not None,
    )
    shown["subscriptionName"] = row.get("subscriptionName")
    shown["resourceGroup"] = row.get("resourceGroup")
    shown["canScale"] = row.get("capacityId") is not None
    shown["skuChoices"] = list(SKU_CHOICES)
    if row.get("unread"):
        shown["error"] = UNREAD_SUBSCRIPTION
    return shown


def _wake_capacity(
    rows: list[dict[str, Any]], row: Mapping[str, Any], found: Mapping[str, Capacity]
) -> Capacity | None:
    """The wake Logic App for *row*, unless another row shares its name (never guessed)."""
    key = str(row["name"]).lower()
    if sum(1 for item in rows if str(item["name"]).lower() == key) != 1:
        return None
    return found.get(key)


async def _fleet_overview(
    shared: Fleet, env: Mapping[str, str], transport: httpx.AsyncBaseTransport | None
) -> dict[str, Any]:
    """``GET`` with the fleet: its list and every wake Logic App's probe, all at once."""
    found = capacities(env)
    rate = usd_per_cu_hour(env)

    async def listed() -> tuple[list[dict[str, Any]], int] | FabricError:
        try:
            return await _fleet_list(shared, transport)
        except FabricError as exc:
            _logger.warning("fabric fleet list failed: %s", exc)
            return exc

    async def probed(item: Capacity) -> tuple[str, dict[str, Any]] | FabricError:
        try:
            return await _probe(item, transport)
        except FabricError as exc:
            _logger.warning("fabric details failed: %s", exc)
            return exc

    fleet_answer, *probes = await asyncio.gather(
        listed(), *(probed(item) for item in found.values())
    )
    by_name = dict(zip(found, probes, strict=True))
    answer: dict[str, Any] = {"configured": True, "fleet": True}
    rows: list[dict[str, Any]] = []
    shown: list[dict[str, Any]] = []
    if isinstance(fleet_answer, FabricError):
        answer["fleetError"] = str(fleet_answer)
    else:
        rows, errors = fleet_answer
        if errors:
            answer["fleetError"] = (
                f"Azure did not answer for {errors} subscription"
                f"{'' if errors == 1 else 's'}; "
                "capacities there show their last known state, or are missing if this "
                "server has not listed them before."
            )
    for row in rows:
        capacity = _wake_capacity(rows, row, found)
        probe = by_name.get(capacity.name) if capacity is not None else None
        fresh = isinstance(probe, tuple)
        # A failed probe falls back to the last good details: the wake Logic App's
        # Automation reads stall now and then (module note, 2026-10-21), and the
        # runbook times barely move between two probes.
        details = probe[1] if fresh else _known.get(capacity.name) if capacity else None
        shown.append(_fleet_public(row, capacity, details, rate, fresh=fresh))
    # A wake Logic App's capacity the list missed (its subscription unread, or the
    # list failed) keeps today's row, so it never vanishes from the card.
    listed_names = {str(row["name"]).lower() for row in rows}
    for name, capacity in found.items():
        if name in listed_names:
            continue
        probe = by_name[name]
        shown.append(
            _failed_row(capacity, probe)
            if isinstance(probe, FabricError)
            else _public(capacity, *probe)
        )
    shown.sort(key=lambda item: (str(item["name"]).lower(), item.get("capacityId") or ""))
    answer["capacities"] = shown
    return answer


async def fleet_act(
    shared: Fleet,
    name: str,
    action: Literal["wake", "suspend", "scale"],
    *,
    sku: str | None = None,
    environ: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Start, pause or resize the capacity called *name* through the fleet; never retried.

    The SKU is checked before anything else. The ARM id comes from the last
    list (one more list call when there is none, or the name is not on it).
    Every failure before the write is sent is ``unsent`` (:class:`FabricError`),
    so the router may Start or Pause through the capacity's own wake Logic App.
    A name two listed capacities share is refused (409), never guessed.
    Answers the capacity's full row as ``GET`` shows it, plus ``action`` and
    ``changed``; the wake Logic App's last details ride along when it has one.
    """
    env = os.environ if environ is None else environ
    if action == "scale":
        sku = check_sku(name, sku)
    key = name.lower()

    def matching(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if str(row["name"]).lower() == key]

    rows = list(_fleet_known)
    if not matching(rows):
        rows, _errors = await _fleet_list(shared, transport)
    targets = matching(rows)
    if not targets:
        raise FabricError(
            f"No Fabric capacity is called {name!r} in the capacities list; read the page again.",
            code=CODE_NOT_FOUND,
            status=404,
            unsent=True,
        )
    if len(targets) > 1:
        raise FabricError(
            f"{len(targets)} Fabric capacities are called {targets[0]['name']}, so the server "
            "will not guess which one to change. Change it in the Azure portal, or rename one.",
            code=CODE_AMBIGUOUS,
            status=409,
        )
    target = targets[0]
    capacity_id = target.get("capacityId")
    if capacity_id is None:
        raise FabricError(
            f"{target['name']}: the capacities list gave no usable resource id for it, so it "
            "cannot be changed from here.",
            unsent=True,
        )
    sent: Literal["resume", "suspend", "scale"] = "resume" if action == "wake" else action
    async with _action_lock(key):
        try:
            answer = await fleet_call(
                shared,
                sent,
                capacity_id=capacity_id,
                sku=sku if action == "scale" else None,
                label=str(target["name"]),
                transport=transport,
            )
        finally:
            forget(key)
    row = answer["capacity"]
    if row is None:
        row = next(
            (item for item in answer["capacities"] if item.get("capacityId") == capacity_id),
            None,
        )
    if row is None:
        raise FabricError(
            f"{target['name']}: the capacities Logic App's answer did not include it; "
            "read the page again."
        )
    # A write answers its own row only: that row is updated, the rest kept.
    _remember_fleet_row(row)
    found = capacities(env)
    capacity = _wake_capacity(list(_fleet_known), row, found)
    details = _known.get(capacity.name) if capacity is not None else None
    return {
        # The details are the last ones read, not this action's: no keep awake.
        **_fleet_public(row, capacity, details, usd_per_cu_hour(env), fresh=False),
        "action": action,
        "changed": answer["changed"],
    }


__all__ = [
    "CAPACITY_KEY_HEADER",
    "CODE_AMBIGUOUS",
    "CODE_BAD_HOURS",
    "CODE_BAD_SKU",
    "CODE_FAILED",
    "CODE_FLEET_NOT_CONFIGURED",
    "CODE_NOT_FOUND",
    "CODE_SUSPEND_NOT_CONFIGURED",
    "CODE_TIMEOUT",
    "DEFAULT_MAX_HOURS",
    "DEFAULT_USD_PER_CU_HOUR",
    "FLEET_NOT_CONFIGURED",
    "FLEET_WRITE_TIMEOUT_S",
    "HOW_TO_ENABLE",
    "KEEP_AWAKE_CHOICES",
    "KEEP_AWAKE_HOURS_MAX",
    "KEEP_AWAKE_HOURS_MIN",
    "REQUEST_TIMEOUT_S",
    "SKU_CHOICES",
    "STATUS_TTL_S",
    "SUSPEND_KEY_HEADER",
    "UNREAD_SUBSCRIPTION",
    "Capacity",
    "FabricError",
    "Fleet",
    "act",
    "call",
    "capacities",
    "check_sku",
    "find",
    "fleet",
    "fleet_act",
    "fleet_call",
    "forget",
    "hourly_for",
    "iso",
    "keep_awake_options",
    "overview",
    "refresh_task",
    "sleeps_at",
    "status",
    "suspend_not_configured",
    "usd_per_cu_hour",
]
