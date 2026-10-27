"""Preset suggestions: what the scheduled preset reviewer proposes, and what became of each.

Added 2026-10-24 (preset insights PR 3). The reviewer (``agents/preset-reviewer``) reads how
role presets performed (``preset_review.build_read``) and submits at most five suggestions
through ``submit_preset_suggestions``. This module holds them:

* ``own``: a change to one preset in a user's list, as ``changes`` (``path`` and ``value``)
  the server can apply. ``before`` is the preset when the review ran; Apply refuses with
  ``preset_changed`` when the preset no longer matches it, and keeps ``after`` so Undo can
  tell whether the preset is still what Apply wrote. Presets have only a list revision, no
  per-preset history (``role_prefs``), so the two copies are the undo.
* ``starter``: a change to the shipped starters in code, or one with no field to change. An
  admin turns it into a GitHub issue; the server never edits code.
* ``no_change``: the reviewer checked the preset and found nothing that clears the bar.

Storage copies ``role_prefs``: one JSON file, written atomically under a lock, a corrupt
file moved aside. The file is :data:`STATE_FILE_ENV` when set, else
``/data/ae-preset-suggestions.json`` when ``/data`` is a directory, else memory only. Shape:
``{"version": 1, "reviews": [...], "suggestions": [...], "forced": {owner: epoch},
"due_reads": {session: epoch}}``. ``due_reads`` names the sessions whose read answered due, the
only ones allowed to submit.
``forced`` is the "Run now" flag: it makes the next ``preset_insights_read`` for that owner
due even inside the 13-day gap, and is consumed by that read.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ae_omni_policies import role_prefs

_logger = logging.getLogger(__name__)

STATE_FILE_ENV = "AE_PRESET_SUGGESTIONS_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-preset-suggestions.json"
MAX_REVIEWS = 50
MAX_SUGGESTIONS = 300
MAX_PER_REVIEW = 5
MAX_CHANGES = 6
LIST_LIMIT = 100
FORCED_MAX_AGE_S = 30 * 60.0
RESERVATION_TTL_S = 2 * 60.0
MAX_DUE_READS = 200
DROPPABLE_STATUSES = frozenset({"dismissed", "issue_opened", "undone"})

Kind = Literal["own", "starter", "no_change"]
Status = Literal[
    "open",
    "applying",
    "applied",
    "undoing",
    "dismissed",
    "issue_opening",
    "issue_opened",
    "undone",
]
MAX_VALUE_BYTES = 2000

_ROLE_NAMES = "|".join(role_prefs.ROLES)
_RULE_NAMES = (
    "raise_or_lower|lead_steps_down|auto_raises|helpers|review_merge|reviewer_unavailable"
)
# roles.<role>, roles.reviewer.reviewers.<0-2>, rules.<rule>
PATH_PATTERN = re.compile(
    rf"^(?:roles\.(?:{_ROLE_NAMES})|roles\.reviewer\.reviewers\.[0-2]|rules\.(?:{_RULE_NAMES}))$"
)


class SuggestionError(Exception):
    """A suggestion step refused; carries the HTTP status and a code for the route."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class Change(BaseModel):
    """One field change: a path from the allowlist and the value to put there."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(max_length=60)
    value: Any

    @model_validator(mode="after")
    def _allowed(self) -> Change:
        size = len(json.dumps(self.value, separators=(",", ":")).encode("utf-8"))
        if size > MAX_VALUE_BYTES:
            raise ValueError(
                f"the value for {self.path!r} is {size} bytes; the limit is {MAX_VALUE_BYTES}"
            )
        if not PATH_PATTERN.match(self.path):
            raise ValueError(
                f"path {self.path!r} is not one of roles.<role>, "
                "roles.reviewer.reviewers.<0-2> or rules.<rule>"
            )
        return self


class Item(BaseModel):
    """One suggestion as the reviewer submits it."""

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    owner: str | None = Field(default=None, max_length=200)
    preset_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    title: str = Field(min_length=1, max_length=120)
    why: str = Field(min_length=1, max_length=800)
    saving: str | None = Field(default=None, max_length=200)
    changes: list[Change] = Field(default_factory=list, max_length=MAX_CHANGES)

    @model_validator(mode="after")
    def _by_kind(self) -> Item:
        if self.kind in ("own", "no_change") and not self.preset_id:
            raise ValueError(f"a {self.kind} suggestion needs a preset_id")
        if self.kind in ("own", "starter") and not (self.saving or "").strip():
            raise ValueError(f"a {self.kind} suggestion needs a saving")
        if self.kind == "own" and not self.changes:
            raise ValueError("an own suggestion needs at least one change")
        if self.kind == "no_change" and self.changes:
            raise ValueError("a no_change item carries no changes")
        return self


class SubmitBody(BaseModel):
    """``POST /sessions/{id}/preset-suggestions``."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=800)
    suggestions: list[Item] = Field(max_length=MAX_PER_REVIEW)


class _StoredReview(BaseModel):
    """A review entry as the file holds it; an entry that does not fit is dropped at load."""

    model_config = ConfigDict(extra="allow")

    id: str
    session: str
    owner: str
    admin: bool = False
    at: str
    summary: str
    count: int


class _StoredChange(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str
    value: Any = None


class _StoredSuggestion(BaseModel):
    """A suggestion record as the file holds it; one that does not fit is dropped at load."""

    model_config = ConfigDict(extra="allow")

    id: str
    review_id: str
    review_session: str
    owner: str
    kind: Kind
    preset_id: str | None = None
    preset_title: str | None = None
    title: str
    why: str
    saving: str | None = None
    changes: list[_StoredChange] = Field(default_factory=list)
    status: Status
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    applied_revision: int | None = None
    issue_url: str | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def _own_has_before(self) -> _StoredSuggestion:
        if self.kind == "own" and self.before is None:
            raise ValueError("an own suggestion needs its before copy")
        return self


def _valid_records(
    raw: list[Any], model: type[BaseModel], what: str, path: Path | None
) -> list[dict[str, Any]]:
    """The entries of *raw* that fit *model*; each one dropped is logged."""
    kept: list[dict[str, Any]] = []
    for entry in raw:
        try:
            if not isinstance(entry, dict):
                raise ValueError("not an object")
            model.model_validate(entry)
        except (ValueError, ValidationError) as exc:
            _logger.warning(
                "preset suggestions: dropped an invalid %s from %s (%s): %s",
                what,
                path,
                _short(exc),
                str(entry.get("id") if isinstance(entry, dict) else entry)[:60],
            )
            continue
        kept.append(entry)
    return kept


def _set_path(dump: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node: Any = dump
    for part in parts[:-1]:
        if isinstance(node, list):
            index = int(part)
            if index >= len(node):
                raise ValueError(f"{path}: there is no reviewer {index + 1} to change")
            node = node[index]
        else:
            if part not in node or not isinstance(node[part], dict | list):
                raise ValueError(f"{path}: there is nothing at {part!r} to change")
            node = node[part]
    last = parts[-1]
    if isinstance(node, list):
        index = int(last)
        if index >= len(node):
            raise ValueError(f"{path}: there is no reviewer {index + 1} to change")
        node[index] = value
    else:
        node[last] = value


def apply_changes(
    preset_dump: dict[str, Any], changes: list[Change] | list[dict[str, Any]]
) -> Any:
    """The preset with *changes* applied, validated.

    :param preset_dump: A preset as ``Preset.model_dump(mode="json")`` gives it.
    :param changes: ``Change`` objects or ``{"path", "value"}`` dicts.
    :returns: The new :class:`role_prefs.Preset`.
    :raises ValueError: When a path does not fit the preset or the result is not a valid preset
        (a ``pydantic.ValidationError`` is one).
    """
    dump = copy.deepcopy(preset_dump)
    for change in changes:
        path, value = (
            (change.path, change.value)
            if isinstance(change, Change)
            else (str(change.get("path")), change.get("value"))
        )
        if not PATH_PATTERN.match(path):
            raise ValueError(f"path {path!r} is not allowed")
        _set_path(dump, path, copy.deepcopy(value))
    return role_prefs.Preset.model_validate(dump)


def _dump(preset: Any) -> dict[str, Any]:
    return preset.model_dump(mode="json")


def _iso(now: datetime) -> str:
    return now.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _describe_role(value: Any) -> str:
    if isinstance(value, dict):
        mode = value.get("mode")
        if mode == "fixed":
            return f"{value.get('model')} {value.get('effort')}"
        if mode == "agent":
            return f"agent {value.get('agent_name') or value.get('agent')}"
        if mode == "multi":
            return f"{len(value.get('reviewers') or [])} reviewers"
        if mode:
            return str(mode)
    return json.dumps(value, separators=(",", ":"))


def _walk(dump: Any, path: str) -> Any:
    node = dump
    for part in path.split("."):
        try:
            node = node[int(part)] if isinstance(node, list) else node[part]
        except (KeyError, IndexError, TypeError, ValueError):
            return None
    return node


def changes_text(record: dict[str, Any]) -> str:
    """One line per change, from the preset before and after: what Apply does or did.

    A role row reads ``Builder: claude-opus-5-5 high to claude-sonnet-5-5 medium``; anything else
    reads ``path: old to new``. Empty for a record with no changes.
    """
    changes = record.get("changes") or []
    before = record.get("before")
    after = record.get("after")
    if after is None and isinstance(before, dict):
        try:
            after = _dump(apply_changes(before, changes))
        except (ValueError, TypeError):
            after = None
    lines: list[str] = []
    for change in changes:
        path = str(change.get("path"))
        new = change.get("value")
        old = _walk(before, path) if isinstance(before, dict) else None
        if isinstance(after, dict):
            new = _walk(after, path)
        match = re.fullmatch(r"roles\.(\w+)", path)
        if match and match.group(1) in role_prefs.ROLES:
            name = match.group(1).capitalize()
            lines.append(f"{name}: {_describe_role(old)} to {_describe_role(new)}")
        else:
            lines.append(
                f"{path}: {json.dumps(old, separators=(',', ':'))} to "
                f"{json.dumps(new, separators=(',', ':'))}"
            )
    return "\n".join(lines)


def bare_title(title: str, preset_title: str | None) -> str:
    """*title* without a leading ``"<preset_title>:"``; the UIs put the preset name in front.

    Returns ``""`` when the title is nothing but the preset name, which submit refuses.
    """
    if preset_title and ":" in title:
        head, rest = title.split(":", 1)
        if head.strip().casefold() == preset_title.strip().casefold():
            return rest.strip()
    return title


def public(record: dict[str, Any], viewer: str | None) -> dict[str, Any]:
    """*record* for a client: no ``before``/``after``, plus ``can_apply``, ``can_undo`` and
    ``changes_text`` (server-rendered, one line per change)."""
    mine = viewer is not None and record.get("owner") == viewer
    out = {k: v for k, v in record.items() if k not in ("before", "after", "prev_status")}
    out["can_apply"] = bool(
        mine
        and record.get("kind") == "own"
        and record.get("status") in ("open", "undone", "applying")
    )
    out["can_undo"] = bool(mine and record.get("status") in ("applied", "undoing"))
    out["changes_text"] = changes_text(record) if record.get("kind") == "own" else ""
    if isinstance(out.get("title"), str):
        out["title"] = bare_title(out["title"], out.get("preset_title")) or out["title"]
    return out


class SuggestionStore:
    """Reviews and suggestions, optionally mirrored to a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        # Lock order: this lock is held across ``roles.put`` in apply and undo, so role_prefs
        # must never call back into this module (it would take the two locks in the other order).
        self._lock = threading.RLock()
        self._path = path
        self._data: dict[str, Any] = self._empty()
        self._loaded = False
        self._memory_only = False

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": 1, "reviews": [], "suggestions": [], "forced": {}, "due_reads": {}}

    def _quarantine(self, reason: object) -> None:
        assert self._path is not None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        aside = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        try:
            os.replace(self._path, aside)
        except OSError as exc:
            self._memory_only = True
            _logger.warning(
                "preset suggestions: %s is unreadable (%s) and could not be moved aside (%s); "
                "suggestions stay in memory until restart.",
                self._path,
                reason,
                exc,
            )
            return
        _logger.warning(
            "preset suggestions: %s was unreadable (%s); moved it to %s and started empty.",
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
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("reviews"), list)
            or not isinstance(raw.get("suggestions"), list)
        ):
            self._quarantine("not a preset suggestions file")
            return
        forced = raw.get("forced")
        due_reads = raw.get("due_reads")
        self._data = {
            "version": 1,
            "reviews": _valid_records(raw["reviews"], _StoredReview, "review", self._path),
            "suggestions": _valid_records(
                raw["suggestions"], _StoredSuggestion, "suggestion", self._path
            ),
            "forced": forced if isinstance(forced, dict) else {},
            "due_reads": due_reads if isinstance(due_reads, dict) else {},
        }

    def _save(self) -> bool:
        if self._path is None or self._memory_only:
            return True
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=".ae-preset-suggestions-", dir=str(self._path.parent)
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, separators=(",", ":"))
            os.replace(tmp, self._path)
        except OSError as exc:
            _logger.warning("preset suggestions: could not write %s: %s", self._path, exc)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            return False
        return True

    def _commit(self, snapshot: dict[str, Any]) -> None:
        """Save, or put *snapshot* back and raise when the file cannot be written."""
        if not self._save():
            self._data = snapshot
            raise SuggestionError(
                503,
                "suggestions_not_saved",
                f"could not write {self._path}; check that the folder is writable, then retry.",
            )

    def _trim(self) -> None:
        reviews = self._data["reviews"]
        if len(reviews) > MAX_REVIEWS:
            newest: dict[Any, int] = {}
            for index, review in enumerate(reviews):
                newest[review.get("owner")] = index
            keep_always = set(newest.values())
            excess = len(reviews) - MAX_REVIEWS
            drop: set[int] = set()
            for index in range(len(reviews)):
                if len(drop) >= excess:
                    break
                if index not in keep_always:
                    drop.add(index)
            self._data["reviews"] = [r for i, r in enumerate(reviews) if i not in drop]
        sugs = self._data["suggestions"]
        # Only finished records go, oldest first: an open own or starter suggestion, or one
        # applied, applying, undoing or issue_opening, is still something a person can act on or
        # undo. When nothing is droppable the list stays over the cap rather than lose one.
        while len(sugs) > MAX_SUGGESTIONS:
            index = next((i for i, s in enumerate(sugs) if _droppable(s)), None)
            if index is None:
                break
            del sugs[index]

    def _find(self, sid: str) -> dict[str, Any]:
        for record in self._data["suggestions"]:
            if record.get("id") == sid:
                return record
        raise SuggestionError(404, "not_found", f"no suggestion {sid}")

    # -- reads

    def reviews(self) -> list[dict[str, Any]]:
        """Every review entry, oldest first (copies)."""
        with self._lock:
            self._load()
            return copy.deepcopy(self._data["reviews"])

    def all_suggestions(self) -> list[dict[str, Any]]:
        """Every stored suggestion, any status, oldest first (copies; not capped like a list)."""
        with self._lock:
            self._load()
            return copy.deepcopy(self._data["suggestions"])

    def get(self, sid: str) -> dict[str, Any]:
        """One suggestion record (a copy).

        :raises SuggestionError: 404 when there is none.
        """
        with self._lock:
            self._load()
            return copy.deepcopy(self._find(sid))

    def list_for(self, owner: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """``(suggestions, reviews)``, newest first, last :data:`LIST_LIMIT`; all if None."""
        with self._lock:
            self._load()
            sugs = [
                s for s in self._data["suggestions"] if owner is None or s.get("owner") == owner
            ]
            reviews = [
                r for r in self._data["reviews"] if owner is None or r.get("owner") == owner
            ]
            return (
                copy.deepcopy(sugs[::-1][:LIST_LIMIT]),
                copy.deepcopy(reviews[::-1][:LIST_LIMIT]),
            )

    def last_review(self, owner: str) -> dict[str, Any] | None:
        """The newest review entry whose owner is *owner*, or None."""
        with self._lock:
            self._load()
            for review in reversed(self._data["reviews"]):
                if review.get("owner") == owner:
                    return copy.deepcopy(review)
            return None

    def open_count(self, owner: str) -> int:
        """How many of *owner*'s own and starter suggestions are still open."""
        with self._lock:
            self._load()
            return sum(
                1
                for s in self._data["suggestions"]
                if s.get("owner") == owner
                and s.get("status") == "open"
                and s.get("kind") != "no_change"
            )

    # -- the run-now flag

    def force(self, owner: str, now: float) -> None:
        """Mark *owner*'s next reviewer read due, whatever the last review's age."""
        with self._lock:
            self._load()
            snapshot = copy.deepcopy(self._data)
            self._data["forced"][owner] = now
            self._commit(snapshot)

    def take_forced(self, owner: str, now: float) -> bool:
        """True when a fresh (under 30 minutes) flag was set; the flag is consumed either way."""
        with self._lock:
            self._load()
            stamp = self._data["forced"].get(owner)
            if stamp is None:
                return False
            snapshot = copy.deepcopy(self._data)
            del self._data["forced"][owner]
            self._commit(snapshot)
            return isinstance(stamp, int | float) and 0 <= now - stamp < FORCED_MAX_AGE_S

    def peek_forced(self, owner: str, now: float) -> bool:
        """Whether a fresh flag is set, without consuming it."""
        with self._lock:
            self._load()
            stamp = self._data["forced"].get(owner)
            return isinstance(stamp, int | float) and 0 <= now - stamp < FORCED_MAX_AGE_S

    # -- due reads

    def note_due_read(self, session: str, now: float) -> None:
        """Remember that *session* was told its read is due: only such a session may submit."""
        with self._lock:
            self._load()
            snapshot = copy.deepcopy(self._data)
            reads = self._data.setdefault("due_reads", {})
            reads[session] = now
            while len(reads) > MAX_DUE_READS:
                del reads[min(reads, key=lambda k: float(reads[k] or 0))]
            self._commit(snapshot)

    def has_due_read(self, session: str) -> bool:
        """Whether *session* was ever told its read is due."""
        with self._lock:
            self._load()
            return session in self._data.get("due_reads", {})

    # -- submit

    def has_review_for(self, session: str) -> bool:
        """Whether *session* already submitted."""
        with self._lock:
            self._load()
            return any(r.get("session") == session for r in self._data["reviews"])

    def submit(
        self,
        *,
        session: str,
        owner: str,
        admin: bool,
        body: SubmitBody,
        roles_for: Callable[[str], Any],
        now: datetime,
    ) -> dict[str, Any]:
        """Validate and store *body*; returns the review entry.

        :param roles_for: ``user -> RolesState`` (or None) for the user a suggestion names.
        :raises SuggestionError: 409 ``already_submitted``; 422 ``invalid_suggestion`` naming the
            item index.
        """
        with self._lock:
            self._load()
            if any(r.get("session") == session for r in self._data["reviews"]):
                raise SuggestionError(
                    409, "already_submitted", "this session already submitted its suggestions"
                )
            stamp = _iso(now)
            review_id = _new_id("rv")
            records: list[dict[str, Any]] = []
            for index, item in enumerate(body.suggestions):
                target = item.owner or owner
                if target != owner and not admin:
                    raise SuggestionError(
                        422,
                        "invalid_suggestion",
                        f"suggestions[{index}]: owner must be {owner!r} unless the review "
                        "owner is an admin reading all presets",
                    )
                state = roles_for(target)
                preset = None
                if state is not None and item.preset_id:
                    preset = next((p for p in state.presets if p.id == item.preset_id), None)
                starter = next((p for p in role_prefs.STARTERS if p.id == item.preset_id), None)
                if item.kind == "own" and preset is None:
                    raise SuggestionError(
                        422,
                        "invalid_suggestion",
                        f"suggestions[{index}]: {target!r} has no preset {item.preset_id!r}",
                    )
                before = after_check = None
                if item.kind == "own" and starter is not None and _dump(preset) == _dump(starter):
                    raise SuggestionError(
                        422,
                        "invalid_suggestion",
                        f"suggestions[{index}]: preset {item.preset_id!r} is not in the read's "
                        "presets for this owner (it still matches the shipped starter); "
                        "use a starter suggestion",
                    )
                if item.kind == "own":
                    before = _dump(preset)
                    try:
                        after_check = _dump(apply_changes(before, item.changes))
                    except ValueError as exc:
                        raise SuggestionError(
                            422,
                            "invalid_suggestion",
                            f"suggestions[{index}]: the changes do not make a valid preset: "
                            f"{_short(exc)}",
                        ) from exc
                    if after_check == before:
                        raise SuggestionError(
                            422,
                            "invalid_suggestion",
                            f"suggestions[{index}]: the changes leave the preset as it is",
                        )
                shown = preset or starter
                title = bare_title(item.title, shown.title if shown is not None else None)
                if not title:
                    raise SuggestionError(
                        422,
                        "invalid_suggestion",
                        f"suggestions[{index}]: the title is only the preset name; say what "
                        "the suggestion is (the preset name is shown in front of it)",
                    )
                records.append(
                    {
                        "id": _new_id("sg"),
                        "review_id": review_id,
                        "review_session": session,
                        "owner": target,
                        "kind": item.kind,
                        "preset_id": item.preset_id,
                        "preset_title": shown.title if shown is not None else None,
                        "title": title,
                        "why": item.why,
                        "saving": (item.saving or "").strip() or None,
                        "changes": [c.model_dump() for c in item.changes],
                        "status": "open",
                        "before": before,
                        "after": None,
                        "applied_revision": None,
                        "issue_url": None,
                        "created_at": stamp,
                        "updated_at": stamp,
                    }
                )
            review = {
                "id": review_id,
                "session": session,
                "owner": owner,
                "admin": admin,
                "at": stamp,
                "summary": body.summary.strip(),
                "count": len(records),
            }
            snapshot = copy.deepcopy(self._data)
            self._data["reviews"].append(review)
            self._data["suggestions"].extend(records)
            self._trim()
            self._commit(snapshot)
            return copy.deepcopy(review)

    # -- state machine

    def _set_status(
        self, record: dict[str, Any], status: Status, now: datetime, **fields: Any
    ) -> None:
        snapshot = copy.deepcopy(self._data)
        record.update({"status": status, "updated_at": _iso(now), **fields})
        self._commit(snapshot)

    def _review_date(self, record: dict[str, Any]) -> str:
        for review in self._data["reviews"]:
            if review.get("id") == record.get("review_id"):
                return str(review.get("at", ""))[:10]
        return str(record.get("created_at", ""))[:10]

    def _swap(
        self,
        roles: Any,
        user: str,
        record: dict[str, Any],
        expect: dict[str, Any],
        new: dict[str, Any],
    ) -> int:
        """Put *new* in place of the preset when it still equals *expect*; the new revision."""
        preset_id = record["preset_id"]
        for attempt in range(2):
            state = roles.get(user)
            current = next((p for p in state.presets if p.id == preset_id), None)
            if current is None:
                raise SuggestionError(
                    409, "preset_gone", f"The preset {preset_id!r} no longer exists."
                )
            if _dump(current) != expect:
                raise SuggestionError(
                    409,
                    "preset_changed",
                    f"This preset changed since the review on {self._review_date(record)}. "
                    "Dismiss it or edit the preset yourself.",
                )
            swapped = [
                role_prefs.Preset.model_validate(new) if p.id == preset_id else p
                for p in state.presets
            ]
            put = role_prefs.RolesPut(
                revision=state.revision,
                default_preset=state.default_preset,
                apply_start_on=state.apply_start_on,
                presets=swapped,
                starters_seen=state.starters_seen,
                insights=state.insights,
            )
            try:
                result = roles.put(user, put)
            except role_prefs.RolesSaveError as exc:
                raise SuggestionError(503, role_prefs.CODE_NOT_SAVED, str(exc)) from exc
            if not result.conflict:
                return int(result.state.revision)
            _ = attempt
        raise SuggestionError(
            409, "conflict", "The role presets changed on another device; reload and retry."
        )

    def _live(self, roles: Any, user: str, record: dict[str, Any]) -> tuple[Any, int]:
        """``(the live preset as a dump or None, the list revision)``."""
        state = roles.get(user)
        current = next((p for p in state.presets if p.id == record["preset_id"]), None)
        return (_dump(current) if current is not None else None), int(state.revision)

    def _recover(self, roles: Any, user: str, record: dict[str, Any], when: datetime) -> bool:
        """Settle a record left in ``applying`` or ``undoing`` by a failed write.

        The live preset decides: equal to the step's target means the swap landed, so the final
        status is recorded and True returns; otherwise the status goes back to what it was.
        """
        status = record.get("status")
        if status not in ("applying", "undoing"):
            return False
        target = record.get("after") if status == "applying" else record.get("before")
        live, revision = self._live(roles, user, record)
        if live is not None and live == target:
            self._set_status(
                record,
                "applied" if status == "applying" else "undone",
                when,
                applied_revision=revision,
            )
            return True
        self._set_status(record, record.get("prev_status") or "open", when)
        return False

    def _two_step(
        self,
        roles: Any,
        user: str,
        record: dict[str, Any],
        when: datetime,
        *,
        working: Status,
        done: Status,
        expect: dict[str, Any],
        new: dict[str, Any],
        fields: dict[str, Any],
    ) -> int:
        """Record *working* first, swap the preset, then record *done*.

        A failed swap puts the status back; a failed final write leaves *working*, which the
        next Apply or Undo settles through :meth:`_recover`.
        """
        prior = record.get("status")
        self._set_status(record, working, when, prev_status=prior, **fields)
        try:
            revision = self._swap(roles, user, record, expect, new)
        except SuggestionError:
            with contextlib.suppress(SuggestionError):
                self._set_status(record, prior, when)
            raise
        self._set_status(record, done, when, applied_revision=revision)
        return revision

    def apply(
        self, user: str, sid: str, *, roles: Any | None = None, now: datetime | None = None
    ) -> tuple[dict[str, Any], int]:
        """Apply an ``own`` suggestion to *user*'s preset; ``(record, new revision)``.

        The record is written as ``applying`` (with ``after``) before the preset changes and as
        ``applied`` after, so a failure between the two is found and settled on the next try.

        :raises SuggestionError: 404, 403 ``not_owner``, 409 ``not_applicable``,
            409 ``preset_changed``, 409 ``preset_gone``, 409 ``conflict``, 422 ``invalid_change``.
        """
        roles = roles or role_prefs.store()
        when = now or datetime.now(UTC)
        with self._lock:
            self._load()
            record = self._find(sid)
            if record.get("owner") != user:
                raise SuggestionError(403, "not_owner", "only the preset's owner can apply this")
            if record.get("kind") != "own" or record.get("status") not in (
                "open",
                "undone",
                "applying",
            ):
                raise SuggestionError(
                    409, "not_applicable", "this suggestion cannot be applied in its state"
                )
            if self._recover(roles, user, record, when):
                return copy.deepcopy(record), int(record.get("applied_revision") or 0)
            before = record.get("before")
            try:
                new = _dump(apply_changes(before, record.get("changes") or []))
            except (ValueError, TypeError) as exc:
                raise SuggestionError(
                    422, "invalid_change", f"The change no longer fits: {_short(exc)}"
                ) from exc
            revision = self._two_step(
                roles,
                user,
                record,
                when,
                working="applying",
                done="applied",
                expect=before,
                new=new,
                fields={"after": new},
            )
            return copy.deepcopy(record), revision

    def undo(
        self, user: str, sid: str, *, roles: Any | None = None, now: datetime | None = None
    ) -> tuple[dict[str, Any], int]:
        """Put the preset back to ``before`` when it is still ``after``; ``(record, revision)``.

        Written as ``undoing`` before the swap and ``undone`` after, like :meth:`apply`.
        """
        roles = roles or role_prefs.store()
        when = now or datetime.now(UTC)
        with self._lock:
            self._load()
            record = self._find(sid)
            if record.get("owner") != user:
                raise SuggestionError(403, "not_owner", "only the preset's owner can undo this")
            if record.get("status") not in ("applied", "undoing") or not record.get("after"):
                raise SuggestionError(
                    409, "not_applicable", "only an applied suggestion can be undone"
                )
            if self._recover(roles, user, record, when):
                return copy.deepcopy(record), int(record.get("applied_revision") or 0)
            revision = self._two_step(
                roles,
                user,
                record,
                when,
                working="undoing",
                done="undone",
                expect=record["after"],
                new=record["before"],
                fields={},
            )
            return copy.deepcopy(record), revision

    def dismiss(
        self, user: str, sid: str, admin: bool, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Close an open or undone suggestion; its owner, or an admin for any."""
        when = now or datetime.now(UTC)
        with self._lock:
            self._load()
            record = self._find(sid)
            if record.get("owner") != user and not admin:
                raise SuggestionError(
                    403, "not_owner", "only the owner or an admin can dismiss this"
                )
            if record.get("status") not in ("open", "undone"):
                raise SuggestionError(
                    409, "not_applicable", "only an open or undone suggestion can be dismissed"
                )
            self._set_status(record, "dismissed", when)
            return copy.deepcopy(record)

    def reserve_issue(self, sid: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Mark an open ``starter`` suggestion ``issue_opening`` before the GitHub call.

        :raises SuggestionError: 409 ``issue_in_progress`` when another request holds it;
            409 ``not_applicable`` when it is not an open starter suggestion.
        """
        when = now or datetime.now(UTC)
        with self._lock:
            self._load()
            record = self._find(sid)
            if record.get("status") == "issue_opening" and not self._reservation_stale(
                record, when
            ):
                raise SuggestionError(
                    409, "issue_in_progress", "an issue is already being opened for this"
                )
            if record.get("kind") != "starter" or record.get("status") not in (
                "open",
                "issue_opening",
            ):
                raise SuggestionError(
                    409, "not_applicable", "only an open starter suggestion becomes an issue"
                )
            self._set_status(record, "issue_opening", when, reserved_at=_iso(when))
            return copy.deepcopy(record)

    @staticmethod
    def _reservation_stale(record: dict[str, Any], now: datetime) -> bool:
        """Whether an ``issue_opening`` hold is older than :data:`RESERVATION_TTL_S` (or has no
        usable stamp), so a request that died mid-call never blocks the suggestion for good."""
        try:
            held = datetime.fromisoformat(str(record.get("reserved_at")).replace("Z", "+00:00"))
        except ValueError:
            return True
        return (now - held).total_seconds() >= RESERVATION_TTL_S

    def release_issue(self, sid: str, *, now: datetime | None = None) -> None:
        """Put an ``issue_opening`` suggestion back to ``open`` after the GitHub call failed."""
        when = now or datetime.now(UTC)
        with self._lock:
            self._load()
            record = self._find(sid)
            if record.get("status") == "issue_opening":
                self._set_status(record, "open", when)

    def mark_issue(self, sid: str, url: str, *, now: datetime | None = None) -> dict[str, Any]:
        """Record the GitHub issue opened for a ``starter`` suggestion (open or reserved)."""
        when = now or datetime.now(UTC)
        with self._lock:
            self._load()
            record = self._find(sid)
            if record.get("kind") != "starter" or record.get("status") not in (
                "open",
                "issue_opening",
            ):
                raise SuggestionError(
                    409, "not_applicable", "only an open starter suggestion becomes an issue"
                )
            self._set_status(record, "issue_opened", when, issue_url=url)
            return copy.deepcopy(record)


def _droppable(record: dict[str, Any]) -> bool:
    """Whether :meth:`SuggestionStore._trim` may drop *record*: finished, nothing left to do."""
    return record.get("kind") == "no_change" or record.get("status") in DROPPABLE_STATUSES


def _short(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()))
        return f"{where}: {first.get('msg', 'invalid')}" if where else str(first.get("msg", exc))
    return " ".join(str(exc).split())[:300]


def _default_path() -> Path | None:
    raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: SuggestionStore | None = None
_store_lock = threading.Lock()


def store() -> SuggestionStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SuggestionStore(_default_path())
        return _store


def reset_store(replacement: SuggestionStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


__all__ = [
    "FORCED_MAX_AGE_S",
    "PATH_PATTERN",
    "RESERVATION_TTL_S",
    "STATE_FILE_ENV",
    "Change",
    "Item",
    "SubmitBody",
    "SuggestionError",
    "SuggestionStore",
    "apply_changes",
    "changes_text",
    "public",
    "reset_store",
    "store",
]
