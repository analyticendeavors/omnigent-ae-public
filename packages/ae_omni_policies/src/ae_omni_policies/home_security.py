"""``GET /v1/ae/home-security``: a compact read of the home app's Security tab (2026-11-08).

Why: a self-hosted home app shows the owner's network health, and will soon show a
firewall's status. The Admin Center's Home security card shows that status in one line and links
out when it needs a person. OmniGent only reads the home app's unauthenticated
``GET <base>/api/security?lite=1``; it never talks to the firewall and holds no firewall token.

The answer is a few labels and one count: whether the home app answered, whether its last scan is
stale, the scan time, the number of open alerts, the firewall's reachability as the home app saw
it, and two links. A link passes through only when it is a plain web link; the firewall portal
link only when it is https on a ``.firewalla.net`` host. Nothing from the home app's body is
passed on beyond those fields: never an alert's text, a device name or an address.

The read is cached for 60 s (a failed read too, so a dead home app is asked once a minute, not
once a request), has a 5 s timeout, and never raises: a failure is ``reachable: false``. Without
``AE_HOME_SECURITY_URL`` the card is off and the answer is ``{"off": true}``.

The request goes through its own transport with no client in between, as ``databricks.py``
does, so httpx writes no request line (and so no address) to the server log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from ae_omni_policies import card_config

URL_ENV = "AE_HOME_SECURITY_URL"
LINK_ENV = "AE_HOME_SECURITY_LINK"
CARD_ID = "home-security"
CACHE_S = 60.0
TIMEOUT_S = 5.0
MAX_BODY = 1_000_000
PORTAL_SUFFIX = ".firewalla.net"
API_STATES = ("up", "down", "unknown")

_cache: tuple[str, float, dict[str, Any]] | None = None
_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None


def reset() -> None:
    """Forget the cached answer (tests)."""
    global _cache, _lock
    _cache = None
    _lock = None


def _make_transport() -> httpx.AsyncBaseTransport:
    return httpx.AsyncHTTPTransport()


def _canonical(raw: object, *, extras: bool) -> str | None:
    """A canonical http(s) link for *raw*, or ``None`` (2026-11-08, review).

    Python's ``urlsplit`` and a browser read some strings differently (``https://a\\b.example``),
    so anything the two could disagree on is refused before parsing: backslashes, whitespace,
    control and non-ASCII characters, and any userinfo. The result is built from the parsed
    parts, so what is checked is what is returned. *extras* False refuses a query or fragment.
    """
    if not isinstance(raw, str) or not raw or len(raw) > 500 or not raw.isascii():
        return None
    if "\\" in raw or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in raw):
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host or "@" in parts.netloc:
        return None
    if not extras and (parts.query or parts.fragment):
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def web_link(raw: object) -> str | None:
    """*raw* as a canonical http or https link with a host, else ``None``."""
    return _canonical(raw, extras=True)


def portal_link(raw: object) -> str | None:
    """*raw* as a canonical https link to ``firewalla.net`` or a subdomain, else ``None``."""
    text = _canonical(raw, extras=True)
    if text is None:
        return None
    parts = urlsplit(text)
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host == "firewalla.net" or host.endswith(PORTAL_SUFFIX)):
        return None
    return text


def _base_url(raw: object) -> str | None:
    """The home app's base address: a canonical link with no query or fragment."""
    text = raw.strip() if isinstance(raw, str) else raw
    return _canonical(text, extras=False)


def _stamp(raw: object) -> str | None:
    """A short timestamp string as sent, or ``None``; the page parses it."""
    if isinstance(raw, str) and 0 < len(raw) <= 40:
        return raw
    return None


def _firewalla(health: object) -> dict[str, Any] | None:
    if not isinstance(health, Mapping):
        return None
    fw = health.get("firewalla")
    if not isinstance(fw, Mapping):
        return None
    api = fw.get("api")
    online = fw.get("boxOnline")
    return {
        "api": api if api in API_STATES else "unknown",
        "boxOnline": online if isinstance(online, bool) else None,
        "checkedAt": _stamp(fw.get("checkedAt")),
    }


ACTIONS = ("reviewed", "name", "revoke-key")


def _time(raw: object) -> float:
    if not isinstance(raw, str):
        return float("nan")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("nan")


def _hidden(alert: object, received: float) -> bool:
    """Whether the home app's own page hides *alert* (``security.js`` alertHidden).

    An alert whose reviewed request is waiting is hidden at once. A done request hides it only
    until a summary newer than the request arrives (``receivedAt`` after ``finishedAt``); from
    then the summary decides. A failed request hides nothing. As in the page, an unreadable
    ``finishedAt`` or ``receivedAt`` leaves a done request hiding.
    """
    if not isinstance(alert, Mapping) or not alert.get("id"):
        return False
    found: dict[str, Any] = {}
    requests = alert.get("requests")
    if isinstance(requests, Mapping):
        found.update({k: v for k, v in requests.items() if isinstance(v, Mapping)})
    latest = alert.get("request")
    if isinstance(latest, Mapping) and latest.get("action") not in found:
        found[str(latest.get("action"))] = latest
    reviewed = found.get("reviewed")
    if reviewed is not None and reviewed.get("status") == "requested":
        return True
    for action in ACTIONS:
        req = found.get(action)
        if req is None or req.get("status") != "done":
            continue
        finished = _time(req.get("finishedAt"))
        if not received > finished:
            return True
    return False


def visible_alerts(alerts: list[Any], received_at: object) -> int:
    """How many of *alerts* the home app's page shows."""
    received = _time(received_at)
    return sum(1 for alert in alerts if not _hidden(alert, received))


def shape(body: object, link: str | None, read_at: str) -> dict[str, Any]:
    """The compact answer from the home app's body; a body of the wrong shape is unreachable."""
    out: dict[str, Any] = {
        "reachable": False,
        "stale": False,
        "lastScan": None,
        "alerts": 0,
        "firewalla": None,
        "portalUrl": None,
        "link": link,
        "readAt": read_at,
    }
    if not isinstance(body, Mapping):
        return out
    if body.get("ok") is not True:
        return out
    out["reachable"] = True
    summary = body.get("summary")
    if not isinstance(summary, Mapping):
        # The home app answered but has no scan yet: say so as stale.
        out["stale"] = True
        return out
    out["stale"] = bool(body.get("stale"))
    out["lastScan"] = _stamp(summary.get("lastScan"))
    alerts = summary.get("alerts")
    out["alerts"] = (
        visible_alerts(alerts, body.get("receivedAt")) if isinstance(alerts, list) else 0
    )
    health = summary.get("health")
    out["firewalla"] = _firewalla(health)
    if isinstance(health, Mapping) and isinstance(health.get("firewalla"), Mapping):
        out["portalUrl"] = portal_link(health["firewalla"].get("portalUrl"))
    return out


async def _get_json(url: str, transport: httpx.AsyncBaseTransport) -> object:
    request = httpx.Request(
        "GET",
        url,
        headers={"accept": "application/json"},
        extensions={"timeout": httpx.Timeout(TIMEOUT_S).as_dict()},
    )
    response = await transport.handle_async_request(request)
    try:
        if response.status_code != 200:
            raise ValueError("unusable answer")
        raw = bytearray()
        async for chunk in response.aiter_bytes():
            raw += chunk
            if len(raw) > MAX_BODY:
                raise ValueError("answer too large")
    finally:
        await response.aclose()
    return json.loads(bytes(raw))


def request_url(base: str) -> str:
    """The lite security address under *base* (a canonical base with no query or fragment)."""
    parts = urlsplit(base)
    path = parts.path.rstrip("/") + "/api/security"
    return urlunsplit((parts.scheme, parts.netloc, path, "lite=1", ""))


async def read(base: str, link: str | None) -> dict[str, Any]:
    """One uncached read of the home app at *base*; never raises."""
    read_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = request_url(base)
    transport = _make_transport()
    try:
        body = await asyncio.wait_for(_get_json(url, transport), TIMEOUT_S + 1)
        return shape(body, link, read_at)
    except Exception:  # noqa: BLE001 (a failed read is "unreachable", never an error to the route)
        return shape(None, link, read_at)
    finally:
        with contextlib.suppress(Exception):  # closing a transport must not hide the answer
            await transport.aclose()


def _get_lock() -> asyncio.Lock:
    """The read lock for the running event loop (a lock is tied to the loop that contends it)."""
    global _lock
    loop = asyncio.get_running_loop()
    if _lock is None or _lock[0] is not loop:
        _lock = (loop, asyncio.Lock())
    return _lock[1]


def _fresh(base: str, link: str | None) -> dict[str, Any] | None:
    hit = _cache
    if hit is not None and hit[0] == base and hit[1] > time.monotonic():
        if hit[2].get("link") == link:
            return hit[2]
    return None


async def overview(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The card's answer: ``{"off": true}`` without a base URL, else the cached compact read.

    One read at a time: callers that arrive while one is running wait for it and share its answer.
    """
    global _cache
    base = _base_url(card_config.value(URL_ENV, environ))
    if base is None:
        return {"off": True, "card": CARD_ID}
    raw_link = card_config.value(LINK_ENV, environ)
    link = web_link(raw_link.strip() if raw_link else raw_link)
    if (hit := _fresh(base, link)) is not None:
        return hit
    async with _get_lock():
        if (hit := _fresh(base, link)) is not None:
            return hit
        answer = await read(base, link)
        _cache = (base, time.monotonic() + CACHE_S, answer)
        return answer
