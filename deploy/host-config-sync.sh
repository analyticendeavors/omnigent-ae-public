#!/usr/bin/env bash
# Push the shared AE setup from the box into one user's host container
# (2026-10-28, deploy/README.md "Syncing the shared AE setup").
#
#   deploy/host-container.sh member sync-config [--dry-run]
#   deploy/host-config-sync.sh member [--dry-run]     # the same thing
#
# The source is refs/remotes/origin/main of the box's ae-claude-skills clone, read
# with `git archive`, never its working tree or a local main, so unmerged or
# uncommitted work cannot leak. The clone is kept current by ae-rules-sync.timer, so
# this script never fetches (it warns when origin/main is over six hours old).
# What goes is an allowlist (claude-config/skills-manifest.txt, scope team,
# surface any or linux, plus a fixed set of team files); the lists of what
# never goes are the deny paths at the top of host-config-sync.py and the
# personal markers in the box's scrub file (config/host-scrub.txt, or
# AE_HOST_SCRUB_FILE; deploy/README.md). The archive is scanned
# before anything is streamed, and the extracted copy again inside the
# container before anything is placed.
#
# Nothing runs as root and nothing restarts or stops the container. A container
# that is not running is a skipped sync (one line, exit 0), not a failure; a docker
# error (daemon down, no permission) is a failure.
#
# Environment (all optional):
#   AE_SKILLS_CLONE    the clone to read (default ~/repos/ae-claude-skills)
#   AE_SYNC_CONTAINER  tests only: sync into this container instead of
#                      omnigent-host-<user>; accepted only as synctest-<name>
set -euo pipefail

# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../scripts/_lib.sh"

usage() { die "usage: deploy/host-config-sync.sh <user> [--dry-run]"; }

[ $# -ge 1 ] || usage
HOST_USER="$1"
shift
DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) usage ;;
  esac
done
[[ "$HOST_USER" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || die "user '$HOST_USER' must be lower-case letters, digits and dashes"

# The target is always omnigent-host-<user>. AE_SYNC_CONTAINER exists for tests and
# is accepted only for a synctest-* name, so a typo or a stray variable can never
# point a production sync at another user's container.
CONTAINER="omnigent-host-$HOST_USER"
if [ -n "${AE_SYNC_CONTAINER:-}" ]; then
  [[ "$AE_SYNC_CONTAINER" =~ ^synctest-[a-z0-9-]+$ ]] \
    || die "AE_SYNC_CONTAINER='$AE_SYNC_CONTAINER' is refused: only synctest-<name> may override the target, which is otherwise omnigent-host-<name>"
  CONTAINER="$AE_SYNC_CONTAINER"
fi
CLONE="${AE_SKILLS_CLONE:-$HOME/repos/ae-claude-skills}"
PY="$(dirname "${BASH_SOURCE[0]}")/host-config-sync.py"
MAX_AGE_SECONDS=21600

[ -d "$CLONE/.git" ] || [ -f "$CLONE/.git" ] || [ -f "$CLONE/HEAD" ] \
  || die "$CLONE is not a git clone; set AE_SKILLS_CLONE to the ae-claude-skills clone"
# A path from the manifest is a path, never a glob (git archive and ls-tree read it).
export GIT_LITERAL_PATHSPECS=1

# One sync per user at a time (the timer and a hand run can overlap). The lock sits
# under the runtime dir, not /tmp, and does not move with TMPDIR.
LOCK_DIR="${XDG_RUNTIME_DIR:-$HOME/.cache}"
mkdir -p "$LOCK_DIR"
exec 9>"$LOCK_DIR/ae-host-config-sync-$HOST_USER.lock"
flock -n 9 || { note "sync for $HOST_USER is already running; skipped"; exit 0; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# true, false or absent; any other docker failure (daemon down, no permission) is an
# error, not a skipped sync.
container_state() {
  local out rc=0
  out="$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>&1)" || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "$out"
    return 0
  fi
  case "${out,,}" in
    *"no such object"* | *"no such container"*) echo absent ;;
    *) die "docker inspect $CONTAINER failed (exit $rc): $out" ;;
  esac
}

if [ "$DRY_RUN" -eq 0 ]; then
  STATE="$(container_state)"
  if [ "$STATE" != true ]; then
    note "container $CONTAINER is not running; sync skipped"
    exit 0
  fi
fi

# The source must be origin/main: a local main may hold unmerged work. The clone is
# kept current by ae-rules-sync.timer; a stale one is a warning, not a failure.
REF=refs/remotes/origin/main
SHA="$(git -C "$CLONE" rev-parse --verify -q "$REF^{commit}")" \
  || die "$REF does not exist in $CLONE; the sync reads origin/main only (is ae-rules-sync.timer running? git fetch origin there)"
AGE=$(($(date +%s) - $(git -C "$CLONE" log -1 --format=%ct "$SHA")))
if [ "$AGE" -gt "$MAX_AGE_SECONDS" ]; then
  warn "origin/main in $CLONE is $((AGE / 3600)) hours old; the clone may have stopped updating (ae-rules-sync.timer)"
fi
note "syncing ae-claude-skills ${SHA:0:12} ($REF) into $CONTAINER"

# The plan pins the commit sha, so the ref cannot move between planning and archiving.
python3 -I "$PY" plan --repo "$CLONE" --ref "$SHA" >"$TMP/plan.json"

readonly JSON_LIST='import json, sys
plan = json.load(open(sys.argv[1]))
key = sys.argv[2]
if key == "warnings":
    print("\n".join(plan["warnings"]))
elif key == "skills":
    print(" ".join(s["name"] for s in plan["skills"]))
else:
    print("\0".join(plan[key]), end="")'
plan_get() { python3 -I -c "$JSON_LIST" "$TMP/plan.json" "$1"; }

while IFS= read -r line; do
  [ -z "$line" ] || warn "$line"
done < <(plan_get warnings)

mapfile -d '' ARCHIVE_PATHS < <(plan_get archive_paths)
git -C "$CLONE" archive --format=tar -o "$TMP/team.tar" "$SHA" -- "${ARCHIVE_PATHS[@]}"

# Scan 1, on the box, before a byte is streamed.
python3 -I "$PY" scan-tar "$TMP/team.tar" --plan-file "$TMP/plan.json" \
  || die "safety scan failed on the archive; nothing was sent to $CONTAINER"

if [ "$DRY_RUN" -eq 1 ]; then
  note "dry run: the scan passed. Skills: $(plan_get skills)"
  note "files that would be sent:"
  mapfile -d '' EXPECTED < <(plan_get expected_files)
  printf '  %s\n' "${EXPECTED[@]}"
  note "dry run: nothing was sent or changed"
  exit 0
fi

# One stream carries the archive, the plan and the helper into one `docker exec`, so the
# plan travels as a file (not an argument) and the whole in-container run sits under one lock.
mkdir -p "$TMP/payload/team"
tar -x --no-same-owner -C "$TMP/payload/team" -f "$TMP/team.tar"
cp "$TMP/plan.json" "$TMP/payload/plan.json"
cp "$PY" "$TMP/payload/host-config-sync.py"
# Test hook, honoured only for a synctest-* container: put a file into the stream AFTER
# scan 1, to prove scan 2 inside the container catches what scan 1 could not.
if [[ "$CONTAINER" == synctest-* ]] && [ -n "${AE_SYNC_TEST_INJECT_FILE:-}" ]; then
  mkdir -p "$TMP/payload/team/$(dirname "$AE_SYNC_TEST_INJECT_FILE")"
  printf '%s' "${AE_SYNC_TEST_INJECT_CONTENT:-}" >"$TMP/payload/team/$AE_SYNC_TEST_INJECT_FILE"
fi

# Runs inside the container as its own user: lock, extract into a per-run directory,
# scan 2, place. Scan 2 failing removes the extracted copy and names the offending paths
# only (the scanner never prints file content); nothing is placed.
DRIVER="$(
  cat <<'DRIVER_EOF'
set -euo pipefail
umask 022
base="$HOME/.claude/.ae-sync"
mkdir -p "$base"
exec 8>"$base/lock"
if ! flock -n 8; then
  echo "another sync is running in this container; skipped"
  exit 0
fi
rm -rf "$base"/run-*
run="$base/run-$$"
mkdir -p "$run"
trap 'rm -rf "$run"' EXIT
tar -x --no-same-owner -C "$run"
if ! python3 -I "$run/host-config-sync.py" scan-dir "$run/team" --plan-file "$run/plan.json"; then
  rm -rf "$run/team"
  echo "SCAN FAILED in the container: the extracted copy was removed and nothing was placed" >&2
  exit 3
fi
python3 -I "$run/host-config-sync.py" place --stage "$run/team" --plan-file "$run/plan.json"
DRIVER_EOF
)"

rc=0
tar -c -C "$TMP/payload" -f "$TMP/payload.tar" .
docker exec -i -u agent "$CONTAINER" bash -c "$DRIVER" <"$TMP/payload.tar" || rc=$?
case "$rc" in
  0) ;;
  3) die "safety scan failed inside $CONTAINER; the extracted copy was removed, nothing was placed. The offending paths are listed above" ;;
  *) die "syncing into $CONTAINER finished with errors (exit $rc); see above" ;;
esac
note "synced ae-claude-skills ${SHA:0:12} into $CONTAINER"
