"""Parking Lot note assembly, ported from the task-store worker.

Source: ``task-store-worker/src/index.ts`` in analyticendeavors/ae-claude-skills
(constants, ``oneLine``, ``firstSentence``, ``repoSlug``, ``normalizeTags``,
``estimateFor``, ``resurfaceDate``, ``buildNotes``, ``nextFromNotes``). The worker
stays the canonical route C on chat surfaces; this port lets the server file and
read the same note shape from ``/v1/ae/lot``.

Two implementations of one note can drift, so ``tests/fixtures/golden_note.json``
is generated from the worker's own TypeScript by ``scripts/regen-golden-note.sh``
and ``tests/test_notes.py`` compares every function here against it byte for
byte. When the worker changes, regenerate the fixture and the test says what
moved.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

Quadrant = Literal["hotfix", "feature", "admin"]

# The gate's QUADRANT words mapped to the store's two axes. Not the textbook
# Eisenhower ordering: urgent-only sits directly under Q1 in the app (the owner,
# 2026-08-13), so ``feature`` is Q3 and ``admin`` Q2. Never send a priority;
# the store overwrites it from the axes.
QUADRANT_AXES: dict[str, dict[str, int]] = {
    "hotfix": {"urgent": 1, "important": 1},  # Q1
    "feature": {"urgent": 0, "important": 1},  # Q3
    "admin": {"urgent": 1, "important": 0},  # Q2
}

# Days until a parked fork resurfaces (the owner, 2026-08-30). "Come back and
# decide", not "do it by then".
RESURFACE_DAYS: dict[str, int] = {"hotfix": 3, "feature": 30, "admin": 14}

# The zone when AE_TASK_STORE_TZ names none. UTC since 2026-10-22, so a
# stranger's server is not on Analytic Endeavors' clock; the box sets its own.
DEFAULT_TZ = "UTC"
PARKING_LOT_NAME = "Parking Lot"
# A list is only the phone's if its surface says ``todo``; ``parking`` never
# syncs to Microsoft To Do, which is the whole point of a parking lot.
PARKING_SURFACE = "parking"
SUMMARY_CAP = 120
RESUME_HEADING = "### Resume in Claude"

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MINUTES = re.compile(r"\bmin(ute)?s?\b", re.IGNORECASE)
_NEWLINE_RUNS = re.compile(r"\s*\n+\s*")
_FENCES = re.compile(r"`{3,}")
_SENTENCE_STOP = re.compile(r"[.!?](\s|$)")
_GITHUB_PREFIX = re.compile(r"^((https?://)?(www\.)?github\.com/|git@github\.com:)", re.IGNORECASE)
_DOT_GIT = re.compile(r"\.git$", re.IGNORECASE)
# The worker's ``\w`` is ASCII (JavaScript), so spell it out rather than use
# Python's Unicode-aware ``\w``.
_SLUG_PART = re.compile(r"^[A-Za-z0-9_.\-]+$")
_WORD_CHAR = re.compile(r"[A-Za-z0-9_]")


@dataclass(frozen=True)
class NoteFields:
    """The five note lines plus the pieces the note is built from.

    :param summary: The gate's ``FORK:`` clause, the bold line at the top.
    :param thread: THREAD short-name; becomes the task title.
    :param repo: ``owner/name`` when the fork belongs to a repository, else ``None``.
    :param state: STATE, one line.
    :param next: NEXT, one physical action.
    :param slot: SLOT outcome (``never opened``, ``freed``, ``swapped-back``).
    :param quadrant: ``hotfix``, ``feature`` or ``admin``.
    :param est: EST in the gate's own words.
    :param from_: FROM: the thread's task, surface and date.
    """

    summary: str
    thread: str
    repo: str | None
    state: str
    next: str
    slot: str
    quadrant: str
    est: str
    from_: str


def is_real_date(value: str) -> bool:
    """Whether *value* is a real calendar date, not merely date-shaped."""
    if not _DATE_ONLY.match(value):
        return False
    year, month, day = (int(part) for part in value.split("-"))
    try:
        date(year, month, day)
    except ValueError:
        return False
    return True


def resurface_date(tz: str, days_ahead: int, *, today: date | None = None) -> str:
    """A calendar date *days_ahead* from today, reckoned in *tz*.

    The horizon is counted in calendar days: today's date is resolved in the
    zone first and the days are added to the date, so a daylight-saving
    transition never shifts the result.

    :param tz: IANA zone name, e.g. ``"America/Los_Angeles"``.
    :param days_ahead: Calendar days to add.
    :param today: Override for tests; defaults to today in *tz*.
    :returns: ``YYYY-MM-DD``.
    """
    start = today if today is not None else datetime.now(ZoneInfo(tz)).date()
    return (start + timedelta(days=days_ahead)).isoformat()


def normalize_tags(supplied: Iterable[str] | None) -> list[str]:
    """``parked`` first, then the supplied tags lowercased, trimmed and deduped."""
    out = ["parked"]
    for raw in supplied or []:
        tag = raw.strip().lower()
        if tag and tag not in out:
            out.append(tag)
    return out


def estimate_for(est: str | None) -> Literal["15", "60plus"] | None:
    """The store's estimate for the gate's size words.

    Anything reading as minutes becomes ``"15"``, everything larger ``"60plus"``;
    the phrase itself survives verbatim on the note's ``EST:`` line.
    """
    if not est or not est.strip():
        return None
    return "15" if _MINUTES.search(est) else "60plus"


def one_line(value: str) -> str:
    """One line, guaranteed: newlines collapse to spaces, triple backticks are defused."""
    return _FENCES.sub("'''", _NEWLINE_RUNS.sub(" ", value)).strip()


def first_sentence(text: str) -> str:
    """The first sentence of *text*, capped, for a note filed without a summary."""
    flat = one_line(text)
    match = _SENTENCE_STOP.search(flat)
    stop = match.start() if match else -1
    if stop != -1 and stop + 1 <= SUMMARY_CAP:
        return flat[: stop + 1]
    if len(flat) <= SUMMARY_CAP:
        return flat
    cut = flat[:SUMMARY_CAP]
    space = cut.rfind(" ")
    return (cut[:space] if space > 40 else cut).rstrip() + "..."


def repo_slug(raw: str | None) -> str | None:
    """``owner/name`` from a slug or a GitHub URL, or ``None`` when neither."""
    if not raw:
        return None
    trimmed = _DOT_GIT.sub("", _GITHUB_PREFIX.sub("", one_line(raw)).strip("/"))
    parts = trimmed.split("/")
    if len(parts) < 2:
        return None
    owner, name = parts[0], parts[1]

    def ok(segment: str) -> bool:
        return bool(_SLUG_PART.match(segment)) and bool(_WORD_CHAR.search(segment))

    # A GitHub owner never has a dot, so a dotted first segment is a host
    # ("gitlab.com/owner/name"), never an owner.
    return f"{owner}/{name}" if ok(owner) and "." not in owner and ok(name) else None


def summary_for(summary: str | None, state: str) -> str:
    """The bold line: the supplied summary, else the first sentence of *state*."""
    return (summary or "").strip() or first_sentence(state)


def build_notes(
    note: NoteFields,
    checklist: Iterable[str] | None = None,
    task_url: str | None = None,
) -> str:
    """The handoff note the app renders, verbatim from the worker.

    A checklist is the leading run of ``- [ ]`` lines and nothing is ever
    prepended above it. Blank lines are load-bearing: the app joins consecutive
    non-blank lines into one paragraph. The machine-readable half is fenced so
    it survives that joining and pastes into any Claude conversation in one drag.
    """
    out: list[str] = []
    for item in checklist or []:
        step = one_line(item)
        if step:
            out.append(f"- [ ] {step}")
    if out:
        out.append("")
    if note.repo:
        out.append(f"[{note.repo}](https://github.com/{note.repo})")
        out.append("")
    summary = one_line(note.summary)
    state = one_line(note.state)
    out.append(f"**{summary}**")
    out.append("")
    if state and state != summary:
        out.append(state)
        out.append("")
    out.append(f"**Next:** {one_line(note.next)}")
    out.append("")
    out.append("---")
    out.append("")
    out.append(RESUME_HEADING)
    out.append("")
    out.append("```")
    out.append(f"THREAD: {one_line(note.thread)}")
    if note.repo:
        out.append(f"REPO: {note.repo}")
    out.append(f"STATE: {one_line(note.state)}")
    out.append(f"NEXT: {one_line(note.next)}")
    out.append(f"SLOT: {one_line(note.slot)}")
    out.append(
        f"QUADRANT: {note.quadrant} · EST: {one_line(note.est)} · FROM: {one_line(note.from_)}"
    )
    if task_url:
        out.append(f"TASK: {task_url}")
    out.append("```")
    return "\n".join(out)


def next_from_notes(notes: str | None) -> str | None:
    """The ``NEXT:`` line back out of a stored note, for list views."""
    for line in (notes or "").split("\n"):
        if line.startswith("NEXT: "):
            return line[len("NEXT: ") :].strip()
    return None


def resume_block_from_notes(notes: str | None) -> str | None:
    """The fenced "Resume in Claude" block's contents, or ``None`` when absent."""
    lines = (notes or "").split("\n")
    try:
        start = lines.index(RESUME_HEADING)
    except ValueError:
        return None
    fence_open = next((i for i in range(start + 1, len(lines)) if lines[i] == "```"), None)
    if fence_open is None:
        return None
    fence_close = next((i for i in range(fence_open + 1, len(lines)) if lines[i] == "```"), None)
    if fence_close is None:
        return None
    return "\n".join(lines[fence_open + 1 : fence_close])
