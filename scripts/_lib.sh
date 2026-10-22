#!/usr/bin/env bash
# Shared helpers for the patch-series scripts. Sourced by the other scripts in
# this directory, never run on its own.
set -euo pipefail

# PATCH_ROOT (2026-10-20): the checkout to act on when these scripts run from a
# copy outside it. The patch rebase bot runs main's copy against a PR checkout,
# because a PR cut before the bot existed carries a script without --ci.
ROOT="${PATCH_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOCK="$ROOT/upstream.lock"
UPSTREAM_DIR="$ROOT/upstream"
# Read by the scripts that source this file, not here.
# shellcheck disable=SC2034
PATCH_DIR="$ROOT/patches"

die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
note() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

# Read upstream.lock into the environment and refuse a malformed one early,
# so a typo in the lock fails here with the field named, not three commands
# later inside git.
load_lock() {
  [ -f "$LOCK" ] || die "missing $LOCK"
  # shellcheck disable=SC1090
  . "$LOCK"
  local k
  for k in UPSTREAM_REPO UPSTREAM_TAG UPSTREAM_SHA AE_FORK AE_BRANCH AE_BRANCH_SHA PATCH_COUNT; do
    [ -n "${!k:-}" ] || die "$LOCK: $k is empty"
  done
  [[ "$UPSTREAM_SHA" =~ ^[0-9a-f]{40}$ ]] || die "$LOCK: UPSTREAM_SHA is not a 40-character sha"
  [[ "$AE_BRANCH_SHA" =~ ^[0-9a-f]{40}$ ]] || die "$LOCK: AE_BRANCH_SHA is not a 40-character sha"
  [[ "$PATCH_COUNT" =~ ^[0-9]+$ ]] || die "$LOCK: PATCH_COUNT is not a number"
  [ -e "$UPSTREAM_DIR/.git" ] || die "upstream/ is not checked out; run: git submodule update --init"
}

# The submodule is normally checked out shallow at its pointer (actions/checkout
# hands depth 1 down to submodules), so the tag commit the series applies onto
# can be absent. Fetch it from origin, the fork, with a bounded depth. The sync
# script pushes every pinned tag to the fork, but a session whose git proxy
# refuses tag pushes (2026-09-24, the v0.15.0 sync) leaves the fork without
# it; the tag commit is still an ancestor of the pushed ae/ branch, so fall
# back to fetching it by sha, which GitHub serves for any reachable commit.
ensure_tag_commit() {
  if ! git -C "$UPSTREAM_DIR" cat-file -e "${UPSTREAM_SHA}^{commit}" 2>/dev/null; then
    note "fetching $UPSTREAM_TAG from origin (this shallow checkout lacks it)"
    if ! git -C "$UPSTREAM_DIR" fetch --depth=200 origin "refs/tags/${UPSTREAM_TAG}:refs/tags/${UPSTREAM_TAG}"; then
      note "origin has no tag $UPSTREAM_TAG; fetching $UPSTREAM_SHA by sha instead"
      git -C "$UPSTREAM_DIR" fetch --depth=200 origin "$UPSTREAM_SHA"
    fi
  fi
  # --verify --quiet: without them, rev-parse echoes an unknown ref name back
  # on stdout, and a checkout with no tag refs (CI fetches the submodule by
  # commit) would compare that literal string against the sha and fail.
  local got
  got="$(git -C "$UPSTREAM_DIR" rev-parse --verify --quiet "refs/tags/${UPSTREAM_TAG}^{commit}" || true)"
  if [ -n "$got" ] && [ "$got" != "$UPSTREAM_SHA" ]; then
    die "tag $UPSTREAM_TAG resolves to $got but upstream.lock says $UPSTREAM_SHA"
  fi
}

# Exporting the series needs the commits between the tag and HEAD. A shallow
# checkout at the pointer has none of them, so deepen from the shallow boundary
# just far enough (the series plus a margin) before giving up.
ensure_history() {
  ensure_tag_commit
  if git -C "$UPSTREAM_DIR" merge-base --is-ancestor "$UPSTREAM_SHA" HEAD 2>/dev/null; then
    return 0
  fi
  if [ -f "$(git -C "$UPSTREAM_DIR" rev-parse --git-dir)/shallow" ]; then
    note "deepening the shallow checkout to reach $UPSTREAM_TAG"
    git -C "$UPSTREAM_DIR" fetch --deepen=$((PATCH_COUNT + 5)) origin
    git -C "$UPSTREAM_DIR" merge-base --is-ancestor "$UPSTREAM_SHA" HEAD 2>/dev/null && return 0
  fi
  die "upstream/ HEAD $(git -C "$UPSTREAM_DIR" rev-parse HEAD) does not descend from $UPSTREAM_TAG ($UPSTREAM_SHA)"
}

# set_lock KEY value: rewrite one line of upstream.lock in place.
set_lock() {
  local key="$1" val="$2"
  grep -q "^${key}=" "$LOCK" || die "$LOCK has no $key line"
  sed -i.bak "s|^${key}=.*|${key}=${val}|" "$LOCK"
  rm -f "$LOCK.bak"
}
