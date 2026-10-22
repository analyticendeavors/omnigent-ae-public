"""The release the box runs against the newest one tagged, for the Session Dashboard.

Added 2026-10-17 with the Session Dashboard header. Since
https://github.com/analyticendeavors/omnigent-ae/pull/109 the box's auto-update
holds a release while a session is mid-turn (for up to two hours), so a merge no
longer shows up at once; the header's release row says which tag runs, whether a
newer one is waiting, and how many sessions it is waiting on.

- ``running``: ``OMNIGENT_IMAGE_TAG``, which compose hands the server from
  ``config/server.env`` (the tag the box deployed).
- ``latest``: the highest ``ae-vX.Y.Z.N`` tag on ``AE_RELEASE_REPO``
  (default ``analyticendeavors/omnigent-ae-public``; AE's box sets its private
  repo) through the server's GitHub token, cached
  for two minutes behind one lock; ``null`` when GitHub is off or fails
  (``error`` says why).
- ``state`` says only that a newer tag exists, not that the box will install
  it: a pin, a rolled-back tag or a hold on the box stays out of view here.
- ``mid_turn``: sessions whose last turn is running or waiting on an approval
  and whose runner checked in within five minutes: the predicate
  ``deploy/auto-update.sh`` waits on (its ``busy_count``), over the
  non-archived ``default`` rows, sub-agent sessions included.
- ``state``: ``current``, ``waiting`` (a newer tag exists), or ``unknown``.
- ``env_missing``: settings ``config/server.env.example`` sets that the server's
  environment lacks or leaves empty (``env_drift.py``, 2026-10-20); ``null`` when the image has
  no example.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from ae_omni_policies import env_drift, github, sessions

RUNNING_ENV = "OMNIGENT_IMAGE_TAG"
REPO_ENV = "AE_RELEASE_REPO"
# The public repository, which every operator can read (2026-10-22); a fork
# that publishes its own image sets AE_RELEASE_REPO.
DEFAULT_REPO = "analyticendeavors/omnigent-ae-public"
CACHE_S = 120.0
_TAG = re.compile(r"^ae-v(\d+(?:\.\d+)+)$")
_MID_TURN = {"running", "waiting"}
# auto-update.sh counts a session only while its runner checked in this recently.
RUNNER_FRESH_S = 300
_lock = asyncio.Lock()

# (expires at, latest tag or None, error or None), monotonic clock.
_cache: tuple[float, str | None, str | None] | None = None


def reset_cache() -> None:
    global _cache, _lock
    _cache = None
    _lock = asyncio.Lock()


def version_of(tag: str | None) -> tuple[int, ...] | None:
    """``ae-v0.16.0.35`` as ``(0, 16, 0, 35)``; None for any other shape."""
    match = _TAG.match(tag or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def newest(names: list[str]) -> str | None:
    tagged = [(version_of(name), name) for name in names]
    ranked = sorted((version, name) for version, name in tagged if version is not None)
    return ranked[-1][1] if ranked else None


async def _latest(transport: Any = None) -> tuple[str | None, str | None]:
    if _cache is not None and _cache[0] > time.monotonic():
        return _cache[1], _cache[2]
    async with _lock:
        return await _refresh(transport)


async def _refresh(transport: Any) -> tuple[str | None, str | None]:
    global _cache
    now = time.monotonic()
    if _cache is not None and _cache[0] > now:
        return _cache[1], _cache[2]
    settings = github.GitHubSettings.from_env()
    repo = (os.environ.get(REPO_ENV) or "").strip() or DEFAULT_REPO
    if not settings.configured:
        latest, error = None, "the server has no GitHub token (AE_GITHUB_TOKEN)"
    else:
        try:
            async with github.GitHubClient(settings, transport=transport) as client:
                latest, error = newest(await client.tag_names(repo, "ae-v")), None
        except github.GitHubError as exc:
            latest, error = None, str(exc)
    _cache = (now + CACHE_S, latest, error)
    return latest, error


def _mid_turn() -> int:
    fresh = int(time.time()) - RUNNER_FRESH_S
    return sum(
        1
        for row in sessions.list_top_level()
        if row.live_status in _MID_TURN and (row.runner_last_seen or 0) >= fresh
    )


async def status(transport: Any = None) -> dict[str, Any]:
    running = (os.environ.get(RUNNING_ENV) or "").strip() or None
    (latest, error), mid_turn, env_missing = await asyncio.gather(
        _latest(transport), asyncio.to_thread(_mid_turn), asyncio.to_thread(env_drift.missing)
    )
    have, newest_version = version_of(running), version_of(latest)
    if have is None or newest_version is None:
        state = "unknown"
    elif newest_version > have:
        state = "waiting"
    else:
        state = "current"
    return {
        "running": running,
        "latest": latest,
        "state": state,
        "mid_turn": mid_turn,
        "error": error,
        "env_missing": env_missing,
    }


__all__ = ["newest", "reset_cache", "status", "version_of"]
