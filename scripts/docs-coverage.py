#!/usr/bin/env python3
"""Which core patches since a release no feature page covers (2026-10-22).

  scripts/docs-coverage.py --since <ref> [--until <ref>] [--git-dir <dir>]

The public release runs this before it opens the "Release <tag>" pull request
(agents-and-automation home-server/omnigent-ae/public-release/sync-public.sh
prepare), so the README's showcase and docs/features keep up with the patch
series once per public release, never on every merge. Reid asked for exactly that
cadence: the feature docs and the tour stay in sync per public release, not in dev.

Patches are matched by their file name without the number, so a renumbered patch
is neither new nor uncovered. A patch is new when that name is not in patches/ at
--since. It is covered when a page under docs/features at --until links a file
with that name, or when docs/features/uncited-patches.txt lists it with a reason
(review fixes and plumbing nobody would look for on a feature page). A patch whose
subject was reworded gets a new name and counts as new; cover the new name. Exit 1
lists the uncovered patches, exit 0 means there are none, and exit 2 means the check
could not run (a bad ref, a git failure), so a caller never reads a crash as a docs
gap. Both answers also list,
for information, the merges since --since that changed packages/ or agents/
without a patch, since those change features too and no number tracks them.

Reads committed git objects only, so it runs against a bare repository and any two
refs without a checkout; a page or a list edited but not committed does not count.
The release runs this file only at the sha256 its script pins
(DOCS_COVERAGE_SHA256 in sync-public.sh), since it runs with admin credentials: a
change here needs the new sum set there before the next public release.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCH = re.compile(r"^(?:patches/)?(\d{4})-(.+)\.patch$")
SKIP_LIST = "docs/features/uncited-patches.txt"
PAGES = "docs/features"
# Only the pages cite: the skip list sits beside them and names patches too.
PAGE_GLOB = f"{PAGES}/*.md"


class Git:
    def __init__(self, git_dir: str | None) -> None:
        self.base = ["git", "--git-dir", git_dir] if git_dir else ["git", "-C", str(ROOT)]

    def run(self, *args: str, check: bool = True) -> str:
        done = subprocess.run([*self.base, *args], capture_output=True, text=True, check=False)
        if check and done.returncode != 0:
            print(
                f"git {' '.join(args)} failed: {done.stderr.strip()}\n"
                "--since and --until must name commits whose tree holds patches/, such as "
                "release tags (git tag --list 'ae-v*'); the public release passes "
                "refs/priv-tags/<tag>.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return done.stdout

    def patches(self, ref: str) -> dict[str, tuple[str, str]]:
        """``{name without number: (number, file name)}`` of patches/ at *ref*."""
        found = {}
        for line in self.run("ls-tree", "--name-only", f"{ref}:patches/").splitlines():
            match = PATCH.match(line)
            if match:
                found[match.group(2)] = (match.group(1), line)
        return found

    def cited(self, ref: str, name: str) -> bool:
        """Whether a page links a patch file with this name, whatever its number."""
        out = self.run(
            "grep", "-l", "-F", "-e", f"-{name}.patch", ref, "--", PAGE_GLOB, check=False
        )
        return bool(out.strip())

    def subject(self, ref: str, file_name: str) -> str:
        """The commit subject a patch file carries, unwrapped, without its prefixes."""
        lines = self.run("show", f"{ref}:patches/{file_name}").splitlines()
        for i, line in enumerate(lines):
            if line.startswith("Subject:"):
                parts = [line[len("Subject:") :].strip()]
                for more in lines[i + 1 :]:
                    if not more.startswith(" "):
                        break
                    parts.append(more.strip())
                subject = " ".join(parts)
                return re.sub(r"^(\[PATCH[^\]]*\]\s*)?(\[ae-patch\]\s*)?", "", subject)
        return file_name

    def skip_list(self, ref: str) -> set[str] | None:
        """Names without number that the skip list excuses; ``None`` when it is missing."""
        done = subprocess.run(
            [*self.base, "show", f"{ref}:{SKIP_LIST}"], capture_output=True, text=True, check=False
        )
        if done.returncode != 0:
            return None
        listed = set()
        for raw in done.stdout.splitlines():
            entry = raw.split("#", 1)[0].split()
            if entry:
                match = PATCH.match(entry[0])
                listed.add(match.group(2) if match else entry[0])
        return listed

    def merges_outside_patches(self, since: str, until: str) -> list[str]:
        """First-parent subjects since *since* that changed packages/ or agents/ but no patch."""
        log = self.run(
            "log", "--first-parent", "-m", "--name-only", "--format=%x00%s", f"{since}..{until}"
        )
        out = []
        for commit in log.split("\0")[1:]:
            subject, _, rest = commit.partition("\n")
            files = [name for name in rest.splitlines() if name]
            touched = any(name.startswith(("packages/", "agents/")) for name in files)
            if touched and not any(PATCH.match(name) for name in files):
                out.append(subject)
        return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--since", required=True, help="the release the last public one was cut from")
    ap.add_argument("--until", default="HEAD", help="the release being prepared (default HEAD)")
    ap.add_argument("--git-dir", default=None, help="a repository other than this checkout's")
    a = ap.parse_args(argv)
    git = Git(a.git_dir)

    before = git.patches(a.since)
    after = git.patches(a.until)
    new = {name: after[name] for name in after if name not in before}
    skipped = git.skip_list(a.until)
    covered, listed, missing = [], [], []
    for name, (number, file_name) in sorted(new.items(), key=lambda item: item[1][0]):
        if git.cited(a.until, name):
            covered.append(number)
        elif skipped is not None and name in skipped:
            listed.append(number)
        else:
            missing.append((number, file_name))

    since = re.sub(r"^refs/(priv-tags|tags)/", "", a.since)
    print(
        f"docs coverage since {since}: {len(new)} new core patch(es), {len(covered)} on a "
        f"feature page, {len(listed)} listed in {SKIP_LIST}"
    )
    if skipped is None:
        print(f"warning: no {SKIP_LIST} at {a.until}, so no patch is excused")
    if missing:
        print(f"not covered (add each to a page under {PAGES}, or to {SKIP_LIST} with a reason):")
        for number, file_name in missing:
            print(f"  {number} {git.subject(a.until, file_name)}")
    outside = git.merges_outside_patches(a.since, a.until)
    if outside:
        print("changed outside the patch series since then (check the pages cover them):")
        for subject in outside:
            print(f"  {subject}")
    return 1 if missing else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - a crash is exit 2, never the exit 1 that means gaps
        traceback.print_exc()
        sys.exit(2)
