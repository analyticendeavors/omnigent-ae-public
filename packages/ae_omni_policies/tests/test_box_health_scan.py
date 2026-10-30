"""Box health scans: caps, symlinks, staleness, live sessions and the /tmp listing (2026-10-28)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ae_omni_policies import box_health_scan as scan
from ae_omni_policies.box_health_scan import Budget

DAY = 86400
NOW = time.time()
UUID_A = "3f2a1c00-1111-4222-8333-444455556666"
UUID_B = "9a8b7c00-1111-4222-8333-444455556666"


def age(path: Path, days: float) -> None:
    stamp = NOW - days * DAY
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def make(path: Path, days: float, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    age(path, days)
    return path


def make_session(
    root: Path, project: str, uuid: str, days: float, fresh_deep: bool = False
) -> Path:
    session = root / project / uuid
    (session / "tasks").mkdir(parents=True)
    old = make(session / "tasks" / "out.txt", days)
    if fresh_deep:
        make(session / "tasks" / "deep" / "new.txt", 0)
    for folder in (session / "tasks" / "deep", session / "tasks", session, session.parent):
        if folder.exists():
            age(folder, days)
    assert old.exists()
    return session


# ── walks ───────────────────────────────────────────────────────────────────


def test_the_entry_cap_marks_the_walk_partial(tmp_path: Path) -> None:
    for i in range(10):
        make(tmp_path / f"f{i}", 0)
    result = scan.walk(str(tmp_path), Budget(4, 30))
    assert result.partial and result.entries == 4


def test_the_time_cap_marks_the_walk_partial(tmp_path: Path) -> None:
    for i in range(10):
        make(tmp_path / f"f{i}", 0)
    ticks = iter(range(1000))
    budget = Budget(1000, 3, clock=lambda: float(next(ticks)))
    assert scan.walk(str(tmp_path), budget).partial


def test_symlinks_are_listed_but_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    for i in range(5):
        make(outside / f"big{i}", 0, "y" * 5000)
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    result = scan.walk(str(root), Budget(1000, 30))
    assert result.entries == 1 and not result.partial


def test_newest_mtime_anywhere_in_the_tree_counts(tmp_path: Path) -> None:
    root = tmp_path / "t"
    make(root / "a" / "old.txt", 10)
    make(root / "a" / "b" / "c" / "new.txt", 1)
    for folder in (root, root / "a", root / "a" / "b", root / "a" / "b" / "c"):
        age(folder, 10)
    result = scan.walk(str(root), Budget(1000, 30))
    assert abs(NOW - result.newest_mtime - DAY) < 5


def test_a_skipped_path_is_passed_over(tmp_path: Path) -> None:
    make(tmp_path / "keep" / "a", 0)
    make(tmp_path / "skip" / "b", 0)
    skip = frozenset({str(tmp_path / "skip")})
    assert scan.walk(str(tmp_path), Budget(100, 30), skip).entries == 2


# ── Claude roots ────────────────────────────────────────────────────────────


def claude_scan(root: Path, **kw) -> scan.TreeScan:
    return scan.scan_claude_root(str(root), now=NOW, stale_days=2, budget=Budget(10_000, 30), **kw)


def test_claude_root_counts_projects_sessions_and_stale(tmp_path: Path) -> None:
    root = tmp_path / "claude-1000"
    make_session(root, "-home-demo-repos-x", UUID_A, 5)
    make_session(root, "-home-demo-repos-x", UUID_B, 0)
    make(root / "bundled-skills" / "s.md", 9)
    result = claude_scan(root)
    assert (result.projects, result.sessions, result.stale_sessions) == (1, 2, 1)
    assert result.stale_files >= 2 and result.files > result.stale_files
    assert result.oldest_days is not None and 4.9 < result.oldest_days < 5.1
    assert not result.partial


def test_a_fresh_file_deep_inside_keeps_the_session_alive(tmp_path: Path) -> None:
    root = tmp_path / "claude-1000"
    make_session(root, "-p", UUID_A, 6, fresh_deep=True)
    assert claude_scan(root).stale_sessions == 0


def test_a_session_a_live_process_holds_is_never_stale(tmp_path: Path) -> None:
    root = tmp_path / "claude-1000"
    session = make_session(root, "-p", UUID_A, 6)
    make_session(root, "-p", UUID_B, 6)
    result = claude_scan(root, live={str(session)})
    assert result.stale_sessions == 1


def test_a_locked_session_is_never_stale(tmp_path: Path) -> None:
    root = tmp_path / "claude-1000"
    session = make_session(root, "-p", UUID_A, 6)
    info = session.stat()
    ident = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    assert claude_scan(root, locks=frozenset({ident})).stale_sessions == 0
    assert claude_scan(root).stale_sessions == 1


def test_a_cut_walk_calls_nothing_stale(tmp_path: Path) -> None:
    root = tmp_path / "claude-1000"
    for i in range(6):
        make_session(root, "-p", f"3f2a1c0{i}-1111-4222-8333-444455556666", 6)
    result = scan.scan_claude_root(str(root), now=NOW, stale_days=2, budget=Budget(8, 30))
    assert result.partial and result.stale_sessions < 6


def test_a_missing_root_and_an_unreadable_root(tmp_path: Path) -> None:
    assert not claude_scan(tmp_path / "nope").exists
    closed = tmp_path / "closed"
    closed.mkdir()
    age(closed, 3)
    closed.chmod(0)
    try:
        result = claude_scan(closed)
        if os.getuid() != 0:
            assert not result.readable and result.top_age_days is not None
            assert 2.9 < result.top_age_days < 3.1
    finally:
        closed.chmod(0o700)


def test_pytest_root_counts_runs(tmp_path: Path) -> None:
    root = tmp_path / "pytest-of-owner"
    make(root / "pytest-1" / "a.txt", 5)
    make(root / "pytest-2" / "a.txt", 0)
    for folder in (root / "pytest-1", root):
        age(folder, 5)
    result = scan.scan_pytest_root(str(root), now=NOW, stale_days=2, budget=Budget(1000, 30))
    assert (result.sessions, result.stale_sessions) == (2, 1)


def test_find_lines_from_a_container_never_return_names() -> None:
    old = NOW - 5 * DAY
    lines = [
        f"1\td\t{old}\t8\t-secret-proj\n",
        f"2\td\t{old}\t8\t-secret-proj/{UUID_A}\n",
        f"3\tf\t{old}\t8\t-secret-proj/{UUID_A}/ZQX-file.txt\n",
        f"2\td\t{NOW}\t8\t-secret-proj/{UUID_B}\n",
        "garbage line\n",
    ]
    result = scan.aggregate_claude(lines, now=NOW, stale_days=2)
    assert (result.projects, result.sessions, result.stale_sessions) == (1, 2, 1)
    assert "ZQX" not in repr(result) and "secret" not in repr(result)
    cut = scan.aggregate_claude(lines, now=NOW, stale_days=2, truncated=True)
    assert cut.partial


# ── /tmp ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "group"),
    [
        ("sec-appr-AbC123xyz", "sec-appr-*"),
        ("sec-push-AbC123xyz", "sec-push-*"),
        ("sec-other-AbC123", "sec-*"),
        ("media-abcdef12", "media-*"),
        ("tmpab12cd34", "tmp*"),
        ("tmp.notit", "other"),
        ("agents-review.AbCdEf", "*-review.*"),
        ("dotnet-diagnostic-12-34-socket", "dotnet-diagnostic-*"),
        ("systemd-private-xyz", "other"),
    ],
)
def test_tmp_names_group_by_pattern(name: str, group: str) -> None:
    assert scan.group_of(name) == group


def test_scan_tmp_counts_by_owner_without_opening_a_litter_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ubuntu's 10 day rule counts a folder's access time; listing litter would keep it alive."""
    (tmp_path / "claude-1000").mkdir()
    (tmp_path / "pytest-of-owner").mkdir()
    old_dir = tmp_path / "sec-appr-AbC123xyz"
    make(old_dir / "inside" / "f.txt", 0)
    age(old_dir, 5)
    make(tmp_path / "media-abcdef12.bin", 5, "z" * 8192)
    make(tmp_path / "fresh-thing", 0)
    opened: list[str] = []
    real = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p: (opened.append(str(p)), real(p))[1])
    result = scan.scan_tmp(str(tmp_path), now=NOW, stale_days=2, budget=Budget(10_000, 30))
    assert opened == [str(tmp_path)]
    me = os.getuid()
    groups = result.litter[me]
    assert groups["sec-appr-*"].entries == 1 and groups["sec-appr-*"].stale == 1
    assert groups["media-*"].stale == 1 and groups["media-*"].bytes >= 8192
    assert groups["other"].entries == 1 and groups["other"].stale == 0
    assert result.claude_dirs[me].endswith("claude-1000")
    assert result.pytest_dirs[me].endswith("pytest-of-owner")


# ── live sessions ───────────────────────────────────────────────────────────


def fake_proc(tmp_path: Path, root: Path) -> str:
    proc = tmp_path / "proc"
    held = make_session(root, "-p", UUID_A, 6)
    resumed = make_session(root, "-p", UUID_B, 6)
    one = proc / "101"
    (one / "fd").mkdir(parents=True)
    (one / "cwd").symlink_to(tmp_path)
    (one / "fd" / "3").symlink_to(held / "tasks")
    (one / "cmdline").write_bytes(b"node\0server.js\0")
    two = proc / "102"
    (two / "fd").mkdir(parents=True)
    (two / "cwd").symlink_to(tmp_path)
    (two / "cmdline").write_bytes(f"/usr/bin/claude\0--resume\0{UUID_B}\0".encode())
    (two / "exe").symlink_to("/usr/bin/claude")
    assert resumed.exists()
    return str(proc)


def test_live_sessions_are_found_the_way_the_guard_finds_them(tmp_path: Path) -> None:
    root = tmp_path / "claude-1000"
    proc = fake_proc(tmp_path, root)
    live = scan.live_session_dirs([str(root)], os.getuid(), proc)
    assert live == {str(root / "-p" / UUID_A), str(root / "-p" / UUID_B)}


def test_a_resume_argument_of_a_non_claude_process_is_ignored(tmp_path: Path) -> None:
    root = tmp_path / "claude-1000"
    proc = Path(fake_proc(tmp_path, root))
    (proc / "102" / "exe").unlink()
    (proc / "102" / "exe").symlink_to("/usr/bin/vim")
    (proc / "102" / "cmdline").write_bytes(f"vim\0--resume\0{UUID_B}\0".encode())
    live = scan.live_session_dirs([str(root)], os.getuid(), str(proc))
    assert live == {str(root / "-p" / UUID_A)}


def test_read_locks_parses_flock_rows(tmp_path: Path) -> None:
    table = tmp_path / "locks"
    table.write_text(
        "1: FLOCK  ADVISORY  READ 4016977 103:02:131 0 EOF\n"
        "2: POSIX  ADVISORY  WRITE 55 08:01:99 0 EOF\n"
    )
    assert scan.read_locks(str(table)) == {(0x103, 0x02, 131)}
    assert scan.read_locks(str(tmp_path / "missing")) == frozenset()


# ── enumeration itself is bounded (2026-10-28, review of PR 389) ─────────────


class CountingScandir:
    """Wraps os.scandir so a test can see how many entries were pulled from the listing."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.pulled: dict[str, int] = {}
        real = os.scandir

        def counting(path: str):
            handle = real(path)
            key = str(path)
            self.pulled.setdefault(key, 0)
            outer = self

            class Wrapped:
                def __enter__(self_inner):
                    handle.__enter__()
                    return self_inner

                def __exit__(self_inner, *args):
                    return handle.__exit__(*args)

                def __iter__(self_inner):
                    return self_inner

                def __next__(self_inner):
                    entry = next(handle)
                    outer.pulled[key] += 1
                    return entry

                def close(self_inner):
                    handle.close()

            return Wrapped()

        monkeypatch.setattr(os, "scandir", counting)


def test_the_top_listing_of_a_claude_root_stops_at_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "claude-1000"
    root.mkdir()
    for i in range(200):
        make(root / f"helper{i}", 0)
    seen = CountingScandir(monkeypatch)
    result = scan.scan_claude_root(str(root), now=NOW, stale_days=2, budget=Budget(10, 30))
    assert result.partial
    assert seen.pulled[str(root)] <= 11


def test_the_project_listing_stops_at_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "claude-1000"
    project = root / "-p"
    project.mkdir(parents=True)
    for i in range(200):
        make(project / f"x{i}", 0)
    seen = CountingScandir(monkeypatch)
    result = scan.scan_claude_root(str(root), now=NOW, stale_days=2, budget=Budget(10, 30))
    assert result.partial
    assert seen.pulled[str(project)] <= 11


def test_the_tmp_listing_stops_at_the_cap_and_says_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for i in range(300):
        make(tmp_path / f"media-{i:08d}", 0)
    seen = CountingScandir(monkeypatch)
    result = scan.scan_tmp(str(tmp_path), now=NOW, stale_days=2, budget=Budget(25, 30))
    assert result.partial
    assert seen.pulled[str(tmp_path)] <= 26
    assert sum(agg.entries for groups in result.litter.values() for agg in groups.values()) <= 25


def test_the_tmp_listing_obeys_the_seconds_cap(tmp_path: Path) -> None:
    for i in range(50):
        make(tmp_path / f"media-{i:08d}", 0)
    ticks = iter(range(10_000))
    budget = Budget(10_000, 3, clock=lambda: float(next(ticks)))
    assert scan.scan_tmp(str(tmp_path), now=NOW, stale_days=2, budget=budget).partial


@pytest.mark.parametrize(
    "name", ["claude-\u0661\u0662\u0663", "claude-12345678901", "claude-", "claude-1\n"]
)
def test_a_claude_name_that_is_not_one_to_ten_ascii_digits_is_litter(
    tmp_path: Path, name: str
) -> None:
    (tmp_path / name).mkdir()
    result = scan.scan_tmp(str(tmp_path), now=NOW, stale_days=2, budget=Budget(100, 30))
    assert not result.claude_dirs
    assert sum(a.entries for g in result.litter.values() for a in g.values()) == 1
