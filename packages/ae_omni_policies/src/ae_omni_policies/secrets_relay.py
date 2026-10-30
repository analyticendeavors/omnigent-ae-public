"""Read the vault's secret attributes on the box and post them to ``/v1/ae/secrets/meta``.

``python -m ae_omni_policies.secrets_relay`` runs once and exits; the user timer
``deploy/systemd/user/ae-omnigent-secrets-meta.timer`` runs it daily (2026-10-23).
Run it by hand after changing a secret's expiry to see the Admin Center's Server
secrets list catch up at once; ``--dry-run`` prints what it would post and
posts nothing.

The server never reads Key Vault (``secrets_meta.py`` says why), so this runs as
the owner, where ``az login`` lives, and sends name, enabled, expires and updated for
the vault names the server maps to its keys. ``az keyvault secret list`` returns
attributes only; this module never calls ``secret show``, so no value is read at
any point, and anything the list returns beyond those four fields is dropped
before it is printed or posted.

Both vaults (2026-10-23, the Key Vault card): it posts every secret of the box's
vault and of each extra vault (none by default; set ``AE_EXTRA_VAULTS``,
comma-separated), plus each secret's ``ae-source`` tag. It reads the
end dates of every Entra app's client secrets with ``az ad app list`` (appId,
display name and ``endDateTime`` only; never the ``hint``, which is the start of
the secret) and puts the latest one on each secret tagged
``ae-source=entra:<appId>``. And it checks, by key name only, whether the host's
env file (``/secure/env/omnigent-host.env``, ``AE_HOST_ENV_FILE``) has each host
key: the file is read line by line and only a yes or no per key is kept.

Why the latest end date: rotating an app secret adds a new credential and the
vault gets the new value, while the old credential lingers until it ends. The
newest end date is the one the vault's copy most likely has. Each such row also
carries how many secrets the app has, so the page can say "latest of 2".

Review of PR 301 (2026-10-23): the list also carries ``created``, the current
version's creation time, which is what "changed" means on the page; tagging bumps
``updated`` and must not read as a change. The az calls run side by side (one per
vault, one for the apps), and a part that fails is named in the post's ``failed``
so the server keeps its previous reading of it instead of wiping it.

``--propose-tags`` (``deploy/vault-source-tags.sh``) prints the ``ae-source`` tags
it would set, matching secret names to app display names and to known pairs;
``--apply`` sets them with ``az keyvault secret set-attributes``, keeping the
secret's other tags. Tags are attributes, so this too never reads a value.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from ae_omni_policies import omni, secrets_meta

VAULT_ENV = secrets_meta.VAULT_ENV
EXTRA_VAULTS_ENV = "AE_EXTRA_VAULTS"
HOST_ENV_FILE_ENV = "AE_HOST_ENV_FILE"
HOST_ENV_FILE = Path("/secure/env/omnigent-host.env")
AZ_BIN_ENV = "AE_AZ_BIN"
TIMEOUT_S = 60
SOURCE_TAG = "ae-source"

# One JMESPath projection: attributes and the one tag, so nothing else leaves az.
QUERY = (
    "[].{name:name,enabled:attributes.enabled,"
    "expires:attributes.expires,updated:attributes.updated,created:attributes.created,"
    'source:tags."ae-source"}'
)
FIELDS = ("name", "enabled", "expires", "updated", "created", "source")

# Apps with at least one client secret: id, name and each secret's end date.
# passwordCredentials also carries "hint", the first characters of the secret
# itself, so the projection names endDateTime alone.
APPS_QUERY = (
    "[?length(passwordCredentials) > `0`]"
    ".{appId:appId,displayName:displayName,ends:passwordCredentials[].endDateTime}"
)

# For --propose-tags only: each secret's tags, so --apply can keep the others.
TAGS_QUERY = "[].{name:name,tags:tags}"

# Vault secrets whose Entra app the name alone does not say (2026-10-23).
KNOWN_SOURCES: tuple[str, ...] = (  # plus OIDC_SOURCE_NAME, from the environment
    # azure-cost-client-secret: its app id comes from AE_AZURE_COST_APP_ID (known_sources).
    # Matched by the lead on 2026-10-23, each app credential's start time within a
    # minute of the secret's creation, and approved by the owner. hc-client-secret stays
    # out: its app is most likely in another tenant. The app ids are real tenant
    # identifiers, so they come from AE_SECRET_SOURCE_APPS (2026-10-29), not from here.
    "ae-client-secret",  # M365 MCP Server (AE)
    "devops-bridge-client-secret",  # AE Task Bridge
    "cf-access-azure-client-secret",  # Cloudflare Access - AE Shortener
    "esignatures-oauth-ms-client-secret",  # AE MCP Connectors
)
SOURCE_APPS_ENV = "AE_SECRET_SOURCE_APPS"
OIDC_APP_ENV = "AE_OIDC_APP_ID"
AZURE_COST_APP_ENV = "AE_AZURE_COST_APP_ID"
AZURE_COST_SECRET = "azure-cost-client-secret"
OIDC_SOURCE_NAME = "omnigent-oidc-client-secret"


def known_sources() -> dict[str, str]:
    """KNOWN_SOURCES plus the server's OIDC client app, which the environment names.

    The app ids are real tenant identifiers, so they are not in the source tree: set
    ``AE_OIDC_APP_ID`` (the same id as OMNIGENT_OIDC_CLIENT_ID in config/server.env) and
    ``AE_AZURE_COST_APP_ID`` (the Azure cost reader's app) on the relay's unit. The other known
    pairs come from ``AE_SECRET_SOURCE_APPS``, ``<vault secret>=<app id>`` comma-separated;
    while it is unset those secrets get no known-pair proposal.
    """
    known: dict[str, str] = {}
    for pair in os.environ.get(SOURCE_APPS_ENV, "").split(","):
        secret, _, pair_app = pair.partition("=")
        secret, pair_app = secret.strip(), pair_app.strip()
        if secret in KNOWN_SOURCES and pair_app:
            known[secret] = pair_app
    app_id = os.environ.get(OIDC_APP_ENV, "").strip()
    if app_id:
        known[OIDC_SOURCE_NAME] = app_id
    cost_app = os.environ.get(AZURE_COST_APP_ENV, "").strip()
    if cost_app:
        known[AZURE_COST_SECRET] = cost_app
    return known


SECRET_SUFFIXES = ("-client-secret", "-sp-secret", "-secret")
_WORD = re.compile(r"[a-z0-9]+")

Runner = Callable[[Sequence[str]], str]


class RelayError(Exception):
    """Something the operator has to fix; the message says what."""


def _run(argv: Sequence[str]) -> str:
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RelayError(f"could not run {argv[0]}: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()[-1:] or ["no message"]
        raise RelayError(
            f"`az {' '.join(argv[1:4])}` failed ({detail[0][:200]}). Check `az account show` "
            "as this user; if the login expired, run `az login --use-device-code`."
        )
    return done.stdout


def _list(out: str, what: str) -> list[Any]:
    try:
        rows = json.loads(out)
    except ValueError as exc:
        raise RelayError(f"az answered something that is not JSON ({exc})") from exc
    if not isinstance(rows, list):
        raise RelayError(f"az answered something other than a list of {what}")
    return rows


def list_secrets(vault: str, *, az_bin: str, runner: Runner = _run) -> list[dict[str, Any]]:
    """Every secret's attributes and source tag in *vault*, nothing else.

    :raises RelayError: When az fails or answers something that is not the list.
    """
    out = runner(
        [
            az_bin,
            "keyvault",
            "secret",
            "list",
            "--vault-name",
            vault,
            "--query",
            QUERY,
            "-o",
            "json",
        ]
    )
    return [
        {field: row.get(field) for field in FIELDS}
        for row in _list(out, "secrets")
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    ]


def read_vault(
    vault: str,
    *,
    az_bin: str,
    runner: Runner = _run,
    sources: Sources | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> secrets_meta.MetaPost:
    """The box vault's secrets, every one, with each tagged secret's source end date.

    :param sources: Entra appId (lower case) to its latest secret end date and count.
    :param rows: The vault's listing when already read (the side-by-side gather).
    :raises RelayError: When az fails, answers something that is not the list, or
        no secret matches a server key (a wrong vault: the server keeps its last
        good reading instead).
    """
    listed = rows if rows is not None else list_secrets(vault, az_bin=az_bin, runner=runner)
    kept = _with_sources(listed, sources)
    if not any(row["name"] in secrets_meta.KNOWN_NAMES for row in kept):
        raise RelayError(
            f"no secret in vault '{vault}' matches a server key, so nothing was posted and the "
            "server keeps its last good reading. Check the vault name (--vault or "
            f"{VAULT_ENV}) and that this login can list it."
        )
    try:
        return secrets_meta.MetaPost.model_validate({"vault": vault, "secrets": kept})
    except ValueError as exc:
        raise RelayError(f"az listed a secret this relay could not read ({exc})") from exc


def read_extra(
    vault: str,
    *,
    az_bin: str,
    runner: Runner = _run,
    sources: Sources | None = None,
    rows: list[dict[str, Any]] | None = None,
) -> secrets_meta.VaultPost:
    """Another vault's secrets, for the Key Vault card.

    :raises RelayError: As :func:`list_secrets`, or when a row cannot be read.
    """
    listed = rows if rows is not None else list_secrets(vault, az_bin=az_bin, runner=runner)
    try:
        return secrets_meta.VaultPost.model_validate(
            {"vault": vault, "secrets": _with_sources(listed, sources)}
        )
    except ValueError as exc:
        raise RelayError(
            f"az listed a secret in '{vault}' this relay could not read ({exc})"
        ) from exc


Sources = dict[str, tuple[datetime, int]]
"""Entra appId (lower case) to its latest secret end date and how many secrets it has."""


def _with_sources(rows: list[dict[str, Any]], sources: Sources | None) -> list[dict[str, Any]]:
    for row in rows:
        tag = row.get("source")
        if not isinstance(tag, str) or not secrets_meta.SOURCE_PATTERN.match(tag):
            row["source"] = None
            continue
        found = (sources or {}).get(tag.split(":", 1)[1].lower())
        if found is not None:
            row["source_expires"] = found[0].isoformat()
            row["source_count"] = found[1]
    return rows


@dataclass(frozen=True)
class App:
    app_id: str
    name: str
    ends: tuple[datetime, ...]

    @property
    def latest(self) -> datetime | None:
        return max(self.ends, default=None)


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        return secrets_meta._utc(parsed)
    except ValueError:
        return None


def read_apps(*, az_bin: str, runner: Runner = _run) -> list[App]:
    """Every Entra app this login can see with client secrets, and their end dates.

    :raises RelayError: When az fails or answers something that is not the list.
    """
    out = runner([az_bin, "ad", "app", "list", "--all", "--query", APPS_QUERY, "-o", "json"])
    apps: list[App] = []
    for row in _list(out, "apps"):
        if not isinstance(row, dict) or not isinstance(row.get("appId"), str):
            continue
        ends = row.get("ends") if isinstance(row.get("ends"), list) else []
        found = tuple(when for when in map(_when, ends) if when is not None)
        apps.append(App(row["appId"].lower(), str(row.get("displayName") or ""), found))
    return apps


def source_dates(apps: Sequence[App]) -> Sources:
    return {app.app_id: (app.latest, len(app.ends)) for app in apps if app.latest is not None}


def host_keys(path: Path) -> dict[str, bool] | None:
    """Whether the host's env file sets each host key, by name only; ``None`` if unreadable.

    Each line is split at its first ``=`` and only whether the right side is
    non-empty is kept; the value itself is dropped at once.
    """
    found = dict.fromkeys(secrets_meta.HOST_VAULT_NAMES, False)
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                key, sep, rest = line.strip().removeprefix("export ").partition("=")
                if sep and key.strip() in found:
                    found[key.strip()] = bool(rest.strip().strip("'\"").strip())
    except (OSError, UnicodeDecodeError):
        return None
    return found


# ── --propose-tags ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Proposal:
    vault: str
    name: str
    app: App
    why: str
    tags: dict[str, str]
    """The secret's tags today; --apply keeps them and adds ae-source."""

    @property
    def value(self) -> str:
        return f"entra:{self.app.app_id}"

    def argv(self, az_bin: str) -> list[str]:
        merged = {**self.tags, SOURCE_TAG: self.value}
        return [
            az_bin,
            "keyvault",
            "secret",
            "set-attributes",
            "--vault-name",
            self.vault,
            "--name",
            self.name,
            "--tags",
            *(f"{key}={value}" for key, value in merged.items()),
            "-o",
            "none",
        ]


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower())) - {"ae"}


def match_app(name: str, apps: Sequence[App]) -> tuple[App, str] | None:
    """The app a secret named *name* most likely belongs to, and why; ``None`` if unsure.

    A known pair wins. Otherwise the name less its ``-client-secret`` (or
    ``-sp-secret``, ``-secret``) must have every word in exactly one app's display
    name ("ae-auditor" and "AE Security Auditor"). The company word "ae" counts for
    nothing. Only that way round: "omnigent-oidc-cookie-secret" is not an app
    secret just because an app is called "AE OmniGent".
    """
    by_id = {app.app_id: app for app in apps}
    known = known_sources().get(name)
    if known and known in by_id:
        return by_id[known], "known pair"
    stem = next((name[: -len(end)] for end in SECRET_SUFFIXES if name.endswith(end)), None)
    words = _words(stem or "")
    if not words:
        return None
    hits = [app for app in apps if words <= _words(app.name)]
    if len(hits) != 1:
        return None
    return hits[0], f"name matches '{hits[0].name}'"


def propose_tags(
    vaults: dict[str, list[dict[str, Any]]], apps: Sequence[App]
) -> tuple[list[Proposal], list[str]]:
    """The tags to set, and a line for each secret already tagged."""
    proposals: list[Proposal] = []
    tagged: list[str] = []
    for vault, rows in vaults.items():
        for row in rows:
            name = row.get("name")
            if not isinstance(name, str):
                continue
            tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
            tags = {str(key): str(value) for key, value in tags.items()}
            if SOURCE_TAG in tags:
                tagged.append(f"{vault}/{name}: already {SOURCE_TAG}={tags[SOURCE_TAG]}")
                continue
            found = match_app(name, apps)
            if found is not None:
                proposals.append(Proposal(vault, name, found[0], found[1], tags))
    return proposals, tagged


def _day(when: datetime | None) -> str:
    return when.date().isoformat() if when else "no end date"


def run_propose(
    *,
    vaults: Sequence[str],
    az_bin: str | None,
    apply: bool,
    runner: Runner = _run,
    out: Any = None,
) -> int:
    """Print (and with *apply*, set) the ae-source tags; the exit status."""
    out = out or sys.stdout
    if az_bin is None:
        print("secrets: no az on PATH.", file=out)
        return 1
    try:
        apps = read_apps(az_bin=az_bin, runner=runner)
        listed = {
            vault: _list(
                runner(
                    [
                        az_bin,
                        "keyvault",
                        "secret",
                        "list",
                        "--vault-name",
                        vault,
                        "--query",
                        TAGS_QUERY,
                        "-o",
                        "json",
                    ]
                ),
                "secrets",
            )
            for vault in vaults
        }
    except RelayError as exc:
        print(f"secrets: {exc}", file=out)
        return 1
    proposals, tagged = propose_tags(listed, apps)
    print(
        f"# {len(apps)} Entra apps with client secrets; {len(proposals)} tags proposed", file=out
    )
    for line in tagged:
        print(f"# {line}", file=out)
    for item in proposals:
        print(
            f"# {item.vault}/{item.name} -> {item.app.name} ({item.app.app_id}), "
            f"ends {_day(item.app.latest)}, {item.why}",
            file=out,
        )
        print(" ".join(item.argv("az")), file=out)
    if not apply:
        if proposals:
            print("# dry run: nothing set. Run again with --apply to set these tags.", file=out)
        return 0
    failed = 0
    for item in proposals:
        try:
            runner(item.argv(az_bin))
        except RelayError as exc:
            failed += 1
            print(f"secrets: tagging {item.vault}/{item.name} failed: {exc}", file=out)
    print(f"# set {len(proposals) - failed} of {len(proposals)} tags", file=out)
    return 1 if failed else 0


async def post(client: httpx.AsyncClient, body: secrets_meta.MetaPost) -> int:
    """``POST /v1/ae/secrets/meta``; how many secrets the server now holds, every vault
    (carried-forward parts included).

    :raises RelayError: On a transport failure or a non-2xx answer.
    """
    path = "/v1/ae/secrets/meta"
    try:
        response = await client.post(path, json=body.model_dump(mode="json"))
    except httpx.HTTPError as exc:
        raise RelayError(f"POST {path} failed: {exc}") from exc
    if response.status_code == 401:
        raise RelayError(
            f"POST {path} answered 401: the stored login expired; run `omnigent login "
            f"{client.base_url}` as this user"
        )
    if response.status_code >= 300:
        raise RelayError(f"POST {path} answered {response.status_code}: {response.text[:200]}")
    return int(response.json().get("kept", 0))


def _gather(
    vault: str,
    extra_vaults: Sequence[str],
    host_env: Path | None,
    *,
    az_bin: str,
    runner: Runner,
    out: Any,
) -> tuple[secrets_meta.MetaPost, bool]:
    """The post, and whether a part of it could not be read (said on *out*).

    The az calls run side by side: each takes seconds, and the timer has a limit.
    A failed part is named in the post's ``failed``, so the server keeps its last
    good reading of it.
    """
    extras = [name for name in dict.fromkeys(extra_vaults) if name != vault]
    with ThreadPoolExecutor(max_workers=2 + len(extras)) as pool:
        apps_job = pool.submit(read_apps, az_bin=az_bin, runner=runner)
        box_job = pool.submit(list_secrets, vault, az_bin=az_bin, runner=runner)
        extra_jobs = {
            name: pool.submit(list_secrets, name, az_bin=az_bin, runner=runner) for name in extras
        }
    failed: list[str] = []
    try:
        sources = source_dates(apps_job.result())
    except RelayError as exc:
        # Source dates are a bonus: the vault dates still go out.
        print(f"secrets: no Entra end dates ({exc}); the server keeps its last ones", file=out)
        sources = {}
        failed.append("entra")
    body = read_vault(vault, az_bin=az_bin, runner=runner, sources=sources, rows=box_job.result())
    for name, job in extra_jobs.items():
        try:
            body.vaults.append(
                read_extra(name, az_bin=az_bin, runner=runner, sources=sources, rows=job.result())
            )
        except RelayError as exc:
            print(f"secrets: vault '{name}' left out ({exc}); the server keeps its last", file=out)
            failed.append(f"vault:{name}")
    if host_env is not None:
        body.host = host_keys(host_env)
        if body.host is None:
            print(
                f"secrets: could not read {host_env}; the server keeps its last host reading",
                file=out,
            )
            failed.append("host")
    body.failed = failed
    return body, bool(failed)


async def run(
    *,
    vault: str,
    az_bin: str | None,
    settings: omni.Settings,
    dry_run: bool = False,
    runner: Runner = _run,
    transport: httpx.AsyncBaseTransport | None = None,
    out: Any = None,
    extra_vaults: Sequence[str] = (),
    host_env: Path | None = None,
) -> int:
    """Read, then print (dry run) or post; the exit status.

    A part that could not be read (the Entra dates, an extra vault, the host's
    file) is said and left out, the rest is posted, and the exit status is 1 so
    the timer shows the run as failed.
    """
    out = out or sys.stdout
    if az_bin is None:
        print(
            "secrets: no az on PATH. Without sudo: uv tool install azure-cli; "
            "then az login --use-device-code.",
            file=out,
        )
        return 1
    try:
        body, partial = await asyncio.to_thread(
            _gather, vault, extra_vaults, host_env, az_bin=az_bin, runner=runner, out=out
        )
    except RelayError as exc:
        print(f"secrets: {exc}", file=out)
        return 1
    if dry_run:
        print(json.dumps(body.model_dump(mode="json"), indent=2), file=out)
        return 1 if partial else 0
    if not settings.configured:
        print(settings.problem, file=out)
        return 1
    async with omni.http_client(settings, transport=transport) as client:
        try:
            kept = await post(client, body)
        except RelayError as exc:
            print(f"secrets: {exc}", file=out)
            return 1
    sent = len(body.secrets) + sum(len(extra.secrets) for extra in body.vaults)
    vaults = ", ".join([vault, *(extra.vault for extra in body.vaults)])
    print(
        f"secrets: posted {sent} from {vaults}; the server now holds {kept} across all vaults",
        file=out,
    )
    return 1 if partial else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ae_omni_policies.secrets_relay",
        description="Post the vault's secret expiry dates (no values) to the OmniGent server.",
    )
    parser.add_argument(
        "--vault",
        default=None,
        help=f"the Key Vault (default: ${VAULT_ENV}, required one way or the other)",
    )
    parser.add_argument(
        "--az-bin",
        default=None,
        help=f"the az executable (default: ${AZ_BIN_ENV}, else az on PATH)",
    )
    parser.add_argument(
        "--extra-vault",
        action="append",
        default=None,
        help=f"another vault to list for the Key Vault card, repeatable (default: "
        f"${EXTRA_VAULTS_ENV}, comma-separated, else none)",
    )
    parser.add_argument(
        "--host-env",
        default=None,
        help=f"the host's env file, checked by key name only (default: ${HOST_ENV_FILE_ENV}, "
        f"else {HOST_ENV_FILE})",
    )
    parser.add_argument("--dry-run", action="store_true", help="print, do not post")
    parser.add_argument(
        "--propose-tags",
        action="store_true",
        help=f"print the {SOURCE_TAG} tags that would map vault secrets to Entra apps",
    )
    parser.add_argument("--apply", action="store_true", help="with --propose-tags: set them")
    args = parser.parse_args(argv)
    vault = args.vault or secrets_meta.box_vault()
    az_bin = args.az_bin or os.environ.get(AZ_BIN_ENV) or shutil.which("az")
    if args.extra_vault is not None:
        extra = [name for name in args.extra_vault if name]
    else:
        raw = os.environ.get(EXTRA_VAULTS_ENV)
        extra = [name.strip() for name in (raw or "").split(",") if name.strip()]
    if args.apply and not args.propose_tags:
        parser.error("--apply goes with --propose-tags")
    if not vault:
        # Fail the unit, not just the log: a card that goes stale silently is the failure this
        # catches. The tag proposal does not need the box name, so the extra vaults still get it;
        # a posted reading is keyed by the box vault, so that run has nothing it can send.
        print(
            f"secrets_relay: {VAULT_ENV} is not set; the box vault was not read", file=sys.stderr
        )
        if args.propose_tags and extra:
            run_propose(vaults=extra, az_bin=az_bin, apply=args.apply)
        return 1
    if args.propose_tags:
        return run_propose(vaults=[vault, *extra], az_bin=az_bin, apply=args.apply)
    host_env = Path(args.host_env or os.environ.get(HOST_ENV_FILE_ENV) or HOST_ENV_FILE)
    return asyncio.run(
        run(
            vault=vault,
            az_bin=az_bin,
            settings=omni.Settings.from_env(),
            dry_run=args.dry_run,
            extra_vaults=extra,
            host_env=host_env,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
