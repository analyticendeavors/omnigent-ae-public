"""The repository's own composite actions meet the workflow standards (2026-10-22).

tests/deploy holds the workflows to CLAUDE.md's standards, but a local action under
.github/actions is not a workflow, so its run steps would otherwise go unchecked.
"""

from __future__ import annotations

import shutil
import subprocess
import warnings
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTIONS = sorted((ROOT / ".github" / "actions").glob("*/action.yml"))


def _runs(action: Path) -> list[tuple[str, str]]:
    steps = yaml.safe_load(action.read_text())["runs"].get("steps", [])
    return [(step.get("name", "?"), step["run"]) for step in steps if "run" in step]


def test_there_are_local_actions_to_check() -> None:
    assert ACTIONS, ".github/actions has no action.yml; the CI jobs use install-just"


@pytest.mark.parametrize("action", ACTIONS, ids=lambda p: p.parent.name)
def test_run_steps_meet_the_workflow_standards(action: Path) -> None:
    for name, run in _runs(action):
        assert run.lstrip().startswith("set -euo pipefail"), f"{name} must start with it"
        assert "${{" not in run, f"{name} interpolates an expression in run"
        for line in run.splitlines():
            if line.lstrip().startswith("curl "):
                assert "--max-time" in line and "--fail" in line, f"{name}: {line.strip()}"


@pytest.mark.parametrize("action", ACTIONS, ids=lambda p: p.parent.name)
def test_run_steps_are_shellcheck_clean(action: Path, tmp_path: Path) -> None:
    if shutil.which("shellcheck") is None:
        warnings.warn("shellcheck is not installed here; CI installs it.", stacklevel=2)
        return
    for index, (name, run) in enumerate(_runs(action)):
        script = tmp_path / f"step{index}.sh"
        script.write_text("#!/usr/bin/env bash\n" + run)
        done = subprocess.run(
            ["shellcheck", str(script)], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, f"{name}:\n{done.stdout}"
