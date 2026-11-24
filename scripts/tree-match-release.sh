#!/usr/bin/env bash
# Decide whether a commit just pushed to main may release without waiting for
# CI on main. Added 2026-11-10 for .github/workflows/release.yml.
#
# Why: CI on main reruns the whole suite on code a pull request already
# tested, then the release waits for it (3.5 to 7 minutes on the box runners).
# When the squash commit's tree is byte-for-byte the PR head's tree, that run
# can only repeat what the PR's run proved, so the release may start at once.
# CI on main still runs afterwards, as the record; its own release run then
# finds the commit tagged and does nothing. On the last 20 merges before this
# was written, 10 matched (the rest merged after main had moved on).
#
# Usage: scripts/tree-match-release.sh SHA
#   GH_REPO (owner/name) and GH_TOKEN must be set; gh and git on PATH; run
#   inside a clone of GH_REPO that has SHA.
#
# Prints two lines, ready to append to $GITHUB_OUTPUT:
#   fast=true|false
#   reason=<one line saying why>
# Exit 0 for either answer; non-zero only when the inputs are broken. A failed
# lookup answers false, so the release falls back to waiting for CI on main.
#
# fast=true only when every one of these holds:
#   1. exactly one merged pull request into main, from a branch of GH_REPO
#      itself, has SHA as its merge commit (the list can lag the merge by a
#      few seconds, so an empty answer is asked again, up to three times);
#   2. SHA's tree equals that pull request's head commit's tree;
#   3. the newest CI run (ci.yml, pull_request event) on that head completed
#      with success, and its "Record the tested tree" annotation names SHA's
#      tree. A pull request run tests GitHub's merge of the head into the base
#      as it stood then, not the head alone, so a matching head is not enough:
#      a run against an older main, or against another base before the PR was
#      retargeted, tested different code. CI is not a required check on main
#      (only `leaks` is), so this script checks it itself rather than trusting
#      the merge;
#   4. the newest `leaks` check run by GitHub Actions on that head completed
#      with success.
#
# Accepted (2026-11-10): the `leaks` result can be days old, scanned against
# the denylist of that day. CI on main still scans the pushed commits and the
# tree afterwards; the tag and the image are private, and a public release
# runs its own scan.
set -euo pipefail

die() { printf 'tree-match-release: %s\n' "$*" >&2; exit 2; }

answer() {
  printf 'fast=%s\nreason=%s\n' "$1" "$2"
  exit 0
}

[ "$#" -eq 1 ] || die "usage: tree-match-release.sh SHA"
sha="$1"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "SHA must be a full 40-character commit id, got '$sha'"
[[ "${GH_REPO:-}" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "GH_REPO must be set to owner/name, got '${GH_REPO:-}'"
git cat-file -e "${sha}^{commit}" 2>/dev/null || die "commit $sha is not in this clone"

# 1. The pull request this commit merged. SHA and GH_REPO are checked above,
# so they are safe inside the jq program.
prs=""
for attempt in 1 2 3; do
  if ! prs="$(gh api "repos/${GH_REPO}/commits/${sha}/pulls" \
      --jq ".[] | select(.merged_at != null and .merge_commit_sha == \"${sha}\" and .base.ref == \"main\" and .head.repo.full_name == \"${GH_REPO}\") | \"\\(.number) \\(.head.sha)\"")"; then
    answer false "could not list the pull requests for ${sha:0:12}; waiting for CI on main"
  fi
  [ -n "$prs" ] || [ "$attempt" -eq 3 ] || sleep "${TREE_MATCH_RETRY_SLEEP:-5}"
  [ -z "$prs" ] || break
done
count="$(printf '%s' "$prs" | grep -c . || true)"
if [ "$count" -eq 0 ]; then
  answer false "no merged pull request from this repository into main has ${sha:0:12} as its merge commit; waiting for CI on main"
fi
if [ "$count" -ne 1 ]; then
  answer false "${sha:0:12} is the merge commit of ${count} merged pull requests into main, not exactly one; waiting for CI on main"
fi
read -r pr head <<<"$prs"
[[ "$head" =~ ^[0-9a-f]{40}$ ]] || answer false "PR ${pr} reported a malformed head commit; waiting for CI on main"

# 2. Same code.
if ! git cat-file -e "${head}^{commit}" 2>/dev/null; then
  git fetch --quiet --no-tags origin "$head" 2>/dev/null || answer false "could not fetch PR ${pr} head ${head:0:12}; waiting for CI on main"
fi
merged_tree="$(git rev-parse "${sha}^{tree}")"
head_tree="$(git rev-parse "${head}^{tree}")"
if [ "$merged_tree" != "$head_tree" ]; then
  answer false "PR ${pr} merged after main moved: ${sha:0:12} differs from its head ${head:0:12}; waiting for CI on main"
fi

# 3. CI passed on the head commit, on exactly this tree.
if ! run="$(gh api "repos/${GH_REPO}/actions/workflows/ci.yml/runs?head_sha=${head}&event=pull_request&per_page=100" \
    --jq '.workflow_runs | if length == 0 then "missing" else (max_by(.id) | "\(.status) \(.conclusion) \(.check_suite_id)") end')"; then
  answer false "could not read CI runs for PR ${pr} head ${head:0:12}; waiting for CI on main"
fi
read -r ci_status ci_conclusion suite <<<"$run"
if [ "${ci_status} ${ci_conclusion:-}" != "completed success" ]; then
  answer false "newest CI run on PR ${pr} head ${head:0:12} is '${run% *}', not completed success; waiting for CI on main"
fi
[[ "$suite" =~ ^[0-9]+$ ]] || answer false "CI run on PR ${pr} head ${head:0:12} has no check suite id; waiting for CI on main"
if ! jobs="$(gh api "repos/${GH_REPO}/check-suites/${suite}/check-runs?per_page=100" \
    --jq '.check_runs[] | select(.name == "Patch series matches and applies") | .id')"; then
  answer false "could not read the CI jobs for PR ${pr} head ${head:0:12}; waiting for CI on main"
fi
tested=""
for job in $jobs; do
  [[ "$job" =~ ^[0-9]+$ ]] || continue
  if ! found="$(gh api "repos/${GH_REPO}/check-runs/${job}/annotations?per_page=100" \
      --jq '.[] | select(.title == "tested-tree") | .message')"; then
    answer false "could not read the tested tree for PR ${pr} head ${head:0:12}; waiting for CI on main"
  fi
  tested="${tested}${found:+${found}$'\n'}"
done
tested_trees="$(printf '%s' "$tested" | sort -u)"
if [ -z "$tested_trees" ]; then
  answer false "CI on PR ${pr} head ${head:0:12} recorded no tested tree (a run from before 2026-11-10); waiting for CI on main"
fi
if [ "$tested_trees" != "$merged_tree" ]; then
  answer false "CI on PR ${pr} head ${head:0:12} tested a different tree (main moved, or another base); waiting for CI on main"
fi

# 4. The leak scan passed on the head commit.
if ! leaks="$(gh api "repos/${GH_REPO}/commits/${head}/check-runs?check_name=leaks&per_page=100" \
    --jq '[.check_runs[] | select(.app.slug == "github-actions")] | if length == 0 then "missing" else (max_by(.id) | "\(.status) \(.conclusion)") end')"; then
  answer false "could not read the leaks check for PR ${pr} head ${head:0:12}; waiting for CI on main"
fi
if [ "$leaks" != "completed success" ]; then
  answer false "leaks check on PR ${pr} head ${head:0:12} is '${leaks}', not completed success; waiting for CI on main"
fi

answer true "PR ${pr} head ${head:0:12} has the same tree as ${sha:0:12}, and its CI tested exactly that tree and passed, as did leaks"
