#!/usr/bin/env bash
# Move the pin to a newer upstream release tag, re-apply the series, re-export it.
# Stops at the first conflict and never resolves one itself; the procedure and
# the reasons behind each step are in docs/UPSTREAM-SYNC.md.
# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

usage() {
  cat >&2 <<EOM
usage: scripts/sync-upstream.sh vX.Y.Z           start a sync to that upstream release tag
       scripts/sync-upstream.sh --finish vX.Y.Z  after resolving a conflict (git -C upstream am --continue)
EOM
  exit 2
}

FINISH=0
case "${1:-}" in
  --finish) FINISH=1; shift ;;
esac
NEW_TAG="${1:-}"
[[ "$NEW_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage

load_lock
cd "$UPSTREAM_DIR" || exit

if [ "$FINISH" -eq 0 ]; then
  # Only before a start: by --finish the start has already rewritten the lock
  # and moved the pointer, so the repo is never clean then (2026-10-18).
  [ -z "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ] || die "the repo has uncommitted changes; commit or stash them first"
  [ -z "$(git status --porcelain)" ] || die "upstream/ has local changes; commit or stash them first"
  [ "$NEW_TAG" != "$UPSTREAM_TAG" ] || die "already pinned to $UPSTREAM_TAG"

  # The submodule's origin is the fork, which never receives upstream's new
  # tags on its own: fetch the tag from the real upstream, then push it to the
  # fork so CI (which only talks to origin) can fetch it later.
  git remote get-url upstream >/dev/null 2>&1 || git remote add upstream "$UPSTREAM_REPO"
  note "fetching $NEW_TAG from $UPSTREAM_REPO"
  git fetch --depth=50 upstream "refs/tags/${NEW_TAG}:refs/tags/${NEW_TAG}"
  new_sha="$(git rev-parse --verify "refs/tags/${NEW_TAG}^{commit}")"
  note "pushing $NEW_TAG to the fork"
  # A refused tag push (some sandboxed sessions' git proxies allow branch
  # pushes only) is not fatal: CI and the scripts fetch the tag commit by sha
  # from the pushed ae/ branch when the tag is missing (_lib.sh).
  if ! git push origin "refs/tags/${NEW_TAG}:refs/tags/${NEW_TAG}"; then
    note "warning: could not push $NEW_TAG to the fork; push it later with:"
    note "  git -C upstream push origin refs/tags/${NEW_TAG}:refs/tags/${NEW_TAG}"
  fi

  git checkout -q --detach "$new_sha"
  set_lock UPSTREAM_TAG "$NEW_TAG"
  set_lock UPSTREAM_SHA "$new_sha"
  note "upstream/ is at $NEW_TAG ($new_sha); applying the series"

  if ! "$ROOT/scripts/apply-patches.sh"; then
    cat >&2 <<EOM

The series stopped applying. Resolve the conflict in upstream/ (git add the files,
then git -C upstream am --continue), then run:
  scripts/sync-upstream.sh --finish $NEW_TAG
EOM
    exit 1
  fi
fi

if [ -d "$(git rev-parse --git-dir)/rebase-apply" ]; then
  die "git am is still in progress in upstream/; finish it (git -C upstream am --continue) or abort it first"
fi
# export-patches.sh exports commits only, so an uncommitted fix in upstream/ would be lost.
[ -z "$(git status --porcelain)" ] || die "upstream/ has uncommitted changes; commit them to the series first"

"$ROOT/scripts/export-patches.sh"
set_lock AE_BRANCH "ae/${NEW_TAG}"
git -C "$ROOT" config -f .gitmodules submodule.upstream.branch "ae/${NEW_TAG}"
git -C "$ROOT" submodule sync -q -- upstream
git -C "$ROOT" add .gitmodules upstream.lock patches upstream
load_lock

cat <<EOM

Synced to $NEW_TAG. What remains is deliberate and stays manual:
  1. git -C upstream push origin HEAD:refs/heads/${AE_BRANCH}
  2. just upstream-smoke image        (and, once they exist: test web-check ext-build ext-doctor)
  3. bump requires_omnigent and the omnigent pins in packages/*/pyproject.toml (later phases)
  4. git commit -m "Sync upstream to ${NEW_TAG}" and open the PR "chore(upstream): ${NEW_TAG}"
  5. after the merge: git tag ae-${NEW_TAG}.1 && git push origin ae-${NEW_TAG}.1
EOM
