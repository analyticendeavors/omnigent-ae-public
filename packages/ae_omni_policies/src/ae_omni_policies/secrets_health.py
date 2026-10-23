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
Reid chose a mirrored list with that test on 2026-10-20 over shipping the
script in the image or a shared key file, which would have changed the deploy
script and the Dockerfile. The host's ``CLAUDE_CODE_OAUTH_TOKEN`` is left out:
it lives in the host's own file, which the server never sees.

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

from pydantic import BaseModel

from ae_omni_policies import fabric, taskstore

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


class SecretKey(BaseModel):
    key: str
    present: bool
    use: str


class SecretsView(BaseModel):
    required: list[SecretKey]
    optional: list[SecretKey]
    missing: list[str]
    """Required keys the server lacks, for the section's alarm."""


def _rows(keys: Mapping[str, str], env: Mapping[str, str]) -> list[SecretKey]:
    return [
        SecretKey(key=key, present=bool((env.get(key) or "").strip()), use=use)
        for key, use in keys.items()
    ]


def _capacity_rows(env: Mapping[str, str]) -> list[SecretKey]:
    rows: list[SecretKey] = []
    found = fabric.capacities(env)
    for name, capacity in found.items():
        rows += _rows(
            {
                f"{fabric.WAKE_URL_PREFIX}{capacity.env_suffix}": f"Fabric Wake, {name}",
                f"{fabric.SUSPEND_KEY_PREFIX}{capacity.env_suffix}": f"Fabric Off, {name}",
            },
            env,
        )
    # A suspend key with no wake URL beside it is a typo or a half-set pair;
    # list it so the card points at it instead of hiding it.
    paired = {capacity.env_suffix for capacity in found.values()}
    for key in sorted(env):
        suffix = key.removeprefix(fabric.SUSPEND_KEY_PREFIX)
        if suffix != key and suffix not in paired and (env.get(key) or "").strip():
            rows += _rows({key: f"Fabric Off, {suffix.lower()}, no wake URL beside it"}, env)
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
    required = _rows(needed, env)
    return SecretsView(
        required=required,
        optional=_rows(OPTIONAL, env) + _capacity_rows(env) + _rows(unused, env),
        missing=[row.key for row in required if not row.present],
    )


__all__ = [
    "CONDITIONAL",
    "OPTIONAL",
    "PER_CAPACITY",
    "REQUIRED",
    "SecretKey",
    "SecretsView",
    "view",
]
