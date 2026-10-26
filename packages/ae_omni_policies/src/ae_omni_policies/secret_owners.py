"""Who owns each Key Vault secret, set from the Admin Center's Key Vault card (2026-10-23).

Why: secrets will be split between Reid and Steve (and others later), and the
expiry alerts (``secret_alerts.py``) should say whose secret it is. Reid asked to
see the owner at a glance on the card and to set it there for secrets that have
none yet.

The owner is an OmniGent user id (the sign-in email), picked from the server's
own member list (``GET /v1/ae/vault/people`` asks ``/auth/users`` as the caller).
The map lives in a state file under ``/data``, never in Key Vault: the server
never writes the vault, and a tag copy beside this file would drift. The trade,
accepted on the plan page: the owner does not show in the Azure portal.

A secret with no owner is counted on the card, so the gaps close over time; its
alerts go to the task app the server files into, which today is Reid's.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import threading
from pathlib import Path

from pydantic import BaseModel, Field

STATE_FILE_ENV = "AE_SECRET_OWNERS_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-secret-owners.json"

MAX_NAMES = 500
OWNER_PATTERN = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}$")
"""A sign-in id: one ``@``, no spaces. Anything else is refused, not stored."""


class OwnerMap(BaseModel):
    owners: dict[str, str] = Field(default_factory=dict)
    """``<vault>/<secret name>`` to the owner's user id."""


def key_of(vault: str, name: str) -> str:
    return f"{vault}/{name}"


def display_name(user_id: str) -> str:
    """``reid.havens@example.com`` reads as "Reid Havens"; the id itself when it has no words."""
    local = user_id.split("@", 1)[0]
    words = [word for word in re.split(r"[._-]+", local) if word]
    return " ".join(word[:1].upper() + word[1:] for word in words) or user_id


class OwnerSaveError(OSError):
    """The owner file could not be written."""


class SecretOwnersStore:
    """The owner map, kept in a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self._memory = OwnerMap()

    @property
    def writable(self) -> bool:
        """Whether a change survives a restart (a file path is set)."""
        return self._path is not None

    def read(self) -> dict[str, str]:
        """Every owner by ``<vault>/<name>``; empty when there is no file or it cannot be read."""
        with self._lock:
            if self._path is None:
                return dict(self._memory.owners)
            try:
                text = self._path.read_text(encoding="utf-8")
            except OSError:
                return {}
            try:
                return dict(OwnerMap.model_validate_json(text).owners)
            except ValueError:
                return {}

    def _read_for_write(self) -> dict[str, str]:
        """The map as :meth:`read` gives it, except that a file that exists but cannot
        be read is an error (review of PR 304: one bad read plus a save wiped it)."""
        if self._path is None:
            return dict(self._memory.owners)
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise OwnerSaveError(f"could not read {self._path}: {exc}") from exc
        try:
            return dict(OwnerMap.model_validate_json(text).owners)
        except ValueError as exc:
            raise OwnerSaveError(
                f"{self._path} is not a valid owner file; fix or remove it"
            ) from exc

    def set(self, vault: str, names: list[str], owner: str | None) -> dict[str, str]:
        """Give every name in *names* (one vault) to *owner*, or clear it with ``None``.

        :returns: The whole map afterwards.
        :raises OwnerSaveError: When the file cannot be read back or written; an
            unreadable file is never overwritten with only the new entries.
        """
        with self._lock:
            owners = self._read_for_write()
            for name in names:
                if owner is None:
                    owners.pop(key_of(vault, name), None)
                else:
                    owners[key_of(vault, name)] = owner
            state = OwnerMap(owners=owners)
            if self._path is None:
                self._memory = state
                return dict(owners)
            tmp: str | None = None
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".ae-secret-owners-", dir=str(self._path.parent))
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(state.model_dump_json())
                os.replace(tmp, self._path)
            except OSError as exc:
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                raise OwnerSaveError(str(exc)) from exc
            return dict(owners)


def _default_path() -> Path | None:
    raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if STATE_FILE_ENV in os.environ:
        return None
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: SecretOwnersStore | None = None
_store_lock = threading.Lock()


def store() -> SecretOwnersStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SecretOwnersStore(_default_path())
        return _store


def reset_store(replacement: SecretOwnersStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


class OwnerPost(BaseModel):
    """``POST /v1/ae/vault/owner``: one vault, one or more secrets, one owner or none."""

    vault: str = Field(pattern=r"^[0-9A-Za-z-]{3,24}$")
    names: list[str] = Field(min_length=1, max_length=MAX_NAMES)
    owner: str | None = Field(default=None, max_length=255)


__all__ = [
    "MAX_NAMES",
    "OWNER_PATTERN",
    "STATE_FILE_ENV",
    "OwnerMap",
    "OwnerPost",
    "OwnerSaveError",
    "SecretOwnersStore",
    "display_name",
    "key_of",
    "reset_store",
    "store",
]
