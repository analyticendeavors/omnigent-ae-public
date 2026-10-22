#!/usr/bin/env bash
# Carry a pull request's fork commits onto the series another PR just merged:
# rebase them onto the base branch's AE_BRANCH_SHA, re-export patches/ and the
# lock, and rewrite the PR's old patch numbers to the new ones in the lines the
# PR itself added to the docs and the justfile.
#
# Usage: scripts/rebase-patches.sh [base] [--ci]   (base defaults to origin/main)
#
# Safe to run again after a rebase conflict: resolve in upstream/, run
# `git -C upstream rebase --continue`, then rerun. The old numbers come from
# the file names still in patches/, which only the export at the end replaces.
#
# --ci (2026-10-20, for .github/workflows/patch-rebase.yml): never stops for a
# person. It first merges base into the branch: patches/*.patch, upstream.lock
# and the upstream pointer keep the branch's side (the export below rewrites
# all three), and where both sides added lines at the same spot in one of the
# DOCS files it keeps base's lines and puts the branch's after them. Any other
# conflict, in this repository or in the fork rebase, aborts the merge or the
# rebase, names the files, and exits non-zero. On success it commits the merge
# and the renumbered result, so the branch is ready to push. It never pushes.
# With PATCH_REBASE_REPORT set to a file path, it appends key=value lines there
# for the workflow: status=rebased (new commits to push) or status=current,
# map=<old>=<new>,..., fork_sha=<sha>, and one conflict=<path> per file that
# needs a person. The workflow runs main's copy of these scripts with
# PATCH_ROOT pointing at the PR checkout (scripts/_lib.sh).
# shellcheck source=scripts/_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

BASE="origin/main"
CI=0
merged=0
for arg in "$@"; do
  case "$arg" in
    --ci) CI=1 ;;
    -*) die "usage: $0 [base] [--ci]" ;;
    *) BASE="$arg" ;;
  esac
done
# docs/CHANGELOG.md joined 2026-10-21: the build diary moved there from README.md
# (https://github.com/analyticendeavors/omnigent-ae/pull/193), so every patch PR
# adds a line to it, and the bot stopped on
# https://github.com/analyticendeavors/omnigent-ae/pull/199 because it did not
# count as a doc.
DOCS=(README.md patches/README.md docs/UPSTREAM-SYNC.md docs/CHANGELOG.md justfile)
REPORT="${PATCH_REBASE_REPORT:-/dev/null}"
report() { printf '%s\n' "$*" >>"$REPORT"; }

is_doc() {
  local d
  for d in "${DOCS[@]}"; do [ "$d" = "$1" ] && return 0; done
  return 1
}

# Resolve one conflicted doc from its three index stages. Every conflict hunk
# must have an empty base side (both sides only added lines there); the result
# is base's added lines, then the branch's. Anything else returns 1 and leaves
# the file conflicted.
union_doc() {
  local path="$1" dir rc=0
  git cat-file -e ":1:$path" 2>/dev/null || return 1
  git cat-file -e ":2:$path" 2>/dev/null || return 1
  git cat-file -e ":3:$path" 2>/dev/null || return 1
  dir="$(mktemp -d)"
  git show ":1:$path" >"$dir/base"
  git show ":2:$path" >"$dir/ours"
  git show ":3:$path" >"$dir/theirs"
  git merge-file -p --diff3 --marker-size=31 -L ours -L base -L theirs \
    "$dir/ours" "$dir/base" "$dir/theirs" >"$dir/merged" || rc=$?
  # merge-file exits with the conflict count, or above 127 on an error.
  if [ "$rc" -gt 127 ]; then
    rm -rf "$dir"
    return 1
  fi
  if ! perl -ne '
    BEGIN { $st = 0 }
    if ($st == 0 && /^<{31} ours$/) { $st = 1; @o = (); @b = (); @t = (); next }
    if ($st == 1 && /^\|{31} base$/) { $st = 2; next }
    if ($st == 2 && /^={31}$/) { $st = 3; next }
    if ($st == 3 && /^>{31} theirs$/) { exit 3 if @b; print @t, @o; $st = 0; next }
    if    ($st == 0) { print }
    elsif ($st == 1) { push @o, $_ }
    elsif ($st == 2) { push @b, $_ }
    else             { push @t, $_ }
    END { exit 4 if $st != 0 }
  ' "$dir/merged" >"$dir/resolved"; then
    rm -rf "$dir"
    return 1
  fi
  cp "$dir/resolved" "$path"
  rm -rf "$dir"
  git add -- "$path"
}

# --ci only: merge base into the branch, settling the conflicts every patch PR
# gets when another one merged first, and refusing every other kind.
ci_merge_base() {
  if git merge-base --is-ancestor "$BASE" HEAD; then
    note "HEAD already contains $BASE; no merge needed"
    return 0
  fi
  note "merging $BASE into HEAD"
  git merge --no-ff --no-commit "$BASE" >/dev/null 2>&1 || true
  git rev-parse -q --verify MERGE_HEAD >/dev/null \
    || die "git merge $BASE did not start (an untracked file in the way?); run it by hand to see why"

  local path person=()
  while IFS= read -r -d '' path; do
    if [ "$path" = upstream ]; then
      git update-index --cacheinfo "160000,$(git rev-parse ":2:upstream"),upstream"
    elif [ "$path" = upstream.lock ] || [[ "$path" == patches/*.patch ]]; then
      if git cat-file -e ":2:$path" 2>/dev/null; then
        git checkout --ours -- "$path"
        git add -- "$path"
      else
        git rm --quiet --cached -- "$path"
        rm -f -- "$path"
      fi
    elif is_doc "$path" && union_doc "$path"; then
      note "kept both sides' new lines in $path ($BASE's first)"
    else
      person+=("$path")
    fi
  done < <(git diff --name-only --diff-filter=U -z)

  if [ "${#person[@]}" -gt 0 ]; then
    for path in "${person[@]}"; do report "conflict=$path"; done
    git merge --abort
    die "merging $BASE conflicts in ${person[*]}; a person has to merge it (nothing was committed)"
  fi
  [ -z "$(git diff --name-only --diff-filter=U)" ] || die "unmerged paths remain after the merge; resolve them by hand"
  git commit --quiet --no-edit -m "Merge $BASE so this branch's patches can move onto its series."
  merged=1
}

load_lock
cd "$ROOT" || exit

remote="${BASE%%/*}"
if [ "$remote" != "$BASE" ] && git remote | grep -qx "$remote"; then
  git fetch --quiet "$remote" "${BASE#*/}"
fi
git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null || die "unknown base $BASE"

if [ "$CI" -eq 1 ]; then
  [ -z "$(git status --porcelain --untracked-files=no)" ] || die "--ci needs a clean checkout; commit or stash first"
  ci_merge_base
  load_lock
fi

[ -z "$(git status --porcelain -- "${DOCS[@]}")" ] \
  || die "${DOCS[*]} have uncommitted changes; commit or stash them first (the renumber edits only lines the branch committed)"
[ ! -d "$(git -C "$UPSTREAM_DIR" rev-parse --path-format=absolute --git-path rebase-merge)" ] \
  || die "a rebase is in progress in upstream/; resolve it, run 'git -C upstream rebase --continue', then rerun"
[ -z "$(git -C "$UPSTREAM_DIR" status --porcelain)" ] || die "upstream/ has local changes; commit or stash them first"

base_lock="$(git show "$BASE:upstream.lock")" || die "$BASE has no upstream.lock"
base_field() { printf '%s\n' "$base_lock" | sed -n "s/^$1=//p"; }
base_sha="$(base_field AE_BRANCH_SHA)"
base_count="$(base_field PATCH_COUNT)"
[[ "$base_sha" =~ ^[0-9a-f]{40}$ ]] || die "$BASE:upstream.lock: AE_BRANCH_SHA is not a 40-character sha"
[ "$(base_field UPSTREAM_SHA)" = "$UPSTREAM_SHA" ] \
  || die "$BASE pins $(base_field UPSTREAM_TAG) but this branch pins $UPSTREAM_TAG; that is a sync, not a rebase (docs/UPSTREAM-SYNC.md)"

ensure_history
if ! git -C "$UPSTREAM_DIR" cat-file -e "${base_sha}^{commit}" 2>/dev/null; then
  note "fetching $base_sha ($BASE's AE_BRANCH_SHA) from the fork"
  git -C "$UPSTREAM_DIR" fetch --quiet origin "$base_sha"
fi
if ! git -C "$UPSTREAM_DIR" merge-base --is-ancestor "$UPSTREAM_SHA" "$base_sha" 2>/dev/null; then
  git -C "$UPSTREAM_DIR" fetch --quiet --deepen=$((base_count + 5)) origin
fi

head="$(git -C "$UPSTREAM_DIR" rev-parse HEAD)"
fork_point="$(git -C "$UPSTREAM_DIR" merge-base HEAD "$base_sha")"
if [ "$fork_point" = "$base_sha" ]; then
  note "upstream/ already sits on $BASE's AE_BRANCH_SHA ${base_sha:0:9}; no rebase needed"
elif [ "$CI" -eq 1 ] && [ "$fork_point" = "$head" ]; then
  # A branch that changed only docs under patches/ has no fork commits; it
  # just moves up to the base's series.
  note "upstream/ HEAD ${head:0:9} has no commits of its own; moving it to ${base_sha:0:9}"
  git -C "$UPSTREAM_DIR" checkout --quiet --detach "$base_sha"
else
  own="$(git -C "$UPSTREAM_DIR" rev-list --count "$fork_point..HEAD")"
  [ "$own" -gt 0 ] || die "upstream/ HEAD ${head:0:9} has no commits of its own past $BASE's series"
  note "rebasing $own commit(s) from ${fork_point:0:9} onto ${base_sha:0:9} ($BASE's AE_BRANCH_SHA)"
  if ! git -C "$UPSTREAM_DIR" rebase --quiet --onto "$base_sha" "$fork_point"; then
    if [ "$CI" -eq 1 ]; then
      while IFS= read -r f; do report "conflict=upstream/$f"; done \
        < <(git -C "$UPSTREAM_DIR" diff --name-only --diff-filter=U)
      git -C "$UPSTREAM_DIR" rebase --abort
      die "the fork rebase onto ${base_sha:0:9} conflicts; a person has to rebase it (the merge commit is local only)"
    fi
    die "rebase stopped on a conflict; resolve it in upstream/, run 'git -C upstream rebase --continue', then rerun this recipe"
  fi
fi

shopt -s nullglob
before=()
for f in "$PATCH_DIR"/*.patch; do before+=("$(basename "$f")"); done
in_base=" $(git ls-tree --name-only "$BASE" patches/ | sed -n 's|^patches/||p' | tr '\n' ' ')"

"$(dirname "${BASH_SOURCE[0]}")/export-patches.sh"

# The branch's own patches are the export's last files, past the base's count.
# Each keeps its slug (format-patch builds it from the commit subject), so the
# old number is the pre-export file with the same slug that the base lacks.
map=""
prev=""
first=1
after=("$PATCH_DIR"/*.patch)
for f in "${after[@]:base_count}"; do
  name="$(basename "$f")"
  new="${name:0:4}"
  slug="${name:5}"
  old=""
  for b in "${before[@]}"; do
    [ "${b:5}" = "$slug" ] || continue
    case "$in_base" in *" $b "*) continue ;; esac
    [ -z "$old" ] || die "two patches named *-$slug before the export; renumber the docs by hand"
    old="${b:0:4}"
  done
  # The first patch's entry in patches/README.md ends "after `<the base's last
  # patch>`", which no map entry covers (issue 214). A first patch new to
  # patches/ has no old predecessor, so nothing moves.
  if [ "$first" -eq 1 ] && [ -n "$old" ] && [ "$((10#$old - 1))" -ne "$base_count" ]; then
    prev="$(printf '%04d=%04d' $((10#$old - 1)) "$base_count")"
  fi
  first=0
  if [ -z "$old" ]; then
    note "$name is new to patches/; no old number to rewrite"
  elif [ "$old" != "$new" ]; then
    note "patch $old is now $new"
    map="${map:+$map,}$old=$new"
  fi
done

if [ -z "$map" ]; then
  note "no patch number changed; nothing to rewrite"
else
  # Only lines the branch added since it left $BASE are rewritten, so a number
  # the base's own docs use for a different patch is never touched. One pass
  # with the whole map, so a chain like 0110=0111,0111=0112 cannot cascade.
  merge_base="$(git merge-base "$BASE" HEAD)"
  for doc in "${DOCS[@]}"; do
    lines="$(git diff -U0 "$merge_base" HEAD -- "$doc" \
      | sed -n 's/^@@ -[0-9,]* +\([0-9]*\)\(,\([0-9]*\)\)\{0,1\} @@.*/\1 \3/p' \
      | awk '{n = ($2 == "") ? 1 : $2; for (i = 0; i < n; i++) printf "%d,", $1 + i}')"
    [ -n "$lines" ] || continue
    # The predecessor ("after" then the number, also wrapped onto the next
    # line) moves in the same substitution as the map, so neither feeds the
    # other, and only in patches/README.md.
    doc_prev=""
    [ "$doc" != patches/README.md ] || doc_prev="$prev"
    MAP="$map" PREV="$doc_prev" LINES="$lines" perl -i -pe '
      BEGIN {
        %m = split /[=,]/, $ENV{MAP}; %l = map { $_ => 1 } split /,/, $ENV{LINES};
        ($po, $pn) = split /=/, $ENV{PREV};
        $po //= ""; $wrap = 0;
      }
      if ($l{$.}) {
        my $start = $wrap;
        s{(\bafter\s+`?|^\s*`?(?=\d{4}\b))?\b(\d{4})\b}{
          my ($p, $n) = ($1 // "", $2);
          my $pred = defined $1 && ($p =~ /after/ || $start) && $n eq $po;
          $p . ($pred ? $pn : exists $m{$n} ? $m{$n} : $n)
        }ge;
        s/\bP(\d+)\b/my $k = sprintf("%04d", $1); exists $m{$k} ? "P" . ($m{$k} + 0) : "P$1"/ge;
        $wrap = /\bafter\s*$/;
      } else {
        $wrap = 0;
      }
    ' "$doc"
  done
  note "renumbered ($map) in the lines this branch added to ${DOCS[*]}"
  [ -z "$prev" ] || note "moved the \"after ${prev%=*}\" note to ${prev#*=} in patches/README.md"
fi

if [ "$CI" -eq 1 ]; then
  git add -A -- patches upstream.lock upstream "${DOCS[@]}"
  # status=rebased means the branch has new commits to push: the merge
  # alone counts (main may have moved only its docs).
  if ! git diff --cached --quiet; then
    said="${map//=/ to }"
    git commit --quiet -m "Move this branch's patches onto $BASE's series${map:+, renumbering ${said//,/, }}."
    report "status=rebased"
  elif [ "$merged" -eq 1 ]; then
    note "the merge was all it took; the series already sat on $BASE's"
    report "status=rebased"
  else
    note "nothing to do; the branch already contains $BASE and sits on its series"
    report "status=current"
  fi
  report "map=$map"
  report "fork_sha=$(git -C "$UPSTREAM_DIR" rev-parse HEAD)"
  exit 0
fi

[ -n "$map" ] || exit 0
note "next: review 'git diff', commit patches/, upstream.lock, the upstream pointer and the docs together,"
note "force-push the fork topic branch (git -C upstream push --force-with-lease origin HEAD:<topic>), and fix the PR title and body"
