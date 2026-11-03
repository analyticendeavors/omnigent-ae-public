"""Reviewer personas: a named reviewer brief a preset's reviewer slot can point at.

Added 2026-11-01 by omnigent-ae (revision 2 of the security reviewer slot, patch 0277).
A persona is a purpose, an optional model and effort, a ``when`` (``always`` or ``risky``)
and free-text instructions. Five are built in here, read-only, always present and never
stored per user: ``general``, ``security``, ``performance``, ``tests`` and ``accessibility``.
A user adds their own (at most :data:`MAX_PERSONAS`) in ``RolesState.personas``; a user id
may not reuse a built-in id. A reviewer slot names one with ``persona``; the slot's own model
and effort override the persona's. A slot that names a persona that no longer exists reads as
a plain reviewer, never an error.

Free text reaches the lead's prompt, so it carries no control characters; only
``instructions`` may hold newlines, and the lead notice renders them so they cannot break
the notice's structure (upstream ``omnigent/inner/ae_reviewers.py``).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_PERSONAS = 20
ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
NAME_MAX = 40
PURPOSE_MAX = 200
TRIGGER_MAX = 200
INSTRUCTIONS_MAX = 1000
# Characters no persona text may hold (review fix, patch 0278): C0/C1 controls (instructions may
# keep newline and tab), line and paragraph separators, and the bidi-override, isolate and
# zero-width characters that can make text read as something else in the lead's prompt.
_HIDDEN = frozenset(
    "\u200b\u200c\u200d\u200e\u200f\u2028\u2029\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\ufeff\u00ad\u061c"
    "\u180e\u034f"
)
_MODEL_ID = r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$"

Effort = Literal["low", "medium", "high", "xhigh", "max"]
When = Literal["always", "risky"]

# Catalog ids, as role_prefs.OPUS and SONNET (kept here so this module imports nothing from it).
_OPUS = "claude-opus-5-5"
_SONNET = "claude-sonnet-5-5"


class Persona(BaseModel):
    """One reviewer persona, as the Personas section in Settings > Roles shows it."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1, max_length=NAME_MAX)
    purpose: str = Field(min_length=1, max_length=PURPOSE_MAX)
    model: str | None = Field(default=None, pattern=_MODEL_ID)
    effort: Effort | None = None
    when: When = "always"
    # What makes the diff worth this reviewer, for a persona that runs only when the diff is
    # risky; the lead notice names it on the reviewer's line. Absent: the instructions say.
    trigger: str | None = Field(default=None, max_length=TRIGGER_MAX)
    instructions: str = Field(min_length=1, max_length=INSTRUCTIONS_MAX)

    @model_validator(mode="after")
    def _no_control_or_hidden_characters(self) -> Persona:
        for field in ("name", "purpose", "trigger", "instructions"):
            bad = bad_character(
                getattr(self, field) or "", keep="\n\t" if field == "instructions" else ""
            )
            if bad:
                raise ValueError(
                    f"a persona's {field} cannot contain {bad}; remove it"
                    + (
                        " (new lines and tabs are fine in instructions)"
                        if field != "instructions"
                        else ""
                    )
                )
        return self

    @model_validator(mode="after")
    def _text_is_not_blank(self) -> Persona:
        for field in ("name", "purpose", "instructions"):
            if not getattr(self, field).strip():
                raise ValueError(f"a persona's {field} cannot be blank")
        if self.trigger is not None and not self.trigger.strip():
            raise ValueError("a persona's trigger cannot be blank; leave it out instead")
        return self


def bad_character(text: str, *, keep: str = "") -> str | None:
    """Plain words for the first control, invisible or bidi character in *text*, else None."""
    for ch in text:
        if ch in keep:
            continue
        if ch in _HIDDEN:
            return f"the invisible or direction-changing character U+{ord(ch):04X}"
        if ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F:
            return f"a control character (U+{ord(ch):04X})"
    return None


_SECURITY_TRIGGER = (
    "authentication or OAuth, secrets or key vaults, MCP servers, HTTP endpoints or Workers, "
    "parsing of outside input, permissions or policies, or anything bound for the public repo"
)

BUILTIN_PERSONAS: tuple[Persona, ...] = tuple(
    Persona.model_validate(raw)
    for raw in (
        {
            "id": "general",
            "name": "General",
            "purpose": "Plain correctness review of the diff",
            "model": _SONNET,
            "effort": "medium",
            "instructions": (
                "Review the diff for correctness: logic errors, wrong or missing edge cases, "
                "broken callers, and changes that do not do what the task asked.\n"
                "Read the changed code and what calls it; do not review style or formatting.\n"
                "Report each issue with the file and line, what goes wrong and for which input."
            ),
        },
        {
            "id": "security",
            "name": "Security",
            "purpose": "Security only, reporting issues with a concrete attack path",
            "model": _OPUS,
            "effort": "high",
            "when": "risky",
            "trigger": _SECURITY_TRIGGER,
            "instructions": (
                "Brief yourself from the security-audit skill's focused-review mode.\n"
                "Review the diff for security issues only: authentication and OAuth, secrets "
                "and key vaults, MCP servers, HTTP endpoints and Workers, parsing of outside "
                "input, permissions and policies, and anything bound for the public repo.\n"
                "Report only an issue with a concrete attack path: who the attacker is, what "
                "they send or reach, and what they gain. Drop findings without one.\n"
                "Do not report style, performance or general bugs."
            ),
        },
        {
            "id": "performance",
            "name": "Performance",
            "purpose": "Slow paths, wasted work and unbounded growth in the changed code",
            "model": _SONNET,
            "effort": "medium",
            "instructions": (
                "Review the diff for performance problems only.\n"
                "Look for work repeated in a loop, queries or requests per item, unbounded "
                "lists, caches or logs, blocking calls on a hot path, and large reads that "
                "could be bounded.\n"
                "Report an issue only with the input size at which it hurts and the cheaper "
                "alternative. Skip micro-optimizations."
            ),
        },
        {
            "id": "tests",
            "name": "Tests",
            "purpose": "Missing or weak tests for the changed code",
            "model": _SONNET,
            "effort": "medium",
            "instructions": (
                "Review the tests that go with the diff.\n"
                "Name each changed behavior, branch or error path that no test covers, and "
                "each test that would still pass if the change were wrong (no assertion, "
                "mocked-away logic, copied expected values).\n"
                "For each gap say the one test to add: its input and the assertion."
            ),
        },
        {
            "id": "accessibility",
            "name": "Accessibility",
            "purpose": "WAI-ARIA, keyboard use and contrast in changed UI",
            "model": _SONNET,
            "effort": "medium",
            "when": "risky",
            "trigger": "web UI: components, markup, styles or copy that a person sees or operates",
            "instructions": (
                "Review the changed UI for accessibility only.\n"
                "Check roles, names and states against WAI-ARIA, that every control works "
                "from the keyboard with a visible focus, that focus moves sensibly in "
                "dialogs and menus, and that text and controls meet WCAG AA contrast in "
                "light and dark themes.\n"
                "Report each issue with the element and the fix."
            ),
        },
    )
)
BUILTIN_IDS: frozenset[str] = frozenset(p.id for p in BUILTIN_PERSONAS)


def builtin(persona_id: str) -> Persona | None:
    """The built-in persona with this id, or ``None``."""
    return next((p for p in BUILTIN_PERSONAS if p.id == persona_id), None)


def find(persona_id: str, user_personas: list[Persona] | None) -> Persona | None:
    """The persona a slot names: a built-in first, then the user's own; ``None`` when gone."""
    return builtin(persona_id) or next(
        (p for p in user_personas or [] if p.id == persona_id), None
    )


def check_user_personas(personas: list[Persona]) -> None:
    """Raise ``ValueError`` for a repeated id, a built-in id or more than :data:`MAX_PERSONAS`."""
    if len(personas) > MAX_PERSONAS:
        raise ValueError(f"at most {MAX_PERSONAS} personas")
    ids = [p.id for p in personas]
    repeated = sorted({i for i in ids if ids.count(i) > 1})
    if repeated:
        raise ValueError(f"persona ids must be unique; repeated: {', '.join(repeated)}")
    clash = sorted(set(ids) & BUILTIN_IDS)
    if clash:
        raise ValueError(
            f"persona id {', '.join(clash)} is a built-in persona; duplicate it under a new id"
        )


def builtin_view() -> list[dict[str, Any]]:
    """The built-in personas as JSON, in their fixed order, for ``GET /roles``."""
    return [p.model_dump(exclude_none=True) for p in BUILTIN_PERSONAS]
