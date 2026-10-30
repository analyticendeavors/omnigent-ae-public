"""Each user's manual sidebar order, kept on the server so it follows them across devices.

The owner asked on 2026-10-18 to put sessions in an order of their own choosing and
to see the same order on their laptop and their phone. Upstream keeps the sidebar
view (grouping, ordering, filters) per device in ``localStorage``, which cannot
do that. This module is the server's half: one ordered list of session ids per
user, with a revision number so two devices that edit at once do not silently
overwrite each other (the later write gets a conflict carrying the current
state, and the web client replays its move on top of it).

Session ids are ``uuid4().hex`` (``omnigent/db/utils.py``
``generate_conversation_id``), so an id must be 32 lowercase hex characters.
The list holds at most :data:`MAX_ORDER` ids; the client drops unloaded ids
from the end first.

The file shape is ``{"version": 1, "users": {"<user>": {"order": [...],
"revision": n, "updated_at": "<iso utc>"}}}``. Where it lives:
:data:`STATE_FILE_ENV` when set; else ``/data/ae-sidebar.json`` when ``/data``
is a directory (the box bind-mounts it and backs it up); else nowhere, and the
order lives in memory only, so a dev server or a test never writes to ``/``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)

STATE_FILE_ENV = "AE_SIDEBAR_STATE_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-sidebar.json"
MAX_ORDER = 500
_ID = re.compile(r"[0-9a-f]{32}")
CODE_NOT_SAVED = "sidebar_not_saved"


class SidebarOrder(BaseModel):
    """A user's saved order, newest session ids wherever the client put them."""

    model_config = ConfigDict(extra="forbid")

    order: list[str]
    revision: int


class SidebarOrderPut(BaseModel):
    """The body of ``PUT /sidebar``: the whole new order and the revision it was based on."""

    model_config = ConfigDict(extra="forbid")

    # Bounded so a huge request is refused before it is walked; 501 to 2000 ids still reach
    # normalize_order's own 400.
    order: list[str] = Field(max_length=MAX_ORDER * 4)
    revision: int = Field(ge=0)


def normalize_order(ids: list[str]) -> list[str]:
    """Check every id and keep the first of any duplicates.

    :param ids: Session ids in the order the client wants.
    :returns: The same ids with repeats removed.
    :raises ValueError: When an id is not 32 lowercase hex characters, or more
        than :data:`MAX_ORDER` remain after removing repeats.
    """
    seen: set[str] = set()
    kept: list[str] = []
    for item in ids:
        if not isinstance(item, str) or not _ID.fullmatch(item):
            shown = item[:40] if isinstance(item, str) else repr(item)[:40]
            raise ValueError(
                f"{shown!r} is not a session id (32 lowercase hex characters); "
                "reload the sidebar and retry."
            )
        if item not in seen:
            seen.add(item)
            kept.append(item)
    if len(kept) > MAX_ORDER:
        raise ValueError(
            f"the order holds {len(kept)} sessions but at most {MAX_ORDER} are kept; "
            "drop the oldest ids and retry."
        )
    return kept


class SidebarSaveError(OSError):
    """The order could not be written to the state file; nothing was changed."""


@dataclass(frozen=True)
class PutResult:
    """What :meth:`SidebarOrderStore.put` did: the state now held, and whether it conflicted."""

    state: SidebarOrder
    conflict: bool = False


def _lenient(ids: list[Any]) -> list[str]:
    """Valid, first-seen ids from a stored list, at most :data:`MAX_ORDER`."""
    seen: set[str] = set()
    kept: list[str] = []
    for item in ids:
        if isinstance(item, str) and _ID.fullmatch(item) and item not in seen:
            seen.add(item)
            kept.append(item)
    return kept[:MAX_ORDER]


class SidebarOrderStore:
    """Per-user order, optionally mirrored to a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._users: dict[str, dict[str, Any]] = {}
        self._loaded = False
        # Set when a bad file could not be moved aside: never overwrite it this process.
        self._memory_only = False

    def _quarantine(self, reason: object) -> None:
        """Move an unreadable state file aside so the next save cannot wipe it."""
        assert self._path is not None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        aside = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        try:
            os.replace(self._path, aside)
        except OSError as exc:
            self._memory_only = True
            _logger.warning(
                "ae sidebar: %s is unreadable (%s) and could not be moved aside (%s); "
                "orders stay in memory until restart. Fix or remove the file.",
                self._path,
                reason,
                exc,
            )
            return
        _logger.warning(
            "ae sidebar: %s was unreadable (%s); moved it to %s and started empty.",
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
            self._quarantine("not a sidebar state file")
            return
        for user, entry in users.items():
            if not isinstance(entry, dict):
                continue
            order = entry.get("order")
            revision = entry.get("revision")
            if (
                not isinstance(order, list)
                or not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 0
            ):
                continue
            cleaned = _lenient(order)
            if len(cleaned) != len(order):
                _logger.warning(
                    "ae sidebar: dropped %d invalid or repeated ids from %s's saved order",
                    len(order) - len(cleaned),
                    user,
                )
            self._users[str(user)] = {
                "order": cleaned,
                "revision": revision,
                "updated_at": str(entry.get("updated_at") or ""),
            }

    def _save(self) -> bool:
        if self._path is None or self._memory_only:
            return True
        body = {"version": 1, "users": self._users}
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-sidebar-", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
            os.replace(tmp, self._path)
        except OSError as exc:
            _logger.warning("ae sidebar: could not write %s: %s", self._path, exc)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            return False
        return True

    def get(self, user: str) -> SidebarOrder:
        """The saved order for *user*: empty at revision 0 when none was saved."""
        with self._lock:
            self._load()
            entry = self._users.get(user)
            if entry is None:
                return SidebarOrder(order=[], revision=0)
            return SidebarOrder(order=list(entry["order"]), revision=entry["revision"])

    def put(
        self, user: str, order: list[str], base_revision: int, *, now: datetime | None = None
    ) -> PutResult:
        """Store *order* when *base_revision* is the revision held; else report a conflict.

        :param user: The user key.
        :param order: Already normalized ids (:func:`normalize_order`).
        :param base_revision: The revision the client's order was based on.
        :returns: The new state at ``base_revision + 1``, or the current state
            with ``conflict`` set when the held revision differs.
        """
        with self._lock:
            self._load()
            entry = self._users.get(user)
            held = entry["revision"] if entry is not None else 0
            if held != base_revision:
                current = list(entry["order"]) if entry is not None else []
                return PutResult(SidebarOrder(order=current, revision=held), conflict=True)
            revision = held + 1
            self._users[user] = {
                "order": list(order),
                "revision": revision,
                "updated_at": (now or datetime.now(UTC)).isoformat(),
            }
            if not self._save():
                if entry is None:
                    del self._users[user]
                else:
                    self._users[user] = entry
                raise SidebarSaveError(
                    f"could not write {self._path}; check that the folder is writable and "
                    "has space, then retry."
                )
            return PutResult(SidebarOrder(order=list(order), revision=revision))


def _default_path() -> Path | None:
    raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: SidebarOrderStore | None = None
_store_lock = threading.Lock()


def store() -> SidebarOrderStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SidebarOrderStore(_default_path())
        return _store


def reset_store(replacement: SidebarOrderStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


__all__ = [
    "CODE_NOT_SAVED",
    "DATA_DIR",
    "MAX_ORDER",
    "STATE_FILE_ENV",
    "PutResult",
    "SidebarOrder",
    "SidebarOrderPut",
    "SidebarOrderStore",
    "SidebarSaveError",
    "normalize_order",
    "reset_store",
    "store",
]
