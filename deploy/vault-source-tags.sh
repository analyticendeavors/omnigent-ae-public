#!/usr/bin/env bash
# Propose (and with --apply, set) the ae-source tags that tie Key Vault secrets to
# the Entra apps that issue them, so the Admin Center can show each app secret's
# real end date. 2026-10-23, the Key Vault card.
#
#   deploy/vault-source-tags.sh           # print the tags it would set, set nothing
#   deploy/vault-source-tags.sh --apply   # set them
#
# Why: only 2 of 103 vault secrets carry an expiry, but every Entra client secret
# has a real end date on its app. The daily relay (secrets_relay.py) reads those
# dates with `az ad app list` and puts them on each vault secret tagged
# `ae-source=entra:<appId>`. Tags come back from `az keyvault secret list`, so the
# relay still never reads a value.
#
# Matching: a known pair (azure-cost-client-secret is ae-omnigent-azure-cost), or
# every word of the name less its -client-secret ending found in exactly one
# app's display name (ae-auditor-client-secret and "AE Security Auditor"). A
# secret with no sure match is left out; add it to KNOWN_SOURCES in
# secrets_relay.py once its app is known. A
# secret already tagged is left alone. Each proposal prints as a comment (vault,
# secret, app, end date, why) and the az command that sets it; --apply runs those
# commands, keeping the secret's other tags.
#
# Tagging is not a change: the Admin Center's "changed" date and its 180-day count
# use when the secret's current version was created, which a tag never touches
# (set-attributes does bump `updated`, 2026-10-23).
#
# Run as reid, where `az login` lives. Lists the box vault and every vault in
# AE_EXTRA_VAULTS, as the relay does. The script reads both from the
# config/host.env next to it, unless they are already exported; that file is
# the relay unit's only when the script runs from the deployed tree
# (~/deploy/omnigent-ae). From a git checkout it says which file it used, or
# that none was found. Reads names, tags and app end dates only: never
# `secret show`, never a credential's hint.
#
# The logic is the relay's (`python -m ae_omni_policies.secrets_relay
# --propose-tags`), run from this checkout's package source so it works before
# the host venv picks up a release. PYTHON overrides the interpreter (default:
# the host venv, which has httpx and pydantic).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
src="$here/../packages/ae_omni_policies/src"
python="${PYTHON:-$HOME/.omnigent-host/venv/bin/python}"

# Load config/host.env the way systemd reads an EnvironmentFile (plain KEY=VALUE
# lines), without overriding anything already exported. A missing file is fine.
env_file="$here/../config/host.env"
if [ -r "$env_file" ]; then
  echo "vault-source-tags: reading $env_file" >&2
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      [A-Za-z_]*=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      *[!A-Za-z0-9_]*) continue ;;
    esac
    case "$value" in
      \"*\") value="${value#\"}" value="${value%\"}" ;;
      \'*\') value="${value#\'}" value="${value%\'}" ;;
    esac
    if [ -z "${!key+x}" ]; then
      export "$key=$value"
    fi
  done <"$env_file"
else
  echo "vault-source-tags: no $env_file; only exported AE_VAULT_NAME and AE_EXTRA_VAULTS apply" >&2
fi

case "${1:-}" in
  "" | --apply) ;;
  -h | --help)
    sed -n '2,7p' "$0"
    exit 0
    ;;
  *)
    echo "vault-source-tags: unknown option '$1'; use --apply or nothing" >&2
    exit 2
    ;;
esac

if [ ! -x "$python" ]; then
  echo "vault-source-tags: no python at $python; set PYTHON to one with httpx and pydantic" >&2
  exit 1
fi

PYTHONPATH="$src${PYTHONPATH:+:$PYTHONPATH}" exec "$python" -m ae_omni_policies.secrets_relay --propose-tags "$@"
