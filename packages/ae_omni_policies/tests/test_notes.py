"""notes.py against the worker's own output.

``fixtures/golden_note.json`` is produced by ``scripts/regen-golden-note.sh``
from the TypeScript in ae-claude-skills ``task-store-worker/src/index.ts``; the
inputs live in ``fixtures/golden_note_inputs.json``. A failure here means the
port and the worker disagree: regenerate the fixture from the current worker
first, then decide which side moved.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ae_omni_policies import notes

FIXTURES = Path(__file__).parent / "fixtures"
INPUTS = json.loads((FIXTURES / "golden_note_inputs.json").read_text(encoding="utf-8"))
GOLDEN = json.loads((FIXTURES / "golden_note.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(INPUTS["notes"]))
def test_build_notes_matches_the_worker(name: str) -> None:
    case = INPUTS["notes"][name]
    note = notes.NoteFields(
        summary=notes.summary_for(case.get("summary"), case["state"]),
        thread=case["thread"],
        repo=notes.repo_slug(case.get("repo")),
        state=case["state"],
        next=case["next"],
        slot=case["slot"],
        quadrant=case["quadrant"],
        est=case["est"],
        from_=case["from"],
    )
    assert (
        notes.build_notes(note, case.get("checklist"), case.get("task_url"))
        == (GOLDEN["notes"][name])
    )


@pytest.mark.parametrize("est", INPUTS["estimate_for"])
def test_estimate_for_matches_the_worker(est: str) -> None:
    assert notes.estimate_for(est) == GOLDEN["estimate_for"][est]


@pytest.mark.parametrize("raw", INPUTS["repo_slug"])
def test_repo_slug_matches_the_worker(raw: str) -> None:
    assert notes.repo_slug(raw) == GOLDEN["repo_slug"][raw]


@pytest.mark.parametrize("text", INPUTS["first_sentence"])
def test_first_sentence_matches_the_worker(text: str) -> None:
    assert notes.first_sentence(text) == GOLDEN["first_sentence"][text]


@pytest.mark.parametrize("name", sorted(INPUTS["normalize_tags"]))
def test_normalize_tags_matches_the_worker(name: str) -> None:
    assert notes.normalize_tags(INPUTS["normalize_tags"][name]) == GOLDEN["normalize_tags"][name]


@pytest.mark.parametrize("name", sorted(INPUTS["next_from_notes"]))
def test_next_from_notes_matches_the_worker(name: str) -> None:
    assert (
        notes.next_from_notes(INPUTS["next_from_notes"][name]) == (GOLDEN["next_from_notes"][name])
    )


def test_resume_block_round_trips() -> None:
    full = GOLDEN["notes"]["full"]
    block = notes.resume_block_from_notes(full)
    assert block is not None
    assert block.startswith("THREAD: golden-note-full\nREPO: analyticendeavors/omnigent-ae\n")
    assert block.endswith("&q=golden-note-full")
    assert "```" not in block


def test_resume_block_absent() -> None:
    assert notes.resume_block_from_notes("just prose") is None
    assert notes.resume_block_from_notes(None) is None
    assert notes.resume_block_from_notes("### Resume in Claude\n\n```\nunterminated") is None


@pytest.mark.parametrize(
    ("value", "real"),
    [("2026-09-23", True), ("2026-02-29", False), ("2026-13-01", False), ("26-09-23", False)],
)
def test_is_real_date(value: str, real: bool) -> None:
    assert notes.is_real_date(value) is real


def test_resurface_date_counts_calendar_days_across_dst() -> None:
    # 2026-03-06 plus three days is the 9th, not the 10th: calendar days, not
    # 72 hours across the spring-forward transition.
    assert notes.resurface_date("America/Los_Angeles", 3, today=date(2026, 3, 6)) == "2026-03-09"
    assert notes.resurface_date("America/Los_Angeles", 30, today=date(2026, 12, 15)) == (
        "2027-01-14"
    )


def test_resurface_days_by_quadrant() -> None:
    assert notes.RESURFACE_DAYS == {"hotfix": 3, "feature": 30, "admin": 14}
    assert notes.QUADRANT_AXES["feature"] == {"urgent": 0, "important": 1}
    assert notes.QUADRANT_AXES["admin"] == {"urgent": 1, "important": 0}
