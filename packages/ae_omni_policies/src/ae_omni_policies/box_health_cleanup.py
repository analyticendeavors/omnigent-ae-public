"""The server's side of the Box health cleanup buttons (2026-10-29).

A box admin presses a button; the server keeps one request in ``/data``; the box's relay
(``box_health_relay.py``) finds it in the answer to its next post (within five minutes),
runs it as its own user, and posts the result. The server never reaches into the box.

Lifecycle, derived from the stored times so nothing needs a timer:

======== =============================================================================
queued   asked, not yet handed to the relay (under 15 minutes)
untaken  queued for 15 to 60 minutes: the box's timer may be stopped
expired  never picked up in 60 minutes
running  handed to the relay, no result yet (under 30 minutes)
lost     handed over more than 30 minutes ago with no result
done     the relay's result, ``ok``
failed   the relay's result, not ``ok``
cancelled  withdrawn while still queued
======== =============================================================================

Only one request is active at a time; done, failed, expired, lost and cancelled are shown
for 24 hours and then forgotten.

Privacy: the request holds an action word, an owner label and counts. The audit log line
names the box admin, the action, the owner label and the counts, never a folder, project,
session or file name. The card shows who ran it only to OmniGent admins.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from ae_omni_policies import box_health

_logger = logging.getLogger("ae_omni_policies.box_health_cleanup")

FILE_ENV = "AE_BOX_HEALTH_CLEANUP_FILE"
DATA_FILE_NAME = "ae-box-health-cleanup.json"

UNTAKEN_S = 10 * 60
EXPIRED_S = 15 * 60
"""A request the box has not taken in 15 minutes expires and is never handed over later
(2026-10-29, review of PR 395): what an admin confirmed describes the box now, not an hour on."""
LOST_S = 30 * 60
KEEP_S = 24 * 3600

CODE_BUSY = "box_health_busy"
CODE_STALE = "box_health_stale"
CODE_CHANGED = "box_health_changed"
CODE_LABEL = "box_health_label"

CleanupState = Literal[
    "queued", "untaken", "expired", "running", "lost", "done", "failed", "cancelled"
]
ACTIVE = frozenset({"queued", "untaken", "running"})

NAMES = {
    "claude_scratch": "Claude temp folders",
    "tmp_litter": "test leftovers in /tmp",
    "review_checkouts": "review checkouts in /tmp",
    "docker_prune": "Docker images and build cache",
}


class Result(BaseModel):
    """What the relay did. Counts and fixed words only."""

    id: str
    ok: bool
    removed: Annotated[int, Field(ge=0, le=10**9)] = 0
    bytes_freed: Annotated[int, Field(ge=0, le=10**15)] = 0
    skipped: dict[str, Annotated[int, Field(ge=0, le=10**9)]] = Field(default_factory=dict)
    """Fixed reason words only: a word this server does not know (a relay newer than the
    server) is dropped, not rejected, so a finished removal is never answered with a 422."""
    errors: list[box_health.ErrorCode] = Field(default_factory=list, max_length=20)
    partial: bool = False
    finished_at: datetime | None = None

    @field_validator("skipped", mode="before")
    @classmethod
    def _known_reasons(cls, value: object) -> object:
        if isinstance(value, dict):
            return {k: v for k, v in value.items() if k in box_health.SKIP_REASONS}
        return value


class Record(BaseModel):
    id: str
    action: box_health.CleanupAction
    owner: str
    count: int
    files: int = 0
    bytes: int = 0
    by: str
    requested_at: datetime
    taken_at: datetime | None = None
    cancelled_at: datetime | None = None
    finished_at: datetime | None = None
    result: Result | None = None


def _secs(now: datetime, then: datetime | None) -> float:
    return (now - then).total_seconds() if then else 0.0


def state_of(record: Record, now: datetime) -> CleanupState | None:
    """The record's state now; ``None`` once a finished one is older than a day."""
    if record.result is not None:
        done_at = record.finished_at or record.taken_at or record.requested_at
        if _secs(now, done_at) > KEEP_S:
            return None
        return "done" if record.result.ok else "failed"
    if record.cancelled_at is not None:
        return None if _secs(now, record.cancelled_at) > KEEP_S else "cancelled"
    if record.taken_at is not None:
        age = _secs(now, record.taken_at)
        if age > KEEP_S:
            return None
        return "lost" if age > LOST_S else "running"
    age = _secs(now, record.requested_at)
    if age >= EXPIRED_S:
        return None if age > EXPIRED_S + KEEP_S else "expired"
    return "untaken" if age >= UNTAKEN_S else "queued"


class CleanupStore:
    """The one request, in a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self._memory: Record | None = None

    def read(self) -> Record | None:
        with self._lock:
            if self._path is None:
                return self._memory
            try:
                return Record.model_validate_json(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

    def _write(self, record: Record | None) -> None:
        if self._path is None:
            self._memory = record
            return
        if record is None:
            with contextlib.suppress(OSError):
                self._path.unlink()
            return
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-box-cleanup-", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(record.model_dump_json())
            os.replace(tmp, self._path)
        except OSError:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            raise

    def request(
        self,
        *,
        action: box_health.CleanupAction,
        owner: str,
        offer: box_health.OfferItem,
        by: str,
        now: datetime,
    ) -> Record | None:
        """Store a new request; ``None`` when another is still active.

        :raises OSError: When the file cannot be written.
        """
        with self._lock:
            before = self.read()
            if before is not None and state_of(before, now) in ACTIVE:
                return None
            record = Record(
                id=uuid.uuid4().hex,
                action=action,
                owner=owner,
                count=offer.count,
                files=offer.files,
                bytes=offer.bytes,
                by=by,
                requested_at=now,
            )
            self._write(record)
            return record

    def cancel(self, now: datetime) -> Literal["ok", "none", "taken"]:
        with self._lock:
            record = self.read()
            if record is None or state_of(record, now) not in ACTIVE | {"expired"}:
                return "none"
            if record.taken_at is not None:
                return "taken"
            self._write(record.model_copy(update={"cancelled_at": now}))
            return "ok"

    def take(self, now: datetime) -> Record | None:
        """Hand a queued request to the relay (once): marks it taken and returns it."""
        with self._lock:
            record = self.read()
            if record is None or record.taken_at or record.cancelled_at or record.result:
                return None
            if state_of(record, now) not in {"queued", "untaken"}:
                return None
            taken = record.model_copy(update={"taken_at": now})
            self._write(taken)
            return taken

    def finish(self, result: Result, now: datetime) -> bool:
        """Keep the relay's result; ``False`` for a wrong or already-finished id."""
        with self._lock:
            record = self.read()
            if (
                record is None
                or record.id != result.id
                or record.taken_at is None
                or record.result is not None
                or record.cancelled_at is not None
            ):
                return False
            self._write(
                record.model_copy(
                    update={"result": result, "finished_at": result.finished_at or now}
                )
            )
            return True


def _default_path() -> Path | None:
    raw = (os.environ.get(FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if FILE_ENV in os.environ:
        return None
    if box_health.DATA_DIR.is_dir():
        return box_health.DATA_DIR / DATA_FILE_NAME
    return None


_store: CleanupStore | None = None
_store_lock = threading.Lock()


def store() -> CleanupStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = CleanupStore(_default_path())
        return _store


def reset_store(replacement: CleanupStore | None = None) -> None:
    global _store
    with _store_lock:
        _store = replacement


# ── the view ────────────────────────────────────────────────────────────────


def offers_of(stored: box_health.Stored | None) -> list[box_health.OfferItem]:
    """What the box offered in its newest slow scan (none when cleanup is off there)."""
    if stored is None or stored.reading.slow is None or stored.reading.slow.cleanup is None:
        return []
    cleanup = stored.reading.slow.cleanup
    return cleanup.actions if cleanup.enabled else []


def find_offer(
    stored: box_health.Stored | None, action: str, owner: str
) -> box_health.OfferItem | None:
    return next((o for o in offers_of(stored) if o.action == action and o.owner == owner), None)


_FAILED_LABEL = re.compile(r"(?:user|container) (?P<label>.+?)(?: scan| volume| cleanup)?")


def labels_allowed(body: box_health.BoxHealthPost, environ: dict[str, str] | None = None) -> bool:
    """Whether every user label in *body* is on ``AE_BOX_USER_LABELS`` (empty: any).

    Labels appear in the user rows, the offers, the aging counts and the failed parts
    (``user <label>``, ``container <label> scan``; a part holds at most 24 label
    characters, so it matches when it begins a listed label)."""
    env = os.environ if environ is None else environ
    allowed = {p.strip() for p in env.get("AE_BOX_USER_LABELS", "").split(",") if p.strip()}
    if not allowed:
        return True
    seen: set[str] = set()
    if body.slow is not None:
        seen |= {u.label for u in body.slow.users if u.label != "System"}
        if body.slow.cleanup is not None:
            seen |= {a.owner for a in body.slow.cleanup.actions if a.owner != "all"}
            seen |= {a.owner for a in body.slow.cleanup.auto_aged}
    if not seen <= allowed:
        return False
    for part in body.failed:
        hit = _FAILED_LABEL.fullmatch(part)
        if hit and not any(label.startswith(hit.group("label")) for label in allowed):
            return False
    return True


def _possessive(label: str, where: str = "box") -> str:
    base = f"{label}'" if label.endswith("s") else f"{label}'s"
    return f"{base} container's" if where == "container" else base


WAIT = "The box runs it within 5 minutes."


def confirm_text(offer: box_health.OfferItem, stale_days: float) -> str:
    """The sentence the confirm step shows: whose items, how many, how big, what is kept,
    and the up-to-five-minute wait."""
    days = f"{stale_days:g}"
    size = box_health.size_words(offer.bytes)
    n = box_health.count_words(offer.count)
    whose = _possessive(offer.owner, offer.where)
    if offer.action == "claude_scratch":
        what = (
            "temp folders (Claude sessions and test runs)"
            if offer.where == "container"
            else "Claude temp folders"
        )
        return (
            f"Remove {n} of {whose} {what} older than {days} days, about {size} "
            f"in {box_health.count_words(offer.files)} items? Folders changed in the last "
            f"{days} days, folders in use and anything not owned by the cleanup user are left "
            f"alone. This cannot be undone. {WAIT}"
        )
    if offer.action == "tmp_litter":
        groups = ", ".join(f"{g.group} {box_health.count_words(g.count)}" for g in offer.groups)
        tail = f": {groups}" if groups else ""
        return (
            f"Remove {n} of {whose} test leftovers older than {days} days from the top of "
            f"/tmp{tail}? Only these name patterns, only files {offer.owner} owns, never git "
            f"checkouts, and a folder with anything changed lately is kept. This cannot be "
            f"undone. {WAIT}"
        )
    if offer.action == "review_checkouts":
        return (
            f"Remove up to {n} of {whose} review checkouts in /tmp older than {days} days? "
            "A checkout with uncommitted changes, a stash or a commit no remote has is kept. "
            f"This cannot be undone. {WAIT}"
        )
    return (
        f"Remove dangling Docker images and build cache older than {days} days, up to about "
        f"{size}? Tagged images, containers and volumes are kept. This cannot be undone. {WAIT}"
    )


def button_text(offer: box_health.OfferItem) -> str:
    n = box_health.count_words(offer.count)
    if offer.action == "claude_scratch":
        return f"Remove {n} folders"
    if offer.action == "tmp_litter":
        return f"Remove {n} items"
    if offer.action == "review_checkouts":
        return f"Remove {n} checkouts"
    return "Prune Docker"


HINTS = {
    "claude_scratch": "Removes this user's Claude temp folders older than the stale age.",
    "tmp_litter": "Removes old test leftovers at the top of /tmp, by fixed name patterns.",
    "review_checkouts": "Removes old review clones in /tmp that have no uncommitted changes.",
    "docker_prune": "Removes dangling Docker images and old build cache. Volumes are kept.",
}


def _clock(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%H:%M")


def message_of(record: Record, state: str, *, show_login: bool) -> str:
    who = record.by if show_login else "a box admin"
    name = NAMES.get(record.action, record.action)
    if state == "queued":
        return (
            f"Queued by {who} at {_clock(record.requested_at)} UTC; the box runs it within "
            "5 minutes"
        )
    if state == "untaken":
        return "The box has not picked this up; its timer may be stopped"
    if state == "expired":
        return "Never picked up by the box"
    if state == "running":
        return f"Running on the box: {name}"
    if state == "lost":
        return "The box did not report back"
    if state == "cancelled":
        return f"Cancelled before the box took it ({name})"
    result = record.result
    if result is None:
        return ""
    at = _clock(result.finished_at or record.requested_at)
    if state == "done":
        freed = box_health.size_words(result.bytes_freed)
        if record.action == "docker_prune":
            return f"Pruned Docker, freed {freed}, at {at} UTC"
        noun = {"claude_scratch": "folders", "tmp_litter": "items"}.get(record.action, "checkouts")
        return (
            f"Removed {box_health.count_words(result.removed)} {noun}, freed {freed}, at {at} UTC"
        )
    words = {
        "DISABLED": "cleanup is switched off on the box",
        "REFUSED": "the box refused to run it",
        "NOT_POSSIBLE": "the box cannot run that one",
        # An older relay still posts CHANGED when its plan outgrew the confirmed count.
        "CHANGED": "the numbers changed since you confirmed; open it and press again",
        "CHECK_FAILED": "a safety check on the box could not finish, so nothing was removed",
    }
    why = "; ".join(words.get(code, f"error {code}") for code in result.errors[:2]) or "no reason"
    return f"The cleanup failed: {why}"


def decorate(
    view: box_health.BoxHealthView,
    stored: box_health.Stored | None,
    record: Record | None,
    now: datetime,
    *,
    show_login: bool,
    cfg: box_health.Config | None = None,
) -> box_health.BoxHealthView:
    """Add the offers, the request's state and the aging line to *view*."""
    cfg = cfg or box_health.config()
    slow = stored.reading.slow if stored else None
    stale_days = slow.stale_days if slow else 2.0
    state = state_of(record, now) if record else None
    active = state in ACTIVE
    fresh = view.state == "ok"
    offers = offers_of(stored)
    actions: list[dict[str, Any]] = []
    for offer in offers:
        can_run, why = True, None
        if active:
            can_run, why = False, "Another cleanup is queued or running."
        elif not fresh:
            can_run, why = False, "The box's reading is too old; the box would not take it."
        actions.append(
            {
                "id": offer.action,
                "owner": offer.owner,
                "where": offer.where,
                "label": button_text(offer),
                "count": offer.count,
                "files": offer.files,
                "bytes": offer.bytes,
                "confirm": confirm_text(offer, stale_days),
                "can_run": can_run,
                "why_not": why,
                "hint": HINTS[offer.action],
            }
        )
    cleanup: dict[str, Any] | None = None
    if record is not None and state is not None:
        retry = state in {"failed", "expired", "lost"}
        cleanup = {
            "state": state,
            "action": record.action,
            "owner": record.owner,
            "label": NAMES.get(record.action, record.action),
            "by": record.by if show_login else "a box admin",
            "requested_at": record.requested_at,
            "taken_at": record.taken_at,
            "finished_at": record.finished_at,
            "result": (
                {
                    "removed": record.result.removed,
                    "bytes_freed": record.result.bytes_freed,
                    "skipped": dict(record.result.skipped),
                    "errors": list(record.result.errors),
                    "partial": record.result.partial,
                }
                if record.result
                else None
            ),
            "message": message_of(record, state, show_login=show_login),
            "can_cancel": state in {"queued", "untaken"},
            "can_retry": retry,
        }
    details = dict(view.details)
    posted = slow.cleanup if slow else None
    details["cleanup"] = {
        "enabled": bool(posted.enabled) if posted else True,
        "auto_aged": [a.model_dump() for a in posted.auto_aged] if posted else [],
    }
    needs = [n.model_copy(update={"action": _need_action(n.key, offers)}) for n in view.needs]
    del cfg
    return view.model_copy(
        update={"actions": actions, "cleanup": cleanup, "details": details, "needs": needs}
    )


def _need_action(key: str, offers: list[box_health.OfferItem]) -> str | None:
    """The cleanup that clears a Needs-you line, only when the box offers one for that very
    owner and kind (box or container): another user's host-side folders have no button."""
    parts = key.split(":")
    if key == "docker":
        return "docker_prune" if any(o.action == "docker_prune" for o in offers) else None
    if len(parts) == 4 and parts[0] == "claude":
        _, owner, _where, kind = parts
        action = "claude_scratch"
    elif len(parts) == 3 and parts[0] == "litter":
        _, owner, kind = parts
        action = "tmp_litter"
    else:
        return None
    wanted = "container" if kind == "container" else "box"
    return (
        action
        if any(o.action == action and o.owner == owner and o.where == wanted for o in offers)
        else None
    )


def audit(by: str, action: str, owner: str, count: int) -> None:
    """The log line for a cleanup request: the box admin, the action, the owner label and
    the count. Never an item name."""
    _logger.warning(
        "box health cleanup requested by %s: %s for %s (%d items)", by, action, owner, count
    )
