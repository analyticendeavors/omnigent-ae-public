#!/usr/bin/env python3
"""Port the triage-gate scenarios from ae-claude-skills into test fixtures.

``scripts/test-triage-gate.sh`` in analyticendeavors/ae-claude-skills holds
nine scripted conversations and, per scenario, the tokens the assistant's
next reply must and must not start a line with (``FORK:``, ``Absorbed:``,
``THREAD:`` and friends). Those tokens are the contract the observer
(``packages/ae_omni_policies/src/ae_omni_policies/observer.py``) captures, so
the scenarios are the observer's fixtures, and two copies of one contract
drift: this script reads the shell script's ``transcript()`` and
``expectation()`` cases and rewrites the script-owned fields of
``packages/ae_omni_policies/tests/fixtures/triage/<scenario>.json``
(``transcript``, ``tokens``, ``expectation``), keeping the fields this repo
owns (``reply``, the canonical next assistant turn; ``labels`` and
``absent``, what the observer must write from it). Added 2026-09-23 with
Phase 4.

Run it when the harness changes, commit the result, and let
``test_triage_fixtures.py`` say what moved. A scenario the script drops is
reported and its fixture left in place for a human to delete.

Usage:
    scripts/port-triage-fixtures.py <path to an ae-claude-skills checkout>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "packages" / "ae_omni_policies" / "tests" / "fixtures" / "triage"
SOURCE = Path("scripts") / "test-triage-gate.sh"
COMMENT = (
    "Ported from scripts/test-triage-gate.sh in analyticendeavors/ae-claude-skills by "
    "scripts/port-triage-fixtures.py. `transcript`, `tokens` and `expectation` are the "
    "harness's and are rewritten on every run; `reply` (the canonical next assistant turn), "
    "`labels` and `absent` (what the observer must write from it) belong to this repository."
)
OURS = ("reply", "labels", "absent")

_CASE = re.compile(r"^\s{4}([a-z0-9-]+)\)\s+cat <<'EOF'$")
_TURN = re.compile(r"^\[(Human|Assistant)\]\s?(.*)$")
_EXPECT = re.compile(r"^\s{4}([a-z0-9-]+)\)\s+printf '(.*)' ;;$")
_ROLES = {"Human": "user", "Assistant": "assistant"}


def parse_transcripts(lines: list[str]) -> dict[str, list[dict[str, str]]]:
    """``{scenario: [{"role", "text"}, ...]}`` from the ``transcript()`` case blocks."""
    scenarios: dict[str, list[dict[str, str]]] = {}
    name: str | None = None
    turns: list[dict[str, str]] = []
    for line in lines:
        if name is None:
            match = _CASE.match(line)
            if match:
                name, turns = match.group(1), []
            continue
        if line == "EOF":
            scenarios[name] = turns
            name = None
            continue
        match = _TURN.match(line)
        if match:
            turns.append({"role": _ROLES[match.group(1)], "text": match.group(2)})
        elif turns:
            turns[-1]["text"] += "\n" + line
    return scenarios


def parse_expectations(lines: list[str]) -> dict[str, dict[str, object]]:
    """``{scenario: {"tokens": {...}, "expectation": str}}`` from the ``expectation()`` cases."""
    out: dict[str, dict[str, object]] = {}
    for line in lines:
        match = _EXPECT.match(line)
        if not match:
            continue
        fields = match.group(2).replace("\\t", "\t").replace("\\n", "").split("\t")
        must, must_not, soft, human = (fields + ["", "", "", ""])[:4]
        out[match.group(1)] = {
            "tokens": {
                "must": must.split(),
                "must_not": must_not,
                "soft": soft.split(),
            },
            "expectation": human,
        }
    return out


def scenario_order(lines: list[str]) -> list[str]:
    for line in lines:
        if line.startswith("SCENARIOS="):
            return line.split("=", 1)[1].strip('"').split()
    raise SystemExit("no SCENARIOS= line in the harness")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    script = Path(argv[1]) / SOURCE
    if not script.is_file():
        print(f"no harness at {script}", file=sys.stderr)
        return 2
    lines = script.read_text(encoding="utf-8").splitlines()
    names = scenario_order(lines)
    transcripts = parse_transcripts(lines)
    expectations = parse_expectations(lines)
    missing = [n for n in names if n not in transcripts or n not in expectations]
    if missing:
        print(f"scenarios without a transcript or expectation: {missing}", file=sys.stderr)
        return 1
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for name in names:
        path = FIXTURES / f"{name}.json"
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        fixture = {
            "_comment": COMMENT,
            "name": name,
            "expectation": expectations[name]["expectation"],
            "transcript": transcripts[name],
            "tokens": expectations[name]["tokens"],
            **{key: existing.get(key) for key in OURS},
        }
        if fixture["labels"] is None:
            fixture["labels"] = {}
        if fixture["absent"] is None:
            fixture["absent"] = []
        path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        state = "kept reply" if fixture["reply"] else "REPLY MISSING, write one"
        print(f"{path.relative_to(ROOT)}: {state}")
    for stale in sorted(FIXTURES.glob("*.json")):
        if stale.stem not in names:
            print(f"{stale.relative_to(ROOT)}: not in the harness any more; delete it by hand")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
