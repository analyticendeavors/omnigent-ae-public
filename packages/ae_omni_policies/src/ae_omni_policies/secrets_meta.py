"""When each server secret expires, kept from the box's Key Vault reading (2026-10-23).

Why: the Admin Center's Server secrets list (``secrets_health.py``) says which
keys the server has; the owner also wants to see which ones expire, when, and where
to rotate them, so a secret does not lapse unseen (the Databricks CLI secret,
2026-10-23, had no reminder at all).

The server never reads Key Vault: the vault uses access policies, and giving
the live container a vault login only for dates is more reach than the card
needs. The box does the reading instead. ``secrets_relay.py`` runs as the owner on
a daily timer, lists the vault's secrets (``az keyvault secret list`` returns
attributes only, never a value) and posts name, enabled, expires and updated
for the names below to ``POST /v1/ae/secrets/meta``. This module keeps that
post in a state file and works out each row's expiry words.

Names and dates only: a value, its length or a hash of it is never read,
stored or sent.

Both vaults (2026-10-23, the Key Vault card): the relay now posts every secret of
the box's vault and of each extra vault (``AE_EXTRA_VAULTS``), and the file
keeps all of them, so ``vault_inventory.py`` can list every key we own. Until then
the post was cut down to the names below. Every name now lands in ``/data`` and an
admin-only route; names say which services we use, values never leave Key Vault.
The owner accepted that trade on the concept page. The relay also says, by key name
only, whether the host's env file has its Claude token (``host``).

Source expiry (2026-10-23): a vault secret tagged ``ae-source=entra:<appId>``
carries the Entra app's real credential end date (``source_expires``), which the
relay reads with ``az ad app list``. The GitHub token's comes from GitHub itself
(``key_health.py``). A row's effective expiry is the earlier of the vault's date
and the source's, and the row says which one it is (``expiry_from``).

Last changed (review of PR 301, 2026-10-23): "changed" and the 180-day count use
``created``, when the secret's current version was made, not ``updated``. Setting a
tag or an expiry bumps ``updated``, so the source tags would have read as a change
made today. ``secret list`` reports the current version's ``created`` (checked
against ``list-versions`` that day), so no extra call is needed.

Partial readings (same review): the relay names the parts it could not read
(``failed``: ``entra``, ``host``, ``vault:<name>``), and :meth:`SecretsMetaStore.put`
keeps the previous reading of each, so a failed ``az ad app list`` or an
unreachable second vault never wipes good data. ``parts`` says when each part was
last read; a carried part shows "from <date>" on the page.

``VAULT_NAMES`` mirrors the ``KV_*`` defaults in ``deploy/pull-secrets.sh``;
``tests/test_secrets_health.py`` parses the script and fails on a difference.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

VAULT_ENV = "AE_VAULT_NAME"
"""The variable naming the box's Key Vault; the name lives in the box's env only."""

AZURE_COST_SECRET = "azure-cost-client-secret"
AZURE_COST_APP_ENV = "AE_AZURE_COST_APP_ID"
DATABRICKS_ACCOUNT_ENV = "AE_DATABRICKS_ACCOUNT_ID"
"""The Entra app id of the Azure cost reader. A real tenant identifier, so it is not in the
source tree (2026-10-28): the box's env names it, and the Entra link is hidden while unset."""


def box_vault() -> str | None:
    """The box's Key Vault name from ``AE_VAULT_NAME``, or ``None`` when it is unset or empty."""
    return os.environ.get(VAULT_ENV, "").strip() or None


STATE_FILE_ENV = "AE_SECRETS_META_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-secrets-meta.json"

SOON_DAYS = 60
"""A secret this close to its expiry shows amber."""
STALE_AFTER_S = 36 * 3600
"""The daily timer missed a run when the newest reading is older than this."""

# Each server key's Key Vault name, as deploy/pull-secrets.sh fetches it.
VAULT_NAMES: dict[str, str] = {
    "POSTGRES_PASSWORD": "omnigent-ae-postgres-password",
    "AE_CF_ACCESS_CLIENT_ID": "cf-access-svc-taskstore-client-id",
    "AE_CF_ACCESS_CLIENT_SECRET": "cf-access-svc-taskstore-client-secret",
    "OMNIGENT_OIDC_CLIENT_SECRET": "omnigent-oidc-client-secret",
    "OMNIGENT_OIDC_COOKIE_SECRET": "omnigent-oidc-cookie-secret",
    "AE_GITHUB_TOKEN": "omnigent-ae-github-token",
    "AE_FABRIC_WAKE_URL_AETRAINING": "aetraining-wake-url",
    "AE_FABRIC_SUSPEND_KEY_AETRAINING": "aetraining-suspend-key",
    "AE_FABRIC_CAPACITIES_URL": "fabric-capacities-url",
    "AE_FABRIC_CAPACITIES_KEY": "fabric-capacities-key",
    "AE_DATABRICKS_CLIENT_ID": "databricks-mvp-client-id",
    "AE_DATABRICKS_CLIENT_SECRET": "databricks-mvp-client-secret",
    "AE_AZURE_COST_TENANT_ID": "azure-cost-tenant-id",
    "AE_AZURE_COST_CLIENT_ID": "azure-cost-client-id",
    "AE_AZURE_COST_CLIENT_SECRET": "azure-cost-client-secret",
    "AE_SHARED_ACCOUNTS_KEY": "omnigent-ae-shared-accounts-key",
}

KNOWN_NAMES = frozenset(VAULT_NAMES.values())

# The host's own keys, which live in /secure/env/omnigent-host.env, not the
# server's file. The relay reports each one's presence by name only.
HOST_VAULT_NAMES: dict[str, str] = {"CLAUDE_CODE_OAUTH_TOKEN": "claude-code-oauth-token"}

BOX_NAMES = frozenset(VAULT_NAMES.values()) | frozenset(HOST_VAULT_NAMES.values())
"""Every vault name the box pulls, server and host."""

VAULT_HOST_SUFFIX = ".vault." + "azure.net"
"""The DNS suffix of a Key Vault; built in two pieces so no source file carries the whole host."""

MAX_SECRETS = 1000
"""The most secrets one vault may post (2026-10-23: 103 and 10 today)."""

PART_PATTERN = re.compile(r"^(?:entra|host|vault:[0-9A-Za-z-]{3,24})$")
"""A part of a reading the relay may report as failed."""

SOURCE_PATTERN = re.compile(r"^entra:[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
"""The one source tag value the relay understands: ``entra:<appId>``."""

GITHUB_VAULT_NAME = "omnigent-ae-github-token"
"""The vault secret behind AE_GITHUB_TOKEN; its source expiry comes from GitHub's own header."""


def _default_vault() -> str:
    return box_vault() or ""


VAULT_PATTERN = r"^[0-9A-Za-z-]{3,24}$"
"""A Key Vault name; keeps a posted name from reshaping the links built from it."""


def edit_url(vault: str, name: str) -> str:
    """The Azure portal page of secret *name* in *vault*."""
    return (
        "https://portal.azure.com/#@/asset/Microsoft_Azure_KeyVault/Secret/"
        f"https://{vault}{VAULT_HOST_SUFFIX}/secrets/{name}"
    )


# Where each secret is rotated, by vault name: a link and one line saying which
# account it opens. Each line names its account because a Databricks login in
# the wrong tenant cost an hour on 2026-10-23. Anything not listed gets the
# Key Vault page and DEFAULT_HINT.
DEFAULT_HINT = (
    "Make the new value where it is issued, then save it as a new version of this secret."
)
ROTATE: dict[str, tuple[str, str]] = {
    "omnigent-ae-github-token": (
        "https://github.com/settings/personal-access-tokens",
        "GitHub, signed in as the account that owns the token. "
        "Regenerate it, then save the new value here.",
    ),
    "databricks-mvp-client-id": (
        "https://accounts.cloud.databricks.com/user-management/serviceprincipals",
        "Databricks account Analytic Endeavors (MVP){account}, "
        "service principal ae-admin-center. The client ID does not change; rotate the secret.",
    ),
    "databricks-mvp-client-secret": (
        "https://accounts.cloud.databricks.com/user-management/serviceprincipals",
        "Databricks account Analytic Endeavors (MVP){account}, "
        "service principal ae-admin-center, Secrets tab. "
        "Check the account name before you generate one.",
    ),
}

ExpiryState = Literal["ok", "soon", "expired", "none", "unknown"]
ExpiryFrom = Literal["vault", "entra", "github"]


def _utc(value: datetime | None) -> datetime | None:
    """UTC and aware, years 1971 to 3000: a naive time from a hand-edited file or an
    extreme one whose conversion overflows would otherwise fail later, in a request."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    try:
        value = value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError("date out of range") from exc
    if not 1971 <= value.year <= 3000:
        raise ValueError("date out of range (years 1971 to 3000)")
    return value


class SecretMeta(BaseModel):
    """One vault secret's attributes, as the relay posts them."""

    name: str = Field(min_length=1, max_length=127)
    enabled: bool = True
    expires: datetime | None = None
    updated: datetime | None = None
    source: str | None = None
    """The ``ae-source`` tag, ``entra:<appId>``; anything else is dropped."""
    source_expires: datetime | None = None
    """When the source's credential ends (the Entra app's, read by the relay)."""
    source_count: int | None = Field(default=None, ge=0, le=100)
    """How many client secrets the source app has; ``source_expires`` is the latest's."""
    created: datetime | None = None
    """When the current version was made; tags and expiry edits leave it alone."""

    @property
    def changed(self) -> datetime | None:
        """When the value last changed: the current version's creation, else ``updated``."""
        return self.created or self.updated

    @field_validator("expires", "updated", "source_expires", "created")
    @classmethod
    def _in_utc(cls, value: datetime | None) -> datetime | None:
        return _utc(value)

    @field_validator("enabled", mode="before")
    @classmethod
    def _enabled(cls, value: object) -> object:
        """A row az lists without the attribute counts as enabled, as the vault's default."""
        return True if value is None else value

    @field_validator("source", mode="before")
    @classmethod
    def _known_source(cls, value: object) -> str | None:
        """A tag in any other shape is ignored, not refused: a hand-set tag must not
        cost the whole reading."""
        return value if isinstance(value, str) and SOURCE_PATTERN.match(value) else None


ENTRA_CREDENTIALS_URL = (
    "https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationMenuBlade/~/"
    "Credentials/appId/{app_id}"
)


def rotate_for(name: str) -> tuple[str | None, str] | None:
    """:data:`ROTATE`'s entry for a vault secret. The Azure cost app's Entra link is built from
    ``AE_AZURE_COST_APP_ID`` and is ``None`` (the vault page opens instead) while that is unset.
    The Databricks hints name the account id from ``AE_DATABRICKS_ACCOUNT_ID`` and leave it out
    while that is unset (2026-10-29)."""
    if name == AZURE_COST_SECRET:
        app_id = os.environ.get(AZURE_COST_APP_ENV, "").strip()
        if not app_id:
            return None, "Entra app ae-omnigent-azure-cost, Certificates and secrets."
        return (
            ENTRA_CREDENTIALS_URL.format(app_id=app_id),
            f"Entra app ae-omnigent-azure-cost ({app_id}), Certificates and secrets.",
        )
    entry = ROTATE.get(name)
    if entry is not None and "{account}" in entry[1]:
        account = os.environ.get(DATABRICKS_ACCOUNT_ENV, "").strip()
        entry = (entry[0], entry[1].replace("{account}", f", {account}" if account else ""))
    return entry


def rotate_link(meta: SecretMeta, vault: str) -> tuple[str, str]:
    """Where *meta* is rotated and what that page is: :data:`ROTATE`, else the Entra
    app its source tag names (2026-10-23, the expiry alerts), else its Key Vault page."""
    known = rotate_for(meta.name)
    if known is not None:
        return known[0] or edit_url(vault, meta.name), known[1]
    if meta.source and meta.source.startswith("entra:"):
        app_id = meta.source.removeprefix("entra:")
        return (
            ENTRA_CREDENTIALS_URL.format(app_id=app_id),
            f"Entra app {app_id}, Certificates and secrets. "
            "Add a new client secret, then save it as a new version of this secret.",
        )
    return edit_url(vault, meta.name), DEFAULT_HINT


class VaultPost(BaseModel):
    """One more vault's secrets, attributes only."""

    vault: str = Field(pattern=VAULT_PATTERN)
    secrets: list[SecretMeta] = Field(max_length=MAX_SECRETS)


class MetaPost(BaseModel):
    """The relay's body: the box vault's secrets, the other vaults', and the host's keys."""

    vault: str = Field(
        default_factory=_default_vault, validate_default=True, pattern=VAULT_PATTERN
    )
    """The vault the box pulls from; the Edit links point at it."""
    secrets: list[SecretMeta] = Field(max_length=MAX_SECRETS)
    vaults: list[VaultPost] = Field(default_factory=list, max_length=4)
    """Other vaults (the website's), for the Key Vault card only."""
    host: dict[str, bool] | None = None
    """Host key name to whether the host's env file has it; ``None`` when not checked."""
    failed: list[str] = Field(default_factory=list, max_length=10)
    """Parts the relay could not read (``entra``, ``host``, ``vault:<name>``); the
    store keeps their previous reading."""

    @field_validator("failed")
    @classmethod
    def _parts(cls, value: list[str]) -> list[str]:
        return [part for part in value if PART_PATTERN.match(part)]

    @field_validator("host")
    @classmethod
    def _host_keys(cls, value: dict[str, bool] | None) -> dict[str, bool] | None:
        if value is None:
            return None
        return {key: bool(on) for key, on in value.items() if key in HOST_VAULT_NAMES}


class Reading(BaseModel):
    read_at: datetime

    @field_validator("read_at")
    @classmethod
    def _read_at_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    vault: str = Field(
        default_factory=_default_vault, validate_default=True, pattern=VAULT_PATTERN
    )
    """Defaults to ``AE_VAULT_NAME``; with neither, the reading fails to load (card: not read)."""
    secrets: dict[str, SecretMeta]
    vaults: dict[str, dict[str, SecretMeta]] = Field(default_factory=dict)
    """The other vaults' secrets by vault name; empty from a relay older than 2026-10-23."""
    host: dict[str, bool] | None = None
    parts: dict[str, datetime] = Field(default_factory=dict)
    """When each part (``entra``, ``host``, ``vault:<name>``) was last read."""

    def carried(self, part: str) -> datetime | None:
        """When *part* was last read, if this reading carries it from an earlier one."""
        when = self.parts.get(part)
        return when if when is not None and when < self.read_at else None


class SecretsMetaStore:
    """The newest reading, kept in a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self._memory: Reading | None = None

    def read(self) -> Reading | None:
        """The stored reading; ``None`` when there is none, or the file cannot be read."""
        with self._lock:
            if self._path is None:
                return self._memory
            try:
                return Reading.model_validate_json(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

    def put(self, post: MetaPost, *, now: datetime | None = None) -> int:
        """Keep *post* as the newest reading; how many secrets it holds, every vault.

        :raises OSError: When the file cannot be written.
        """
        at = now or datetime.now(UTC)
        reading = carry_forward(post, self.read(), at)
        total = len(reading.secrets) + sum(len(rows) for rows in reading.vaults.values())
        with self._lock:
            if self._path is None:
                self._memory = reading
                return total
            tmp: str | None = None
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".ae-secrets-meta-", dir=str(self._path.parent))
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(reading.model_dump_json())
                os.replace(tmp, self._path)
            except OSError:
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                raise
        return total


def carry_forward(post: MetaPost, before: Reading | None, at: datetime) -> Reading:
    """*post* as a reading, with the previous reading of every part it marks failed."""
    failed = set(post.failed)
    kept = {item.name: item for item in post.secrets}
    others = {
        extra.vault: {item.name: item for item in extra.secrets}
        for extra in post.vaults
        if extra.vault != post.vault
    }
    host = post.host
    parts = dict(before.parts) if before else {}
    fresh = ["entra", *(f"vault:{name}" for name in [post.vault, *others])]
    if post.host is not None:
        fresh.append("host")
    for part in fresh:
        if part not in failed:
            parts[part] = at
    if before is not None:
        for part in failed:
            name = part.removeprefix("vault:")
            if part.startswith("vault:") and name not in others and name in before.vaults:
                others[name] = before.vaults[name]
        if "host" in failed and host is None:
            host = before.host
        if "entra" in failed:
            old = {
                (vault, item.name): item
                for vault, rows in [(before.vault, before.secrets), *before.vaults.items()]
                for item in rows.values()
            }
            for vault, rows in [(post.vault, kept), *others.items()]:
                for name, item in rows.items():
                    prev = old.get((vault, name))
                    if (
                        item.source
                        and item.source_expires is None
                        and prev is not None
                        and prev.source == item.source
                        and prev.source_expires is not None
                    ):
                        rows[name] = item.model_copy(
                            update={
                                "source_expires": prev.source_expires,
                                "source_count": prev.source_count,
                            }
                        )
    return Reading(
        read_at=at, vault=post.vault, secrets=kept, vaults=others, host=host, parts=parts
    )


def _default_path() -> Path | None:
    raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if STATE_FILE_ENV in os.environ:
        return None
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: SecretsMetaStore | None = None
_store_lock = threading.Lock()


def store() -> SecretsMetaStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SecretsMetaStore(_default_path())
        return _store


def reset_store(replacement: SecretsMetaStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


class Words(BaseModel):
    """What the list says about one key beyond present or missing."""

    vault_name: str | None = None
    enabled: bool | None = None
    expires: str | None = None
    """The effective expiry: the earlier of the vault's date and the source's."""
    updated: str | None = None
    days_left: int | None = None
    expiry_state: ExpiryState | None = None
    vault_expires: str | None = None
    """The vault's own expiry date, when it has one."""
    source_expires: str | None = None
    """The issuer's own end date (Entra, GitHub), when known."""
    expiry_from: ExpiryFrom | None = None
    """Which date ``expires`` is."""
    source_count: int | None = None
    """How many client secrets the Entra app has, when more than one."""
    source_from: str | None = None
    """When the Entra dates were last read, if this reading carries them forward."""
    edit_url: str | None = None
    rotate_url: str | None = None
    rotate_hint: str | None = None


def words(key: str, reading: Reading | None, now: datetime) -> Words:
    """Vault name, links and expiry for box key *key*; empty for a key with no vault name."""
    vault_name = VAULT_NAMES.get(key) or HOST_VAULT_NAMES.get(key)
    if vault_name is None:
        return Words()
    rotate_url, rotate_hint = rotate_for(vault_name) or (None, DEFAULT_HINT)
    vault = reading.vault if reading else box_vault()
    edit = edit_url(vault, vault_name) if vault else None
    out = Words(
        vault_name=vault_name,
        edit_url=edit,
        rotate_url=rotate_url or edit,
        rotate_hint=rotate_hint,
        expiry_state="unknown",
    )
    meta = reading.secrets.get(vault_name) if reading else None
    if meta is None:
        return out
    out.enabled = meta.enabled
    out.updated = meta.changed.isoformat() if meta.changed else None
    entra_from = reading.carried("entra") if reading else None
    return out.model_copy(update=expiry(meta, now, entra_from=entra_from).model_dump())


class Expiry(BaseModel):
    """A secret's expiry words: the effective date, where it came from, and its state."""

    expires: str | None = None
    vault_expires: str | None = None
    source_expires: str | None = None
    expiry_from: ExpiryFrom | None = None
    source_count: int | None = None
    source_from: str | None = None
    days_left: int | None = None
    expiry_state: ExpiryState = "none"


def source_of(meta: SecretMeta) -> tuple[ExpiryFrom, datetime] | None:
    """The issuer's end date for *meta*: Entra's from the relay, GitHub's from its header."""
    if meta.source_expires is not None and meta.source:
        return "entra", meta.source_expires
    if meta.name == GITHUB_VAULT_NAME:
        from ae_omni_policies import key_health

        when = key_health.expiry("AE_GITHUB_TOKEN")
        if when is not None:
            return "github", when
    return None


def days_until(when: datetime, now: datetime) -> int:
    """Whole days to *when*; negative once past (-1 on the day it passed)."""
    return (when - now).days if when > now else -1 - (now - when).days


def expiry(
    meta: SecretMeta, now: datetime, *, source: bool = True, entra_from: datetime | None = None
) -> Expiry:
    """The earlier of the vault's expiry and the source's, with its state.

    :param source: Whether the issuer's date may count (only the box vault's
        copy of the GitHub token is the one the server holds).
    :param entra_from: When the Entra dates were last read, if they are carried.
    """
    found = source_of(meta) if source else None
    entra = found is not None and found[0] == "entra"
    out = Expiry(
        vault_expires=meta.expires.isoformat() if meta.expires else None,
        source_expires=found[1].isoformat() if found else None,
        source_count=meta.source_count if entra and (meta.source_count or 0) > 1 else None,
        source_from=entra_from.isoformat() if entra and entra_from else None,
    )
    picks: list[tuple[datetime, ExpiryFrom]] = []
    if meta.expires is not None:
        picks.append((meta.expires, "vault"))
    if found is not None:
        picks.append((found[1], found[0]))
    if not picks:
        return out
    when, origin = min(picks, key=lambda pick: pick[0])
    out.expires = when.isoformat()
    out.expiry_from = origin
    out.days_left = days_until(when, now)
    out.expiry_state = "expired" if when <= now else "soon" if out.days_left < SOON_DAYS else "ok"
    return out


def stale(reading: Reading | None, now: datetime) -> bool:
    """Whether the newest reading is older than the daily timer should allow, or there is none."""
    return reading is None or (now - reading.read_at).total_seconds() > STALE_AFTER_S


__all__ = [
    "BOX_NAMES",
    "GITHUB_VAULT_NAME",
    "HOST_VAULT_NAMES",
    "KNOWN_NAMES",
    "MAX_SECRETS",
    "ROTATE",
    "SOON_DAYS",
    "STATE_FILE_ENV",
    "VAULT_ENV",
    "VAULT_HOST_SUFFIX",
    "VAULT_NAMES",
    "VAULT_PATTERN",
    "Expiry",
    "MetaPost",
    "Reading",
    "SecretMeta",
    "SecretsMetaStore",
    "VaultPost",
    "Words",
    "box_vault",
    "carry_forward",
    "days_until",
    "edit_url",
    "expiry",
    "reset_store",
    "rotate_link",
    "source_of",
    "stale",
    "store",
    "words",
]
