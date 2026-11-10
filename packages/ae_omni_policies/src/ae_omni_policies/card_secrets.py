"""GitHub tokens typed into the Cards panel, encrypted at rest (2026-11-08, card setup PR 3).

The ``shared_accounts.py`` pattern for a second kind of secret: a read-only GitHub token a
company admin saves for one account (``company/git/<owner>``) or one admin saves for their own
use (``user/<login>/git/<github-login>``). Only GitHub tokens are saved here; every other key
stays in the server's environment.

Each token is encrypted with Fernet (AES-128-CBC with an HMAC-SHA256 tag) under
:data:`KEY_ENV`, read from the environment at use time. Several comma-separated keys rotate:
the first encrypts, any of them decrypts, and the first load re-encrypts every record under the
first so the old key can be dropped. Without the key nothing can be saved and nothing decrypts;
:func:`meta` still answers (a record exists) because that is not secret.

A token never leaves this module except through :func:`get_token`, which only the server's own
GitHub reads call. No meta, list, error or log line carries a token or its ciphertext.

The file shape is ``{"version": 1, "records": {"<id>": {"ciphertext", "saved_by", "saved_at",
"github_login", "expires_at"}}}``. It lives at :data:`STATE_FILE_ENV` when that is set (blank
means memory only), else ``/data/ae-card-secrets.json`` when ``/data`` is a directory, else in
memory only, so a dev server or a test never writes to ``/``. Written ``0600`` through a temp
file in the same directory and ``os.replace``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

KEY_ENV = "AE_CARD_SECRETS_KEY"
STATE_FILE_ENV = "AE_CARD_SECRETS_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-card-secrets.json"

CODE_NO_KEY = "card_secrets_key_missing"
CODE_BAD_KEY = "card_secrets_key_invalid"
CODE_NOT_SAVED = "card_secrets_not_saved"
CODE_NOT_FOUND = "card_secrets_not_found"

#: A record id: ``company/git/<owner>`` or ``user/<login>/git/<github-login>`` (GitHub names are
#: lower-cased; a sign-in login is any text without "/" so a prefix names exactly one user).
_RECORD_ID = re.compile(
    r"^(?:company/git/[a-z0-9-]{1,39}|user/[^/\x00-\x1f]{1,200}/git/[a-z0-9-]{1,39})$"
)
_LOGIN = re.compile(r"^[^/\x00-\x1f]{1,200}$")


class CardSecretsError(Exception):
    """A refusal with an error code and an HTTP status for the route to answer with."""

    status_code = 400
    code = "invalid_input"


class KeyMissingError(CardSecretsError):
    status_code = 409
    code = CODE_NO_KEY


class KeyInvalidError(CardSecretsError):
    status_code = 503
    code = CODE_BAD_KEY


class SaveError(CardSecretsError):
    status_code = 503
    code = CODE_NOT_SAVED


def company_id(owner: str) -> str:
    """The record id of the company token for GitHub account *owner*."""
    return f"company/git/{owner.strip().lower()}"


def user_id(login: str, account: str) -> str:
    """The record id of *login*'s own token for GitHub account *account*."""
    return f"user/{login}/git/{account.strip().lower()}"


def user_prefix(login: str) -> str:
    """The prefix every record of *login* starts with (``user/<login>/``)."""
    return f"user/{login}/"


def valid_login(login: str) -> bool:
    """Whether *login* can be part of a record id (no slash, no control character)."""
    return bool(_LOGIN.fullmatch(login))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cipher() -> Any:
    """The Fernet (or MultiFernet) built from :data:`KEY_ENV`.

    :raises KeyMissingError: When the variable is unset or blank.
    :raises KeyInvalidError: When a key is not a Fernet key.
    """
    from cryptography.fernet import Fernet, MultiFernet

    raw = (os.environ.get(KEY_ENV) or "").strip()
    if not raw:
        raise KeyMissingError(
            f"{KEY_ENV} is not set in the server's environment, so GitHub tokens cannot be "
            'saved here. Generate a key with `python -c "from cryptography.fernet import '
            'Fernet; print(Fernet.generate_key().decode())"`, store it in Key Vault as '
            "ae-card-secrets-key, run deploy/pull-secrets.sh and recreate the omnigent "
            "container."
        )
    keys = [part.strip() for part in raw.split(",") if part.strip()]
    try:
        fernets = [Fernet(key.encode("ascii")) for key in keys]
    except (ValueError, UnicodeEncodeError) as exc:
        raise KeyInvalidError(
            f"{KEY_ENV} is not a valid Fernet key (32 url-safe base64 bytes, as "
            "Fernet.generate_key() prints). Replace it and recreate the omnigent container."
        ) from exc
    return fernets[0] if len(fernets) == 1 else MultiFernet(fernets)


def require_key() -> None:
    """Raise unless a usable key is set (a route calls this before it does anything else).

    :raises KeyMissingError: When :data:`KEY_ENV` is unset or blank.
    :raises KeyInvalidError: When it is not a Fernet key.
    """
    _cipher()


def key_configured() -> bool:
    """Whether :data:`KEY_ENV` holds a usable key; never raises."""
    try:
        _cipher()
    except CardSecretsError:
        return False
    return True


def _decrypt(ciphertext: str) -> str | None:
    """The token in *ciphertext*, or ``None`` when there is no key or none opens it."""
    from cryptography.fernet import InvalidToken

    try:
        cipher = _cipher()
        return cipher.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except CardSecretsError:
        return None
    except (InvalidToken, ValueError, UnicodeError):
        return None


def _reseal(ciphertext: str) -> str | None:
    """*ciphertext* under the first key, or ``None`` when no key opens it."""
    from cryptography.fernet import InvalidToken, MultiFernet

    try:
        cipher = _cipher()
    except CardSecretsError:
        return None
    if not isinstance(cipher, MultiFernet):
        return ciphertext
    try:
        return cipher.rotate(ciphertext.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeError):
        return None


def _meta_of(entry: dict[str, Any], *, key_present: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "set": True,
        "saved_by": entry["saved_by"],
        "saved_at": entry["saved_at"],
        "github_login": entry["github_login"],
        "expires_at": entry["expires_at"],
    }
    if key_present and _decrypt(entry["ciphertext"]) is None:
        out["unreadable"] = True
    return out


_NO_META: dict[str, Any] = {
    "set": False,
    "saved_by": None,
    "saved_at": None,
    "github_login": None,
    "expires_at": None,
}


class CardSecretsStore:
    """The saved tokens, mirrored to a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._records: dict[str, dict[str, Any]] = {}
        self._loaded = False
        # Entries this version does not understand (a newer version's, or a hand edit): never
        # served, but written back unchanged so a save cannot lose them.
        self._unknown: dict[str, Any] = {}

    # ── file ─────────────────────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._loaded:
            return
        if self._path is None:
            self._loaded = True
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._loaded = True
            return
        except (OSError, ValueError) as exc:
            # Never overwrite a file we could not read: it holds the only copy of the
            # ciphertexts. Every call fails until it is fixed.
            raise SaveError(
                f"{self._path} could not be read ({type(exc).__name__}); fix or move the file "
                "aside, then retry."
            ) from exc
        records = raw.get("records") if isinstance(raw, dict) else None
        if not isinstance(records, dict):
            raise SaveError(
                f"{self._path} is not a saved tokens file; fix or move it aside, then retry."
            )
        kept: dict[str, dict[str, Any]] = {}
        for record_id, entry in records.items():
            if (
                not isinstance(record_id, str)
                or not _RECORD_ID.fullmatch(record_id)
                or not isinstance(entry, dict)
                or not isinstance(entry.get("ciphertext"), str)
            ):
                _logger.warning("ae card secrets: kept an unknown entry in %s", self._path)
                if isinstance(record_id, str):
                    self._unknown[record_id] = entry
                continue
            expires = entry.get("expires_at")
            kept[record_id] = {
                "ciphertext": entry["ciphertext"],
                "saved_by": str(entry.get("saved_by") or ""),
                "saved_at": str(entry.get("saved_at") or ""),
                "github_login": str(entry.get("github_login") or ""),
                "expires_at": expires if isinstance(expires, str) and expires else None,
            }
        self._records = kept
        self._loaded = True
        self._reseal_all()

    def _reseal_all(self) -> None:
        """With several keys set, re-encrypt every record under the first, so the old can go.

        Runs once as the file loads. A record no key opens stays as it is; a failed write
        leaves the file as it was.
        """
        if not key_configured() or "," not in (os.environ.get(KEY_ENV) or ""):
            return
        changed = False
        resealed: dict[str, dict[str, Any]] = {}
        for record_id, entry in self._records.items():
            sealed = _reseal(entry["ciphertext"])
            if sealed is None:
                resealed[record_id] = entry
                continue
            changed = changed or sealed != entry["ciphertext"]
            resealed[record_id] = {**entry, "ciphertext": sealed}
        if changed:
            try:
                self._save(resealed)
            except SaveError:
                _logger.warning("ae card secrets: could not re-encrypt under the first key")

    def _save(self, records: dict[str, dict[str, Any]]) -> None:
        if self._path is None:
            self._records = records
            return
        body = {"version": 1, "records": {**self._unknown, **records}}
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-card-secrets-", dir=str(self._path.parent))
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
            os.replace(tmp, self._path)
        except OSError as exc:
            _logger.warning("ae card secrets: could not write %s: %s", self._path, exc)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            raise SaveError(
                f"could not write {self._path}; check that the folder is writable and has "
                "space, then retry."
            ) from exc
        self._records = records

    # ── API ──────────────────────────────────────────────────────────────────────────────────

    def put(
        self,
        record_id: str,
        token: str,
        *,
        saved_by: str,
        github_login: str,
        expires_at: str | None,
    ) -> None:
        """Save (or replace) *token* under *record_id*, encrypted before it is held anywhere.

        :raises KeyMissingError: When no key is set.
        :raises KeyInvalidError: When the key is not a Fernet key.
        :raises SaveError: When the file cannot be read or written.
        :raises ValueError: When *record_id* is not a well-formed id.
        """
        if not _RECORD_ID.fullmatch(record_id):
            raise ValueError("not a record id")
        sealed = _cipher().encrypt(token.encode("utf-8")).decode("ascii")
        with self._lock:
            self._load()
            entry = {
                "ciphertext": sealed,
                "saved_by": saved_by,
                "saved_at": _now(),
                "github_login": github_login,
                "expires_at": expires_at,
            }
            self._save({**self._records, record_id: entry})

    def get_token(self, record_id: str) -> str | None:
        """The decrypted token, for the server's own GitHub reads; ``None`` when there is none.

        ``None`` too when no key is set or none opens the record (the record reads as absent).
        """
        with self._lock:
            self._load()
            entry = self._records.get(record_id)
            sealed = entry["ciphertext"] if entry else None
        return _decrypt(sealed) if sealed else None

    def meta(self, record_id: str) -> dict[str, Any]:
        """What is known about *record_id*; never a token or its ciphertext."""
        with self._lock:
            self._load()
            entry = self._records.get(record_id)
            if entry is None:
                return dict(_NO_META)
            return _meta_of(entry, key_present=key_configured())

    def list_meta(self, prefix: str) -> dict[str, dict[str, Any]]:
        """:func:`meta` of every record whose id starts with *prefix*, by id."""
        with self._lock:
            self._load()
            present = key_configured()
            return {
                record_id: _meta_of(entry, key_present=present)
                for record_id, entry in sorted(self._records.items())
                if record_id.startswith(prefix)
            }

    def delete(self, record_id: str) -> bool:
        """Remove *record_id*; whether it existed."""
        with self._lock:
            self._load()
            if record_id not in self._records:
                return False
            self._save({k: v for k, v in self._records.items() if k != record_id})
            return True

    def delete_prefix(self, prefix: str) -> int:
        """Remove every record whose id starts with *prefix*; how many went."""
        with self._lock:
            self._load()
            kept = {k: v for k, v in self._records.items() if not k.startswith(prefix)}
            gone = len(self._records) - len(kept)
            if gone:
                self._save(kept)
            return gone


def _default_path() -> Path | None:
    if STATE_FILE_ENV in os.environ:
        raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
        return Path(raw) if raw else None
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: CardSecretsStore | None = None
_store_lock = threading.Lock()


def store() -> CardSecretsStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = CardSecretsStore(_default_path())
        return _store


def reset_store(replacement: CardSecretsStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


def put(
    record_id: str,
    token: str,
    *,
    saved_by: str,
    github_login: str,
    expires_at: str | None,
) -> None:
    store().put(
        record_id, token, saved_by=saved_by, github_login=github_login, expires_at=expires_at
    )


def get_token(record_id: str) -> str | None:
    return store().get_token(record_id)


def meta(record_id: str) -> dict[str, Any]:
    return store().meta(record_id)


def list_meta(prefix: str) -> dict[str, dict[str, Any]]:
    return store().list_meta(prefix)


def delete(record_id: str) -> bool:
    return store().delete(record_id)


def delete_prefix(prefix: str) -> int:
    return store().delete_prefix(prefix)


__all__ = [
    "CODE_BAD_KEY",
    "CODE_NOT_FOUND",
    "CODE_NOT_SAVED",
    "CODE_NO_KEY",
    "KEY_ENV",
    "STATE_FILE_ENV",
    "CardSecretsError",
    "CardSecretsStore",
    "KeyInvalidError",
    "KeyMissingError",
    "SaveError",
    "company_id",
    "delete",
    "delete_prefix",
    "get_token",
    "key_configured",
    "list_meta",
    "meta",
    "put",
    "require_key",
    "reset_store",
    "store",
    "user_id",
    "user_prefix",
    "valid_login",
]
