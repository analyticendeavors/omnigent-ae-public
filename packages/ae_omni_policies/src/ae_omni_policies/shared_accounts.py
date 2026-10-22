"""Shared Claude accounts: company-paid subscription logins the admin keeps on the server.

Added 2026-10-20, so a second AE user's host can use company-paid Claude
accounts that the admin manages, without copying tokens between hosts.
Until then a named Claude account lived on one host only: the host keeps the
names in ``~/.omnigent/claude-accounts.json`` and each token as its own secret
(``omnigent/onboarding/claude_accounts.py``). A shared account is kept here
instead, on the server, and every user's account picker lists it (marked
Shared) beside that host's own accounts.

What is kept, per account: ``name`` (the id, same alphabet as a host account,
so ``shared:<name>`` can never collide with a host name, which has no colon),
``label`` (what the picker shows; the admin can change it), the token
encrypted, and who added and last changed it, when.

The token is a long-lived ``claude setup-token`` OAuth token
(``sk-ant-oat...``). It does not refresh, so several hosts can run on it at
once. It is encrypted at rest with Fernet (AES-128-CBC with an HMAC-SHA256
tag, from ``cryptography``) under :data:`KEY_ENV`, read from the server's
environment at use time; on the box that is ``/secure/env/omnigent-ae.env``,
written by ``deploy/pull-secrets.sh`` from Key Vault
``omnigent-ae-shared-accounts-key``. Several comma-separated keys rotate: the
first encrypts, any of them decrypts, and the file's first load re-encrypts
every token under the first so the old key can be dropped. Without the key the names and labels
still list (they are not secret) and nothing can be added or fetched; the
error says which variable to set.

Tokens leave the server only on ``GET /v1/ae/claude-accounts/shared/{name}/token``,
which refuses a browser (``shared_accounts_routes.py`` says how), and are
never in a list answer or a log line.

The file shape is ``{"version": 1, "accounts": [{"name", "label", "token",
"created_by", "created_at", "updated_by", "updated_at"}]}`` with ``token`` the
Fernet ciphertext. Where it lives: :data:`STATE_FILE_ENV` when set; else
``/data/ae-shared-claude-accounts.json`` when ``/data`` is a directory (the
box bind-mounts ``/secure/omnigent/data`` there, the encrypted volume, and
backs it up); else nowhere, memory only, so a dev server or a test never
writes to ``/``. Written ``0600`` through a temp file and a rename.
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

KEY_ENV = "AE_SHARED_ACCOUNTS_KEY"
STATE_FILE_ENV = "AE_SHARED_ACCOUNTS_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-shared-claude-accounts.json"
# What a host-side account id starts with; host names cannot hold a colon.
PREFIX = "shared:"
MAX_ACCOUNTS = 50
TOKEN_PREFIX = "sk-ant-oat"
# The host's own rule for an account name (claude_accounts._valid_name).
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,63}")

CODE_NO_KEY = "shared_accounts_key_missing"
CODE_BAD_KEY = "shared_accounts_key_invalid"
CODE_UNREADABLE = "shared_account_unreadable"
CODE_NOT_SAVED = "shared_accounts_not_saved"
CODE_EXISTS = "shared_account_exists"
CODE_NOT_FOUND = "shared_account_not_found"
CODE_FULL = "shared_accounts_full"


class SharedAccountsError(Exception):
    """A refusal with an error code and an HTTP status for the route to answer with."""

    status_code = 400
    code = "invalid_input"


class KeyMissingError(SharedAccountsError):
    status_code = 503
    code = CODE_NO_KEY


class KeyInvalidError(SharedAccountsError):
    status_code = 503
    code = CODE_BAD_KEY


class UnreadableError(SharedAccountsError):
    status_code = 503
    code = CODE_UNREADABLE


class SaveError(SharedAccountsError):
    status_code = 503
    code = CODE_NOT_SAVED


class ExistsError(SharedAccountsError):
    status_code = 409
    code = CODE_EXISTS


class NotFoundError(SharedAccountsError):
    status_code = 404
    code = CODE_NOT_FOUND


class FullError(SharedAccountsError):
    status_code = 409
    code = CODE_FULL


def account_id(name: str) -> str:
    """The id a host and the picker use for shared account *name*: ``shared:<name>``."""
    return f"{PREFIX}{name}"


def valid_name(name: str) -> bool:
    """Whether *name* is a usable account name (the host's own rule)."""
    return bool(_NAME.fullmatch(name))


def check_name(name: str) -> str:
    name = name.strip()
    if not valid_name(name):
        raise SharedAccountsError(
            "Use a name of up to 64 letters, digits, spaces, - or _, starting with a "
            "letter or digit."
        )
    return name


def check_label(label: str) -> str:
    label = label.strip()
    if not valid_name(label):
        raise SharedAccountsError(
            "Use a label of up to 64 letters, digits, spaces, - or _, starting with a "
            "letter or digit."
        )
    return label


def check_token(token: str) -> str:
    token = token.strip()
    if not token.startswith(TOKEN_PREFIX) or any(ch.isspace() for ch in token):
        raise SharedAccountsError(
            "Paste the OAuth token that `claude setup-token` prints (it starts with "
            f"{TOKEN_PREFIX}), not a password or an API key."
        )
    return token


def _cipher() -> Any:
    """The Fernet (or MultiFernet, for several keys) built from :data:`KEY_ENV`.

    :raises KeyMissingError: When the variable is unset or blank.
    :raises KeyInvalidError: When a key is not a Fernet key.
    """
    from cryptography.fernet import Fernet, MultiFernet

    raw = (os.environ.get(KEY_ENV) or "").strip()
    if not raw:
        raise KeyMissingError(
            f"{KEY_ENV} is not set on the server, so shared Claude accounts cannot be "
            'added or used. Generate a key with `python -c "from cryptography.fernet '
            'import Fernet; print(Fernet.generate_key().decode())"`, store it in Key Vault '
            "as omnigent-ae-shared-accounts-key, run deploy/pull-secrets.sh (it writes "
            "/secure/env/omnigent-ae.env) and recreate the omnigent container."
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


def key_configured() -> bool:
    """Whether :data:`KEY_ENV` holds a usable key; never raises."""
    try:
        _cipher()
    except SharedAccountsError:
        return False
    return True


def encrypt(token: str) -> str:
    """*token* as Fernet ciphertext under the first configured key."""
    return _cipher().encrypt(token.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """The token in *ciphertext*.

    :raises UnreadableError: When no configured key opens it (the key changed).
    """
    from cryptography.fernet import InvalidToken

    cipher = _cipher()
    try:
        return cipher.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise UnreadableError(
            f"The stored token cannot be decrypted with the {KEY_ENV} now set; the key "
            "changed. Put the old key back (comma-separated after the new one rotates it), "
            "or remove the account and add it again."
        ) from exc


def reseal(ciphertext: str) -> str:
    """*ciphertext* re-encrypted under the first configured key (itself when it already is).

    :raises KeyMissingError: When no key is set.
    :raises UnreadableError: When no configured key opens it.
    """
    from cryptography.fernet import InvalidToken, MultiFernet

    cipher = _cipher()
    if not isinstance(cipher, MultiFernet):
        return ciphertext
    try:
        return cipher.rotate(ciphertext.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise UnreadableError("A stored token cannot be opened with the keys now set.") from exc


class SharedAccountView(BaseModel):
    """One shared account as any signed-in user sees it: never the token."""

    model_config = ConfigDict(extra="forbid")

    name: str
    account: str
    label: str
    created_by: str
    created_at: str
    updated_by: str
    updated_at: str


class AddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=200)
    label: str | None = Field(default=None, max_length=200)
    token: str = Field(max_length=4000)


class LabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(max_length=200)


@dataclass(frozen=True)
class TokenAnswer:
    """What a host gets for a launch: the token, and the account's id and label."""

    name: str
    label: str
    token: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SharedAccountsStore:
    """The shared accounts, mirrored to a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._accounts: list[dict[str, str]] = []
        self._loaded = False
        self._load_error: str | None = None

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
            # Never overwrite a file we could not read: it holds the only copy
            # of the ciphertexts. Every call fails until it is fixed.
            self._load_error = f"{self._path} could not be read ({exc})"
            raise SaveError(
                f"{self._load_error}; fix or move the file aside, then retry."
            ) from exc
        accounts = raw.get("accounts") if isinstance(raw, dict) else None
        if not isinstance(accounts, list):
            raise SaveError(
                f"{self._path} is not a shared accounts file; fix or move it aside, then retry."
            )
        kept: list[dict[str, str]] = []
        for entry in accounts:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            token = entry.get("token")
            if not isinstance(name, str) or not valid_name(name) or not isinstance(token, str):
                _logger.warning("ae shared accounts: skipped an invalid entry in %s", self._path)
                continue
            if any(item["name"] == name for item in kept):
                continue
            label = entry.get("label")
            kept.append(
                {
                    "name": name,
                    "label": label if isinstance(label, str) and valid_name(label) else name,
                    "token": token,
                    "created_by": str(entry.get("created_by") or ""),
                    "created_at": str(entry.get("created_at") or ""),
                    "updated_by": str(entry.get("updated_by") or ""),
                    "updated_at": str(entry.get("updated_at") or ""),
                }
            )
        self._accounts = kept
        self._loaded = True
        self._reseal_all()

    def _reseal_all(self) -> None:
        """With several keys set, re-encrypt every token under the first, so the old can go.

        Runs once as the file loads. A token no key opens stays as it is (the
        error surfaces when it is fetched); a failed write leaves the file as it was.
        """
        if not key_configured() or "," not in (os.environ.get(KEY_ENV) or ""):
            return
        changed = False
        resealed: list[dict[str, str]] = []
        for entry in self._accounts:
            try:
                sealed = reseal(entry["token"])
            except SharedAccountsError:
                resealed.append(entry)
                continue
            changed = changed or sealed != entry["token"]
            resealed.append({**entry, "token": sealed})
        if changed:
            try:
                self._save(resealed)
            except SaveError:
                _logger.warning("ae shared accounts: could not re-encrypt under the first key")

    def _save(self, accounts: list[dict[str, str]]) -> None:
        if self._path is None:
            self._accounts = accounts
            return
        body = {"version": 1, "accounts": accounts}
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-shared-accounts-", dir=str(self._path.parent))
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
            os.replace(tmp, self._path)
        except OSError as exc:
            _logger.warning("ae shared accounts: could not write %s: %s", self._path, exc)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            raise SaveError(
                f"could not write {self._path}; check that the folder is writable and has "
                "space, then retry."
            ) from exc
        self._accounts = accounts

    @staticmethod
    def _view(entry: dict[str, str]) -> SharedAccountView:
        return SharedAccountView(
            name=entry["name"],
            account=account_id(entry["name"]),
            label=entry["label"],
            created_by=entry["created_by"],
            created_at=entry["created_at"],
            updated_by=entry["updated_by"],
            updated_at=entry["updated_at"],
        )

    def _find(self, name: str) -> dict[str, str]:
        for entry in self._accounts:
            if entry["name"] == name:
                return entry
        raise NotFoundError(f"No shared Claude account is named {name!r}.")

    def views(self) -> list[SharedAccountView]:
        """Every shared account, by label; never a token."""
        with self._lock:
            self._load()
            views = [self._view(entry) for entry in self._accounts]
        return sorted(views, key=lambda view: (view.label.lower(), view.name))

    def add(self, name: str, label: str | None, token: str, who: str) -> SharedAccountView:
        """Add an account; its token is encrypted before it is held anywhere.

        :raises SharedAccountsError: On a bad name, label or token, a missing or
            bad key, a name or label already used, or a failed write.
        """
        name = check_name(name)
        label = check_label(label if label and label.strip() else name)
        sealed = encrypt(check_token(token))
        with self._lock:
            self._load()
            if any(entry["name"] == name for entry in self._accounts):
                raise ExistsError(f"A shared Claude account is already named {name!r}.")
            if any(entry["label"].lower() == label.lower() for entry in self._accounts):
                raise ExistsError(f"A shared Claude account is already labelled {label!r}.")
            if len(self._accounts) >= MAX_ACCOUNTS:
                raise FullError(f"At most {MAX_ACCOUNTS} shared accounts are kept.")
            stamp = _now()
            entry = {
                "name": name,
                "label": label,
                "token": sealed,
                "created_by": who,
                "created_at": stamp,
                "updated_by": who,
                "updated_at": stamp,
            }
            self._save([*self._accounts, entry])
            return self._view(entry)

    def relabel(self, name: str, label: str, who: str) -> SharedAccountView:
        """Change what the picker shows for *name*; the id and token stay."""
        label = check_label(label)
        with self._lock:
            self._load()
            current = self._find(name)
            if any(
                entry["name"] != name and entry["label"].lower() == label.lower()
                for entry in self._accounts
            ):
                raise ExistsError(f"A shared Claude account is already labelled {label!r}.")
            changed = {**current, "label": label, "updated_by": who, "updated_at": _now()}
            self._save([changed if entry is current else entry for entry in self._accounts])
            return self._view(changed)

    def delete(self, name: str) -> SharedAccountView:
        """Remove *name* and its token; a host that selected it fails its next launch."""
        with self._lock:
            self._load()
            current = self._find(name)
            self._save([entry for entry in self._accounts if entry is not current])
            return self._view(current)

    def token(self, name: str) -> TokenAnswer:
        """The decrypted token for *name*, for one launch."""
        with self._lock:
            self._load()
            current = self._find(name)
            sealed, label = current["token"], current["label"]
        return TokenAnswer(name=name, label=label, token=decrypt(sealed))

    def known(self, name: str) -> bool:
        """Whether *name* is a shared account; never raises."""
        try:
            with self._lock:
                self._load()
                return any(entry["name"] == name for entry in self._accounts)
        except SharedAccountsError:
            return False


def _default_path() -> Path | None:
    raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: SharedAccountsStore | None = None
_store_lock = threading.Lock()


def store() -> SharedAccountsStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SharedAccountsStore(_default_path())
        return _store


def reset_store(replacement: SharedAccountsStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


def is_known_account(account: str) -> bool:
    """Whether host-side id *account* (``shared:<name>``) names a shared account here.

    Registered with upstream's shared-account hook (patch 0117) so a fork can
    pick a shared account the source host does not list itself.
    """
    if not account.startswith(PREFIX):
        return False
    return store().known(account[len(PREFIX) :])


__all__ = [
    "CODE_BAD_KEY",
    "CODE_EXISTS",
    "CODE_FULL",
    "CODE_NOT_FOUND",
    "CODE_NOT_SAVED",
    "CODE_NO_KEY",
    "CODE_UNREADABLE",
    "DATA_DIR",
    "KEY_ENV",
    "PREFIX",
    "STATE_FILE_ENV",
    "AddRequest",
    "LabelRequest",
    "SharedAccountView",
    "SharedAccountsError",
    "SharedAccountsStore",
    "TokenAnswer",
    "account_id",
    "decrypt",
    "encrypt",
    "is_known_account",
    "key_configured",
    "reset_store",
    "store",
    "valid_name",
]
