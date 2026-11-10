"""Each admin's own Git repo list (2026-11-08, Admin Center card setup PR 3).

The Git card's company list is one list for the whole server, read with company tokens. This
module keeps the other kind: a list per admin, read only with that admin's own GitHub token
(:mod:`ae_omni_policies.card_secrets`, record ``user/<login>/git/<account>``). A personal repo is
never read with a company token, with ``AE_GITHUB_TOKEN`` or with another user's token; the
reading rule lives in :class:`ae_omni_policies.git_center.PersonalSettings`, which asks
:func:`token_for`.

What is kept, per sign-in login: the repos (``{"repo": "owner/name", "account":
"<github-login-lower>"}``, at most :data:`MAX_REPOS`), a revision number so two devices that save
at once do not silently overwrite each other (the later save gets a conflict carrying the current
state), and ``seen_at``, the last time that login opened the Git card (written at most once an
hour, so there is no disk churn). The background refresh reads only logins seen in the last seven
days, at most :data:`MAX_USERS` of them.

The file shape is ``{"version": 1, "users": {"<login>": {"revision": n, "repos": [...],
"seen_at": "<iso>"}}}``. Where it lives: :data:`STATE_FILE_ENV` when set (blank means memory
only), else ``/data/ae-git-personal.json`` when ``/data`` is a directory, else memory only, like
``sidebar_prefs``. It holds repo names, never a token.

Offboarding: :func:`clear_user` drops a login's repos and every token saved under
``user/<login>/``. :func:`sync_roster` asks OmniGent's member list (as the calling admin, at most
once every ten minutes for the whole server) and clears a login that is no longer a member; a
member who is no longer an admin is skipped by every read and refresh, not deleted. A missing,
failed or empty answer never deletes anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Request

from ae_omni_policies import card_secrets, github

_logger = logging.getLogger(__name__)

STATE_FILE_ENV = "AE_GIT_PERSONAL_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-git-personal.json"
MAX_USERS = 20
MAX_REPOS = 30
#: ``seen_at`` is rewritten at most this often per login.
SEEN_EVERY_S = 3600.0
#: The background refresh reads logins seen within this long.
ACTIVE_DAYS = 7
#: The member list is asked at most this often for the whole server.
ROSTER_EVERY_S = 600.0
#: How many members the roster asks for (upstream's OIDC ``/auth/users`` defaults to 100 and
#: allows 1000). An answer that fills the limit may be cut short, so it prunes nobody.
ROSTER_LIMIT = 1000
CODE_NOT_SAVED = "git_personal_not_saved"


class PersonalSaveError(OSError):
    """The list could not be written to its file; nothing was changed."""


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(text: object) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize(repos: list[dict[str, str]]) -> list[dict[str, str]]:
    """*repos* with the account lower-cased, the repo name checked and repeats removed.

    :raises ValueError: When a name is not ``owner/name``, an account is blank, or more than
        :data:`MAX_REPOS` remain.
    """
    seen: set[tuple[str, str]] = set()
    kept: list[dict[str, str]] = []
    for item in repos:
        repo = str(item.get("repo") or "").strip()
        account = str(item.get("account") or "").strip().lower()
        if not github._REPO.match(repo) or len(repo) > 140:
            raise ValueError("each repo must be written owner/name")
        if not account or len(account) > 39:
            raise ValueError("each repo needs the GitHub account whose token reads it")
        key = (repo.lower(), account)
        if key not in seen:
            seen.add(key)
            kept.append({"repo": repo, "account": account})
    if len(kept) > MAX_REPOS:
        raise ValueError(f"at most {MAX_REPOS} personal repos are kept; remove some first")
    return kept


@dataclass(frozen=True)
class PutResult:
    """What :meth:`PersonalStore.put` did: the state now held, and whether it conflicted."""

    revision: int
    repos: list[dict[str, str]]
    conflict: bool = False


class PersonalStore:
    """Per-login lists, optionally mirrored to a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._users: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._memory_only = False
        self._seen_written: dict[str, float] = {}

    # ── file ─────────────────────────────────────────────────────────────────────────────────

    def _quarantine(self, reason: object) -> None:
        assert self._path is not None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        aside = self._path.with_name(f"{self._path.name}.corrupt-{stamp}")
        try:
            os.replace(self._path, aside)
        except OSError as exc:
            self._memory_only = True
            _logger.warning(
                "ae git personal: %s is unreadable (%s) and could not be moved aside (%s); "
                "lists stay in memory until restart.",
                self._path,
                type(reason).__name__ if isinstance(reason, Exception) else reason,
                type(exc).__name__,
            )
            return
        _logger.warning("ae git personal: %s was unreadable; moved it to %s", self._path, aside)

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
            self._quarantine("not a personal Git file")
            return
        for login, entry in users.items():
            if not isinstance(login, str) or not card_secrets.valid_login(login):
                continue
            if not isinstance(entry, dict):
                continue
            revision = entry.get("revision")
            repos = entry.get("repos")
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 0
                or not isinstance(repos, list)
            ):
                continue
            try:
                cleaned = normalize([r for r in repos if isinstance(r, dict)])
            except ValueError:
                cleaned = []
            self._users[login] = {
                "revision": revision,
                "repos": cleaned,
                "seen_at": str(entry.get("seen_at") or ""),
            }

    def _save(self) -> bool:
        if self._path is None or self._memory_only:
            return True
        body = {"version": 1, "users": self._users}
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-git-personal-", dir=str(self._path.parent))
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
            os.replace(tmp, self._path)
        except OSError as exc:
            _logger.warning("ae git personal: could not write %s: %s", self._path, exc)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            return False
        return True

    # ── reading ──────────────────────────────────────────────────────────────────────────────

    def get(self, login: str) -> tuple[int, list[dict[str, str]]]:
        """(revision, repos) for *login*: ``(0, [])`` when none was saved."""
        with self._lock:
            self._load()
            entry = self._users.get(login)
            if entry is None:
                return 0, []
            return entry["revision"], [dict(r) for r in entry["repos"]]

    def counts(self) -> dict[str, tuple[int, int]]:
        """login to (accounts with repos, repos) for every stored login."""
        with self._lock:
            self._load()
            return {
                login: (len({r["account"] for r in entry["repos"]}), len(entry["repos"]))
                for login, entry in self._users.items()
            }

    def logins(self) -> list[str]:
        with self._lock:
            self._load()
            return sorted(self._users)

    def active(self, now: datetime, *, days: int = ACTIVE_DAYS) -> list[str]:
        """Logins with repos that opened the card within *days*, most recent first."""
        with self._lock:
            self._load()
            cutoff = now - timedelta(days=days)
            rows = []
            for login, entry in self._users.items():
                seen = _stamp(entry.get("seen_at"))
                if entry["repos"] and seen is not None and seen >= cutoff:
                    rows.append((seen, login))
        return [login for _seen, login in sorted(rows, reverse=True)]

    # ── writing ──────────────────────────────────────────────────────────────────────────────

    def put(
        self,
        login: str,
        repos: list[dict[str, str]],
        base_revision: int,
        *,
        now: datetime | None = None,
    ) -> PutResult:
        """Replace *login*'s list when *base_revision* is the revision held.

        :param repos: Already normalized (:func:`normalize`).
        :raises PersonalSaveError: When the file cannot be written; nothing changes.
        """
        moment = now or datetime.now(UTC)
        with self._lock:
            self._load()
            entry = self._users.get(login)
            held = entry["revision"] if entry is not None else 0
            if held != base_revision:
                current = [dict(r) for r in entry["repos"]] if entry is not None else []
                return PutResult(held, current, conflict=True)
            revision = held + 1
            self._users[login] = {
                "revision": revision,
                "repos": [dict(r) for r in repos],
                "seen_at": _iso(moment),
            }
            if not self._save():
                if entry is None:
                    del self._users[login]
                else:
                    self._users[login] = entry
                raise PersonalSaveError(
                    f"could not write {self._path}; check that the folder is writable and has "
                    "space, then retry."
                )
            self._seen_written[login] = time.monotonic()
            return PutResult(revision, [dict(r) for r in repos])

    def touch(self, login: str, *, now: datetime | None = None) -> bool:
        """Note that *login* opened the Git card; written at most once an hour per login.

        Only a login that already has a list is recorded. Returns whether it wrote.
        """
        clock = time.monotonic()
        with self._lock:
            self._load()
            entry = self._users.get(login)
            if entry is None:
                return False
            last = self._seen_written.get(login)
            if last is not None and clock - last < SEEN_EVERY_S:
                return False
            before = entry["seen_at"]
            entry["seen_at"] = _iso(now or datetime.now(UTC))
            self._seen_written[login] = clock
            if not self._save():
                entry["seen_at"] = before
                self._seen_written.pop(login, None)
                return False
            return True

    def drop_user(self, login: str) -> bool:
        """Remove *login*'s list (their tokens are the caller's to remove); whether it existed."""
        with self._lock:
            self._load()
            entry = self._users.pop(login, None)
            if entry is None:
                return False
            self._seen_written.pop(login, None)
            if not self._save():
                self._users[login] = entry
                raise PersonalSaveError(
                    f"could not write {self._path}; check that the folder is writable and has "
                    "space, then retry."
                )
            return True


def _default_path() -> Path | None:
    if STATE_FILE_ENV in os.environ:
        raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
        return Path(raw) if raw else None
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: PersonalStore | None = None
_store_lock = threading.Lock()


def store() -> PersonalStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = PersonalStore(_default_path())
        return _store


_login_locks: dict[str, asyncio.Lock] = {}


def login_lock(login: str) -> asyncio.Lock:
    """The lock that serializes one login's changes (a save, a repo edit, a removal).

    A route holds it across reading the revision, writing the list and writing the token, so two
    overlapping requests of one person cannot interleave and leave a token that no list matches.
    """
    return _login_locks.setdefault(login, asyncio.Lock())


def reset_store(replacement: PersonalStore | None = None) -> None:
    """Drop the process-wide store and the held member list (tests change the environment)."""
    global _store
    with _store_lock:
        _store = replacement
    _login_locks.clear()
    reset_roster()


# ── tokens ────────────────────────────────────────────────────────────────────────────────────


def token_for(login: str, account: str) -> str:
    """*login*'s own saved token for GitHub account *account*; ``""`` when there is none.

    The only token a personal repo is ever read with.
    """
    return card_secrets.get_token(card_secrets.user_id(login, account)) or ""


def clear_account(login: str, account: str) -> None:
    """Remove *login*'s token for *account*; their repos for it are the caller's to drop."""
    card_secrets.delete(card_secrets.user_id(login, account))


def clear_user(login: str) -> tuple[bool, int]:
    """Offboard *login*: drop their list and every token saved under ``user/<login>/``.

    The log line names the login only. Returns (had a list, tokens removed).
    """
    tokens = card_secrets.delete_prefix(card_secrets.user_prefix(login))
    had = store().drop_user(login)
    _logger.info("ae git personal: cleared %s (list %s, %d tokens)", login, had, tokens)
    return had, tokens


# ── the member list ──────────────────────────────────────────────────────────────────────────

#: login to is_admin, from the last good answer; ``None`` until one arrives.
_roster: dict[str, bool] | None = None
#: False when the last answer filled the limit asked, so a login missing from it is unknown.
_roster_complete = True
_roster_asked: float | None = None
_roster_lock = threading.Lock()


def reset_roster() -> None:
    global _roster, _roster_asked, _roster_complete
    with _roster_lock:
        _roster = None
        _roster_asked = None
        _roster_complete = True


def roster() -> dict[str, bool] | None:
    """The last good member list (login to is_admin), or ``None`` when none was ever read."""
    return _roster


def is_active_admin(login: str) -> bool:
    """Whether *login* may have a personal list read: an admin in the last good roster.

    With no roster held, the login is taken as an admin (the routes check the live caller).
    """
    if _roster is None:
        return True
    if login not in _roster:
        return not _roster_complete
    return _roster[login]


async def sync_roster(
    request: Request, *, now: float | None = None, caller: str | None = None
) -> None:
    """Refresh the member list (at most every :data:`ROSTER_EVERY_S`) and clear departed logins.

    Asks ``/auth/users`` as the caller. Only a 200 answer that lists users changes anything:
    a stored login absent from it is cleared; a present one that is not an admin is skipped by
    :func:`is_active_admin`. A failed, non-200 or empty answer deletes nothing and keeps the
    last good roster. An answer with as many users as the limit asked (:data:`ROSTER_LIMIT`)
    may be cut short: it is kept for the admin check but prunes nobody. The *caller* is never
    pruned.
    """
    global _roster, _roster_asked, _roster_complete
    from omnigent.server.auth import RESERVED_USER_LOCAL

    from ae_omni_policies import shared_accounts_routes

    clock = time.monotonic() if now is None else now
    with _roster_lock:
        if _roster_asked is not None and clock - _roster_asked < ROSTER_EVERY_S:
            return
        _roster_asked = clock
    response = await shared_accounts_routes.ask_as_caller(
        request, f"/auth/users?limit={ROSTER_LIMIT}"
    )
    if response is None or response.status_code != 200:
        return
    try:
        body = response.json()
    except ValueError:
        return
    rows = body.get("users") if isinstance(body, dict) else None
    seen = {
        row["id"]: bool(row.get("is_admin"))
        for row in rows or []
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    if not seen:
        return
    complete = len(rows or []) < ROSTER_LIMIT
    with _roster_lock:
        _roster = seen
        _roster_complete = complete
    if not complete:
        return
    for login in store().logins():
        if login not in seen and login != RESERVED_USER_LOCAL and login != caller:
            try:
                clear_user(login)
            except (PersonalSaveError, card_secrets.CardSecretsError) as exc:
                _logger.warning(
                    "ae git personal: could not clear %s (%s)", login, type(exc).__name__
                )


__all__ = [
    "CODE_NOT_SAVED",
    "MAX_REPOS",
    "MAX_USERS",
    "STATE_FILE_ENV",
    "PersonalSaveError",
    "PersonalStore",
    "PutResult",
    "clear_account",
    "clear_user",
    "is_active_admin",
    "login_lock",
    "normalize",
    "reset_roster",
    "reset_store",
    "roster",
    "store",
    "sync_roster",
    "token_for",
]
