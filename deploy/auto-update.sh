#!/usr/bin/env bash
# Deploy the newest ae-v* image tag on the box by itself, with a health check
# and a rollback. Run every ten minutes by the user timer
# deploy/systemd/user/ae-omnigent-auto-update.timer, as reid, no sudo.
# Added 2026-09-24 so that pushing a tag is the whole release: before this,
# every release ended with a sudo line typed on the box, which a three-week
# trip with only a laptop cannot rely on.
#
#   deploy/auto-update.sh             # one pass: fetch, compare, deploy or not
set -euo pipefail

# One pass, in order:
#   Before step 0, since 2026-10-19: while the deploy hold set on the Admin
#      Center page is on (deploy_held, below), the pass records on-hold and
#      stops. Nothing is fetched, deployed or rebuilt. It asks again just
#      before step 8 writes the new tag.
#   0. A host rebuild left pending by an earlier pass ($STATE_DIR/host-pending)
#      is retried first on every pass, before the fetch and any fast-forward,
#      and only while the checkout's host code matches the running tag.
#   1. git fetch --tags; the target is the highest ae-v* tag by version sort,
#      or the tag in $STATE_DIR/pin when that file exists (pinning).
#   2. Nothing to do when the target is the running OMNIGENT_IMAGE_TAG, sorts
#      below it, or is the tag in $STATE_DIR/failed (rolled back once).
#   3. Held: $STATE_DIR/hold-upstream exists and the target's upstream part
#      (X.Y.Z in ae-vX.Y.Z.N) differs from the running tag's. Nothing moves,
#      the checkout included; an AE-only tag still deploys.
#   4. Refuse a tag whose commit is not on origin/main.
#   5. Fail a busy checkout; else git merge --ff-only origin/main and git submodule update --init, so
#      compose.sh and the override match the release. Every git step here is
#      retried a few times while another git process holds a lock.
#   6. docker pull the tag. Not published yet (image.yml still building) is a
#      quiet no-op; the next pass tries again. Any other pull error fails.
#   7. The database may migrate (the upstream part or the upstream code moved):
#      pg_dump it into $STATE_DIR/dumps first. A failed dump deploys nothing.
#   8. Record the running tag in $STATE_DIR/previous, write the new tag into
#      config/server.env, deploy/compose.sh up -d, then GET /health with the
#      retries the runbook uses (the port resets connections while
#      migrations run).
#   9. Unhealthy: write the old tag back, up -d again, check health again,
#      record the tag in $STATE_DIR/failed and exit 1, so the unit shows
#      failed and the journal carries the reason.
#  10. Healthy and the host's code changed between the two tags: rebuild
#      the host venv and restart ae-omnigent-host (host_moved, below). A
#      failure leaves $STATE_DIR/host-pending for step 0 of the next pass.

# Every pass writes one line to $STATE_DIR/last (time, from, to, result); every
# pass that is not a no-op also appends it to $STATE_DIR/log. The journal
# (journalctl --user -u ae-omnigent-auto-update) has the full output.
#
# A pass also waits while a session is mid-turn (wait_for_quiet, below):
# after the checks of steps 4 and 5 and before the fast-forward, again just
# before step 8 writes the new tag, and before the host retry in step 0.
# Nothing moves, it records result=deferred, the next pass asks again, and
# two hours ($MAX_DEFER seconds) after the first wait on the running tag it
# goes ahead anyway. Wall time since the wait began counts toward the two
# hours, with one exception: when $PAUSE_GAP seconds (half an hour) passed
# between two passes that held the lock (the timer was stopped or the box was
# down), the wait starts again, so ending a pause does not deploy over busy
# sessions at once. Every pass that holds the lock writes $STATE_DIR/last-pass
# as that heartbeat, including a pass that later fails. A pass that finds the
# deploy hold on removes the wait's files, so once the hold ends busy sessions
# get a fresh two hours: a hold is a pause too.
#
# What it needs: reid in the docker group and logged in to ghcr.io
# (`docker login ghcr.io`, once; compose pulls with the invoking user's
# credentials, and root's login does not help a user unit), read access to
# /secure/env/omnigent-ae.env (640 root:reid, for compose.sh), and a checkout
# reid owns. It never reads a secret itself and never calls sudo.

# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../scripts/_lib.sh"

# Overrides, all optional (the tests set them): AE_SERVER_SETTINGS (also read
# by compose.sh), the state directory, the checkout, the branch a tag must be
# on, the command run when the upstream pin moved (empty skips it), the
# command that prints a pg_dump of the running database on stdout (empty means
# the compose exec in run_dump; there is no way to skip the dump), and how
# often and how far apart a git step is retried while a lock is held.
SETTINGS="${AE_SERVER_SETTINGS:-$ROOT/config/server.env}"
STATE_DIR="${AE_AUTO_UPDATE_STATE:-$HOME/.omnigent-ae-auto-update}"
REPO="${AE_AUTO_UPDATE_REPO:-$ROOT}"
BRANCH="${AE_AUTO_UPDATE_BRANCH:-main}"
HOST_REBUILD="${AE_AUTO_UPDATE_HOST_REBUILD-$ROOT/deploy/install-host.sh}"
DUMP_CMD="${AE_AUTO_UPDATE_DUMP:-}"
GIT_TRIES="${AE_AUTO_UPDATE_GIT_TRIES:-5}"
GIT_RETRY_DELAY="${AE_AUTO_UPDATE_GIT_RETRY_DELAY:-2}"
BUSY_CMD="${AE_AUTO_UPDATE_BUSY:-}"
MAX_DEFER="${AE_AUTO_UPDATE_MAX_DEFER:-7200}"
# Three timer periods: a longer silence between two passes is a pause. A value
# that is not a whole number, or is under two timer periods (1200), would make
# an ordinary slow pass look like a pause, so it falls back to 1800.
PAUSE_GAP="${AE_AUTO_UPDATE_PAUSE_GAP:-1800}"
if ! [[ "$PAUSE_GAP" =~ ^[0-9]+$ ]] || [ "$PAUSE_GAP" -lt 1200 ]; then
  note "AE_AUTO_UPDATE_PAUSE_GAP=$PAUSE_GAP is not a whole number of at least 1200 seconds; using 1800"
  PAUSE_GAP=1800
fi
BUSY_TIMEOUT="${AE_AUTO_UPDATE_BUSY_TIMEOUT:-30}"
# The deploy hold's reader (a test override, like BUSY_CMD; empty means the
# compose exec in read_deploy_hold). It shares BUSY_TIMEOUT.
HOLD_CMD="${AE_AUTO_UPDATE_DEPLOY_HOLD:-}"
COMPOSE="$ROOT/deploy/compose.sh"
# The image comes from config/server.env (AE_IMAGE, the same line the compose
# overlay interpolates), read once setting() exists.
IMAGE=""

HOST_PENDING="$STATE_DIR/host-pending"
DEFERRED="$STATE_DIR/deferred"
DEFERRED_HOST="$STATE_DIR/deferred-host"
HOLD="$STATE_DIR/hold-upstream"
DUMP_DIR="$STATE_DIR/dumps"
DUMP_KEEP=3

FROM=""
TO=""
GIT_ERROR=""
DUMP_ERROR=""

# uv, which deploy/install-host.sh needs, installs to ~/.local/bin; the unit
# spells that out, and this covers a pass started by hand or from cron.
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH" ;;
esac
export PATH

# What the host venv is built from; a change to any of these between the
# running tag and the new one rebuilds it. Tag to tag, not the checkout before
# and after this pass: a pass that ends "waiting" (image not built yet) has
# already fast-forwarded the checkout, so the pass that deployed saw no
# movement and skipped the rebuild. That is how ae-v0.13.0.8 reached the box
# on 2026-09-24 without ae_omni_policies.limits_relay in the host venv
# (https://github.com/analyticendeavors/omnigent-ae/pull/26).
HOST_PATHS=(upstream upstream.lock packages/ae_omni_policies)

# code_moved PATH...: whether any PATH differs between tag FROM and tag TO.
# An unknown FROM (a tag written by hand, or pruned) counts as moved: a
# needless rebuild or dump costs a minute, a missed one leaves the host behind
# or the database without a way back.
code_moved() {
  git -C "$REPO" rev-parse --verify --quiet "refs/tags/$FROM^{commit}" >/dev/null || return 0
  ! git -C "$REPO" diff --quiet "refs/tags/$FROM^{commit}" "refs/tags/$TO^{commit}" -- "$@"
}

host_moved() {
  code_moved "${HOST_PATHS[@]}"
}

# upstream_of TAG: the upstream part of ae-vX.Y.Z.N (X.Y.Z), empty for a tag
# not in that shape.
upstream_of() {
  local t="${1#ae-v}"
  if [[ "$1" =~ ^ae-v[0-9]+(\.[0-9]+)+$ ]]; then
    printf '%s' "${t%.*}"
  fi
}

# upstream_tag_moved: the upstream part differs between FROM and TO. A tag
# not in the ae-vX.Y.Z.N shape counts as moved.
upstream_tag_moved() {
  local a b
  a="$(upstream_of "$FROM")"
  b="$(upstream_of "$TO")"
  [ -z "$a" ] || [ -z "$b" ] || [ "$a" != "$b" ]
}

# db_may_migrate: the migrations ship with upstream, so a move of the upstream
# part of the tag, or of the upstream code itself (a patch can carry one
# without the version changing), may migrate the database.
db_may_migrate() {
  upstream_tag_moved || code_moved upstream upstream.lock
}

mkdir -p "$STATE_DIR"

# One pass at a time: a slow pull or a long migration can outlast the timer's
# ten minutes, and two passes editing server.env at once would race.
exec 9>"$STATE_DIR/lock"
if ! flock -n 9; then
  note "another auto-update pass is running; skipping this one"
  exit 0
fi

# Heartbeat, added 2026-10-18: every pass that holds the lock records when it
# started, so wait_for_quiet can tell a pause (no pass for PAUSE_GAP seconds)
# from a long run of passes that merely failed.
PREV_PASS=""
[ ! -r "$STATE_DIR/last-pass" ] || read -r PREV_PASS <"$STATE_DIR/last-pass" || true
PASS_START="$(date +%s)"
PASS_GAP=0
[[ "$PREV_PASS" =~ ^[0-9]+$ ]] && PASS_GAP=$((PASS_START - PREV_PASS))
printf '%s\n' "$PASS_START" >"$STATE_DIR/last-pass.tmp" && mv "$STATE_DIR/last-pass.tmp" "$STATE_DIR/last-pass"

# status RESULT [DETAIL]: the one-line record. noop, waiting, skipped, held,
# deferred and on-hold go to `last` only, so the log keeps the deploys and
# the failures, not 144 lines a day.
status() {
  local line detail="${2:-}"
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) from=${FROM:-none} to=${TO:-none} result=$1"
  detail="${detail//$'\n'/ }"
  [ -z "$detail" ] || line="$line detail=$detail"
  printf '%s\n' "$line" >"$STATE_DIR/last.tmp"
  mv "$STATE_DIR/last.tmp" "$STATE_DIR/last"
  case "$1" in
    noop | waiting | skipped | held | deferred | on-hold) ;;
    *) printf '%s\n' "$line" >>"$STATE_DIR/log" ;;
  esac
  note "$line"
}

# fail RESULT DETAIL: record, shout into the journal, exit 1 (the unit fails,
# so `systemctl --user --failed` shows it).
fail() {
  status "$1" "$2"
  printf 'AUTO-UPDATE %s: %s\n' "$1" "$2" >&2
  exit 1
}

# first_error_line TEXT: git's own first "fatal:" or "error:" line, else its
# first non-empty line, for the record.
first_error_line() {
  local line
  line="$(printf '%s\n' "$1" | grep -m 1 -E '^(fatal|error):' || true)"
  [ -n "$line" ] || line="$(printf '%s\n' "$1" | grep -m 1 -v '^[[:space:]]*$' || true)"
  printf '%s' "${line:-git printed nothing}"
}

# run_git ARGS...: git -C $REPO ARGS, retried while another git process holds
# a lock in the checkout. Observed 2026-09-24 19:00 PDT: a session's `git
# status` in this checkout held .git/index.lock at the moment the merge ran,
# the pass failed, and the record blamed local commits that did not exist. A
# lock like that clears in milliseconds, so GIT_TRIES tries GIT_RETRY_DELAY
# seconds apart ride it out. The lock file is never deleted here: it may
# belong to a live git process. When the step finally fails, GIT_ERROR holds
# git's own first error line, which goes into the record in place of a guess.
run_git() {
  local try out
  for ((try = 1; try <= GIT_TRIES; try++)); do
    if out="$(git -C "$REPO" "$@" 2>&1)"; then
      [ -z "$out" ] || printf '%s\n' "$out"
      return 0
    fi
    printf '%s\n' "$out" >&2
    case "$out" in
      *"Unable to create '"*".lock': File exists"*)
        if [ "$try" -lt "$GIT_TRIES" ]; then
          note "git $1: another git process holds a lock in $REPO; try $((try + 1)) of $GIT_TRIES in ${GIT_RETRY_DELAY}s"
          sleep "$GIT_RETRY_DELAY"
          continue
        fi
        GIT_ERROR="$(first_error_line "$out") (still held after $GIT_TRIES tries; the lock belongs to another git process and is never deleted here)"
        return 1
        ;;
    esac
    break
  done
  GIT_ERROR="$(first_error_line "$out")"
  return 1
}

setting() {
  sed -n "s/^$1=//p" "$SETTINGS" | tail -n 1
}

# Rewrites the one line in place (sed -i writes a temp file and renames it, so
# a concurrent reader sees the old file or the new one, never half of each).
set_tag() {
  sed -i "s/^OMNIGENT_IMAGE_TAG=.*/OMNIGENT_IMAGE_TAG=$1/" "$SETTINGS"
  [ "$(setting OMNIGENT_IMAGE_TAG)" = "$1" ] || die "could not write OMNIGENT_IMAGE_TAG=$1 into $SETTINGS"
}

healthy() {
  curl --silent --show-error --output /dev/null \
    --max-time 10 --retry 12 --retry-delay 5 --retry-all-errors --fail \
    "http://127.0.0.1:$PORT/health"
}

# rebuild_host: the host venv and a restart of the host unit; clears
# host-pending on success. Callers write host-pending first, so a pass killed
# halfway (the unit's timeout) still leaves the retry behind.
rebuild_host() {
  "$HOST_REBUILD" && systemctl --user restart ae-omnigent-host.service && rm -f "$HOST_PENDING" "$DEFERRED_HOST"
}

# checkout_matches_running: whether the checkout would build the host the
# running tag needs: the tag exists here, HEAD has the same host code as it,
# and the working tree (the checked-out submodule included) matches HEAD.
checkout_matches_running() {
  git -C "$REPO" rev-parse --verify --quiet "refs/tags/$FROM^{commit}" >/dev/null || return 1
  git -C "$REPO" diff --quiet "refs/tags/$FROM^{commit}" HEAD -- "${HOST_PATHS[@]}" || return 1
  git -C "$REPO" diff --quiet --ignore-submodules=dirty HEAD -- "${HOST_PATHS[@]}"
}

# busy_count: how many sessions are mid-turn, on stdout. A session counts
# when its last relay-observed live_status is running or waiting (2 and 3 in
# upstream's enum_codecs.SESSION_LIVE_STATUS; waiting is a turn held on an
# approval prompt, which a restart ends too) and its runner checked in within
# five minutes, so a row left running by a runner that died does not hold
# every deploy. Read through compose.sh exec, like run_dump, and given
# BUSY_TIMEOUT seconds, so a postgres that hangs costs half a minute, not the
# unit's twenty. The single quotes are deliberate: the variables expand
# inside the container.
busy_count() {
  if [ -n "$BUSY_CMD" ]; then
    timeout "$BUSY_TIMEOUT" "$BUSY_CMD"
    return
  fi
  # shellcheck disable=SC2016
  timeout "$BUSY_TIMEOUT" "$COMPOSE" exec -T postgres sh -c 'exec psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM omnigent_conversation_metadata WHERE live_status IN (2, 3) AND runner_last_seen >= extract(epoch from now())::bigint - 300"'
}

# wait_for_quiet FILE KEY: added 2026-10-17. A deploy that restarts the
# host ends every turn in flight ("The connection to the host dropped
# unexpectedly"), and the server restart may as well; on 2026-10-17 three
# merges between 06:30 and 07:10 PDT cut every live session three times.
# Returns 0 when no session is mid-turn, or when MAX_DEFER seconds have
# passed since the first wait on KEY; else returns 1 with the reason in
# DEFER_DETAIL. KEY is the running tag for a deploy, so a newer tag merged
# while sessions stay busy does not start the clock again, and the pending
# tag for the host retry. FILE ($DEFERRED for a deploy, $DEFERRED_HOST for
# the host retry, so the two never reset each other) holds "KEY SINCE": the
# key and when it first waited. Wall time since then counts toward MAX_DEFER,
# except that, added 2026-10-18, a gap of PAUSE_GAP seconds or more between
# this pass and the previous pass that held the lock (PASS_GAP, from the
# last-pass heartbeat; auto-update was stopped or down) restarts the wait
# once, in the first wait_for_quiet of the pass that sees the gap (SINCE older
# than PASS_START). Before, stopping the timer for hours counted toward the
# two, and the first pass after the pause went ahead over busy sessions. No
# heartbeat yet (the first pass after the upgrade) never restarts. The
# file outlives a go-ahead past the limit: it goes when
# the deploy (or the rebuild) succeeds or when a pass finds nobody mid-turn,
# so a pass that fails after the go-ahead (an image not published yet, a
# failed pull or rebuild) does not buy busy sessions another two hours. A
# count that fails or is not a number goes ahead as before, with a note: a
# broken query must not stall every release.
DEFER_DETAIL=""
wait_for_quiet() {
  local file="$1" key="$2" count now since held_key="" held_since=""
  if ! count="$(busy_count)" || ! [[ "$count" =~ ^[0-9]+$ ]]; then
    note "could not count the sessions mid-turn (got: ${count:-nothing}); going ahead as before the wait existed"
    return 0
  fi
  if [ "$count" -eq 0 ]; then
    rm -f "$file"
    return 0
  fi
  now="$(date +%s)"
  since="$now"
  [ ! -r "$file" ] || read -r held_key held_since <"$file" || true
  if [ "$held_key" = "$key" ] && [[ "$held_since" =~ ^[0-9]+$ ]]; then
    since="$held_since"
    if [ "$PASS_GAP" -ge "$PAUSE_GAP" ] && [ "$held_since" -lt "$PASS_START" ]; then
      note "no pass for $((PASS_GAP / 60)) minutes (auto-update was paused or down); the wait on $key starts again"
      since="$now"
      printf '%s %s\n' "$key" "$now" >"$file.tmp" && mv "$file.tmp" "$file"
    fi
  else
    printf '%s %s\n' "$key" "$now" >"$file.tmp" && mv "$file.tmp" "$file"
  fi
  if [ $((now - since)) -ge "$MAX_DEFER" ]; then
    note "$count session(s) still mid-turn after $(((now - since) / 60)) minutes of waiting on $key; going ahead anyway"
    return 0
  fi
  DEFER_DETAIL="$count session(s) mid-turn; waiting since $(date -u -d "@$since" +%H:%MZ) on $key, and it goes ahead anyway at $(date -u -d "@$((since + MAX_DEFER))" +%H:%MZ)"
  return 1
}

# read_deploy_hold: the hold as the server's CLI prints it (deploy_hold.py):
# `none`, or `held <until epoch> <set_by>`; exit 2 for a hold file it cannot
# read.
read_deploy_hold() {
  if [ -n "$HOLD_CMD" ]; then
    timeout "$BUSY_TIMEOUT" "$HOLD_CMD"
    return
  fi
  timeout "$BUSY_TIMEOUT" "$COMPOSE" exec -T omnigent python -m ae_omni_policies.deploy_hold
}

# deploy_held: added 2026-10-19 with the Admin Center's Deploys section. A
# tap there holds deploys for 1, 2 or 3 hours and the hold ends on its own;
# before, holding one meant stopping this timer by hand, which the page could
# not show and nobody remembered to undo. Returns 0 while a hold is on (its
# end in HOLD_UNTIL, who set it in HOLD_BY), else 1. The hold lives in the
# server's /data volume, under /secure/omnigent on the box, which is
# root-only, so this user unit cannot read the file directly; it asks the
# running server's own CLI through compose.sh exec instead (reid is in the
# docker group, like the busy count and run_dump). Only the last line of
# stdout counts, since the import may print first. It fails open, like the
# busy count: a read that fails, times out after BUSY_TIMEOUT seconds or
# prints anything but `none` or `held <digits> <word>` goes ahead as if there
# were no hold, with a note, so a broken hold (or a server that is down, or
# an image from before the hold existed, which is the first deploy of this
# change) never blocks a release.
HOLD_UNTIL=""
HOLD_BY=""
deploy_held() {
  local out="" rc=0 last now reason held_re='^held ([0-9]{1,12}) ([^[:space:]]+)$'
  HOLD_UNTIL=""
  HOLD_BY=""
  out="$(read_deploy_hold)" || rc=$?
  last="$(printf '%s\n' "$out" | tail -n 1)"
  if [ "$rc" -ne 0 ]; then
    reason="exit $rc"
    [ "$rc" -ne 124 ] || reason="no answer in ${BUSY_TIMEOUT}s"
    note "could not read the deploy hold ($reason); going ahead as if there were none"
    return 1
  fi
  if [ "$last" = none ]; then
    return 1
  fi
  if ! [[ "$last" =~ $held_re ]]; then
    note "could not read the deploy hold (got: ${last:-nothing}); going ahead as if there were none"
    return 1
  fi
  now="$(date +%s)"
  if [ "$((10#${BASH_REMATCH[1]}))" -le "$now" ]; then
    return 1
  fi
  HOLD_UNTIL="$((10#${BASH_REMATCH[1]}))"
  HOLD_BY="${BASH_REMATCH[2]}"
  return 0
}

# retry_host_pending: added 2026-09-24. A healthy deploy whose host rebuild
# failed used to record host-failed once; the next pass saw the new tag
# running, exited noop, and the host stayed on the old code until someone
# typed the two commands. Now the failed rebuild leaves host-pending (the tag
# it was for) and every pass retries it, first thing: before the fetch, so a
# fetch failure cannot block the recovery, and before any fast-forward, so
# the host is never built from a newer release than the server runs. A
# checkout that has already moved past the running tag (a pass that ended
# waiting, a rolled-back tag) is left alone; the pass that deploys the
# checkout's tag rebuilds, because host-pending is still there. A failed
# retry records host-failed but lets the pass go on, so a newer tag (one
# that fixes the rebuild, say) can still deploy; a pass that deploys nothing
# then fails the unit (finish).
HOST_RETRY_FAILED=""
retry_host_pending() {
  local tag
  if [ ! -e "$HOST_PENDING" ] || [ -z "$HOST_REBUILD" ]; then
    return 0
  fi
  tag="$(cat "$HOST_PENDING")"
  if ! checkout_matches_running; then
    status host-deferred "host-pending (${tag:-no tag named}) not retried: the host code in $REPO (${HOST_PATHS[*]}) is not the running tag $FROM's, and building it would put the host ahead of the server; the pass that deploys the checkout's tag rebuilds it"
    return 0
  fi
  if ! wait_for_quiet "$DEFERRED_HOST" "${tag:-unnamed}"; then
    status deferred "the host rebuild pending for ${tag:-no tag named} is not retried yet: $DEFER_DETAIL"
    return 0
  fi
  note "retrying the host rebuild left pending for ${tag:-an unnamed tag}, from $REPO at the running tag's host code"
  if rebuild_host; then
    status host-ok "host rebuilt for $FROM on a retry (pending for ${tag:-no tag named})"
    return 0
  fi
  HOST_RETRY_FAILED="the host rebuild pending for ${tag:-no tag named} failed again; the next pass retries it (by hand: deploy/install-host.sh, then systemctl --user restart ae-omnigent-host)"
  status host-failed "$HOST_RETRY_FAILED"
  printf 'AUTO-UPDATE host-failed: %s\n' "$HOST_RETRY_FAILED" >&2
}

# finish RESULT [DETAIL]: a pass that deploys nothing: records and exits 0,
# or exits 1 without a second record when this pass's host retry failed (its
# host-failed line stays in `last`, and the unit shows failed).
# checkout_busy: print why $REPO is not a clean checkout of $BRANCH and
# return 0, or return 1 when it is clean. Added 2026-10-13 with the move to a
# deploy clone nobody works in (deploy/README.md, "The deploy clone"):
# until then the fast-forward and the submodule update ran in the checkout
# every session opened, reset whatever branch a session had checked out in
# upstream/, and the host built its runners from that branch. A busy clone
# now fails the pass, so the unit shows failed and nothing is overwritten;
# the first pass after it is clean deploys. Untracked files do not count.
checkout_busy() {
  local branch changes
  branch="$(git -C "$REPO" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [ "$branch" != "$BRANCH" ]; then
    printf '%s is on %s, not %s' "$REPO" "${branch:-a detached HEAD}" "$BRANCH"
    return 0
  fi
  changes="$(git -C "$REPO" status --porcelain --untracked-files=no 2>/dev/null | head -n 3 | tr '\n' ' ')"
  if [ -n "$changes" ]; then
    printf '%s has changes (git status: %s)' "$REPO" "${changes% }"
    return 0
  fi
  return 1
}

finish() {
  if [ -n "$HOST_RETRY_FAILED" ]; then
    note "no deploy this pass ($1); failing the unit for the host rebuild"
    exit 1
  fi
  status "$@"
  exit 0
}

# run_dump: a pg_dump of the running database on stdout. Same command as
# deploy/backup-omnigent.sh, through compose.sh exec, which reid can run
# without sudo (docker group, 640 root:reid secrets) exactly like the up -d
# below. The single quotes are deliberate: the variables expand inside the
# container.
run_dump() {
  if [ -n "$DUMP_CMD" ]; then
    "$DUMP_CMD"
    return
  fi
  # shellcheck disable=SC2016
  "$COMPOSE" exec -T postgres sh -c 'exec pg_dump --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
}

# dump_db: added 2026-09-24. An image rollback does not roll back migrations,
# so before a deploy that may migrate, the database as the running tag left it
# goes to $DUMP_DIR/omnigent-<UTC stamp>-<running tag>.sql.gz, newest
# $DUMP_KEEP kept. Not /secure/omnigent/backups: that is root-only and this
# runs as reid. Restore: deploy/README.md, "Rollback".
dump_db() {
  local stamp out tmp old f
  mkdir -p "$DUMP_DIR"
  chmod 700 "$DUMP_DIR"
  rm -f -- "$DUMP_DIR"/*.tmp
  stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
  out="$DUMP_DIR/omnigent-$stamp-$FROM.sql.gz"
  tmp="$out.tmp"
  # pipefail makes a pg_dump failure fail the line although gzip exits 0.
  if ! run_dump | gzip -9 >"$tmp"; then
    rm -f -- "$tmp"
    DUMP_ERROR="the dump command exited non-zero (the journal has its output)"
    return 1
  fi
  if ! gzip -t "$tmp"; then
    rm -f -- "$tmp"
    DUMP_ERROR="the dump is not a valid gzip stream"
    return 1
  fi
  # A gzip of nothing is still about twenty bytes, so look inside.
  if [ -z "$(gzip -dc "$tmp" | head -c 1)" ]; then
    rm -f -- "$tmp"
    DUMP_ERROR="the dump is empty"
    return 1
  fi
  chmod 600 "$tmp"
  mv -f -- "$tmp" "$out"
  # The names start with the UTC stamp, so a name sort is a time sort.
  mapfile -t old < <(find "$DUMP_DIR" -maxdepth 1 -type f -name 'omnigent-*.sql.gz' -printf '%f\n' | sort -r | tail -n +"$((DUMP_KEEP + 1))")
  for f in "${old[@]}"; do
    rm -f -- "${DUMP_DIR:?}/${f:?}"
  done
  note "dumped the database to $out before the upgrade; ${#old[@]} older dump(s) removed"
}

[ -w "$SETTINGS" ] || die "cannot write $SETTINGS. Copy config/server.env.example to config/server.env as reid (deploy/README.md, step 3)."
FROM="$(setting OMNIGENT_IMAGE_TAG)"
[ -n "$FROM" ] || die "$SETTINGS has no OMNIGENT_IMAGE_TAG line; add the running tag (deploy/README.md, step 3)."
IMAGE="$(setting AE_IMAGE)"
[ -n "$IMAGE" ] || die "$SETTINGS has no AE_IMAGE line; add AE_IMAGE=ghcr.io/<owner>/<repo>, the package this box pulls (deploy/README.md, step 3)."
PORT="$(setting OMNIGENT_PORT)"
PORT="${PORT:-8010}"

# The deploy hold, first: before the host retry, the fetch and any
# fast-forward, so a held box changes nothing at all. Both wait files go, so
# the two-hour busy wait starts fresh when the hold ends.
if deploy_held; then
  rm -f "$DEFERRED" "$DEFERRED_HOST"
  finish on-hold "deploys are held until $(date -u -d "@$HOLD_UNTIL" +%H:%MZ) by $HOLD_BY (Admin Center, Deploys); nothing was fetched, deployed or rebuilt"
fi

retry_host_pending

run_git fetch --quiet --tags origin \
  || fail fetch-failed "git fetch origin failed in $REPO: $GIT_ERROR (ssh -T git@github.com as reid tests the deploy key)"

PIN="$(cat "$STATE_DIR/pin" 2>/dev/null || true)"
if [ -n "$PIN" ]; then
  TO="$PIN"
else
  TO="$(git -C "$REPO" tag --list 'ae-v*' --sort=-version:refname | sed -n 1p)"
fi

if [ -z "$TO" ]; then
  finish noop "no ae-v* tag"
fi
if [ "$TO" = "$FROM" ]; then
  finish noop
fi
if [ -z "$PIN" ] && [ "$(printf '%s\n%s\n' "$FROM" "$TO" | sort -V | tail -n 1)" = "$FROM" ]; then
  finish noop "running tag is newer than any ae-v* tag; write a tag to $STATE_DIR/pin to move it"
fi
if [ "$(cat "$STATE_DIR/failed" 2>/dev/null || true)" = "$TO" ]; then
  finish skipped "$TO was rolled back once; delete $STATE_DIR/failed to retry it"
fi

# Added 2026-09-24 for the three-week trip: an upstream move brings upstream's
# migrations and behaviour changes, which an iPhone cannot debug. The hold
# stops before the fast-forward, so compose.sh and the override keep matching
# the running image; the tag is not marked failed and deploys once lifted.
if [ -e "$HOLD" ] && upstream_tag_moved; then
  from_up="$(upstream_of "$FROM")"
  to_up="$(upstream_of "$TO")"
  finish held "$TO moves upstream from ${from_up:-an unknown version} to ${to_up:-an unknown version}; staying on $FROM while $HOLD exists (rm $HOLD to let it deploy)"
fi

git -C "$REPO" rev-parse --verify --quiet "refs/tags/$TO^{commit}" >/dev/null \
  || fail refused "tag $TO does not exist in $REPO (check $STATE_DIR/pin)"
git -C "$REPO" merge-base --is-ancestor "refs/tags/$TO^{commit}" "refs/remotes/origin/$BRANCH" \
  || fail refused "tag $TO is not on origin/$BRANCH; releases are tagged on $BRANCH only"

if busy="$(checkout_busy)"; then
  fail busy "$busy; nothing was deployed or reset. Commit, stash or switch back to $BRANCH there (it should be a clone nobody works in, deploy/README.md), and the next pass deploys $TO"
fi

if ! wait_for_quiet "$DEFERRED" "$FROM"; then
  finish deferred "$DEFER_DETAIL"
fi

run_git merge --ff-only --quiet "refs/remotes/origin/$BRANCH" \
  || fail git-failed "git merge --ff-only origin/$BRANCH failed in $REPO: $GIT_ERROR (git status there)"
run_git submodule update --init --quiet \
  || fail git-failed "git submodule update --init failed in $REPO: $GIT_ERROR"

if ! pull_out="$(docker pull --quiet "$IMAGE:$TO" 2>&1)"; then
  case "$pull_out" in
    *"manifest unknown"* | *"not found"* | *"manifest for"*)
      finish waiting "$IMAGE:$TO is not published yet (image.yml still running?)"
      ;;
    *)
      fail pull-failed "docker pull $IMAGE:$TO: $(printf '%s' "$pull_out" | tr '\n' ' ') (not logged in? docker login ghcr.io as reid, deploy/README.md \"Automatic updates\")"
      ;;
  esac
fi

if db_may_migrate; then
  note "the upstream code moved between $FROM and $TO; dumping the database before the upgrade"
  dump_db \
    || fail dump-failed "pg_dump before moving from $FROM to $TO failed: $DUMP_ERROR; nothing was deployed and the next pass tries again (deploy/compose.sh ps shows whether postgres is up)"
fi

# Asked again here: the pull and the dump can take minutes, and a turn
# started meanwhile would be cut by the restart below.
if ! wait_for_quiet "$DEFERRED" "$FROM"; then
  finish deferred "$DEFER_DETAIL"
fi

# The deploy hold, asked again: it may have been set during the pull or the
# dump. The checkout may already be on $TO here, as for a deferral at this
# point; the server stays on $FROM until the hold ends. Both wait clocks go,
# as at the first check: retry_host_pending may have written $DEFERRED_HOST
# earlier in this pass, and a stale one would let the next pass rebuild the
# host over busy sessions at once.
if deploy_held; then
  rm -f "$DEFERRED" "$DEFERRED_HOST"
  finish on-hold "$TO deploys after the hold set by $HOLD_BY ends at $(date -u -d "@$HOLD_UNTIL" +%H:%MZ) (Admin Center, Deploys); the server stays on $FROM"
fi

printf '%s\n' "$FROM" >"$STATE_DIR/previous"
note "deploying $TO (was $FROM)"
set_tag "$TO"
if "$COMPOSE" up -d --remove-orphans && healthy; then
  rm -f "$STATE_DIR/failed" "$DEFERRED"
  if [ -n "$HOST_REBUILD" ] && { [ -e "$HOST_PENDING" ] || host_moved; }; then
    note "the host's code moved between $FROM and $TO (or a rebuild was pending); rebuilding the host to match the server"
    printf '%s\n' "$TO" >"$HOST_PENDING"
    rebuild_host \
      || fail host-failed "server is healthy on $TO but the host rebuild failed; the next pass retries it (by hand: deploy/install-host.sh, then systemctl --user restart ae-omnigent-host)"
  fi
  status ok
  exit 0
fi

printf 'AUTO-UPDATE: %s is unhealthy; rolling back to %s\n' "$TO" "$FROM" >&2
printf '%s\n' "$TO" >"$STATE_DIR/failed"
set_tag "$FROM"
if "$COMPOSE" up -d --remove-orphans && healthy; then
  fail rolled-back "$TO failed its health check; back on $FROM (deploy/compose.sh logs omnigent for why)"
fi
fail rollback-failed "$TO failed its health check and $FROM did not come back healthy either; the server is DOWN (deploy/README.md, Rollback)"
