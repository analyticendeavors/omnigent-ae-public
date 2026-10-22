#!/usr/bin/env bash
# Export the commits between the pinned tag and upstream/ HEAD into patches/ and
# update upstream.lock. With --check, export to a temp dir instead and fail if
# the committed patches/ or the lock differ (what CI runs).
# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

CHECK=0
case "${1:-}" in
  --check) CHECK=1 ;;
  "") ;;
  *) die "usage: $0 [--check]" ;;
esac

load_lock
ensure_history
cd "$UPSTREAM_DIR" || exit

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# --zero-commit, --no-signature and --full-index drop the three things that
# would otherwise differ between machines: the From: object id, the git
# version trailer, and the abbreviated blob ids on `index` lines, whose length
# git picks from the object count of the checkout (7 characters here, 8 on the
# CI runner's deeper fetch; caught by the first non-empty series, 2026-09-23).
git format-patch --quiet --zero-commit --no-signature --full-index -o "$tmp" "${UPSTREAM_SHA}..HEAD" >/dev/null

shopt -s nullglob
exported=("$tmp"/*.patch)
count="${#exported[@]}"
head="$(git rev-parse HEAD)"

if [ "$CHECK" -eq 1 ]; then
  fail=0
  committed=("$PATCH_DIR"/*.patch)
  if [ "${#committed[@]}" -ne "$count" ]; then
    note "patches/ holds ${#committed[@]} file(s) but the export has $count"
    fail=1
  fi
  if ! diff -r -u --exclude=README.md "$PATCH_DIR" "$tmp"; then
    fail=1
  fi
  if [ "$head" != "$AE_BRANCH_SHA" ]; then
    note "upstream/ HEAD $head differs from upstream.lock AE_BRANCH_SHA $AE_BRANCH_SHA"
    fail=1
  fi
  if [ "$count" != "$PATCH_COUNT" ]; then
    note "upstream.lock PATCH_COUNT $PATCH_COUNT differs from the export count $count"
    fail=1
  fi
  [ "$fail" -eq 0 ] || die "patches/ or upstream.lock drifted from upstream/; run 'just patches-export' and commit the result"
  note "patches ok: $count patch(es) match upstream/ HEAD $head"
  exit 0
fi

rm -f "$PATCH_DIR"/*.patch
if [ "$count" -gt 0 ]; then
  cp "${exported[@]}" "$PATCH_DIR"/
fi
set_lock AE_BRANCH_SHA "$head"
set_lock PATCH_COUNT "$count"
note "exported $count patch(es) to patches/; upstream.lock now says AE_BRANCH_SHA=$head PATCH_COUNT=$count"
