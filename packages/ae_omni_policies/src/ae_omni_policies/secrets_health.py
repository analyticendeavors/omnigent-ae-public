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
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel

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
    "AE_FABRIC_WAKE_URL_AETRAINING": "Fabric Wake, aetraining",
    "AE_FABRIC_SUSPEND_KEY_AETRAINING": "Fabric Off, aetraining",
    "AE_FABRIC_CAPACITIES_URL": "Fabric capacity list and resize",
    "AE_FABRIC_CAPACITIES_KEY": "Fabric capacity list and resize",
    "AE_DATABRICKS_CLIENT_ID": "Databricks section",
    "AE_DATABRICKS_CLIENT_SECRET": "Databricks section",
    "AE_AZURE_COST_TENANT_ID": "Azure card",
    "AE_AZURE_COST_CLIENT_ID": "Azure card",
    "AE_AZURE_COST_CLIENT_SECRET": "Azure card",
    "AE_SHARED_ACCOUNTS_KEY": "Shared Claude accounts",
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


def view(environ: Mapping[str, str] | None = None) -> SecretsView:
    """Every expected key with whether ``environ`` (default: the process) has it."""
    env = os.environ if environ is None else environ
    required = _rows(REQUIRED, env)
    return SecretsView(
        required=required,
        optional=_rows(OPTIONAL, env),
        missing=[row.key for row in required if not row.present],
    )


__all__ = ["OPTIONAL", "REQUIRED", "SecretKey", "SecretsView", "view"]
