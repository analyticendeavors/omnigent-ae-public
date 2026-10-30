"""The box-side cleanups behind the Box health card's buttons (2026-10-29).

Why: on 2026-10-28 the box's ``/tmp`` ran out of inodes because nothing aged Claude Code's
per-session temp folders or the test leftovers at the top of ``/tmp``. PR 1 shows the
bloat; this module removes it when a box admin presses a button, with the same two-day
rule the box's tmpfiles rule applies (``ae-claude-tmp-guard`` protects live sessions).

Four actions, run as the relay's own user (never root) on the relay's own files:

- ``claude_scratch``: ``<root>/<project>/<session uuid>`` folders under the Claude temp
  roots whose newest mtime is older than the stale age;
- ``tmp_litter``: top-level ``/tmp`` entries whose names match a fixed allowlist;
- ``review_checkouts``: ``/tmp/<name>-review.<id>`` clones with no uncommitted changes;
- ``docker_prune``: dangling images and build cache older than the stale age.

Every action makes the checks in :func:`preflight`, :func:`check_root` and
:func:`Remover.remove`: no root, no symlink-unsafe ``rmtree``, a root that is a real
folder, direct children found with ``os.scandir`` (never joined from outside names),
no symlink, an owner that is this user, the whole tree proven old, an inode that has
not changed, no process working inside, no git worktree, and a non-blocking ``flock``
that the temp guard's lock on a live session folder makes fail.

Privacy: this module names no folder, project, session or file in what it returns.
It returns counts, sizes, name-pattern groups and fixed reason words only; names stay
in its memory.

Container items are cleaned by the relay (``box_health_relay.container_clean``), which
holds the fixed ``docker exec`` templates.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ae_omni_policies import box_health_scan
from ae_omni_policies.box_health import SKIP_REASONS

_logger = logging.getLogger("ae_omni_policies.box_cleanup")

Action = Literal["claude_scratch", "tmp_litter", "review_checkouts", "docker_prune"]
ACTIONS: tuple[Action, ...] = ("claude_scratch", "tmp_litter", "review_checkouts", "docker_prune")

# Each run stops after this many top-level items or seconds and says ``partial``.
MAX_ITEMS = 5000
MAX_SECONDS = 120.0
GIT_TIMEOUT_S = 10
DOCKER_TIMEOUT_S = 300

PROJECT_RE = re.compile(r"^-[A-Za-z0-9._-]{1,200}$")
SESSION_RE = box_health_scan.UUID_RE
REVIEW_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}-review\.[A-Za-z0-9]{6,}$")

# The only top-level /tmp names tmp_litter may remove (2026-10-29, tightened to the shapes
# the box really has: mktemp's six-character suffix, never a looser tail). The group is the
# label the card already shows.
LITTER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sec-appr-*", re.compile(r"^sec-appr-[A-Za-z0-9]{6}$")),
    ("sec-push-*", re.compile(r"^sec-push-(?:get-)?[A-Za-z0-9]{6}$")),
    ("sec-*", re.compile(r"^sec-[A-Za-z0-9]{6}$")),
    ("media-*", re.compile(r"^media-[A-Za-z0-9]{6}$")),
    ("dfs-*", re.compile(r"^dfs-[A-Za-z0-9]{6}$")),
    ("plex-*", re.compile(r"^plex-[A-Za-z0-9]{6}$")),
    ("tmp*", re.compile(r"^tmp[a-z0-9_]{8}$")),
    ("omnigent-codex-skills-*", re.compile(r"^omnigent-codex-skills-[A-Za-z0-9]{6}$")),
    ("omnigent-osenv-*", re.compile(r"^omnigent-osenv-[A-Za-z0-9]{6}$")),
    ("dotnet-diagnostic-*", re.compile(r"^dotnet-diagnostic-(?P<pid>[0-9]{1,10})-[0-9]+-socket$")),
    (
        "clr-debug-pipe-*",
        re.compile(r"^clr-debug-pipe-(?P<pid>[0-9]{1,10})-[0-9]+-(?:in|out)$"),
    ),
)


def litter_group(name: str) -> tuple[str, re.Match[str]] | None:
    """The allowlisted group a top-level /tmp name belongs to; ``None`` when it matches none."""
    for group, pattern in LITTER_PATTERNS:
        if hit := pattern.match(name):
            return group, hit
    return None


class Refused(Exception):
    """The run cannot start; the message is a fixed phrase safe to post."""


@dataclass
class Candidate:
    """One thing the run may remove, as the scan saw it."""

    path: str
    dev: int
    ino: int
    is_dir: bool
    group: str = ""
    files: int = 1
    bytes: int = 0
    cutoff: float = 0.0
    """The age line the tree was proven against; proven again just before removal."""
    top_git: bool = False
    """The one ``.git`` allowed is the top-level folder (a review checkout)."""
    newest: float = 0.0
    """The newest mtime the scan proved for the item; the cap on a confirmed count keeps
    the oldest items first."""


@dataclass
class Plan:
    """What a run would remove now. Counts only leave this module."""

    candidates: list[Candidate] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    partial: bool = False

    @property
    def count(self) -> int:
        return len(self.candidates)

    @property
    def files(self) -> int:
        return sum(c.files for c in self.candidates)

    @property
    def bytes(self) -> int:
        return sum(c.bytes for c in self.candidates)

    def order(self) -> None:
        """Oldest first by proven newest mtime, ties by path, so every limit that follows
        (the item clock, the confirmed count) keeps the same, oldest items."""
        self.candidates.sort(key=lambda c: (c.newest, c.path))

    def cap(self, expected: int | None) -> None:
        """Keep only the *expected* oldest candidates; the rest stay for a later run and
        are counted under ``over_confirmed``. A plan no larger than *expected*, or no
        *expected*, is only put in order."""
        self.order()
        if expected is None or len(self.candidates) <= expected:
            return
        self.skipped["over_confirmed"] += len(self.candidates) - max(expected, 0)
        del self.candidates[max(expected, 0) :]

    def groups(self) -> dict[str, int]:
        out: Counter[str] = Counter()
        for c in self.candidates:
            if c.group:
                out[c.group] += 1
        return dict(out)


@dataclass
class Outcome:
    """What a run did."""

    removed: int = 0
    bytes_freed: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    errors: list[str] = field(default_factory=list)
    partial: bool = False

    def error(self, exc: OSError) -> None:
        code = errno.errorcode.get(exc.errno or 0, "EIO")
        if code not in self.errors and len(self.errors) < 20:
            self.errors.append(code)


def preflight(*, geteuid: Callable[[], int] = os.geteuid) -> None:
    """The checks every action makes before it touches anything.

    :raises Refused: Running as root, or a platform whose ``rmtree`` can be raced through a
        symlink.
    """
    if geteuid() == 0:
        raise Refused("the cleanup refuses to run as root")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise Refused("this platform's rmtree is not safe against symlink races")


def check_root(root: str) -> bool:
    """Whether *root* is an absolute path to a real folder (not a link) that resolves to itself."""
    if not os.path.isabs(root):
        return False
    try:
        info = os.lstat(root)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and os.path.realpath(root) == root


def busy_targets(uid: int, proc: str = "/proc") -> list[str]:
    """Where *uid*'s processes work: each one's working folder and open files. Link targets
    only; nothing is read. A candidate is in use when one of these is inside it."""
    found: set[str] = set()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return []
    me = str(os.getpid())
    for pid in pids:
        if pid == me:
            continue  # this run's own handles (the folder it has open to lock it)
        base = f"{proc}/{pid}"
        try:
            if os.stat(base).st_uid != uid:
                continue
            found.add(os.readlink(f"{base}/cwd"))
            for fd in os.listdir(f"{base}/fd"):
                with contextlib.suppress(OSError):
                    found.add(os.readlink(f"{base}/fd/{fd}"))
        except OSError:
            continue
    return sorted(found)


def in_use(path: str, targets: Sequence[str]) -> bool:
    prefix = path.rstrip("/") + "/"
    return any(t == path or t.startswith(prefix) for t in targets)


def pid_alive(pid: int, proc: str = "/proc") -> bool:
    return os.path.exists(f"{proc}/{pid}")


class Clock:
    """The run's item cap and seconds cap."""

    def __init__(
        self,
        max_items: int = MAX_ITEMS,
        max_seconds: float = MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.items_left = max_items
        self._clock = clock
        self._deadline = clock() + max_seconds

    def spend(self) -> bool:
        if self.items_left <= 0 or self._clock() > self._deadline:
            return False
        self.items_left -= 1
        return True


def _owned(info: os.stat_result, uid: int) -> bool:
    return info.st_uid == uid


WALK_ENTRIES = 100_000
WALK_SECONDS = 10.0


@dataclass
class Proof:
    """What a walk of one candidate's tree showed."""

    newest: float = 0.0
    files: int = 0
    bytes: int = 0
    partial: bool = False
    foreign: bool = False
    git: bool = False
    crossdev: bool = False

    def reason(self, cutoff: float) -> str | None:
        """The fixed word for why the tree must be kept, or ``None`` when it is proven old,
        whole, ours, one filesystem and holds no git checkout."""
        if self.foreign:
            return "foreign"
        if self.git:
            return "git"
        if self.crossdev:
            return "mount"
        if self.partial:
            return "unproven"
        if self.newest >= cutoff:
            return "recent"
        return None


def prove_tree(
    path: str, uid: int, *, top_git: bool = False, budget: box_health_scan.Budget | None = None
) -> Proof:
    """Walk *path* (symlinks listed, never followed) and report its newest mtime, its size,
    and whether any entry belongs to another user, sits on another filesystem, or is a
    ``.git`` (below the top level when *top_git*). An unreadable folder or a cap hit makes
    the walk ``partial``: its age cannot be proven."""
    proof = Proof()
    budget = budget or box_health_scan.Budget(WALK_ENTRIES, WALK_SECONDS)
    try:
        top = os.lstat(path)
    except OSError:
        proof.partial = True
        return proof
    proof.newest = top.st_mtime
    proof.bytes = top.st_blocks * 512
    stack = [(path, 0)]
    while stack:
        here, depth = stack.pop()
        try:
            handle = os.scandir(here)
        except OSError:
            proof.partial = True
            continue
        with handle:
            for entry in handle:
                if not budget.spend():
                    proof.partial = True
                    return proof
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    proof.partial = True
                    continue
                proof.files += 1
                proof.bytes += info.st_blocks * 512
                proof.newest = max(proof.newest, info.st_mtime)
                if info.st_uid != uid:
                    proof.foreign = True
                if entry.name == ".git" and not (top_git and depth == 0):
                    proof.git = True
                if info.st_dev != top.st_dev:
                    proof.crossdev = True
                    continue
                if stat.S_ISDIR(info.st_mode):
                    stack.append((entry.path, depth + 1))
    return proof


def plan_claude_scratch(
    roots: Iterable[str],
    *,
    now: float,
    stale_days: float,
    uid: int,
    budget: box_health_scan.Budget,
    clock: Clock,
    locks: frozenset[tuple[int, int, int]] = frozenset(),
    live: frozenset[str] | set[str] = frozenset(),
    targets: Sequence[str] = (),
) -> Plan:
    """Session folders ``<root>/<project>/<session uuid>`` that are provably older than the
    stale age and not in use. A walk that hits its cap leaves the folder alone (its age
    cannot be proven)."""
    plan = Plan()
    cutoff = now - stale_days * box_health_scan.DAY_S
    for root in roots:
        if not check_root(root):
            plan.skipped["symlink"] += 1 if os.path.lexists(root) else 0
            continue
        try:
            projects = os.scandir(root)
        except OSError:
            continue
        with projects:
            for project in projects:
                if not PROJECT_RE.match(project.name):
                    continue
                try:
                    pinfo = project.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(pinfo.st_mode) or not stat.S_ISDIR(pinfo.st_mode):
                    plan.skipped["symlink"] += 1
                    continue
                if not _owned(pinfo, uid):
                    plan.skipped["foreign"] += 1
                    continue
                _plan_sessions(
                    plan, project.path, cutoff, uid, budget, clock, locks, live, targets
                )
                if plan.partial:
                    return plan
    return plan


def _plan_sessions(
    plan: Plan,
    project_path: str,
    cutoff: float,
    uid: int,
    budget: box_health_scan.Budget,
    clock: Clock,
    locks: frozenset[tuple[int, int, int]],
    live: frozenset[str] | set[str],
    targets: Sequence[str],
) -> None:
    try:
        sessions = os.scandir(project_path)
    except OSError:
        return
    with sessions:
        for session in sessions:
            if not SESSION_RE.match(session.name):
                continue
            if not clock.spend():
                plan.partial = True
                return
            try:
                info = session.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                plan.skipped["symlink"] += 1
                continue
            if not _owned(info, uid):
                plan.skipped["foreign"] += 1
                continue
            ident = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
            if session.path in live or ident in locks:
                plan.skipped["locked"] += 1
                continue
            proof = prove_tree(session.path, uid, budget=budget)
            if budget.exhausted:
                plan.skipped["unproven"] += 1
                plan.partial = True
                return
            if reason := proof.reason(cutoff):
                plan.skipped[reason] += 1
                continue
            if in_use(session.path, targets):
                plan.skipped["running"] += 1
                continue
            plan.candidates.append(
                Candidate(
                    path=session.path,
                    dev=info.st_dev,
                    ino=info.st_ino,
                    is_dir=True,
                    files=proof.files + 1,
                    bytes=proof.bytes,
                    cutoff=cutoff,
                    newest=proof.newest,
                )
            )


def plan_tmp_litter(
    root: str,
    *,
    now: float,
    stale_days: float,
    uid: int,
    clock: Clock,
    targets: Sequence[str] = (),
    alive: Callable[[int], bool] = pid_alive,
) -> Plan:
    """Top-level entries of *root* that match :data:`LITTER_PATTERNS`, owned by *uid* and
    older than the stale age. A folder is never opened (its mtime is its own); a debug
    socket or pipe whose process is still alive is kept."""
    plan = Plan()
    cutoff = now - stale_days * box_health_scan.DAY_S
    if not check_root(root):
        return plan
    try:
        handle = os.scandir(root)
    except OSError:
        return plan
    with handle:
        for entry in handle:
            hit = litter_group(entry.name)
            if hit is None:
                continue
            group, match = hit
            if not clock.spend():
                plan.partial = True
                break
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            mode = info.st_mode
            if stat.S_ISLNK(mode):
                plan.skipped["symlink"] += 1
                continue
            if not (is_dir := stat.S_ISDIR(mode)) and not any(
                f(mode) for f in (stat.S_ISREG, stat.S_ISSOCK, stat.S_ISFIFO)
            ):
                continue
            if not _owned(info, uid):
                plan.skipped["foreign"] += 1
                continue
            if info.st_mtime >= cutoff:
                plan.skipped["recent"] += 1
                continue
            if "pid" in match.groupdict() and alive(int(match.group("pid"))):
                plan.skipped["alive"] += 1
                continue
            files, size = 1, 0 if is_dir else info.st_blocks * 512
            if is_dir:
                proof = prove_tree(entry.path, uid)
                if reason := proof.reason(cutoff):
                    plan.skipped[reason] += 1
                    continue
                if in_use(entry.path, targets):
                    plan.skipped["running"] += 1
                    continue
                files, size = proof.files + 1, proof.bytes
            plan.candidates.append(
                Candidate(
                    path=entry.path,
                    dev=info.st_dev,
                    ino=info.st_ino,
                    is_dir=is_dir,
                    group=group,
                    files=files,
                    bytes=size,
                    cutoff=cutoff,
                    newest=proof.newest if is_dir else info.st_mtime,
                )
            )
    return plan


GitStatus = Callable[[str], int | None]
"""Whether a clone has uncommitted changes: 0 clean, 1 changes, ``None`` could not tell."""


def _git(path: str, *args: str) -> str | None:
    """Output of one read-only ``git -C path ...`` within the timeout; ``None`` when it
    failed or timed out (the caller then keeps the checkout)."""
    try:
        done = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def git_status_of(path: str) -> int | None:
    """0 only when nothing in the clone would be lost: no uncommitted change, no stash and
    no commit that is not on a remote. 1 when something would be; ``None`` when git could
    not tell (kept, like 1)."""
    for args in (
        ("status", "--porcelain", "--ignore-submodules"),
        ("stash", "list"),
        ("log", "--branches", "--not", "--remotes", "-n", "1", "--format=%H"),
    ):
        out = _git(path, *args)
        if out is None:
            return None
        if out.strip():
            return 1
    return 0


def plan_review_checkouts(
    root: str,
    *,
    now: float,
    stale_days: float,
    uid: int,
    clock: Clock,
    targets: Sequence[str] = (),
    status: GitStatus | None = None,
) -> Plan:
    """``<name>-review.<id>`` git clones at the top of *root*, owned by *uid*, older than the
    stale age. ``.git`` must be a folder. With *status* given, a clone with changes is kept
    (``changes``); without it (the offer) every such clone is counted, since the check
    runs git once per clone."""
    plan = Plan()
    cutoff = now - stale_days * box_health_scan.DAY_S
    if not check_root(root):
        return plan
    try:
        handle = os.scandir(root)
    except OSError:
        return plan
    with handle:
        for entry in handle:
            if not REVIEW_RE.match(entry.name):
                continue
            if not clock.spend():
                plan.partial = True
                break
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                plan.skipped["symlink"] += 1
                continue
            if not _owned(info, uid):
                plan.skipped["foreign"] += 1
                continue
            if info.st_mtime >= cutoff:
                plan.skipped["recent"] += 1
                continue
            try:
                git = os.lstat(os.path.join(entry.path, ".git"))
            except OSError:
                plan.skipped["git"] += 1
                continue
            if not stat.S_ISDIR(git.st_mode):
                plan.skipped["git"] += 1
                continue
            proof = prove_tree(entry.path, uid, top_git=True)
            if reason := proof.reason(cutoff):
                plan.skipped[reason] += 1
                continue
            if in_use(entry.path, targets):
                plan.skipped["running"] += 1
                continue
            if status is not None and status(entry.path) != 0:
                plan.skipped["changes"] += 1
                continue
            plan.candidates.append(
                Candidate(
                    path=entry.path,
                    dev=info.st_dev,
                    ino=info.st_ino,
                    is_dir=True,
                    files=proof.files + 1,
                    bytes=proof.bytes,
                    cutoff=cutoff,
                    top_git=True,
                    newest=proof.newest,
                )
            )
    return plan


class Remover:
    """Removes planned candidates, checking each again right before."""

    def __init__(
        self,
        roots: Sequence[str],
        uid: int,
        clock: Clock | None = None,
        *,
        status: GitStatus | None = None,
        proc: str = "/proc",
    ) -> None:
        self._roots = tuple(r.rstrip("/") for r in roots)
        self._uid = uid
        self._clock = clock or Clock()
        self._status = status
        self._proc = proc

    def _inside_root(self, path: str) -> bool:
        return any(path.startswith(root + "/") for root in self._roots)

    def remove(self, candidate: Candidate, outcome: Outcome) -> bool:
        """Remove *candidate*; ``True`` when it is gone. A reason word or an error code is
        added to *outcome* otherwise."""
        path = candidate.path
        if not self._inside_root(path):
            outcome.skipped["changed"] += 1
            return False
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            outcome.skipped["changed"] += 1
            return False
        except OSError as exc:
            outcome.error(exc)
            return False
        if (
            (info.st_dev, info.st_ino) != (candidate.dev, candidate.ino)
            or stat.S_ISLNK(info.st_mode)
            or not _owned(info, self._uid)
        ):
            outcome.skipped["changed"] += 1
            return False
        if not candidate.is_dir:
            if info.st_mtime >= candidate.cutoff:
                outcome.skipped["recent"] += 1
                return False
            try:
                os.unlink(path)
            except OSError as exc:
                outcome.error(exc)
                return False
            return True
        fd = -1
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            held = os.fstat(fd)
            if (held.st_dev, held.st_ino) != (candidate.dev, candidate.ino):
                outcome.skipped["changed"] += 1
                return False
            try:
                # The temp guard holds a shared lock on every live session's folder, so an
                # exclusive try fails for a session that is running.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                outcome.skipped["locked"] += 1
                return False
            # The tree is proven old again now, under the lock: a file written since the
            # scan (a nested edit does not move a folder's own mtime) keeps it.
            if reason := prove_tree(path, self._uid, top_git=candidate.top_git).reason(
                candidate.cutoff
            ):
                outcome.skipped[reason] += 1
                return False
            # Live use and, for a review checkout, the git checks are repeated now too: a
            # process may have started working here, or a change been made, since the plan.
            if in_use(path, busy_targets(self._uid, self._proc)):
                outcome.skipped["running"] += 1
                return False
            if candidate.top_git and self._status is not None and self._status(path) != 0:
                outcome.skipped["changes"] += 1
                return False
            failures: list[OSError] = []
            shutil.rmtree(path, onexc=lambda _fn, _p, exc: failures.append(exc))  # type: ignore[arg-type]
            for exc in failures:
                if isinstance(exc, OSError):
                    outcome.error(exc)
            return not os.path.lexists(path)
        except OSError as exc:
            outcome.error(exc)
            return False
        finally:
            if fd >= 0:
                os.close(fd)


def run_plan(plan: Plan, remover: Remover, clock: Clock | None = None) -> Outcome:
    """Remove every candidate of *plan*, then fold the plan's skips into the outcome."""
    outcome = Outcome(skipped=Counter(plan.skipped), partial=plan.partial)
    clock = clock or Clock()
    for candidate in plan.candidates:
        if not clock.spend():
            outcome.partial = True
            break
        if remover.remove(candidate, outcome):
            outcome.removed += 1
            outcome.bytes_freed += candidate.bytes
    return outcome


def prune_docker(run: Callable[[Sequence[str], float], tuple[int, str]], hours: int) -> Outcome:
    """Dangling images and build cache older than *hours*; never volumes or containers.

    *run* runs an argument list (no shell) and gives (status, output)."""
    outcome = Outcome()
    until = f"until={max(1, int(hours))}h"
    for argv in (
        ("docker", "image", "prune", "-f", "--filter", until),
        ("docker", "builder", "prune", "-f", "--filter", until),
    ):
        code, out = run(argv, DOCKER_TIMEOUT_S)
        if code != 0:
            outcome.errors.append("ETIMEDOUT" if code == 124 else "EIO")
            continue
        outcome.bytes_freed += parse_reclaimed(out)
        outcome.removed += 1
    return outcome


_UNITS = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}


def parse_reclaimed(text: str) -> int:
    """``Total reclaimed space: 1.2GB`` as bytes (0 when absent)."""
    hit = re.search(r"Total reclaimed space:\s*([0-9.]+)\s*([kKMGT]?B)", text)
    if not hit:
        return 0
    try:
        return int(float(hit.group(1)) * _UNITS[hit.group(2).upper()])
    except (KeyError, ValueError):
        return 0


def run_argv(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except OSError:
        return 127, ""
    return done.returncode, done.stdout


def host_run(
    action: Action,
    *,
    roots: Sequence[str],
    tmp: str,
    now: float,
    stale_days: float,
    max_entries: int,
    max_seconds: float,
    uid: int | None = None,
    docker_run: Callable[[Sequence[str], float], tuple[int, str]] = run_argv,
    git_status: GitStatus = git_status_of,
    geteuid: Callable[[], int] = os.geteuid,
    proc: str = "/proc",
    locks_path: str = "/proc/locks",
    expected: int | None = None,
    remove_seconds: float = MAX_SECONDS,
) -> Outcome:
    """One host-side action, start to finish.

    With *expected* (the count the admin confirmed) a fresh plan holding more items than
    that removes only the *expected* oldest (ties by path) and counts the rest as
    ``over_confirmed``, so a run never removes more than was confirmed and an item that
    aged meanwhile waits for the next run.

    :raises Refused: When the preflight checks fail.
    """
    preflight(geteuid=geteuid)
    uid = os.getuid() if uid is None else uid
    clock = Clock(max_entries, max_seconds)  # planning only reads; removal has its own cap
    if action == "docker_prune":
        return prune_docker(docker_run, max(1, int(stale_days * 24)))
    targets = busy_targets(uid, proc)
    all_roots = [*roots, tmp]
    if action == "claude_scratch":
        live = box_health_scan.live_session_dirs(roots, uid, proc)
        plan = plan_claude_scratch(
            roots,
            now=now,
            stale_days=stale_days,
            uid=uid,
            budget=box_health_scan.Budget(max_entries, max_seconds),
            clock=clock,
            locks=box_health_scan.read_locks(locks_path),
            live=live,
            targets=targets,
        )
    elif action == "tmp_litter":
        plan = plan_tmp_litter(
            tmp, now=now, stale_days=stale_days, uid=uid, clock=clock, targets=targets
        )
    else:
        plan = plan_review_checkouts(
            tmp,
            now=now,
            stale_days=stale_days,
            uid=uid,
            clock=clock,
            targets=targets,
            status=git_status,
        )
    plan.cap(expected)
    remover = Remover(
        all_roots,
        uid,
        status=git_status if action == "review_checkouts" else None,
        proc=proc,
    )
    return run_plan(plan, remover, Clock(MAX_ITEMS, remove_seconds))


def dry_run_counts(
    *,
    roots: Sequence[str],
    tmp: str,
    now: float,
    stale_days: float,
    max_entries: int,
    max_seconds: float,
    uid: int | None = None,
    proc: str = "/proc",
    locks_path: str = "/proc/locks",
) -> dict[str, dict[str, object]]:
    """What each host action would remove, as counts only. Deletes nothing."""
    uid = os.getuid() if uid is None else uid
    clock = Clock(max_entries, max_seconds)
    targets = busy_targets(uid, proc)
    scratch = plan_claude_scratch(
        roots,
        now=now,
        stale_days=stale_days,
        uid=uid,
        budget=box_health_scan.Budget(max_entries, max_seconds),
        clock=clock,
        locks=box_health_scan.read_locks(locks_path),
        live=box_health_scan.live_session_dirs(roots, uid, proc),
        targets=targets,
    )
    litter = plan_tmp_litter(
        tmp,
        now=now,
        stale_days=stale_days,
        uid=uid,
        clock=Clock(max_entries, max_seconds),
        targets=targets,
    )
    reviews = plan_review_checkouts(
        tmp,
        now=now,
        stale_days=stale_days,
        uid=uid,
        clock=Clock(max_entries, max_seconds),
        targets=targets,
    )
    out: dict[str, dict[str, object]] = {}
    for name, plan in (
        ("claude_scratch", scratch),
        ("tmp_litter", litter),
        ("review_checkouts", reviews),
    ):
        out[name] = {
            "count": plan.count,
            "files": plan.files,
            "bytes": plan.bytes,
            "skipped": dict(plan.skipped),
            "partial": plan.partial,
        }
        if name == "tmp_litter":
            out[name]["groups"] = plan.groups()
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ae_omni_policies.box_cleanup",
        description="Print what the box cleanups would remove now. Deletes nothing; counts only.",
    )
    parser.add_argument("--dry-run", action="store_true", required=True)
    parser.add_argument("--stale-days", type=float, default=2)
    args = parser.parse_args(argv)
    from ae_omni_policies import box_health_relay

    settings = box_health_relay.settings_from_env()
    home = str(Path.home())
    uid = os.getuid()
    roots = [
        box_health_relay._expand(t.replace("{uid}", str(uid)), home) for t in settings.claude_roots
    ]
    counts = dry_run_counts(
        roots=roots,
        tmp="/tmp",
        now=time.time(),
        stale_days=args.stale_days,
        max_entries=settings.max_entries,
        max_seconds=settings.max_seconds,
    )
    json.dump(counts, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


__all__ = [
    "ACTIONS",
    "LITTER_PATTERNS",
    "SKIP_REASONS",
    "Candidate",
    "Clock",
    "Outcome",
    "Plan",
    "Refused",
    "Remover",
    "check_root",
    "dry_run_counts",
    "host_run",
    "litter_group",
    "plan_claude_scratch",
    "plan_review_checkouts",
    "plan_tmp_litter",
    "preflight",
    "prune_docker",
    "run_plan",
]

if __name__ == "__main__":
    raise SystemExit(main())
