"""Bounded folder scans for the box health relay (2026-10-28).

Why: the box's ``/tmp`` ran out of inodes on 2026-10-28 because Claude Code's
per-session temp folders (252,775 files across 477 sessions) and tens of thousands of
test leftovers were never cleaned. The relay (``box_health_relay.py``) counts them
hourly; this module does the counting without ever running away with the box:

- every walk has an entry cap and a seconds cap (a shared :class:`Budget`) and says
  ``partial`` when it stopped early, so a count reads "at least";
- nothing follows a symlink (``os.scandir`` with ``follow_symlinks=False``), so a
  link to a big folder elsewhere adds nothing and cannot lead a scan out of its root;
- sizes are ``st_blocks * 512``, what the disk really holds;
- a session is stale when the newest mtime anywhere in its tree is older than the
  cutoff, so a folder with one fresh file deep inside is kept alive; an unproven
  tree (a cap hit, an unreadable folder) is never called stale.

Privacy: names are read here only to decide what an entry is (a project, a session,
a pattern group) and are dropped. What leaves this module is counts, sizes and ages.
The same aggregation reads ``find -printf`` lines from a container
(:func:`aggregate_claude`, :func:`aggregate_pytest`), which carry names the relay
never keeps.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

DAY_S = 86400.0

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

Entry = tuple[tuple[str, ...], bool, float, int]
"""A path below a scan's root as name parts, whether it is a folder, its mtime and its size."""


class Budget:
    """An entry cap and a seconds cap shared by the walks of one scan."""

    def __init__(
        self,
        max_entries: int,
        max_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.entries_left = max_entries
        self._clock = clock
        self._deadline = clock() + max_seconds
        self.exhausted = False
        self.partial = False

    def spend(self) -> bool:
        """Count one entry; ``False`` once the entry cap or the seconds cap is spent."""
        if self.exhausted:
            return False
        if self.entries_left <= 0 or self._clock() > self._deadline:
            self.exhausted = True
            self.partial = True
            return False
        self.entries_left -= 1
        return True


def iter_tree(root: str, budget: Budget, skip: frozenset[str] = frozenset()) -> Iterator[Entry]:
    """Every entry below *root*, depth first, symlinks listed but never followed.

    A path in *skip* is passed over whole (it is counted by another scan).

    An unreadable folder marks the budget ``partial`` (the tree cannot be proven
    old); the cap marks it ``exhausted`` as well and ends the walk.
    """
    stack: list[tuple[str, tuple[str, ...]]] = [(root, ())]
    while stack:
        path, parts = stack.pop()
        try:
            handle = os.scandir(path)
        except OSError:
            budget.partial = True
            continue
        with handle:
            for entry in handle:
                if entry.path in skip:
                    continue
                if not budget.spend():
                    return
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    budget.partial = True
                    continue
                is_dir = stat.S_ISDIR(info.st_mode)
                rel = (*parts, entry.name)
                yield rel, is_dir, info.st_mtime, info.st_blocks * 512
                if is_dir:
                    stack.append((entry.path, rel))


@dataclass
class WalkResult:
    files: int = 0
    dirs: int = 0
    bytes: int = 0
    newest_mtime: float = 0.0
    partial: bool = False

    @property
    def entries(self) -> int:
        return self.files + self.dirs


def walk(root: str, budget: Budget, skip: frozenset[str] = frozenset()) -> WalkResult:
    """Total *root*'s tree under *budget*: entries, bytes and the newest mtime anywhere in it."""
    result = WalkResult()
    try:
        top = os.lstat(root)
    except OSError:
        result.partial = True
        return result
    result.newest_mtime = top.st_mtime
    result.bytes = top.st_blocks * 512
    before = budget.partial
    budget.partial = False
    for _parts, is_dir, mtime, size in iter_tree(root, budget, skip):
        result.bytes += size
        result.newest_mtime = max(result.newest_mtime, mtime)
        if is_dir:
            result.dirs += 1
        else:
            result.files += 1
    result.partial = budget.partial
    budget.partial = before or result.partial
    return result


# ── Claude Code temp folders and pytest temp ────────────────────────────────


@dataclass
class _Group:
    newest: float = 0.0
    files: int = 0
    bytes: int = 0
    complete: bool = True
    held: bool = False


@dataclass
class TreeScan:
    """Totals for one root: its entries, and the groups (sessions) counted inside it."""

    exists: bool = True
    readable: bool = True
    projects: int = 0
    sessions: int = 0
    stale_sessions: int = 0
    files: int = 0
    bytes: int = 0
    stale_files: int = 0
    stale_bytes: int = 0
    oldest_days: float | None = None
    top_age_days: float | None = None
    partial: bool = False


GroupKey = Callable[[tuple[str, ...]], tuple[str, ...] | None]


def claude_key(parts: tuple[str, ...]) -> tuple[str, ...] | None:
    """A Claude root's session: ``<project starting with ->/<uuid>``; everything else is loose."""
    if len(parts) >= 2 and parts[0].startswith("-") and UUID_RE.match(parts[1]):
        return parts[:2]
    return None


def pytest_key(parts: tuple[str, ...]) -> tuple[str, ...] | None:
    """A pytest temp root's run: each ``pytest-<n>`` folder directly below it."""
    return parts[:1] if parts and parts[0].startswith("pytest-") else None


class Accumulator:
    """Folds entries into a :class:`TreeScan` without keeping any name beyond a group key."""

    def __init__(self, key: GroupKey) -> None:
        self._key = key
        self._groups: dict[tuple[str, ...], _Group] = {}
        self._projects: set[str] = set()
        self._git: set[tuple[str, ...]] = set()
        """Groups with a ``.git`` anywhere in them: a checkout is never called stale."""
        self.files = 0
        self.bytes = 0
        self.partial = False

    def add(self, parts: tuple[str, ...], is_dir: bool, mtime: float, size: int) -> None:
        self.files += 1
        self.bytes += size
        if is_dir and len(parts) == 1 and parts[0].startswith("-"):
            self._projects.add(parts[0])
        key = self._key(parts)
        if key is None or (len(key) == len(parts) and not is_dir):
            return
        group = self._groups.setdefault(key, _Group())
        if ".git" in parts[len(key) :]:
            self._git.add(key)
        group.newest = max(group.newest, mtime)
        group.files += 1
        group.bytes += size

    def mark_held(self, key: tuple[str, ...]) -> None:
        """A live session holds *key*'s folder (the guard's lock), so it is never stale."""
        self._groups.setdefault(key, _Group()).held = True

    def mark_incomplete(self, key: tuple[str, ...]) -> None:
        """The walk of *key* did not finish, so its age is unproven."""
        self._groups.setdefault(key, _Group()).complete = False

    def stale_groups(
        self, now: float, stale_days: float, skipped: Counter[str] | None = None
    ) -> list[tuple[tuple[str, ...], int, int, float]]:
        """The groups :meth:`finish` calls stale, as (key, entries, bytes, newest mtime):
        proven old, whole walk, not held. Used by the container cleanup, which removes
        exactly these."""
        cutoff = now - stale_days * DAY_S
        out = []
        for key, group in self._groups.items():
            if group.complete and not group.held and group.newest and group.newest < cutoff:
                if key in self._git:
                    if skipped is not None:
                        skipped["git"] += 1
                    continue
                out.append((key, group.files, group.bytes, group.newest))
        return out

    def finish(self, now: float, stale_days: float, *, partial: bool = False) -> TreeScan:
        cutoff = now - stale_days * DAY_S
        scan = TreeScan(
            projects=len(self._projects),
            sessions=len(self._groups),
            files=self.files,
            bytes=self.bytes,
            partial=partial or self.partial,
        )
        oldest: float | None = None
        for key, group in self._groups.items():
            if group.newest:
                age = (now - group.newest) / DAY_S
                oldest = age if oldest is None else max(oldest, age)
            if (
                group.complete
                and not group.held
                and group.newest
                and group.newest < cutoff
                and key not in self._git
            ):
                scan.stale_sessions += 1
                scan.stale_files += group.files
                scan.stale_bytes += group.bytes
        scan.oldest_days = None if oldest is None else round(oldest, 2)
        return scan


def scan_tree(
    root: str,
    key: GroupKey,
    *,
    now: float,
    stale_days: float,
    budget: Budget,
    group_depth: int,
    locks: frozenset[tuple[int, int, int]] = frozenset(),
    live: frozenset[str] | set[str] = frozenset(),
) -> TreeScan:
    """Scan a Claude or pytest temp root.

    A missing root gives ``exists=False``; one that cannot be listed gives
    ``readable=False`` with the folder's own age, which is all the box can say about
    another user's folder. *group_depth* is how many name parts make a group (2 for
    a Claude session, 1 for a pytest run); the walk goes group by group so a cap
    that stops one marks only that group unproven.
    """
    try:
        top = os.lstat(root)
    except FileNotFoundError:
        return TreeScan(exists=False)
    except OSError:
        return TreeScan(readable=False)
    if not stat.S_ISDIR(top.st_mode):
        return TreeScan(exists=False)
    top_age = round(max(0.0, now - top.st_mtime) / DAY_S, 2)
    try:
        handle = os.scandir(root)
    except OSError:
        return TreeScan(readable=False, top_age_days=top_age)
    acc = Accumulator(key)
    # Lazily: spend the budget on each entry before the next is read, so a folder with
    # millions of entries is cut at the cap and not listed whole first.
    with handle:
        for entry in handle:
            if not budget.spend():
                break
            _visit_level1(acc, entry, group_depth, budget, locks, live)
            if budget.exhausted:
                break
    scan = acc.finish(now, stale_days, partial=budget.partial)
    scan.top_age_days = top_age
    return scan


def _visit_level1(
    acc: Accumulator,
    entry: os.DirEntry[str],
    group_depth: int,
    budget: Budget,
    locks: frozenset[tuple[int, int, int]],
    live: frozenset[str] | set[str],
) -> None:
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        budget.partial = True
        return
    is_dir = stat.S_ISDIR(info.st_mode)
    acc.add((entry.name,), is_dir, info.st_mtime, info.st_blocks * 512)
    if not is_dir:
        return
    if group_depth == 1:
        _feed(acc, entry.path, (entry.name,), budget, group=(entry.name,))
    elif entry.name.startswith("-"):
        _feed_level2(acc, entry.path, entry.name, budget, locks, live)
    else:
        _feed(acc, entry.path, (entry.name,), budget, group=None)


def _feed(
    acc: Accumulator,
    path: str,
    prefix: tuple[str, ...],
    budget: Budget,
    *,
    group: tuple[str, ...] | None,
) -> None:
    """Add the tree below *path* to *acc*; mark *group* unproven if the walk was cut short."""
    before = budget.partial
    budget.partial = False
    for rel, is_dir, mtime, size in iter_tree(path, budget):
        acc.add((*prefix, *rel), is_dir, mtime, size)
    cut = budget.partial
    budget.partial = before or cut
    if cut:
        acc.partial = True
        if group is not None:
            acc.mark_incomplete(group)


def _feed_level2(
    acc: Accumulator,
    path: str,
    project: str,
    budget: Budget,
    locks: frozenset[tuple[int, int, int]],
    live: frozenset[str] | set[str],
) -> None:
    try:
        handle = os.scandir(path)
    except OSError:
        budget.partial = True
        acc.partial = True
        return
    with handle:
        for child in handle:
            if not budget.spend():
                return
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                budget.partial = True
                acc.partial = True
                continue
            is_dir = stat.S_ISDIR(info.st_mode)
            parts = (project, child.name)
            acc.add(parts, is_dir, info.st_mtime, info.st_blocks * 512)
            if is_dir:
                key = claude_key(parts)
                ident = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
                if key is not None and (child.path in live or ident in locks):
                    acc.mark_held(key)
                _feed(acc, child.path, parts, budget, group=key)
            if budget.exhausted:
                return


def scan_claude_root(
    root: str,
    *,
    now: float,
    stale_days: float,
    budget: Budget,
    locks: frozenset[tuple[int, int, int]] = frozenset(),
    live: frozenset[str] | set[str] = frozenset(),
) -> TreeScan:
    return scan_tree(
        root,
        claude_key,
        now=now,
        stale_days=stale_days,
        budget=budget,
        group_depth=2,
        locks=locks,
        live=live,
    )


def live_session_dirs(roots: Iterable[str], uid: int, proc: str = "/proc") -> set[str]:
    """The Claude session folders a live process of *uid* works in, found the way
    ``ae-claude-tmp-guard`` finds them (2026-10-28): a process whose working folder or an
    open file lies inside ``<root>/<project>/<session uuid>``, or a Claude process whose
    command line names that session (``--resume`` or ``--session-id``). Only link targets
    and the command line are read, never file contents; the paths stay in this process.
    """
    roots = [r.rstrip("/") for r in roots if r]
    if not roots:
        return set()
    uuid = UUID_RE.pattern.strip("^$")
    alternatives = "|".join(re.escape(r) for r in roots)
    match = re.compile(rf"^(?P<dir>(?:{alternatives})/[^/]+/{uuid})(?:/|$)")
    arg = re.compile(rf"^--(?:resume|session-id)(?:=(?P<inline>{uuid}))?$")
    found: set[str] = set()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return found
    for pid in pids:
        base = f"{proc}/{pid}"
        try:
            if os.stat(base).st_uid != uid:
                continue
            targets = [os.readlink(f"{base}/cwd")]
            for fd in os.listdir(f"{base}/fd"):
                with contextlib.suppress(OSError):
                    targets.append(os.readlink(f"{base}/fd/{fd}"))
        except OSError:
            continue
        for target in targets:
            if hit := match.match(target):
                found.add(hit.group("dir"))
        try:
            with open(f"{base}/cmdline", "rb") as handle:
                args = handle.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if not any(os.path.basename(a) in ("claude", "claude.exe") for a in args[:1]):
            with contextlib.suppress(OSError):
                exe = os.path.basename(os.readlink(f"{base}/exe").removesuffix(" (deleted)"))
                if exe not in ("claude", "claude.exe"):
                    continue
        for i, item in enumerate(args):
            hit_arg = arg.match(item)
            if not hit_arg:
                continue
            value = hit_arg.group("inline") or (args[i + 1] if i + 1 < len(args) else "")
            if not UUID_RE.match(value):
                continue
            for root in roots:
                with contextlib.suppress(OSError):
                    for project in os.listdir(root):
                        path = os.path.join(root, project, value)
                        if os.path.isdir(path) and not os.path.islink(path):
                            found.add(path)
    return found


def read_locks(path: str = "/proc/locks") -> frozenset[tuple[int, int, int]]:
    """The (major, minor, inode) of every file held under a BSD ``flock``.

    ``ae-claude-tmp-guard`` holds a shared lock on each live Claude session's temp
    folder, and ``systemd-tmpfiles`` skips a locked folder. Reading ``/proc/locks``
    tells the scan which folders are held without taking a lock itself.
    """
    out: set[tuple[int, int, int]] = set()
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return frozenset()
    for line in lines:
        fields = line.split()
        if len(fields) < 6 or "FLOCK" not in fields:
            continue
        ident = (
            fields[fields.index("FLOCK") + 4] if len(fields) > fields.index("FLOCK") + 4 else ""
        )
        parts = ident.split(":")
        if len(parts) == 3:
            try:
                out.add((int(parts[0], 16), int(parts[1], 16), int(parts[2])))
            except ValueError:
                continue
    return frozenset(out)


def scan_pytest_root(root: str, *, now: float, stale_days: float, budget: Budget) -> TreeScan:
    return scan_tree(
        root, pytest_key, now=now, stale_days=stale_days, budget=budget, group_depth=1
    )


def _fold_find(lines: Iterable[str], key: GroupKey, *, truncated: bool) -> Accumulator:
    """Fold ``find -printf '%d\\t%y\\t%T@\\t%b\\t%P\\n'`` lines into an :class:`Accumulator`.

    ``%b`` is 512-byte blocks and ``%P`` the path below the root; the names stay in
    this function. A line that does not parse is skipped. When *truncated*, the last
    group seen (``find`` prints a folder's tree together) is left unproven.
    """
    acc = Accumulator(key)
    last: tuple[str, ...] | None = None
    for line in lines:
        fields = line.rstrip("\n").split("\t", 4)
        if len(fields) != 5:
            continue
        depth, kind, mtime, blocks, rel = fields
        try:
            int(depth)
            stamp = float(mtime)
            size = int(blocks) * 512
        except ValueError:
            continue
        parts = tuple(p for p in rel.split("/") if p)
        if not parts:
            continue
        acc.add(parts, kind == "d", stamp, size)
        group = key(parts)
        if group is not None:
            last = group
    if truncated and last is not None:
        acc.mark_incomplete(last)
    return acc


def _aggregate_find(
    lines: Iterable[str], key: GroupKey, *, now: float, stale_days: float, truncated: bool
) -> TreeScan:
    return _fold_find(lines, key, truncated=truncated).finish(now, stale_days, partial=truncated)


def stale_claude_groups(
    lines: Iterable[str],
    *,
    now: float,
    stale_days: float,
    truncated: bool = False,
    skipped: Counter[str] | None = None,
) -> list[tuple[tuple[str, ...], int, int, float]]:
    """The Claude sessions (project, uuid) of a container listing that are provably older
    than the stale age, with their entry count and bytes. A cut-short listing proves
    nothing about its last session, and a whole listing that was cut removes nothing."""
    if truncated:
        return []
    return _fold_find(lines, claude_key, truncated=False).stale_groups(now, stale_days, skipped)


def stale_pytest_groups(
    lines: Iterable[str],
    *,
    now: float,
    stale_days: float,
    truncated: bool = False,
    skipped: Counter[str] | None = None,
) -> list[tuple[tuple[str, ...], int, int, float]]:
    """Like :func:`stale_claude_groups` for the ``pytest-<n>`` runs of a pytest temp root."""
    if truncated:
        return []
    return _fold_find(lines, pytest_key, truncated=False).stale_groups(now, stale_days, skipped)


def aggregate_claude(
    lines: Iterable[str], *, now: float, stale_days: float, truncated: bool = False
) -> TreeScan:
    return _aggregate_find(lines, claude_key, now=now, stale_days=stale_days, truncated=truncated)


def aggregate_pytest(
    lines: Iterable[str], *, now: float, stale_days: float, truncated: bool = False
) -> TreeScan:
    return _aggregate_find(lines, pytest_key, now=now, stale_days=stale_days, truncated=truncated)


# ── top-level /tmp entries ──────────────────────────────────────────────────

# First match wins. The words are fixed labels; they are what the card shows, and an
# entry that matches none is counted as "other".
LITTER_GROUPS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sec-appr-*", re.compile(r"^sec-appr-")),
    ("sec-push-*", re.compile(r"^sec-push-")),
    ("sec-*", re.compile(r"^sec-")),
    ("media-*", re.compile(r"^media-")),
    ("dfs-*", re.compile(r"^dfs-")),
    ("plex-*", re.compile(r"^plex-")),
    ("tmp*", re.compile(r"^tmp[a-z0-9_]{8}$")),
    ("omnigent-codex-skills-*", re.compile(r"^omnigent-codex-skills-")),
    ("omnigent-osenv-*", re.compile(r"^omnigent-osenv-")),
    ("dotnet-diagnostic-*", re.compile(r"^dotnet-diagnostic-")),
    ("clr-debug-pipe-*", re.compile(r"^clr-debug-pipe-")),
    ("*-review.*", re.compile(r"^.+-review\..+$")),
)
OTHER_GROUP = "other"
CLAUDE_TOP_RE = re.compile(r"claude-[0-9]{1,10}")
"""Matched with ``fullmatch``: ``\\d`` takes Unicode digits and ``$`` a trailing newline."""
PYTEST_TOP_RE = re.compile(r"pytest-of-.+")


def group_of(name: str) -> str:
    """The litter group a top-level ``/tmp`` name belongs to."""
    for label, pattern in LITTER_GROUPS:
        if pattern.match(name):
            return label
    return OTHER_GROUP


@dataclass
class LitterAgg:
    entries: int = 0
    stale: int = 0
    bytes: int = 0
    """Plain files only: a folder is never opened, so its contents are not counted."""
    inodes: int = 0
    unreadable: int = 0
    partial: bool = False
    measured: int = 0
    """Always 0 for litter: the posted ``inodes`` stays null (see :func:`scan_tmp`)."""


@dataclass
class TmpScan:
    """The top of ``/tmp`` by owner: leftovers grouped, plus where each owner's temp roots are."""

    litter: dict[int, dict[str, LitterAgg]] = field(default_factory=dict)
    claude_dirs: dict[int, str] = field(default_factory=dict)
    pytest_dirs: dict[int, str] = field(default_factory=dict)
    partial: bool = False


def scan_tmp(root: str, *, now: float, stale_days: float, budget: Budget) -> TmpScan:
    """Group ``root``'s top-level entries by (owner uid, pattern group), one ``lstat`` each.

    Litter folders are never opened (2026-10-28, lead): Ubuntu's stock
    ``q /tmp 1777 root root 10d`` rule counts a folder's access time and ``/tmp`` is
    ``strictatime``, so listing a folder would keep it alive. Each entry gives its
    count, whether it is stale (its own mtime is older than the cutoff) and, for a
    plain file, its size. ``claude-<n>`` and ``pytest-of-*`` are not litter: their
    paths go back to the caller to scan as Claude roots and pytest temp.

    The listing is read lazily under *budget* (an entry cap and a seconds cap): a
    ``/tmp`` with millions of entries is cut at the cap, and ``partial`` says so.
    """
    out = TmpScan()
    cutoff = now - stale_days * DAY_S
    try:
        handle = os.scandir(root)
    except OSError:
        out.partial = True
        return out
    with handle:
        for entry in handle:
            if not budget.spend():
                out.partial = True
                break
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            uid = info.st_uid
            is_dir = stat.S_ISDIR(info.st_mode)
            if CLAUDE_TOP_RE.fullmatch(entry.name) and is_dir:
                out.claude_dirs.setdefault(uid, entry.path)
                continue
            if PYTEST_TOP_RE.fullmatch(entry.name) and is_dir:
                out.pytest_dirs.setdefault(uid, entry.path)
                continue
            agg = out.litter.setdefault(uid, {}).setdefault(group_of(entry.name), LitterAgg())
            agg.entries += 1
            if info.st_mtime < cutoff:
                agg.stale += 1
            if not is_dir:
                agg.bytes += info.st_blocks * 512
    return out


__all__ = [
    "LITTER_GROUPS",
    "Accumulator",
    "Budget",
    "LitterAgg",
    "TmpScan",
    "TreeScan",
    "WalkResult",
    "aggregate_claude",
    "aggregate_pytest",
    "group_of",
    "iter_tree",
    "live_session_dirs",
    "read_locks",
    "scan_claude_root",
    "scan_pytest_root",
    "scan_tmp",
    "stale_claude_groups",
    "stale_pytest_groups",
    "walk",
]
