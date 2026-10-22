#!/usr/bin/env bash
# PID 1's child in a user's OmniGent host container (2026-10-20, deploy/README.md
# "Second user's host (container)"). Runs the host in the foreground the same
# way the box's own user unit does (python -m omnigent.host.service_entry), and
# plays the part systemd plays there:
#
# - no stored login yet: wait for one instead of exiting, so the container can
#   be up before anyone has signed in, and `deploy/host-container.sh <user>
#   login` is all it takes to bring the host online;
# - a permanent stop (service_entry exits 0 or 78 when the login is missing,
#   lapsed or rejected): wait until a new login rewrites the token file, then
#   start again, instead of a restart loop against a dead credential (and try
#   the stored login again every 15 minutes, in case the stop had another cause);
# - any other exit: start again after 10 seconds (the unit's Restart=on-failure).
#
# docker stop sends SIGTERM to this script (compose runs it under tini with
# init: true); it passes the signal to the host and exits once the host has.
set -uo pipefail

server="${OMNIGENT_SERVER_URL:?OMNIGENT_SERVER_URL is not set; it comes from deploy/host-container/docker-compose.yml}"
tokens="$HOME/.omnigent/auth_tokens.json"
child=""

stop() {
  if [ -n "$child" ]; then
    kill -TERM "$child" 2>/dev/null
    wait "$child" 2>/dev/null
  fi
  exit 0
}
trap stop TERM INT

# Sleep in the background so the TERM trap runs at once instead of after it.
pause() { sleep "$1" & wait $!; }
stamp() { stat -c %Y "$tokens" 2>/dev/null || echo 0; }

# $1: the token file's mtime to wait past. $2: give up and start the host again
# after this many seconds anyway (0: never), in case the stop was not the login.
wait_for_login() {
  local since="$1" limit="$2" waited=0
  echo "ae-host: no usable OmniGent login for $server. An operator on the box runs: deploy/host-container.sh <user> login" >&2
  while [ "$(stamp)" = "$since" ]; do
    if [ "$limit" -gt 0 ] && [ "$waited" -ge "$limit" ]; then
      echo "ae-host: no new login after ${limit} s; trying the stored one again" >&2
      return
    fi
    pause 10
    waited=$((waited + 10))
  done
  echo "ae-host: a new login was stored; starting the host" >&2
}

while :; do
  if [ ! -s "$tokens" ]; then
    wait_for_login 0 0
  fi
  before="$(stamp)"
  python -m omnigent.host.service_entry --server "$server" &
  child=$!
  wait "$child"
  rc=$?
  child=""
  case "$rc" in
    0|78) wait_for_login "$before" 900 ;;
    *) echo "ae-host: host exited with status $rc; starting it again in 10 s" >&2
       pause 10 ;;
  esac
done
