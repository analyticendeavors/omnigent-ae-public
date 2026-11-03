#!/usr/bin/env bash
# Remove the CI box runners: stop and disable the units, deregister each runner
# from GitHub, delete the units and the slice. Leaves the ghrunner user, its
# files and the apt packages (so a reinstall is quick).
#
# Run as root with a file holding a removal token (README.md has the command that
# writes one): sudo bash uninstall.sh <token-file>. The file is deleted after use.
set -euo pipefail

RUNNER_USER="ghrunner"
RUNNER_HOME="/home/${RUNNER_USER}"
COUNT=5

if [ "$(id -u)" -ne 0 ]; then
  echo "error: run as root (sudo bash $0 <token-file>)" >&2
  exit 1
fi

TOKEN_FILE="${1:-}"
RUNNER_TOKEN=""
if [ -n "$TOKEN_FILE" ]; then
  [ -f "$TOKEN_FILE" ] || { echo "error: token file ${TOKEN_FILE} not found" >&2; exit 1; }
  RUNNER_TOKEN="$(tr -d '[:space:]' <"$TOKEN_FILE")"
  trap 'rm -f "$TOKEN_FILE"' EXIT
fi

for i in $(seq 1 "$COUNT"); do
  systemctl disable --now "ghrunner@${i}.service" 2>/dev/null || true
done

for i in $(seq 1 "$COUNT"); do
  dir="${RUNNER_HOME}/runner-${i}"
  if [ -f "${dir}/.runner" ]; then
    if [ -z "${RUNNER_TOKEN:-}" ]; then
      echo "error: runner-${i} is registered and no token file was given (use a remove token)" >&2
      exit 1
    fi
    (cd "$dir" && runuser -u "$RUNNER_USER" -- ./config.sh remove --token "$RUNNER_TOKEN")
  fi
done

rm -f /etc/systemd/system/ghrunner@.service /etc/systemd/system/ghrunner.slice
systemctl daemon-reload
echo "removed: units, slice and registrations. The ${RUNNER_USER} user and the packages stay."
