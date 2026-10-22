#!/usr/bin/env bash
# Refuse a tree where upstream.lock, .gitmodules, the checked-out submodule and
# patches/ disagree. Cheap, and first in CI, so every other job can trust the lock.
# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

load_lock

head="$(git -C "$UPSTREAM_DIR" rev-parse HEAD)"
[ "$head" = "$AE_BRANCH_SHA" ] || die "upstream/ is at $head but upstream.lock AE_BRANCH_SHA is $AE_BRANCH_SHA; run 'git submodule update --init' to match the pointer, or 'just patches-export' to update the lock"

shopt -s nullglob
patches=("$PATCH_DIR"/*.patch)
[ "${#patches[@]}" -eq "$PATCH_COUNT" ] || die "patches/ holds ${#patches[@]} .patch file(s) but PATCH_COUNT is $PATCH_COUNT; run 'just patches-export'"

if [ "$PATCH_COUNT" -eq 0 ] && [ "$UPSTREAM_SHA" != "$AE_BRANCH_SHA" ]; then
  die "PATCH_COUNT is 0 but AE_BRANCH_SHA differs from UPSTREAM_SHA; an empty series must pin the tag commit itself"
fi

url="$(git -C "$ROOT" config -f .gitmodules submodule.upstream.url || true)"
branch="$(git -C "$ROOT" config -f .gitmodules submodule.upstream.branch || true)"
[ "$url" = "$AE_FORK" ] || die ".gitmodules url is '$url' but upstream.lock AE_FORK is '$AE_FORK'"
[ "$branch" = "$AE_BRANCH" ] || die ".gitmodules branch is '$branch' but upstream.lock AE_BRANCH is '$AE_BRANCH'"

note "lock ok: $UPSTREAM_TAG ($UPSTREAM_SHA) + $PATCH_COUNT patch(es) = $AE_BRANCH @ $AE_BRANCH_SHA"
