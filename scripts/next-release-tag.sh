#!/usr/bin/env bash
# Decide whether a commit on main gets the next ae-v* release tag, and which.
# Added 2026-09-24 for .github/workflows/release.yml, so that merging a PR is the
# whole release while Reid travels with only a laptop: the workflow tags, the
# box's auto-update timer deploys.
#
# Usage: scripts/next-release-tag.sh [--force] [--tip REF] SHA
#
# Prints three lines, ready to append to $GITHUB_OUTPUT:
#   decision=tag|skip
#   tag=<ae-vX.Y.Z.N, empty on skip>
#   reason=<one line saying why>
# Exit 0 for either decision; non-zero only when the inputs are broken.
#
# Rules, in order:
#   1. With --tip, SHA must be the commit REF points at (the current tip of
#      main); an older commit is skipped, the newer one's run tags it.
#   2. A commit that already carries any ae-v* tag is skipped (idempotent;
#      also covers a tag Reid pushed by hand).
#   3. Unless --force, something that ships must have changed since the
#      newest ae-v* tag reachable from SHA: packages/, config/, agents/,
#      deploy/Dockerfile, patches/, upstream.lock or the upstream submodule
#      pointer. A docs-only or tests-only merge makes no tag. No earlier tag
#      at all counts as a change.
#   4. The tag is ae-v<UPSTREAM_TAG without v>.<N>, UPSTREAM_TAG read from
#      upstream.lock at SHA; N is one more than the highest existing N for that
#      upstream tag across all tags, or 1 when the pin moved.
#
# Tags must be present locally (git fetch --tags) before this runs.
set -euo pipefail

SHIPPING_PATHS=(packages config agents deploy/Dockerfile patches upstream.lock upstream)

die() { printf 'next-release-tag: %s\n' "$*" >&2; exit 2; }

emit() {
  printf 'decision=%s\ntag=%s\nreason=%s\n' "$1" "$2" "$3"
  exit 0
}

force=false
tip=""
sha=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) force=true; shift ;;
    --tip) [ $# -ge 2 ] || die "--tip needs a ref"; tip="$2"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) [ -z "$sha" ] || die "one SHA only"; sha="$1"; shift ;;
  esac
done
[ -n "$sha" ] || die "usage: $0 [--force] [--tip REF] SHA"

commit="$(git rev-parse --verify --quiet "$sha^{commit}")" \
  || die "$sha is not a commit in this checkout; fetch it first"

if [ -n "$tip" ]; then
  tip_commit="$(git rev-parse --verify --quiet "$tip^{commit}")" \
    || die "$tip does not resolve; fetch it first"
  if [ "$tip_commit" != "$commit" ]; then
    emit skip "" "${commit:0:12} is no longer the tip of $tip (now ${tip_commit:0:12}); the run for the tip tags it"
  fi
fi

existing="$(git tag --points-at "$commit" --list 'ae-v*' --sort=-version:refname | sed -n 1p)"
if [ -n "$existing" ]; then
  emit skip "" "${commit:0:12} is already tagged $existing"
fi

lock="$(git show "$commit:upstream.lock" 2>/dev/null)" \
  || die "upstream.lock is missing at ${commit:0:12}"
upstream_tag="$(printf '%s\n' "$lock" | sed -n 's/^UPSTREAM_TAG=//p' | sed -n 1p)"
[[ "$upstream_tag" =~ ^v[0-9]+(\.[0-9]+)*$ ]] \
  || die "upstream.lock at ${commit:0:12} has no usable UPSTREAM_TAG (got '$upstream_tag')"
base="ae-v${upstream_tag#v}"

if [ "$force" != true ]; then
  last="$(git tag --merged "$commit" --list 'ae-v*' --sort=-version:refname | sed -n 1p)"
  if [ -n "$last" ]; then
    changed="$(git diff --name-only "$last" "$commit" -- "${SHIPPING_PATHS[@]}")"
    if [ -z "$changed" ]; then
      emit skip "" "nothing that ships changed since $last (${SHIPPING_PATHS[*]}); run the release workflow with force to tag anyway"
    fi
    since="since $last: $(printf '%s\n' "$changed" | wc -l | tr -d ' ') shipping path(s) changed"
  else
    since="no earlier ae-v* tag"
  fi
else
  since="forced"
fi

# Highest N for this upstream tag, over every tag (not only ancestors), so a
# tag on a commit off main still reserves its number.
n=0
while IFS= read -r t; do
  suffix="${t#"$base".}"
  if [[ "$suffix" =~ ^[0-9]+$ ]] && [ "$suffix" -gt "$n" ]; then
    n="$suffix"
  fi
done < <(git tag --list "$base.*")
next="$base.$((n + 1))"

emit tag "$next" "$since"
