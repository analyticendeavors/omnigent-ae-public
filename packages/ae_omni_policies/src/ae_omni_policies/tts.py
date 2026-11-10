"""Natural voices for Speak replies: one sentence in, mp3 out, through Azure Speech.

The web app asks ``POST /v1/ae/tts`` for each sentence and plays the audio in a queue. With
no key or region set the voice list is empty and the app keeps the device's own voices.
"""

from __future__ import annotations

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
MAX_TEXT = 600
RATES = (1.0, 1.25)
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

_transport: httpx.AsyncBaseTransport | None = None
_buckets: dict[str, tuple[float, float]] = {}
# Per user: when the first character of the window was charged, and characters spent since.
_budgets: dict[str, tuple[float, int]] = {}
_REGION_RE = re.compile(r"^[a-z0-9]+$")
# XML 1.0 forbids these; Azure answers 400 for the whole sentence if one slips in.
_XML_FORBIDDEN = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")


class TtsUnavailable(Exception):
    """Speech could not be produced; the message is safe to show (no key, no response body)."""


def reset(transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Empty the rate buckets; tests also set the transport (``None`` is the network)."""
    global _transport
    _buckets.clear()
    _budgets.clear()
    _transport = transport


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
    if rate == 1.25:
        body = f'<prosody rate="+25%">{body}</prosody>'
    return (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">'
        f'<voice name="{voice_id}">{body}</voice></speak>'
    )


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
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=_transport) as client:
            response = await client.post(
                url, headers=headers, content=ssml(text, voice_id, rate).encode("utf-8")
            )
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
        _logger.warning("speech request failed: %s", type(exc).__name__)
        raise TtsUnavailable("The speech service could not be reached.") from exc
    if response.status_code != 200:
        _logger.warning("speech request refused with status %s", response.status_code)
        raise TtsUnavailable(f"The speech service answered {response.status_code}.")
    return response.content
