#!/usr/bin/env bash
# Reconnect the box host when the server rejects its credential. Added
# 2026-10-20, after the Microsoft sign-in cutover did this by hand.
#
#   deploy/host-relink.sh watch     # the user unit ae-omnigent-host-relink.service: runs forever
#   deploy/host-relink.sh relink    # one login loop now (a person at the keyboard)
set -euo pipefail

# What it fixes: when the server turns the host's credential away, the host
# (ae-omnigent-host.service) keeps retrying, or, if it never connected since
# it started, gives up after three tries and the unit fails. The fix is
# `omnigent login` on the box, the printed sign-in link opened by Reid within
# five minutes (the server's login ticket lasts 300 s), then a host RESTART:
# the host keeps its credential in memory, so a start does nothing on a running
# unit. The credential lasts 720 h and renews itself, so this is for rare
# failures.
#
# watch follows the host's journal. The host prints a rejection to it only at
# the first one and at every 30th ("The server has rejected it N times in a
# row"; the rest go to its own log file, upstream omnigent/host/connect.py), so
# the streak is that N. Once it reaches THRESHOLD (30), or the unit is found
# failed, the login loop runs once, then not again for COOLDOWN (15 min) after
# that loop ended. A "Connected as" line ends the streak.
#
# The Admin Center's "Send me a reconnect link" button leaves a request on the
# server. watch takes it every POLL_FAST (15 s) while the host is being
# rejected or its unit is down, and every POLL_IDLE (10 min) while it looks
# healthy; in between, the healthy check is only `systemctl --user is-active`,
# every POLL_SLOW (120 s). A request runs the loop unless the server's own
# hosts table says the host is online right now: the restart at the end would
# cut every session on a connected host short.
#
# The login loop: up to ATTEMPTS (5) sign-in links. Each one is `omnigent
# login` with BROWSER=true, its link sent as a task-store reminder by the
# server's CLI (which moves it to the public sign-in host, the one Microsoft
# calls back to). A "Logged in as" line restarts the host and ends the loop;
# an expired link, or a reminder that could not be sent, starts the next
# attempt. Requests left by the button are dropped when a loop starts and when
# it ends, so one incident is one login and one restart. The login's output
# holds the ticket, so it lives in a 600 file under STATE_DIR only while its
# attempt runs.
#
# Talking to the server: the status the tile shows, the button's request and
# the reminder go through the running server's own CLI (`deploy/compose.sh
# exec -T omnigent python -m ae_omni_policies.host_relink ...`), and the
# online check reads the hosts table through `compose.sh exec -T postgres
# psql`, as auto-update.sh's busy count does, because the server's /data
# volume is under /secure/omnigent, which is root-only, and the task-store
# token is in the server's environment, not the host's. A server that is down
# makes those calls fail; the loop goes on without them.
#
# Test overrides (tests/deploy/test_host_relink.py): AE_RELINK_CLI,
# AE_RELINK_JOURNAL and AE_RELINK_HOST_ONLINE replace the server CLI, the
# journal and the online check with one command each; AE_RELINK_OMNIGENT and
# AE_RELINK_SYSTEMCTL replace the binaries; the numbers below take
# AE_RELINK_<NAME>.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT="${AE_RELINK_UNIT:-ae-omnigent-host.service}"
HOST_NAME="${AE_RELINK_HOST_NAME:-$(hostname)}"
OMNIGENT_BIN="${AE_RELINK_OMNIGENT:-$HOME/.omnigent-host/venv/bin/omnigent}"
SYSTEMCTL="${AE_RELINK_SYSTEMCTL:-systemctl}"
STATE_DIR="${AE_RELINK_STATE_DIR:-$HOME/.local/state/ae-host-relink}"
THRESHOLD="${AE_RELINK_THRESHOLD:-30}"
COOLDOWN="${AE_RELINK_COOLDOWN:-900}"
ATTEMPTS="${AE_RELINK_ATTEMPTS:-5}"
URL_WAIT="${AE_RELINK_URL_WAIT:-30}"
TICKET_TTL="${AE_RELINK_TICKET_TTL:-300}"
POLL_FAST="${AE_RELINK_POLL_FAST:-15}"
POLL_SLOW="${AE_RELINK_POLL_SLOW:-120}"
POLL_IDLE="${AE_RELINK_POLL_IDLE:-600}"
RETRY_PAUSE="${AE_RELINK_RETRY_PAUSE:-10}"
SERVER_URL="${OMNIGENT_SERVER_URL:-}"
REJECTED_RE='host tunnel was rejected'
STREAK_RE='rejected it ([0-9]+) times in a row'

note() { printf '%s %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
die() { note "error: $*"; exit 1; }

# cli ARGS...: the server's host_relink CLI; only its last stdout line counts.
# Every child gets /dev/null on stdin: in watch, stdin is the journal, and
# `compose exec` or the login would swallow its lines.
cli() {
  if [ -n "${AE_RELINK_CLI:-}" ]; then
    "$AE_RELINK_CLI" "$@" </dev/null
  else
    "$ROOT/deploy/compose.sh" exec -T omnigent python -m ae_omni_policies.host_relink "$@" </dev/null
  fi
}

# report PHASE [key=value...]: what the tile shows. A failure is a note, never fatal.
report() {
  cli status "$@" >/dev/null 2>&1 || note "could not record the status ($*) on the server"
}

# drop_requests: take and discard any request the button left.
drop_requests() {
  local reply
  reply="$(cli take 2>/dev/null | tail -n 1 || true)"
  case "$reply" in
    request\ *) note "dropped a pending reconnect request (${reply#request })" ;;
  esac
}

# unit_state: active, failed, inactive, activating... as systemd says.
unit_state() {
  "$SYSTEMCTL" --user is-active "$UNIT" </dev/null 2>/dev/null || true
}

# host_online: the server's hosts table on stdout, `online`, `offline` or
# `unknown` (the query failed). Online means status online and seen within the
# last 90 s, upstream's host_is_live.
host_online() {
  local out
  if [ -n "${AE_RELINK_HOST_ONLINE:-}" ]; then
    out="$("$AE_RELINK_HOST_ONLINE" </dev/null 2>/dev/null || true)"
  else
    # The variables expand inside the postgres container, not here.
    # shellcheck disable=SC2016
    out="$(printf '%s\n' "SELECT CASE WHEN status = 1 AND updated_at >= extract(epoch from now())::bigint - 90 THEN 'online' ELSE 'offline' END FROM hosts WHERE name = :'name' AND deleted_at IS NULL AND sandbox_provider IS NULL ORDER BY updated_at DESC LIMIT 1;" \
      | timeout 30 "$ROOT/deploy/compose.sh" exec -T postgres sh -c \
        'exec psql -X -At -v ON_ERROR_STOP=1 -v name="$1" -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
        sh "$HOST_NAME" 2>/dev/null || true)"
  fi
  out="$(printf '%s\n' "$out" | tail -n 1)"
  case "$out" in
    online | offline) printf '%s\n' "$out" ;;
    *) printf 'unknown\n' ;;
  esac
}

# The journal from now on.
journal_follow() {
  if [ -n "${AE_RELINK_JOURNAL:-}" ]; then "$AE_RELINK_JOURNAL"; return; fi
  journalctl --user -fu "$UNIT" -o cat -n 0
}

# The login output holds a live ticket: removed on exit and on HUP, INT or
# TERM, with the login itself stopped. Only a SIGKILL can leave it behind,
# and the next attempt overwrites it.
LOGIN_OUT=""
LOGIN_PID=""
trap '[ -z "$LOGIN_OUT" ] || rm -f -- "$LOGIN_OUT"' EXIT
trap '[ -z "$LOGIN_PID" ] || kill "$LOGIN_PID" 2>/dev/null; exit 129' HUP
trap '[ -z "$LOGIN_PID" ] || kill "$LOGIN_PID" 2>/dev/null; exit 130' INT
trap '[ -z "$LOGIN_PID" ] || kill "$LOGIN_PID" 2>/dev/null; exit 143' TERM

link_in() {
  sed -n 's|^Opening browser for login: ||p' "$1" | head -n 1
}

# end_login: stop the attempt's login, if still running, and remove its output.
end_login() {
  if [ -n "$LOGIN_PID" ]; then
    kill "$LOGIN_PID" 2>/dev/null || true
    wait "$LOGIN_PID" 2>/dev/null || true
  fi
  LOGIN_PID=""
  [ -z "$LOGIN_OUT" ] || rm -f -- "$LOGIN_OUT"
}

# relink WHY: the login loop. 0 once signed in and the host restarted, else 1.
relink() {
  local why="$1" attempt url expires rc tries pushed=0
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
  note "relink ($why): up to $ATTEMPTS sign-in links"
  # This loop answers any request already waiting.
  drop_requests
  for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
    report sending "attempt=$attempt" "attempts=$ATTEMPTS"
    LOGIN_OUT="$STATE_DIR/login.txt"
    (umask 077; : >"$LOGIN_OUT")
    BROWSER=true "$OMNIGENT_BIN" login "$SERVER_URL" </dev/null >"$LOGIN_OUT" 2>&1 &
    LOGIN_PID=$!
    url=""
    tries=$((URL_WAIT * 5))
    while [ "$tries" -gt 0 ]; do
      url="$(link_in "$LOGIN_OUT")"
      [ -n "$url" ] && break
      if ! kill -0 "$LOGIN_PID" 2>/dev/null; then
        url="$(link_in "$LOGIN_OUT")"
        break
      fi
      sleep 0.2
      tries=$((tries - 1))
    done
    if [ -z "$url" ]; then
      note "attempt $attempt of $ATTEMPTS: the login printed no link"
      end_login
      sleep "$RETRY_PAUSE"
      continue
    fi
    expires=$(($(date +%s) + TICKET_TTL))
    if ! cli push "$url" "$expires" >/dev/null; then
      # Nobody can open a link that never arrived: stop this login, try the next.
      note "attempt $attempt of $ATTEMPTS: the reminder could not be sent; starting another"
      end_login
      report sending "attempt=$attempt" "attempts=$ATTEMPTS" \
        "message=The reminder with sign-in link $attempt could not be sent, so another is on its way."
      sleep "$RETRY_PAUSE"
      continue
    fi
    pushed=1
    note "attempt $attempt of $ATTEMPTS: sign-in link sent"
    report sent "attempt=$attempt" "attempts=$ATTEMPTS" "expires_at=$expires"
    rc=0
    wait "$LOGIN_PID" || rc=$?
    LOGIN_PID=""
    if [ "$rc" -eq 0 ] && grep -q '^Logged in as' "$LOGIN_OUT"; then
      rm -f -- "$LOGIN_OUT"
      note "signed in; restarting $UNIT"
      report restarting "attempt=$attempt" "attempts=$ATTEMPTS"
      if "$SYSTEMCTL" --user restart "$UNIT" </dev/null; then
        # A tap that landed while this loop ran is answered too.
        drop_requests
        return 0
      fi
      note "the restart of $UNIT failed"
      report failed "attempt=$attempt" "attempts=$ATTEMPTS" "reason=restart"
      return 1
    fi
    rm -f -- "$LOGIN_OUT"
    note "attempt $attempt of $ATTEMPTS ended without a sign-in (exit $rc)"
  done
  if [ "$pushed" -eq 1 ]; then
    report failed "attempt=$ATTEMPTS" "attempts=$ATTEMPTS" "reason=lapsed"
  else
    report failed "attempt=$ATTEMPTS" "attempts=$ATTEMPTS" "reason=push"
  fi
  cli giveup "$ATTEMPTS" >/dev/null || note "the giving-up reminder could not be sent"
  return 1
}

# watch: follow the journal; see the header for what it does.
watch() {
  local streak=0 since=0 state last_run=0 last_poll=0 last_check=0 now line wait_s interval
  local rc reply online
  state="$(unit_state)"
  note "watching $UNIT (unit $state)"
  while :; do
    now="$(date +%s)"
    if [ "$streak" -gt 0 ] || [ "$state" = failed ] || [ "$state" = inactive ]; then
      interval="$POLL_FAST"
    else
      interval="$POLL_IDLE"
    fi
    wait_s=$((last_poll + interval - now))
    [ $((last_check + POLL_SLOW - now)) -ge "$wait_s" ] || wait_s=$((last_check + POLL_SLOW - now))
    [ "$wait_s" -ge 1 ] || wait_s=1
    rc=0
    IFS= read -r -t "$wait_s" line || rc=$?
    if [ "$rc" -eq 0 ]; then
      if [[ "$line" =~ $REJECTED_RE ]]; then
        if [[ "$line" =~ $STREAK_RE ]]; then
          streak="$((10#${BASH_REMATCH[1]}))"
        elif [ "$streak" -eq 0 ]; then
          streak=1
        fi
        [ "$since" -ne 0 ] || since="$(date +%s)"
        report rejected "streak=$streak" "since=$since"
        now="$(date +%s)"
        if [ "$streak" -ge "$THRESHOLD" ] && [ $((now - last_run)) -ge "$COOLDOWN" ]; then
          note "$streak straight rejections"
          relink "after $streak rejections" || true
          last_run="$(date +%s)"
        fi
      elif [[ "$line" == *"Connected as '"* ]]; then
        if [ "$streak" -gt 0 ]; then
          note "host connected"
          report connected
        fi
        streak=0
        since=0
      fi
    elif [ "$rc" -le 128 ]; then
      die "the journal of $UNIT ended; systemd restarts this watcher"
    fi
    now="$(date +%s)"
    if [ $((now - last_check)) -ge "$POLL_SLOW" ]; then
      last_check="$now"
      state="$(unit_state)"
      # A host that never connected since it started gives up after three
      # rejections and its unit fails, with no 30th line to count.
      if [ "$state" = failed ] && [ $((now - last_run)) -ge "$COOLDOWN" ]; then
        note "$UNIT has failed"
        relink "the host unit failed" || true
        last_run="$(date +%s)"
        state="$(unit_state)"
      fi
    fi
    now="$(date +%s)"
    if [ $((now - last_poll)) -ge "$interval" ]; then
      last_poll="$now"
      reply="$(cli take 2>/dev/null | tail -n 1 || true)"
      case "$reply" in
        request\ *)
          online="$(host_online)"
          if [ "$online" = online ]; then
            note "a reconnect link was asked for, but the server sees the host online; nothing to do"
            report connected
          else
            note "a reconnect link was asked for (${reply#request }; server sees the host $online)"
            relink "asked for" || true
            last_run="$(date +%s)"
          fi
          ;;
      esac
    fi
  done
}

main() {
  [ -n "$SERVER_URL" ] || die "OMNIGENT_SERVER_URL is not set (config/host.env); cannot log in"
  case "${1:-}" in
    watch) watch < <(journal_follow) ;;
    relink) relink "by hand" ;;
    *) die "usage: deploy/host-relink.sh watch|relink" ;;
  esac
}

# One line, so a deploy that rewrites this file under a running watcher
# cannot change what bash reads next.
main "$@"; exit $?
