"""Whether a merged omnigent-ae pull request runs on the box yet, for the action dock.

Added 2026-10-20 with the chat's action dock (``patches/README.md``, the action
dock entry). Since https://github.com/analyticendeavors/omnigent-ae/pull/109 the
box's auto-update holds a release while a session is mid-turn, and since
https://github.com/analyticendeavors/omnigent-ae/pull/142 a deploy hold can pause
it for hours, so "merged" no longer means "live". The dock's deploy line and the
feed's merged row say which.

- ``state``: ``live`` when the running release (``OMNIGENT_IMAGE_TAG``) contains
  the pull request's merge commit, ``waiting`` when it does not, ``unknown``
  when that cannot be told (GitHub off or failing, no running tag, the pull
  request is not merged or is not in the release repo). The dock shows no
  deploy item for ``unknown``.
- Contained means GitHub's compare from the merge commit to the running tag
  answers ``ahead`` or ``identical``.
- ``running``, ``latest`` and ``mid_turn`` are ``release.status()``'s, so the
  dock and the Session Dashboard's release row never disagree.
- ``hold_until`` and ``hold_by``: the deploy hold in force (``deploy_hold.py``),
  else ``null``.
- ``merged_at``: GitHub's ISO time, for "merged 4m ago".

The compare answer is cached for two minutes per (tag, merge commit), behind a
lock per key like ``release.py``'s tag list; a merged pull request's merge
commit and time never change, so they are kept for the process's life. A pull
request merged into another branch (stacked) never reaches a tag: ``unknown``.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from ae_omni_policies import deploy_hold, github, release

CACHE_S = 120.0
_CONTAINED = {"ahead", "identical"}
# One lock per (tag, merge sha), so a cold compare waits only on its own key.
_locks: dict[tuple[str, str], asyncio.Lock] = {}

# (tag, merge sha) -> (expires at, contained), monotonic clock.
_compare: dict[tuple[str, str], tuple[float, bool]] = {}
# (repo, number) -> (merge sha, merged at), merged pull requests only.
_merged: dict[tuple[str, int], tuple[str, str | None]] = {}


def reset_cache() -> None:
    _compare.clear()
    _merged.clear()
    _locks.clear()


def release_repo() -> str:
    return (os.environ.get(release.REPO_ENV) or "").strip() or release.DEFAULT_REPO


class NotOnDefaultBranch(Exception):
    """The pull request merged into a branch other than the repo's default."""


async def _merge_of(
    client: github.GitHubClient, repo: str, number: int
) -> tuple[str, str | None] | None:
    key = (repo.lower(), number)
    if key in _merged:
        return _merged[key]
    pull = await client.pull(repo, number)
    sha = pull.get("merge_commit_sha")
    if not pull.get("merged_at") or not isinstance(sha, str) or not sha:
        return None
    # Merged into another branch (a stacked pull request): its merge commit
    # never reaches a release tag, so no deploy state is claimed.
    base = pull.get("base")
    base = base if isinstance(base, dict) else {}
    target = base.get("repo")
    default = target.get("default_branch") if isinstance(target, dict) else None
    if default and base.get("ref") != default:
        raise NotOnDefaultBranch(f"{repo} #{number} merged into {base.get('ref')}, not {default}")
    merged_at = pull.get("merged_at")
    _merged[key] = (sha, merged_at if isinstance(merged_at, str) else None)
    return _merged[key]


async def _contains(client: github.GitHubClient, repo: str, tag: str, sha: str) -> bool:
    key = (tag, sha)
    held = _compare.get(key)
    if held is not None and held[0] > time.monotonic():
        return held[1]
    async with _locks.setdefault(key, asyncio.Lock()):
        held = _compare.get(key)
        if held is not None and held[0] > time.monotonic():
            return held[1]
        contained = await client.compare_status(repo, sha, tag) in _CONTAINED
        _compare[key] = (time.monotonic() + CACHE_S, contained)
        return contained


async def status(repo: str, number: int, transport: Any = None) -> dict[str, Any]:
    """The deploy state of *repo* #*number*; never raises for a GitHub failure."""
    answer: dict[str, Any] = {
        "state": "unknown",
        "running": None,
        "latest": None,
        "mid_turn": 0,
        "hold_until": None,
        "hold_by": None,
        "merged_at": None,
        "error": None,
    }
    settings = github.GitHubSettings.from_env()
    # The cheap refusals first: they skip the session scan release.status() runs.
    if repo.lower() != release_repo().lower():
        answer["error"] = f"{repo} is not the release repository ({release_repo()})"
        return answer
    if not settings.configured:
        answer["error"] = "the server has no GitHub token (AE_GITHUB_TOKEN)"
        return answer
    rel, reading = await asyncio.gather(
        release.status(transport), asyncio.to_thread(deploy_hold.store().read)
    )
    hold = deploy_hold.view(reading)
    answer.update(
        running=rel["running"],
        latest=rel["latest"],
        mid_turn=rel["mid_turn"],
        hold_until=hold.until if hold.held else None,
        hold_by=hold.set_by if hold.held else None,
    )
    running = rel["running"]
    if not running:
        answer["error"] = f"the server does not know its release ({release.RUNNING_ENV} unset)"
    else:
        try:
            async with github.GitHubClient(settings, transport=transport) as client:
                merge = await _merge_of(client, repo, number)
                if merge is None:
                    answer["error"] = f"{repo} #{number} is not merged"
                else:
                    answer["merged_at"] = merge[1]
                    live = await _contains(client, repo, running, merge[0])
                    answer["state"] = "live" if live else "waiting"
        except (github.GitHubError, NotOnDefaultBranch) as exc:
            answer["error"] = str(exc)
    return answer


__all__ = ["CACHE_S", "release_repo", "reset_cache", "status"]
