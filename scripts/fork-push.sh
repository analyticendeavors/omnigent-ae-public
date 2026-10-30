#!/usr/bin/env bash
# Push a topic branch to the public fork only after the leak scan passes (2026-10-28).
#
#   scripts/fork-push.sh BRANCH [-f | --force-with-lease[=<ref>:<sha>]]   (run it inside upstream/)
#
# The pre-push hook covers a push only when `just hooks-install` ran in this clone and the
# push does not say --no-verify. This script runs the same check itself, every time, and
# refuses on a hit before anything leaves the box. AE_FORK_PUSH_REMOTE names the remote (the
# fork, origin by default).
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: scripts/fork-push.sh BRANCH [git push options]" >&2
  exit 2
fi
branch="$1"
shift
# Only these options pass through: --tags, --follow-tags, --all and --mirror would push refs
# the scan below never saw (2026-10-28, second review).
for option in "$@"; do
  case "$option" in
    -f | --force-with-lease | --force-with-lease=*) ;;
    *)
      echo "fork-push: the option '$option' is refused: it could push refs that were not scanned. Allowed: -f, --force-with-lease, --force-with-lease=<ref>:<sha>." >&2
      exit 2
      ;;
  esac
done
remote="${AE_FORK_PUSH_REMOTE:-origin}"
here="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

local_sha="$(git rev-parse --verify "refs/heads/$branch")"
remote_sha="$(git ls-remote --heads "$remote" "refs/heads/$branch" | cut -f1)"
if [ -n "$remote_sha" ]; then
  git fetch --quiet "$remote" "refs/heads/$branch" || true
else
  remote_sha="0000000000000000000000000000000000000000"
fi
url="$(git remote get-url "$remote")"

printf 'refs/heads/%s %s refs/heads/%s %s\n' "$branch" "$local_sha" "$branch" "$remote_sha" |
  "$here/hooks/pre-push" "$remote" "$url"

# The scan above is this push's check; --no-verify only stops the hook running it twice.
exec git push --no-verify "$@" "$remote" "refs/heads/$branch"
