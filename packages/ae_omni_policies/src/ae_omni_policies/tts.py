"""Natural voices for Speak replies: one sentence in, mp3 out, through Azure Speech.

The web app asks ``POST /v1/ae/tts`` for each sentence and plays the audio in a queue. With
no key or region set the voice list is empty and the app keeps the device's own voices.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import NamedTuple
from xml.sax.saxutils import escape

import httpx

_logger = logging.getLogger(__name__)

KEY_ENV = "AE_SPEECH_KEY"
REGION_ENV = "AE_SPEECH_REGION"
ROCKY_SOCKET_ENV = "AE_ROCKY_TTS_SOCKET"
MAX_TEXT = 600
RATES = (1.0, 1.25, 1.5)
RATE_LIMIT_PER_MINUTE = 120
MAX_BUCKETS = 256
IDLE_EVICT_S = 600.0
DAILY_CHAR_BUDGET = 200_000
BUDGET_WINDOW_S = 24 * 3600.0
TIMEOUT_S = 10.0
OUTPUT_FORMAT = "audio-24khz-48kbitrate-mono-mp3"


class Voice(NamedTuple):
    id: str
    name: str


VOICES: tuple[Voice, ...] = (
    Voice("en-US-AvaMultilingualNeural", "Ava"),
    Voice("en-US-AndrewMultilingualNeural", "Andrew"),
    Voice("en-US-EmmaMultilingualNeural", "Emma"),
    Voice("en-US-BrianMultilingualNeural", "Brian"),
)
ROCKY_VOICE = Voice("rocky-xtts", "Rocky")

_transport: httpx.AsyncBaseTransport | None = None
_rocky_transport: httpx.AsyncBaseTransport | None = None
# One Azure client for every sentence: keeping the connection warm skips a TLS handshake each time.
_client: httpx.AsyncClient | None = None
# The loop that made it: an AsyncClient's pooled connections belong to one event loop.
_client_loop: asyncio.AbstractEventLoop | None = None
# Close tasks still running, held so the loop cannot collect them half done.
_closing: set[asyncio.Task[None]] = set()
_CLIENT_LIMITS = httpx.Limits(
    max_connections=100, max_keepalive_connections=20, keepalive_expiry=60.0
)
_buckets: dict[str, tuple[float, float]] = {}
# Per user: when the first character of the window was charged, and characters spent since.
_budgets: dict[str, tuple[float, int]] = {}
_REGION_RE = re.compile(r"^[a-z0-9]+$")
# XML 1.0 forbids these; Azure answers 400 for the whole sentence if one slips in.
_XML_FORBIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


class TtsUnavailable(Exception):
    """Speech could not be produced; the message is safe to show (no key, no response body)."""


def reset(
    transport: httpx.AsyncBaseTransport | None = None,
    rocky_transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Empty the rate buckets; tests also set the transport (``None`` is the network)."""
    global _transport, _rocky_transport
    _drop_client()
    _buckets.clear()
    _budgets.clear()
    _transport = transport
    _rocky_transport = rocky_transport


def _drop_client(only: httpx.AsyncClient | None = None) -> None:
    """Forget the shared client and close it where its loop can still run the close.

    With ``only`` set, the drop happens only while that client is still the shared one: a
    request that failed on an old client must not close the newer one another request is using.

    On the client's own running loop the close is a task (kept in ``_closing``). From another
    thread it is handed to the client's loop when that loop still runs. When that loop is
    gone (a finished ``asyncio.run``) dropping is safe: the pool's sockets die with the loop
    and the garbage collector closes the rest; there is nothing left to await on.
    """
    global _client, _client_loop
    if only is not None and _client is not only:
        return
    old, loop = _client, _client_loop
    _client = _client_loop = None
    if old is None or old.is_closed or loop is None or loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        task = loop.create_task(old.aclose())
        _closing.add(task)
        task.add_done_callback(_closing.discard)
    elif loop.is_running():
        asyncio.run_coroutine_threadsafe(old.aclose(), loop)


def _key() -> str:
    return os.environ.get(KEY_ENV, "").strip()


def _region() -> str:
    return os.environ.get(REGION_ENV, "").strip()


def configured() -> bool:
    region = _region()
    if region and not _REGION_RE.match(region):
        # A region becomes part of the request host, so only a plain name is trusted.
        _logger.warning("%s is not a plain region name; natural voices are off.", REGION_ENV)
        return False
    return bool(_key() and region)


def voice_ids() -> frozenset[str]:
    return frozenset(v.id for v in VOICES)


def rocky_socket() -> str | None:
    path = os.environ.get(ROCKY_SOCKET_ENV, "").strip()
    return path if path.startswith("/") else None


def _rocky_client(timeout: float) -> httpx.AsyncClient:
    socket = rocky_socket()
    if socket is None:
        raise TtsUnavailable("Rocky voice is not set up on this server.")
    transport = _rocky_transport or httpx.AsyncHTTPTransport(uds=socket)
    return httpx.AsyncClient(transport=transport, timeout=timeout)


async def rocky_available() -> bool:
    if rocky_socket() is None:
        return False
    try:
        async with _rocky_client(2.0) as client:
            response = await client.get("http://rocky/health")
        body = response.json()
        return (
            response.status_code == 200 and isinstance(body, dict) and body.get("voice") == "rocky"
        )
    except (httpx.HTTPError, ValueError, TypeError):
        return False


async def synthesize_rocky(text: str, rate: float) -> bytes:
    try:
        async with _rocky_client(120.0) as client:
            response = await client.post(
                "http://rocky/synthesize", json={"text": text, "rate": rate}
            )
    except (httpx.HTTPError, ValueError) as exc:
        _logger.warning("rocky speech request failed: %s", type(exc).__name__)
        raise TtsUnavailable("The Rocky voice service could not be reached.") from exc
    if (
        response.status_code != 200
        or response.headers.get("content-type") != "audio/wav"
        or not response.content.startswith(b"RIFF")
    ):
        _logger.warning("rocky speech request refused with status %s", response.status_code)
        raise TtsUnavailable("The Rocky voice service could not make this sentence.")
    return response.content


def _make_room(entries: dict[str, tuple[float, float]], clock: float) -> None:
    """Keep ``entries`` under the cap: drop the long idle first, then the oldest."""
    if len(entries) < MAX_BUCKETS:
        return
    for who in [w for w, (_, stamp) in entries.items() if clock - stamp > IDLE_EVICT_S]:
        del entries[who]
    while len(entries) >= MAX_BUCKETS:
        del entries[min(entries, key=lambda w: entries[w][1])]


def take_token(user: str | None, now: float | None = None) -> bool:
    """Spend one call from the user's bucket (120 a minute, refilled continuously)."""
    clock = time.monotonic() if now is None else now
    who = user or "local"
    if who not in _buckets:
        _make_room(_buckets, clock)
    tokens, stamp = _buckets.get(who, (float(RATE_LIMIT_PER_MINUTE), clock))
    refill = (clock - stamp) * RATE_LIMIT_PER_MINUTE / 60
    tokens = min(float(RATE_LIMIT_PER_MINUTE), tokens + refill)
    if tokens < 1:
        _buckets[who] = (tokens, clock)
        return False
    _buckets[who] = (tokens - 1, clock)
    return True


def charge_chars(user: str | None, chars: int, now: float | None = None) -> bool:
    """Spend ``chars`` from the user's daily budget; False (nothing spent) when it would overrun.

    The window opens at the first charge and resets 24 hours later.
    """
    clock = time.monotonic() if now is None else now
    who = user or "local"
    start, spent = _budgets.get(who, (clock, 0))
    if clock - start >= BUDGET_WINDOW_S:
        start, spent = clock, 0
    if spent + chars > DAILY_CHAR_BUDGET:
        _budgets[who] = (start, spent)
        return False
    if who not in _budgets and len(_budgets) >= MAX_BUCKETS:
        for old in [w for w, (t, _) in _budgets.items() if clock - t >= BUDGET_WINDOW_S]:
            del _budgets[old]
        while len(_budgets) >= MAX_BUCKETS:
            del _budgets[min(_budgets, key=lambda w: _budgets[w][0])]
    _budgets[who] = (start, spent + chars)
    return True


def ssml(text: str, voice_id: str, rate: float) -> str:
    text = _XML_FORBIDDEN.sub("", text)
    body = escape(text, {'"': "&quot;", "'": "&apos;"})
    if rate in (1.25, 1.5):
        body = f'<prosody rate="+{round((rate - 1) * 100)}%">{body}</prosody>'
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">'
        f'<voice name="{voice_id}">{body}</voice></speak>'
    )


def _azure_client() -> httpx.AsyncClient:
    """The shared Azure client for the running loop, built on first use or after a loop change."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _drop_client()
        _client = httpx.AsyncClient(timeout=TIMEOUT_S, transport=_transport, limits=_CLIENT_LIMITS)
        _client_loop = loop
    return _client


async def synthesize(text: str, voice_id: str, rate: float) -> bytes:
    """The mp3 for one sentence; raises :class:`TtsUnavailable` on any failure."""
    if not configured():
        raise TtsUnavailable("Natural voices are not set up on this server.")
    url = f"https://{_region()}.tts.speech.microsoft.com/cognitiveservices/v1"
    headers = {
        "Ocp-Apim-Subscription-Key": _key(),
        "Content-Type": "application/ssml+xml",
        "X-Microsoft-OutputFormat": OUTPUT_FORMAT,
        "User-Agent": "omnigent-ae",
    }
    content = ssml(text, voice_id, rate).encode("utf-8")
    try:
        client = _azure_client()
        try:
            response = await client.post(url, headers=headers, content=content)
        except httpx.TransportError as exc:
            if isinstance(exc, httpx.TimeoutException):
                raise
            # A kept-alive connection the far side closed: rebuild the client and try once.
            _drop_client(client)
            client = _azure_client()
            try:
                response = await client.post(url, headers=headers, content=content)
            except httpx.TransportError as again:
                if not isinstance(again, httpx.TimeoutException):
                    _drop_client(client)
                raise
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, RuntimeError) as exc:
        _logger.warning("speech request failed: %s", type(exc).__name__)
        raise TtsUnavailable("The speech service could not be reached.") from exc
    if response.status_code != 200:
        _logger.warning("speech request refused with status %s", response.status_code)
        raise TtsUnavailable(f"The speech service answered {response.status_code}.")
    return response.content
