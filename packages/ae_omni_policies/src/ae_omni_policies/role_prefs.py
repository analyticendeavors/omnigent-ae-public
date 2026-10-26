"""Each user's role presets: which model and effort every role gets, kept on the server.

Reid asked on 2026-10-23 (role routing) to stop restating "Opus plans,
Sonnet builds, Haiku polls" in every prompt and pick it once instead. A preset
names a model and effort for each role (lead, planner, reviewer, builder,
helper) plus three rules about changing models mid-task. Settings > Roles edits
the list; the lead notice (upstream ``omnigent/inner/ae_roles.py``) reads it on
every turn and tells the lead which model to give each helper it starts.

Seven starters (:data:`STARTERS`; the first four are pinned) are owned here. A user who
never saved reads as revision 0 with the starters and ``balanced`` as the default; "Restore
starters" in the web client appends any missing starter and saves.

Each preset also carries a Helpers rule (``rules.helpers``, added 2026-10-23
when the composer's Auto / Solo / Team button folded into presets): ``any``
lets the lead bring in other agents and sessions, ``own`` keeps work with the
lead and its native subagents (what Solo used to mean). It defaults to ``any``
so presets saved before it stay valid. The Solo starter came with it; a user
who saved before it existed gets it once, appended on read, and the starters a
user has been offered are kept in ``starters_seen`` (absent means the three
original ones) so deleting a starter does not bring it back.

Reviewers (2026-10-24, multiple reviewers per preset): ``roles.reviewer`` is
either one role setting, as before (read as one reviewer), or
``{"mode": "multi", "reviewers": [slot, ...]}`` with 1 to 3 slots. A slot is a
Claude model and effort (``fixed``, run as a native subagent), ``auto`` (slot 1
only, so a second opinion never lands on the lead's own pick), or another agent:
``{"mode": "agent", "agent": "<agent id>", "agent_name": "codex-native-ui" or None,
"name": "Codex", "model": id or None, "effort": effort or None}`` (``agent_name`` is how
readers find the live id when an install seeded the agent under a different one; the
agent runs as a fresh child session each review; ``None`` means the agent's default).
Clients write the single shape for one Claude reviewer. Two rules go with it, both with
defaults so older presets stay valid:
``review_merge`` (``merged``: one merged verdict, or ``separate``: each list,
then the verdict) and ``reviewer_unavailable`` (``run_rest``: skip it and say
so, or ``ask``); both are left out of the stored JSON when they hold the default.
Balanced and YOLO ship with Codex as Reviewer 2; a user who
already saved keeps the presets they saved and gets the new starters only
through "Restore starters".

Same storage rules as ``sidebar_prefs.py``: one revision per user so two
devices that edit at once get a conflict instead of a silent overwrite. The file
shape is ``{"version": 1, "users": {"<user>": {"revision": n, "updated_at":
"<iso utc>", "default_preset": id, "apply_start_on": bool, "presets": [...],
"starters_seen": [id, ...], "insights": {"enabled", "weeks", "scope", "show"}}}}``
(``insights``, added 2026-10-24, is optional and left out until set; ``preset_runs`` reads it).
Where it lives: :data:`STATE_FILE_ENV` when set; else ``/data/ae-roles.json``
when ``/data`` is a directory; else memory only.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    model_serializer,
    model_validator,
)

_logger = logging.getLogger(__name__)

STATE_FILE_ENV = "AE_ROLES_STATE_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-roles.json"
MAX_PRESETS = 30
CODE_NOT_SAVED = "roles_not_saved"
DEFAULT_PRESET = "balanced"
SOLO_PRESET = "solo"
# The starters every user had before ``starters_seen`` existed (2026-10-23).
ORIGINAL_STARTERS: tuple[str, ...] = ("save-tokens", "balanced", "yolo")
# The starters that came with pins (2026-10-24). Offering any of them once ends the
# pin migration in :func:`with_new_starters`, even if the rest wait for room.
PIN_ERA_STARTERS: tuple[str, ...] = ("think-it-through", "deep-debug", "quick-fix")
# Reserved for the per-session override: the lead chooses among the user's presets.
LEAD_PICKS = "lead-picks"

ROLES: tuple[str, ...] = ("lead", "planner", "reviewer", "builder", "helper")
Effort = Literal["low", "medium", "high", "xhigh", "max"]
Color = Literal["green", "teal", "blue", "violet", "amber", "red", "pink", "slate"]

# Catalog ids as the claude-native harness reports them (checked 2026-10-23
# against ~/.omnigent/cache/model-catalogs/claude-native-*.json on the box).
OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
FABLE = "claude-fable-5-1"

# Free text reaches the lead's prompt, one line per preset: no control characters.
_NO_CONTROL = r"^[^\x00-\x1f\x7f-\x9f\u2028\u2029]*$"
# Catalog ids seen so far: "claude-opus-5-5", "gpt-5.6-sol", "anthropic/claude-sonnet-5",
# "system.ai.claude-fable-5-1", "claude-fable-5-1[1m]".
_MODEL_ID = r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$"


MAX_REVIEWERS = 3
# The id GET /v1/agents reports: a bare 32-char hex for built-ins, other forms elsewhere.
_AGENT_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
# Codex's built-in agent id (sha256 of "builtin:codex-native-ui", first 32 hex digits;
# omnigent.db.utils.builtin_agent_id).
CODEX_AGENT_ID = "16a06503889b0c3034496821afd41b9e"
CODEX_NAME = "Codex"
# The built-in agent's registered name. An install seeded before the id scheme keeps a random
# id for this row, so slots also carry the name and the server and web resolve the live id by it.
CODEX_AGENT_NAME = "codex-native-ui"
_AGENT_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AutoRole(_Strict):
    """The lead picks the model for this role."""

    mode: Literal["auto"]


class SessionRole(_Strict):
    """Lead only: the session's own model and effort."""

    mode: Literal["session"]


class FixedRole(_Strict):
    """A fixed catalog model and effort."""

    mode: Literal["fixed"]
    model: str = Field(pattern=_MODEL_ID)
    effort: Effort


RoleSetting = Annotated[AutoRole | SessionRole | FixedRole, Field(discriminator="mode")]


class AgentReviewer(_Strict):
    """A reviewer slot run by another agent (Codex and so on) as a fresh child session."""

    mode: Literal["agent"]
    agent: str = Field(pattern=_AGENT_ID)
    # The agent's display name, as the team picker shows it; the lead notice names it.
    name: str = Field(min_length=1, max_length=40, pattern=_NO_CONTROL)
    # The agent's registered name ("codex-native-ui"), stable where the id is not: readers
    # resolve the live id by it when set. None for a slot that only knows an id.
    agent_name: str | None = Field(default=None, pattern=_AGENT_NAME)
    # None: the agent's default model or effort.
    model: str | None = Field(default=None, pattern=_MODEL_ID)
    effort: Effort | None = None

    @model_validator(mode="after")
    def _named(self) -> AgentReviewer:
        if not self.name.strip():
            raise ValueError("a reviewer agent's name cannot be blank")
        return self


ReviewerSlot = Annotated[AutoRole | FixedRole | AgentReviewer, Field(discriminator="mode")]


class MultiReviewer(_Strict):
    """One to three reviewers, run independently on the same diff and merged by the lead."""

    mode: Literal["multi"]
    reviewers: list[ReviewerSlot] = Field(min_length=1, max_length=MAX_REVIEWERS)

    @model_validator(mode="after")
    def _auto_first_only(self) -> MultiReviewer:
        if any(isinstance(slot, AutoRole) for slot in self.reviewers[1:]):
            raise ValueError("only reviewer 1 can be auto; name a model or agent for the others")
        return self


ReviewerSetting = Annotated[
    AutoRole | SessionRole | FixedRole | MultiReviewer, Field(discriminator="mode")
]


class Roles(_Strict):
    """One setting per role, all five required."""

    lead: RoleSetting
    planner: RoleSetting
    # One setting (one reviewer, the shape before 2026-10-24) or 1 to 3 reviewers.
    reviewer: ReviewerSetting
    builder: RoleSetting
    helper: RoleSetting

    @model_validator(mode="after")
    def _session_is_lead_only(self) -> Roles:
        for role in ROLES[1:]:
            if isinstance(getattr(self, role), SessionRole):
                raise ValueError(f"{role} cannot use the session model; only the lead can")
        return self


class Rules(_Strict):
    """When the lead may change models mid-task."""

    raise_or_lower: Literal["off", "tell", "ask"]
    lead_steps_down: StrictBool
    auto_raises: StrictInt = Field(ge=0, le=3)
    # "any": the lead may bring in other agents and sessions; "own": only its
    # native subagents. Missing in presets saved before 2026-10-23, so "any".
    helpers: Literal["any", "own"] = "any"
    # With two or more reviewers (2026-10-24): one merged verdict, or each list then the
    # verdict; and what to do when a reviewer cannot start. Defaults keep older presets valid.
    review_merge: Literal["merged", "separate"] = "merged"
    reviewer_unavailable: Literal["run_rest", "ask"] = "run_rest"

    @model_serializer(mode="wrap")
    def _omit_review_defaults(self, handler: Any) -> dict[str, Any]:
        """Leave the two review rules out when they hold their defaults.

        Re-saving a preset nobody touched then writes the same file as before 2026-10-24.
        """
        data: dict[str, Any] = handler(self)
        for key, default in (("review_merge", "merged"), ("reviewer_unavailable", "run_rest")):
            if data.get(key) == default:
                del data[key]
        return data


class Custom(_Strict):
    """A per-session customization carried in the ``ae.team`` label.

    The session picker that sets this field writes it.
    """

    roles: Roles
    rules: Rules


class Preset(_Strict):
    """One named preset, as Settings > Roles shows it."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    title: str = Field(min_length=1, max_length=40, pattern=_NO_CONTROL)
    icon: str = Field(pattern=r"^[a-z0-9-]{1,48}$")
    color: Color
    description: str = Field(default="", max_length=120, pattern=_NO_CONTROL)
    when_to_use: str = Field(default="", max_length=400, pattern=_NO_CONTROL)
    pinned: StrictBool = False
    roles: Roles
    rules: Rules

    @model_validator(mode="after")
    def _not_reserved(self) -> Preset:
        if self.id == LEAD_PICKS:
            raise ValueError(f"{LEAD_PICKS!r} is reserved for the lead picks option")
        if not self.title.strip():
            raise ValueError("a preset title cannot be blank")
        return self


class Insights(_Strict):
    """The user's preset-insights setting (2026-10-24): whether the run log is written for them.

    ``enabled`` switches the per-run log on, ``weeks`` is how far back a report reads, and
    ``scope`` is whose runs it covers. The roles editor carries it unchanged.
    """

    enabled: StrictBool
    weeks: StrictInt = Field(ge=1, le=52)
    scope: Literal["all", "own"]
    # What the Presets card shows (2026-10-24): cost, tokens or both. Old saved values lack it.
    show: Literal["cost", "tokens", "both"] = "both"


class _PresetList(_Strict):
    default_preset: str
    apply_start_on: StrictBool
    presets: list[Preset] = Field(min_length=1, max_length=MAX_PRESETS)
    # Starter ids this user has been offered; None means the original three.
    starters_seen: list[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")]] | None = (
        Field(default=None, max_length=64)
    )
    # Absent until the user sets it; left out of the stored JSON and the views while None.
    insights: Insights | None = None

    @model_serializer(mode="wrap")
    def _without_absent_insights(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if data.get("insights") is None:
            data.pop("insights", None)
        return data

    @model_validator(mode="after")
    def _ids_and_default(self) -> _PresetList:
        ids = [preset.id for preset in self.presets]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            raise ValueError(f"preset ids must be unique; repeated: {', '.join(repeated)}")
        if self.default_preset not in ids:
            raise ValueError(f"default_preset {self.default_preset!r} is not one of the presets")
        return self


class RolesState(_PresetList):
    """A user's saved presets and the revision they are at."""

    revision: StrictInt = Field(ge=0)


class RolesPut(_PresetList):
    """The body of ``PUT /roles``: the whole new list and the revision it was based on."""

    revision: StrictInt = Field(ge=0)


def _fixed(model: str, effort: str) -> dict[str, str]:
    return {"mode": "fixed", "model": model, "effort": effort}


_SESSION = {"mode": "session"}


def _reviewers(*slots: dict[str, Any]) -> dict[str, Any]:
    return {"mode": "multi", "reviewers": list(slots)}


def _codex(effort: str) -> dict[str, Any]:
    return {
        "mode": "agent",
        "agent": CODEX_AGENT_ID,
        "agent_name": CODEX_AGENT_NAME,
        "name": CODEX_NAME,
        "effort": effort,
    }


STARTERS: tuple[Preset, ...] = tuple(
    Preset.model_validate(raw)
    for raw in (
        {
            "id": "save-tokens",
            "pinned": True,
            "title": "Save tokens",
            "icon": "coins",
            "color": "green",
            "description": "Sonnet plans, Haiku does the rest",
            "when_to_use": "Routine or well-understood work where cost matters more than depth.",
            "roles": {
                "lead": _SESSION,
                "planner": _fixed(SONNET, "high"),
                "reviewer": _fixed(HAIKU, "medium"),
                "builder": _fixed(HAIKU, "medium"),
                "helper": _fixed(HAIKU, "low"),
            },
            "rules": {"raise_or_lower": "tell", "lead_steps_down": True, "auto_raises": 1},
        },
        {
            "id": "balanced",
            "pinned": True,
            "title": "Balanced",
            "icon": "scale",
            "color": "teal",
            "description": "Opus plans, Sonnet builds, Sonnet and Codex review",
            "when_to_use": "Most tasks: design judgment up front, then a pinned plan built "
            "and checked at a lower cost.",
            "roles": {
                "lead": _SESSION,
                "planner": _fixed(OPUS, "high"),
                "reviewer": _reviewers(_fixed(SONNET, "medium"), _codex("medium")),
                "builder": _fixed(SONNET, "medium"),
                "helper": _fixed(HAIKU, "low"),
            },
            "rules": {"raise_or_lower": "tell", "lead_steps_down": True, "auto_raises": 1},
        },
        {
            "id": SOLO_PRESET,
            "pinned": True,
            "title": "Solo",
            "icon": "user",
            "color": "slate",
            "description": "Balanced roles, own subagents only",
            "when_to_use": "Work that should stay in this chat: the lead and its native "
            "subagents, no other agents or sessions.",
            "roles": {
                "lead": _SESSION,
                "planner": _fixed(OPUS, "high"),
                "reviewer": _fixed(SONNET, "medium"),
                "builder": _fixed(SONNET, "medium"),
                "helper": _fixed(HAIKU, "low"),
            },
            "rules": {
                "raise_or_lower": "tell",
                "lead_steps_down": True,
                "auto_raises": 1,
                "helpers": "own",
            },
        },
        {
            "id": "yolo",
            "pinned": True,
            "title": "YOLO",
            "icon": "rocket",
            "color": "red",
            "description": "Fable at max on every role, Codex as second reviewer",
            "when_to_use": "Hard, high-stakes problems where quality beats cost and nobody "
            "is around to approve a raise.",
            "roles": {
                **{role: _fixed(FABLE, "max") for role in ROLES},
                "reviewer": _reviewers(_fixed(FABLE, "max"), _codex("xhigh")),
            },
            "rules": {"raise_or_lower": "off", "lead_steps_down": False, "auto_raises": 0},
        },
        {
            "id": "think-it-through",
            "title": "Think it through",
            "icon": "brain",
            "color": "violet",
            "description": "Opus xhigh plans and reviews, Sonnet builds",
            "when_to_use": "Design calls, research and architecture: most of the cost goes "
            "into thinking, and the build stays cheap.",
            "roles": {
                "lead": _SESSION,
                "planner": _fixed(OPUS, "xhigh"),
                "reviewer": _reviewers(_fixed(OPUS, "high"), _codex("high")),
                "builder": _fixed(SONNET, "medium"),
                "helper": _fixed(HAIKU, "low"),
            },
            "rules": {"raise_or_lower": "tell", "lead_steps_down": True, "auto_raises": 1},
        },
        {
            "id": "deep-debug",
            "title": "Deep debug",
            "icon": "bug",
            "color": "amber",
            "description": "Opus high everywhere, Codex checks the fix",
            "when_to_use": "A bug whose cause is unknown: every role can root-cause, and "
            "the lead does not step down mid-hunt.",
            "roles": {
                "lead": _SESSION,
                "planner": _fixed(OPUS, "high"),
                "reviewer": _reviewers(_fixed(OPUS, "high"), _codex("high")),
                "builder": _fixed(OPUS, "high"),
                "helper": _fixed(SONNET, "low"),
            },
            "rules": {"raise_or_lower": "tell", "lead_steps_down": False, "auto_raises": 1},
        },
        {
            "id": "quick-fix",
            "title": "Quick fix",
            "icon": "bolt",
            "color": "blue",
            "description": "Sonnet low, one reviewer, Haiku helps",
            "when_to_use": "Small, clear changes such as a copy fix, a config value or a "
            "one-file bug: the cheapest preset that still reviews.",
            "roles": {
                "lead": _SESSION,
                "planner": _fixed(SONNET, "low"),
                "reviewer": _fixed(SONNET, "low"),
                "builder": _fixed(SONNET, "low"),
                "helper": _fixed(HAIKU, "low"),
            },
            "rules": {
                "raise_or_lower": "tell",
                "lead_steps_down": True,
                "auto_raises": 1,
                "helpers": "own",
            },
        },
    )
)


def starters() -> list[dict[str, Any]]:
    """The canonical starters as JSON, in their fixed order."""
    return [preset.model_dump() for preset in STARTERS]


def never_saved() -> RolesState:
    """What a user who never saved reads: the starters, ``balanced`` the default."""
    return RolesState(
        revision=0,
        default_preset=DEFAULT_PRESET,
        apply_start_on=True,
        presets=[preset.model_copy(deep=True) for preset in STARTERS],
        starters_seen=[preset.id for preset in STARTERS],
    )


def with_new_starters(state: RolesState) -> RolesState:
    """*state* with every starter the user has not been offered yet appended.

    A starter counts as offered once it is in the list (appended here or
    already there); one that does not fit under :data:`MAX_PRESETS` stays
    unoffered and is appended on a later read once there is room. The returned
    ``starters_seen`` lists every offered id; :meth:`RolePrefsStore.put` stores it.
    """
    seen = list(state.starters_seen if state.starters_seen is not None else ORIGINAL_STARTERS)
    presets = list(state.presets)
    ids = {preset.id for preset in presets}
    # Presets saved before pins existed keep today's picker: the first append pins
    # what was already there. Only a state offered none of the pin-era starters yet
    # qualifies, so a full list that gets them one at a time is never re-pinned.
    unpinned_state = not any(preset.pinned for preset in presets) and not any(
        starter_id in seen for starter_id in PIN_ERA_STARTERS
    )
    appended = False
    for starter in STARTERS:
        if starter.id in seen:
            continue
        if starter.id not in ids:
            if len(presets) >= MAX_PRESETS:
                continue
            if unpinned_state and not appended:
                presets = [preset.model_copy(update={"pinned": True}) for preset in presets]
            presets.append(starter.model_copy(deep=True))
            ids.add(starter.id)
            appended = True
        seen.append(starter.id)
    return state.model_copy(update={"presets": presets, "starters_seen": seen})


def validate_custom(raw: object) -> dict[str, Any]:
    """Check a per-session ``custom`` block from the ``ae.team`` label.

    :param raw: The decoded ``custom`` value.
    :returns: ``{"roles": ..., "rules": ...}`` as plain JSON.
    :raises ValueError: When it is not a valid roles and rules pair.
    """
    try:
        return Custom.model_validate(raw).model_dump()
    except ValidationError as exc:
        raise ValueError(str(exc)) from exc


class RolesSaveError(OSError):
    """The presets could not be written to the state file; nothing was changed."""


@dataclass(frozen=True)
class PutResult:
    """What :meth:`RolePrefsStore.put` did: the state now held, and whether it conflicted."""

    state: RolesState
    conflict: bool = False


class RolePrefsStore:
    """Per-user presets, optionally mirrored to a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        # user -> the entry as stored on disk (a valid one, or a bad one kept verbatim).
        self._users: dict[str, dict[str, Any]] = {}
        # user -> the parsed state, only for valid entries.
        self._states: dict[str, RolesState] = {}
        self._loaded = False
        self._memory_only = False

    def _quarantine(self, reason: object) -> None:
        assert self._path is not None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        aside = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        try:
            os.replace(self._path, aside)
        except OSError as exc:
            self._memory_only = True
            _logger.warning(
                "ae roles: %s is unreadable (%s) and could not be moved aside (%s); "
                "presets stay in memory until restart. Fix or remove the file.",
                self._path,
                reason,
                exc,
            )
            return
        _logger.warning(
            "ae roles: %s was unreadable (%s); moved it to %s and started empty.",
            self._path,
            reason,
            aside,
        )

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._path is None:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self._quarantine(exc)
            return
        users = raw.get("users") if isinstance(raw, dict) else None
        if not isinstance(users, dict):
            self._quarantine("not a roles state file")
            return
        for user, entry in users.items():
            if not isinstance(entry, dict):
                continue
            revision = entry.get("revision")
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
                continue
            self._users[str(user)] = entry
            try:
                self._states[str(user)] = RolesState.model_validate(
                    {k: v for k, v in entry.items() if k != "updated_at"}
                )
            except ValidationError as exc:
                # Kept verbatim in the file until the user saves; reads as the starters
                # at the stored revision so the next save does not conflict.
                _logger.warning(
                    "ae roles: %s's saved presets are invalid (%s); serving the starters",
                    user,
                    exc.errors()[0].get("msg", "invalid") if exc.errors() else "invalid",
                )

    def _save(self) -> bool:
        if self._path is None or self._memory_only:
            return True
        body = {"version": 1, "users": self._users}
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-roles-", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
            os.replace(tmp, self._path)
        except OSError as exc:
            _logger.warning("ae roles: could not write %s: %s", self._path, exc)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            return False
        return True

    def _current(self, user: str) -> RolesState:
        state = self._states.get(user)
        if state is not None:
            return state.model_copy(deep=True)
        entry = self._users.get(user)
        fallback = never_saved()
        if entry is not None:
            fallback.revision = entry["revision"]
        return fallback

    def get(self, user: str) -> RolesState:
        """The saved presets for *user*; the starters at revision 0 when none were saved.

        Starters added since the user last saved are appended (see :func:`with_new_starters`).
        """
        with self._lock:
            self._load()
            return with_new_starters(self._current(user))

    def all_states(self) -> dict[str, RolesState]:
        """Every user's saved presets (valid entries only), each with new starters appended.

        A user whose saved entry is invalid, or who never saved, is left out. Added with the
        preset reviewer (2026-10-24), which reads other users' lists when an admin runs it.
        """
        with self._lock:
            self._load()
            return {
                user: with_new_starters(state.model_copy(deep=True))
                for user, state in self._states.items()
            }

    def put(self, user: str, body: RolesPut, *, now: datetime | None = None) -> PutResult:
        """Store *body* when its revision is the one held; else report a conflict.

        :param user: The user key.
        :param body: The validated new list and the revision it was based on.
        :returns: The new state at ``body.revision + 1``, or the current state
            with ``conflict`` set when the held revision differs.
        :raises RolesSaveError: When the file could not be written (nothing changed).
        """
        with self._lock:
            self._load()
            current = with_new_starters(self._current(user))
            if current.revision != body.revision:
                return PutResult(current, conflict=True)
            # A starter counts as seen when it was already stored as seen, the client
            # says its read offered it (``starters_seen``), or it is in the saved list.
            # One the server only appended on this read is not seen: a PUT based on a
            # read from before it existed must not hide it.
            stored = self._current(user).starters_seen
            in_list = {preset.id for preset in body.presets}
            offered = [
                *(stored if stored is not None else ORIGINAL_STARTERS),
                *(body.starters_seen or []),
                *(starter.id for starter in STARTERS if starter.id in in_list),
            ]
            seen = list(dict.fromkeys(offered))
            state = RolesState(
                revision=current.revision + 1,
                default_preset=body.default_preset,
                apply_start_on=body.apply_start_on,
                presets=[preset.model_copy(deep=True) for preset in body.presets],
                starters_seen=seen,
                # Absent keeps the saved setting; an explicit null clears it.
                insights=(
                    body.insights if "insights" in body.model_fields_set else current.insights
                ),
            )
            old_entry = self._users.get(user)
            old_state = self._states.get(user)
            self._users[user] = {
                **state.model_dump(),
                "updated_at": (now or datetime.now(UTC)).isoformat(),
            }
            self._states[user] = state
            if not self._save():
                if old_entry is None:
                    del self._users[user]
                else:
                    self._users[user] = old_entry
                if old_state is None:
                    del self._states[user]
                else:
                    self._states[user] = old_state
                raise RolesSaveError(
                    f"could not write {self._path}; check that the folder is writable and "
                    "has space, then retry."
                )
            return PutResult(state.model_copy(deep=True))


def view(state: RolesState) -> dict[str, Any]:
    """The JSON ``GET /roles`` and ``PUT /roles`` answer with: the state plus the starters."""
    return {**state.model_dump(), "starters": starters()}


def _default_path() -> Path | None:
    raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: RolePrefsStore | None = None
_store_lock = threading.Lock()


def store() -> RolePrefsStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = RolePrefsStore(_default_path())
        return _store


def reset_store(replacement: RolePrefsStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


__all__ = [
    "CODEX_AGENT_ID",
    "CODEX_AGENT_NAME",
    "CODE_NOT_SAVED",
    "DATA_DIR",
    "DEFAULT_PRESET",
    "LEAD_PICKS",
    "MAX_PRESETS",
    "MAX_REVIEWERS",
    "ORIGINAL_STARTERS",
    "PIN_ERA_STARTERS",
    "ROLES",
    "SOLO_PRESET",
    "STARTERS",
    "STATE_FILE_ENV",
    "Preset",
    "PutResult",
    "RolePrefsStore",
    "RolesPut",
    "RolesSaveError",
    "RolesState",
    "never_saved",
    "reset_store",
    "starters",
    "store",
    "validate_custom",
    "view",
    "with_new_starters",
]
