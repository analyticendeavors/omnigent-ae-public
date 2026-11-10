"""The picture a Social post shares (2026-11-07): fetched by the server, served in chunks.

The extension frame can show only ``data:`` and ``blob:`` images and cannot fetch, so the card
asks ``GET /v1/ae/social/visual/{slot}/{part}`` for the image in base64 pieces that each stay
under the bridge's 32,768-character cap, and builds a ``blob:`` URL from them. The server
fetches only from a short list of sources, never follows a redirect off them, keeps at most
:data:`MAX_BYTES`, and holds the last :data:`CACHE_ENTRIES` pictures for :data:`CACHE_TTL_S`.
"""

from __future__ import annotations

import base64
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 3 * 1024 * 1024
CHUNK_CHARS = 24_000
"""Base64 characters per part: a multiple of 4, so every part decodes on its own."""
CACHE_ENTRIES = 20
CACHE_TTL_S = 3600
FETCH_TIMEOUT_S = 15.0
MAX_REDIRECTS = 3
IMAGE_TYPES = ("image/jpeg", "image/png")
# The hosts a tool's picture may be fetched from: GitHub's social previews always, plus the
# comma-separated hosts in AE_SOCIAL_IMAGE_HOSTS. The owner's own site is no longer in code
# (2026-11-07, card setup): the public install carries none of the owner's names.
IMAGE_HOSTS_ENV = "AE_SOCIAL_IMAGE_HOSTS"
PUBLIC_IMAGE_HOSTS = ("opengraph.githubassets.com",)
YOUTUBE_HOST = "i.ytimg.com"
THUMBNAIL_NAMES = ("maxresdefault.jpg", "hqdefault.jpg")
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class VisualError(Exception):
    """A picture that cannot be served; ``status`` is the answer's HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Picture:
    type: str
    size: int
    b64: str

    @property
    def parts(self) -> int:
        return max(1, -(-len(self.b64) // CHUNK_CHARS))


def host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    """Whether *url* is plain https (no userinfo, no port) on one of *hosts*."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and parts.hostname in hosts
        and port is None
        and "@" not in parts.netloc
    )


def tool_image_hosts() -> tuple[str, ...]:
    """The hosts a tool's picture may come from, read from the environment at each call."""
    extra = tuple(
        host.strip().lower()
        for host in (os.environ.get(IMAGE_HOSTS_ENV) or "").split(",")
        if host.strip()
    )
    return PUBLIC_IMAGE_HOSTS + extra


def tool_image_ok(url: str | None) -> bool:
    """A tool's image link is one the server may fetch."""
    return bool(url) and host_allowed(url or "", tool_image_hosts())


def thumbnail_urls(video_id: str) -> list[str]:
    """The YouTube thumbnail links to try, largest first."""
    if not _VIDEO_ID.fullmatch(video_id):
        return []
    return [f"https://{YOUTUBE_HOST}/vi/{video_id}/{name}" for name in THUMBNAIL_NAMES]


# ── the cache ─────────────────────────────────────────────────────────────────────────────────

_cache: OrderedDict[str, tuple[float, Picture]] = OrderedDict()
_transport: httpx.AsyncBaseTransport | None = None


def reset_cache(transport: httpx.AsyncBaseTransport | None = None) -> None:
    """Empty the cache; tests also set the transport the fetch uses (``None`` is the network)."""
    global _transport
    _cache.clear()
    _transport = transport


def _cached(key: str, now: float) -> Picture | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    if now - entry[0] > CACHE_TTL_S:
        del _cache[key]
        return None
    _cache.move_to_end(key)
    return entry[1]


def _remember(key: str, picture: Picture, now: float) -> None:
    _cache[key] = (now, picture)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_ENTRIES:
        _cache.popitem(last=False)


# ── the fetch ─────────────────────────────────────────────────────────────────────────────────


async def _fetch_one(
    client: httpx.AsyncClient, url: str, hosts: tuple[str, ...]
) -> Picture | None:
    """*url*'s picture, or ``None`` for a 404. Redirects are followed only within *hosts*."""
    for _ in range(MAX_REDIRECTS + 1):
        if not host_allowed(url, hosts):
            raise VisualError(502, "the image link redirects off the allowed hosts; not fetched")
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                target = response.headers.get("location", "")
                url = urljoin(url, target)
                continue
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise VisualError(502, f"the image host answered {response.status_code}")
            kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if kind not in IMAGE_TYPES:
                raise VisualError(
                    502, f"the image host sent {kind or 'no type'}, not a jpeg or png"
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise VisualError(502, "the image is larger than 3 MB; not used")
            if not body:
                raise VisualError(502, "the image host sent an empty file")
            return Picture(kind, len(body), base64.b64encode(bytes(body)).decode("ascii"))
    raise VisualError(502, "the image link redirects too many times")


async def fetch(
    key: str,
    urls: list[str],
    hosts: tuple[str, ...],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: float | None = None,
) -> Picture:
    """The first of *urls* that exists (a 404 tries the next), cached under *key*."""
    clock = time.monotonic() if now is None else now
    if (hit := _cached(key, clock)) is not None:
        return hit
    if not urls:
        raise VisualError(404, "this post has no image")
    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_S, transport=transport or _transport
        ) as client:
            for url in urls:
                picture = await _fetch_one(client, url, hosts)
                if picture is not None:
                    _remember(key, picture, clock)
                    return picture
    except httpx.HTTPError as exc:
        raise VisualError(
            502, f"could not fetch the image ({type(exc).__name__}); try again"
        ) from exc
    raise VisualError(404, "the image host has no such image")


def chunk(picture: Picture, part: int) -> dict[str, Any]:
    """Part *part* of *picture*, in the shape the card reads."""
    if part < 0 or part >= picture.parts:
        raise VisualError(
            400, f"part {part} does not exist; this image has {picture.parts} parts (from 0)"
        )
    start = part * CHUNK_CHARS
    return {
        "type": picture.type,
        "size": picture.size,
        "parts": picture.parts,
        "part": part,
        "data": picture.b64[start : start + CHUNK_CHARS],
    }
