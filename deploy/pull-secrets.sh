#!/usr/bin/env bash
# Pull the OmniGent deployment's secrets from Azure Key Vault into the two
# environment files on the encrypted volume, and nothing else. Phase 6,
# 2026-09-23.
#
#   deploy/pull-secrets.sh          # fetch and (re)write both files
#   deploy/pull-secrets.sh --check  # report which keys each file holds, no values
#
# Writes, 640 root:reid, atomically (a temp file in the same directory, then
# mv), so a reader never sees a half-written file and a failed run leaves the
# previous one in place:
#
#   /secure/env/omnigent-ae.env    POSTGRES_PASSWORD, AE_CF_ACCESS_CLIENT_ID,
#                                  AE_CF_ACCESS_CLIENT_SECRET, OMNIGENT_OIDC_CLIENT_SECRET,
#                                  OMNIGENT_OIDC_COOKIE_SECRET, optional: AE_GITHUB_TOKEN,
#                                  AE_FABRIC_WAKE_URL_AETRAINING, AE_FABRIC_SUSPEND_KEY_AETRAINING,
#                                  AE_FABRIC_CAPACITIES_URL, AE_FABRIC_CAPACITIES_KEY, AE_DATABRICKS_CLIENT_ID,
#                                  AE_DATABRICKS_CLIENT_SECRET, AE_SHARED_ACCOUNTS_KEY  (the server)
#   /secure/env/omnigent-host.env  CLAUDE_CODE_OAUTH_TOKEN      (the host)
#
# The non-secret settings live in config/server.env and config/host.env (from
# the .example files), never here. The OIDC pair (2026-10-14,
# deploy/README.md, "Adding an OIDC provider"): rotating the cookie key voids every login grant.
#
# Run it as reid, where `az login` lives: the fetch happens unprivileged, in a
# private temp directory, and only the placement into /secure/env goes through
# sudo (install, then mv). Root never runs `az`; secrets never pass through
# argv or the log. The box has no `az` of its own (secure-volume/README.md):
# `uv tool install azure-cli` puts one in ~/.local/bin without sudo, then
# `az login --use-device-code`.
#
# The Key Vault names below were checked against ae-secrets-vault on
# 2026-09-24: the Cloudflare Access service token for task-store lives under
# cf-access-svc-taskstore-*, and claude-code-oauth-token exists. A wrong name
# fails loudly at the fetch, naming it; fix the variable here, not the vault.
# The Postgres password is new with this deployment and has to be created
# first:
#   az keyvault secret set --vault-name ae-secrets-vault \
#     --name omnigent-ae-postgres-password --value "$(openssl rand -hex 24)"
set -euo pipefail

# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../scripts/_lib.sh"

VAULT="${AE_VAULT_NAME:-ae-secrets-vault}"
KV_POSTGRES_PASSWORD="${KV_POSTGRES_PASSWORD:-omnigent-ae-postgres-password}"
KV_CF_ACCESS_CLIENT_ID="${KV_CF_ACCESS_CLIENT_ID:-cf-access-svc-taskstore-client-id}"
KV_CF_ACCESS_CLIENT_SECRET="${KV_CF_ACCESS_CLIENT_SECRET:-cf-access-svc-taskstore-client-secret}"
KV_OIDC_CLIENT_SECRET="${KV_OIDC_CLIENT_SECRET:-omnigent-oidc-client-secret}"
KV_OIDC_COOKIE_SECRET="${KV_OIDC_COOKIE_SECRET:-omnigent-oidc-cookie-secret}"
KV_CLAUDE_CODE_OAUTH_TOKEN="${KV_CLAUDE_CODE_OAUTH_TOKEN:-claude-code-oauth-token}"
# Optional (2026-09-24, the Your move page's pull request section; since the
# PR cards' Merge button the same day, read and write): a fine-grained token
# on the repos the sessions open pull requests in, scopes in deploy/README.md.
# Absent from the vault, the run carries on without
# it and says so, and the page says how to add it (deploy/README.md, "The
# GitHub token").
KV_GITHUB_TOKEN="${KV_GITHUB_TOKEN:-omnigent-ae-github-token}"
# Optional (2026-10-16, the Admin Center's Fabric section): the aetraining capacity's
# Logic App wake URL (the shared wake link; its query carries the trigger's
# signature) and the suspend key that turns on Off for it. Absent, the page
# says how to add them; the wake URL alone gives Wake without Off. Another
# capacity is two more lines like these, AE_FABRIC_WAKE_URL_<NAME> and
# AE_FABRIC_SUSPEND_KEY_<NAME> (packages/ae_omni_policies/src/ae_omni_policies/fabric.py).
KV_FABRIC_WAKE_URL_AETRAINING="${KV_FABRIC_WAKE_URL_AETRAINING:-aetraining-wake-url}"
KV_FABRIC_SUSPEND_KEY_AETRAINING="${KV_FABRIC_SUSPEND_KEY_AETRAINING:-aetraining-suspend-key}"
# Optional (2026-10-21, the capacity fleet): the shared la-fabric-capacities Logic
# App's trigger URL and the key it wants in its x-capacity-key header. With both,
# the Admin Center lists every Fabric capacity and can pause, start and resize
# each; without them it shows only the AE_FABRIC_WAKE_URL_<NAME> ones, as before.
KV_FABRIC_CAPACITIES_URL="${KV_FABRIC_CAPACITIES_URL:-fabric-capacities-url}"
KV_FABRIC_CAPACITIES_KEY="${KV_FABRIC_CAPACITIES_KEY:-fabric-capacities-key}"
# Optional (2026-10-19, the Admin Center's Databricks section): the ae-admin-center
# service principal's OAuth client ID and secret on the AWS workspace. Both are
# needed; absent, the section says how to add them.
KV_DATABRICKS_CLIENT_ID="${KV_DATABRICKS_CLIENT_ID:-databricks-mvp-client-id}"
KV_DATABRICKS_CLIENT_SECRET="${KV_DATABRICKS_CLIENT_SECRET:-databricks-mvp-client-secret}"
# Optional (2026-10-20, shared Claude accounts): the Fernet key that encrypts the
# shared accounts' tokens at rest (deploy/README.md, "Shared Claude accounts").
# Absent, the Admin Center's section says so and no shared account can be added or used.
KV_SHARED_ACCOUNTS_KEY="${KV_SHARED_ACCOUNTS_KEY:-omnigent-ae-shared-accounts-key}"

ENV_DIR="${AE_SECURE_ENV_DIR:-/secure/env}"
SERVER_ENV="$ENV_DIR/omnigent-ae.env"
HOST_ENV="$ENV_DIR/omnigent-host.env"
OWNER="${AE_ENV_OWNER:-root}"
GROUP="${AE_ENV_GROUP:-reid}"
MODE=640

# The OIDC pair is required: leaving it out on a vault error would fail the
# next restart in OIDC mode instead of this run.
SERVER_KEYS=(POSTGRES_PASSWORD AE_CF_ACCESS_CLIENT_ID AE_CF_ACCESS_CLIENT_SECRET OMNIGENT_OIDC_CLIENT_SECRET OMNIGENT_OIDC_COOKIE_SECRET)
HOST_KEYS=(CLAUDE_CODE_OAUTH_TOKEN)
# The Admin Center's Secrets section lists both server arrays from its own copy
# (packages/ae_omni_policies/src/ae_omni_policies/secrets_health.py, with what
# each key turns on); a test fails when the two differ, so a new key goes there too.
# Written when the vault has them, reported but never required by --check.
SERVER_OPTIONAL_KEYS=(AE_GITHUB_TOKEN AE_FABRIC_WAKE_URL_AETRAINING AE_FABRIC_SUSPEND_KEY_AETRAINING AE_FABRIC_CAPACITIES_URL AE_FABRIC_CAPACITIES_KEY AE_DATABRICKS_CLIENT_ID AE_DATABRICKS_CLIENT_SECRET AE_SHARED_ACCOUNTS_KEY)

usage() {
  # The header comment, up to the first blank-free `set` line.
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

# Presence per key, never a value: `grep -c` on the key prefix. Reading the
# files may need sudo; as root or as a member of the group it does not.
# Exit 1 when anything is missing or has the wrong mode or owner.
check() {
  local rc=0 f k
  for f in "$SERVER_ENV" "$HOST_ENV"; do
    if [ ! -e "$f" ]; then
      printf 'MISSING  %s\n' "$f"
      rc=1
      continue
    fi
    local got_mode got_owner
    got_mode="$(stat -c '%a' "$f")"
    got_owner="$(stat -c '%U:%G' "$f")"
    if [ "$got_mode" != "$MODE" ] || [ "$got_owner" != "$OWNER:$GROUP" ]; then
      printf 'WRONG    %s is %s %s, want %s %s:%s\n' "$f" "$got_mode" "$got_owner" "$MODE" "$OWNER" "$GROUP"
      rc=1
    else
      printf 'ok       %s (%s %s)\n' "$f" "$got_mode" "$got_owner"
    fi
    local keys=("${SERVER_KEYS[@]}")
    [ "$f" = "$HOST_ENV" ] && keys=("${HOST_KEYS[@]}")
    if [ ! -r "$f" ]; then
      printf '  ?      cannot read %s as %s; rerun as root or a member of %s\n' "$f" "$(id -un)" "$GROUP"
      rc=1
      continue
    fi
    for k in "${keys[@]}"; do
      if grep -qs "^${k}=." "$f"; then
        printf '  ok     %s\n' "$k"
      else
        printf '  MISSING %s\n' "$k"
        rc=1
      fi
    done
    if [ "$f" = "$SERVER_ENV" ]; then
      for k in "${SERVER_OPTIONAL_KEYS[@]}"; do
        if grep -qs "^${k}=." "$f"; then
          printf '  ok     %s (optional)\n' "$k"
        else
          printf '  absent %s (optional)\n' "$k"
        fi
      done
    fi
  done
  return "$rc"
}

# Fetch one secret into the named variable. The value goes into a shell
# variable and from there straight into the temp file; it is never an argv
# element of any command and never echoed.
fetch() {
  local var="$1" name="$2" value
  if ! value="$(az keyvault secret show --vault-name "$VAULT" --name "$name" --query value -o tsv 2>/dev/null)"; then
    die "could not read secret '$name' from vault '$VAULT'. Check the name (the KV_* variables at the top of this script), that the secret exists (az keyvault secret list --vault-name $VAULT --query '[].name' -o tsv) and that this login may read it."
  fi
  [ -n "$value" ] || die "secret '$name' in vault '$VAULT' is empty; refusing to write an empty value"
  case "$value" in
    *$'\n'*|*$'\r'*) die "secret '$name' contains a line break; an env file cannot carry it" ;;
  esac
  printf -v "$var" '%s' "$value"
}

# Like fetch, for a secret the deployment runs without: a missing or empty
# secret leaves the variable empty and says which feature stays off. Any
# other failure is the same as fetch's (a bad login fails fetch first).
fetch_optional() {
  local var="$1" name="$2" what="$3" value
  printf -v "$var" '%s' ""
  if ! value="$(az keyvault secret show --vault-name "$VAULT" --name "$name" --query value -o tsv 2>/dev/null)" || [ -z "$value" ]; then
    note "optional secret '$name' is not in vault '$VAULT'; $what stays off"
    return 0
  fi
  case "$value" in
    *$'\n'*|*$'\r'*) die "secret '$name' contains a line break; an env file cannot carry it" ;;
  esac
  # Compose interpolates $ in the env file, so a Fabric or Databricks value
  # holding one would reach the server altered (2026-10-16). Refuse it by name;
  # the value is never printed.
  case "$var:$value" in
    AE_FABRIC_*:*'$'*|AE_DATABRICKS_*:*'$'*) die "secret '$name' contains a '\$'; Compose interpolates the env file and would change it. Store a value without one (a Logic App URL has none; use a hex suspend key), then rerun." ;;
  esac
  printf -v "$var" '%s' "$value"
}

# Put a temp file into place as $OWNER:$GROUP $MODE, atomically. install copies
# to a temp name in the destination directory with the final ownership and
# mode; mv renames it over the old file in one step. sudo only when needed.
place() {
  local src="$1" dst="$2"
  local as_root=()
  [ "$(id -u)" -eq 0 ] || as_root=(sudo)
  "${as_root[@]}" install -m "$MODE" -o "$OWNER" -g "$GROUP" "$src" "$dst.tmp"
  "${as_root[@]}" mv -f "$dst.tmp" "$dst"
}

main() {
  case "${1:-}" in
    -h|--help) usage; exit 0 ;;
    --check) check; exit $? ;;
    "") ;;
    *) die "unknown argument '$1' (try --help)" ;;
  esac

  command -v az >/dev/null 2>&1 || die "az is not on PATH. Without sudo: uv tool install azure-cli; then az login --use-device-code."
  az account show >/dev/null 2>&1 || die "az is not logged in. Run: az login --use-device-code (a browser on any device finishes it), then rerun."
  [ -d "$ENV_DIR" ] || die "$ENV_DIR does not exist. If /secure is not mounted, the TPM unlock failed: see home-server/secure-volume/README.md in agents-and-automation."
  getent group "$GROUP" >/dev/null || die "group '$GROUP' does not exist on this machine (AE_ENV_GROUP overrides it)"

  # Script-global on purpose: the EXIT trap runs after main returns, when a
  # `local` would already be gone. With a local, the trap aborted on an unset
  # variable and left the plaintext copy in $TMPDIR (seen on the box,
  # 2026-09-24).
  AE_PULL_TMP="$(umask 077 && mktemp -d)"
  trap 'rm -rf -- "${AE_PULL_TMP:-/nonexistent}"' EXIT
  local tmp="$AE_PULL_TMP"

  local POSTGRES_PASSWORD AE_CF_ACCESS_CLIENT_ID AE_CF_ACCESS_CLIENT_SECRET CLAUDE_CODE_OAUTH_TOKEN
  local AE_GITHUB_TOKEN OMNIGENT_OIDC_CLIENT_SECRET OMNIGENT_OIDC_COOKIE_SECRET
  local AE_FABRIC_WAKE_URL_AETRAINING AE_FABRIC_SUSPEND_KEY_AETRAINING
  local AE_FABRIC_CAPACITIES_URL AE_FABRIC_CAPACITIES_KEY
  local AE_DATABRICKS_CLIENT_ID AE_DATABRICKS_CLIENT_SECRET AE_SHARED_ACCOUNTS_KEY
  note "reading ${#SERVER_KEYS[@]} + ${#HOST_KEYS[@]} secrets from vault '$VAULT'"
  fetch POSTGRES_PASSWORD "$KV_POSTGRES_PASSWORD"
  fetch AE_CF_ACCESS_CLIENT_ID "$KV_CF_ACCESS_CLIENT_ID"
  fetch AE_CF_ACCESS_CLIENT_SECRET "$KV_CF_ACCESS_CLIENT_SECRET"
  fetch OMNIGENT_OIDC_CLIENT_SECRET "$KV_OIDC_CLIENT_SECRET"
  fetch OMNIGENT_OIDC_COOKIE_SECRET "$KV_OIDC_COOKIE_SECRET"
  fetch CLAUDE_CODE_OAUTH_TOKEN "$KV_CLAUDE_CODE_OAUTH_TOKEN"
  fetch_optional AE_GITHUB_TOKEN "$KV_GITHUB_TOKEN" "the Your move page's pull request section"
  fetch_optional AE_FABRIC_WAKE_URL_AETRAINING "$KV_FABRIC_WAKE_URL_AETRAINING" "the Admin Center's aetraining card"
  fetch_optional AE_FABRIC_SUSPEND_KEY_AETRAINING "$KV_FABRIC_SUSPEND_KEY_AETRAINING" "Off on the Admin Center's aetraining card"
  fetch_optional AE_FABRIC_CAPACITIES_URL "$KV_FABRIC_CAPACITIES_URL" "the Admin Center's full capacity list and resize"
  fetch_optional AE_FABRIC_CAPACITIES_KEY "$KV_FABRIC_CAPACITIES_KEY" "the Admin Center's full capacity list and resize"
  fetch_optional AE_DATABRICKS_CLIENT_ID "$KV_DATABRICKS_CLIENT_ID" "the Admin Center's Databricks section"
  fetch_optional AE_DATABRICKS_CLIENT_SECRET "$KV_DATABRICKS_CLIENT_SECRET" "the Admin Center's Databricks section"
  fetch_optional AE_SHARED_ACCOUNTS_KEY "$KV_SHARED_ACCOUNTS_KEY" "shared Claude accounts"

  local stamp
  stamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  {
    printf '# Written by deploy/pull-secrets.sh (omnigent-ae) at %s. Do not edit; rerun the script.\n' "$stamp"
    printf '# Read by deploy/compose.sh (compose interpolation and the omnigent service env_file).\n'
    printf '%s=%s\n' POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
    printf '%s=%s\n' AE_CF_ACCESS_CLIENT_ID "$AE_CF_ACCESS_CLIENT_ID"
    printf '%s=%s\n' AE_CF_ACCESS_CLIENT_SECRET "$AE_CF_ACCESS_CLIENT_SECRET"
    printf '%s=%s\n' OMNIGENT_OIDC_CLIENT_SECRET "$OMNIGENT_OIDC_CLIENT_SECRET"
    printf '%s=%s\n' OMNIGENT_OIDC_COOKIE_SECRET "$OMNIGENT_OIDC_COOKIE_SECRET"
    if [ -n "$AE_GITHUB_TOKEN" ]; then
      printf '%s=%s\n' AE_GITHUB_TOKEN "$AE_GITHUB_TOKEN"
    fi
    if [ -n "$AE_FABRIC_WAKE_URL_AETRAINING" ]; then
      printf '%s=%s\n' AE_FABRIC_WAKE_URL_AETRAINING "$AE_FABRIC_WAKE_URL_AETRAINING"
    fi
    if [ -n "$AE_FABRIC_SUSPEND_KEY_AETRAINING" ]; then
      printf '%s=%s\n' AE_FABRIC_SUSPEND_KEY_AETRAINING "$AE_FABRIC_SUSPEND_KEY_AETRAINING"
    fi
    if [ -n "$AE_FABRIC_CAPACITIES_URL" ]; then
      printf '%s=%s\n' AE_FABRIC_CAPACITIES_URL "$AE_FABRIC_CAPACITIES_URL"
    fi
    if [ -n "$AE_FABRIC_CAPACITIES_KEY" ]; then
      printf '%s=%s\n' AE_FABRIC_CAPACITIES_KEY "$AE_FABRIC_CAPACITIES_KEY"
    fi
    if [ -n "$AE_DATABRICKS_CLIENT_ID" ]; then
      printf '%s=%s\n' AE_DATABRICKS_CLIENT_ID "$AE_DATABRICKS_CLIENT_ID"
    fi
    if [ -n "$AE_DATABRICKS_CLIENT_SECRET" ]; then
      printf '%s=%s\n' AE_DATABRICKS_CLIENT_SECRET "$AE_DATABRICKS_CLIENT_SECRET"
    fi
    if [ -n "$AE_SHARED_ACCOUNTS_KEY" ]; then
      printf '%s=%s\n' AE_SHARED_ACCOUNTS_KEY "$AE_SHARED_ACCOUNTS_KEY"
    fi
  } > "$tmp/server.env"
  {
    printf '# Written by deploy/pull-secrets.sh (omnigent-ae) at %s. Do not edit; rerun the script.\n' "$stamp"
    printf '# Read by the ae-omnigent-host user unit (EnvironmentFile).\n'
    printf '%s=%s\n' CLAUDE_CODE_OAUTH_TOKEN "$CLAUDE_CODE_OAUTH_TOKEN"
  } > "$tmp/host.env"

  note "placing $SERVER_ENV and $HOST_ENV ($MODE $OWNER:$GROUP; sudo may prompt)"
  place "$tmp/server.env" "$SERVER_ENV"
  place "$tmp/host.env" "$HOST_ENV"
  note "done. Verify with: deploy/pull-secrets.sh --check"
}

main "$@"
