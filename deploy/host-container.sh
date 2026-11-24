#!/usr/bin/env bash
# One user's OmniGent host container on the box: build it, run it, sign it in
# (2026-10-20, deploy/README.md "Second user's host (container)").
#
#   deploy/host-container.sh member build      # the host and egress images, tagged like the server
#   deploy/host-container.sh member up -d      # start or update (what the user unit runs; builds if missing)
#   deploy/host-container.sh member login      # prints the sign-in link the user opens
#   deploy/host-container.sh member logs -f host
#   deploy/host-container.sh member ps
#   deploy/host-container.sh member shell      # a bash shell inside, as the container's user
#   deploy/host-container.sh member stop | down
#   deploy/host-container.sh member sync-config [--dry-run]   # the shared AE skills, rules, MCP and settings
#
# Anything after the user that is not build, login, shell or sync-config goes to
# `docker compose` for the project omnigent-host-<user>, like compose.sh does
# for the server. As <user> (in the docker group), no sudo.
#
# Reads two settings from config/server.env, and nothing else from it:
# OMNIGENT_IMAGE_TAG (the images are tagged with the server's release, so the
# host runs the same OmniGent as the server) and OMNIGENT_OIDC_REDIRECT_URI
# (its origin is the public address the sign-in link is rewritten to).
set -euo pipefail

# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/../scripts/_lib.sh"

usage() { die "usage: deploy/host-container.sh <user> build|login|shell|<docker compose args> (under the gate only: build and login for steve, 'up -d --remove-orphans' and 'down'; shell, ps, logs and other compose commands are unsupported)"; }

[ $# -ge 2 ] || usage
HOST_USER="$1"
shift
[[ "$HOST_USER" =~ ^[a-z][a-z0-9-]{0,30}$ ]] || die "user '$HOST_USER' must be lower-case letters, digits and dashes (it names the containers, network and volume)"

# sync-config (2026-10-28): the shared AE setup into the user's volume. It reads
# nothing from the server's settings and never touches the compose project, so it
# runs before those are needed (deploy/host-config-sync.sh).
if [ "$1" = sync-config ]; then
  shift
  exec "$(dirname "${BASH_SOURCE[0]}")/host-config-sync.sh" "$HOST_USER" "$@"
fi

SERVER_SETTINGS="${AE_SERVER_SETTINGS:-$ROOT/config/server.env}"
COMPOSE_FILE="$ROOT/deploy/host-container/docker-compose.yml"
CONTEXT="$ROOT/deploy/host-container"
NETWORK="omnigent-host-$HOST_USER"
PROJECT="omnigent-host-$HOST_USER"
USER_ENV="$ROOT/config/hosts/$HOST_USER.env"

[ -r "$SERVER_SETTINGS" ] || die "cannot read $SERVER_SETTINGS; the host images are tagged with the server's OMNIGENT_IMAGE_TAG from it (deploy/README.md, step 3)."

setting() { sed -n "s/^$1=//p" "$SERVER_SETTINGS" | tail -n 1 | tr -d '"'"'"; }

TAG="$(setting OMNIGENT_IMAGE_TAG)"
[ -n "$TAG" ] || die "OMNIGENT_IMAGE_TAG is not set in $SERVER_SETTINGS"
export OMNIGENT_IMAGE_TAG="$TAG" AE_HOST_USER="$HOST_USER"
if [ -r "$USER_ENV" ]; then
  export AE_HOST_ENV_FILE="$USER_ENV"
fi

# Root hardening: with the root-owned gate installed, the two lifecycle commands the unit runs go
# through its fixed verbs, and build goes through `steve build` (steve only). Everything else
# (login, shell, ps, exec) has no verb. The else branches are the old direct calls and are removed
# after the cutover.
AE_GATE="${AE_GATE:-/usr/local/sbin/ae-gate}"
compose() {
  if [ -x "$AE_GATE" ]; then
    case "$*" in
      "up -d --remove-orphans") sudo -n "$AE_GATE" stack-up "$PROJECT" ;;
      "down") sudo -n "$AE_GATE" stack-stop "$PROJECT" ;;
      *) die "host-container.sh $HOST_USER $*: the gate has no verb for this (only 'up -d --remove-orphans' and 'down')" ;;
    esac
  else
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE" "$@"
  fi
}

build() {
  load_lock
  local head
  head="$(git -C "$UPSTREAM_DIR" rev-parse HEAD)"
  [ "$head" = "$AE_BRANCH_SHA" ] \
    || die "upstream/ is at $head, but upstream.lock pins $AE_BRANCH_SHA; run git submodule update --init so the host matches the server"
  # The checkout can sit ahead of the running server (a deferred or rolled-back
  # deploy leaves it newer), so the pin must also be the one the server's
  # release tag was cut with.
  local tagged
  tagged="$(git -C "$ROOT" show "refs/tags/$TAG:upstream.lock" 2>/dev/null | sed -n 's/^AE_BRANCH_SHA=//p')" \
    || tagged=""
  [ -n "$tagged" ] || die "cannot read upstream.lock at tag $TAG in $ROOT (git fetch --tags?); cannot check the host would match the server"
  [ "$tagged" = "$AE_BRANCH_SHA" ] \
    || die "the server runs $TAG (upstream pin $tagged) but this checkout pins $AE_BRANCH_SHA; wait for the deploy to settle, or check out $TAG, so the host matches the server"
  note "building omnigent-ae-host:$TAG from upstream $UPSTREAM_TAG with $PATCH_COUNT AE patches"
  if [ -x "$AE_GATE" ]; then
    [ "$HOST_USER" = steve ] || die "the gate builds the host images for steve only; no verb for $HOST_USER"
    sudo -n "$AE_GATE" steve build "$TAG" >/dev/null
  else
    docker build -q -f "$UPSTREAM_DIR/deploy/docker/Dockerfile" --target host \
      -t "omnigent-ae-host-base:$TAG" "$UPSTREAM_DIR" >/dev/null
    docker build -q -f "$CONTEXT/Dockerfile" --build-arg "BASE=omnigent-ae-host-base:$TAG" \
      -t "omnigent-ae-host:$TAG" "$CONTEXT" >/dev/null
    docker build -q -f "$CONTEXT/egress.Dockerfile" -t "omnigent-ae-host-egress:$TAG" "$CONTEXT" >/dev/null
  fi
  note "built omnigent-ae-host:$TAG and omnigent-ae-host-egress:$TAG"
}

images_present() {
  docker image inspect "omnigent-ae-host:$TAG" "omnigent-ae-host-egress:$TAG" >/dev/null 2>&1
}

# The sign-in: `omnigent login` against the internal URL, inside the
# container, as its user. The server answers with a /auth/login?ticket=...
# path on that internal URL, which only the container can reach; the ticket
# lives in the server process, so the same path on the public origin (the
# Cloudflare name, where the Microsoft callback lands) completes it, and the
# CLI's poll over the internal network picks the login up. The link is good
# for five minutes; whoever signs in with it owns the host.
login_stream() {
  if [ -x "$AE_GATE" ]; then
    sudo -n "$AE_GATE" steve login
  else
    compose exec -T -e PYTHONUNBUFFERED=1 host omnigent login http://omnigent:8000
  fi
}

login() {
  local public
  public="$(setting OMNIGENT_OIDC_REDIRECT_URI | sed -E 's#^(https?://[^/]+).*#\1#')"
  [ -n "$public" ] || die "OMNIGENT_OIDC_REDIRECT_URI is not set in $SERVER_SETTINGS; the sign-in link needs the public origin"
  # Root hardening: under the gate, `steve login` checks the container is running and runs the
  # same login inside it (steve only). The else arm is the old direct call, removed after the cutover.
  if [ -x "$AE_GATE" ]; then
    [ "$HOST_USER" = steve ] || die "the gate signs in the steve host only; no verb for $HOST_USER"
  else
    compose ps --status running --services 2>/dev/null | grep -qx host \
      || die "the host container is not running; start it first: deploy/host-container.sh $HOST_USER up -d"
  fi
  note "signing in the $HOST_USER host; the link below is good for five minutes"
  login_stream 2>&1 \
    | while IFS= read -r line; do
        case "$line" in
          *"/auth/login?ticket="*)
            note ""
            note "Open this link in the user's own browser and sign in with their Microsoft account:"
            note "  $public/auth/login?ticket=${line##*/auth/login?ticket=}"
            note "" ;;
          *) note "$line" ;;
        esac
      done
}

case "$1" in
  build)
    build ;;
  login)
    login ;;
  shell)
    compose exec host bash -l ;;
  up)
    # The gate has no network inspect verb; stack-up checks the snapshot's networks itself.
    [ -x "$AE_GATE" ] || docker network inspect "$NETWORK" >/dev/null 2>&1 \
      || die "network $NETWORK does not exist. It belongs to the server stack: add it to config/compose.local.yml (copy config/compose.local.yml.example; the omnigent service's networks and the top-level networks block) and run deploy/compose.sh up -d, which recreates the server container"
    if [ -x "$AE_GATE" ]; then
      # Under the gate images are never built on every start: build when the release tag changed
      # since the last build, or when stack-up fails (a missing image), then retry once.
      built_file="${XDG_STATE_HOME:-$HOME/.local/state}/ae-host-container/built-$HOST_USER"
      mkdir -p "$(dirname "$built_file")"
      if [ "$(cat "$built_file" 2>/dev/null || true)" != "$TAG" ]; then
        build
        printf '%s\n' "$TAG" >"$built_file"
      fi
      compose "$@" || {
        note "stack-up failed; building the host images for $TAG and trying once more"
        build
        printf '%s\n' "$TAG" >"$built_file"
        compose "$@"
      }
    else
      images_present || build
      compose "$@"
    fi ;;
  *)
    compose "$@" ;;
esac
