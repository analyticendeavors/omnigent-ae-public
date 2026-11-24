"""First seen, per-item events and the Done log for the Admin Center (2026-11-16, D2 phase 2).

Three things live in one JSON file on the data volume (``AE_ATTENTION_LOG_FILE``, default
``/data/ae-attention-log.json``), written with the same atomic replace as the dismissal file:

* ``items``: per item key, ``first_seen`` (the first time any read listed it), ``last_seen`` and a
  list of events ``{at, what, by}``. ``what`` is one of :data:`WHAT`; ``by`` is the sign-in login
  for something a person did and ``None`` otherwise. The page's Age column and the opened row's
  history box read these.
* ``done``: items that left the list because they were fixed, each ``{id, source, kind, title,
  done_at, first_seen}``. "Done" means *no longer reported*, nothing more: the log cannot tell a
  fix from a card that went quiet. :meth:`LogStore.track` holds the rules; ``attention.py``
  runs it from the five-minute cleanup.
* ``tracked``: the items the previous clean pass listed, with how many clean passes in a row
  have missed them. It is what makes the one-pass grace survive a restart.

Every write takes one asyncio lock and re-reads the file inside it. An unreadable file (bad
JSON, wrong shape) is **unknown**, never empty: reads report ``ok=False``, writes raise
:class:`LogUnreadable` and the file is left as it is until it reads again.

Kept 30 days and at most 2,000 events (oldest dropped first); the Done list is held to the same
two limits.

Privacy, the same rule as the dismissal file: no free text from outside except an item's title
through ``attention.clean_text``, and a Box health or Box host item has **no title at all**: the
caller stores the source's name and the kind's label instead. A Git item of one person's own
repos is keyed by that person's login (:func:`key_for`), so another admin never reads its events.
``tests/test_attention.py`` runs the box privacy test over this file too.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

FILE_ENV = "AE_ATTENTION_LOG_FILE"
FILE_NAME = "ae-attention-log.json"
DATA_DIR = Path("/data")
VERSION = 1

#: Days an event or a Done entry is kept.
KEEP_DAYS = 30
#: Most events kept in all; the oldest go first.
MAX_EVENTS = 2000
#: Most Done entries kept, with the same 30 days.
MAX_DONE = 2000
#: A ``last_seen`` is rewritten only when it is older than this, so a page read is rarely a write.
SEEN_REFRESH_S = 12 * 3600

WHAT = (
    "first-seen",
    "dismissed",
    "snoozed",
    "woke",
    "restored",
    "chat-started",
    "done",
    "back",
    "assigned",
    "unassigned",
)

#: The ``by`` of an event the server wrote itself (an assignment cleared because its item left).
SYSTEM = "system"


class LogUnreadable(Exception):
    """The log file exists but cannot be read; nothing was written."""


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def key_for(item_id: str, login: str) -> str:
    """The log key of an item: its id, except a personal Git item, which is per login."""
    return f"{login}|{item_id}" if item_id.startswith("git-mine:") else item_id


@dataclass
class Snapshot:
    """What the file held when it was read."""

    ok: bool
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    done: list[dict[str, Any]] = field(default_factory=list)
    tracked: dict[str, dict[str, Any]] = field(default_factory=dict)

    def first_seen(self, key: str) -> str | None:
        row = self.items.get(key)
        return row.get("first_seen") if row else None

    def assigned(self, key: str) -> dict[str, Any] | None:
        """The item's ``{to, by, at}`` assignment, or ``None``."""
        row = self.items.get(key)
        value = row.get("assigned") if row else None
        return dict(value) if isinstance(value, dict) else None

    def events(self, key: str) -> list[dict[str, Any]]:
        """The item's events, oldest first, led by its first sight (made up from `first_seen`)."""
        row = self.items.get(key)
        if not row:
            return []
        first = row.get("first_seen")
        lead = [_event(parse(first), "first-seen", None)] if parse(first) else []
        return lead + list(row.get("events") or [])


def _valid(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    items, done, tracked = body.get("items"), body.get("done"), body.get("tracked")
    return (
        isinstance(items, dict)
        and all(isinstance(r, dict) for r in items.values())
        and isinstance(done, list)
        and all(isinstance(r, dict) for r in done)
        and isinstance(tracked, dict)
        and all(isinstance(r, dict) for r in tracked.values())
    )


def _atomic_write(path: Path, text: str) -> None:
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ae-attention-log-", dir=str(path.parent))
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


def _event(at: datetime, what: str, by: str | None, to: str | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {"at": iso(at), "what": what, "by": by}
    if to is not None:
        event["to"] = to  # who an `assigned` event gave the item to (2026-11-16, D2 phase 3)
    return event


def _clear_assigned(items: dict[str, dict[str, Any]], key: str, now: datetime) -> None:
    """Drop an item's assignment because the item left, and log it as the system's doing."""
    row = items.get(key)
    if row is not None and isinstance(row.get("assigned"), dict):
        del row["assigned"]
        row["events"].append(_event(now, "unassigned", SYSTEM))


def _row(items: dict[str, dict[str, Any]], key: str, now: datetime) -> dict[str, Any]:
    row = items.get(key)
    if row is None:
        # First sight is the row's own `first_seen`, not an event: a burst of new items must never
        # push people's actions (dismissed, snoozed, chat started) out of the 2,000-event cap.
        row = items[key] = {"first_seen": iso(now), "last_seen": iso(now), "events": []}
    return row


def prune(body: dict[str, Any], now: datetime) -> None:
    """Hold the file to 30 days and 2,000 events, oldest first.

    An item record stays while it is tracked, was seen in the last 30 days or has a recent event,
    so an item open for 40 days keeps its first-seen date.
    """
    cutoff = now - timedelta(days=KEEP_DAYS)
    tracked = body["tracked"]
    for key in list(body["items"]):
        row = body["items"][key]
        row["events"] = [
            e for e in row.get("events") or [] if (parse(e.get("at")) or now) >= cutoff
        ]
        seen = parse(row.get("last_seen")) or parse(row.get("first_seen")) or now
        if key not in tracked and seen < cutoff and not row["events"] and not row.get("assigned"):
            del body["items"][key]
    flat = sorted(
        (
            (str(e.get("at") or ""), key, i)
            for key, row in body["items"].items()
            for i, e in enumerate(row["events"])
        ),
    )
    excess = len(flat) - MAX_EVENTS
    if excess > 0:
        drop: dict[str, set[int]] = {}
        for _at, key, i in flat[:excess]:
            drop.setdefault(key, set()).add(i)
        for key, indexes in drop.items():
            row = body["items"][key]
            row["events"] = [e for i, e in enumerate(row["events"]) if i not in indexes]
    body["done"] = sorted(
        (d for d in body["done"] if (parse(d.get("done_at")) or now) >= cutoff),
        key=lambda d: str(d.get("done_at") or ""),
    )[-MAX_DONE:]


class LogStore:
    """The log file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._memory: dict[str, Any] = {"items": {}, "done": [], "tracked": {}}
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _guard(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    def _read(self) -> tuple[bool, dict[str, Any]]:
        if self._path is None:
            return True, json.loads(json.dumps(self._memory))
        blank: dict[str, Any] = {"items": {}, "done": [], "tracked": {}}
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return True, blank
        except OSError:
            return False, blank
        if not text.strip():
            return True, blank
        try:
            body = json.loads(text)
        except ValueError:
            return False, blank
        if not _valid(body):
            return False, blank
        return True, body

    def _write(self, body: dict[str, Any]) -> None:
        if self._path is None:
            self._memory = body
            return
        _atomic_write(
            self._path,
            json.dumps({"version": VERSION, **body}, separators=(",", ":")),
        )

    async def snapshot(self) -> Snapshot:
        """The file as it is now (no lock: a write is one atomic replace)."""
        ok, body = await asyncio.to_thread(self._read)
        return Snapshot(ok, body["items"], body["done"], body["tracked"])

    async def _edit(self, change: Callable[[dict[str, Any]], Any], now: datetime) -> Any:
        async with self._guard():
            ok, body = await asyncio.to_thread(self._read)
            if not ok:
                raise LogUnreadable(
                    "The Needs you activity log cannot be read, so nothing was saved. Fix or "
                    f"remove the file named by {FILE_ENV} on the server, then try again."
                )
            result = change(body)
            prune(body, now)
            await asyncio.to_thread(self._write, body)
            return result

    async def note_seen(self, keys: Iterable[str], now: datetime) -> int:
        """Record first sight of each key; rewrite ``last_seen`` only when it is stale.

        Writes nothing when every key is known and fresh. Returns how many keys were new.
        """
        keys = list(dict.fromkeys(keys))
        if not keys:
            return 0
        snap = await self.snapshot()
        if not snap.ok:
            raise LogUnreadable("The Needs you activity log cannot be read.")
        stale = timedelta(seconds=SEEN_REFRESH_S)
        if all(
            k in snap.items and now - (parse(snap.items[k].get("last_seen")) or now) < stale
            for k in keys
        ):
            return 0

        def change(body: dict[str, Any]) -> int:
            new = 0
            for key in keys:
                known = key in body["items"]
                row = _row(body["items"], key, now)
                row["last_seen"] = iso(now)
                new += 0 if known else 1
            return new

        return await self._edit(change, now)

    async def add_events(
        self, entries: Iterable[tuple[str, str, str | None]], now: datetime
    ) -> None:
        """Append ``(key, what, by)`` events; a key never seen before gets its first-seen too."""
        rows = [e for e in entries if e[1] in WHAT]
        if not rows:
            return

        def change(body: dict[str, Any]) -> None:
            for key, what, by in rows:
                row = _row(body["items"], key, now)
                row["last_seen"] = iso(now)
                row["events"].append(_event(now, what, by))

        await self._edit(change, now)

    async def set_assigned(
        self,
        key: str,
        to: str | None,
        by: str,
        now: datetime,
        info: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Assign the item to *to* (``None`` unassigns) and log it; returns the new value.

        One locked write with the file's usual rules: an unreadable file raises
        :class:`LogUnreadable` and nothing changes. *info* (``source``, ``kind``, ``title``,
        ``scope``) puts an item the cleanup has not seen yet into ``tracked``, so the assignment
        is cleared when the item leaves even if the first cleanup pass never listed it.
        """

        def change(body: dict[str, Any]) -> dict[str, Any] | None:
            row = _row(body["items"], key, now)
            row["last_seen"] = iso(now)
            if to is None:
                if isinstance(row.get("assigned"), dict):
                    del row["assigned"]
                    row["events"].append(_event(now, "unassigned", by))
                return None
            value = {"to": to, "by": by, "at": iso(now)}
            row["assigned"] = value
            row["events"].append(_event(now, "assigned", by, to))
            if info is not None and key not in body["tracked"]:
                body["tracked"][key] = {**dict(info), "misses": 0}
            return dict(value)

        return await self._edit(change, now)

    async def track(
        self,
        now: datetime,
        present: Mapping[str, Mapping[str, Any]],
        clean_scopes: set[str],
        *,
        blocked: Callable[[str], bool],
        off_sources: set[str],
    ) -> dict[str, int]:
        """One pass of the Done rules over the tracked items.

        :param present: Item id to ``{source, kind, title, scope}`` for every item any source
            lists now (personal Git items are left out by the caller).
        :param clean_scopes: Scopes that read fresh, clean and uncapped just now.
        :param blocked: ``blocked(id)`` is true when anyone dismissed or snoozed the item, which
            leaves it silently.
        :param off_sources: Sources whose card is off; their items leave silently.
        :returns: Counts: ``tracked``, ``done``, ``silent``.

        An item is logged only when it was listed on the previous clean pass and is absent on two
        clean passes in a row. A pass whose scope did not read cleanly changes nothing about that
        item except resetting its miss count, so "two in a row" never spans a doubtful read.
        """
        counts = {"tracked": 0, "done": 0, "silent": 0}

        def change(body: dict[str, Any]) -> None:
            tracked = body["tracked"]
            for item_id in list(tracked):
                row = tracked[item_id]
                if item_id in present:
                    row["misses"] = 0
                    continue
                if row.get("source") in off_sources:
                    if (body["items"].get(item_id) or {}).get("assigned"):
                        # A card turned off and on again must not lose the assignment: the
                        # row waits (miss count unchanged); tracking resumes when it returns.
                        continue
                    del tracked[item_id]
                    counts["silent"] += 1
                    continue
                if row.get("scope") not in clean_scopes:
                    row["misses"] = 0
                    continue
                row["misses"] = int(row.get("misses") or 0) + 1
                if row["misses"] < 2:
                    continue
                del tracked[item_id]
                # Gone on two clean passes, Done or silent: the assignment ends with the item.
                _clear_assigned(body["items"], item_id, now)
                if blocked(item_id):
                    counts["silent"] += 1
                    continue
                first = (body["items"].get(item_id) or {}).get("first_seen")
                body["done"].append(
                    {
                        "id": item_id,
                        "source": row.get("source"),
                        "kind": row.get("kind"),
                        "title": row.get("title") or "",
                        "done_at": iso(now),
                        "first_seen": first,
                    }
                )
                irow = _row(body["items"], item_id, now)
                irow["events"].append(_event(now, "done", None))
                counts["done"] += 1
            for item_id, info in present.items():
                if info.get("scope") not in clean_scopes:
                    continue
                if item_id not in tracked:
                    tracked[item_id] = {**info, "misses": 0}
                    counts["tracked"] += 1
                    events = (body["items"].get(item_id) or {}).get("events") or []
                    if events and events[-1].get("what") == "done":
                        # Listed again after leaving: the history says so (2026-11-23).
                        events.append(_event(now, "back", None))
                else:
                    tracked[item_id].update(
                        {k: v for k, v in info.items() if k in ("title", "scope", "kind")}
                    )

        await self._edit(change, now)
        return counts


_store: LogStore | None = None


def store() -> LogStore:
    """The process-wide store, built from the environment on first use."""
    global _store
    if _store is None:
        _store = LogStore(_path_from_env())
    return _store


def reset_store(replacement: LogStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment)."""
    global _store
    _store = replacement
