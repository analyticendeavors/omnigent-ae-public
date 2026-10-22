"""Lazy, advisory compliance reports. Markdown never participates in merge gating."""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any

from ae_omni_policies import github

TTL_S = 60.0
MAX_REPORTS = 128
_cache: dict[tuple[str, int], tuple[float, dict[str, Any] | None]] = {}
_generation = 0
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(?:🔍\ufe0f?\s+)?AE Compliance Review\s*#*\s*$")
# An explicit metadata line immediately below the title, never a SHA mentioned
# inside findings or quoted code. The current producer does not emit this marker.
_SHA_MARKER = re.compile(r"^<!-- ae-compliance-reviewed-sha: ([0-9a-fA-F]{40}) -->$")
_VERDICT = re.compile(
    r"^ {0,3}(?:#{1,6}\s+)?(?:Verdict:\s*)?(?:\*\*)?"
    r"(APPROVED|BLOCKED|CHANGES REQUIRED|CHANGES REQUESTED)"
    r"(?:\*\*)?(?:\s*[:.].*|\s+[-–—].*|\s*#*\s*)$",
    re.IGNORECASE,
)


def reset_cache() -> None:
    global _generation
    _generation += 1
    _cache.clear()


def forget(repo: str, number: int) -> None:
    global _generation
    _generation += 1
    _cache.pop((repo.lower(), number), None)


def _report(comment: dict[str, Any], repo: str, number: int) -> dict[str, Any] | None:
    user = comment.get("user")
    if not isinstance(user, dict) or user.get("type") != "Bot":
        return None
    if user.get("login") != "github-actions[bot]":
        return None
    body = comment.get("body")
    if not isinstance(body, str):
        return None
    lines = body.strip("\r\n").splitlines()
    if not lines or not _HEADING.fullmatch(lines[0]):
        return None
    comment_id = comment.get("id")
    updated = comment.get("updated_at")
    if type(comment_id) is not int or comment_id <= 0 or not isinstance(updated, str):
        raise github.GitHubError("GitHub returned incomplete compliance comment metadata.")
    try:
        timestamp = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Missing timezone")
    except ValueError as exc:
        raise github.GitHubError("GitHub returned an invalid compliance update time.") from exc
    content = [line for line in lines[1:] if line.strip()]
    reviewed_sha = None
    if content and (marker := _SHA_MARKER.fullmatch(content[0])):
        reviewed_sha = marker[1].lower()
        content.pop(0)
    verdict = "unknown"
    if content and (match := _VERDICT.fullmatch(content[0])):
        verdict = "approved" if match[1].upper() == "APPROVED" else "blocked"
    return {
        "body": body,
        "verdict": verdict,
        # Construct from validated route components and GitHub's numeric ID.
        # Never expose a potentially unsafe html_url supplied in a response.
        "url": f"https://github.com/{repo}/pull/{number}#issuecomment-{comment_id}",
        "updated_at": updated,
        "reviewed_sha": reviewed_sha,
        "revision": "unknown",
    }


async def read_report(
    client: github.GitHubClient, repo: str, number: int, *, fresh: bool = False
) -> dict[str, Any] | None:
    """Read every bounded comment page and compare an explicit SHA to a fresh head.

    Failures propagate for Retry, including failures after a previously cached
    report. Updated timestamps rank reports but never establish SHA provenance.
    """
    key = (repo.lower(), number)
    held = _cache.get(key)
    if not fresh and held is not None and time.monotonic() - held[0] < TTL_S:
        return held[1]
    generation = _generation
    try:
        comments = await client.issue_comments(repo, number)
        reports = [
            (report, c["id"]) for c in comments if (report := _report(c, repo, number)) is not None
        ]
        # A new comment can share an update second with the older report.
        # GitHub's numeric ID resolves that tie without trusting API ordering.
        latest = max(
            reports,
            key=lambda item: (
                datetime.fromisoformat(item[0]["updated_at"].replace("Z", "+00:00")),
                item[1],
            ),
            default=None,
        )
        report = latest[0] if latest is not None else None
        if report is not None and report["reviewed_sha"]:
            pull = await client.pull(repo, number)
            head = (pull.get("head") or {}).get("sha")
            if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
                raise github.GitHubError("GitHub did not provide the current pull request head.")
            report["revision"] = "current" if head.lower() == report["reviewed_sha"] else "stale"
    except github.GitHubError:
        _cache.pop(key, None)
        raise
    if generation == _generation:
        _cache.pop(key, None)
        if len(_cache) >= MAX_REPORTS:
            del _cache[next(iter(_cache))]
        _cache[key] = (time.monotonic(), report)
    return report
