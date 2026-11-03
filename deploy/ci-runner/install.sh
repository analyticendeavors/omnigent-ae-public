#!/usr/bin/env bash
# Install the CI box runners: five GitHub Actions self-hosted runners for
# analyticendeavors/omnigent-ae under their own unprivileged user, as systemd units.
#
# Run once as root: sudo bash install.sh <token-file>
# <token-file> holds a registration token (README.md has the command that writes
# one); this script reads it and deletes it. It may be left out when every runner
# is already registered. Safe to run again: it only fills in what is missing or
# out of date, and restarts only the units it changed (a unit running a job
# unchanged is left alone).
set -euo pipefail

RUNNER_VERSION="2.338.2"
RUNNER_SHA256="baebf204307bac6193480852732f0aba2ae97b6c5597c244f4d5558390147c40"
REPO_URL="https://github.com/analyticendeavors/omnigent-ae"
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
fi

work="$(mktemp -d)"
cleanup() {
  rm -rf "$work"
  [ -z "$TOKEN_FILE" ] || rm -f "$TOKEN_FILE"
}
trap cleanup EXIT

# 1. The user: no sudo, not in docker, not in the owner's group.
if ! id "$RUNNER_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$RUNNER_HOME" --shell /bin/bash --user-group "$RUNNER_USER"
fi
if [ "$(id -nG "$RUNNER_USER")" != "$RUNNER_USER" ]; then
  echo "error: ${RUNNER_USER} must belong to its own group only (no sudo, docker or the owner's group); it is in: $(id -nG "$RUNNER_USER")" >&2
  exit 1
fi
chmod 0750 "$RUNNER_HOME"

# 2. No other home directory may be readable or searchable by others: the runner
# user could read it.
for d in /home/*/; do
  d="${d%/}"
  [ "$d" = "$RUNNER_HOME" ] && continue
  [ -d "$d" ] || continue
  mode="$(stat -c %a "$d")"
  if [ $((8#$mode & 8#005)) -ne 0 ]; then
    echo "error: ${d} is readable or searchable by others (mode ${mode}); run: chmod o-rwx ${d}" >&2
    exit 1
  fi
done

# 3. What the python job would otherwise apt-install on every run.
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends shellcheck webp curl ca-certificates git

# 4. The job-started hook. Each runner has its own HOME, so this clears only its own
# tools: no job inherits another job's, and no job loses a tool to a neighbour.
hooks="${RUNNER_HOME}/hooks"
mkdir -p "$hooks"
cat >"${hooks}/job-started.sh" <<'HOOK'
#!/usr/bin/env bash
# Runs before every job on this runner (ACTIONS_RUNNER_HOOK_JOB_STARTED).
set -euo pipefail
rm -rf "${HOME:?}/.local/bin"
mkdir -p "${HOME}/.local/bin"
# Leftovers of earlier jobs; anything younger than a minute belongs to this job.
if [ -n "${RUNNER_TEMP:-}" ] && [ -d "$RUNNER_TEMP" ]; then
  find "$RUNNER_TEMP" -mindepth 1 -maxdepth 1 -mmin +1 -exec rm -rf {} +
fi
HOOK
chmod 0755 "${hooks}/job-started.sh"
chown -R "${RUNNER_USER}:${RUNNER_USER}" "$hooks"

# 5. The units first (so a changed unit marks every runner as changed).
cat >"${work}/ghrunner.slice" <<'UNIT'
[Unit]
Description=CI box runners (omnigent-ae)

[Slice]
MemoryMax=32G
CPUQuota=1200%
UNIT

# ProtectHome=tmpfs hides every home directory; BindPaths puts the runner user's
# back. A job can reach the runner home, /usr, /opt and a private /tmp, nothing else.
cat >"${work}/ghrunner@.service" <<UNIT
[Unit]
Description=GitHub Actions runner %i (omnigent-ae)
After=network-online.target
Wants=network-online.target

[Service]
User=${RUNNER_USER}
Environment=HOME=${RUNNER_HOME}/home-%i
WorkingDirectory=${RUNNER_HOME}/runner-%i
ExecStart=${RUNNER_HOME}/runner-%i/run.sh
Restart=always
RestartSec=10
Nice=10
Slice=ghrunner.slice
ProtectHome=tmpfs
BindPaths=${RUNNER_HOME}
ProtectSystem=full
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
UNIT

units_changed=0
for u in ghrunner.slice ghrunner@.service; do
  if ! cmp -s "${work}/${u}" "/etc/systemd/system/${u}"; then
    install -m 0644 "${work}/${u}" "/etc/systemd/system/${u}"
    units_changed=1
  fi
done
[ "$units_changed" -eq 0 ] || systemctl daemon-reload

# 6. Five runner instances. changed[i]=1 means "restart this unit at the end".
archive="${work}/runner.tar.gz"
fetched=0
fetch_runner() {
  [ "$fetched" -eq 0 ] || return 0
  curl --fail --silent --show-error --location --max-time 300 --retry 3 --output "$archive" \
    "https://github.com/actions/runner/releases/download/v${RUNNER_VERSION}/actions-runner-linux-x64-${RUNNER_VERSION}.tar.gz"
  echo "${RUNNER_SHA256}  ${archive}" | sha256sum --check --strict -
  fetched=1
}

declare -a changed
for i in $(seq 1 "$COUNT"); do
  changed[i]="$units_changed"
  dir="${RUNNER_HOME}/runner-${i}"
  home="${RUNNER_HOME}/home-${i}"
  install -d -m 0700 -o "$RUNNER_USER" -g "$RUNNER_USER" "$home"

  have=""
  if [ -x "${dir}/bin/Runner.Listener" ]; then
    have="$(runuser -u "$RUNNER_USER" -- "${dir}/bin/Runner.Listener" --version 2>/dev/null || true)"
  fi
  # Runners update themselves, so a newer version than the pin is normal: reinstall only a
  # missing runner or one older than the pin, never downgrade (that would stop a unit mid-job).
  oldest="$(printf '%s\n%s\n' "${have:-0}" "$RUNNER_VERSION" | sort -V | head -n 1)"
  if [ -z "$have" ] || [ "$oldest" != "$RUNNER_VERSION" ]; then
    fetch_runner
    # A running unit is stopped for the swap and started again below.
    systemctl stop "ghrunner@${i}.service" 2>/dev/null || true
    mkdir -p "$dir"
    tar -xzf "$archive" -C "$dir"
    chown -R "${RUNNER_USER}:${RUNNER_USER}" "$dir"
    "${dir}/bin/installdependencies.sh" >/dev/null
    changed[i]=1
  fi

  # Registered means both files exist; anything less is configured again.
  if [ ! -f "${dir}/.runner" ] || [ ! -f "${dir}/.credentials" ]; then
    if [ -z "$RUNNER_TOKEN" ]; then
      echo "error: runner-${i} is not registered and no token file was given" >&2
      exit 1
    fi
    systemctl stop "ghrunner@${i}.service" 2>/dev/null || true
    (cd "$dir" && runuser -u "$RUNNER_USER" -- ./config.sh --unattended --replace --url "$REPO_URL" \
      --token "$RUNNER_TOKEN" --name "ae-box-${i}" --labels "self-hosted,ae-box" --work _work)
    changed[i]=1
  fi

  # Point the runner at the hook (rewrite the one line, keep the rest of .env).
  touch "${dir}/.env"
  grep -v '^ACTIONS_RUNNER_HOOK_JOB_STARTED=' "${dir}/.env" >"${dir}/.env.new" || true
  echo "ACTIONS_RUNNER_HOOK_JOB_STARTED=${hooks}/job-started.sh" >>"${dir}/.env.new"
  if ! cmp -s "${dir}/.env.new" "${dir}/.env"; then
    changed[i]=1
  fi
  mv "${dir}/.env.new" "${dir}/.env"
  chown "${RUNNER_USER}:${RUNNER_USER}" "${dir}/.env"
done

# 7. Enable all; start or restart only what changed or is not running.
for i in $(seq 1 "$COUNT"); do
  unit="ghrunner@${i}.service"
  systemctl enable "$unit" >/dev/null 2>&1
  if [ "${changed[i]}" -eq 1 ]; then
    systemctl restart "$unit"
  elif ! systemctl is-active --quiet "$unit"; then
    systemctl start "$unit"
  fi
done

echo "installed: ${COUNT} runners (ae-box-1..${COUNT}), enabled at boot. Check: systemctl list-units 'ghrunner@*'"
echo "They take jobs only once CI_BOX_RUNNER is on and CI_BOX_AUTHORS is set (see README.md)."
