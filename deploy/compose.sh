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
#   /secure/env/omnigent-ae.env  secrets, written by deploy/pull-secrets.sh
# Both are readable by root and by reid (640 root:reid), so the same command
# works under sudo for the system unit and unprivileged for a look at the logs
# when reid is in the docker group.
set -euo pipefail

# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../scripts/_lib.sh"

SERVER_SETTINGS="${AE_SERVER_SETTINGS:-$ROOT/config/server.env}"
SERVER_SECRETS="${AE_SERVER_SECRETS:-/secure/env/omnigent-ae.env}"
# The overlay's env_file reads these (compose interpolates ${AE_SERVER_SETTINGS}
# from the process environment), so it follows this checkout or the override.
export AE_SERVER_SETTINGS="$SERVER_SETTINGS" AE_SERVER_SECRETS="$SERVER_SECRETS"
UPSTREAM_COMPOSE="$UPSTREAM_DIR/deploy/docker/docker-compose.yaml"
OVERRIDE="$ROOT/deploy/docker-compose.override.yml"

[ -r "$SERVER_SETTINGS" ] || die "cannot read $SERVER_SETTINGS. Copy config/server.env.example to config/server.env and fill in the tag and the tailnet URL (deploy/README.md, step 3)."
[ -r "$SERVER_SECRETS" ] || die "cannot read $SERVER_SECRETS. Run deploy/pull-secrets.sh first (deploy/README.md, step 1); if /secure is not mounted, see home-server/secure-volume/README.md in agents-and-automation."
[ -f "$UPSTREAM_COMPOSE" ] || die "missing $UPSTREAM_COMPOSE; run: git submodule update --init"

exec docker compose \
  --env-file "$SERVER_SETTINGS" \
  --env-file "$SERVER_SECRETS" \
  -f "$UPSTREAM_COMPOSE" \
  -f "$OVERRIDE" \
  "$@"
