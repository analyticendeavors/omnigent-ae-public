"""The Admin Center's card registry: what cards exist, which are on, and how each is set up.

Why (2026-11-07, card setup): every card was configured only by server environment variables,
with no on/off switch and no setup screen, and the Social card carried the owner's channels
into the public install. This module is the server's single source of truth for three things:

* the **catalog**: each card's id, name, group, one line of description, the Needs you sources
  it owns (:data:`ae_omni_policies.attention.SOURCES`), the Box health secret keys only it
  uses, and its setup fields (the environment variable behind each, never a value);
* the **switch**: an admin turns a card on or off for everyone on the server. State lives in
  ``/data/ae-admin-cards.json`` (``AE_ADMIN_CARDS_FILE`` moves it, an empty value keeps it in
  memory), revisioned so two admins editing at once get a conflict, not a silent overwrite;
* the **private cards**: a card marked private (Social) exists only on a server whose
  ``AE_PRIVATE_CARDS`` lists it, so the public install never shows the owner's.

Off means the board, the status pill and Needs you only. Routes that hosts, deploy scripts,
sessions or the MCP tool list depend on keep answering (deploys, accounts, presets, decision,
box). Only a card's own outside-calling read routes answer ``{"off": true}``.

The state file follows ``attention_dismissals.py``: an unreadable file (bad JSON, wrong shape)
is **unknown**, never empty. Reads report ``state_ok`` false and fall back to the defaults;
writes raise and leave the file untouched, so a damaged file is never overwritten.

The store is in process, like :mod:`ae_omni_policies.sidebar_prefs`, because the server runs as
one process; two server processes would not see each other's switches until a restart.

A private card (Social) is on by default once a server names it in ``AE_PRIVATE_CARDS``: the
owner chose "your server turns it on with one setting" (2026-11-07). The admin switch then
works like any other card's.

Nothing here returns an environment value. Setup status is presence only (an empty or
spaces-only value is missing, as :mod:`ae_omni_policies.secrets_health` does).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ae_omni_policies import card_secrets, github

_logger = logging.getLogger(__name__)

STATE_FILE_ENV = "AE_ADMIN_CARDS_FILE"
PRIVATE_ENV = "AE_PRIVATE_CARDS"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-admin-cards.json"
CODE_NOT_SAVED = "cards_not_saved"
CODE_UNREADABLE = "cards_state_unreadable"
CODE_CONFLICT = "cards_changed_elsewhere"
CODE_UNKNOWN_CARD = "unknown_card"
DOCS_PATH = "docs/features/admin-center.md"
HINT_MAX = 140
RETRY_S = 30.0
"""A damaged state file is read again at most this often (or sooner when its mtime changes)."""

Kind = Literal["text", "list", "number", "date", "bool", "url", "secret"]
Group = Literal["ship", "box", "cloud", "money"]


@dataclass(frozen=True)
class Field_:
    """One setup field: the variable behind it and a plain-words hint.

    *prefix* marks a name that ends in ``_`` and stands for a family of variables (one per
    capacity or per owner); it counts as set when any variable with that prefix has a value.
    """

    label: str
    env: str
    kind: Kind
    hint: str
    env_only: bool = False
    prefix: bool = False
    editable: bool = False
    """Typed in the Cards panel (2026-11-07). Only a field every reader of which runs in the
    server process, and which is not a URL, host, endpoint, tenant, client id or secret."""
    default: str | None = None
    """What the code uses when nothing is set, shown as "Default: <value>" (never a secret)."""
    unit: str = ""
    """For a *prefix* field: what one variable stands for ("account", "capacity")."""
    host: bool = False
    """Read by a host-side relay or script, so it is set in config/host.env, not here."""
    typed: bool = False
    """A secret typed on the screen through its own routes (never through ``/cards/settings``,
    so it stays ``env_only`` there); the answer lists each owner's set-up status (2026-11-08)."""


@dataclass(frozen=True)
class Card:
    """One card in the catalog."""

    id: str
    name: str
    group: Group
    description: str
    default_on: bool = True
    private: bool = False
    sources: tuple[str, ...] = ()
    secret_keys: tuple[str, ...] = ()
    secret_prefixes: tuple[str, ...] = ()
    fields: tuple[Field_, ...] = field(default_factory=tuple)


_GITHUB_TOKEN = Field_(
    "GitHub token",
    "AE_GITHUB_TOKEN",
    "secret",
    "A read-only token the server uses for GitHub. It never leaves the server.",
    env_only=True,
)
_DATABRICKS = (
    Field_(
        "Databricks address",
        "AE_DATABRICKS_HOST",
        "url",
        "The workspace address, starting with https://. Set in the environment only.",
        env_only=True,
    ),
    Field_(
        "Databricks client id",
        "AE_DATABRICKS_CLIENT_ID",
        "secret",
        "The id of the service principal that reads Databricks.",
        env_only=True,
    ),
    Field_(
        "Databricks client secret",
        "AE_DATABRICKS_CLIENT_SECRET",
        "secret",
        "The secret of that service principal. It never leaves the server.",
        env_only=True,
    ),
)

#: The catalog, in board order within each group (the web's DOMAINS order).
CATALOG: tuple[Card, ...] = (
    Card(
        "deploys",
        "Deploys",
        "ship",
        "Hold automatic deploys, and see the release running against the newest.",
        fields=(
            Field_(
                "Release repository",
                "AE_RELEASE_REPO",
                "text",
                "Where the newest release is checked, as owner/repo. Optional.",
            ),
            _GITHUB_TOKEN,
        ),
        secret_keys=("AE_GITHUB_TOKEN",),
    ),
    Card(
        "git",
        "Git repos",
        "ship",
        "Pull requests, security alerts and old branches across your repositories.",
        sources=("git", "git-mine"),
        secret_keys=("AE_GITHUB_TOKEN", "AE_CARD_SECRETS_KEY"),
        fields=(
            Field_(
                "Sweep config",
                "AE_GIT_SWEEP_CONFIG",
                "text",
                "A file that lists the repositories to watch, as owner/repo:path. Default: none.",
                editable=True,
            ),
            Field_(
                "Sweep workflow",
                "AE_GIT_SWEEP_WORKFLOW",
                "text",
                "The sweep workflow file whose runs are shown. Default dependency-sweep.yml.",
                editable=True,
                default="dependency-sweep.yml",
            ),
            Field_(
                "Repositories",
                "AE_GIT_REPOS",
                "list",
                "Extra repositories to watch, comma separated, as owner/repo. Default: none.",
                editable=True,
            ),
            Field_(
                "View only",
                "AE_GIT_VIEW_ONLY",
                "list",
                "Repositories shown but left out of the totals, comma separated, as owner/repo.",
                editable=True,
            ),
            Field_(
                "Branches to keep",
                "AE_GIT_KEEP_BRANCHES",
                "list",
                "Branches kept on purpose and never flagged as old, as owner/repo:branch.",
                editable=True,
            ),
            Field_(
                "Token for one account",
                "AE_GIT_TOKEN_",
                "secret",
                "One read-only token per GitHub account: type it here, or set it in the "
                "server's environment, which wins.",
                env_only=True,
                prefix=True,
                unit="account",
                typed=True,
            ),
            _GITHUB_TOKEN,
            Field_(
                "GitHub address",
                "AE_GITHUB_API_URL",
                "url",
                "Only for GitHub Enterprise: the API address. Leave unset for github.com.",
                env_only=True,
                default="https://api.github.com",
            ),
        ),
    ),
    Card(
        "social",
        "Social",
        "ship",
        "A plan for what to post on LinkedIn and YouTube, and when.",
        private=True,
        fields=(
            Field_(
                "Channel ids",
                "AE_SOCIAL_CHANNEL_IDS",
                "text",
                "The YouTube channel id for each channel, as a JSON object.",
            ),
            Field_(
                "Channel names",
                "AE_SOCIAL_CHANNEL_NAMES",
                "text",
                "The name each channel goes by in a post, as a JSON object.",
            ),
            Field_(
                "Tools to promote",
                "AE_SOCIAL_TOOLS",
                "text",
                "The tools the weekly plan promotes, as a JSON list of id, name and link.",
            ),
        ),
    ),
    Card(
        "box",
        "Box health",
        "box",
        "Heat, disk, cleanup and the secrets this server expects.",
        sources=("box", "host"),
        fields=(
            Field_(
                "Box admins",
                "AE_BOX_ADMINS",
                "list",
                "Sign-ins that may see this card without being an OmniGent admin.",
            ),
            Field_(
                "Box users",
                "AE_BOX_USERS",
                "list",
                "The users on the box whose temp folders are counted.",
                host=True,
            ),
            Field_(
                "Cleanup buttons",
                "AE_BOX_HEALTH_CLEANUP",
                "text",
                "Set to off to hide every cleanup button and stop the automatic aging.",
                host=True,
                default="on",
            ),
        ),
    ),
    Card(
        "home-security",
        "Home security",
        "box",
        "The home network's health and its firewall, read from the home app.",
        fields=(
            Field_(
                "Home app address",
                "AE_HOME_SECURITY_URL",
                "url",
                "Where the home app answers, as http://host:port. Set in the environment only.",
                env_only=True,
            ),
            Field_(
                "Security page link",
                "AE_HOME_SECURITY_LINK",
                "url",
                "The web address people open for the home app's Security tab.",
                env_only=True,
            ),
        ),
    ),
    Card(
        "compute",
        "Compute",
        "cloud",
        "Fabric capacities you can wake or pause, and Databricks warehouses.",
        secret_keys=(
            "AE_FABRIC_CAPACITIES_URL",
            "AE_FABRIC_CAPACITIES_KEY",
            "AE_DATABRICKS_CLIENT_ID",
            "AE_DATABRICKS_CLIENT_SECRET",
        ),
        secret_prefixes=("AE_FABRIC_WAKE_URL_", "AE_FABRIC_SUSPEND_KEY_"),
        fields=(
            Field_(
                "Wake link for one capacity",
                "AE_FABRIC_WAKE_URL_",
                "secret",
                "The signed link that wakes one capacity, named with the capacity in capitals.",
                env_only=True,
                prefix=True,
                unit="capacity",
            ),
            Field_(
                "Pause key for one capacity",
                "AE_FABRIC_SUSPEND_KEY_",
                "secret",
                "The key that lets the card pause that capacity. Optional.",
                env_only=True,
                prefix=True,
                unit="capacity",
            ),
            Field_(
                "Capacity list link",
                "AE_FABRIC_CAPACITIES_URL",
                "secret",
                "The signed link that lists every capacity at once. It carries a signature.",
                env_only=True,
            ),
            Field_(
                "Capacity list key",
                "AE_FABRIC_CAPACITIES_KEY",
                "secret",
                "The key sent with the capacity list link.",
                env_only=True,
            ),
            Field_(
                "Price per capacity hour",
                "AE_FABRIC_USD_PER_CU_HOUR",
                "number",
                "Dollars per capacity-unit hour, for the cost estimate. Default 0.18.",
                editable=True,
                default="0.18",
            ),
            *_DATABRICKS,
        ),
    ),
    Card(
        "credit",
        "Databricks credit",
        "cloud",
        "Credit used against a trial or commit amount.",
        sources=("databricks",),
        secret_keys=("AE_DATABRICKS_CLIENT_ID", "AE_DATABRICKS_CLIENT_SECRET"),
        fields=(
            Field_(
                "Credit amount",
                "AE_DATABRICKS_CREDIT_USD",
                "number",
                "The credit in dollars. Without it, and both dates, the card shows no credit.",
                editable=True,
            ),
            Field_(
                "Credit cadence",
                "AE_DATABRICKS_CREDIT_CADENCE",
                "text",
                "monthly or once. Default once.",
                editable=True,
                default="once",
            ),
            Field_(
                "Credit start",
                "AE_DATABRICKS_CREDIT_START",
                "date",
                "The first day the credit counts, as YYYY-MM-DD.",
                editable=True,
            ),
            Field_(
                "Credit end",
                "AE_DATABRICKS_CREDIT_END",
                "date",
                "The last day the credit counts, as YYYY-MM-DD.",
                editable=True,
            ),
            *_DATABRICKS,
        ),
    ),
    Card(
        "azure",
        "Azure spend",
        "cloud",
        "Spend by subscription, with cost targets and alarms.",
        sources=("cost",),
        secret_keys=(
            "AE_AZURE_COST_TENANT_ID",
            "AE_AZURE_COST_CLIENT_ID",
            "AE_AZURE_COST_CLIENT_SECRET",
        ),
        fields=(
            Field_(
                "Subscriptions",
                "AE_AZURE_COST_SUBSCRIPTIONS",
                "list",
                "Subscriptions to read, comma separated, each as id=name (the name is optional).",
                editable=True,
            ),
            Field_(
                "Tenant",
                "AE_AZURE_COST_TENANT_ID",
                "text",
                "The directory the service principal belongs to. Set in the environment only.",
                env_only=True,
            ),
            Field_(
                "Client id",
                "AE_AZURE_COST_CLIENT_ID",
                "secret",
                "The id of a service principal that can read cost.",
                env_only=True,
            ),
            Field_(
                "Client secret",
                "AE_AZURE_COST_CLIENT_SECRET",
                "secret",
                "The secret of that service principal. It never leaves the server.",
                env_only=True,
            ),
        ),
    ),
    Card(
        "vault",
        "Key Vault",
        "cloud",
        "When each secret in your vault expires.",
        sources=("vault",),
        fields=(
            Field_(
                "Vault name",
                "AE_VAULT_NAME",
                "text",
                "The name of the Key Vault the box reads each day.",
                host=True,
            ),
        ),
    ),
    Card(
        "decision",
        "Decision model",
        "cloud",
        "Whether the decision model is on, and how many calls it has made.",
        secret_keys=("AE_DECISION_API_KEY", "AE_DECISION_ENDPOINT"),
        fields=(
            Field_(
                "Model address",
                "AE_DECISION_ENDPOINT",
                "url",
                "Where the model answers. Set in the environment only.",
                env_only=True,
            ),
            Field_(
                "Model key",
                "AE_DECISION_API_KEY",
                "secret",
                "The key the server sends to the model. It never leaves the server.",
                env_only=True,
            ),
            Field_(
                "Denylist file",
                "AE_DECISION_DENYLIST_FILE",
                "text",
                "A file listing what the model must never decide. It must not be empty.",
            ),
            Field_(
                "Switch",
                "AE_DECISION_ENABLED",
                "text",
                "Set to 1 to turn the decision model on once the rest is set.",
                default="off",
            ),
        ),
    ),
    Card(
        "accounts",
        "Accounts",
        "money",
        "Shared Claude and Codex accounts and their limits.",
        sources=("accounts",),
        secret_keys=("AE_SHARED_ACCOUNTS_KEY",),
        fields=(
            Field_(
                "Encryption key",
                "AE_SHARED_ACCOUNTS_KEY",
                "secret",
                "The key that protects shared account tokens at rest. It never leaves the server.",
                env_only=True,
            ),
        ),
    ),
    Card(
        "presets",
        "Spend",
        "money",
        "Preset runs and what each family of chats costs.",
        sources=("family",),
        fields=(
            Field_(
                "Preset runs",
                "AE_PRESET_RUNS",
                "text",
                "Set to 0 to stop recording preset runs.",
                default="on",
            ),
            Field_(
                "Family cost",
                "AE_FAMILY_COST",
                "text",
                "Set to 0 to stop counting what each family of chats costs.",
                default="on",
            ),
        ),
    ),
)

BY_ID: dict[str, Card] = {card.id: card for card in CATALOG}


def _check_catalog() -> None:
    for card in CATALOG:
        for item in card.fields:
            if len(item.hint) > HINT_MAX:
                raise ValueError(f"hint for {item.env} is over {HINT_MAX} characters")


_check_catalog()


# ── private cards ─────────────────────────────────────────────────────────────────────────────


def private_enabled(environ: Mapping[str, str] | None = None) -> set[str]:
    """The private card ids this server opted in to with :data:`PRIVATE_ENV`."""
    env = os.environ if environ is None else environ
    return {part.strip() for part in (env.get(PRIVATE_ENV) or "").split(",") if part.strip()}


def exists(card_id: str, environ: Mapping[str, str] | None = None) -> bool:
    """Whether the card is in this server's catalog (a private card only when opted in)."""
    card = BY_ID.get(card_id)
    if card is None:
        return False
    return not card.private or card_id in private_enabled(environ)


def visible(environ: Mapping[str, str] | None = None) -> list[Card]:
    """The catalog this server shows: every card, less the private ones it did not opt in to."""
    return [card for card in CATALOG if exists(card.id, environ)]


# ── the state file ────────────────────────────────────────────────────────────────────────────


class CardsUnreadable(Exception):
    """The state file exists but is damaged; writes are refused so it is never overwritten."""


class CardsSaveError(OSError):
    """The state could not be written; nothing was changed."""


class CardsConflict(Exception):
    """The caller's revision is not the one held; *state* is what is held now."""

    def __init__(self, state: dict[str, Any]) -> None:
        super().__init__("the card state changed elsewhere")
        self.state = state


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _valid(raw: Any) -> bool:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return False
    revision = raw.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        return False
    cards = raw.get("cards")
    if not isinstance(cards, dict):
        return False
    for entry in cards.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("on"), bool):
            return False
    settings = raw.get("settings", {})
    if not isinstance(settings, dict):
        return False
    for fields in settings.values():
        if not isinstance(fields, dict):
            return False
        for entry in fields.values():
            if not isinstance(entry, dict) or not isinstance(entry.get("value"), str):
                return False
    return True


class CardStore:
    """The saved on/off state, optionally mirrored to a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._revision = 0
        self._cards: dict[str, dict[str, Any]] = {}
        # card id -> variable name -> {"value", "by", "at"} (settings typed in the panel,
        # 2026-11-07); kept in the same file under the same revision as the switches.
        self._settings: dict[str, dict[str, dict[str, str]]] = {}
        self._loaded = False
        self._ok = True
        self._retry_at = 0.0
        self._bad_mtime: float | None = None

    def _mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime if self._path else None
        except OSError:
            return None

    def _load(self) -> None:
        """Read the file once it is readable; an unreadable one is retried on the next call."""
        if self._loaded or self._path is None:
            return
        if not self._ok and time.monotonic() < self._retry_at and self._mtime() == self._bad_mtime:
            return
        self._retry_at = time.monotonic() + RETRY_S
        self._bad_mtime = self._mtime()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._loaded = True
            self._ok = True
            return
        except (OSError, ValueError) as exc:
            self._ok = False
            _logger.warning("ae cards: %s is unreadable (%s); using defaults.", self._path, exc)
            return
        if not _valid(raw):
            self._ok = False
            _logger.warning("ae cards: %s is not a card state file; using defaults.", self._path)
            return
        self._revision = raw["revision"]
        self._cards = {
            str(key): {
                "on": entry["on"],
                "by": str(entry.get("by") or ""),
                "at": str(entry.get("at") or ""),
            }
            for key, entry in raw["cards"].items()
        }
        self._settings = {
            str(card): {
                str(name): {
                    "value": entry["value"],
                    "by": str(entry.get("by") or ""),
                    "at": str(entry.get("at") or ""),
                }
                for name, entry in fields.items()
            }
            for card, fields in raw.get("settings", {}).items()
        }
        self._loaded = True
        self._ok = True

    def _save(self) -> None:
        if self._path is None:
            return
        body = {
            "version": 1,
            "revision": self._revision,
            "cards": self._cards,
            "settings": self._settings,
        }
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-admin-cards-", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError as exc:
            _logger.warning("ae cards: could not write %s: %s", self._path, exc)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            raise CardsSaveError(
                f"could not write {self._path}; check that the folder is writable and has "
                "space, then retry."
            ) from exc

    def _snap(self) -> dict[str, Any]:
        return {
            "revision": self._revision,
            "state_ok": True,
            "cards": {key: dict(entry) for key, entry in self._cards.items()},
            "settings": {
                card: {name: dict(entry) for name, entry in fields.items()}
                for card, fields in self._settings.items()
            },
        }

    def snapshot(self) -> dict[str, Any]:
        """``{"revision", "state_ok", "cards", "settings"}``; defaults when unreadable."""
        with self._lock:
            self._load()
            if not self._ok:
                return {"revision": 0, "state_ok": False, "cards": {}, "settings": {}}
            return self._snap()

    def saved_value(self, card_id: str, env_name: str) -> str | None:
        """The value typed in the panel for one variable, or ``None``.

        An unreadable file is unknown, so it answers ``None`` (the reader then falls back to its
        default), never a stale guess.
        """
        with self._lock:
            self._load()
            if not self._ok:
                return None
            entry = self._settings.get(card_id, {}).get(env_name)
            return entry["value"] if entry else None

    def set_on(
        self, card_id: str, on: bool, login: str, revision: int, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """Switch a card, when *revision* is the one held; returns the new snapshot.

        :raises CardsUnreadable: When the file is damaged; it is left as it was.
        :raises CardsConflict: When *revision* is stale; carries the current snapshot.
        :raises CardsSaveError: When the write fails; the held state is restored.
        """
        with self._lock:
            self._load()
            if not self._ok:
                raise CardsUnreadable(
                    f"the card state file {self._path} is damaged; fix or remove it, then retry."
                )
            if revision != self._revision:
                raise CardsConflict(self._snap())
            before = (self._revision, dict(self._cards))
            self._cards[card_id] = {
                "on": on,
                "by": login,
                "at": _iso(now or datetime.now(UTC)),
            }
            self._revision += 1
            try:
                self._save()
            except CardsSaveError:
                self._revision, self._cards = before
                raise
            snap = self._snap()
        _logger.info("ae cards: %s turned %s by %s", card_id, "on" if on else "off", login)
        return snap

    def set_settings(
        self,
        card_id: str,
        changes: Mapping[str, str | None],
        login: str,
        revision: int,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Save (or, with ``None``, clear) variables typed in the panel; returns the snapshot.

        The caller has already validated each value. One revision bump covers the whole
        request; a request that changes nothing bumps nothing and writes nothing.
        The log line names the card, the variables and the login, never a value.

        :raises CardsUnreadable: When the file is damaged; it is left as it was.
        :raises CardsConflict: When *revision* is stale; carries the current snapshot.
        :raises CardsSaveError: When the write fails; the held state is restored.
        """
        with self._lock:
            self._load()
            if not self._ok:
                raise CardsUnreadable(
                    f"the card state file {self._path} is damaged; fix or remove it, then retry."
                )
            if revision != self._revision:
                raise CardsConflict(self._snap())
            held = self._settings.get(card_id, {})
            if all(
                (value is None and name not in held)
                or (value is not None and name in held and held[name]["value"] == value)
                for name, value in changes.items()
            ):
                # Nothing would change (clearing what is not saved, or saving what is already
                # saved): no new revision, no file write, no log line.
                return self._snap()
            before = (
                self._revision,
                {card: {n: dict(e) for n, e in f.items()} for card, f in self._settings.items()},
            )
            stamp = _iso(now or datetime.now(UTC))
            fields = self._settings.setdefault(card_id, {})
            for name, value in changes.items():
                if value is None:
                    fields.pop(name, None)
                else:
                    fields[name] = {"value": value, "by": login, "at": stamp}
            if not fields:
                self._settings.pop(card_id, None)
            self._revision += 1
            try:
                self._save()
            except CardsSaveError:
                self._revision, self._settings = before
                raise
            snap = self._snap()
        _logger.info(
            "ae cards: %s settings changed (%s) by %s", card_id, ", ".join(sorted(changes)), login
        )
        return snap


def _default_path() -> Path | None:
    if STATE_FILE_ENV in os.environ:
        raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
        return Path(raw) if raw else None
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: CardStore | None = None
_store_lock = threading.Lock()


def store() -> CardStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = CardStore(_default_path())
        return _store


def reset_store(replacement: CardStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


# ── asking ────────────────────────────────────────────────────────────────────────────────────


def is_on(
    card_id: str,
    environ: Mapping[str, str] | None = None,
    *,
    snap: Mapping[str, Any] | None = None,
) -> bool:
    """Whether the card is on: it exists here, and its saved switch (else its default) says on.

    *snap* is a snapshot the caller already holds, so a whole answer reads one state.
    """
    card = BY_ID.get(card_id)
    if card is None or not exists(card_id, environ):
        return False
    saved = (snap if snap is not None else store().snapshot())["cards"].get(card_id)
    if saved is not None:
        return bool(saved["on"])
    return card.default_on


def off_sources(environ: Mapping[str, str] | None = None) -> set[str]:
    """The Needs you sources whose every owning card is off. ``secrets`` is never in it."""
    owners: dict[str, list[str]] = {}
    for card in CATALOG:
        for source in card.sources:
            owners.setdefault(source, []).append(card.id)
    return {
        source
        for source, ids in owners.items()
        if not any(is_on(card_id, environ) for card_id in ids)
    }


def all_off(*card_ids: str, environ: Mapping[str, str] | None = None) -> bool:
    """True when every named card is off."""
    return not any(is_on(card_id, environ) for card_id in card_ids)


def off_answer(card_id: str) -> dict[str, Any]:
    """What an off card's own read route answers, with no outside call."""
    return {"off": True, "card": card_id}


def _present(env: Mapping[str, str], name: str, prefix: bool) -> bool:
    return _count(env, name, prefix) > 0


def _count(env: Mapping[str, str], name: str, prefix: bool) -> int:
    """How many non-blank variables answer to *name* (a prefix stands for a family)."""
    if prefix:
        return sum(
            1 for key, value in env.items() if key.startswith(name) and (value or "").strip()
        )
    return 1 if (env.get(name) or "").strip() else 0


EDITABLE: dict[str, str] = {
    item.env: card.id for card in CATALOG for item in card.fields if item.editable
}
"""Editable variable name -> the card that owns it (each is owned by exactly one card)."""


def _check_editable() -> None:
    seen: set[str] = set()
    for card in CATALOG:
        for item in card.fields:
            if not item.editable:
                continue
            if item.kind in ("url", "secret") or item.env_only or item.prefix or item.host:
                raise ValueError(f"{item.env} cannot be editable")
            if item.env in seen:
                raise ValueError(f"{item.env} is editable on two cards")
            seen.add(item.env)


_check_editable()


def field_source(
    item: Field_, env: Mapping[str, str], saved: Mapping[str, Any] | None
) -> Literal["env", "saved", "default", "missing"]:
    """Where a field's value comes from: the environment wins, then the panel, then the code."""
    if _present(env, item.env, item.prefix):
        return "env"
    if item.editable and saved is not None and item.env in saved:
        return "saved"
    return "default" if item.default is not None else "missing"


def keys_dropped_when_off(
    environ: Mapping[str, str] | None = None,
) -> tuple[set[str], tuple[str, ...]]:
    """Secret keys (and key prefixes) whose every owning card is off.

    A key several cards list (the GitHub token) is dropped only when all of them are off.
    """
    env = os.environ if environ is None else environ
    key_owners: dict[str, list[str]] = {}
    prefix_owners: dict[str, list[str]] = {}
    for card in CATALOG:
        for key in card.secret_keys:
            key_owners.setdefault(key, []).append(card.id)
        for prefix in card.secret_prefixes:
            prefix_owners.setdefault(prefix, []).append(card.id)
    keys = {key for key, ids in key_owners.items() if not any(is_on(i, env) for i in ids)}
    prefixes = tuple(
        prefix for prefix, ids in prefix_owners.items() if not any(is_on(i, env) for i in ids)
    )
    return keys, prefixes


def token_owners(env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """Each company GitHub account's token status: ``{owner, env_name, source, saved_by,
    saved_at, github_login, expires_at}``, never a value.

    ``source`` is ``env`` (the server's environment sets the account's variable, which wins),
    ``saved`` (typed on the screen) or ``missing``. ``fallback`` says ``AE_GITHUB_TOKEN`` would
    read the account anyway. The owners are the company repo list's, plus any with a saved token.
    """
    from ae_omni_policies import git_center

    env = os.environ if env is None else env
    try:
        saved = card_secrets.list_meta("company/git/") if env is os.environ else {}
    except card_secrets.CardSecretsError as exc:
        _logger.warning("ae cards: saved tokens not read (%s)", type(exc).__name__)
        saved = {}
    owners = git_center.company_owners(env)
    known = {o.lower() for o in owners}
    for record_id in saved:
        name = record_id.rsplit("/", 1)[1]
        if name not in known:
            owners.append(name)
            known.add(name)
    fallback = bool((env.get(github.TOKEN_ENV) or "").strip())
    rows = []
    for owner in sorted(owners, key=str.lower):
        env_name = git_center.owner_env_name(owner)
        meta = saved.get(card_secrets.company_id(owner)) or {}
        if (env.get(env_name) or "").strip():
            source = "env"
        elif meta.get("set"):
            source = "saved"
        else:
            source = "missing"
        rows.append(
            {
                "owner": owner,
                "env_name": env_name,
                "source": source,
                "saved_by": meta.get("saved_by") if source == "saved" else None,
                "saved_at": meta.get("saved_at") if source == "saved" else None,
                "github_login": meta.get("github_login") if source == "saved" else None,
                "expires_at": meta.get("expires_at") if source == "saved" else None,
                "unreadable": bool(meta.get("unreadable")) and source == "saved",
                "fallback": fallback,
            }
        )
    return rows


def _field_row(item: Field_, env: Mapping[str, str], typed: Mapping[str, Any]) -> dict[str, Any]:
    """One field's answer: where its value comes from and, for a field typed in the panel, that
    value. An environment value is never returned; a prefix field reports only a count."""
    source = field_source(item, env, typed)
    row: dict[str, Any] = {
        "label": item.label,
        "env": item.env,
        "kind": item.kind,
        "hint": item.hint,
        "env_only": item.env_only,
        "status": "set" if source in ("env", "saved") else "missing",
        "editable": item.editable,
        "source": source,
        "host": item.host,
        "default": item.default,
        "saved_by": None,
        "saved_at": None,
        "value": None,
        "count": None,
        "unit": item.unit or None,
        "typed": item.typed,
    }
    if item.prefix:
        row["count"] = _count(env, item.env, True)
    if item.typed:
        row["key_configured"] = card_secrets.key_configured()
        row["owners"] = token_owners(env)
    entry = typed.get(item.env) if item.editable else None
    if entry is not None:
        row["saved_by"] = entry.get("by") or None
        row["saved_at"] = entry.get("at") or None
        row["value"] = entry["value"]
    return row


def view(
    environ: Mapping[str, str] | None = None, *, snap: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The Cards panel's answer: revision, state_ok and every visible card. Never a value.

    Every row and the revision come from one snapshot (*snap*, else a fresh one), so a switch
    landing mid-answer cannot pair one state with another's revision.
    """
    env = os.environ if environ is None else environ
    snap = snap if snap is not None else store().snapshot()
    rows = []
    for card in visible(env):
        saved = snap["cards"].get(card.id)
        typed = snap.get("settings", {}).get(card.id, {})
        rows.append(
            {
                "id": card.id,
                "name": card.name,
                "group": card.group,
                "description": card.description,
                "sources": list(card.sources),
                "on": is_on(card.id, env, snap=snap),
                "default_on": card.default_on,
                "changed_by": saved["by"] if saved else None,
                "changed_at": saved["at"] if saved else None,
                "fields": [_field_row(item, env, typed) for item in card.fields],
            }
        )
    return {"revision": snap["revision"], "state_ok": snap["state_ok"], "cards": rows}


__all__ = [
    "BY_ID",
    "CATALOG",
    "CODE_CONFLICT",
    "CODE_NOT_SAVED",
    "CODE_UNKNOWN_CARD",
    "CODE_UNREADABLE",
    "DOCS_PATH",
    "PRIVATE_ENV",
    "STATE_FILE_ENV",
    "Card",
    "CardStore",
    "CardsConflict",
    "CardsSaveError",
    "CardsUnreadable",
    "Field_",
    "all_off",
    "exists",
    "is_on",
    "keys_dropped_when_off",
    "off_answer",
    "off_sources",
    "private_enabled",
    "reset_store",
    "store",
    "view",
    "visible",
]
