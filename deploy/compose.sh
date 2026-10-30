#!/usr/bin/env bash
# The one compose command for the box: upstream's compose file plus the AE
# overlay, with both environment files, from wherever this checkout lives.
# Phase 6, 2026-09-23.
#
#   deploy/compose.sh up -d          # start or update (what the systemd unit runs)
#   deploy/compose.sh pull           # fetch the tag named in config/server.env
#   deploy/compose.sh logs -f omnigent
#   deploy/compose.sh exec -T postgres pg_dump ...   # what backup-omnigent.sh runs
#   deploy/compose.sh down           # stop (the unit's ExecStop)
#
# Why a wrapper: compose interpolates ${POSTGRES_PASSWORD}, ${OMNIGENT_IMAGE_TAG}
# and ${OMNIGENT_PORT} from the process environment or the project's .env,
# never from a service's env_file, and the project directory is upstream's
# deploy/docker/. Typing the two --env-file flags and the two -f flags by hand,
# every time, is how a deploy ends up with a different tag than the unit runs.
#
# Environment files, in order (a later file wins on a repeated key):
#   config/server.env            settings, copied from config/server.env.example
#   /secure/env/omnigent-ae.env  secrets, written by deploy/pull-secrets.sh, or
#                                config/secrets.env whenever that exists
#                                (README.md "Quick start", 2026-10-22).
#                                AE_SERVER_SECRETS names any other file.
# Both are readable by root and by <user> (640 root:<user>), so the same command
# works under sudo for the system unit and unprivileged for a look at the logs
# when <user> is in the docker group.
#
# Compose files, in order: upstream's, deploy/docker-compose.override.yml, and
# config/compose.local.yml when it exists (this machine's own additions, such
# as the per-user host networks; config/compose.local.yml.example).
set -euo pipefail

# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../scripts/_lib.sh"

SERVER_SETTINGS="${AE_SERVER_SETTINGS:-$ROOT/config/server.env}"
# config/secrets.env, when it exists, wins over /secure: a try-out checkout on a
# machine that also runs the full deploy must never start on that deploy's
# secrets (found 2026-10-22, walking the Quick start on the box).
SERVER_SECRETS="${AE_SERVER_SECRETS:-/secure/env/omnigent-ae.env}"
if [ -z "${AE_SERVER_SECRETS:-}" ] && [ -e "$ROOT/config/secrets.env" ]; then
  SERVER_SECRETS="$ROOT/config/secrets.env"
fi
# The overlay's env_file reads these (compose interpolates ${AE_SERVER_SETTINGS}
# from the process environment), so it follows this checkout or the override.
export AE_SERVER_SETTINGS="$SERVER_SETTINGS" AE_SERVER_SECRETS="$SERVER_SECRETS"
UPSTREAM_COMPOSE="$UPSTREAM_DIR/deploy/docker/docker-compose.yaml"
OVERRIDE="$ROOT/deploy/docker-compose.override.yml"
LOCAL_COMPOSE="$ROOT/config/compose.local.yml"

[ -r "$SERVER_SETTINGS" ] || die "cannot read $SERVER_SETTINGS. Copy config/server.env.example to config/server.env and fill in its REQUIRED block (README.md, Quick start; deploy/README.md, step 3 for the full deploy)."
[ -r "$SERVER_SECRETS" ] || die "cannot read $SERVER_SECRETS. For a try-out, write config/secrets.env with POSTGRES_PASSWORD=<random> (README.md, Quick start); for the full deploy, run deploy/pull-secrets.sh (deploy/README.md, step 1)."
[ -f "$UPSTREAM_COMPOSE" ] || die "missing $UPSTREAM_COMPOSE; run: git submodule update --init"

files=(-f "$UPSTREAM_COMPOSE" -f "$OVERRIDE")
[ -f "$LOCAL_COMPOSE" ] && files+=(-f "$LOCAL_COMPOSE")

# Say when the checkout's secrets file is the one in effect: once a try-out
# grows into the full deploy, a forgotten config/secrets.env would otherwise
# shadow /secure without a word (review of PR 258, 2026-10-22).
if [ "$SERVER_SECRETS" = "$ROOT/config/secrets.env" ]; then
  note "compose.sh: secrets from $SERVER_SECRETS (delete it, or set AE_SERVER_SECRETS, to use /secure/env/omnigent-ae.env)" >&2
fi

# The project name decides which containers a command touches, and upstream's
# compose file names it `omnigent` for every checkout. A second checkout on a
# machine that already runs the stack (a Quick start beside the full deploy)
# would recreate the running server on its own data directory, or stop it, so
# refuse a lifecycle command when the running project was started from another
# checkout's overlay (review of PR 258, 2026-10-22).
project="${COMPOSE_PROJECT_NAME:-omnigent}"
lifecycle=""
prev=""
for arg in "$@"; do
  case "$prev" in -p | --project-name) project="$arg" ;; esac
  case "$arg" in --project-name=*) project="${arg#--project-name=}" ;; esac
  case "$arg" in up | down | start | stop | restart | rm | create | run) lifecycle="${lifecycle:-$arg}" ;; esac
  prev="$arg"
done
if [ -n "$lifecycle" ]; then
  # Any one container from this checkout is enough: on the box the server and
  # Postgres were last created from two different checkouts (2026-10-22).
  owners="$(docker ps -a --filter "label=com.docker.compose.project=$project" \
    --format '{{.Label "com.docker.compose.project.config_files"}}' 2>/dev/null || true)"
  if [ -n "$owners" ] && ! grep -qF -- "$OVERRIDE" <<<"$owners"; then
    die "compose project '$project' already runs from another checkout ($(head -n 1 <<<"$owners")); '$lifecycle' here would replace or stop it. For a second stack, prefix every compose.sh command with COMPOSE_PROJECT_NAME=<another name> and give it its own OMNIGENT_PORT (README.md, Quick start)."
  fi
  # Per-user host networks live in config/compose.local.yml since 2026-10-22;
  # a machine upgraded from the overlay that declared them, without that file,
  # would recreate the server off them and cut those hosts off. Only the
  # default project: a renamed try-out never carried them.
  if [ "$project" = omnigent ] && [ ! -f "$LOCAL_COMPOSE" ] && [ -n "$(docker network ls -q --filter name=omnigent-host- 2>/dev/null)" ]; then
    warn "omnigent-host-* networks exist but $LOCAL_COMPOSE does not, so the server joins none of them and those host containers lose it; copy config/compose.local.yml.example with each user's name (deploy/README.md, \"Second user's host (container)\")"
  fi
fi

exec docker compose \
  --env-file "$SERVER_SETTINGS" \
  --env-file "$SERVER_SECRETS" \
  "${files[@]}" \
  "$@"
