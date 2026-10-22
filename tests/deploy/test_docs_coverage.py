"""scripts/docs-coverage.py: core patches a public release must show (2026-10-22).

A throwaway repository shaped like this one (patches/, docs/features, the skip
list, a package) gets a release tag, then a second round of changes; the script
must name only the new patch that no page links and no skip-list line covers.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "docs-coverage.py"


def _patch(subject: str) -> str:
    return f"From 0 Mon Sep 17 00:00:00 2001\nSubject: [PATCH 1/1] [ae-patch] {subject}\n\n---\n"


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args], check=True, capture_output=True, text=True
        ).stdout

    def write(self, rel: str, text: str) -> None:
        path = self.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def coverage(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--git-dir", str(self.path / ".git"), "--since", "v1"],
            capture_output=True,
            text=True,
            check=False,
        )


def _released(tmp_path: Path) -> Repo:
    repo = Repo(tmp_path)
    repo.write("patches/0001-ae-patch-One.patch", _patch("One thing"))
    repo.write("patches/0002-ae-patch-Two.patch", _patch("Two things"))
    repo.write("docs/features/page.md", "[one](../../patches/0001-ae-patch-One.patch)\n")
    repo.commit("First release")
    repo.git("tag", "v1")
    return repo


def test_names_only_the_new_patch_no_page_or_skip_line_covers(tmp_path: Path) -> None:
    repo = _released(tmp_path)
    # Two is renumbered, not new; Three gets a page, Four a skip line, Five nothing.
    repo.git("mv", "patches/0002-ae-patch-Two.patch", "patches/0006-ae-patch-Two.patch")
    for number, name in (("0003", "Three"), ("0004", "Four"), ("0005", "Five")):
        repo.write(f"patches/{number}-ae-patch-{name}.patch", _patch(f"{name} things"))
    repo.write(
        "docs/features/page.md",
        "[one](../../patches/0001-ae-patch-One.patch)\n[three](../../patches/0003-ae-patch-Three.patch)\n",
    )
    repo.write(
        "docs/features/uncited-patches.txt",
        "# comment\n0004-ae-patch-Four.patch  # internal\n",
    )
    repo.commit("Three patches")
    repo.write("packages/ae_omni_policies/thing.py", "x = 1\n")
    repo.commit("Admin Center: a card without a patch")

    ran = repo.coverage()
    assert ran.returncode == 1, ran.stdout + ran.stderr
    assert "3 new core patch(es), 1 on a feature page, 1 listed" in ran.stdout
    assert "  0005 Five things" in ran.stdout
    assert "Four" not in ran.stdout.split("not covered", 1)[1].split("changed outside", 1)[0]
    assert "Two things" not in ran.stdout, "a renumbered patch is not new"
    assert "  Admin Center: a card without a patch" in ran.stdout

    repo.write("docs/features/other.md", "[five](../../patches/0005-ae-patch-Five.patch)\n")
    repo.commit("Document five")
    ran = repo.coverage()
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "not covered" not in ran.stdout


def test_a_skip_line_may_leave_out_the_number(tmp_path: Path) -> None:
    repo = _released(tmp_path)
    repo.write("patches/0003-ae-patch-Three.patch", _patch("Three things"))
    repo.write("docs/features/uncited-patches.txt", "ae-patch-Three  # internal\n")
    repo.commit("Three")
    ran = repo.coverage()
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "1 listed" in ran.stdout


def test_a_page_still_covers_a_patch_the_rebase_bot_renumbered(tmp_path: Path) -> None:
    # The page links the number the patch had when it was documented.
    repo = _released(tmp_path)
    repo.write("patches/0004-ae-patch-Three.patch", _patch("Three things"))
    repo.write("docs/features/three.md", "[three](../../patches/0003-ae-patch-Three.patch)\n")
    repo.write("docs/features/uncited-patches.txt", "")
    repo.commit("Three, renumbered after its page was written")
    ran = repo.coverage()
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "1 on a feature page" in ran.stdout


def test_a_missing_skip_list_is_said_out_loud(tmp_path: Path) -> None:
    repo = _released(tmp_path)
    repo.write("patches/0003-ae-patch-Three.patch", _patch("Three things"))
    repo.commit("Three")
    ran = repo.coverage()
    assert ran.returncode == 1
    assert "warning: no docs/features/uncited-patches.txt at HEAD" in ran.stdout


def test_only_merges_without_a_patch_file_are_listed_as_outside(tmp_path: Path) -> None:
    repo = _released(tmp_path)
    repo.write("docs/features/uncited-patches.txt", "ae-patch-Three  # internal\n")
    repo.write("packages/ae_omni_policies/a.py", "a = 1\n")
    repo.write("patches/README.md", "notes\n")
    repo.commit("A package change with a patches/README.md note")
    repo.write("packages/ae_omni_policies/b.py", "b = 1\n")
    repo.write("patches/0003-ae-patch-Three.patch", _patch("Three things"))
    repo.commit("A patch change")
    ran = repo.coverage()
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert "  A package change with a patches/README.md note" in ran.stdout
    assert "  A patch change" not in ran.stdout


def test_a_ref_without_patches_is_exit_2_not_a_gap(tmp_path: Path) -> None:
    # The public release reads exit 1 as "pages are missing"; a failure must not look like that.
    repo = _released(tmp_path)
    ran = subprocess.run(
        [sys.executable, str(SCRIPT), "--git-dir", str(repo.path / ".git"), "--since", "nope"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ran.returncode == 2
    assert "must name commits whose tree holds patches/" in ran.stderr
