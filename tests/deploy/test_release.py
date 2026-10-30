"""The release workflow and scripts/next-release-tag.sh, without GitHub (2026-09-24).

The script runs against a throwaway git repository shaped like this one (an
upstream.lock, shipping paths, docs, a submodule pointer), so each test sets up
a history of commits and tags and reads back the decision. The workflow is
held to the repo's standards and to the facts the decision depends on: the CI
trigger, the tip-of-main and force paths, and the dispatch of image.yml (a tag
pushed with GITHUB_TOKEN starts no workflow run by itself).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "next-release-tag.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE_WORKFLOW = WORKFLOWS / "release.yml"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}

LOCK = """UPSTREAM_REPO=https://github.com/omnigent-ai/omnigent.git
UPSTREAM_TAG={tag}
UPSTREAM_SHA=eebef804e1fe4beddc61ea232b951cddddd7f890
AE_FORK=https://github.com/analyticendeavors/omnigent.git
AE_BRANCH=ae/{tag}
AE_BRANCH_SHA=9445361ae8d07a46d851660e39d595aa8ace1d57
PATCH_COUNT=4
"""


class Repo:
    """A repository with this one's shipping layout, for driving the script."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.env = {**os.environ, **GIT_ENV}
        path.mkdir()
        self.git("init", "--quiet", "--initial-branch=main")
        self.write("upstream.lock", LOCK.format(tag="v0.13.0"))
        self.write("packages/ae_omni_policies/__init__.py", "")
        self.write("README.md", "# test\n")
        self.pointer("1" * 40)
        self.commit("Initial layout.")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.path, env=self.env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def write(self, rel: str, content: str) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self.git("add", rel)

    def pointer(self, sha: str) -> None:
        """Stage the upstream submodule pointer (a gitlink), as the real repo carries it."""
        self.git("update-index", "--add", "--cacheinfo", f"160000,{sha},upstream")

    def commit(self, message: str = "Change.") -> str:
        self.git("commit", "--quiet", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def change(self, rel: str) -> str:
        self.write(rel, os.urandom(8).hex())
        return self.commit(f"Change {rel}.")

    def tag(self, name: str, rev: str = "HEAD", *, annotated: bool = True) -> None:
        if annotated:
            self.git("tag", "-a", name, rev, "-m", name)
        else:
            self.git("tag", name, rev)

    def decide(self, *args: str) -> dict[str, str]:
        result = subprocess.run(
            [str(SCRIPT), *args],
            cwd=self.path,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        out = dict(line.split("=", 1) for line in result.stdout.splitlines())
        assert set(out) == {"decision", "tag", "reason"}, result.stdout
        return out


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


# ── the version bump ───────────────────────────────────────────────────────


def test_first_release_on_a_pin_is_n_1(repo: Repo) -> None:
    out = repo.decide("HEAD")
    assert out["decision"] == "tag"
    assert out["tag"] == "ae-v0.13.0.1"
    assert "no earlier" in out["reason"]


@pytest.mark.parametrize(
    "path",
    [
        "packages/ae_omni_policies/slots.py",
        "config/server.yaml",
        "agents/steward/agent.yaml",
        "deploy/Dockerfile",
        "patches/0005-new.patch",
    ],
)
def test_a_shipping_change_takes_the_next_n(repo: Repo, path: str) -> None:
    repo.tag("ae-v0.13.0.1", annotated=False)
    repo.change("packages/ae_omni_policies/a.py")
    repo.tag("ae-v0.13.0.2")
    repo.change(path)
    out = repo.decide("HEAD")
    assert (out["decision"], out["tag"]) == ("tag", "ae-v0.13.0.3"), out
    assert "since ae-v0.13.0.2" in out["reason"]


def test_the_submodule_pointer_ships(repo: Repo) -> None:
    repo.tag("ae-v0.13.0.1")
    repo.pointer("2" * 40)
    repo.commit("Move the pointer.")
    assert repo.decide("HEAD")["tag"] == "ae-v0.13.0.2"


def test_a_moved_pin_restarts_n_at_1(repo: Repo) -> None:
    repo.tag("ae-v0.13.0.7")
    repo.write("upstream.lock", LOCK.format(tag="v0.14.0"))
    repo.pointer("3" * 40)
    repo.commit("Sync upstream to v0.14.0.")
    out = repo.decide("HEAD")
    assert (out["decision"], out["tag"]) == ("tag", "ae-v0.14.0.1"), out


def test_the_lock_is_read_at_the_commit_not_the_worktree(repo: Repo) -> None:
    repo.tag("ae-v0.13.0.1")
    target = repo.change("config/server.yaml")
    (repo.path / "upstream.lock").write_text(LOCK.format(tag="v9.9.9"))
    assert repo.decide(target)["tag"] == "ae-v0.13.0.2"


def test_n_counts_every_tag_on_the_pin_not_only_ancestors(repo: Repo) -> None:
    base = repo.git("rev-parse", "HEAD")
    repo.tag("ae-v0.13.0.1")
    repo.git("checkout", "--quiet", "-b", "side")
    repo.change("config/side.yaml")
    repo.tag("ae-v0.13.0.9")
    repo.git("checkout", "--quiet", "main")
    assert repo.git("rev-parse", "HEAD") == base
    repo.change("config/main.yaml")
    out = repo.decide("HEAD")
    assert out["tag"] == "ae-v0.13.0.10", "a number used off main is never reused"
    assert "since ae-v0.13.0.1" in out["reason"], "the change check compares with an ancestor"


def test_version_sort_not_string_sort(repo: Repo) -> None:
    for n in (1, 2, 10):
        repo.change(f"config/{n}.yaml")
        repo.tag(f"ae-v0.13.0.{n}")
    repo.change("config/next.yaml")
    assert repo.decide("HEAD")["tag"] == "ae-v0.13.0.11"


def test_another_pins_tags_do_not_count(repo: Repo) -> None:
    repo.tag("ae-v0.12.0.8")
    repo.change("config/server.yaml")
    assert repo.decide("HEAD")["tag"] == "ae-v0.13.0.1"


# ── when nothing is tagged ─────────────────────────────────────────────────


@pytest.mark.parametrize("paths", [["README.md"], ["tests/deploy/test_x.py", "docs/LABELS.md"]])
def test_docs_or_tests_only_makes_no_tag(repo: Repo, paths: list[str]) -> None:
    repo.tag("ae-v0.13.0.3")
    for path in paths:
        repo.change(path)
    out = repo.decide("HEAD")
    assert out == {
        "decision": "skip",
        "tag": "",
        "reason": out["reason"],
    }
    assert "nothing that ships changed since ae-v0.13.0.3" in out["reason"]
    assert "force" in out["reason"], "the skip says how to ship anyway"


def test_force_tags_a_docs_only_commit(repo: Repo) -> None:
    repo.tag("ae-v0.13.0.3")
    repo.change("README.md")
    out = repo.decide("--force", "HEAD")
    assert (out["decision"], out["tag"], out["reason"]) == ("tag", "ae-v0.13.0.4", "forced")


@pytest.mark.parametrize("annotated", [True, False])
@pytest.mark.parametrize("force", [False, True])
def test_a_tagged_commit_is_never_tagged_again(repo: Repo, annotated: bool, force: bool) -> None:
    """Covers a tag the owner pushes by hand before the workflow runs, of either kind."""
    repo.tag("ae-v0.13.0.3")
    repo.change("patches/0004-status.patch")
    repo.tag("ae-v0.13.0.4", annotated=annotated)
    out = repo.decide(*(["--force"] if force else []), "HEAD")
    assert out["decision"] == "skip"
    assert out["tag"] == ""
    assert "already tagged ae-v0.13.0.4" in out["reason"]


def test_a_commit_that_is_not_the_tip_is_skipped(repo: Repo) -> None:
    repo.tag("ae-v0.13.0.1")
    older = repo.change("config/a.yaml")
    repo.change("config/b.yaml")
    out = repo.decide("--tip", "main", older)
    assert out["decision"] == "skip"
    assert "no longer the tip of main" in out["reason"]
    tip = repo.decide("--tip", "main", "main")
    assert (tip["decision"], tip["tag"]) == ("tag", "ae-v0.13.0.2"), "the tip carries both changes"


def test_broken_inputs_fail_loudly(repo: Repo) -> None:
    for args in ([], ["deadbeef" * 5], ["--tip", "nope", "HEAD"], ["--bogus", "HEAD"]):
        result = subprocess.run(
            [str(SCRIPT), *args], cwd=repo.path, env=repo.env, capture_output=True, text=True
        )
        assert result.returncode != 0, args
        assert result.stdout == "", (
            "a broken run must not print a decision the workflow could act on"
        )
        assert result.stderr.strip(), args


def test_script_is_executable_and_parses() -> None:
    assert os.access(SCRIPT, os.X_OK)
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


# ── the workflow ───────────────────────────────────────────────────────────


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(RELEASE_WORKFLOW.read_text())


def _on(wf: dict[str, Any]) -> dict[str, Any]:
    return wf[True] if True in wf else wf["on"]  # PyYAML reads the bare key `on` as boolean True


def _steps() -> dict[str, dict[str, Any]]:
    return {s["name"]: s for s in _workflow()["jobs"]["tag"]["steps"]}


def test_release_workflow_runs_after_ci_on_main_or_by_hand() -> None:
    wf = _workflow()
    on = _on(wf)
    ci_name = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())["name"]
    assert on["workflow_run"] == {
        "workflows": [ci_name],
        "types": ["completed"],
        "branches": ["main"],
    }
    force = on["workflow_dispatch"]["inputs"]["force"]
    assert force["type"] == "boolean"
    assert force["default"] is False
    condition = wf["jobs"]["tag"]["if"]
    assert "github.event.workflow_run.conclusion == 'success'" in condition
    assert "github.event.workflow_run.event == 'push'" in condition
    assert condition.startswith("vars.AE_IMAGE != '' && ("), (
        "a repository without the AE_IMAGE variable never tags itself"
    )


def test_release_workflow_permissions_and_concurrency() -> None:
    wf = _workflow()
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["tag"]["permissions"] == {"contents": "write", "actions": "write"}
    assert wf["concurrency"]["group"] == "release"
    assert wf["concurrency"]["cancel-in-progress"] is False, "a half-done release must finish"


def test_release_workflow_meets_the_repo_standards() -> None:
    for name, job in _workflow()["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), f"job {name} has no timeout-minutes"
        for step in job["steps"]:
            run = step.get("run")
            if run is None:
                continue
            assert run.lstrip().startswith("set -euo pipefail"), step["name"]
            assert "${{" not in run, f"{step['name']} interpolates an expression in its run body"
            for line in run.splitlines():
                if line.strip().startswith("curl"):
                    assert "--max-time" in line and "--fail" in line
    for step in _steps().values():
        for key, value in (step.get("env") or {}).items():
            if "secrets." in str(value):
                assert key == "GH_TOKEN", "secrets reach scripts through env as GH_TOKEN only"


def test_release_workflow_checks_the_image_name_before_it_tags() -> None:
    # A bad AE_IMAGE fails here, before a tag exists that image.yml cannot
    # publish and the box would wait on forever (2026-10-21,
    # https://github.com/analyticendeavors/omnigent-ae-public/issues/13).
    names = [s["name"] for s in _workflow()["jobs"]["tag"]["steps"]]
    check = "AE_IMAGE names a package under this repository's owner"
    assert names.index(check) == names.index("Checkout main with every tag") + 1
    step = _steps()[check]
    assert step["env"] == {
        "AE_IMAGE": "${{ vars.AE_IMAGE }}",
        "OWNER": "${{ github.repository_owner }}",
    }
    assert 'scripts/check-image-name.sh "$AE_IMAGE" "$OWNER"' in step["run"]


def test_release_workflow_tags_with_the_script_on_the_tip_of_main() -> None:
    steps = _steps()
    checkout = steps["Checkout main with every tag"]
    assert checkout["with"] == {"ref": "main", "fetch-depth": 0, "fetch-tags": True}
    pick = steps["Pick the commit to release"]
    assert pick["env"]["RUN_SHA"] == "${{ github.event.workflow_run.head_sha }}"
    assert "refs/heads/main" in pick["run"], "a dispatch from another branch is refused"
    decide = steps["Decide the tag"]["run"]
    assert "scripts/next-release-tag.sh" in decide
    assert "--tip origin/main" in decide
    assert "--force" in decide
    assert "$GITHUB_OUTPUT" in decide
    ci_check = steps["CI passed on the commit"]
    assert ci_check["if"] == "github.event_name == 'workflow_dispatch'"
    assert "--status success" in ci_check["run"]


def test_release_workflow_dispatches_image_yml_on_the_new_tag() -> None:
    """A tag pushed with GITHUB_TOKEN starts no run, so image.yml is dispatched explicitly."""
    steps = _steps()
    push = steps["Push the tag"]
    assert push["if"] == "steps.decide.outputs.decision == 'tag'"
    assert 'git push origin "refs/tags/$TAG"' in push["run"]
    publish = steps["Publish the image for the tag"]
    assert publish["if"] == "steps.decide.outputs.decision == 'tag'"
    assert 'gh workflow run image.yml --ref "$TAG" -f push=true' in publish["run"]
    assert publish["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    image = yaml.safe_load((WORKFLOWS / "image.yml").read_text())
    image_on = _on(image)
    assert image_on["workflow_dispatch"]["inputs"]["push"]["type"] == "boolean"
    assert "tag:ae-v*)" in next(
        s["run"]
        for s in image["jobs"]["image"]["steps"]
        if s.get("name") == "Decide the image tag and whether to push"
    ), "image.yml must honour push on a dispatched ae-v* tag ref"


def test_release_workflow_is_documented() -> None:
    readme = (ROOT / "README.md").read_text()
    assert ".github/workflows/release.yml" in readme
    for doc in (ROOT / "deploy" / "README.md",):
        text = doc.read_text()
        assert "release.yml" in text or "release workflow" in text, doc
        assert "force" in text, f"{doc} says how to ship a docs-only change"


def test_release_workflow_passes_actionlint() -> None:
    if shutil.which("actionlint") is None:
        message = (
            "actionlint is not installed here; uv tool install actionlint-py. "
            "The check ran in CI, not on this machine."
        )
        warnings.warn(message, stacklevel=2)
        pytest.skip(message)
    result = subprocess.run(
        ["actionlint", str(RELEASE_WORKFLOW.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
