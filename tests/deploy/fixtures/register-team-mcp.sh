#!/usr/bin/env bash
# Registers the shared MCP servers listed in team-mcp.json at user scope, using
# only `claude mcp` (never a hand edit of ~/.claude.json).
#
# Usage: register-team-mcp.sh [--owner] [--dry-run] [-h]
#   --owner    register the servers without a team restriction: the variables
#              in each entry's "owner_env_drop" are left out (freshbooks then
#              uses the owner's own defaults: both accounts, the shared token).
#   --dry-run  print what would change and the commands; change nothing.
#
# Safe to re-run. Each server is read back with `claude mcp get` first and left
# alone when its command, args, env or URL already match. A changed server is
# removed and added again, and the old definition is put back if the add fails,
# so a server is never lost. A sign-in ("auth": "interactive") server is never
# removed unless its URL changed, because removing it wipes the user's sign-in.
#
# Credentials are never inline. Servers read their secrets from Key Vault at
# startup through the signed-in user's own `az login`.
#
# team-mcp.json is looked for next to this script, then one folder up, then in
# ../claude-config/ (the repo layout). A missing, unparsable or empty file, or
# an entry missing a required field, exits non-zero.
# A same-named server in local or project scope is not ours to replace: it is
# reported with a warning and skipped (exit 0), so a sync does not fail on it.
# Limit: the restore after a failed add splits the old args on spaces, so a HOME
# path that contains spaces is not supported.
# Needs: bash 4.4+, jq, and the claude CLI (not needed for --dry-run).
set -euo pipefail

die() { echo "register-team-mcp: $*" >&2; exit 1; }

if ! { [ "${BASH_VERSINFO[0]}" -gt 4 ] || { [ "${BASH_VERSINFO[0]}" -eq 4 ] && [ "${BASH_VERSINFO[1]}" -ge 4 ]; }; }; then
  die "bash 4.4 or newer is required (this is ${BASH_VERSION})"
fi

OWNER=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --owner) OWNER=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "register-team-mcp: unknown option '$arg'" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG=""
for candidate in "$HERE/team-mcp.json" "$HERE/../team-mcp.json" "$HERE/../claude-config/team-mcp.json"; do
  if [ -f "$candidate" ]; then CONFIG="$candidate"; break; fi
done
[ -n "$CONFIG" ] || die "team-mcp.json not found near $HERE"
command -v jq >/dev/null 2>&1 || die "jq is required but not installed"
HAVE_CLAUDE=1
command -v claude >/dev/null 2>&1 || HAVE_CLAUDE=0
if [ "$DRY" -eq 0 ] && [ "$HAVE_CLAUDE" -eq 0 ]; then
  die "the 'claude' CLI is not on PATH; install Claude Code first"
fi

# Validate before touching anything. jq runs directly (not inside a process
# substitution) so a failure here stops the script under set -e.
jq -e . "$CONFIG" >/dev/null 2>&1 || die "$CONFIG is not valid JSON"
jq -e '
  (.servers | type == "array" and length > 0) and
  all(.servers[];
    (.name | type == "string" and test("^[A-Za-z0-9_.-]+$")) and
    (.transport == "stdio" or .transport == "http") and
    ((.auth // "none") | . == "none" or . == "interactive") and
    (if .transport == "stdio"
       then (.command | type == "string" and length > 0)
       else (.url | type == "string" and startswith("https://")) end))
' "$CONFIG" >/dev/null 2>&1 \
  || die "$CONFIG needs a non-empty \"servers\" list; each entry needs name, transport (stdio|http), command (stdio) or an https url (http), and auth none|interactive"

# One JSON object per line, {{HOME}} filled in, owner-dropped env removed.
ROWS_JSON=$(jq -c --arg home "$HOME" --argjson owner "$OWNER" '
  .servers[]
  | (if $owner == 1 then (.owner_env_drop // []) else [] end) as $drop
  | .env = ((.env // {}) | with_entries(select(.key as $k | $drop | index($k) | not)))
  | walk(if type == "string" then gsub("\\{\\{HOME\\}\\}"; $home) else . end)
' "$CONFIG")
mapfile -t ROWS <<<"$ROWS_JSON"

# Read a server back from `claude mcp get` into globals: CUR_FOUND (0/1),
# CUR_USER (1 when it lives in user scope), CUR_TYPE, CUR_COMMAND, CUR_ARGS
# (space-joined), CUR_URL and CUR_ENV (KEY=VALUE lines, sorted with LC_ALL=C so
# a locale cannot make two equal sets look different).
read_current() {
  local out
  CUR_FOUND=0 CUR_USER=0 CUR_SCOPE="" CUR_TYPE="" CUR_COMMAND="" CUR_ARGS="" CUR_URL="" CUR_ENV=""
  [ "$HAVE_CLAUDE" -eq 1 ] || return 0
  out=$(claude mcp get "$1" 2>/dev/null) || return 0
  CUR_FOUND=1
  CUR_SCOPE=$(sed -n 's/^  Scope: //p' <<<"$out" | head -n1)
  CUR_TYPE=$(sed -n 's/^  Type: //p' <<<"$out" | head -n1)
  CUR_COMMAND=$(sed -n 's/^  Command: //p' <<<"$out" | head -n1)
  CUR_ARGS=$(sed -n 's/^  Args: //p' <<<"$out" | head -n1)
  CUR_URL=$(sed -n 's/^  URL: //p' <<<"$out" | head -n1)
  CUR_ENV=$(awk '/^  Environment:/ { on = 1; next } on && /^    / { sub(/^    /, ""); print; next } { on = 0 }' <<<"$out" | LC_ALL=C sort)
  if grep -q '^  Scope: User config' <<<"$out"; then CUR_USER=1; fi
}

# Build the `claude mcp add` argv for one row into the array ADD, under name $4.
build_add() {
  local row=$1 transport=$3 as=$4
  local -a envargs cmdargs
  if [ "$transport" = "stdio" ]; then
    mapfile -t envargs < <(jq -r '.env | to_entries[] | "--env\n\(.key)=\(.value)"' <<<"$row")
    mapfile -t cmdargs < <(jq -r '[.command] + (.args // []) | .[]' <<<"$row")
    ADD=(claude mcp add --scope user --transport stdio "$as" "${envargs[@]}" -- "${cmdargs[@]}")
  else
    ADD=(claude mcp add --scope user --transport http "$as" "$(jq -r '.url' <<<"$row")")
  fi
}

FAILED=0
for row in "${ROWS[@]}"; do
  name=$(jq -r '.name' <<<"$row")
  transport=$(jq -r '.transport' <<<"$row")
  auth=$(jq -r '.auth // "none"' <<<"$row")
  want_env=$(jq -r '.env | to_entries[] | "\(.key)=\(.value)"' <<<"$row" | LC_ALL=C sort)
  detail="$transport"
  if [ "$transport" = "stdio" ]; then
    detail="$detail, env: $(jq -r '.env | to_entries | map("\(.key)=\(.value)") | if length == 0 then "none" else join(",") end' <<<"$row")"
  fi
  [ "$auth" = "interactive" ] && detail="$detail, sign in from /mcp"

  build_add "$row" "$name" "$transport" "$name"

  if [ "$HAVE_CLAUDE" -eq 0 ]; then
    echo "$name: would register ($detail; claude CLI not found, current state unknown)"
    echo "  ${ADD[*]}"
    continue
  fi

  read_current "$name"
  # A same-named server in local or project scope is not ours to replace.
  if [ "$CUR_FOUND" -eq 1 ] && [ "$CUR_USER" -eq 0 ]; then
    echo "$name: WARNING skipped; a server with this name exists in another scope (${CUR_SCOPE:-unknown}). Remove it there to let the team entry register." >&2
    continue
  fi

  same=0
  if [ "$CUR_FOUND" -eq 1 ] && [ "$CUR_TYPE" = "$transport" ]; then
    if [ "$transport" = "stdio" ]; then
      want_args=$(jq -r '(.args // []) | join(" ")' <<<"$row")
      want_cmd=$(jq -r '.command' <<<"$row")
      if [ "$CUR_COMMAND" = "$want_cmd" ] && [ "$CUR_ARGS" = "$want_args" ] && [ "$CUR_ENV" = "$want_env" ]; then same=1; fi
    elif [ "$CUR_URL" = "$(jq -r '.url' <<<"$row")" ]; then
      same=1
    fi
  fi

  if [ "$same" -eq 1 ]; then
    echo "$name: unchanged ($detail)"
    continue
  fi
  verb="registered"
  [ "$CUR_FOUND" -eq 1 ] && verb="updated"

  if [ "$DRY" -eq 1 ]; then
    echo "$name: would be $verb ($detail)"
    [ "$CUR_FOUND" -eq 1 ] && echo "  claude mcp remove --scope user $name"
    echo "  ${ADD[*]}"
    continue
  fi

  if [ "$CUR_FOUND" -eq 1 ]; then
    claude mcp remove --scope user "$name" >/dev/null 2>&1 || true
  fi

  if "${ADD[@]}" >/dev/null; then
    echo "$name: $verb ($detail)"
  else
    echo "$name: FAILED to add" >&2
    FAILED=1
    # Put the previous definition back so the server is never lost.
    if [ "$CUR_FOUND" -eq 1 ]; then
      if [ "$CUR_TYPE" = "stdio" ]; then
        restore=(claude mcp add --scope user --transport stdio "$name")
        while IFS= read -r kv; do
          if [ -n "$kv" ]; then restore+=(--env "$kv"); fi
        done <<<"$CUR_ENV"
        read -r -a old_args <<<"$CUR_ARGS"
        restore+=(-- "$CUR_COMMAND" "${old_args[@]}")
      else
        restore=(claude mcp add --scope user --transport "${CUR_TYPE:-http}" "$name" "$CUR_URL")
      fi
      if "${restore[@]}" >/dev/null 2>&1; then
        echo "$name: previous definition restored" >&2
      else
        echo "$name: previous definition could NOT be restored; re-add it by hand" >&2
      fi
    fi
  fi
done
exit "$FAILED"
