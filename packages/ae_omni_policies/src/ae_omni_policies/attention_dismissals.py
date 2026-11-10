"""Per-person dismissals for the Admin Center's Needs you list (2026-11-03, plan section 3.4).

A dismissed item is hidden for the person who dismissed it until one of three things happens:
it gets **worse** (:func:`worse`, one comparator per kind, decided on the stored state and never
on a hash or a time text), it **expires** (3 days for an urgent item, 14 for a review item), or
the person restores it. Nothing here deletes a dismissal because an item looked absent: the
cleanup (:meth:`DismissalStore.tidy`, run every five minutes by ``attention.py``) only starts the
clock for an item missing from a scope that read fresh and clean, and drops the record after
an hour of that, so a problem that was fixed and later recurs shows again.

The file is one JSON document on the data volume (``AE_ATTENTION_DISMISSALS_FILE``, default
``/data/ae-attention-dismissals.json``), written with the same atomic replace as
``cost_targets``. Keyed by sign-in login, then item id. Every write takes one asyncio lock and
re-reads the file inside it, so two tabs or two people dismissing at once both land.

An unreadable file (bad JSON, wrong shape) is **unknown**, never empty: reads report
``ok=False``, writes raise :class:`DismissalsUnreadable`, and the file is left exactly as it is
until it reads again. A broken file must not show every dismissed item as new.

Privacy: a record holds the item id, its state, the tone, the scope, a reason and times. For a
Box health item the state is keys, tones, steps and counts, and the reason comes from
:data:`BOX_REASONS`, so a typed folder or session name can never land in the file
(``tests/test_attention.py`` runs the box privacy test over it). The reason of a Git item is
free text up to :data:`REASON_MAX` characters, shown only to its author.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

FILE_ENV = "AE_ATTENTION_DISMISSALS_FILE"
FILE_NAME = "ae-attention-dismissals.json"
DATA_DIR = Path("/data")

#: Days a dismissal lasts before the item comes back for a look, by the item's tone.
EXPIRY_DAYS = {"urgent": 3, "review": 14}
#: How long an item must be absent from fresh, clean reads before its dismissal is dropped.
ABSENT_HOURS = 1
#: Most dismissals kept per person; when full, expired and absent ones go first.
CAP = 500
REASON_MAX = 200
#: The only reasons a Box health item takes: a typed one could carry a folder or session name.
BOX_REASONS = ("Known", "Fix planned", "Not a problem")
VERSION = 1


class DismissalsUnreadable(Exception):
    """The dismissals file exists but cannot be read; nothing was written."""


class ReasonRefused(ValueError):
    """The reason is not one the item's source takes."""


# ── the "worse" rule, one comparator per kind (plan 3.3) ──────────────────────

_SEVERITY = {"CRITICAL": 0, "HIGH": 1, "MODERATE": 2, "LOW": 3, None: 9}


def _numbers(state: Mapping[str, Any], key: str) -> set[Any]:
    value = state.get(key)
    return set(value) if isinstance(value, list) else set()


def _pr_failing(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    # The item only exists while the PR fails or conflicts, so a new head commit means "a new
    # head that still fails". An UNKNOWN merge state right after a push never makes the item.
    if now.get("sha") != old.get("sha"):
        return "a new commit still fails"
    if now.get("conflicts") and not old.get("conflicts"):
        return "it now has conflicts"
    before, after = old.get("failing"), now.get("failing")
    if isinstance(before, list) and isinstance(after, list):
        extra = set(after) - set(before)
        if extra:
            return f"{len(extra)} more check{'s' if len(extra) != 1 else ''} fail"
    return None


def _new_sha(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    return "a new commit" if now.get("sha") != old.get("sha") else None


def _pr_update(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    if now.get("sha") != old.get("sha"):
        return "a new commit"
    if old.get("merge") == "BEHIND" and now.get("merge") == "BLOCKED":
        return "it is now blocked"
    return None


def _alerts_fix(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    added = _numbers(now, "alerts") - _numbers(old, "alerts")
    if added:
        return f"{len(added)} new alert{'s' if len(added) != 1 else ''}"
    if _SEVERITY.get(now.get("severity"), 9) < _SEVERITY.get(old.get("severity"), 9):
        return "a higher severity"
    return None


def _alerts_stuck(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    added = _numbers(now, "alerts") - _numbers(old, "alerts")
    return f"{len(added)} more alert{'s' if len(added) != 1 else ''} stuck" if added else None


def _sweep(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    # Present means failed or late; a different run that is also bad is a later problem.
    return "a later run is also bad" if now.get("run") != old.get("run") else None


def _error_code(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    return "a different error" if now.get("error") != old.get("error") else None


def _amber_to_red(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    return "it is now red" if old.get("tone") == "amber" and now.get("tone") == "red" else None


def _stepped(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    if _amber_to_red(old, now):
        return "it is now red"
    return "it grew a step" if int(now.get("step") or 0) > int(old.get("step") or 0) else None


def _units(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    added = _numbers(now, "units") - _numbers(old, "units")
    if added:
        return f"{len(added)} more unit{'s' if len(added) != 1 else ''} failed"
    if int(now.get("other") or 0) > int(old.get("other") or 0):
        return "more units failed"
    return None


def _scan(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    # Capped and late are different problems; the same one stays hidden until it expires.
    return "a different scan problem" if now.get("key") != old.get("key") else None


#: Secrets (2026-11-06): a lower rank is worse. A missing, expired, refused or disabled secret is
#: out of service; one expiring soon or kept in step across two vaults is a warning.
_SECRET_RANK = {"missing": 0, "refused": 0, "expired": 0, "disabled": 0, "soon": 1, "apart": 1}


def _secret(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    before = _SECRET_RANK.get(str(old.get("trouble")), 1)
    after = _SECRET_RANK.get(str(now.get("trouble")), 1)
    if after < before:
        return f"it is now {now.get('trouble')}"
    expires = now.get("expires")
    if expires and expires != old.get("expires"):
        return "the expiry date moved"
    return None


def _cost(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    # Only a later period is worse: pausing the newer of two capacities lowers the maximum.
    if str(now.get("period") or "") > str(old.get("period") or ""):
        return "a new period is over too"
    added = _numbers(now, "over") - _numbers(old, "over")
    return f"{len(added)} more over" if added else None


def _credit(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    return "a new credit period is over too" if now.get("end") != old.get("end") else None


def _host(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    return "a newer failure" if int(now.get("since") or 0) > int(old.get("since") or 0) else None


def _account(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    return "the next window filled too" if now.get("resets") != old.get("resets") else None


def _family(old: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    return "over the flag again today" if now.get("day") != old.get("day") else None


#: Kind to comparator. A kind with none (a stale or unreadable box reading) is never dismissible.
COMPARATORS: dict[str, Callable[[Mapping[str, Any], Mapping[str, Any]], str | None]] = {
    "pr-failing": _pr_failing,
    "pr-ready": _new_sha,
    "pr-update": _pr_update,
    "alerts-fix": _alerts_fix,
    "alerts-stuck": _alerts_stuck,
    "branch": _new_sha,
    "sweep": _sweep,
    "account": _error_code,
    "repo-error": _error_code,
    "heat": _amber_to_red,
    "cpu": _amber_to_red,
    "memory": _amber_to_red,
    "container": _amber_to_red,
    "guard": _amber_to_red,
    "mount": _stepped,
    "temp": _stepped,
    "litter": _stepped,
    "docker": _stepped,
    "units": _units,
    "scan": _scan,
    "host-failed": _host,
    "cost-over": _cost,
    "credit-over": _credit,
    "secret-missing": _secret,
    "secret-expired": _secret,
    "secret-refused": _secret,
    "secret-soon": _secret,
    "vault-expired": _secret,
    "vault-soon": _secret,
    "vault-disabled": _secret,
    "vault-apart": _secret,
    "account-full": _account,
    "family-over": _family,
}

_SECRET_BACK = "it expires, goes missing or is refused, or its date moves"

#: The dialog's "Comes back if ..." words, by kind.
BACK_IF = {
    "pr-failing": "a new commit still fails, another check fails, or it starts to conflict",
    "pr-ready": "a new commit is pushed",
    "pr-update": "a new commit is pushed, or it becomes blocked",
    "alerts-fix": "an alert not in this set appears, or one gets more severe",
    "alerts-stuck": "another alert gets stuck",
    "branch": "someone pushes to it",
    "sweep": "a later run also fails or runs late",
    "account": "the error changes",
    "repo-error": "the error changes",
    "heat": "it turns red",
    "cpu": "it turns red",
    "memory": "it turns red",
    "container": "it turns red",
    "guard": "it turns red",
    "mount": "it turns red or fills another 5 points",
    "temp": "it turns red or the pile doubles again",
    "litter": "it turns red or the pile doubles again",
    "docker": "it turns red or the pile doubles again",
    "units": "another unit fails",
    "scan": "the scan problem changes from stopped early to late, or the other way",
    "host-failed": "a newer failure is reported",
    "cost-over": "a new period goes over, or something else goes over",
    "credit-over": "a new credit period is used up too",
    "secret-missing": _SECRET_BACK,
    "secret-expired": _SECRET_BACK,
    "secret-refused": _SECRET_BACK,
    "secret-soon": _SECRET_BACK,
    "vault-expired": _SECRET_BACK,
    "vault-soon": _SECRET_BACK,
    "vault-disabled": _SECRET_BACK,
    "vault-apart": _SECRET_BACK,
    "account-full": "the next weekly window fills too",
    "family-over": "it is over the flag again on another day",
}


def dismissible(kind: str) -> bool:
    return kind in COMPARATORS


def worse(kind: str, dismissed: Mapping[str, Any], now: Mapping[str, Any]) -> str | None:
    """Why *now* is worse than the *dismissed* state of the same item, or ``None``.

    Better never resurfaces: fixing two of five alerts keeps the other three hidden. The states
    hold no timestamp, "ago" text or ticking counter, so time text never matters.
    """
    compare = COMPARATORS.get(kind)
    return compare(dismissed, now) if compare else None


# ── records ───────────────────────────────────────────────────────────────────


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def expiry_of(tone: str, at: datetime) -> datetime:
    return at + timedelta(days=EXPIRY_DAYS.get(tone, EXPIRY_DAYS["review"]))


def judge(
    record: Mapping[str, Any], kind: str, state: Mapping[str, Any], now: datetime
) -> tuple[bool, str | None]:
    """(still hidden, why it came back) for a dismissed item seen with *state* at *now*."""
    expires = parse(record.get("expires_at"))
    if expires is not None and now >= expires:
        at = parse(record.get("at"))
        days = (expires - at).days if at is not None else EXPIRY_DAYS["review"]
        return False, f"{days} days passed"
    why = worse(kind, record.get("state") or {}, state)
    return (why is None), why


def clean_reason(source: str, reason: str | None) -> str:
    """*reason* as it may be stored, or :class:`ReasonRefused`."""
    text = " ".join((reason or "").split())
    if source == "box":
        if text and text not in BOX_REASONS:
            raise ReasonRefused(
                "A Box health reason must be one of: " + ", ".join(BOX_REASONS) + "."
            )
        return text
    if len(text) > REASON_MAX:
        raise ReasonRefused(f"The reason is {len(text)} characters; the limit is {REASON_MAX}.")
    return text


@dataclass
class Snapshot:
    """What the file held when it was read."""

    ok: bool
    people: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)

    def mine(self, login: str) -> dict[str, dict[str, Any]]:
        return self.people.get(login, {})


def _atomic_write(path: Path, text: str) -> None:
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ae-attention-", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise


def _path_from_env() -> Path | None:
    raw = os.environ.get(FILE_ENV)
    if raw is not None:
        return Path(raw.strip()) if raw.strip() else None
    return DATA_DIR / FILE_NAME if DATA_DIR.is_dir() else None


class DismissalStore:
    """The dismissals file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._memory: dict[str, dict[str, dict[str, Any]]] = {}
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _guard(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    def _read(self) -> Snapshot:
        if self._path is None:
            return Snapshot(True, json.loads(json.dumps(self._memory)))
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return Snapshot(True)
        except OSError:
            return Snapshot(False)
        if not text.strip():
            return Snapshot(True)
        try:
            body = json.loads(text)
            people = body["people"]
            if not isinstance(people, dict) or not all(
                isinstance(rows, dict) and all(isinstance(r, dict) for r in rows.values())
                for rows in people.values()
            ):
                return Snapshot(False)
        except (ValueError, KeyError, TypeError):
            return Snapshot(False)
        return Snapshot(True, people)

    def _write(self, people: dict[str, dict[str, dict[str, Any]]]) -> None:
        if self._path is None:
            self._memory = people
            return
        _atomic_write(
            self._path, json.dumps({"version": VERSION, "people": people}, separators=(",", ":"))
        )

    async def snapshot(self) -> Snapshot:
        """The file as it is now (no lock: a write is one atomic replace)."""
        return await asyncio.to_thread(self._read)

    async def _edit(self, change: Callable[[dict[str, dict[str, dict[str, Any]]]], Any]) -> Any:
        async with self._guard():
            current = await asyncio.to_thread(self._read)
            if not current.ok:
                raise DismissalsUnreadable(
                    "The dismissals file cannot be read, so nothing was saved. Fix or remove "
                    f"the file named by {FILE_ENV} on the server, then try again."
                )
            result = change(current.people)
            await asyncio.to_thread(self._write, current.people)
            return result

    async def dismiss(
        self,
        login: str,
        item_id: str,
        *,
        source: str,
        scope: str,
        tone: str,
        state: Mapping[str, Any],
        reason: str,
        now: datetime,
    ) -> dict[str, Any]:
        """Hide *item_id* for *login*; replaces an older record of the same item."""
        record = {
            "source": source,
            "scope": scope,
            "tone": tone,
            "state": dict(state),
            "reason": reason,
            "at": iso(now),
            "expires_at": iso(expiry_of(tone, now)),
            "absent_at": None,
        }

        def change(people: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
            rows = people.setdefault(login, {})
            rows[item_id] = record
            _evict(rows, now, keep=item_id)
            return record

        return await self._edit(change)

    async def restore(self, login: str, item_id: str) -> bool:
        """Show *item_id* again for *login*; ``False`` when it was not dismissed."""

        def change(people: dict[str, dict[str, dict[str, Any]]]) -> bool:
            rows = people.get(login, {})
            found = rows.pop(item_id, None) is not None
            if not rows:
                people.pop(login, None)
            return found

        return await self._edit(change)

    async def tidy(
        self, now: datetime, present: set[str], clean_scopes: set[str]
    ) -> dict[str, int]:
        """Run the absent-item clock over every person's records.

        :param present: Ids of every item any source lists now.
        :param clean_scopes: Scopes that read fresh, clean and uncapped just now. Only an item
            missing from one of those counts as absent; a record whose scope did not read
            cleanly has its clock reset, because nothing in that scope reads as absent.
        :returns: Counts: ``started``, ``cleared``, ``reset``, ``dropped``.
        """
        counts = {"started": 0, "cleared": 0, "reset": 0, "dropped": 0}

        def change(people: dict[str, dict[str, dict[str, Any]]]) -> None:
            for login in list(people):
                rows = people[login]
                for item_id in list(rows):
                    record = rows[item_id]
                    absent = parse(record.get("absent_at"))
                    if item_id in present:
                        if record.get("absent_at") is not None:
                            record["absent_at"] = None
                            counts["cleared"] += 1
                    elif record.get("scope") in clean_scopes:
                        if absent is None:
                            record["absent_at"] = iso(now)
                            counts["started"] += 1
                        elif now - absent >= timedelta(hours=ABSENT_HOURS):
                            del rows[item_id]
                            counts["dropped"] += 1
                    elif record.get("absent_at") is not None:
                        record["absent_at"] = None
                        counts["reset"] += 1
                if not rows:
                    del people[login]

        await self._edit(change)
        return counts


def _evict(rows: dict[str, dict[str, Any]], now: datetime, *, keep: str) -> None:
    """Hold *rows* to :data:`CAP`: expired and absent records first, then the oldest."""
    if len(rows) <= CAP:
        return

    def rank(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
        record = item[1]
        expires = parse(record.get("expires_at"))
        spent = (expires is not None and now >= expires) or record.get("absent_at") is not None
        return (0 if spent else 1, str(record.get("at") or ""))

    for item_id, _ in sorted((i for i in rows.items() if i[0] != keep), key=rank):
        if len(rows) <= CAP:
            break
        del rows[item_id]


_store: DismissalStore | None = None


def store() -> DismissalStore:
    """The process-wide store, built from the environment on first use."""
    global _store
    if _store is None:
        _store = DismissalStore(_path_from_env())
    return _store


def reset_store(replacement: DismissalStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment)."""
    global _store
    _store = replacement
