"""Measure the box and post it to ``/v1/ae/box-health`` (2026-10-28).

``python -m ae_omni_policies.box_health_relay`` runs once and exits; the user timer
``deploy/systemd/user/ae-omnigent-box-health.timer`` runs it every five minutes.
``--dry-run`` prints what it would post and posts nothing; ``--slow`` forces the
hourly scans now. Why it exists is in :mod:`ae_omni_policies.box_health`.

Two passes, one post:

- **Fast, every run:** temperatures from ``/sys/class/hwmon`` (matched by chip name,
  never by index: the numbers shuffle between boots), load and CPU busy %, memory,
  local mounts with space and inodes, uptime, and failed systemd units. Cheap; it
  catches inode exhaustion within five minutes.
- **Slow, at most every ``AE_BOX_HEALTH_SLOW_MINUTES``:** per box user, the Claude
  Code temp folders, pytest temp, the leftovers at the top of ``/tmp`` and the
  worktrees; Docker's totals, the journal's size, a few folder sizes, and each mapped
  container, measured through fixed read-only ``docker exec`` commands. The result is
  cached in the state file with its own ``read_at``, so a fast pass never wipes it.

Privacy rule: nothing leaves this process that names a folder, project, session,
branch, worktree, repository or file. Names are read to count and are dropped;
errors say which part failed and never echo a listed line. A user is a label from
``AE_BOX_USERS`` (or the login name for an unlisted id), never a path.

Containers: only a container named in ``AE_BOX_USERS`` is touched. Every command
comes from :data:`CONTAINER_COMMANDS`, a constant table of argument lists. The only
values substituted are the container name and user (checked against the map and a
strict pattern) and a number taken from the container's own listing (digits only).
There is no shell: no ``sh -c``, no ``bash``. The relay runs as ``agent``, never
root, and every in-container command carries ``timeout``; its output is read as
``find -printf`` metadata only, never file contents, and cut at a line cap.

A part that cannot be read goes in ``failed`` and the rest is posted; the exit status
is then 1 so the timer shows the run as failed (as ``secrets_relay`` does).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import pwd
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from ae_omni_policies import box_cleanup, box_health, box_health_scan, omni

_logger = logging.getLogger("ae_omni_policies.box_health_relay")

LOCAL_FS = frozenset({"ext4", "xfs", "btrfs", "vfat", "tmpfs", "f2fs"})
"""Only local filesystems are measured, so ``statvfs`` can never hang on a network mount."""
ALWAYS_SKIP = ("/run/user/",)

CONTAINER_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
DIGITS_RE = re.compile(r"[0-9]{1,10}")
LABEL_RE = re.compile(box_health.LABEL_PATTERN.strip("^$"))

WORKTREE_DAYS = 14
SLOW_DEADLINE_S = 150.0
"""The slow pass stops here, well inside the unit's 4 minute TimeoutStartSec, so a slow
scan can never keep the fast reading from arriving (2026-10-28, review of PR 389)."""
CLEANUP_DEADLINE_S = 100.0
PASS_DEADLINE_S = 195.0
POST_TIMEOUT_S = 10.0
"""A cleanup stops at 100 s, and one whole pass (fast scan, cleanup, slow scan, posts) at
195 s, and each of the three posts waits at most 10 s, so a pass ends before the unit's
4 minute TimeoutStartSec (2026-10-29, review of PR 395)."""
VOLUMES_TTL_S = 24 * 3600
CHANGES_BUDGET_S = 20.0
CHANGES_TIMEOUT_S = 8.0
CONTAINER_TIMEOUT_S = 30
DU_TIMEOUT_S = 60
DF_TIMEOUT_S = 120
HOST_DU_TIMEOUT_S = 120

# Every argument list the relay may send into a container. The placeholders are
# replaced by container_argv() and nothing else: <c> the mapped container, <user>
# the container user, <n> a number from the container's own /tmp listing.
CONTAINER_COMMANDS: dict[str, tuple[str, ...]] = {
    "tmp_list": (
        "docker",
        "exec",
        "-u",
        "<user>",
        "<c>",
        "timeout",
        "30",
        "find",
        "/tmp",
        "-xdev",
        "-mindepth",
        "1",
        "-maxdepth",
        "1",
        "-printf",
        "%y\t%U\t%T@\t%P\n",
    ),
    "claude_tree": (
        "docker",
        "exec",
        "-u",
        "<user>",
        "<c>",
        "timeout",
        "30",
        "find",
        "/tmp/claude-<n>",
        "-xdev",
        "-mindepth",
        "1",
        "-printf",
        "%d\t%y\t%T@\t%b\t%P\n",
    ),
    "pytest_tree": (
        "docker",
        "exec",
        "-u",
        "<user>",
        "<c>",
        "timeout",
        "30",
        "find",
        "/tmp/pytest-of-<user>",
        "-xdev",
        "-mindepth",
        "1",
        "-printf",
        "%d\t%y\t%T@\t%b\t%P\n",
    ),
    "worktrees_list": (
        "docker",
        "exec",
        "-u",
        "<user>",
        "<c>",
        "timeout",
        "30",
        "find",
        "/home/<user>/worktrees",
        "-mindepth",
        "1",
        "-maxdepth",
        "1",
        "-printf",
        "%T@\n",
    ),
    "worktrees_du": (
        "docker",
        "exec",
        "-u",
        "<user>",
        "<c>",
        "timeout",
        "60",
        "du",
        "-sk",
        "/home/<user>/worktrees",
    ),
    "ps": (
        "docker",
        "ps",
        "--size",
        "--filter",
        "name=^<c>$",
        "--format",
        "{{json .}}",
    ),
    "inspect": ("docker", "inspect", "--format", "{{json .Mounts}}", "<c>"),
    "volumes": ("docker", "system", "df", "-v", "--format", "{{json .}}"),
    # Live-use checks run right before a container removal (2026-10-29, review of PR 395):
    # where the container user's processes work, and every uuid on their command lines.
    "procs": (
        "docker",
        "exec",
        "-u",
        "<user>",
        "<c>",
        "timeout",
        "30",
        "find",
        "/proc",
        "-mindepth",
        "2",
        "-maxdepth",
        "3",
        "(",
        "-path",
        "/proc/[0-9]*/cwd",
        "-o",
        "-path",
        "/proc/[0-9]*/fd/*",
        ")",
        "-printf",
        "%p\t%l\\0",
    ),
    "uid": (
        "docker",
        "exec",
        "-u",
        "<user>",
        "<c>",
        "timeout",
        "30",
        "id",
        "-u",
    ),
    "uids": (
        "docker",
        "exec",
        "-u",
        "<user>",
        "<c>",
        "timeout",
        "30",
        "find",
        "/proc",
        "-mindepth",
        "2",
        "-maxdepth",
        "2",
        "(",
        "-path",
        "/proc/[0-9]*/status",
        "-exec",
        "grep",
        "-H",
        "-e",
        "^Uid:",
        "-e",
        "^State:",
        "-e",
        "^Threads:",
        "{}",
        "+",
        ")",
        "-o",
        "(",
        "-path",
        "/proc/[0-9]*/cmdline",
        "-exec",
        "grep",
        "-a",
        "-H",
        "",
        "{}",
        "+",
        ")",
    ),
    "cmdlines": (
        "docker",
        "exec",
        "-u",
        "<user>",
        "<c>",
        "timeout",
        "30",
        "find",
        "/proc",
        "-mindepth",
        "2",
        "-maxdepth",
        "2",
        "-path",
        "/proc/[0-9]*/cmdline",
        "-exec",
        "cat",
        "{}",
        "+",
    ),
}
"""``volumes`` has no container in it: it lists every volume's size, and the relay
picks out the one the container's ``/home/<user>`` is mounted from."""

# The one command that removes anything inside a container (the button and the hourly
# aging both use it): ``rm`` as the container user, on paths that pass CLAUDE_PATH_RE or
# the pytest pattern, sent as arguments after ``--``. ``rm`` removes a symlink itself and
# never follows it, and ``--one-file-system`` keeps it inside the temp folder.
CONTAINER_RM: tuple[str, ...] = (
    "docker",
    "exec",
    "-u",
    "<user>",
    "<c>",
    "timeout",
    "120",
    "rm",
    "-rf",
    "--one-file-system",
    "--",
)
RM_BATCH = 200
CLAUDE_PATH_RE = re.compile(
    r"/tmp/claude-[0-9]{1,10}/-[A-Za-z0-9._-]{1,200}/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def pytest_path_ok(path: str, user: str) -> bool:
    return bool(re.fullmatch(rf"/tmp/pytest-of-{re.escape(user)}/pytest-[0-9]{{1,10}}", path))


def container_path_ok(path: str, user: str) -> bool:
    """Whether *path* is a container temp folder the relay may ask ``rm`` to remove."""
    return bool(CLAUDE_PATH_RE.fullmatch(path)) or pytest_path_ok(path, user)


HOST_COMMANDS: dict[str, tuple[str, ...]] = {
    "docker_df": ("docker", "system", "df", "--format", "{{json .}}"),
    "journal": ("journalctl", "--disk-usage"),
    "failed_system": ("systemctl", "--failed", "--no-legend", "--plain"),
    "failed_user": ("systemctl", "--user", "--failed", "--no-legend", "--plain"),
}


class RelayError(Exception):
    """Something the relay should say plainly: a transport failure, a refused post."""


def container_argv(key: str, *, container: str, user: str, n: str = "") -> list[str]:
    """The argument list for *key* with the placeholders filled in.

    :raises ValueError: When the container, user or number is not in the allowed shape.
    """
    template = CONTAINER_COMMANDS[key]
    if not CONTAINER_RE.fullmatch(container):
        raise ValueError("a container name is not a plain name")
    if not USER_RE.fullmatch(user) or user == "root":
        raise ValueError("a container user is not a plain, non-root user name")
    if "<n>" in "".join(template) and not DIGITS_RE.fullmatch(n):
        raise ValueError("the number from the container's listing is not digits")
    return [
        part.replace("<c>", container).replace("<user>", user).replace("<n>", n)
        for part in template
    ]


CONTAINER_FRESH: tuple[str, ...] = (
    "docker",
    "exec",
    "-u",
    "<user>",
    "<c>",
    "timeout",
    "30",
    "find",
)
"""``find <paths> -newermt @<cutoff> -print``: anything inside the paths newer than the cutoff."""


def container_fresh_argv(
    container: str, user: str, paths: Sequence[str], cutoff: int
) -> list[str]:
    """The freshness check for *paths*: prints every entry inside them changed after *cutoff*.

    :raises ValueError: On a name, user or path that is not allowed, or a bad cutoff."""
    rm = container_rm_argv(container, user, paths)  # the same name, user and path rules
    if cutoff < 0:
        raise ValueError("a cutoff is a positive epoch")
    head = [p.replace("<c>", container).replace("<user>", user) for p in CONTAINER_FRESH]
    return [*head, *rm[len(CONTAINER_RM) :], "-newermt", f"@{int(cutoff)}", "-print"]


def container_rm_argv(container: str, user: str, paths: Sequence[str]) -> list[str]:
    """``docker exec -u <user> <container> rm -rf --one-file-system -- <paths>``.

    :raises ValueError: When the container or user is not plain, or a path is not an
        allowed container temp folder, or there are none or too many.
    """
    if not CONTAINER_RE.fullmatch(container):
        raise ValueError("a container name is not a plain name")
    if not USER_RE.fullmatch(user) or user == "root":
        raise ValueError("a container user is not a plain, non-root user name")
    if not paths or len(paths) > RM_BATCH:
        raise ValueError("an rm batch holds 1 to 200 paths")
    for path in paths:
        if not container_path_ok(path, user):
            raise ValueError("a path is not an allowed container temp folder")
    head = [part.replace("<c>", container).replace("<user>", user) for part in CONTAINER_RM]
    return [*head, *paths]


def matches_container_template(argv: Sequence[str], mapped: Sequence[str], user: str) -> bool:
    """Whether *argv* is one of :data:`CONTAINER_COMMANDS` for a mapped container and the
    configured container *user* (never root; ``settings_from_env`` refuses that).

    The relay checks this right before it runs a command, so a change elsewhere that
    built a different argument list fails here instead of reaching the container.
    """
    if user == "root" or not USER_RE.fullmatch(user):
        return False
    if "-newermt" in argv:
        head = len(CONTAINER_FRESH)
        try:
            cutoff = int(argv[-2].removeprefix("@"))
            for name in mapped:
                if list(argv) == container_fresh_argv(name, user, list(argv[head:-3]), cutoff):
                    return True
        except (ValueError, IndexError):
            return False
        return False
    if "rm" in argv:
        head = len(CONTAINER_RM)
        for name in mapped:
            with contextlib.suppress(ValueError):
                if list(argv) == container_rm_argv(name, user, list(argv[head:])):
                    return True
        return False
    digits = ""
    for part in argv:
        match = re.fullmatch(r"/tmp/claude-([0-9]{1,10})", part)
        if match:
            digits = match.group(1)
    for key in CONTAINER_COMMANDS:
        for name in mapped:
            try:
                expected = container_argv(key, container=name, user=user, n=digits)
            except ValueError:
                continue
            if list(argv) == expected:
                return True
    return False


# ── running commands ────────────────────────────────────────────────────────


@dataclass
class RunResult:
    returncode: int
    out: str = ""


@dataclass
class StreamResult:
    lines: list[str] = field(default_factory=list)
    truncated: bool = False
    returncode: int = 0


class Runner(Protocol):
    def run(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str] | None = None
    ) -> RunResult: ...

    def stream(self, argv: Sequence[str], timeout: float, max_lines: int) -> StreamResult: ...


class SubprocessRunner:
    """Runs argument lists with no shell. Output is returned, never printed."""

    def run(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str] | None = None
    ) -> RunResult:
        merged = {**os.environ, **env} if env else None
        try:
            done = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=merged,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return RunResult(124)
        except OSError:
            return RunResult(127)
        return RunResult(done.returncode, done.stdout)

    def stream(self, argv: Sequence[str], timeout: float, max_lines: int) -> StreamResult:
        result = StreamResult()
        try:
            proc = subprocess.Popen(
                list(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
            )
        except OSError:
            result.returncode = 127
            return result
        timer = threading.Timer(timeout + 5, proc.kill)
        timer.start()
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                if len(result.lines) >= max_lines:
                    result.truncated = True
                    proc.kill()
                    break
                result.lines.append(line)
        finally:
            timer.cancel()
            proc.stdout and proc.stdout.close()
            result.returncode = proc.wait()
        if result.truncated:
            result.returncode = 0
        return result


# ── settings ────────────────────────────────────────────────────────────────


@dataclass
class UsersMap:
    uids: dict[int, str] = field(default_factory=dict)
    containers: dict[str, str] = field(default_factory=dict)
    bad: int = 0
    """How many entries were skipped; the post lists ``users`` as failed when this is not zero."""


def parse_users(raw: str) -> UsersMap:
    """``1000=owner,1001=member,container:name=member`` as a map. A bad entry is skipped and
    counted."""
    out = UsersMap()
    for index, item in enumerate(p.strip() for p in raw.split(",")):
        if not item:
            continue
        key, sep, label = item.partition("=")
        key, label = key.strip(), label.strip()
        ok = bool(sep) and bool(LABEL_RE.fullmatch(label))
        if ok and key.startswith("container:"):
            name = key.removeprefix("container:")
            if CONTAINER_RE.fullmatch(name):
                out.containers[name] = label
                continue
        elif ok and DIGITS_RE.fullmatch(key):
            out.uids[int(key)] = label
            continue
        out.bad += 1
        _logger.warning(
            "AE_BOX_USERS entry %d is not uid=Label or container:name=Label; skipped it", index + 1
        )
    return out


@dataclass
class Settings:
    users: UsersMap
    container_user: str
    claude_roots: list[str]
    stale_days: float
    slow_minutes: int
    max_entries: int
    max_seconds: float
    dirs: list[tuple[str, str]]
    """(label, path) for each folder to size; only the label is posted."""
    worktrees: str
    skip_mounts: list[str]
    state_file: Path
    guard_file: str
    dry_run: bool = False
    """A dry run measures and prints; it never removes anything (set by ``--dry-run``)."""
    cleanup: bool = True
    """``AE_BOX_HEALTH_CLEANUP``: off hides the buttons and stops the hourly container aging."""


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def parse_dirs(raw: str) -> list[tuple[str, str]]:
    """``cache=~/.cache,npm=~/.npm`` as (label, path). A bare path is labelled ``folder N``
    and so is an entry whose label is not a plain name: only the label is ever posted
    (2026-10-28, review of PR 389: a folder path can carry a client's name)."""
    out: list[tuple[str, str]] = []
    for index, item in enumerate(_split(raw), start=1):
        label, sep, path = item.partition("=")
        label, path = label.strip(), path.strip()
        if not sep:
            label, path = f"folder {index}", item
        elif not LABEL_RE.fullmatch(label) or not path:
            label, path = f"folder {index}", path or item
        out.append((label, path))
    return out[:6]


def _float(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if not low <= value <= high:
        _logger.warning("%s=%r is outside %s to %s; using %s", name, raw, low, high, default)
        return default
    return value


def settings_from_env(env: Mapping[str, str] | None = None, home: str | None = None) -> Settings:
    env = os.environ if env is None else env
    home = home or str(Path.home())

    def default(name: str) -> str:
        return next(s.default for s in box_health.HOST_SETTINGS if s.name == name)

    def text(name: str) -> str:
        return (env.get(name) or "").strip() or default(name)

    user = text("AE_BOX_HEALTH_CONTAINER_USER")
    if not USER_RE.fullmatch(user) or user == "root":
        _logger.warning(
            "AE_BOX_HEALTH_CONTAINER_USER is not a plain, non-root user name; using agent"
        )
        user = "agent"
    state = text("AE_BOX_HEALTH_STATE_FILE")
    return Settings(
        users=parse_users(env.get("AE_BOX_USERS", "")),
        container_user=user,
        claude_roots=_split(text("AE_BOX_HEALTH_CLAUDE_ROOTS")),
        stale_days=_float(env, "AE_BOX_HEALTH_STALE_DAYS", 2, 0.01, 3650),
        slow_minutes=int(_float(env, "AE_BOX_HEALTH_SLOW_MINUTES", 60, 1, 10080)),
        max_entries=int(_float(env, "AE_BOX_HEALTH_WALK_MAX_ENTRIES", 500000, 100, 10**8)),
        max_seconds=_float(env, "AE_BOX_HEALTH_WALK_MAX_SECONDS", 30, 1, 3600),
        dirs=parse_dirs(text("AE_BOX_HEALTH_DIRS")),
        worktrees=text("AE_BOX_HEALTH_WORKTREES"),
        skip_mounts=_split(text("AE_BOX_HEALTH_SKIP_MOUNTS")),
        state_file=Path(state.replace("~", home, 1) if state.startswith("~") else state),
        guard_file=text("AE_BOX_HEALTH_GUARD_FILE").replace("{uid}", str(os.getuid())),
        cleanup=text("AE_BOX_HEALTH_CLEANUP").lower() not in {"off", "0", "false", "no"},
    )


def _expand(path: str, home: str) -> str:
    return home + path[1:] if path == "~" or path.startswith("~/") else path


# ── the fast pass ───────────────────────────────────────────────────────────

_SENSOR_ORDER = ("k10temp", "nvme", "acpitz", "amdgpu")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def read_sensors(sys_root: Path) -> tuple[list[box_health.Sensor], float | None]:
    """Temperatures by chip name, never by hwmon index; the GPU's power in watts, if any.

    :raises RelayError: When there is no hwmon at all.
    """
    base = sys_root / "class" / "hwmon"
    if not base.is_dir():
        raise RelayError(f"{base} is missing; the box has no hwmon sensors to read")
    found: list[tuple[int, str, box_health.Sensor]] = []
    power: float | None = None
    for chip_dir in sorted(base.iterdir()):
        name = _read(chip_dir / "name")
        if not name or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", name):
            continue
        rank = next((i for i, p in enumerate(_SENSOR_ORDER) if name.startswith(p)), 9)
        if name.startswith("amdgpu"):
            watts = _read(chip_dir / "power1_average")
            if watts and watts.isdigit():
                power = int(watts) / 1e6
        for item in sorted(chip_dir.glob("temp*_input")):
            number = item.name.removeprefix("temp").removesuffix("_input")
            raw = _read(item)
            if raw is None or not re.fullmatch(r"-?\d+", raw):
                continue
            label = _read(chip_dir / f"temp{number}_label") or f"temp{number}"
            if not re.fullmatch(r"[A-Za-z0-9_. :-]{1,40}", label):
                label = f"temp{number}"
            if name.startswith("nvme") and label != "Composite":
                continue
            found.append(
                (rank, name, box_health.Sensor(chip=name, label=label, c=int(raw) / 1000))
            )
    found.sort(key=lambda item: item[0])
    return [sensor for _, _, sensor in found][:24], power


def _psi(path: Path) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    text = _read(path)
    for line in (text or "").splitlines():
        kind, *pairs = line.split()
        out[kind] = {
            k: float(v) for k, _, v in (p.partition("=") for p in pairs) if k.startswith("avg")
        }
    return out


def read_cpu(proc: Path, cached: dict[str, Any] | None) -> tuple[box_health.Cpu, dict[str, Any]]:
    """Load, threads, busy % against the cached counters, and CPU pressure."""
    load = (_read(proc / "loadavg") or "").split()
    if len(load) < 3:
        raise RelayError(f"{proc / 'loadavg'} did not hold a load average")
    stat = (_read(proc / "stat") or "").splitlines()
    first = stat[0].split() if stat else []
    threads = sum(1 for line in stat if re.match(r"cpu\d+\s", line)) or (os.cpu_count() or 1)
    counters: dict[str, Any] = {}
    busy: float | None = None
    if len(first) >= 5 and first[0] == "cpu":
        values = [int(v) for v in first[1:9] if v.isdigit()]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        counters = {"total": total, "idle": idle}
        if cached and cached.get("total") is not None:
            d_total = total - int(cached["total"])
            d_idle = idle - int(cached["idle"])
            if d_total > 0 and d_idle >= 0:
                busy = round(100.0 * (d_total - d_idle) / d_total, 1)
    psi = _psi(proc / "pressure" / "cpu").get("some", {})
    return (
        box_health.Cpu(
            threads=threads,
            load1=float(load[0]),
            load5=float(load[1]),
            load15=float(load[2]),
            busy_pct=busy,
            psi_some_avg10=psi.get("avg10"),
            psi_some_avg60=psi.get("avg60"),
            psi_some_avg300=psi.get("avg300"),
        ),
        counters,
    )


def read_memory(proc: Path) -> box_health.Memory:
    values: dict[str, int] = {}
    for line in (_read(proc / "meminfo") or "").splitlines():
        key, _, rest = line.partition(":")
        number = rest.split()
        if number and number[0].isdigit():
            values[key] = int(number[0]) * 1024
    if "MemTotal" not in values:
        raise RelayError(f"{proc / 'meminfo'} did not hold MemTotal")
    psi = _psi(proc / "pressure" / "memory")
    return box_health.Memory(
        total_b=values["MemTotal"],
        available_b=values.get("MemAvailable", values.get("MemFree", 0)),
        shmem_b=values.get("Shmem", 0),
        swap_total_b=values.get("SwapTotal", 0),
        swap_free_b=values.get("SwapFree", 0),
        psi_some_avg60=psi.get("some", {}).get("avg60"),
        psi_full_avg60=psi.get("full", {}).get("avg60"),
    )


def _unescape_mount(path: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)


def read_mounts(
    mounts_file: Path,
    skip: Sequence[str],
    statvfs: Callable[[str], os.statvfs_result] = os.statvfs,
) -> list[box_health.Mount]:
    """Local mounts with space and inodes counted the way ``df`` does; skipped prefixes left
    out. Only a mount on the safe list is posted by its path; any other is ``other mount N``,
    with its numbers and no path (2026-10-28, review of PR 389)."""
    text = _read(mounts_file)
    if text is None:
        raise RelayError(f"{mounts_file} could not be read")
    found: dict[str, dict[str, Any]] = {}
    skips = (*ALWAYS_SKIP, *(s.rstrip("/") + "/" for s in skip), *(s.rstrip("/") for s in skip))
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        path, fstype = _unescape_mount(parts[1]), parts[2]
        if fstype not in LOCAL_FS or path in found:
            continue
        if any(path == s or path.startswith(s) for s in skips if s):
            continue
        try:
            st = statvfs(path)
        except OSError:
            continue
        found[path] = {
            "fstype": fstype if re.fullmatch(r"[a-z0-9_.-]{1,16}", fstype) else "other",
            "size_b": st.f_blocks * st.f_frsize,
            "used_b": (st.f_blocks - st.f_bfree) * st.f_frsize,
            "avail_b": st.f_bavail * st.f_frsize,
            "inodes": st.f_files or None,
            "inodes_used": (st.f_files - st.f_ffree) if st.f_files else None,
        }
    ranked = sorted(
        found.items(),
        key=lambda item: (
            item[0] not in ("/", "/tmp"),
            item[0] not in box_health.SAFE_MOUNTS,
            -item[1]["size_b"],
        ),
    )
    out: list[box_health.Mount] = []
    other = 0
    for path, fields in ranked[:20]:
        if path in box_health.SAFE_MOUNTS:
            label = path
        else:
            other += 1
            label = f"other mount {other}"
        try:
            out.append(box_health.Mount(path=label, **fields))
        except ValidationError:
            continue
    return out


def read_failed_units(runner: Runner) -> tuple[list[str], int, list[str], int]:
    """Failed system and user units: the names the card may show, and how many others.

    Only ``ae-*`` units (an ``@instance`` dropped), systemd's own and docker's are named;
    the rest are counted, since a unit name can carry a client or a repository
    (2026-10-28, review of PR 389).

    :raises RelayError: When systemctl cannot be run.
    """
    out: list[tuple[list[str], int]] = []
    for key in ("failed_system", "failed_user"):
        result = runner.run(HOST_COMMANDS[key], 20)
        if result.returncode != 0:
            raise RelayError("systemctl --failed did not answer; the user session may be missing")
        named: list[str] = []
        others = 0
        for line in result.out.splitlines():
            tokens = [t for t in line.split() if t not in ("●", "*", "x")]
            if not tokens or "." not in tokens[0]:
                continue
            shown = box_health.safe_unit(tokens[0])
            if shown is None:
                others += 1
            elif shown not in named:
                named.append(shown)
        out.append((named[:10], others))
    return out[0][0], out[0][1], out[1][0], out[1][1]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_guard(
    path: str,
    now: float,
    alive: Callable[[int], bool] = _pid_alive,
    previous: Mapping[str, Any] | None = None,
) -> tuple[box_health.Guard, dict[str, Any]]:
    """The Claude temp guard's status file (counts only), and the streak to carry forward.

    Running means the file is under two minutes old and its process is alive; a missing
    or unreadable file is not running. A new Claude process is unmatched for one guard
    pass, so ``unmatched_readings`` and ``unmatched_minutes`` count how long a nonzero
    ``claude_unmatched`` has persisted across the relay's own readings.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        updated = float(data["updated_at"])
        pid = int(data["pid"])

        def number(name: str) -> int:
            return max(0, int(data.get(name, 0)))

        locked, failed = number("locked"), number("failed")
        processes, unmatched, remote = (
            number("claude_processes"),
            number("claude_unmatched"),
            number("claude_remote"),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return box_health.Guard(running=False), {}
    age = max(0, int(now - updated))
    streak: dict[str, Any] = {}
    readings = minutes = 0
    if unmatched > 0:
        before = previous or {}
        since = float(before.get("since", now))
        readings = int(before.get("readings", 0)) + 1
        minutes = max(0, int((now - since) / 60))
        streak = {"readings": readings, "since": since}
    return (
        box_health.Guard(
            running=age < 120 and alive(pid),
            locked=locked,
            failed=failed,
            age_s=age,
            claude_processes=processes,
            claude_unmatched=unmatched,
            claude_remote=remote,
            unmatched_readings=readings,
            unmatched_minutes=minutes,
        ),
        streak,
    )


# ── Docker and sizes ────────────────────────────────────────────────────────

_UNITS = {"B": 1, "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
_BIN_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_docker_size(text: str) -> int | None:
    """``98.01GB``, ``293MB (virtual 5.15GB)`` and ``5.82GB (5%)`` as bytes (decimal units)."""
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kKMGT]?B)\b", text)
    if not match:
        return None
    return int(float(match.group(1)) * _UNITS[match.group(2).upper()])


def parse_journal_usage(text: str) -> int | None:
    match = re.search(r"take up ([0-9.]+)([KMGT]?)\b", text)
    if not match:
        return None
    return int(float(match.group(1)) * _BIN_UNITS.get(match.group(2), 1))


def read_docker(runner: Runner) -> box_health.Docker:
    """``docker system df`` totals. :raises RelayError: When docker does not answer."""
    result = runner.run(HOST_COMMANDS["docker_df"], 60)
    if result.returncode != 0:
        raise RelayError(
            "docker system df did not answer; check that this user is in the docker group"
        )
    rows: dict[str, dict[str, Any]] = {}
    for line in result.out.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("Type"), str):
            rows[row["Type"]] = row

    def size(kind: str, field_name: str) -> int:
        return parse_docker_size(str(rows.get(kind, {}).get(field_name, ""))) or 0

    def count(kind: str) -> int:
        raw = str(rows.get(kind, {}).get("TotalCount", "0"))
        return int(raw) if raw.isdigit() else 0

    return box_health.Docker(
        images=count("Images"),
        images_b=size("Images", "Size"),
        images_reclaim_b=size("Images", "Reclaimable"),
        volumes=count("Local Volumes"),
        volumes_b=size("Local Volumes", "Size"),
        build_cache_b=size("Build Cache", "Size"),
        build_reclaim_b=size("Build Cache", "Reclaimable"),
    )


# ── the slow pass ───────────────────────────────────────────────────────────


def _home_of(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_dir
    except KeyError:
        return None


def label_of(uid: int | None, users: UsersMap) -> str:
    """The card's name for *uid*: the map, else the login name, else ``User <id>``;
    ``System`` below 1000."""
    if uid is not None and uid in users.uids:
        return users.uids[uid]
    if uid is None or uid < 1000:
        return "System"
    try:
        login = pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"User {uid}"
    return login if LABEL_RE.fullmatch(login) else f"User {uid}"


def _where(path: str) -> str:
    return "tmp" if path.startswith("/tmp/") else "cache"


def claude_row(scan: box_health_scan.TreeScan, where: str) -> box_health.ClaudeRoot | None:
    """A Claude root as a post row, or ``None`` for one that is simply absent."""
    if not scan.exists:
        return None
    if not scan.readable:
        if scan.top_age_days is None:
            return None
        return box_health.ClaudeRoot(
            where=where,  # type: ignore[arg-type]
            readable=False,
            top_age_days=scan.top_age_days,
        )
    return box_health.ClaudeRoot(
        where=where,  # type: ignore[arg-type]
        projects=scan.projects,
        sessions=scan.sessions,
        stale_sessions=scan.stale_sessions,
        files=scan.files,
        bytes=scan.bytes,
        stale_files=scan.stale_files,
        stale_bytes=scan.stale_bytes,
        oldest_days=scan.oldest_days,
        partial=scan.partial,
    )


def pytest_row(scan: box_health_scan.TreeScan) -> box_health.PytestTemp:
    return box_health.PytestTemp(
        exists=scan.exists,
        readable=scan.readable,
        entries=scan.sessions,
        stale=scan.stale_sessions,
        files=scan.files,
        bytes=scan.bytes,
        stale_files=scan.stale_files,
        stale_bytes=scan.stale_bytes,
    )


def litter_rows(groups: Mapping[str, box_health_scan.LitterAgg]) -> list[box_health.LitterGroup]:
    """Litter groups, the biggest 9 and the rest merged into ``other`` (the post caps at 10)."""
    ranked = sorted(groups.items(), key=lambda item: -item[1].entries)
    keep, rest = ranked[:9], ranked[9:]
    merged = dict(keep)
    if rest:
        other = merged.setdefault(box_health_scan.OTHER_GROUP, box_health_scan.LitterAgg())
        for _, agg in rest:
            other.entries += agg.entries
            other.stale += agg.stale
            other.inodes += agg.inodes
            other.bytes += agg.bytes
            other.unreadable += agg.unreadable
            other.measured += agg.measured
            other.partial = other.partial or agg.partial
    return [
        box_health.LitterGroup(
            group=name,
            entries=agg.entries,
            stale=agg.stale,
            inodes=None,
            bytes=agg.bytes,
            unreadable=0,
            partial=False,
        )
        for name, agg in sorted(merged.items(), key=lambda item: -item[1].entries)
    ]


def scan_worktrees(
    root: str, runner: Runner, now: float, clock: Callable[[], float] = time.monotonic
) -> box_health.Worktrees | None:
    """Count the worktrees in *root*, their size, how many are old and how many have changes.

    Only counts leave this function. The ``git status`` checks share a 20 second
    budget; if it runs out, ``with_changes`` is a lower bound and ``partial`` says so.
    """
    try:
        names = sorted(e.path for e in os.scandir(root) if e.is_dir(follow_symlinks=False))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RelayError(
            f"the worktrees folder could not be listed ({exc.__class__.__name__})"
        ) from exc
    old = 0
    cutoff = now - WORKTREE_DAYS * 86400
    for path in names:
        newest = 0.0
        with contextlib.suppress(OSError):
            newest = os.lstat(path).st_mtime
            with os.scandir(path) as handle:
                for entry in handle:
                    with contextlib.suppress(OSError):
                        newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
        if newest < cutoff:
            old += 1
    size: int | None = None
    du = runner.run(["du", "-skx", root], HOST_DU_TIMEOUT_S)
    if du.returncode == 0 and du.out.split():
        head = du.out.split()[0]
        size = int(head) * 1024 if head.isdigit() else None
    deadline = clock() + CHANGES_BUDGET_S
    changed = 0
    partial = False
    for path in names:
        if clock() > deadline:
            partial = True
            break
        done = runner.run(
            ["git", "-C", path, "status", "--porcelain", "--ignore-submodules=all"],
            CHANGES_TIMEOUT_S,
            env={"GIT_OPTIONAL_LOCKS": "0"},
        )
        if done.returncode == 0 and done.out.strip():
            changed += 1
    return box_health.Worktrees(
        count=len(names), bytes=size, older_14d=old, with_changes=changed, partial=partial
    )


def _container_stream(
    runner: Runner, argv: list[str], settings: Settings, max_lines: int
) -> StreamResult:
    if not matches_container_template(
        argv, list(settings.users.containers), settings.container_user
    ):
        raise RelayError("refused a container command that is not in the fixed table")
    return runner.stream(argv, CONTAINER_TIMEOUT_S + 10, max_lines)


def _container_run(
    runner: Runner, argv: list[str], settings: Settings, timeout: float
) -> RunResult:
    if not matches_container_template(
        argv, list(settings.users.containers), settings.container_user
    ):
        raise RelayError("refused a container command that is not in the fixed table")
    return runner.run(argv, timeout)


def _part(label: str, suffix: str = "") -> str:
    """A failed part's name for a container: the label clipped so the part stays under 48."""
    return f"container {label[:24]}{suffix}"


def _volume_key(name: str) -> str:
    """A volume by a short hash of its name: the cache on disk never holds a volume name."""
    return hashlib.sha256(name.encode()).hexdigest()[:16]


def volume_sizes(runner: Runner, cache: dict[str, Any], now: float) -> dict[str, int]:
    """Every Docker volume's size, from ``docker system df -v``: heavy, and sizes change
    slowly, so it runs at most once a day and the answer is cached in the state, by
    a hash of each volume's name.

    :raises RelayError: When it cannot be read and nothing is cached.
    """
    held = cache.get("volumes")
    fresh = isinstance(held, dict) and now - float(held.get("t", 0)) < VOLUMES_TTL_S
    if fresh:
        return {str(k): int(v) for k, v in held["sizes"].items()}
    df = runner.run(CONTAINER_COMMANDS["volumes"], DF_TIMEOUT_S)
    if df.returncode != 0:
        if isinstance(held, dict):
            return {str(k): int(v) for k, v in held["sizes"].items()}
        raise RelayError("docker system df -v did not answer")
    sizes: dict[str, int] = {}
    for line in df.out.splitlines():
        with contextlib.suppress(ValueError, AttributeError):
            for volume in json.loads(line).get("Volumes", []):
                size = parse_docker_size(str(volume.get("Size", "")))
                if size is not None:
                    sizes[_volume_key(str(volume.get("Name", "")))] = size
    cache["volumes"] = {"t": now, "sizes": sizes}
    return sizes


def scan_container(
    name: str,
    label: str,
    settings: Settings,
    runner: Runner,
    *,
    now: float,
    failed: list[str],
    volumes: Callable[[], dict[str, int]],
    aged: list[box_health.AutoAged] | None = None,
) -> box_health.UserHealth:
    """One mapped container, through the fixed read-only commands. Only counts come back.

    A walk that ends with a nonzero exit status (``timeout`` answers 124) is partial, and a
    partial walk never calls a session stale (2026-10-28, review of PR 389).
    """
    user = settings.container_user
    health = box_health.UserHealth(label=label, kind="container")

    def argv(key: str, n: str = "") -> list[str]:
        return container_argv(key, container=name, user=user, n=n)

    unreadable: set[str] = set()

    def cut(result: StreamResult, what: str, *, missing_ok: bool = False, key: str = "") -> bool:
        """Whether *result* is incomplete; an incomplete walk is also a failed part.

        ``find`` exits 1 whenever one subfolder cannot be read, and still prints the rest,
        so exit 1 with output is a complete walk of what is readable: it is noted in
        *unreadable* (stderr is discarded, so the folders cannot be counted) and is not a
        failed part. Only a cut (a line cap), 124 (``timeout`` fired) or a signal exit
        (128 or more) is partial. Exit 1 with no output is *missing_ok*'s answer (no such
        folder) or a failure."""
        code = result.returncode
        if result.truncated:
            return True
        if code == 0:
            return False
        if code == 1 and result.lines:
            if key:
                unreadable.add(key)
            return False
        if code == 1 and missing_ok:
            return False
        failed.append(_part(label, f" {what}"))
        return True

    part = _part(label)
    try:
        ps = _container_run(runner, argv("ps"), settings, 60)
    except (RelayError, ValueError):
        failed.append(part)
        return health
    if ps.returncode != 0:
        failed.append(part)
        return health
    row: dict[str, Any] = {}
    for line in ps.out.splitlines():
        with contextlib.suppress(ValueError):
            candidate = json.loads(line)
            if isinstance(candidate, dict):
                row = candidate
                break
    health.running = bool(row) and str(row.get("State", "running")) == "running"
    if not health.running:
        return health
    health.writable_b = parse_docker_size(str(row.get("Size", "")))

    # Top-level /tmp: the Claude root, pytest temp and litter, as counts.
    stale_s = settings.stale_days * 86400
    listing = _container_stream(runner, argv("tmp_list"), settings, settings.max_entries)
    if listing.returncode != 0 and not listing.lines:
        failed.append(part)
        return health
    claude_ns: list[str] = []
    has_pytest = False
    groups: dict[str, box_health_scan.LitterAgg] = {}
    for line in listing.lines:
        fields = line.rstrip("\n").split("\t", 3)
        if len(fields) != 4:
            continue
        kind, _uid, mtime, entry = fields
        if box_health_scan.CLAUDE_TOP_RE.fullmatch(entry) and kind == "d":
            claude_ns.append(entry.removeprefix("claude-"))
            continue
        if entry == f"pytest-of-{user}" and kind == "d":
            has_pytest = True
            continue
        if box_health_scan.PYTEST_TOP_RE.fullmatch(entry):
            continue
        agg = groups.setdefault(box_health_scan.group_of(entry), box_health_scan.LitterAgg())
        agg.entries += 1
        with contextlib.suppress(ValueError):
            if float(mtime) < now - stale_s:
                agg.stale += 1
    health.litter = litter_rows(groups)

    roots = []
    removals: list[tuple[str, int, int, int | None]] = []
    """(path, entries, bytes, index into *roots*) of folders the hourly aging may remove."""
    for digits in claude_ns[:2]:
        tree = _container_stream(
            runner, argv("claude_tree", digits), settings, settings.max_entries
        )
        incomplete = cut(tree, "scan", key=f"claude{digits}")
        scan = box_health_scan.aggregate_claude(
            tree.lines,
            now=now,
            stale_days=settings.stale_days,
            truncated=incomplete,
        )
        scan.top_age_days = None
        if claude := claude_row(scan, "container"):
            claude.had_unreadable = f"claude{digits}" in unreadable
            roots.append(claude)
            if tree.returncode == 0 and not incomplete:
                for key, files, size, _newest in box_health_scan.stale_claude_groups(
                    tree.lines, now=now, stale_days=settings.stale_days
                ):
                    project, session = key
                    removals.append(
                        (f"/tmp/claude-{digits}/{project}/{session}", files, size, len(roots) - 1)
                    )
    health.claude = roots[:3]
    if has_pytest:
        tree = _container_stream(runner, argv("pytest_tree"), settings, settings.max_entries)
        incomplete = cut(tree, "scan")
        health.pytest = pytest_row(
            box_health_scan.aggregate_pytest(
                tree.lines,
                now=now,
                stale_days=settings.stale_days,
                truncated=incomplete,
            )
        )
        if tree.returncode == 0 and not incomplete:
            for (run_name,), files, size, _newest in box_health_scan.stale_pytest_groups(
                tree.lines, now=now, stale_days=settings.stale_days
            ):
                removals.append((f"/tmp/pytest-of-{user}/{run_name}", files, size, None))
    if settings.cleanup and aged is not None and removals:
        if settings.dry_run:
            # A dry run says what it would age and removes nothing.
            aged.append(
                box_health.AutoAged(
                    owner=label, count=len(removals), bytes=sum(r[2] for r in removals)
                )
            )
            removals = []
        removed = container_remove(name, settings, runner, [r[0] for r in removals])
        done = removed.done
        gone = [r for r in removals if r[0] in done]
        if removed.failed:
            failed.append(_part(label, " cleanup"))
        for _path, files, size, index in gone:
            if index is None and health.pytest is not None:
                p = health.pytest
                p.entries, p.stale = max(0, p.entries - 1), max(0, p.stale - 1)
                p.files, p.bytes = max(0, p.files - files), max(0, p.bytes - size)
            elif index is not None:
                c = health.claude[index]
                c.sessions, c.stale_sessions = max(0, c.sessions - 1), max(0, c.stale_sessions - 1)
                c.files, c.bytes = max(0, c.files - files), max(0, c.bytes - size)
                c.stale_files, c.stale_bytes = (
                    max(0, c.stale_files - files),
                    max(0, c.stale_bytes - size),
                )
        if gone:
            aged.append(
                box_health.AutoAged(owner=label, count=len(gone), bytes=sum(r[2] for r in gone))
            )

    # Worktrees: count and ages from top-level mtimes only; the size from du.
    wt = _container_stream(runner, argv("worktrees_list"), settings, 5000)
    wt_cut = cut(wt, "scan", missing_ok=True)
    if wt.lines:
        stamps = []
        for line in wt.lines:
            with contextlib.suppress(ValueError):
                stamps.append(float(line))
        size: int | None = None
        du = _container_run(runner, argv("worktrees_du"), settings, DU_TIMEOUT_S + 10)
        if du.returncode == 0 and du.out.split()[:1] and du.out.split()[0].isdigit():
            size = int(du.out.split()[0]) * 1024
        health.worktrees = box_health.Worktrees(
            count=len(stamps),
            bytes=size,
            older_14d=sum(1 for s in stamps if s < now - WORKTREE_DAYS * 86400),
            with_changes=None,
            partial=wt_cut,
        )

    # The home volume's size: find the volume mounted at the container user's home.
    try:
        mounts = _container_run(runner, argv("inspect"), settings, 30)
        home_volume = None
        if mounts.returncode == 0:
            for mount in json.loads(mounts.out or "[]"):
                if mount.get("Destination") == f"/home/{user}" and mount.get("Type") == "volume":
                    home_volume = str(mount.get("Name", ""))
        if home_volume:
            health.home_b = volumes().get(_volume_key(home_volume))
    except (RelayError, ValueError, TypeError, AttributeError):
        failed.append(_part(label, " volume"))
    return health


@dataclass
class RemoveResult:
    """What a container removal did: the paths removed, the fixed reasons for those kept,
    and whether a check failed or was cut short (then nothing in that batch was removed)."""

    done: set[str] = field(default_factory=set)
    skipped: Counter[str] = field(default_factory=Counter)
    failed: bool = False
    partial: bool = False


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def container_guard(
    name: str,
    settings: Settings,
    runner: Runner,
    batch: Sequence[str],
    *,
    now: float,
) -> tuple[set[str], Counter[str]] | None:
    """The paths of *batch* that are safe to remove right now, with the reasons for the rest;
    ``None`` when a check failed or was cut short (nothing is removed then).

    Run immediately before each ``rm`` batch, as the container user, through fixed argument
    lists: (1) anything inside a path changed after the cutoff keeps it; (2) a path that a
    process's working folder or open file is inside, or that sits inside one (other than
    ``/`` and ``/tmp``), keeps it; (3) a session uuid on any process's command line keeps
    it (``--resume`` and ``--session-id``, as the host temp guard does). Only link targets,
    paths and uuids are read; they stay in this process."""
    user = settings.container_user
    cutoff = int(now - settings.stale_days * 86400)
    keep: dict[str, str] = {}

    def run(argv: list[str], cap: int, ok_codes: tuple[int, ...] = (0,)) -> list[str] | None:
        result = _container_stream(runner, argv, settings, cap)
        if result.truncated or result.returncode not in ok_codes:
            return None
        return result.lines

    fresh = run(container_fresh_argv(name, user, batch, cutoff), 10_000)
    if fresh is None:
        return None
    for line in fresh:
        hit = line.rstrip("\n")
        keep.update({p: "recent" for p in batch if _under(hit, p)})

    # Working folders and open files of the container user's processes. A process that set
    # PR_SET_DUMPABLE=0 is missing from the link listing and still lets ``find`` exit 1, so
    # a cwd line is required for every pid of the user (read from the world-readable
    # ``status``); on any gap look once more, then remove nothing.
    uid_lines = run(container_argv("uid", container=name, user=user), 10)
    if not uid_lines or not re.fullmatch(r"[0-9]{1,10}", uid_lines[0].strip()):
        return None
    uid = uid_lines[0].strip()
    targets: list[str] = []
    for _attempt in range(2):
        statuses = run(container_argv("uids", container=name, user=user), 200_000, (0, 1))
        links = run(container_argv("procs", container=name, user=user), 200_000, (0, 1))
        if statuses is None or links is None:
            return None
        try:
            gap, targets = _proc_gap(statuses, links, uid, _uids_helper_argvs(name, user))
        except ValueError:
            gap = {"unread"}  # a half-read or odd listing never looks like "no process of ours"
        if not gap:
            break
    else:
        return None
    for target in targets:
        keep.update({p: "running" for p in batch if _under(target, p) or _under(p, target)})

    cmdlines = run(container_argv("cmdlines", container=name, user=user), 200_000)
    if cmdlines is None:
        return None
    seen = set(re.findall(box_health_scan.UUID_RE.pattern.strip("^$"), "".join(cmdlines)))
    keep.update({p: "running" for p in batch if os.path.basename(p) in seen})
    return {p for p in batch if p not in keep}, Counter(keep.values())


# Anchored record patterns for the ``uids`` listing. Every line of it must be one of these;
# anything else (a command line that held a newline, a half-read file) fails the batch.
_UID_REC = re.compile(r"/proc/([0-9]+)/status:Uid:\t([0-9]+)\t([0-9]+)(?:\t[0-9]+){2}")
_STATE_REC = re.compile(r"/proc/([0-9]+)/status:State:\t([A-Za-z]) \([^)\n]*\)")
_THREADS_REC = re.compile(r"/proc/([0-9]+)/status:Threads:\t([0-9]+)")
_CMD_REC = re.compile(r"/proc/([0-9]+)/cmdline:(.*)", re.S)
_LINK_REC = re.compile(r"/proc/([0-9]+)/(cwd|fd/[0-9]+)\t(.*)", re.S)
LINKS_MAX_CHARS = 8_000_000
_PROC_ARG = re.compile(r"/proc/[0-9]+/(?:status|cmdline)")


def _uids_helper_argvs(container: str, user: str) -> tuple[list[str], list[list[str]]]:
    """The argument lists the ``uids`` check itself runs inside the container, as the
    container sees them: ``timeout`` and its ``find`` child, and the two ``-exec`` prefixes
    that ``find`` hands paths to. Taken from the pinned template, not from a name."""
    full = container_argv("uids", container=container, user=user)
    inner = full[full.index(container) + 1 :]  # timeout 30 find ...
    execs: list[list[str]] = []
    for at, part in enumerate(inner):
        if part == "-exec":
            end = inner.index("{}", at)
            execs.append(inner[at + 1 : end])
    return inner, execs


def _is_helper(argv: list[str], inner: Sequence[str], execs: Sequence[Sequence[str]]) -> bool:
    """Whether *argv* is the ``uids`` check's own process: its ``timeout`` or ``find`` (the
    pinned argument list exactly) or the ``grep`` it execs (the pinned prefix, then only
    ``/proc/<pid>/status`` or ``cmdline`` paths)."""
    if argv in (list(inner), list(inner[2:])):
        return True
    for prefix in execs:
        k = len(prefix)
        if argv[:k] == list(prefix) and len(argv) > k:
            return all(_PROC_ARG.fullmatch(a) for a in argv[k:])
    return False


@dataclass
class _Proc:
    uid: str
    euid: str
    state: str
    threads: int
    argv: list[str] | None = None


def _read_procs(statuses: Sequence[str]) -> dict[str, _Proc]:
    """Fold the ``uids`` listing into one record per pid, failing closed on anything odd.

    :raises ValueError: When a line is not one of the anchored records (a command line that
        held a newline is split into several lines by ``grep``), when a pid has two records of
        one kind (a forged record, or the same), or when a pid lacks its ``Uid``, ``State`` or
        ``Threads`` record (a half-read listing), or when the listing is empty. The check's
        own ``find`` always appears in a good listing.
    """
    uid: dict[str, tuple[str, str]] = {}
    state: dict[str, str] = {}
    threads: dict[str, int] = {}
    cmd: dict[str, list[str]] = {}
    for line in statuses:
        text = line.removesuffix("\n")
        hit = _UID_REC.fullmatch(text)
        if hit:
            if hit.group(1) in uid:
                raise ValueError("a pid has two Uid records")
            uid[hit.group(1)] = (hit.group(2), hit.group(3))
            continue
        hit = _STATE_REC.fullmatch(text)
        if hit:
            if hit.group(1) in state:
                raise ValueError("a pid has two State records")
            state[hit.group(1)] = hit.group(2)
            continue
        hit = _THREADS_REC.fullmatch(text)
        if hit:
            if hit.group(1) in threads:
                raise ValueError("a pid has two Threads records")
            threads[hit.group(1)] = int(hit.group(2))
            continue
        hit = _CMD_REC.fullmatch(text)
        if hit:
            if hit.group(1) in cmd:
                raise ValueError("a pid has two command line records")
            # Exactly one trailing NUL ends the last argument; more are empty arguments.
            cmd[hit.group(1)] = hit.group(2).removesuffix("\0").split("\0")
            continue
        raise ValueError("a line of the status listing is not a record")
    if not uid:
        raise ValueError("the status listing holds no Uid line")
    if not (set(uid) == set(state) == set(threads) and set(cmd) <= set(uid)):
        raise ValueError("a pid is missing its Uid, State or Threads record")
    return {
        pid: _Proc(uid[pid][0], uid[pid][1], state[pid], threads[pid], cmd.get(pid)) for pid in uid
    }


def _read_links(links: Sequence[str]) -> list[tuple[str, str, str]]:
    """The ``procs`` listing as (pid, kind, target) records, failing closed on anything odd.

    The listing is NUL-framed (``-printf '%p\\t%l\\0'``), so a folder name that holds a newline
    or a tab stays inside its own record instead of reading as a second record.

    :raises ValueError: When the listing is too large, not NUL-terminated (cut short), has a
        record that is not ``/proc/<pid>/cwd`` or ``/proc/<pid>/fd/<n>`` + tab + target, or
        holds the same link twice.
    """
    text = "".join(links)
    if len(text) > LINKS_MAX_CHARS:
        raise ValueError("the link listing is too large")
    if not text:
        return []
    if not text.endswith("\0"):
        raise ValueError("the link listing is not terminated")
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for record in text[:-1].split("\0"):
        hit = _LINK_REC.fullmatch(record)
        if not hit:
            raise ValueError("a link record is not well formed")
        pid, kind, target = hit.groups()
        if (pid, kind) in seen:
            raise ValueError("a link appears twice")
        seen.add((pid, kind))
        out.append((pid, kind, target))
    return out


def _proc_gap(
    statuses: Sequence[str],
    links: Sequence[str],
    uid: str,
    helper: tuple[Sequence[str], Sequence[Sequence[str]]] | None = None,
) -> tuple[set[str], list[str]]:
    """The pids of *uid* with no cwd line (one is a gap), and every path the listing showed.

    Link targets that are not paths (pipes, sockets) are left out. A zombie (state Z) holds
    nothing and has no cwd link, so it is skipped, but only when it has a single thread: a
    process whose main thread exited while others run also shows Z, its leader's links are
    unreadable and its live threads are not listed, so any other Z is a gap. The check's own
    processes are skipped, known by their command line matching the pinned argument lists.

    :raises ValueError: When either listing is not whole and well formed (see
        :func:`_read_procs` and :func:`_read_links`).
    """
    procs = _read_procs(statuses)
    # A pid is the container user's by real or effective uid (a setuid-style process too).
    mine = {pid for pid, proc in procs.items() if uid in (proc.uid, proc.euid)}
    forced = {pid for pid in mine if procs[pid].state == "Z" and procs[pid].threads != 1}
    mine -= {pid for pid in mine if procs[pid].state == "Z" and procs[pid].threads == 1}
    if helper is not None:
        mine = {
            pid
            for pid in mine
            if pid in forced or procs[pid].argv is None or not _is_helper(procs[pid].argv, *helper)
        }
    has_cwd: set[str] = set()
    targets: list[str] = []
    for pid, kind, target in _read_links(links):
        if kind == "cwd" and target:
            has_cwd.add(pid)
        if target.startswith("/") and target not in ("/", "/tmp"):
            targets.append(target)
    return (mine - has_cwd) | forced, targets


def container_remove(
    name: str,
    settings: Settings,
    runner: Runner,
    paths: Sequence[str],
    *,
    deadline: Deadline | None = None,
) -> RemoveResult:
    """Remove *paths* in *name* with the one fixed ``rm`` template, 200 at a time, each batch
    only after :func:`container_guard` cleared it. A dry run removes nothing. A path that
    fails the strict patterns is never sent."""
    result = RemoveResult()
    if settings.dry_run:
        return result
    ok = [p for p in paths if container_path_ok(p, settings.container_user)]
    for start in range(0, len(ok), RM_BATCH):
        if deadline is not None and deadline.expired:
            result.partial = True
            break
        batch = ok[start : start + RM_BATCH]
        guarded = container_guard(name, settings, runner, batch, now=time.time())
        if guarded is None:
            result.failed = True
            continue
        safe, reasons = guarded
        result.skipped.update(reasons)
        send = [p for p in batch if p in safe]
        if not send:
            continue
        argv = container_rm_argv(name, settings.container_user, send)
        if not matches_container_template(
            argv, list(settings.users.containers), settings.container_user
        ):
            raise RelayError("refused a container command that is not in the fixed table")
        if runner.run(argv, CONTAINER_TIMEOUT_S * 4).returncode == 0:
            result.done.update(send)
        else:
            result.failed = True
    return result


def container_clean(
    name: str,
    settings: Settings,
    runner: Runner,
    *,
    now: float,
    expected: int | None = None,
    deadline: Deadline | None = None,
) -> box_cleanup.Outcome:
    """The button version of a container cleanup: list, prove each folder old, remove.

    A listing that was cut or unreadable removes nothing from that root."""
    user = settings.container_user
    out = box_cleanup.Outcome()
    listing = _container_stream(
        runner,
        container_argv("tmp_list", container=name, user=user),
        settings,
        settings.max_entries,
    )
    if listing.returncode != 0 and not listing.lines:
        out.errors.append("EIO")
        return out
    digits_list: list[str] = []
    has_pytest = False
    for line in listing.lines:
        fields = line.rstrip("\n").split("\t", 3)
        if len(fields) == 4 and fields[0] == "d":
            if box_health_scan.CLAUDE_TOP_RE.fullmatch(fields[3]):
                digits_list.append(fields[3].removeprefix("claude-"))
            elif fields[3] == f"pytest-of-{user}":
                has_pytest = True
    items: list[tuple[str, int, int, float]] = []
    trees: list[tuple[str, StreamResult, bool]] = [
        (
            f"claude{d}",
            _container_stream(
                runner,
                container_argv("claude_tree", container=name, user=user, n=d),
                settings,
                settings.max_entries,
            ),
            True,
        )
        for d in digits_list[:2]
    ]
    if has_pytest:
        trees.append(
            (
                "pytest",
                _container_stream(
                    runner,
                    container_argv("pytest_tree", container=name, user=user),
                    settings,
                    settings.max_entries,
                ),
                False,
            )
        )
    for key, tree, is_claude in trees:
        if tree.truncated or tree.returncode != 0:
            out.skipped["unproven"] += 1
            out.partial = out.partial or tree.truncated
            continue
        if is_claude:
            n = key.removeprefix("claude")
            for (project, session), files, size, newest in box_health_scan.stale_claude_groups(
                tree.lines, now=now, stale_days=settings.stale_days, skipped=out.skipped
            ):
                items.append((f"/tmp/claude-{n}/{project}/{session}", files, size, newest))
        else:
            for (run_name,), files, size, newest in box_health_scan.stale_pytest_groups(
                tree.lines, now=now, stale_days=settings.stale_days, skipped=out.skipped
            ):
                items.append((f"/tmp/pytest-of-{user}/{run_name}", files, size, newest))
    # Oldest first (ties by path) before any limit, so the item cap and the confirmed count
    # both keep the oldest.
    items.sort(key=lambda i: (i[3], i[0]))
    if len(items) > box_cleanup.MAX_ITEMS:
        items = items[: box_cleanup.MAX_ITEMS]
        out.partial = True
    if expected is not None and len(items) > expected:
        # Only the confirmed number of the oldest go; the rest wait for the next run.
        out.skipped["over_confirmed"] += len(items) - max(expected, 0)
        del items[max(expected, 0) :]
    removed = container_remove(name, settings, runner, [i[0] for i in items], deadline=deadline)
    for path, _files, size, _newest in items:
        if path in removed.done:
            out.removed += 1
            out.bytes_freed += size
    out.skipped.update(removed.skipped)
    out.partial = out.partial or removed.partial
    if removed.failed:
        out.errors.append("CHECK_FAILED")
    return out


def host_roots(settings: Settings, home: str, uid: int | None = None) -> list[str]:
    """This user's Claude temp roots, expanded."""
    uid = os.getuid() if uid is None else uid
    return [_expand(t.replace("{uid}", str(uid)), home) for t in settings.claude_roots]


def build_cleanup(
    settings: Settings,
    users: Sequence[box_health.UserHealth],
    docker: box_health.Docker | None,
    aged: Sequence[box_health.AutoAged],
    *,
    now: float,
    tmp_root: str,
) -> box_health.Cleanup:
    """The cleanup offer: what each button would remove now, from the scan just made.

    The owner's own host items are planned by :mod:`box_cleanup` in dry-run (counts only); a
    container's come from its scan; Docker's from the reclaimable size."""
    if not settings.cleanup:
        return box_health.Cleanup(enabled=False)
    me = os.getuid()
    items: list[box_health.OfferItem] = []
    for user in users:
        if user.kind == "container":
            n = sum(c.stale_sessions for c in user.claude) + (
                user.pytest.stale if user.pytest else 0
            )
            files = sum(c.stale_files for c in user.claude)
            size = sum(c.stale_bytes for c in user.claude)
            if user.pytest:
                files += user.pytest.stale_files
                size += user.pytest.stale_bytes
            if user.running and n:
                items.append(
                    box_health.OfferItem(
                        action="claude_scratch",
                        owner=user.label,
                        where="container",
                        count=n,
                        files=files,
                        bytes=size,
                    )
                )
        elif user.uid == me:
            n = sum(c.stale_sessions for c in user.claude if c.readable)
            if n:
                items.append(
                    box_health.OfferItem(
                        action="claude_scratch",
                        owner=user.label,
                        count=n,
                        files=sum(c.stale_files for c in user.claude),
                        bytes=sum(c.stale_bytes for c in user.claude),
                    )
                )
            plans = {
                "tmp_litter": box_cleanup.plan_tmp_litter(
                    tmp_root,
                    now=now,
                    stale_days=settings.stale_days,
                    uid=me,
                    clock=box_cleanup.Clock(settings.max_entries, settings.max_seconds),
                ),
                "review_checkouts": box_cleanup.plan_review_checkouts(
                    tmp_root,
                    now=now,
                    stale_days=settings.stale_days,
                    uid=me,
                    clock=box_cleanup.Clock(settings.max_entries, settings.max_seconds),
                ),
            }
            for action, plan in plans.items():
                if plan.count:
                    items.append(
                        box_health.OfferItem(
                            action=action,  # type: ignore[arg-type]
                            owner=user.label,
                            count=plan.count,
                            files=plan.files,
                            bytes=plan.bytes,
                            groups=[
                                box_health.OfferGroup(group=g, count=c)
                                for g, c in sorted(plan.groups().items(), key=lambda kv: -kv[1])
                            ][:12],
                        )
                    )
    if docker is not None:
        reclaim = docker.images_reclaim_b + docker.build_reclaim_b
        if reclaim > 0:
            items.append(
                box_health.OfferItem(
                    action="docker_prune", owner="all", where="all", count=1, bytes=reclaim
                )
            )
    return box_health.Cleanup(enabled=True, actions=items[:12], auto_aged=list(aged)[:5])


REASON_SET = frozenset(box_health.SKIP_REASONS)


def run_cleanup_request(
    request: Mapping[str, Any],
    settings: Settings,
    runner: Runner,
    *,
    now: float,
    home: str,
    tmp_root: str = "/tmp",
    deadline_s: float = CLEANUP_DEADLINE_S,
) -> dict[str, Any]:
    """Run the server's queued cleanup and give the result body to post. Never raises.

    The run is bound to what the admin confirmed: when the fresh plan holds more items than
    the request's ``count``, only that many of the oldest are removed and the rest are counted
    as ``over_confirmed``, left for the next run. One overall
    deadline (``deadline_s``, under the unit's timeout) stops it cleanly with a partial result.

    Only an action and an owner the relay itself offers are run: this user's host items,
    a mapped container's temp folders, and Docker."""
    action = str(request.get("action", ""))
    owner = str(request.get("owner", ""))
    result: dict[str, Any] = {
        "id": request.get("id"),
        "ok": False,
        "removed": 0,
        "bytes_freed": 0,
        "skipped": {},
        "errors": [],
        "partial": False,
    }
    outcome = box_cleanup.Outcome()
    deadline = Deadline(deadline_s)
    # Every command, planning included, gets only the time left: an expired deadline answers
    # 124, so a hung docker daemon ends in a partial result that still posts.
    runner = DeadlineRunner(runner, deadline)
    count = request.get("count")
    expected = count if isinstance(count, int) and not isinstance(count, bool) else None
    try:
        if not settings.cleanup:
            outcome.errors.append("DISABLED")
        elif action not in box_cleanup.ACTIONS:
            outcome.errors.append("NOT_POSSIBLE")
        else:
            me_label = label_of(os.getuid(), settings.users)
            container = next(
                (n for n, label in settings.users.containers.items() if label == owner), None
            )
            if action == "claude_scratch" and container is not None:
                outcome = container_clean(
                    container, settings, runner, now=now, expected=expected, deadline=deadline
                )
            elif (action == "docker_prune" and owner == "all") or (
                action != "docker_prune" and owner == me_label
            ):

                def docker_run(argv: Sequence[str], timeout: float) -> tuple[int, str]:
                    done = runner.run(argv, timeout)
                    return done.returncode, done.out

                outcome = box_cleanup.host_run(
                    action,  # type: ignore[arg-type]
                    roots=host_roots(settings, home),
                    tmp=tmp_root,
                    now=now,
                    stale_days=settings.stale_days,
                    max_entries=settings.max_entries,
                    max_seconds=settings.max_seconds,
                    docker_run=docker_run,
                    expected=None if action == "docker_prune" else expected,
                    remove_seconds=max(1.0, deadline.remaining()),
                )
            else:
                outcome.errors.append("NOT_POSSIBLE")
    except box_cleanup.Refused as exc:
        _logger.warning("cleanup refused: %s", exc)
        outcome.errors.append("REFUSED")
    except Exception as exc:  # noqa: BLE001 (the result is posted whatever happens)
        _logger.warning("cleanup: %s", exc.__class__.__name__)
        outcome.errors.append("EIO")
    if deadline.expired:
        outcome.partial = True
    result.update(
        ok=outcome.removed > 0 or not outcome.errors,
        removed=outcome.removed,
        bytes_freed=outcome.bytes_freed,
        skipped={k: v for k, v in outcome.skipped.items() if k in REASON_SET and v},
        errors=outcome.errors[:20],
        partial=outcome.partial,
        finished_at=datetime.fromtimestamp(time.time(), UTC).isoformat(),
    )
    return result


class Deadline:
    """One overall time limit for the slow pass."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._end = clock() + seconds

    def remaining(self) -> float:
        return self._end - self._clock()

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0


class DeadlineRunner:
    """A runner that never lets one command outlast the slow pass's deadline."""

    def __init__(self, inner: Runner, deadline: Deadline) -> None:
        self._inner = inner
        self._deadline = deadline

    def run(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str] | None = None
    ) -> RunResult:
        left = self._deadline.remaining()
        if left <= 0:
            return RunResult(124)
        return self._inner.run(argv, min(timeout, max(1.0, left)), env)

    def stream(self, argv: Sequence[str], timeout: float, max_lines: int) -> StreamResult:
        left = self._deadline.remaining()
        if left <= 0:
            return StreamResult(returncode=124)
        return self._inner.stream(argv, min(timeout, max(1.0, left)), max_lines)


@contextlib.contextmanager
def _guard(failed: list[str], part: str) -> Iterator[None]:
    """Run one part of a pass: if it fails, name it and go on. Only our own messages and the
    exception's class are ever logged, never its text (it can quote a path)."""
    try:
        yield
    except RelayError as exc:
        _logger.warning("%s: %s", part, exc)
        failed.append(part)
    except Exception as exc:  # noqa: BLE001 (any one part may fail; the rest is still posted)
        _logger.warning("%s: %s", part, exc.__class__.__name__)
        failed.append(part)


def scan_dirs(
    settings: Settings,
    home: str,
    skip: frozenset[str],
    deadline: Deadline | None = None,
) -> list[box_health.DirSize]:
    """The size of each configured folder, by its label; the path is never posted."""
    out = []
    for label, raw in settings.dirs:
        if deadline is not None and deadline.expired:
            break
        path = _expand(raw, home)
        seconds = settings.max_seconds
        if deadline is not None:
            seconds = min(seconds, max(deadline.remaining(), 0.1))
        budget = box_health_scan.Budget(settings.max_entries, seconds)
        result = box_health_scan.walk(path, budget, skip)
        if not os.path.isdir(path):
            continue
        out.append(
            box_health.DirSize(
                label=label, bytes=result.bytes, files=result.entries, partial=result.partial
            )
        )
    return out[:6]


def _merge_litter(
    into: dict[str, box_health_scan.LitterAgg], groups: Mapping[str, box_health_scan.LitterAgg]
) -> None:
    for name, agg in groups.items():
        target = into.setdefault(name, box_health_scan.LitterAgg())
        target.entries += agg.entries
        target.stale += agg.stale
        target.inodes += agg.inodes
        target.bytes += agg.bytes
        target.unreadable += agg.unreadable
        target.measured += agg.measured
        target.partial = target.partial or agg.partial


def slow_scan(
    settings: Settings,
    runner: Runner,
    *,
    now: float,
    failed: list[str],
    home: str | None = None,
    tmp_root: str = "/tmp",
    deadline: Deadline | None = None,
    cache: dict[str, Any] | None = None,
) -> box_health.SlowScan:
    """The hourly scans: per user, then Docker, the journal, folder sizes and each container.

    Everything runs under one overall *deadline* (``SLOW_DEADLINE_S`` by default): a
    command is cut to the time left, a part not reached is named as failed
    (``slow scans cut short``), and what was measured is still returned. *cache* is the
    relay's state, where the once-a-day Docker volume sizes are kept.
    """
    home = home or str(Path.home())
    deadline = deadline or Deadline(SLOW_DEADLINE_S)
    cache = cache if cache is not None else {}
    runner = DeadlineRunner(runner, deadline)
    users_map = settings.users
    cut = False

    def over() -> bool:
        nonlocal cut
        if deadline.expired:
            if not cut:
                failed.append("slow scans cut short")
            cut = True
        return cut

    def budget() -> box_health_scan.Budget:
        return box_health_scan.Budget(
            settings.max_entries, min(settings.max_seconds, max(deadline.remaining(), 0.1))
        )

    tmp = box_health_scan.TmpScan()
    with _guard(failed, "tmp"):
        tmp = box_health_scan.scan_tmp(
            tmp_root, now=now, stale_days=settings.stale_days, budget=budget()
        )
        if tmp.partial:
            failed.append("tmp scan cut short")
    locks: frozenset[tuple[int, int, int]] = frozenset()
    live: set[str] = set()
    with _guard(failed, "live sessions"):
        locks = box_health_scan.read_locks()
        live = box_health_scan.live_session_dirs(
            [_expand(t.replace("{uid}", str(os.getuid())), home) for t in settings.claude_roots],
            os.getuid(),
        )

    uids = sorted(
        {
            *users_map.uids,
            *(u for u in tmp.litter if u >= 1000),
            *(u for u in tmp.claude_dirs if u >= 1000),
        }
    )
    me = os.getuid()
    result: list[box_health.UserHealth] = []
    claude_paths: set[str] = set()
    for uid in uids:
        label = label_of(uid, users_map)
        health = box_health.UserHealth(label=label, kind="box", uid=uid)
        result.append(health)
        health.litter = litter_rows(tmp.litter.get(uid, {}))
        if over():
            continue
        with _guard(failed, f"user {label[:24]}"):
            user_home = _home_of(uid) or ""
            for template in settings.claude_roots:
                if "~" in template and not user_home:
                    continue
                path = _expand(template.replace("{uid}", str(uid)), user_home)
                claude_paths.add(path)
                scan = box_health_scan.scan_claude_root(
                    path,
                    now=now,
                    stale_days=settings.stale_days,
                    budget=budget(),
                    locks=locks,
                    live=live,
                )
                if row := claude_row(scan, _where(path)):
                    health.claude.append(row)
            if uid in tmp.pytest_dirs:
                health.pytest = pytest_row(
                    box_health_scan.scan_pytest_root(
                        tmp.pytest_dirs[uid],
                        now=now,
                        stale_days=settings.stale_days,
                        budget=budget(),
                    )
                )
            health.claude = health.claude[:3]
        if uid == me and not over():
            with _guard(failed, "worktrees"):
                health.worktrees = scan_worktrees(_expand(settings.worktrees, home), runner, now)
    system: dict[str, box_health_scan.LitterAgg] = {}
    for uid, groups in tmp.litter.items():
        if uid < 1000 and uid not in users_map.uids:
            _merge_litter(system, groups)
    if system:
        result.append(
            box_health.UserHealth(label="System", kind="box", litter=litter_rows(system))
        )

    def volumes() -> dict[str, int]:
        return volume_sizes(runner, cache, now)

    aged: list[box_health.AutoAged] = []
    for name, label in users_map.containers.items():
        if over():
            break
        with _guard(failed, _part(label)):
            result.append(
                scan_container(
                    name,
                    label,
                    settings,
                    runner,
                    now=now,
                    failed=failed,
                    volumes=volumes,
                    aged=aged,
                )
            )

    docker: box_health.Docker | None = None
    if not over():
        with _guard(failed, "docker"):
            docker = read_docker(runner)
    journal_b: int | None = None
    if not over():
        with _guard(failed, "journal"):
            journal = runner.run(HOST_COMMANDS["journal"], 20)
            if journal.returncode == 0:
                journal_b = parse_journal_usage(journal.out)
            if journal_b is None:
                raise RelayError("journalctl --disk-usage did not answer")
    dirs: list[box_health.DirSize] = []
    if not over():
        with _guard(failed, "folders"):
            dirs = scan_dirs(settings, home, frozenset(claude_paths), deadline)
    over()
    cleanup: box_health.Cleanup | None = None
    with _guard(failed, "cleanup"):
        cleanup = build_cleanup(settings, result, docker, aged, now=now, tmp_root=tmp_root)
    return box_health.SlowScan(
        read_at=datetime.fromtimestamp(now, UTC),
        stale_days=settings.stale_days,
        slow_minutes=settings.slow_minutes,
        users=result[:5],
        docker=docker,
        dirs=dirs,
        journal_b=journal_b,
        cleanup=cleanup,
        settings={
            "AE_BOX_HEALTH_STALE_DAYS": f"{settings.stale_days:g}",
            "AE_BOX_HEALTH_SLOW_MINUTES": str(settings.slow_minutes),
            "AE_BOX_HEALTH_WALK_MAX_ENTRIES": str(settings.max_entries),
            "AE_BOX_HEALTH_WALK_MAX_SECONDS": f"{settings.max_seconds:g}",
        },
    )


# ── one run ─────────────────────────────────────────────────────────────────


@dataclass
class Roots:
    """Where the relay reads the box; tests point these at a fake tree."""

    sys: Path = Path("/sys")
    proc: Path = Path("/proc")
    mounts: Path = Path("/proc/self/mounts")
    tmp: str = "/tmp"


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Write the state file atomically, readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".box-health-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@dataclass
class Fast:
    """The fast pass's readings, before the slow scans join them."""

    sensors: list[box_health.Sensor] = field(default_factory=list)
    gpu_power: float | None = None
    cpu: box_health.Cpu | None = None
    counters: dict[str, Any] = field(default_factory=dict)
    memory: box_health.Memory | None = None
    mounts: list[box_health.Mount] = field(default_factory=list)
    failed_units: list[str] = field(default_factory=list)
    failed_units_other: int = 0
    failed_user_units: list[str] = field(default_factory=list)
    failed_user_units_other: int = 0
    guard: box_health.Guard | None = None
    guard_streak: dict[str, Any] = field(default_factory=dict)
    uptime_s: int | None = None
    stamp: float = 0.0
    failed: list[str] = field(default_factory=list)


def collect_fast(
    settings: Settings,
    *,
    runner: Runner,
    roots: Roots | None = None,
    state: dict[str, Any] | None = None,
    stamp: float | None = None,
) -> Fast:
    """The fast pass: temperatures, load, memory, mounts, failed units and the temp guard."""
    roots = roots or Roots()
    state = state or {}
    fast = Fast(stamp=time.time() if stamp is None else stamp)
    failed = fast.failed
    if settings.users.bad:
        failed.append("users")
    with _guard(failed, "sensors"):
        fast.sensors, fast.gpu_power = read_sensors(roots.sys)
    with _guard(failed, "cpu"):
        fast.cpu, fast.counters = read_cpu(roots.proc, state.get("cpu_stat"))
    with _guard(failed, "memory"):
        fast.memory = read_memory(roots.proc)
    with _guard(failed, "mounts"):
        fast.mounts = read_mounts(roots.mounts, settings.skip_mounts)
    with _guard(failed, "units"):
        (
            fast.failed_units,
            fast.failed_units_other,
            fast.failed_user_units,
            fast.failed_user_units_other,
        ) = read_failed_units(runner)
    fast.guard, fast.guard_streak = read_guard(
        settings.guard_file, fast.stamp, previous=state.get("guard_unmatched")
    )
    uptime = (_read(roots.proc / "uptime") or "").split()
    if uptime and re.fullmatch(r"[0-9.]+", uptime[0]):
        fast.uptime_s = int(float(uptime[0]))
    return fast


def cached_slow(state: Mapping[str, Any]) -> box_health.SlowScan | None:
    cached = state.get("slow")
    if not isinstance(cached, dict):
        return None
    with contextlib.suppress(ValidationError, ValueError):
        return box_health.SlowScan.model_validate(cached)
    return None


def slow_due(
    settings: Settings, slow: box_health.SlowScan | None, stamp: float, *, force: bool = False
) -> bool:
    return (
        force
        or slow is None
        or stamp - slow.read_at.timestamp() >= settings.slow_minutes * 60 - 120
    )


def assemble(
    fast: Fast, slow: box_health.SlowScan | None, slow_failed: Sequence[str] = ()
) -> box_health.BoxHealthPost:
    """The post: the fast readings, a slow scan (new or cached), and every failed part."""
    parts = [p[: box_health.MAX_STRING] for p in [*fast.failed, *slow_failed]]
    return box_health.BoxHealthPost(
        host=_hostname(),
        read_at=datetime.fromtimestamp(fast.stamp, UTC),
        uptime_s=fast.uptime_s,
        failed=list(dict.fromkeys(parts))[:20],
        sensors=fast.sensors,
        gpu_power_w=fast.gpu_power,
        cpu=fast.cpu,
        memory=fast.memory,
        mounts=fast.mounts,
        failed_units=fast.failed_units,
        failed_user_units=fast.failed_user_units,
        failed_units_other=fast.failed_units_other,
        failed_user_units_other=fast.failed_user_units_other,
        guard=fast.guard,
        slow=slow,
    )


def run_slow(
    settings: Settings,
    runner: Runner,
    fast: Fast,
    cache: dict[str, Any],
    cached: box_health.SlowScan | None,
    *,
    roots: Roots | None = None,
    home: str | None = None,
    deadline_s: float = SLOW_DEADLINE_S,
) -> tuple[box_health.SlowScan | None, list[str]]:
    """The slow scans under their overall deadline: the new reading and its failed parts.

    A failure of the whole pass keeps the cached reading and says so."""
    roots = roots or Roots()
    failed: list[str] = []
    try:
        slow = slow_scan(
            settings,
            runner,
            now=fast.stamp,
            failed=failed,
            home=home,
            tmp_root=roots.tmp,
            deadline=Deadline(deadline_s),
            cache=cache,
        )
    except Exception as exc:  # noqa: BLE001 (the fast reading is posted whatever happens here)
        _logger.warning("slow scan: %s", exc.__class__.__name__)
        return cached, [*failed, "slow scans"]
    return slow, failed


def collect(
    settings: Settings,
    *,
    runner: Runner,
    roots: Roots | None = None,
    state: dict[str, Any] | None = None,
    now: float | None = None,
    force_slow: bool = False,
    home: str | None = None,
) -> tuple[box_health.BoxHealthPost, dict[str, Any], list[str]]:
    """Measure both passes into one post. Returns the post, the new state, and the parts that
    failed (also in the post). :func:`run` posts the fast pass first; this is the whole."""
    state = dict(state or {})
    fast = collect_fast(settings, runner=runner, roots=roots, state=state, stamp=now)
    slow = cached_slow(state)
    slow_failed: list[str] = []
    cache: dict[str, Any] = {"volumes": state.get("volumes")} if state.get("volumes") else {}
    if slow_due(settings, slow, fast.stamp, force=force_slow):
        slow, slow_failed = run_slow(settings, runner, fast, cache, slow, roots=roots, home=home)
    post_body = assemble(fast, slow, slow_failed)
    return post_body, new_state_of(fast, slow, cache, state), list(post_body.failed)


def new_state_of(
    fast: Fast,
    slow: box_health.SlowScan | None,
    cache: Mapping[str, Any],
    before: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "cpu_stat": fast.counters or before.get("cpu_stat"),
        "slow": slow.model_dump(mode="json") if slow else None,
        "guard_unmatched": fast.guard_streak,
        "volumes": cache.get("volumes") or before.get("volumes"),
    }


def _hostname() -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "", socket.gethostname())[: box_health.MAX_STRING]
    return name or "box"


async def post(client: httpx.AsyncClient, body: box_health.BoxHealthPost) -> int:
    """``POST /v1/ae/box-health``; how many samples the server now holds.

    :raises RelayError: On a transport failure or a non-2xx answer.
    """
    return int((await post_answer(client, body)).get("samples", 0))


async def post_result(client: httpx.AsyncClient, result: Mapping[str, Any]) -> None:
    """``POST /v1/ae/box-health/cleanup/result``.

    :raises RelayError: On a transport failure or a non-2xx answer.
    """
    path = "/v1/ae/box-health/cleanup/result"
    try:
        async with asyncio.timeout(POST_TIMEOUT_S):
            response = await client.post(path, json=dict(result), timeout=POST_TIMEOUT_S)
    except (httpx.HTTPError, TimeoutError) as exc:
        raise RelayError(f"POST {path} failed ({exc.__class__.__name__})") from exc
    if response.status_code >= 300:
        raise RelayError(f"POST {path} answered {response.status_code}; check the server's log")


async def post_answer(client: httpx.AsyncClient, body: box_health.BoxHealthPost) -> dict[str, Any]:
    """``POST /v1/ae/box-health``; the server's answer (``samples``, and ``cleanup`` when a
    box admin has queued a cleanup for this box to run).

    :raises RelayError: On a transport failure or a non-2xx answer.
    """
    path = "/v1/ae/box-health"
    try:
        # The httpx timeout is per phase (connect, write, read); this one is the whole post.
        async with asyncio.timeout(POST_TIMEOUT_S):
            response = await client.post(
                path, json=body.model_dump(mode="json"), timeout=POST_TIMEOUT_S
            )
    except (httpx.HTTPError, TimeoutError) as exc:
        raise RelayError(
            f"POST {path} failed ({exc.__class__.__name__}); is the server up?"
        ) from exc
    if response.status_code == 401:
        raise RelayError(
            f"POST {path} answered 401: the stored login expired; run `omnigent login "
            f"{client.base_url}` as this user"
        )
    if response.status_code == 403:
        raise RelayError(
            f"POST {path} answered 403: only an OmniGent admin may post; run `omnigent login "
            f"{client.base_url}` as the admin"
        )
    if response.status_code >= 300:
        raise RelayError(f"POST {path} answered {response.status_code}; check the server's log")
    answer = response.json()
    return answer if isinstance(answer, dict) else {}


async def run(
    *,
    settings: Settings,
    omni_settings: omni.Settings,
    runner: Runner | None = None,
    roots: Roots | None = None,
    dry_run: bool = False,
    force_slow: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
    out: Any = None,
    now: float | None = None,
    home: str | None = None,
) -> int:
    """Measure, then print (dry run) or post; the exit status.

    The fast reading is posted first, before any slow scan starts (2026-10-28, review of
    PR 389: a slow pass that outlasted the unit's timeout left the card stale during the
    very incident it exists for). When the slow scans are due they run under one overall
    deadline and a second post carries them.
    """
    out = out or sys.stdout
    runner = runner or SubprocessRunner()
    began = time.monotonic()
    if dry_run:
        # A dry run measures and prints; nothing it reaches may remove anything.
        settings = dataclasses.replace(settings, dry_run=True)
    state = load_state(settings.state_file)
    fast = await asyncio.to_thread(
        collect_fast, settings, runner=runner, roots=roots, state=state, stamp=now
    )
    cached = cached_slow(state)
    due = slow_due(settings, cached, fast.stamp, force=force_slow or dry_run)
    cache: dict[str, Any] = {"volumes": state.get("volumes")} if state.get("volumes") else {}

    answer: dict[str, Any] = {}

    async def send(body: box_health.BoxHealthPost, client: httpx.AsyncClient) -> int | None:
        nonlocal answer
        try:
            answer = await post_answer(client, body)
        except RelayError as exc:
            print(f"box-health: {exc}", file=out)
            return None
        return int(answer.get("samples", 0))

    client_cm: Any = None
    client: httpx.AsyncClient | None = None
    if not dry_run:
        if not omni_settings.configured:
            print(omni_settings.problem, file=out)
            return 1
        client_cm = omni.http_client(omni_settings, transport=transport)
        client = await client_cm.__aenter__()
    try:
        first = assemble(fast, cached)
        samples: int | None = 0
        if client is not None:
            samples = await send(first, client)
            if samples is None:
                return 1
            # Keep the CPU counters and the guard streak now: if the slow pass dies, the
            # next run must not compute busy % against counters from an hour ago.
            try:
                save_state(settings.state_file, new_state_of(fast, cached, cache, state))
            except OSError as exc:
                print(f"box-health: the state file could not be saved ({exc.strerror})", file=out)
        slow, slow_failed = cached, []
        queued = answer.get("cleanup") if isinstance(answer.get("cleanup"), dict) else None
        if queued and client is not None:
            result = await asyncio.to_thread(
                run_cleanup_request,
                queued,
                settings,
                runner,
                now=time.time(),
                home=home or str(Path.home()),
                tmp_root=(roots.tmp if roots else "/tmp"),
            )
            try:
                await post_result(client, result)
            except RelayError as exc:
                print(f"box-health: {exc}", file=out)
            # Rescan now so the next post carries the new numbers.
            cached, due = None, True
        if due:
            left = max(0.0, min(SLOW_DEADLINE_S, PASS_DEADLINE_S - (time.monotonic() - began)))
            slow, slow_failed = await asyncio.to_thread(
                run_slow,
                settings,
                runner,
                fast,
                cache,
                cached,
                roots=roots,
                home=home,
                deadline_s=left,
            )
        body = assemble(fast, slow, slow_failed)
        failed = list(body.failed)
        for part in failed:
            print(f"box-health: could not read {part}; the rest is posted", file=out)
        if dry_run:
            print(json.dumps(body.model_dump(mode="json"), indent=2), file=out)
            return 1 if failed else 0
        if client is not None and due:
            samples = await send(body, client)
            if samples is None:
                return 1
    finally:
        if client_cm is not None:
            await client_cm.__aexit__(None, None, None)
    try:
        save_state(settings.state_file, new_state_of(fast, slow, cache, state))
    except OSError as exc:
        print(
            f"box-health: posted, but the state file could not be saved ({exc.strerror})", file=out
        )
        return 1
    print(f"box-health: posted; the server holds {samples} samples", file=out)
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ae_omni_policies.box_health_relay",
        description="Post the box's heat, CPU, memory, storage and bloat to the OmniGent server.",
    )
    parser.add_argument("--dry-run", action="store_true", help="print, do not post or save")
    parser.add_argument("--slow", action="store_true", help="run the hourly scans now")
    parser.add_argument(
        "--state", default=None, help="the state file (default: $AE_BOX_HEALTH_STATE_FILE)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="box-health: %(message)s")
    settings = settings_from_env()
    if args.state:
        settings.state_file = Path(args.state)
    try:
        return asyncio.run(
            run(
                settings=settings,
                omni_settings=omni.Settings.from_env(),
                dry_run=args.dry_run,
                force_slow=args.slow,
            )
        )
    except Exception as exc:  # noqa: BLE001 (a traceback can quote a path or a name)
        print(
            f"box-health: stopped by {exc.__class__.__name__}; "
            "run it again with --dry-run and --slow to see which part",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
