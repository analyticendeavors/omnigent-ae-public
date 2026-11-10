"""The box-side cleanups: every safety check, on tmp_path trees only (2026-10-29).

Nothing here touches the real /tmp or the real Claude roots; docker is a fake.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ae_omni_policies import box_cleanup, box_health_scan

NOW = time.time()
DAY = 86400
ME = os.getuid()
# Built from two parts so the leak scan sees no cloud-id-shaped literal (2026-10-29).
UUID_A = "3f2a1c00-1111-4222-8333-" + "444455556666"
UUID_B = "9a8b7c00-1111-4222-8333-" + "444455556666"
SECRET = "ZQX-client-acme"


def put(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def age(path: Path, days: float) -> None:
    stamp = NOW - days * DAY
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def age_tree(root: Path, days: float) -> None:
    for dirpath, dirs, files in os.walk(root, topdown=False):
        for name in [*files, *dirs]:
            if not os.path.islink(os.path.join(dirpath, name)):
                age(Path(dirpath) / name, days)
    age(root, days)


def session(root: Path, uuid: str = UUID_A, project: str = f"-{SECRET}", days: float = 5) -> Path:
    path = root / project / uuid
    put(path / "tasks" / "out.txt")
    age_tree(path, days)
    age(path.parent, days)
    return path


def scratch(root: Path, **kw: object) -> box_cleanup.Plan:
    return box_cleanup.plan_claude_scratch(
        [str(root)],
        now=NOW,
        stale_days=2,
        uid=kw.pop("uid", ME),  # type: ignore[arg-type]
        budget=box_health_scan.Budget(10_000, 30),
        clock=kw.pop("clock", box_cleanup.Clock()),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def run(plan: box_cleanup.Plan, root: Path) -> box_cleanup.Outcome:
    return box_cleanup.run_plan(plan, box_cleanup.Remover([str(root)], ME))


# ── preflight and roots ─────────────────────────────────────────────────────


def test_running_as_root_is_refused() -> None:
    with pytest.raises(box_cleanup.Refused):
        box_cleanup.preflight(geteuid=lambda: 0)
    box_cleanup.preflight(geteuid=lambda: 1000)


def test_a_root_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    session(real)
    link = tmp_path / "link"
    link.symlink_to(real)
    assert box_cleanup.check_root(str(real))
    assert not box_cleanup.check_root(str(link))
    assert not box_cleanup.check_root("relative/path")
    assert scratch(link).count == 0


# ── claude_scratch ──────────────────────────────────────────────────────────


def test_an_old_session_is_removed_and_a_fresh_one_kept(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    old = session(root, UUID_A)
    fresh = session(root, UUID_B, days=0.1)
    plan = scratch(root)
    assert plan.count == 1 and plan.skipped["recent"] == 1
    outcome = run(plan, root)
    assert outcome.removed == 1 and not old.exists() and fresh.exists()


def test_a_deep_file_changed_recently_keeps_the_session(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    deep = path / "a" / "b" / "c" / "late.txt"
    put(deep)
    for folder in (deep.parent, deep.parent.parent, deep.parent.parent.parent):
        age(folder, 5)
    age(deep, 0.2)
    plan = scratch(root)
    assert plan.count == 0 and plan.skipped["recent"] == 1


def test_a_symlinked_candidate_is_skipped_and_its_target_survives(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    outside = tmp_path / "outside"
    put(outside / "keep.txt")
    age_tree(outside, 9)
    (root / f"-{SECRET}").mkdir(parents=True)
    (root / f"-{SECRET}" / UUID_A).symlink_to(outside)
    plan = scratch(root)
    assert plan.count == 0 and plan.skipped["symlink"] == 1
    assert (outside / "keep.txt").exists()


def test_a_symlink_inside_a_tree_is_removed_without_following_it(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    outside = tmp_path / "outside"
    put(outside / "keep.txt")
    (path / "tasks" / "link").symlink_to(outside)
    age(path / "tasks" / "link", 5)
    os.utime(path / "tasks", (NOW - 5 * DAY,) * 2)
    plan = scratch(root)
    assert plan.count == 1
    assert run(plan, root).removed == 1
    assert (outside / "keep.txt").exists() and not path.exists()


def test_a_foreign_owner_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    session(root)
    plan = scratch(root, uid=ME + 1)
    assert plan.count == 0 and plan.skipped["foreign"] == 1


def test_a_git_file_at_the_top_is_a_worktree_and_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    put(path / ".git", "gitdir: elsewhere")
    age(path, 5)
    plan = scratch(root)
    assert plan.count == 0 and plan.skipped["git"] == 1


def test_names_outside_the_patterns_are_ignored(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    put(root / "bundled-skills" / UUID_A / "x")
    put(root / f"-{SECRET}" / "not-a-uuid" / "x")
    age_tree(root, 9)
    assert scratch(root).count == 0


def test_a_locked_session_survives_because_the_guard_holds_it(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    guard = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(guard, fcntl.LOCK_SH)
        # The planner did not know (an empty /proc/locks); the remover's own try fails.
        plan = scratch(root)
        assert plan.count == 1
        outcome = run(plan, root)
        assert outcome.removed == 0 and outcome.skipped["locked"] == 1 and path.exists()
    finally:
        os.close(guard)
    assert run(plan, root).removed == 1


def test_a_session_listed_in_proc_locks_is_skipped_in_the_plan(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    info = os.lstat(path)
    ident = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
    plan = scratch(root, locks=frozenset({ident}))
    assert plan.count == 0 and plan.skipped["locked"] == 1


def test_a_session_a_process_works_in_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    plan = scratch(root, targets=[str(path / "tasks")])
    assert plan.count == 0 and plan.skipped["running"] == 1
    assert box_cleanup.in_use(str(path), [str(path)])
    assert not box_cleanup.in_use(str(path), [str(path) + "-sibling"])


def test_an_inode_that_changed_between_scan_and_removal_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    plan = scratch(root)
    swapped = path.with_name("moved")
    path.rename(swapped)
    put(path / "tasks" / "new.txt")
    outcome = run(plan, root)
    assert outcome.removed == 0 and outcome.skipped["changed"] == 1
    assert (path / "tasks" / "new.txt").exists()


def test_the_item_cap_sets_partial(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    for i in range(3):
        session(root, f"3f2a1c00-1111-4222-8333-44445555666{i}")
    plan = scratch(root, clock=box_cleanup.Clock(max_items=2))
    assert plan.partial
    outcome = box_cleanup.run_plan(
        plan, box_cleanup.Remover([str(root)], ME), box_cleanup.Clock(max_items=1)
    )
    assert outcome.partial and outcome.removed <= 1


def test_an_unproven_tree_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    for i in range(3):
        session(root, f"3f2a1c00-1111-4222-8333-44445555666{i}")
    plan = box_cleanup.plan_claude_scratch(
        [str(root)],
        now=NOW,
        stale_days=2,
        uid=ME,
        budget=box_health_scan.Budget(1, 30),
        clock=box_cleanup.Clock(),
    )
    assert plan.count == 0 and plan.partial


def test_an_unreadable_folder_inside_a_session_keeps_the_session(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    locked = path / "locked"
    locked.mkdir()
    age(locked, 5)
    locked.chmod(0)
    try:
        if os.access(locked, os.R_OK):  # running as someone who can read anything
            pytest.skip("cannot make a folder unreadable here")
        plan = scratch(root)
        assert plan.count == 0 and plan.skipped["unproven"] == 1
    finally:
        locked.chmod(0o700)


# ── tmp_litter ──────────────────────────────────────────────────────────────


def litter(tmp: Path, **kw: object) -> box_cleanup.Plan:
    return box_cleanup.plan_tmp_litter(
        str(tmp),
        now=NOW,
        stale_days=2,
        uid=kw.pop("uid", ME),  # type: ignore[arg-type]
        clock=box_cleanup.Clock(),
        **kw,  # type: ignore[arg-type]
    )


def test_only_allowlisted_old_names_are_removed(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    names_old = ["sec-appr-AbC123", "sec-push-get-Zz9Yx8", "media-q1w2e3", "tmpab12cd34"]
    keep = [
        "sec-appr-AbC123-longer",  # tail is not the six-character shape
        "claude-1000",
        "pytest-of-usera",
        "systemd-private-x",
        "my-notes.txt",
        "x-review.AbC123",
    ]
    for name in names_old:
        put(tmp / name / "f")
        age_tree(tmp / name, 5)
    for name in keep:
        put(tmp / name / "f")
        age_tree(tmp / name, 5)
    put(tmp / "sec-push-Fresh1" / "f")
    plan = litter(tmp)
    assert plan.count == len(names_old) and plan.skipped["recent"] == 1
    assert plan.groups()["sec-push-*"] == 1
    outcome = box_cleanup.run_plan(plan, box_cleanup.Remover([str(tmp)], ME))
    assert outcome.removed == len(names_old)
    assert all((tmp / name).exists() for name in keep)
    assert (tmp / "sec-push-Fresh1").exists()


def test_litter_symlinks_foreign_files_and_git_folders_are_kept(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    target = tmp_path / "target"
    put(target / "keep")
    tmp.mkdir()
    (tmp / "dfs-AbC123").symlink_to(target)
    put(tmp / "plex-AbC123" / ".git" / "HEAD")
    age_tree(tmp / "plex-AbC123", 5)
    put(tmp / "media-AbC123")
    age_tree(tmp / "media-AbC123", 5)
    assert litter(tmp, uid=ME + 1).skipped["foreign"] == 2
    plan = litter(tmp)
    assert plan.skipped["symlink"] == 1 and plan.skipped["git"] == 1
    assert [Path(c.path).name for c in plan.candidates] == ["media-AbC123"]
    assert (target / "keep").exists()


def test_a_debug_socket_whose_process_is_alive_is_kept(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    for name in ("dotnet-diagnostic-4242-9-socket", "clr-debug-pipe-5252-9-in"):
        put(tmp / name)
        age_tree(tmp / name, 5)
    plan = litter(tmp, alive=lambda pid: pid == 4242)
    assert [Path(c.path).name for c in plan.candidates] == ["clr-debug-pipe-5252-9-in"]
    assert plan.skipped["alive"] == 1


def test_litter_in_use_by_a_process_is_kept(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    put(tmp / "omnigent-osenv-AbC123" / "f")
    age_tree(tmp / "omnigent-osenv-AbC123", 5)
    plan = litter(tmp, targets=[str(tmp / "omnigent-osenv-AbC123")])
    assert plan.count == 0 and plan.skipped["running"] == 1


def test_the_litter_patterns_match_only_the_shapes_the_box_has() -> None:
    ok = [
        "sec-appr-AbC123",
        "sec-push-AbC123",
        "sec-push-get-AbC123",
        "sec-AbC123",
        "media-AbC123",
        "dfs-AbC123",
        "plex-AbC123",
        "omnigent-codex-skills-AbC123",
        "omnigent-osenv-AbC123",
        "dotnet-diagnostic-12-34-socket",
        "clr-debug-pipe-12-34-out",
    ]
    bad = ["sec-appr-", "sec-appr-AbC12", "sec-appr-AbC1234", "media-AbC123/x", "sec-appr-AbC12\n"]
    assert all(box_cleanup.litter_group(n) for n in ok)
    assert not any(box_cleanup.litter_group(n) for n in bad)


def test_names_a_person_would_choose_are_not_litter() -> None:
    human = [
        "media-backup",
        "media-Photos",
        "plex-backup",
        "plex-Movies",
        "dfs-backup",
        "dfs-Backup",
        "sec-report",
        "sec-Report",
        "tmpdownload",
        "tmpbackups",
        "tmp_scripts",
        "tmp_archive",
        "omnigent-osenv-backup",
        "omnigent-codex-skills-Backup",
    ]
    random = [
        "media-a1b2c3",
        "media-aBcdef",
        "sec-appr-AbCdEf",
        "sec-appr-abcde5",
        "tmp1abcdefg",
        "tmpab_cdef9",
        "omnigent-osenv-x7Yz2Q",
        "omnigent-codex-skills-aBcdef",
    ]
    assert not any(box_cleanup.litter_group(n) for n in human)
    assert all(box_cleanup.litter_group(n) for n in random)


def test_a_capital_in_position_one_alone_is_not_enough() -> None:
    assert not box_cleanup.litter_group("media-Abcdef")
    assert box_cleanup.litter_group("media-abcdeF")


def test_accepted_residual_risk_names_with_a_digit_still_match() -> None:
    # A name with a digit in the six characters (media-Photo1) or in the tmp tail
    # (tmpbackups1) looks like a random suffix, so it matches. Accepted: /tmp on the box is a
    # RAM tmpfs wiped at every boot, and systemd's tmp.conf (q /tmp 1777 root root 10d)
    # already removes any entry unread for 10 days regardless of name; the sweep only
    # shortens that to two days.
    assert box_cleanup.litter_group("media-Photo1")
    assert box_cleanup.litter_group("tmpbackups1")


# ── review_checkouts ────────────────────────────────────────────────────────


def test_a_clean_clone_is_removed_and_one_with_changes_kept(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    for name in ("leak-review.AbC123", "wc-deps-review.Zz9Yx8", "gitfile-review.Qq1Ww2"):
        put(tmp / name / ".git" / "HEAD")
        age_tree(tmp / name, 5)
    dirty = str(tmp / "wc-deps-review.Zz9Yx8")
    plan = box_cleanup.plan_review_checkouts(
        str(tmp),
        now=NOW,
        stale_days=2,
        uid=ME,
        clock=box_cleanup.Clock(),
        status=lambda path: 1 if path == dirty else 0,
    )
    assert plan.count == 2 and plan.skipped["changes"] == 1
    outcome = box_cleanup.run_plan(plan, box_cleanup.Remover([str(tmp)], ME))
    assert outcome.removed == 2 and Path(dirty).exists()


def test_a_review_dir_with_a_git_file_or_no_git_is_kept(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    put(tmp / "a-review.AbC123" / ".git", "gitdir: x")
    put(tmp / "b-review.AbC123" / "README")
    for name in ("a-review.AbC123", "b-review.AbC123"):
        age_tree(tmp / name, 5)
    plan = box_cleanup.plan_review_checkouts(
        str(tmp), now=NOW, stale_days=2, uid=ME, clock=box_cleanup.Clock()
    )
    assert plan.count == 0 and plan.skipped["git"] == 2


# ── docker, host_run and the dry run ────────────────────────────────────────


def test_docker_prune_runs_dangling_only_with_the_age_filter() -> None:
    calls: list[tuple[str, ...]] = []

    def fake(argv: object, _timeout: float) -> tuple[int, str]:
        calls.append(tuple(argv))  # type: ignore[arg-type]
        return 0, "Total reclaimed space: 1.5GB\n"

    outcome = box_cleanup.prune_docker(fake, 48)
    assert outcome.bytes_freed == 3_000_000_000
    assert calls == [
        ("docker", "image", "prune", "-f", "--filter", "until=48h"),
        ("docker", "builder", "prune", "-f", "--filter", "until=48h"),
    ]
    flat = " ".join(" ".join(c) for c in calls)
    assert "-a" not in flat.split() and "volume" not in flat and "container" not in flat


def test_host_run_refuses_root_and_removes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    with pytest.raises(box_cleanup.Refused):
        box_cleanup.host_run(
            "claude_scratch",
            roots=[str(root)],
            tmp=str(tmp_path / "tmp"),
            now=NOW,
            stale_days=2,
            max_entries=1000,
            max_seconds=10,
            geteuid=lambda: 0,
        )
    assert path.exists()


def test_host_run_removes_an_old_session_end_to_end(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    outcome = box_cleanup.host_run(
        "claude_scratch",
        roots=[str(root)],
        tmp=str(tmp_path / "tmp"),
        now=NOW,
        stale_days=2,
        max_entries=1000,
        max_seconds=10,
        proc=str(tmp_path / "proc"),
        locks_path=str(tmp_path / "locks"),
    )
    assert outcome.removed == 1 and not path.exists()


def host_scratch_run(root: Path, tmp_path: Path, expected: int | None) -> box_cleanup.Outcome:
    return box_cleanup.host_run(
        "claude_scratch",
        roots=[str(root)],
        tmp=str(tmp_path / "tmp"),
        now=NOW,
        stale_days=2,
        max_entries=1000,
        max_seconds=10,
        proc=str(tmp_path / "proc"),
        locks_path=str(tmp_path / "locks"),
        expected=expected,
    )


def test_a_plan_larger_than_confirmed_removes_only_the_oldest_confirmed(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    ids = [f"{n:08x}-1111-4222-8333-" + "444455556666" for n in (1, 2, 3)]
    days = [4, 9, 6]
    paths = [session(root, uuid=u, days=d) for u, d in zip(ids, days, strict=True)]
    outcome = host_scratch_run(root, tmp_path, expected=2)
    assert outcome.removed == 2 and outcome.skipped["over_confirmed"] == 1
    assert [p.exists() for p in paths] == [True, False, False]  # the 4-day one is kept


def test_a_plan_no_larger_than_confirmed_runs_in_full(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    outcome = host_scratch_run(root, tmp_path, expected=3)
    assert outcome.removed == 1 and not outcome.skipped["over_confirmed"] and not path.exists()


def test_no_confirmed_count_means_no_cap(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    paths = [session(root, uuid=u) for u in (UUID_A, UUID_B)]
    outcome = host_scratch_run(root, tmp_path, expected=None)
    assert outcome.removed == 2 and not any(p.exists() for p in paths)


def test_a_folder_plan_carries_its_trees_newest_mtime_not_its_own(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    # Old on top, but one file inside is newer (still past the line): 3 days, not 9.
    deep = tmp / "sec-appr-AbC123"
    put(deep / "old.txt")
    put(deep / "sub" / "new.txt")
    age_tree(deep, 9)
    age(deep / "sub" / "new.txt", 3)
    plain = tmp / "sec-appr-XyZ789"
    put(plain)
    age(plain, 5)
    plan = litter(tmp)
    newest = {Path(c.path).name: c.newest for c in plan.candidates}
    assert newest[deep.name] == pytest.approx(NOW - 3 * DAY, abs=2)
    assert newest[plain.name] == pytest.approx(NOW - 5 * DAY, abs=2)
    plan.cap(1)  # the 5-day file is older than the 3-day folder tree
    assert [Path(c.path).name for c in plan.candidates] == [plain.name]


def test_a_review_checkout_carries_its_trees_newest_mtime(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    clone = tmp / "leak-review.AbC123"
    put(clone / ".git" / "HEAD")
    put(clone / "src" / "late.txt")
    age_tree(clone, 9)
    age(clone / "src" / "late.txt", 4)
    plan = box_cleanup.plan_review_checkouts(
        str(tmp), now=NOW, stale_days=2, uid=ME, clock=box_cleanup.Clock(), status=lambda _p: 0
    )
    assert plan.count == 1
    assert plan.candidates[0].newest == pytest.approx(NOW - 4 * DAY, abs=2)


def test_equal_ages_are_removed_in_path_order_and_the_oldest_survive_the_item_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "claude"
    ids = [f"{n:08x}-1111-4222-8333-" + "444455556666" for n in (3, 1, 2, 4)]
    for u in ids[:3]:
        session(root, uuid=u, days=5)  # equal ages
    oldest = session(root, uuid=ids[3], days=9)
    plan = scratch(root)
    plan.order()
    assert [Path(c.path).name for c in plan.candidates] == [
        ids[3],
        *sorted(ids[:3]),
    ]
    monkeypatch.setattr(box_cleanup, "MAX_ITEMS", 2)
    outcome = host_scratch_run(root, tmp_path, expected=None)
    left = sorted(p.name for p in (root / f"-{SECRET}").iterdir())
    # The oldest, then the first of the equal ages by path, are the two removed.
    assert outcome.removed == 2 and not oldest.exists()
    assert left == sorted(ids[:3])[1:]


def test_the_dry_run_deletes_nothing_and_names_nothing(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    path = session(root)
    tmp = tmp_path / "tmp"
    put(tmp / "sec-appr-AbC123" / "f")
    age_tree(tmp / "sec-appr-AbC123", 5)
    counts = box_cleanup.dry_run_counts(
        roots=[str(root)],
        tmp=str(tmp),
        now=NOW,
        stale_days=2,
        max_entries=1000,
        max_seconds=10,
        proc=str(tmp_path / "proc"),
        locks_path=str(tmp_path / "locks"),
    )
    assert counts["claude_scratch"]["count"] == 1 and counts["tmp_litter"]["count"] == 1
    assert path.exists() and (tmp / "sec-appr-AbC123").exists()
    assert SECRET not in json.dumps(counts) and "AbC123" not in json.dumps(counts)


def test_an_outcome_holds_codes_and_counts_never_names(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    session(root)
    plan = scratch(root)
    # Remove the parent's permissions so rmtree fails with an error code.
    outcome = run(plan, root)
    dump = json.dumps({"s": dict(outcome.skipped), "e": outcome.errors, "r": outcome.removed})
    assert SECRET not in dump and UUID_A not in dump


# ── round 1 review of PR 395: nested proof, checkouts, mounts, roots ────────


def old_dir_with(tmp: Path, name: str, nested: str, days_nested: float) -> Path:
    path = tmp / name
    put(path / nested)
    age_tree(path, 5)
    age(path / nested, days_nested)
    return path


def test_a_stale_leftover_with_a_fresh_nested_file_is_kept(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    keep = old_dir_with(tmp, "sec-appr-AbC123", "sub/live.txt", 0.1)
    gone = old_dir_with(tmp, "sec-appr-Zz9Yx8", "sub/old.txt", 5)
    plan = litter(tmp)
    assert [Path(c.path).name for c in plan.candidates] == ["sec-appr-Zz9Yx8"]
    assert plan.skipped["recent"] == 1
    outcome = box_cleanup.run_plan(plan, box_cleanup.Remover([str(tmp)], ME))
    assert outcome.removed == 1 and keep.exists() and not gone.exists()


def test_a_file_written_after_the_scan_keeps_the_folder(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    path = old_dir_with(tmp, "media-AbC123", "sub/old.txt", 5)
    plan = litter(tmp)
    assert plan.count == 1
    put(path / "sub" / "late.txt")  # a nested write does not move the folder's own mtime
    outcome = box_cleanup.run_plan(plan, box_cleanup.Remover([str(tmp)], ME))
    assert outcome.removed == 0 and outcome.skipped["recent"] == 1 and path.exists()


def test_a_review_checkout_with_a_fresh_nested_file_is_kept(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    path = old_dir_with(tmp, "x-review.AbC123", ".git/HEAD", 5)
    put(path / "src" / "live.py")
    age_tree(path, 5)
    age(path / "src" / "live.py", 0.1)
    plan = box_cleanup.plan_review_checkouts(
        str(tmp), now=NOW, stale_days=2, uid=ME, clock=box_cleanup.Clock()
    )
    assert plan.count == 0 and plan.skipped["recent"] == 1


def test_a_clone_nested_in_a_leftover_or_a_session_is_kept(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    old_dir_with(tmp, "dfs-AbC123", "work/clone/.git/HEAD", 5)
    plan = litter(tmp)
    assert plan.count == 0 and plan.skipped["git"] == 1
    root = tmp_path / "claude"
    path = session(root)
    put(path / "repo" / ".git" / "HEAD")
    age_tree(path, 5)
    sessions = scratch(root)
    assert sessions.count == 0 and sessions.skipped["git"] == 1


def test_a_review_checkout_may_have_only_its_own_top_level_git(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    old_dir_with(tmp, "a-review.AbC123", ".git/HEAD", 5)
    old_dir_with(tmp, "b-review.AbC123", ".git/HEAD", 5)
    put(tmp / "b-review.AbC123" / "vendor" / "lib" / ".git" / "HEAD")
    age_tree(tmp / "b-review.AbC123", 5)
    plan = box_cleanup.plan_review_checkouts(
        str(tmp), now=NOW, stale_days=2, uid=ME, clock=box_cleanup.Clock()
    )
    assert [Path(c.path).name for c in plan.candidates] == ["a-review.AbC123"]
    assert plan.skipped["git"] == 1


def test_an_entry_of_another_user_or_another_filesystem_keeps_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = old_dir_with(tmp_path, "sec-AbC123", "sub/f", 5)
    assert box_cleanup.prove_tree(str(path), ME).reason(NOW - 2 * DAY) is None
    assert box_cleanup.prove_tree(str(path), ME + 1).reason(NOW - 2 * DAY) == "foreign"
    real = os.lstat

    def other_device(target: str, *a: object, **k: object) -> object:
        info = real(target, *a, **k)  # type: ignore[arg-type]
        if target == str(path):
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_mtime=info.st_mtime,
                st_blocks=info.st_blocks,
                st_uid=info.st_uid,
                st_ino=info.st_ino,
                st_dev=info.st_dev + 1,
            )
        return info

    monkeypatch.setattr(os, "lstat", other_device)
    assert box_cleanup.prove_tree(str(path), ME).reason(NOW - 2 * DAY) == "mount"


def test_a_candidate_outside_the_roots_is_never_removed(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    outside = tmp_path / "elsewhere" / "sec-appr-AbC123"
    put(outside / "f")
    info = os.lstat(outside)
    bad = box_cleanup.Candidate(
        path=str(outside), dev=info.st_dev, ino=info.st_ino, is_dir=True, cutoff=NOW
    )
    outcome = box_cleanup.Outcome()
    assert not box_cleanup.Remover([str(root)], ME).remove(bad, outcome)
    assert outcome.skipped["changed"] == 1 and outside.exists()


def run_git(cwd: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        check=True,
        capture_output=True,
    )


def test_git_status_keeps_unpushed_commits_and_stashes(tmp_path: Path) -> None:
    import shutil

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    remote = tmp_path / "remote.git"
    run_git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    clone = tmp_path / "clone"
    run_git(tmp_path, "clone", "-q", str(remote), str(clone))
    put(clone / "a.txt")
    run_git(clone, "add", "a.txt")
    run_git(clone, "commit", "-q", "-m", "one")
    assert box_cleanup.git_status_of(str(clone)) == 1  # a commit no remote has
    run_git(clone, "push", "-q", "origin", "HEAD:main")
    run_git(clone, "fetch", "-q")
    assert box_cleanup.git_status_of(str(clone)) == 0
    put(clone / "a.txt", "changed")
    run_git(clone, "stash", "-q")
    assert box_cleanup.git_status_of(str(clone)) == 1  # a stash
    run_git(clone, "stash", "drop", "-q")
    run_git(clone, "checkout", "-q", "-b", "local-only")
    put(clone / "b.txt")
    run_git(clone, "add", "b.txt")
    run_git(clone, "commit", "-q", "-m", "two")
    assert box_cleanup.git_status_of(str(clone)) == 1  # a local-only branch
    assert box_cleanup.git_status_of(str(tmp_path / "not-a-repo")) is None  # git failed: kept


# ── round 2 of the PR 395 review: live use and git again under the lock ─────


def test_a_process_that_starts_working_in_a_folder_after_the_plan_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp = tmp_path / "tmp"
    path = old_dir_with(tmp, "media-AbC123", "sub/old.txt", 5)
    plan = litter(tmp)
    assert plan.count == 1
    monkeypatch.setattr(box_cleanup, "busy_targets", lambda *_a, **_k: [str(path / "sub")])
    outcome = box_cleanup.run_plan(plan, box_cleanup.Remover([str(tmp)], ME))
    assert outcome.removed == 0 and outcome.skipped["running"] == 1 and path.exists()


def test_a_review_checkout_that_gains_a_change_after_the_plan_is_kept(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    path = old_dir_with(tmp, "x-review.AbC123", ".git/HEAD", 5)
    state = {"dirty": 0}
    plan = box_cleanup.plan_review_checkouts(
        str(tmp),
        now=NOW,
        stale_days=2,
        uid=ME,
        clock=box_cleanup.Clock(),
        status=lambda _p: state["dirty"],
    )
    assert plan.count == 1
    state["dirty"] = 1  # a stash or an edit appeared since the plan
    remover = box_cleanup.Remover([str(tmp)], ME, status=lambda _p: state["dirty"])
    outcome = box_cleanup.run_plan(plan, remover)
    assert outcome.removed == 0 and outcome.skipped["changes"] == 1 and path.exists()


def test_the_run_ignores_its_own_open_handle_when_checking_live_use(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    path = old_dir_with(tmp, "dfs-AbC123", "sub/old.txt", 5)
    fd = os.open(path, os.O_RDONLY)  # a handle of this very process, as the remover's own
    try:
        assert not box_cleanup.in_use(str(path), box_cleanup.busy_targets(ME))
    finally:
        os.close(fd)
    plan = litter(tmp)
    assert box_cleanup.run_plan(plan, box_cleanup.Remover([str(tmp)], ME)).removed == 1


def test_an_accumulated_group_with_a_git_path_is_not_stale() -> None:
    lines = [
        f"1\td\t{NOW - 5 * DAY}\t8\t-p\n",
        f"2\td\t{NOW - 5 * DAY}\t8\t-p/{UUID_A}\n",
        f"3\td\t{NOW - 5 * DAY}\t8\t-p/{UUID_A}/repo/.git\n",
        f"2\td\t{NOW - 5 * DAY}\t8\t-p/{UUID_B}\n",
    ]
    skipped: box_cleanup.Counter[str] = box_cleanup.Counter()
    groups = box_health_scan.stale_claude_groups(lines, now=NOW, stale_days=2, skipped=skipped)
    assert [key[1] for key, _f, _b, _n in groups] == [UUID_B] and skipped["git"] == 1
    scan = box_health_scan.aggregate_claude(lines, now=NOW, stale_days=2)
    assert scan.stale_sessions == 1


# ── the unattended sweep (--sweep) ──────────────────────────────────────────


def sweep_tree(tmp: Path) -> dict[str, Path]:
    """An old matching folder plus everything the sweep must keep, on a temp folder."""
    tmp.mkdir(parents=True, exist_ok=True)
    made = {
        "old": old_dir_with(tmp, "media-AbC123", "sub/old.txt", 5),
        "recent": old_dir_with(tmp, "sec-appr-Zz9Yx8", "sub/new.txt", 0.1),
        "human": old_dir_with(tmp, "media-backup", "sub/old.txt", 5),
        "git": old_dir_with(tmp, "dfs-AbC123", "work/.git/HEAD", 5),
    }
    link = tmp / "plex-AbC123"
    link.symlink_to(tmp_target(tmp))
    made["link"] = link
    return made


def tmp_target(tmp: Path) -> Path:
    target = tmp.parent / "elsewhere"
    put(target / "keep.txt")
    return target


def run_main(monkeypatch: pytest.MonkeyPatch, tmp: Path | None, argv: list[str]) -> int:
    if tmp is not None:
        real = box_cleanup.sweep
        monkeypatch.setattr(box_cleanup, "sweep", lambda **kw: real(**{**kw, "tmp": str(tmp)}))
    return box_cleanup.main(argv)


def test_sweep_runs_tmp_litter_only_with_no_expected_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake(*a: object, **kw: object) -> box_cleanup.Outcome:
        seen.append((a, kw))
        return box_cleanup.Outcome()

    monkeypatch.setattr(box_cleanup, "host_run", fake)
    assert box_cleanup.main(["--sweep"]) == 0
    ((args, kw),) = seen
    assert args == ("tmp_litter",)
    assert kw["roots"] == [] and kw["tmp"] == "/tmp" and kw["expected"] is None
    assert kw["stale_days"] == 2


def test_sweep_stale_days_never_goes_below_two(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    def fake(*_a: object, **kw: object) -> box_cleanup.Outcome:
        seen.append(kw["stale_days"])  # type: ignore[arg-type]
        return box_cleanup.Outcome()

    monkeypatch.setattr(box_cleanup, "host_run", fake)
    box_cleanup.main(["--sweep", "--stale-days", "0.01"])
    box_cleanup.main(["--sweep", "--stale-days", "7"])
    assert seen == [2.0, 7]
    assert seen[1] == 7.0 and isinstance(seen[1], float)


@pytest.mark.parametrize("argv", [[], ["--dry-run", "--sweep"], ["--stale-days", "3"]])
def test_exactly_one_of_dry_run_and_sweep_is_required(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        box_cleanup.main(argv)
    assert stop.value.code == 2


def test_sweep_removes_old_litter_keeps_the_rest_and_prints_no_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp = tmp_path / "tmp"
    made = sweep_tree(tmp)
    assert run_main(monkeypatch, tmp, ["--sweep"]) == 0
    assert not made["old"].exists()
    for key in ("recent", "human", "git"):
        assert made[key].exists(), key
    assert made["link"].is_symlink() and (tmp.parent / "elsewhere" / "keep.txt").exists()
    out = capsys.readouterr().out
    line = json.loads(out)
    assert line["removed"] == 1 and line["errors"] == {} and line["partial"] is False
    assert line["groups"] == {"media-*": 1}
    for name in [*os.listdir(tmp), "media-AbC123", "sub", "old.txt"]:
        assert name not in out


def test_sweep_exits_2_when_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(*_a: object, **_kw: object) -> box_cleanup.Outcome:
        raise box_cleanup.Refused("the cleanup refuses to run as root")

    monkeypatch.setattr(box_cleanup, "host_run", refuse)
    assert box_cleanup.main(["--sweep"]) == 2
    assert "refused" in capsys.readouterr().err


def test_sweep_exits_1_when_every_removal_errored(monkeypatch: pytest.MonkeyPatch) -> None:
    done = box_cleanup.Outcome(errors=["EACCES"])
    monkeypatch.setattr(box_cleanup, "host_run", lambda *_a, **_kw: done)
    assert box_cleanup.main(["--sweep"]) == 1
    done.removed = 1
    assert box_cleanup.main(["--sweep"]) == 0


# ── sweep() on a temp folder ────────────────────────────────────────────────


def stale(path: Path, days: float = 5) -> Path:
    age(path, days)
    return path


def run_sweep(tmp: Path) -> box_cleanup.Outcome:
    return box_cleanup.sweep(tmp=str(tmp), now=NOW)


def free_pid() -> int:
    for pid in range(4_000_000, 4_000_000 - 5000, -1):
        if not os.path.exists(f"/proc/{pid}"):
            return pid
    raise AssertionError("no unused pid found")


def test_sweep_removes_a_stale_matching_fifo_file_and_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    os.mkfifo(tmp / "media-a1b2c3")
    put(tmp / "sec-AbC123")
    monkeypatch.chdir(tmp)  # keeps the socket path under the 107-byte limit
    sock = socket.socket(socket.AF_UNIX)
    sock.bind("plex-a1b2c3")
    sock.close()
    for name in ("media-a1b2c3", "sec-AbC123", "plex-a1b2c3"):
        stale(tmp / name)
    keep = put(tmp / "media-backup")
    stale(keep)
    outcome = run_sweep(tmp)
    assert outcome.removed == 3 and not outcome.errors
    assert sorted(os.listdir(tmp)) == ["media-backup"]
    assert outcome.removed_groups == {"media-*": 1, "sec-*": 1, "plex-*": 1}


def test_sweep_keeps_a_debug_socket_of_a_live_process_and_removes_a_dead_one(
    tmp_path: Path,
) -> None:
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    live = f"dotnet-diagnostic-{os.getpid()}-1-socket"
    live_pipe = f"clr-debug-pipe-{os.getpid()}-1-in"
    dead = f"dotnet-diagnostic-{free_pid()}-1-socket"
    dead_pipe = f"clr-debug-pipe-{free_pid()}-1-out"
    for name in (live, live_pipe, dead, dead_pipe):
        stale(put(tmp / name))
    outcome = run_sweep(tmp)
    assert outcome.removed == 2 and outcome.skipped["alive"] == 2
    assert sorted(os.listdir(tmp)) == sorted([live, live_pipe])


def read_only_folder_in_litter(tmp: Path) -> Path:
    folder = tmp / "media-AbC123"
    put(folder / "locked" / "f")
    age_tree(folder, 5)
    (folder / "locked").chmod(0o555)
    return folder


def test_a_read_only_subfolder_gives_exit_0_when_something_else_was_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp = tmp_path / "tmp"
    folder = read_only_folder_in_litter(tmp)
    stale(put(tmp / "sec-AbC123"))
    try:
        assert run_main(monkeypatch, tmp, ["--sweep"]) == 0
    finally:
        (folder / "locked").chmod(0o755)
    assert not (tmp / "sec-AbC123").exists() and folder.exists()


def test_a_read_only_subfolder_gives_exit_1_when_nothing_was_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp = tmp_path / "tmp"
    folder = read_only_folder_in_litter(tmp)
    try:
        assert run_main(monkeypatch, tmp, ["--sweep"]) == 1
    finally:
        (folder / "locked").chmod(0o755)
    line = json.loads(capsys.readouterr().out)
    assert line["removed"] == 0 and "EACCES" in line["errors"]


def test_sweep_refuses_a_temp_folder_that_is_a_link_or_missing(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    for bad in (link, tmp_path / "missing"):
        with pytest.raises(box_cleanup.Refused, match="missing or not a real folder"):
            run_sweep(bad)


def test_sweep_exits_2_for_a_temp_folder_that_is_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert run_main(monkeypatch, link, ["--sweep"]) == 2
    assert "refused" in capsys.readouterr().err


def test_an_entry_swapped_after_the_check_is_skipped(tmp_path: Path) -> None:
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    stale(put(tmp / "media-AbC123"))
    plan = litter(tmp)
    # Rename the checked file away rather than deleting it: a deleted file's inode number
    # can be handed straight to the replacement, which would make the swap invisible.
    (tmp / "media-AbC123").rename(tmp / "kept-aside")
    stale(put(tmp / "media-AbC123"))
    outcome = box_cleanup.run_plan(plan, box_cleanup.Remover([str(tmp)], ME))
    assert outcome.removed == 0 and outcome.skipped["changed"] == 1
    assert (tmp / "media-AbC123").exists() and (tmp / "kept-aside").exists()


# ── each session gets its own share of the cap (2026-11-06) ─────────────────


def big_session(root: Path, uuid: str, count: int = 300) -> Path:
    path = root / f"-{SECRET}" / uuid
    for i in range(count):
        put(path / "tasks" / f"f{i}")
    age_tree(path, 5)
    age(path.parent, 5)
    return path


def share_plan(root: Path, budget: box_health_scan.Budget, share: int = 50) -> box_cleanup.Plan:
    return box_cleanup.plan_claude_scratch(
        [str(root)],
        now=NOW,
        stale_days=2,
        uid=ME,
        budget=budget,
        clock=box_cleanup.Clock(),
        group_max_entries=share,
    )


def test_a_session_over_its_share_no_longer_stops_the_planning_of_the_rest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "claude"
    big = big_session(root, UUID_A)
    small = [session(root, f"3f2a1c00-1111-4222-8333-44445555666{i}") for i in range(5)]
    budget = box_health_scan.Budget(10_000, 30)
    plan = share_plan(root, budget)
    assert not budget.exhausted
    assert plan.count == 5 and plan.partial and plan.skipped["unproven"] == 1
    assert {c.path for c in plan.candidates} == {str(p) for p in small}
    outcome = run(plan, root)
    assert outcome.removed == 5 and big.exists()


def test_the_parent_budget_running_out_still_stops_planning(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    for i in range(5):
        session(root, f"3f2a1c00-1111-4222-8333-44445555666{i}")
    budget = box_health_scan.Budget(7, 30)
    plan = share_plan(root, budget, share=100)
    assert budget.exhausted and plan.partial and plan.count < 5
    assert plan.skipped["unproven"] == 1


def test_a_session_inside_its_share_is_planned_and_the_remover_re_proves_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "claude"
    path = big_session(root, UUID_A, count=60)
    plan = share_plan(root, box_health_scan.Budget(10_000, 30), share=100)
    assert plan.count == 1 and not plan.partial
    assert run(plan, root).removed == 1 and not path.exists()


def test_the_default_share_is_what_the_remover_can_re_prove() -> None:
    assert box_health_scan.GROUP_MAX_ENTRIES == box_cleanup.WALK_ENTRIES


def test_host_run_and_the_dry_run_pass_the_share_on(tmp_path: Path) -> None:
    root = tmp_path / "claude"
    big_session(root, UUID_A)
    session(root, UUID_B)
    common: dict[str, Any] = {
        "roots": [str(root)],
        "tmp": str(tmp_path / "tmp"),
        "now": NOW,
        "stale_days": 2,
        "max_entries": 10_000,
        "max_seconds": 10,
        "proc": str(tmp_path / "proc"),
        "locks_path": str(tmp_path / "locks"),
    }
    counts = box_cleanup.dry_run_counts(**common, group_max_entries=50)
    scratch_counts = counts["claude_scratch"]
    assert scratch_counts["count"] == 1 and scratch_counts["partial"] is True
    outcome = box_cleanup.host_run(
        "claude_scratch",
        **common,
        group_max_entries=50,
    )
    assert outcome.removed == 1


def test_a_share_above_the_remover_default_is_planned_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(box_cleanup, "WALK_ENTRIES", 100)
    root = tmp_path / "claude"
    path = big_session(root, UUID_A, count=150)
    outcome = box_cleanup.host_run(
        "claude_scratch",
        roots=[str(root)],
        tmp=str(tmp_path / "tmp"),
        now=NOW,
        stale_days=2,
        max_entries=10_000,
        max_seconds=10,
        proc=str(tmp_path / "proc"),
        locks_path=str(tmp_path / "locks"),
        group_max_entries=300,
    )
    assert outcome.removed == 1 and not path.exists()
    assert not outcome.skipped.get("unproven")
