#!/usr/bin/env bash
# Apply the patch series onto the pristine upstream tag checked out in upstream/.
# No-op when the series is already applied or empty. Never resolves a conflict.
# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

load_lock
ensure_tag_commit
cd "$UPSTREAM_DIR" || exit

[ -z "$(git status --porcelain)" ] || die "upstream/ has local changes; commit or stash them first"

head="$(git rev-parse HEAD)"
if [ "$head" = "$AE_BRANCH_SHA" ]; then
  note "series already applied: upstream/ is at $AE_BRANCH ($head)"
  exit 0
fi
[ "$head" = "$UPSTREAM_SHA" ] || die "upstream/ HEAD $head is neither the pinned tag $UPSTREAM_TAG nor $AE_BRANCH; run 'git -C upstream checkout --detach $UPSTREAM_SHA' first"

shopt -s nullglob
patches=("$PATCH_DIR"/*.patch)
if [ "${#patches[@]}" -eq 0 ]; then
  note "empty series: upstream/ stays at $UPSTREAM_TAG ($head)"
  exit 0
fi

# --3way resolves against the pre-image blobs when surrounding context drifted;
# --committer-date-is-author-date keeps a re-applied history identical across
# machines apart from the committer identity; --keep-non-patch strips only the
# [PATCH n/m] tag, where plain git am strips every leading bracket and with it
# the [ae-patch] prefix (2026-10-18, the v0.18.0 sync).
if ! git am --3way --committer-date-is-author-date --keep-non-patch "${patches[@]}"; then
  cat >&2 <<EOM
error: the series stopped applying (the conflict is printed above).
  resolve:  edit the conflicted files in upstream/, git add them, then
            git -C upstream am --continue
  give up:  git -C upstream am --abort
  after resolving: scripts/export-patches.sh, then commit patches/, upstream.lock
  and the submodule pointer together.
EOM
  exit 1
fi

applied="$(git rev-list --count "${UPSTREAM_SHA}..HEAD")"
[ "$applied" = "$PATCH_COUNT" ] || die "applied $applied commit(s) but upstream.lock PATCH_COUNT is $PATCH_COUNT"
note "applied $applied patch(es): upstream/ HEAD is now $(git rev-parse HEAD)"
