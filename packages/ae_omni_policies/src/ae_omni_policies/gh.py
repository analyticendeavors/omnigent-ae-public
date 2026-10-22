"""The GitHub CLI on the host, read only, for the orchestrator's ``list_prs``.

Added 2026-09-25 with the orchestrator agent. The server's own GitHub view
(``GET /v1/ae/prs``, :mod:`ae_omni_policies.prs`) needs ``AE_GITHUB_TOKEN`` on
the server and sees only the pull requests a session tracks. ``gh`` on the box
host sees any repository its login can read, so it is the second source; the
self-review of 2026-09-25
(https://github.com/analyticendeavors/omnigent-ae/pull/41, finding 2) found it
not installed and the box's GitHub identity read-only, so every call here
degrades to a sentence that says what is missing and the one command that
fixes it, never a crash.

Only reads are run (``gh auth status`` and ``gh pr list``). The output of
``gh auth status`` is discarded unread: it names the account and, on some
versions, part of the token, and nothing here needs more than its exit code.
The subprocess runs with no stdin and prompts disabled, so a missing login
can never hang a scheduled run on an interactive question.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any

from ae_omni_policies import prs

GH_TIMEOUT_S = 30.0
PR_FIELDS = (
    "number,url,title,state,isDraft,headRefName,baseRefName,mergeable,"
    "mergeStateStatus,reviewDecision,statusCheckRollup,updatedAt,mergedAt"
)
LOGIN_COMMAND = "gh auth login --hostname github.com --git-protocol https --web"
NOT_INSTALLED = (
    "gh is not installed on this host, so pull requests are read from the server only. "
    "Install it on the host that runs the server (https://cli.github.com, into "
    "~/.local/bin), then run "
    f"`{LOGIN_COMMAND}`."
)
NOT_LOGGED_IN = (
    "gh is installed but not logged in on this host, so pull requests are read from the "
    f"server only. On the host that runs the server, run `{LOGIN_COMMAND}` once."
)


def find_gh(environ: dict[str, str] | None = None) -> str | None:
    """The ``gh`` binary: on ``PATH``, else ``~/.local/bin/gh``.

    A host starts a stdio MCP server with a minimal environment whose ``PATH``
    may leave out ``~/.local/bin``, where the self-review suggests installing it.
    """
    env = os.environ if environ is None else environ
    found = shutil.which("gh", path=env.get("PATH"))
    if found:
        return found
    local = Path(env.get("HOME") or Path.home()) / ".local" / "bin" / "gh"
    return str(local) if local.is_file() and os.access(local, os.X_OK) else None


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env |= {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
    return env


async def run(binary: str, *args: str, timeout: float = GH_TIMEOUT_S) -> tuple[int, str, str]:
    """Run ``gh`` once: ``(exit code, stdout, stderr)``; ``124`` on a timeout."""
    proc = await asyncio.create_subprocess_exec(
        binary,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"gh did not answer within {int(timeout)}s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def status() -> dict[str, Any]:
    """``{"available": bool, "reason": str | None, "binary": str | None}``."""
    binary = find_gh()
    if binary is None:
        return {"available": False, "reason": NOT_INSTALLED, "binary": None}
    try:
        code, _, _ = await run(binary, "auth", "status", "--hostname", "github.com")
    except OSError as exc:
        return {"available": False, "reason": f"gh could not start: {exc}", "binary": binary}
    if code != 0:
        return {"available": False, "reason": NOT_LOGGED_IN, "binary": binary}
    return {"available": True, "reason": None, "binary": binary}


def _ci(rollup: list[dict[str, Any]] | None) -> dict[str, Any]:
    """``statusCheckRollup`` in the card's vocabulary (:func:`prs.ci_summary`)."""
    runs: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    for entry in rollup or []:
        if not isinstance(entry, dict):
            continue
        typename = entry.get("__typename")
        if typename == "StatusContext" or (typename is None and "state" in entry):
            statuses.append({"state": str(entry.get("state") or "").lower()})
        else:
            runs.append(
                {
                    "status": str(entry.get("status") or "").lower(),
                    "conclusion": str(entry.get("conclusion") or "").lower() or None,
                }
            )
    return prs.ci_summary(runs, statuses)


def card(entry: dict[str, Any], repo: str) -> dict[str, Any]:
    """One ``gh pr list`` row as the fields :func:`prs.read_card` puts on a card."""
    state = str(entry.get("state") or "").upper()
    pull = {
        "state": "open" if state == "OPEN" else "closed",
        "merged": state == "MERGED",
        "draft": bool(entry.get("isDraft")),
        "mergeable_state": str(entry.get("mergeStateStatus") or "unknown").lower(),
        "mergeable": {"MERGEABLE": True, "CONFLICTING": False}.get(
            str(entry.get("mergeable") or "").upper()
        ),
    }
    review = str(entry.get("reviewDecision") or "").lower() or "none"
    return {
        "repo": repo.lower(),
        "number": entry.get("number"),
        "url": entry.get("url"),
        "title": entry.get("title"),
        "state": prs.pull_state(pull),
        "draft": pull["draft"],
        "head_ref": entry.get("headRefName"),
        "base_ref": entry.get("baseRefName"),
        "updated_at": entry.get("updatedAt"),
        "merged_at": entry.get("mergedAt"),
        "ci": _ci(entry.get("statusCheckRollup")),
        "mergeable": prs.mergeability(pull),
        "review": {"state": review},
    }


async def pull_requests(
    binary: str, repo: str, *, state: str = "open", limit: int = 30
) -> list[dict[str, Any]]:
    """``gh pr list`` for *repo* as cards.

    :raises RuntimeError: With gh's own complaint (clipped) when the list fails.
    """
    code, out, err = await run(
        binary,
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        state,
        "--limit",
        str(limit),
        "--json",
        PR_FIELDS,
    )
    if code != 0:
        reason = " ".join((err or out).split())[:200] or f"exit {code}"
        raise RuntimeError(f"gh pr list --repo {repo} failed: {reason}")
    try:
        rows = json.loads(out or "[]")
    except ValueError as exc:
        raise RuntimeError(f"gh pr list --repo {repo} did not answer JSON") from exc
    return [card(row, repo) for row in rows if isinstance(row, dict)]


__all__ = [
    "LOGIN_COMMAND",
    "NOT_INSTALLED",
    "NOT_LOGGED_IN",
    "card",
    "find_gh",
    "pull_requests",
    "run",
    "status",
]
