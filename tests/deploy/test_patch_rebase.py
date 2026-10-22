"""scripts/rebase-patches.sh --ci and .github/workflows/patch-rebase.yml (2026-10-20).

The bot exists because every open patch PR conflicts once another one merges:
both claimed the next patch number, both appended doc entries in the same
places, and both moved upstream.lock and the submodule pointer. PR 164 and
PR 171 were each rebased by hand three times on 2026-10-20.

The script runs against throwaway repositories shaped like this one: a bare
``origin`` for the product repo, a product checkout with this repo's patch
scripts, and ``upstream/`` as a clone of a fork with a tag and an [ae-patch]
series. Each test builds main and a PR branch that both added patch 0002,
moves main ahead, then runs ``--ci`` on the PR branch and reads the result.
The script never pushes; the tests also check the bare origin is untouched.
The workflow is held to the repo's standards and to the order its pushes
depend on.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ("rebase-patches.sh", "export-patches.sh", "_lib.sh")
WORKFLOW = ROOT / ".github" / "workflows" / "patch-rebase.yml"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}

DOC_HEADS = {
    "README.md": "# test\n\nPatch notes follow.\n",
    "patches/README.md": "# Patches\n\n`0001` adds a.txt's second line.\n",
    "docs/UPSTREAM-SYNC.md": "# Upstream sync\n\nNothing yet.\n",
    "docs/CHANGELOG.md": "# Changelog\n\nPhase 0: the series is empty.\n",
    "justfile": "default:\n    @true\n\n# P1: the first patch.\n",
}


class Fixture:
    """A product repo with a bare origin and an upstream/ fork clone, driven by the script."""

    def __init__(self, tmp: Path) -> None:
        self.env = {**os.environ, **GIT_ENV}
        self.env.pop("PATCH_REBASE_REPORT", None)
        self.report = tmp / "report.txt"
        self.fork = tmp / "fork"
        self.origin = tmp / "origin.git"
        self.repo = tmp / "repo"

        self.fork.mkdir()
        self.git(self.fork, "init", "--quiet", "--initial-branch=ae/v0.18.0")
        (self.fork / "a.txt").write_text("one\n")
        (self.fork / "b.txt").write_text("bee\n")
        self.git(self.fork, "add", ".")
        self.git(self.fork, "commit", "--quiet", "-m", "Upstream release.")
        self.git(self.fork, "tag", "v0.18.0")
        tag_sha = self.git(self.fork, "rev-parse", "HEAD")
        self.edit_fork("a.txt", "one\ntwo\n", "[ae-patch] Add a second line")

        self.git(self.origin.parent, "init", "--quiet", "--bare", str(self.origin))
        self.repo.mkdir()
        self.git(self.repo, "init", "--quiet", "--initial-branch=main")
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        for name in SCRIPTS:
            target = self.repo / "scripts" / name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT / "scripts" / name, target)
        self.git(self.repo, "clone", "--quiet", str(self.fork), "upstream")
        self.git(self.repo / "upstream", "checkout", "--quiet", "--detach", "ae/v0.18.0")
        head = self.git(self.repo / "upstream", "rev-parse", "HEAD")
        (self.repo / "upstream.lock").write_text(
            "UPSTREAM_REPO=https://example.invalid/omnigent.git\n"
            "UPSTREAM_TAG=v0.18.0\n"
            f"UPSTREAM_SHA={tag_sha}\n"
            f"AE_FORK={self.fork}\n"
            "AE_BRANCH=ae/v0.18.0\n"
            f"AE_BRANCH_SHA={head}\n"
            "PATCH_COUNT=1\n"
        )
        (self.repo / "patches").mkdir()
        for rel, text in DOC_HEADS.items():
            self.write(rel, text)
        self.write("packages/x.py", "VALUE = 1\n")
        self.run("export-patches.sh")
        self.commit("Initial layout.")
        self.git(self.repo, "push", "--quiet", "origin", "main")

    # ── plumbing ──

    def git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", "-c", "advice.addEmbeddedRepo=false", *args],
            cwd=cwd,
            env=self.env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def run(
        self, script: str, *args: str, report: bool = False
    ) -> subprocess.CompletedProcess[str]:
        env = {**self.env, "PATCH_REBASE_REPORT": str(self.report)} if report else self.env
        return subprocess.run(
            [str(self.repo / "scripts" / script), *args],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def rebase(self, *, report: bool = False) -> subprocess.CompletedProcess[str]:
        """--ci the way the workflow runs it: main's copy, outside the checkout, PATCH_ROOT set."""
        bot = self.repo.parent / "bot-scripts"
        bot.mkdir(exist_ok=True)
        for name in SCRIPTS:
            shutil.copy2(ROOT / "scripts" / name, bot / name)
        env = {**self.env, "PATCH_ROOT": str(self.repo)}
        if report:
            env["PATCH_REBASE_REPORT"] = str(self.report)
        return subprocess.run(
            [str(bot / "rebase-patches.sh"), "origin/main", "--ci"],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def write(self, rel: str, text: str) -> None:
        target = self.repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def append(self, rel: str, text: str) -> None:
        self.write(rel, self.read(rel) + text)

    def read(self, rel: str) -> str:
        return (self.repo / rel).read_text()

    def commit(self, message: str) -> str:
        self.git(self.repo, "add", "-A")
        self.git(self.repo, "commit", "--quiet", "-m", message)
        return self.git(self.repo, "rev-parse", "HEAD")

    def edit_fork(self, rel: str, text: str, subject: str, cwd: Path | None = None) -> None:
        where = cwd or self.fork
        (where / rel).write_text(text)
        self.git(where, "commit", "--quiet", "-am", subject)

    def patch_branch(self, branch: str, rel: str, text: str, subject: str, label: str) -> None:
        """A branch off main's first commit that adds one fork patch (0002) and its doc entries."""
        self.git(self.repo, "checkout", "--quiet", "-B", branch, "main")
        up = self.repo / "upstream"
        self.git(up, "checkout", "--quiet", "--detach", self.lock()["AE_BRANCH_SHA"])
        self.edit_fork(rel, text, subject, cwd=up)
        assert self.run("export-patches.sh").returncode == 0
        self.append("README.md", f"\n{label} (2026-10-20): `patches/0002`.\n")
        self.append(
            "patches/README.md",
            f"\n`0002` ({label}) changes {rel}, a fix to `0001` after 00015 ms; it sits after\n"
            "`0001`. Its other note: after `0001`.\n",
        )
        self.append(
            "docs/CHANGELOG.md", f"\n{label} (2026-10-21): `patches/0002`, P2, after `0001`.\n"
        )
        self.append("justfile", f"# P2 ({label}): its suites.\n")
        self.commit(f"Add the {label} patch (patch 0002).")

    def lock(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.read("upstream.lock").splitlines())

    def report_lines(self) -> list[str]:
        return self.report.read_text().splitlines() if self.report.exists() else []

    def origin_refs(self) -> str:
        return self.git(self.origin, "for-each-ref", "--format=%(refname) %(objectname)")


def _build(tmp: Path, *, pr_rel: str = "b.txt", main_code: str | None = None) -> Fixture:
    """Main merges its own patch 0002 first; the PR branch, cut before that, also claims 0002."""
    fx = Fixture(tmp)
    fx.patch_branch("pr", pr_rel, "bee\nfrom the pr\n", "[ae-patch] Change b from the PR", "PR")
    if main_code is not None:
        fx.write("packages/x.py", "VALUE = 2  # pr\n")
        fx.commit("Change the value on the PR side.")
    fx.git(fx.repo, "push", "--quiet", "origin", "pr")

    fx.git(fx.repo, "checkout", "--quiet", "main")
    fx.git(fx.repo / "upstream", "checkout", "--quiet", "--detach", fx.lock()["AE_BRANCH_SHA"])
    fx.git(fx.repo, "checkout", "--quiet", "-B", "main-next", "main")
    fx.patch_branch(
        "main-next", "a.txt", "one\ntwo\nthree\n", "[ae-patch] Add a third line", "Main"
    )
    if main_code is not None:
        fx.write("packages/x.py", main_code)
        fx.commit("Change the value on main.")
    fx.git(fx.repo, "push", "--quiet", "origin", "main-next:main")
    fx.git(fx.repo, "branch", "--quiet", "-f", "main", "main-next")

    fx.git(fx.repo, "checkout", "--quiet", "pr")
    fx.git(fx.repo / "upstream", "checkout", "--quiet", "--detach", fx.lock()["AE_BRANCH_SHA"])
    return fx


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return _build(tmp_path)


# ── (a) both sides added doc entries: resolved and renumbered ─────────────


def test_both_sides_doc_entries_resolve_and_renumber(fx: Fixture) -> None:
    origin_before = fx.origin_refs()
    result = fx.rebase(report=True)
    assert result.returncode == 0, result.stdout + result.stderr

    names = sorted(p.name for p in (fx.repo / "patches").glob("*.patch"))
    assert [n[:4] for n in names] == ["0001", "0002", "0003"]
    assert names[1].endswith("-Add-a-third-line.patch"), "main's 0002 keeps its number"
    assert names[2].endswith("-Change-b-from-the-PR.patch"), "the PR's patch moves to 0003"
    lock = fx.lock()
    assert lock["PATCH_COUNT"] == "3"
    assert lock["AE_BRANCH_SHA"] == fx.git(fx.repo / "upstream", "rev-parse", "HEAD")
    assert fx.git(fx.repo, "ls-tree", "HEAD", "upstream").split()[2] == lock["AE_BRANCH_SHA"]

    # main's entries first and untouched, then the PR's, renumbered.
    readme = fx.read("README.md")
    assert readme.endswith(
        "\nMain (2026-10-20): `patches/0002`.\n\nPR (2026-10-20): `patches/0003`.\n"
    ), readme
    patches_readme = fx.read("patches/README.md")
    assert patches_readme.endswith(
        "\n`0002` (Main) changes a.txt, a fix to `0001` after 00015 ms; it sits after\n"
        "`0001`. Its other note: after `0001`.\n"
        "\n`0003` (PR) changes b.txt, a fix to `0001` after 00015 ms; it sits after\n"
        "`0002`. Its other note: after `0002`.\n"
    ), patches_readme  # "after" names main's last patch now, wrapped or not (issue 214)
    assert fx.read("justfile").endswith("# P2 (Main): its suites.\n# P3 (PR): its suites.\n")
    # The build diary moved to docs/CHANGELOG.md, and the bot stopped on it (2026-10-21,
    # https://github.com/analyticendeavors/omnigent-ae/pull/199).
    changelog = fx.read("docs/CHANGELOG.md")
    assert changelog.endswith(
        "\nMain (2026-10-21): `patches/0002`, P2, after `0001`.\n"
        "\nPR (2026-10-21): `patches/0003`, P3, after `0001`.\n"
    ), changelog  # the predecessor moves only in patches/README.md
    assert "<<<<<<<" not in readme + patches_readme + changelog

    assert fx.git(fx.repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert fx.git(fx.repo, "merge-base", "--is-ancestor", "origin/main", "HEAD") == ""
    assert fx.git(fx.repo, "log", "-1", "--format=%s") == (
        "Move this branch's patches onto origin/main's series, renumbering 0002 to 0003."
    )
    assert fx.run("export-patches.sh", "--check").returncode == 0
    report = fx.report_lines()
    assert "status=rebased" in report
    assert "map=0002=0003" in report
    assert f"fork_sha={lock['AE_BRANCH_SHA']}" in report
    assert not [line for line in report if line.startswith("conflict=")]
    assert fx.origin_refs() == origin_before, "the script never pushes"


def test_a_second_run_finds_nothing_to_do(fx: Fixture) -> None:
    assert fx.rebase().returncode == 0
    head = fx.git(fx.repo, "rev-parse", "HEAD")
    result = fx.rebase(report=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert fx.git(fx.repo, "rev-parse", "HEAD") == head
    assert "status=current" in fx.report_lines()


def test_a_merge_alone_still_counts_as_work_to_push(tmp_path: Path) -> None:
    """Main moved only its docs: the merge settles it, and the branch still needs the push."""
    fx = Fixture(tmp_path)
    fx.patch_branch("pr", "b.txt", "bee\nfrom the pr\n", "[ae-patch] Change b from the PR", "PR")
    fork_sha = fx.git(fx.repo / "upstream", "rev-parse", "HEAD")
    fx.git(fx.repo, "checkout", "--quiet", "main")
    fx.append("README.md", "\nA docs-only change on main.\n")
    fx.commit("Add a note on main.")
    fx.git(fx.repo, "push", "--quiet", "origin", "main")
    fx.git(fx.repo, "checkout", "--quiet", "pr")
    fx.git(fx.repo / "upstream", "checkout", "--quiet", "--detach", fork_sha)

    result = fx.rebase(report=True)
    assert result.returncode == 0, result.stdout + result.stderr
    report = fx.report_lines()
    assert "status=rebased" in report and "map=" in report, report
    assert f"fork_sha={fork_sha}" in report, "the fork commits did not move"
    assert len(fx.git(fx.repo, "log", "-1", "--format=%P").split()) == 2, "HEAD is the merge"
    assert fx.read("README.md").endswith(
        "\nA docs-only change on main.\n\nPR (2026-10-20): `patches/0002`.\n"
    )


# ── (b) a real conflict: non-zero, nothing committed, nothing pushed ──────


def test_a_code_conflict_exits_non_zero_and_names_the_file(tmp_path: Path) -> None:
    fx = _build(tmp_path, main_code="VALUE = 3  # main\n")
    head = fx.git(fx.repo, "rev-parse", "HEAD")
    origin_before = fx.origin_refs()
    result = fx.rebase(report=True)
    assert result.returncode != 0, result.stdout
    assert "packages/x.py" in result.stderr
    assert fx.report_lines() == ["conflict=packages/x.py"]
    assert fx.git(fx.repo, "rev-parse", "HEAD") == head, "nothing is committed"
    assert fx.git(fx.repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert not (fx.repo / ".git" / "MERGE_HEAD").exists(), "the merge is aborted"
    assert fx.origin_refs() == origin_before, "nothing is pushed"


def test_a_fork_conflict_exits_non_zero_and_names_the_upstream_file(tmp_path: Path) -> None:
    fx = _build(tmp_path, pr_rel="a.txt")
    upstream_head = fx.git(fx.repo / "upstream", "rev-parse", "HEAD")
    origin_before = fx.origin_refs()
    result = fx.rebase(report=True)
    assert result.returncode != 0, result.stdout
    assert fx.report_lines() == ["conflict=upstream/a.txt"]
    up = fx.repo / "upstream"
    assert fx.git(up, "rev-parse", "HEAD") == upstream_head, "the fork rebase is aborted"
    assert fx.git(up, "status", "--porcelain") == ""
    assert fx.origin_refs() == origin_before, "nothing is pushed"


# ── the workflow ───────────────────────────────────────────────────────────


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps(job: str) -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in _workflow()["jobs"][job]["steps"]}


def test_workflow_triggers_permissions_and_concurrency() -> None:
    wf = _workflow()
    on = wf[True] if True in wf else wf["on"]  # PyYAML reads the bare key `on` as boolean True
    assert on["push"] == {"branches": ["main"]}
    assert on["workflow_dispatch"]["inputs"]["pr"]["required"] is False
    assert wf["permissions"] == {"contents": "read", "pull-requests": "read"}
    for job in ("rebase", "push"):
        group = wf["jobs"][job]["concurrency"]
        assert "matrix.number" in group["group"], job
        assert group["cancel-in-progress"] is False, job


def test_workflow_meets_the_repo_standards() -> None:
    for name, job in _workflow()["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), f"job {name} has no timeout-minutes"
        for step in job["steps"]:
            run = step.get("run")
            if run is None:
                continue
            assert run.lstrip().startswith("set -euo pipefail"), step["name"]
            assert "${{" not in run, f"step {step['name']} interpolates an expression in run"


def test_workflow_warns_and_passes_without_the_token() -> None:
    find = _steps("find")["Find conflicting patch PRs"]
    assert find["env"]["PATCH_BOT_TOKEN"] == "${{ secrets.PATCH_BOT_TOKEN }}"
    assert "::warning::" in find["run"] and "exit 0" in find["run"]


def test_the_job_that_runs_pr_code_never_sees_the_bot_token() -> None:
    """A PR's justfile or a dependency in uv sync could read anything on its runner."""
    rebase = _workflow()["jobs"]["rebase"]
    assert "PATCH_BOT_TOKEN" not in yaml.safe_dump(rebase)
    assert (
        "token"
        not in _steps("rebase")["Checkout the PR branch with the upstream submodule"]["with"]
    )
    push = _workflow()["jobs"]["push"]
    assert push["needs"] == ["find", "rebase"]
    uses = {str(step["uses"]).split("@")[0] for step in push["steps"] if "uses" in step}
    assert uses == {"actions/checkout", "actions/download-artifact"}, uses
    checkout = next(st for st in push["steps"] if "checkout" in str(st.get("uses")))
    assert checkout["with"] == {"ref": "main", "fetch-depth": 0}, "main only, no submodule"
    for step in push["steps"]:
        command = re.search(r"^\s*(just|uv|\S*scripts/\S+\.sh)\b", step.get("run", ""), re.M)
        assert command is None, f"{step['name']} runs {command.group(1) if command else ''}"


def test_workflow_checks_everything_before_it_pushes_and_the_fork_first() -> None:
    rebase = [s["name"] for s in _workflow()["jobs"]["rebase"]["steps"]]
    order = [
        "Rebase the patches onto main",
        "Patch series matches",
        "Lint",
        "Bundle the rebased branches",
    ]
    assert [n for n in rebase if n in order] == order
    assert (
        _steps("rebase")["Bundle the rebased branches"]["if"]
        == "steps.rebase.outputs.status == 'rebased'"
    )
    steps = _steps("push")
    check = steps["Check the result against main, the PR and the fork"]["run"]
    for needle in ('--is-ancestor "$PR_SHA"', "--is-ancestor origin/main", 'grep -vxF "$series"'):
        assert needle in check, needle
    push = steps["Push the fork branch, then the PR branch"]
    assert push["if"] == "steps.check.outputs.go == 'true'"
    body = push["run"]
    assert body.index('--force-with-lease="refs/heads/$TOPIC:$OLD"') < body.index("push origin")
    assert "headRefOid" in body, "a PR whose head moved is skipped"
    assert 'extraheader=" -c credential.helper= -c "credential.helper=$helper"' in body


def test_workflow_is_documented() -> None:
    assert ".github/workflows/patch-rebase.yml" in (ROOT / "README.md").read_text()
    assert "PATCH_BOT_TOKEN" in (ROOT / "docs" / "UPSTREAM-SYNC.md").read_text()
