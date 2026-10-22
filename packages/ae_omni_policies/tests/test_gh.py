"""The GitHub CLI half of ``list_prs``: a fake ``gh`` on PATH, a real subprocess."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from ae_omni_policies import gh

ROLLUP = [
    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
    {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"},
    {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": ""},
    {"__typename": "StatusContext", "state": "SUCCESS"},
]
ROW = {
    "number": 49,
    "url": "https://github.com/analyticendeavors/omnigent-ae/pull/49",
    "title": "Fix overflow",
    "state": "OPEN",
    "isDraft": False,
    "headRefName": "claude/overflow",
    "baseRefName": "main",
    "mergeable": "CONFLICTING",
    "mergeStateStatus": "DIRTY",
    "reviewDecision": "CHANGES_REQUESTED",
    "statusCheckRollup": ROLLUP,
    "updatedAt": "2026-09-25T04:00:00Z",
    "mergedAt": None,
}


def _fake_gh(bin_dir: Path, *, auth_exit: int, prs: str = "[]", pr_exit: int = 0) -> Path:
    """A ``gh`` that prints a token-looking line on auth status, to prove it is never read."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "gh"
    (bin_dir / "prs.json").write_text(prs, encoding="utf-8")
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = auth ]; then echo "Token: gho_SECRETSECRET"; '
        f"exit {auth_exit}; fi\n"
        'if [ "$1" = pr ]; then '
        f'[ {pr_exit} -eq 0 ] || {{ echo "GraphQL: Could not resolve to a Repository" >&2; '
        f"exit {pr_exit}; }}; "
        f'cat "{bin_dir / "prs.json"}"; exit 0; fi\n'
        "exit 2\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_card_speaks_the_server_cards_vocabulary() -> None:
    card = gh.card(ROW, "AnalyticEndeavors/omnigent-ae")
    assert card["repo"] == "analyticendeavors/omnigent-ae"
    assert card["state"] == "open"
    assert card["head_ref"] == "claude/overflow"
    assert card["mergeable"]["state"] == "conflicted"
    assert card["review"]["state"] == "changes_requested"
    assert card["ci"] == {
        "state": "failing",
        "passing": 2,
        "failing": 1,
        "pending": 1,
        "total": 4,
    }
    merged = gh.card(ROW | {"state": "MERGED", "statusCheckRollup": None}, "o/r")
    assert merged["state"] == "merged"
    assert merged["ci"]["state"] == "none"


def test_find_gh_looks_in_local_bin_when_path_does_not(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert gh.find_gh({"PATH": str(tmp_path / "empty"), "HOME": str(home)}) is None
    script = _fake_gh(home / ".local" / "bin", auth_exit=0)
    assert gh.find_gh({"PATH": str(tmp_path / "empty"), "HOME": str(home)}) == str(script)


async def test_status_says_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    monkeypatch.setenv("HOME", str(tmp_path / "nohome"))
    missing = await gh.status()
    assert missing == {"available": False, "reason": gh.NOT_INSTALLED, "binary": None}

    logged_out = _fake_gh(tmp_path / "out", auth_exit=1)
    monkeypatch.setenv("PATH", str(logged_out.parent))
    answer = await gh.status()
    assert answer["available"] is False
    assert answer["reason"] == gh.NOT_LOGGED_IN
    assert gh.LOGIN_COMMAND in answer["reason"]

    ready = _fake_gh(tmp_path / "in", auth_exit=0)
    monkeypatch.setenv("PATH", str(ready.parent))
    ok = await gh.status()
    assert ok == {"available": True, "reason": None, "binary": str(ready)}
    assert "gho_" not in json.dumps([missing, answer, ok]), "auth output is never read"


async def test_pull_requests_parses_rows_and_reports_gh_errors(tmp_path: Path) -> None:
    script = _fake_gh(tmp_path / "bin", auth_exit=0, prs=json.dumps([ROW, "junk"]))
    (card,) = await gh.pull_requests(str(script), "analyticendeavors/omnigent-ae")
    assert card["url"] == ROW["url"]
    failing = _fake_gh(tmp_path / "bad", auth_exit=0, pr_exit=1)
    with pytest.raises(RuntimeError, match="Could not resolve to a Repository"):
        await gh.pull_requests(str(failing), "analyticendeavors/nope")
    garbled = _fake_gh(tmp_path / "garbled", auth_exit=0, prs="not json")
    with pytest.raises(RuntimeError, match="did not answer JSON"):
        await gh.pull_requests(str(garbled), "o/r")
