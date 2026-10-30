"""Which secrets the running server has, by name, for the Admin Center's Secrets section.

Why (2026-10-20, https://github.com/analyticendeavors/omnigent-ae/issues/139):
``deploy/pull-secrets.sh --check`` already says which keys the secrets file
holds, but only at a shell on the box, and a key the server started without
shows up as a feature failing somewhere else (a Databricks section asking for
credentials, a Merge button that cannot merge). The section lists every key
the server expects with present or missing, so the cause is one glance away.

Names only: a value, its length or a hash of it never leaves this module, so
the answer is safe to render on a page and to log.

The key lists mirror ``SERVER_KEYS`` and ``SERVER_OPTIONAL_KEYS`` in
``deploy/pull-secrets.sh``; ``tests/test_secrets_health.py`` parses the script
and fails on any difference, so a key added there has to be added here too.
The owner chose a mirrored list with that test on 2026-10-20 over shipping the
script in the image or a shared key file, which would have changed the deploy
script and the Dockerfile. The host's ``CLAUDE_CODE_OAUTH_TOKEN`` is left out:
it lives in the host's own file, which the server never sees.

Expiry (2026-10-23): each row also carries its Key Vault name, expiry, last change and
Edit and Rotate links, from the box's daily reading (``secrets_meta.py``,
``secrets_relay.py``). The server never reads the vault itself.

Host key and last worked (2026-10-23, the Key Vault card concept): the host's
``CLAUDE_CODE_OAUTH_TOKEN`` joins the required list as a ``where="host"`` row once
the box's relay has said, by key name only, whether the host's env file has it.
Before that reading the row is left out, as it always was. A missing host key goes
in ``host_missing``, not ``missing``, because its fix is the host's file, not a
server restart. Each server row also carries when the server's own calls last
worked with it or were refused (``key_health.py``, in memory since the server
started); a refused required key goes in ``failing``.

Presence is read from the server's environment, which Compose fills from the
secrets file when the container starts, so a re-pull shows here only after the
next restart. A key whose value is empty or only spaces counts as missing.
``--check`` greps for ``KEY=`` and one character, so it would call a value of
spaces present; the script never writes one (it refuses an empty secret).

Required by mode (2026-10-22, README.md "Quick start"): the OIDC pair counts
as required only when the server signs in through OIDC (upstream's
``resolve_auth_source()``: an explicit ``OMNIGENT_AUTH_PROVIDER`` wins, else an
issuer with ``OMNIGENT_AUTH_ENABLED`` on), and the task-store pair only when
``taskstore.Settings`` has an app URL; otherwise both pairs are listed with the
optional keys. A server in accounts mode without the Parking Lot showed
"4 required missing" in red although nothing it uses was missing. The lists
themselves still mirror the script, whose ``--check`` still requires all
five: it is the Key Vault path of a full deploy, which uses both features.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from ae_omni_policies import fabric, key_health, secrets_meta, taskstore

# Each key, with what it turns on, in pull-secrets.sh's order.
REQUIRED: dict[str, str] = {
    "POSTGRES_PASSWORD": "Database",
    "AE_CF_ACCESS_CLIENT_ID": "Parking lot and Your move (task store)",
    "AE_CF_ACCESS_CLIENT_SECRET": "Parking lot and Your move (task store)",
    "OMNIGENT_OIDC_CLIENT_SECRET": "Microsoft sign-in",
    "OMNIGENT_OIDC_COOKIE_SECRET": "Microsoft sign-in",
}
OPTIONAL: dict[str, str] = {
    "AE_GITHUB_TOKEN": "PR cards, Merge, Deploys release check",
    "AE_FABRIC_CAPACITIES_URL": "Fabric capacity list and resize",
    "AE_FABRIC_CAPACITIES_KEY": "Fabric capacity list and resize",
    "AE_DATABRICKS_CLIENT_ID": "Databricks section",
    "AE_DATABRICKS_CLIENT_SECRET": "Databricks section",
    "AE_AZURE_COST_TENANT_ID": "Azure card",
    "AE_AZURE_COST_CLIENT_ID": "Azure card",
    "AE_AZURE_COST_CLIENT_SECRET": "Azure card",
    "AE_SHARED_ACCOUNTS_KEY": "Shared Claude accounts",
}


# Each Fabric capacity's pair (fabric.py) is named by the operator, so its rows
# come from the capacities the server has, not from a fixed list (2026-10-22:
# a stranger's server listed Analytic Endeavors' own capacity as missing).
# pull-secrets.sh fetches the box's own pair; PER_CAPACITY marks those keys.
PER_CAPACITY = (fabric.WAKE_URL_PREFIX, fabric.SUSPEND_KEY_PREFIX)


# Required keys that only a feature in use needs, by feature. view() decides
# whether each feature is on the way the server does: OIDC from upstream's
# resolve_auth_source(), the task store from taskstore.Settings.
CONDITIONAL: dict[str, tuple[str, ...]] = {
    "oidc": ("OMNIGENT_OIDC_CLIENT_SECRET", "OMNIGENT_OIDC_COOKIE_SECRET"),
    "task_store": ("AE_CF_ACCESS_CLIENT_ID", "AE_CF_ACCESS_CLIENT_SECRET"),
}


HOST_KEYS: dict[str, str] = {"CLAUDE_CODE_OAUTH_TOKEN": "Claude on the host"}
"""Keys the host needs (pull-secrets.sh ``HOST_KEYS``); the relay checks the host's file."""

# Which outbound calls prove which keys (key_health.record's feature names).
# Each Fabric capacity's pair is per capacity: see feature_of.
FEATURE_KEYS: dict[str, tuple[str, ...]] = {
    "github": ("AE_GITHUB_TOKEN",),
    "databricks": ("AE_DATABRICKS_CLIENT_ID", "AE_DATABRICKS_CLIENT_SECRET"),
    "azure_cost": (
        "AE_AZURE_COST_TENANT_ID",
        "AE_AZURE_COST_CLIENT_ID",
        "AE_AZURE_COST_CLIENT_SECRET",
    ),
    "taskstore": ("AE_CF_ACCESS_CLIENT_ID", "AE_CF_ACCESS_CLIENT_SECRET"),
    "fabric_fleet": ("AE_FABRIC_CAPACITIES_URL", "AE_FABRIC_CAPACITIES_KEY"),
    "oidc": ("OMNIGENT_OIDC_CLIENT_SECRET", "OMNIGENT_OIDC_COOKIE_SECRET"),
}
_KEY_FEATURE = {key: feature for feature, keys in FEATURE_KEYS.items() for key in keys}


def feature_of(key: str) -> str | None:
    """The key_health feature whose calls prove *key*, if any."""
    if key in _KEY_FEATURE:
        return _KEY_FEATURE[key]
    for prefix, feature in (
        (fabric.WAKE_URL_PREFIX, "fabric_wake"),
        (fabric.SUSPEND_KEY_PREFIX, "fabric_suspend"),
    ):
        if key.startswith(prefix):
            return f"{feature}:{key.removeprefix(prefix)}"
    return None


class SecretKey(secrets_meta.Words):
    """One key: whether the box has it, what it turns on, (from the box's Key Vault
    reading, ``secrets_meta.py``) its vault name, expiry and links, and (from
    ``key_health.py``) when the server's calls last worked with it."""

    key: str
    present: bool
    use: str
    where: Literal["server", "host"] = "server"
    check: Literal["ok", "failed"] | None = None
    """The newest outcome of a call that used the key; ``None`` before the first."""
    checked_at: str | None = None
    check_detail: str | None = None
    """A failure's status code or error class, never a body."""
    worked_at: str | None = None
    """The last success, kept across a later failure."""


class SecretsView(BaseModel):
    required: list[SecretKey]
    optional: list[SecretKey]
    missing: list[str]
    """Required keys the server lacks, for the section's alarm."""
    meta_read_at: str | None = None
    """When the box last posted the vault's attributes; ``None`` before the first."""
    meta_stale: bool = True
    """The daily timer has not posted for 36 hours (or ever): the dates may be old."""
    host_missing: list[str] = []
    """Required host keys the host's env file lacks, per the relay."""
    failing: list[str] = []
    """Required keys whose newest call was refused."""
    checks_since: str = key_health.STARTED.isoformat()
    """When the server started counting call outcomes (they live in memory)."""


def _rows(
    keys: Mapping[str, str],
    env: Mapping[str, str],
    reading: secrets_meta.Reading | None = None,
    now: datetime | None = None,
) -> list[SecretKey]:
    at = now or datetime.now(UTC)
    return [
        SecretKey(
            key=key,
            present=bool((env.get(key) or "").strip()),
            use=use,
            **secrets_meta.words(key, reading, at).model_dump(),
            **_check(key),
        )
        for key, use in keys.items()
    ]


def _check(key: str) -> dict[str, Any]:
    feature = feature_of(key)
    outcome = key_health.get(feature) if feature else None
    if outcome is None:
        return {}
    return {
        "check": "ok" if outcome.ok else "failed",
        "checked_at": outcome.at.isoformat(),
        "check_detail": outcome.detail,
        "worked_at": outcome.last_ok.isoformat() if outcome.last_ok else None,
    }


def _host_rows(reading: secrets_meta.Reading | None, now: datetime) -> list[SecretKey]:
    """The host's keys, once the relay has reported them; none before."""
    if reading is None or reading.host is None:
        return []
    return [
        SecretKey(
            key=key,
            present=reading.host.get(key, False),
            use=use,
            where="host",
            **secrets_meta.words(key, reading, now).model_dump(),
        )
        for key, use in HOST_KEYS.items()
    ]


def _capacity_rows(
    env: Mapping[str, str], reading: secrets_meta.Reading | None, now: datetime
) -> list[SecretKey]:
    rows: list[SecretKey] = []
    found = fabric.capacities(env)
    for name, capacity in found.items():
        rows += _rows(
            {
                f"{fabric.WAKE_URL_PREFIX}{capacity.env_suffix}": f"Fabric Wake, {name}",
                f"{fabric.SUSPEND_KEY_PREFIX}{capacity.env_suffix}": f"Fabric Off, {name}",
            },
            env,
            reading,
            now,
        )
    # A suspend key with no wake URL beside it is a typo or a half-set pair;
    # list it so the card points at it instead of hiding it.
    paired = {capacity.env_suffix for capacity in found.values()}
    for key in sorted(env):
        suffix = key.removeprefix(fabric.SUSPEND_KEY_PREFIX)
        if suffix != key and suffix not in paired and (env.get(key) or "").strip():
            rows += _rows(
                {key: f"Fabric Off, {suffix.lower()}, no wake URL beside it"}, env, reading, now
            )
    return rows


def view(environ: Mapping[str, str] | None = None, auth_source: str | None = None) -> SecretsView:
    """Every expected key with whether ``environ`` (default: the process) has it.

    A conditional key whose feature is off moves to the end of the optional list,
    after one Wake and Off pair per Fabric capacity the environment names.

    :param auth_source: The server's auth mode; upstream's ``resolve_auth_source()``
        (which reads the process environment) when omitted, as in
        ``session_cookie.install``.
    """
    env = os.environ if environ is None else environ
    if auth_source is None:
        from omnigent.server.auth import resolve_auth_source

        auth_source = resolve_auth_source()
    on = {
        "oidc": auth_source == "oidc",
        "task_store": bool(taskstore.Settings.from_env(env).app_url),
    }
    off = {key for feature, keys in CONDITIONAL.items() if not on[feature] for key in keys}
    needed = {key: use for key, use in REQUIRED.items() if key not in off}
    unused = {key: use for key, use in REQUIRED.items() if key in off}
    now = datetime.now(UTC)
    reading = secrets_meta.store().read()
    required = _rows(needed, env, reading, now)
    host = _host_rows(reading, now)
    return SecretsView(
        required=required + host,
        optional=(
            _rows(OPTIONAL, env, reading, now)
            + _capacity_rows(env, reading, now)
            + _rows(unused, env, reading, now)
        ),
        missing=[row.key for row in required if not row.present],
        host_missing=[row.key for row in host if not row.present],
        failing=[row.key for row in required if row.present and row.check == "failed"],
        meta_read_at=reading.read_at.isoformat() if reading else None,
        meta_stale=secrets_meta.stale(reading, now),
    )


__all__ = [
    "CONDITIONAL",
    "FEATURE_KEYS",
    "HOST_KEYS",
    "OPTIONAL",
    "PER_CAPACITY",
    "REQUIRED",
    "SecretKey",
    "SecretsView",
    "feature_of",
    "view",
]
