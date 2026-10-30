"""The box's health for the Admin Center: heat, CPU, memory, storage and bloat (2026-10-28).

Why: on 2026-10-28 the box's ``/tmp`` (a 62 GB RAM tmpfs capped at 1,048,576 inodes)
ran out of inodes, and every Claude Code Bash call failed with ENOSPC for about 15
minutes. Nothing cleaned Claude Code's per-session temp folders, and the owner reaches the
box only through OmniGent, so the Admin Center has to show the box's health for both
box users (the owner and the second box user) without showing either the other's work.

The server cannot read the box, so ``box_health_relay.py`` runs there every five
minutes (a user timer), measures, and posts ``POST /v1/ae/box-health``. This module
holds what both sides share: the post's models, the settings table with its hints,
the store (the newest reading plus a day of five-minute samples) and :func:`view`,
which turns a reading into the colours and the Needs-you lines.

Privacy rule (tested in ``test_box_health_relay.py``): a post never carries a folder,
project, session, branch, worktree, repository or file name, for any user. A per-user
row holds a label from the box's users map, counts, sizes and ages; a Claude temp
folder is a fixed word (``tmp``, ``cache`` or ``container``), never a path. Names the
relay reads to count things stay in its memory.

Who sees it: box admins (``AE_BOX_ADMINS``, besides OmniGent admins) may read the
view; only an OmniGent admin may post, because the relay posts with the admin's
stored login. The view never names the box admins.

The extension bridge refuses an answer over 32,768 characters, so every list is
capped and the history is downsampled; ``test_box_health.py`` builds the worst case
and checks the view stays under 30,000.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator

from ae_omni_policies import box_health_scan

_logger = logging.getLogger("ae_omni_policies.box_health")

STATE_FILE_ENV = "AE_BOX_HEALTH_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-box-health.json"

HISTORY_MAX = 288
"""Five-minute samples kept: 24 hours."""
SAME_SAMPLE_S = 4 * 60
"""A post this soon after the last sample replaces it, so manual runs do not flood it."""
VIEW_POINTS = 96
BUCKET_S = 900
"""The view sends 96 points of 15 minutes, the highest value in each."""

GIB = 1024**3

Tone = Literal["green", "amber", "red", "grey"]
State = Literal["ok", "missing", "stale"]

LABEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}$"
"""A user's label from the box's users map: it is shown, so it is a plain name."""
UNIT_PATTERN = r"^[A-Za-z0-9@._:-]{1,48}$"
MAX_STRING = 48
"""No posted string is longer than this (2026-10-28, review of PR 389): a long or
non-ASCII name could push the view past the bridge's size cap, and a name is more than
the card needs."""
PART_PATTERN = r"^[A-Za-z0-9:_. -]{1,48}$"
FIXED_PARTS = frozenset(
    {
        "users",
        "sensors",
        "cpu",
        "memory",
        "mounts",
        "units",
        "tmp",
        "tmp scan cut short",
        "live sessions",
        "worktrees",
        "docker",
        "journal",
        "folders",
        "slow scans",
        "slow scans cut short",
        "cleanup",
    }
)
"""The failed parts the relay can name besides the two per-user forms below."""
_PART_LABEL = r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,23}"
PART_FORMS = re.compile(
    rf"(?:user {_PART_LABEL}|container {_PART_LABEL}(?: scan| volume| cleanup)?)"
)
LITTER_GROUP_NAMES = frozenset({*(name for name, _ in box_health_scan.LITTER_GROUPS), "other"})
"""The /tmp pattern groups the relay can post (box_health_scan.LITTER_GROUPS, and "other")."""

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(value: str) -> str:
    if _CONTROL.search(value):
        raise ValueError("control characters are not allowed")
    return value


Text = Annotated[str, Field(max_length=200), AfterValidator(_clean)]
Label = Annotated[str, Field(pattern=LABEL_PATTERN)]


# The mounts and unit names the card may name; anything else is counted without its name
# (2026-10-28, review of PR 389: a /mnt/<client> mount or a <repo>.service unit must not
# reach the other box admin).
SAFE_MOUNTS = frozenset(
    {"/", "/tmp", "/boot", "/boot/efi", "/home", "/var", "/secure", "/run", "/dev/shm"}
)
OTHER_MOUNT = re.compile(r"other mount [0-9]{1,2}")
SAFE_UNIT = re.compile(
    r"(?:ae-[A-Za-z0-9._-]+|systemd-[A-Za-z0-9._-]+|docker|containerd)@?"
    r"\.(?:service|timer|socket|mount|path|target|scope|slice)"
)
_INSTANCE = re.compile(r"@[^.]*(?=\.)")


def safe_unit(name: str) -> str | None:
    """*name* as the card may show it, or ``None`` when it is not a unit it may name.

    A template instance is dropped (``ae-x@member.service`` is ``ae-x@.service``), since
    an instance can be anything. Allowed: ``ae-*`` units, systemd's own and docker's.
    """
    shown = _INSTANCE.sub("@", name)
    if len(shown) > MAX_STRING or not SAFE_UNIT.fullmatch(shown):
        return None
    return shown


def _mount_path(value: str) -> str:
    if value in SAFE_MOUNTS or OTHER_MOUNT.fullmatch(value):
        return value
    raise ValueError("a mount is named only from the safe list, or posted as 'other mount N'")


def _safe_unit(value: str) -> str:
    if safe_unit(value) != value:
        raise ValueError("a unit is named only when it is an ae-, systemd or docker unit")
    return value


MountPath = Annotated[str, Field(max_length=MAX_STRING), AfterValidator(_mount_path)]
Unit = Annotated[str, Field(pattern=UNIT_PATTERN), AfterValidator(_safe_unit)]


def _known_part(value: str) -> str:
    if value not in FIXED_PARTS and not PART_FORMS.fullmatch(value):
        raise ValueError("a failed part is named only from the relay's fixed list")
    return value


def _known_group(value: str) -> str:
    if value not in LITTER_GROUP_NAMES:
        raise ValueError("a /tmp group is named only from the relay's fixed list")
    return value


Part = Annotated[
    str, Field(pattern=PART_PATTERN, max_length=MAX_STRING), AfterValidator(_known_part)
]

Count = Annotated[int, Field(ge=0, le=10**15)]


# ── the post ────────────────────────────────────────────────────────────────


class Sensor(BaseModel):
    chip: Annotated[str, Field(max_length=32, pattern=r"^[A-Za-z0-9_.:-]+$")]
    label: Annotated[str, Field(max_length=32, pattern=r"^[A-Za-z0-9_. :-]+$")]
    c: Annotated[float, Field(ge=-100, le=300)]


class Cpu(BaseModel):
    threads: Annotated[int, Field(ge=1, le=4096)]
    load1: Annotated[float, Field(ge=0)]
    load5: Annotated[float, Field(ge=0)]
    load15: Annotated[float, Field(ge=0)]
    busy_pct: Annotated[float, Field(ge=0, le=100)] | None = None
    psi_some_avg10: Annotated[float, Field(ge=0, le=100)] | None = None
    psi_some_avg60: Annotated[float, Field(ge=0, le=100)] | None = None
    psi_some_avg300: Annotated[float, Field(ge=0, le=100)] | None = None


class Memory(BaseModel):
    total_b: Count
    available_b: Count
    shmem_b: Count = 0
    swap_total_b: Count = 0
    swap_free_b: Count = 0
    psi_some_avg60: Annotated[float, Field(ge=0, le=100)] | None = None
    psi_full_avg60: Annotated[float, Field(ge=0, le=100)] | None = None


class Mount(BaseModel):
    path: MountPath
    fstype: Annotated[str, Field(max_length=16, pattern=r"^[a-z0-9_.-]+$")]
    size_b: Count
    used_b: Count
    avail_b: Count
    inodes: Count | None = None
    """``None`` for a filesystem without inodes (vfat)."""
    inodes_used: Count | None = None


ClaudeWhere = Literal["tmp", "cache", "container"]


class ClaudeRoot(BaseModel):
    """One Claude Code temp folder, named by a fixed word, never by its path."""

    where: ClaudeWhere
    exists: bool = True
    readable: bool = True
    projects: Count = 0
    sessions: Count = 0
    stale_sessions: Count = 0
    files: Count = 0
    """Every entry below the root, an inode each."""
    bytes: Count = 0
    stale_files: Count = 0
    stale_bytes: Count = 0
    oldest_days: Annotated[float, Field(ge=0)] | None = None
    top_age_days: Annotated[float, Field(ge=0)] | None = None
    """For a folder the relay cannot read: how long since the folder itself changed."""
    partial: bool = False
    had_unreadable: bool = False
    """``find`` skipped a folder it could not read; the rest was counted in full."""


class PytestTemp(BaseModel):
    exists: bool = True
    readable: bool = True
    entries: Count = 0
    stale: Count = 0
    files: Count = 0
    bytes: Count = 0
    stale_files: Count = 0
    stale_bytes: Count = 0


class LitterGroup(BaseModel):
    """Top-level ``/tmp`` entries of one owner that share a name pattern."""

    group: Annotated[str, Field(max_length=40), AfterValidator(_known_group)]
    entries: Count
    stale: Count = 0
    inodes: Count | None = None
    bytes: Count | None = None
    unreadable: Count = 0
    partial: bool = False


class Worktrees(BaseModel):
    count: Count = 0
    bytes: Count | None = None
    older_14d: Count = 0
    with_changes: Count | None = None
    partial: bool = False


class UserHealth(BaseModel):
    """One box user, or one user's container, or the system's own leftovers."""

    label: Label
    kind: Literal["box", "container"] = "box"
    uid: Annotated[int, Field(ge=0, le=10**9)] | None = None
    running: bool | None = None
    writable_b: Count | None = None
    """A container's writable layer: files there live on the root disk."""
    home_b: Count | None = None
    claude: list[ClaudeRoot] = Field(default_factory=list, max_length=3)
    pytest: PytestTemp | None = None
    litter: list[LitterGroup] = Field(default_factory=list, max_length=10)
    worktrees: Worktrees | None = None


class Docker(BaseModel):
    images: Count = 0
    images_b: Count = 0
    images_reclaim_b: Count = 0
    volumes: Count = 0
    volumes_b: Count = 0
    build_cache_b: Count = 0
    build_reclaim_b: Count = 0


class DirSize(BaseModel):
    label: Label
    """The label from ``AE_BOX_HEALTH_DIRS`` (``label=path``); the path is never posted."""
    bytes: Count
    files: Count = 0
    partial: bool = False


CleanupAction = Literal["claude_scratch", "tmp_litter", "review_checkouts", "docker_prune"]

SKIP_REASONS = (
    "recent",
    "unproven",
    "symlink",
    "foreign",
    "git",
    "changes",
    "running",
    "locked",
    "changed",
    "alive",
    "mount",
    "over_confirmed",
)
SkipReason = Literal[
    "recent",
    "unproven",
    "symlink",
    "foreign",
    "git",
    "changes",
    "running",
    "locked",
    "changed",
    "alive",
    "mount",
    "over_confirmed",
]
ERROR_CODE_PATTERN = r"^[A-Z0-9_]{2,24}$"
ErrorCode = Annotated[str, Field(pattern=ERROR_CODE_PATTERN)]


class OfferGroup(BaseModel):
    """A name-pattern group of ``/tmp`` leftovers a cleanup would remove (a fixed label)."""

    group: Annotated[str, Field(max_length=40), AfterValidator(_known_group)]
    count: Count


class OfferItem(BaseModel):
    """What one cleanup action would remove now, for one owner. Counts only."""

    action: CleanupAction
    owner: Label
    """A label from the users map, or ``all`` for Docker, which is box-wide."""
    where: Literal["box", "container", "all"] = "box"
    count: Count
    files: Count = 0
    bytes: Count = 0
    groups: list[OfferGroup] = Field(default_factory=list, max_length=12)


class AutoAged(BaseModel):
    """What the relay aged on its own in one container during the last hourly pass."""

    owner: Label
    count: Count
    bytes: Count = 0


class Cleanup(BaseModel):
    """The box's cleanup offer: what each button would do, from the box's own scan."""

    enabled: bool = True
    """``AE_BOX_HEALTH_CLEANUP`` on the box; off hides every button and stops the aging."""
    actions: list[OfferItem] = Field(default_factory=list, max_length=12)
    auto_aged: list[AutoAged] = Field(default_factory=list, max_length=5)


class SlowScan(BaseModel):
    """The hourly scans, cached on the box so a fast pass never wipes them."""

    read_at: datetime
    stale_days: Annotated[float, Field(gt=0, le=3650)] = 2
    slow_minutes: Annotated[int, Field(ge=1, le=10080)] = 60
    users: list[UserHealth] = Field(default_factory=list, max_length=5)
    docker: Docker | None = None
    dirs: list[DirSize] = Field(default_factory=list, max_length=6)
    journal_b: Count | None = None
    cleanup: Cleanup | None = None
    settings: dict[str, str] = Field(default_factory=dict, max_length=12)
    """The box's own numeric settings, by name, so the dialog shows what is in force."""

    @field_validator("read_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _as_utc(value)

    @field_validator("settings")
    @classmethod
    def _numbers_only(cls, value: dict[str, str]) -> dict[str, str]:
        known = {s.name for s in SETTINGS if s.side == "host"}
        return {k: v[:12] for k, v in value.items() if k in known and re.fullmatch(r"[0-9.]+", v)}


class Guard(BaseModel):
    """``ae-claude-tmp-guard``: locks every live Claude session's temp folder so the
    two-day rule skips running sessions. Counts only."""

    running: bool = False
    """The status file is under two minutes old and its process is alive."""
    locked: Count = 0
    failed: Count = 0
    age_s: Count | None = None
    claude_processes: Count = 0
    claude_unmatched: Count = 0
    """Claude sessions the guard cannot tie to a folder, so they are unprotected. A new
    process counts for one pass, hence the two fields below."""
    claude_remote: Count = 0
    """Remote Control processes; shown only in the dialog."""
    unmatched_readings: Count = 0
    """How many relay readings in a row saw ``claude_unmatched`` above zero."""
    unmatched_minutes: Count = 0
    """How long, in minutes, it has been above zero without a clear reading."""


class BoxHealthPost(BaseModel):
    """The relay's body."""

    schema_version: Literal[1] = 1
    host: Annotated[str, Field(max_length=MAX_STRING, pattern=r"^[A-Za-z0-9._-]+$")] = "box"
    read_at: datetime
    uptime_s: Annotated[int, Field(ge=0)] | None = None
    failed: list[Part] = Field(default_factory=list, max_length=20)
    """Parts the relay could not read (``sensors``, ``docker``, ``units``, ...)."""
    sensors: list[Sensor] = Field(default_factory=list, max_length=24)
    gpu_power_w: Annotated[float, Field(ge=0, le=5000)] | None = None
    cpu: Cpu | None = None
    memory: Memory | None = None
    mounts: list[Mount] = Field(default_factory=list, max_length=20)
    failed_units: list[Unit] = Field(default_factory=list, max_length=10)
    failed_user_units: list[Unit] = Field(default_factory=list, max_length=10)
    failed_units_other: Count = 0
    """System units that failed and may not be named (everything but ae-, systemd, docker)."""
    failed_user_units_other: Count = 0
    guard: Guard | None = None
    slow: SlowScan | None = None

    @field_validator("read_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ── the settings ────────────────────────────────────────────────────────────

HINT_MAX = 140


@dataclass(frozen=True)
class Setting:
    name: str
    default: str
    hint: str
    side: Literal["server", "host"]


SETTINGS: tuple[Setting, ...] = (
    Setting(
        "AE_BOX_HEALTH_FILE",
        "/data/ae-box-health.json",
        "Where the server keeps the box's last reading and a day of history. Empty keeps it in "
        "memory only.",
        "server",
    ),
    Setting(
        "AE_BOX_ADMINS",
        "",
        "Comma-separated sign-in logins, besides OmniGent admins, who may see the box health "
        "monitor and later run its cleanups.",
        "server",
    ),
    Setting(
        "AE_BOX_USER_LABELS",
        "",
        "Comma-separated user labels the box may post, like owner,member. Empty accepts any "
        "plain label. A reading with another label is refused.",
        "server",
    ),
    Setting(
        "AE_BOX_HEALTH_CLEANUP_FILE",
        "/data/ae-box-health-cleanup.json",
        "Where the server keeps the one pending or last cleanup request. Empty keeps it in "
        "memory only.",
        "server",
    ),
    Setting(
        "AE_BOX_HEALTH_STALE_MINUTES",
        "15",
        "A reading older than this many minutes turns the card amber, since the box timer may "
        "have stopped.",
        "server",
    ),
    Setting(
        "AE_BOX_HEALTH_CPU_HOT_C",
        "90",
        "CPU temperature in C that counts as hot. Short boost bursts are normal and do not warn.",
        "server",
    ),
    Setting(
        "AE_BOX_HEALTH_CPU_SUSTAINED_MINUTES",
        "15",
        "How many minutes every CPU reading must stay hot before the card warns.",
        "server",
    ),
    Setting(
        "AE_BOX_HEALTH_NVME_WARN_C",
        "60",
        "SSD temperature in C that turns the card amber. The drive slows itself near 70.",
        "server",
    ),
    Setting(
        "AE_BOX_HEALTH_DISK_WARN_PCT",
        "85",
        "A mount past this percent of space or inodes turns amber and gets a Needs you line.",
        "server",
    ),
    Setting(
        "AE_BOX_HEALTH_DISK_CRIT_PCT",
        "95",
        "A mount past this percent of space or inodes turns red.",
        "server",
    ),
    Setting(
        "AE_BOX_HEALTH_MEM_WARN_PCT",
        "90",
        "Memory in use, tmpfs included, past this percent turns the card amber.",
        "server",
    ),
    Setting(
        "AE_BOX_HEALTH_LITTER_WARN",
        "5000",
        "Top-level /tmp entries older than the stale age, past this count for one user, get a "
        "Needs you line.",
        "server",
    ),
    Setting(
        "AE_BOX_HEALTH_DOCKER_RECLAIM_WARN_GB",
        "20",
        "Docker space that could be freed, in GB, past which the card asks for a cleanup.",
        "server",
    ),
    Setting(
        "AE_BOX_HEALTH_CONTAINER_LAYER_WARN_GB",
        "5",
        "A container's writable layer past this many GB turns the card amber. Files there live "
        "on the root disk.",
        "server",
    ),
    Setting(
        "AE_BOX_USERS",
        "",
        "Maps box user ids and host containers to card names, like 1000=owner,1001=member. "
        "Others show their login.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_CONTAINER_USER",
        "agent",
        "The user the relay runs its fixed read-only checks as inside each mapped container.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_CLAUDE_ROOTS",
        "/tmp/claude-{uid},~/.cache/claude-tmp/claude-{uid}",
        "Comma-separated Claude Code temp folders to check for old session folders. {uid} is "
        "each mapped user's id.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_STALE_DAYS",
        "2",
        "A session folder or /tmp entry untouched this many days counts as stale.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_SLOW_MINUTES",
        "60",
        "How often the slow scans run: folder sizes, Docker and worktrees. Fast checks run "
        "every pass.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_WALK_MAX_ENTRIES",
        "500000",
        "The most files and folders one scan visits per folder. Past it the count is marked "
        "partial.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_WALK_MAX_SECONDS",
        "30",
        "The most seconds one scan spends per folder. Past it the count is marked partial.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_DIRS",
        "cache=~/.cache,npm=~/.npm",
        "Comma-separated label=path folders whose size the slow scan reports. "
        "Only the label is posted; Claude temp folders are left out.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_WORKTREES",
        "~/worktrees",
        "Folder holding git worktrees. They are counted with size and age, never removed, and "
        "never named.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_SKIP_MOUNTS",
        "/run/user,/run/credentials,/run/lock,/snap,/var/snap,/var/lib/docker",
        "Comma-separated mount path prefixes to leave out of Storage.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_GUARD_FILE",
        "/run/user/{uid}/ae-claude-tmp-guard.json",
        "Status file of the Claude temp guard, which protects live sessions' temp folders from "
        "the cleanup. Counts only.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_CLEANUP",
        "on",
        "on lets a box admin's Clean up button remove old temp files here, and ages mapped "
        "containers hourly. off hides the buttons and stops both.",
        "host",
    ),
    Setting(
        "AE_BOX_HEALTH_STATE_FILE",
        "~/.local/state/ae-omnigent/box-health.json",
        "Where the box keeps its slow scans and CPU counters between passes.",
        "host",
    ),
)

SERVER_SETTINGS = tuple(s for s in SETTINGS if s.side == "server")
HOST_SETTINGS = tuple(s for s in SETTINGS if s.side == "host")


@dataclass(frozen=True)
class Config:
    stale_minutes: int
    cpu_hot_c: float
    cpu_sustained_minutes: int
    nvme_warn_c: float
    disk_warn_pct: float
    disk_crit_pct: float
    mem_warn_pct: float
    litter_warn: int
    docker_reclaim_warn_gb: float
    container_layer_warn_gb: float


def _number(env: Mapping[str, str], name: str, default: float, low: float, high: float) -> float:
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


def config(environ: Mapping[str, str] | None = None) -> Config:
    """The thresholds, read at request time so a changed env file needs no code change."""
    env = os.environ if environ is None else environ
    return Config(
        stale_minutes=int(_number(env, "AE_BOX_HEALTH_STALE_MINUTES", 15, 1, 1440)),
        cpu_hot_c=_number(env, "AE_BOX_HEALTH_CPU_HOT_C", 90, 40, 120),
        cpu_sustained_minutes=int(_number(env, "AE_BOX_HEALTH_CPU_SUSTAINED_MINUTES", 15, 5, 240)),
        nvme_warn_c=_number(env, "AE_BOX_HEALTH_NVME_WARN_C", 60, 30, 120),
        disk_warn_pct=_number(env, "AE_BOX_HEALTH_DISK_WARN_PCT", 85, 1, 100),
        disk_crit_pct=_number(env, "AE_BOX_HEALTH_DISK_CRIT_PCT", 95, 1, 100),
        mem_warn_pct=_number(env, "AE_BOX_HEALTH_MEM_WARN_PCT", 90, 1, 100),
        litter_warn=int(_number(env, "AE_BOX_HEALTH_LITTER_WARN", 5000, 1, 10**9)),
        docker_reclaim_warn_gb=_number(
            env, "AE_BOX_HEALTH_DOCKER_RECLAIM_WARN_GB", 20, 0.1, 10**6
        ),
        container_layer_warn_gb=_number(
            env, "AE_BOX_HEALTH_CONTAINER_LAYER_WARN_GB", 5, 0.1, 10**6
        ),
    )


def admins(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    """The sign-in logins in ``AE_BOX_ADMINS``, lowercased."""
    env = os.environ if environ is None else environ
    raw = env.get("AE_BOX_ADMINS", "")
    return frozenset(part.strip().lower() for part in raw.split(",") if part.strip())


def is_box_admin(user: str | None, environ: Mapping[str, str] | None = None) -> bool:
    """Whether *user* is named in ``AE_BOX_ADMINS`` (an OmniGent admin is checked elsewhere)."""
    return user is not None and user.lower() in admins(environ)


# ── the store ───────────────────────────────────────────────────────────────


class Sample(BaseModel):
    t: float
    cpu_c: float | None = None
    nvme_c: float | None = None
    load1: float | None = None
    mem_pct: float | None = None
    disk_pct: float | None = None


class Stored(BaseModel):
    reading: BoxHealthPost
    received_at: datetime
    history: list[Sample] = Field(default_factory=list)


def cpu_temp(post: BoxHealthPost) -> float | None:
    """The CPU's temperature: k10temp ``Tctl``, else the first k10temp input."""
    chips = [s for s in post.sensors if s.chip.startswith("k10temp")]
    for sensor in chips:
        if sensor.label == "Tctl":
            return sensor.c
    return chips[0].c if chips else None


def nvme_temp(post: BoxHealthPost) -> float | None:
    """The hottest NVMe drive's ``Composite`` temperature."""
    values = [s.c for s in post.sensors if s.chip.startswith("nvme") and s.label == "Composite"]
    return max(values) if values else None


def _pct(used: float, total: float) -> float | None:
    return 100.0 * used / total if total > 0 else None


def memory_pct(post: BoxHealthPost) -> float | None:
    mem = post.memory
    return _pct(mem.total_b - mem.available_b, mem.total_b) if mem else None


def mount_pcts(mount: Mount) -> tuple[float | None, float | None]:
    """Space used and inodes used, in percent, the way ``df`` counts them."""
    space = _pct(mount.used_b, mount.used_b + mount.avail_b)
    inodes = _pct(mount.inodes_used or 0, mount.inodes) if mount.inodes else None
    return space, inodes


def worst_mount_pct(post: BoxHealthPost) -> float | None:
    values = [v for m in post.mounts for v in mount_pcts(m) if v is not None]
    return max(values) if values else None


def sample_of(post: BoxHealthPost) -> Sample:
    return Sample(
        t=post.read_at.timestamp(),
        cpu_c=cpu_temp(post),
        nvme_c=nvme_temp(post),
        load1=post.cpu.load1 if post.cpu else None,
        mem_pct=memory_pct(post),
        disk_pct=worst_mount_pct(post),
    )


class BoxHealthStore:
    """The newest reading and a day of samples, in a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self._memory: Stored | None = None

    def read(self) -> Stored | None:
        """The stored reading; ``None`` when there is none or the file cannot be read."""
        with self._lock:
            if self._path is None:
                return self._memory
            try:
                return Stored.model_validate_json(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

    def put(self, post: BoxHealthPost, *, now: datetime | None = None) -> int:
        """Keep *post* as the newest reading; how many samples the history holds.

        :raises OSError: When the file cannot be written.
        """
        at = now or datetime.now(UTC)
        sample = sample_of(post)
        with self._lock:
            before = self.read()
            history = list(before.history) if before else []
            if history and sample.t - history[-1].t < SAME_SAMPLE_S:
                history[-1] = sample
            else:
                history.append(sample)
            history = history[-HISTORY_MAX:]
            stored = Stored(reading=post, received_at=at, history=history)
            if self._path is None:
                self._memory = stored
                return len(history)
            tmp: str | None = None
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".ae-box-health-", dir=str(self._path.parent))
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(stored.model_dump_json())
                os.replace(tmp, self._path)
            except OSError:
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                raise
        return len(history)


def _default_path() -> Path | None:
    raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if STATE_FILE_ENV in os.environ:
        return None
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: BoxHealthStore | None = None
_store_lock = threading.Lock()


def store() -> BoxHealthStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = BoxHealthStore(_default_path())
        return _store


def reset_store(replacement: BoxHealthStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


# ── the view ────────────────────────────────────────────────────────────────

SETUP = "systemctl --user enable --now ae-omnigent-box-health.timer"

_TONE_RANK = {"grey": 0, "green": 1, "amber": 2, "red": 3}

# The area each relay part belongs to when it fails.
_PART_AREA = {
    "sensors": "heat",
    "cpu": "cpu",
    "memory": "memory",
    "mounts": "storage",
}


class Need(BaseModel):
    key: str
    area: str
    tone: Literal["amber", "red"]
    text: str
    detail: str = ""
    owner: str | None = None
    action: str | None = None
    """The cleanup action that clears this line, when the box offers one (PR 2)."""


class Area(BaseModel):
    id: str
    label: str
    tone: Tone
    value: str
    """Fits a 200 px card: 24 characters or fewer."""
    title: str


class SettingRow(BaseModel):
    name: str
    side: str
    value: str
    hint: str


class History(BaseModel):
    step_s: int = BUCKET_S
    end: datetime | None = None
    cpu_c: list[float | None] = Field(default_factory=list)
    nvme_c: list[float | None] = Field(default_factory=list)
    load1: list[float | None] = Field(default_factory=list)
    mem_pct: list[float | None] = Field(default_factory=list)
    disk_pct: list[float | None] = Field(default_factory=list)


class BoxHealthView(BaseModel):
    state: State
    tone: Tone
    read_at: datetime | None = None
    age_s: int | None = None
    slow_read_at: datetime | None = None
    slow_age_s: int | None = None
    host: str | None = None
    needs: list[Need] = Field(default_factory=list)
    areas: list[Area] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)
    history: History = Field(default_factory=History)
    settings: list[SettingRow] = Field(default_factory=list)
    setup: str | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)
    """The cleanups the box offers now, each with its confirm text (see ``box_health_cleanup``)."""
    cleanup: dict[str, Any] | None = None
    """The one pending or recent cleanup request, with its state."""


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _gib(value: float) -> str:
    return f"{value / GIB:.0f}" if value >= 10 * GIB else f"{value / GIB:.1f}"


def size_words(value: float | None) -> str:
    """Bytes as short words: ``482 MB``, ``13 GB``."""
    if value is None:
        return "n/a"
    for unit, factor in (("TB", 1024**4), ("GB", GIB), ("MB", 1024**2), ("KB", 1024)):
        if value >= factor:
            amount = value / factor
            return f"{amount:.0f} {unit}" if amount >= 10 else f"{amount:.1f} {unit}"
    return f"{int(value)} B"


def count_words(value: float) -> str:
    """A count as short words: ``482k``, ``1.05M``, ``26,132`` below ten thousand."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 10_000:
        return f"{value / 1000:.0f}k"
    return f"{int(value):,}"


def ago_words(seconds: float) -> str:
    if seconds < 90:
        return "just now" if seconds < 30 else f"{int(seconds)} s ago"
    if seconds < 5400:
        return f"{round(seconds / 60)} m ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} d ago"


def _worse(a: Tone, b: Tone) -> Tone:
    return a if _TONE_RANK[a] >= _TONE_RANK[b] else b


def _band(value: float | None, amber: float, red: float) -> Tone:
    if value is None:
        return "grey"
    if value >= red:
        return "red"
    if value >= amber:
        return "amber"
    return "green"


def _clip(text: str, limit: int = 24) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _possessive(label: str) -> str:
    return f"{label}'s"


def _where_words(where: str) -> str:
    return {"tmp": "/tmp", "cache": "cache", "container": "container"}.get(where, where)


def _window(history: list[Sample], now_t: float, minutes: int) -> list[Sample]:
    return [s for s in history if s.t >= now_t - minutes * 60]


def _downsample(history: list[Sample]) -> History:
    if not history:
        return History()
    end = history[-1].t
    out = History(end=datetime.fromtimestamp(end, UTC))
    names = ("cpu_c", "nvme_c", "load1", "mem_pct", "disk_pct")
    series: dict[str, list[float | None]] = {n: [None] * VIEW_POINTS for n in names}
    start = end - VIEW_POINTS * BUCKET_S
    for sample in history:
        if sample.t <= start:
            continue
        index = min(VIEW_POINTS - 1, int((sample.t - start - 1e-6) // BUCKET_S))
        for name in names:
            value = getattr(sample, name)
            if value is None:
                continue
            current = series[name][index]
            series[name][index] = value if current is None else max(current, value)
    for name in names:
        setattr(out, name, [_round(v) for v in series[name]])
    return out


def _sorted_needs(needs: list[tuple[float, Need]]) -> list[Need]:
    needs.sort(key=lambda item: (item[1].tone != "red", -item[0], item[1].key))
    return [need for _, need in needs]


def view(stored: Stored | None, now: datetime, cfg: Config | None = None) -> BoxHealthView:
    """The card's answer: colours, the Needs-you lines, details and a day of history."""
    cfg = cfg or config()
    settings = _setting_rows(stored)
    if stored is None:
        return BoxHealthView(
            state="missing",
            tone="grey",
            areas=[
                Area(id=i, label=label, tone="grey", value="no reading", title="No reading yet")
                for i, label in _AREAS
            ],
            settings=settings,
            setup=SETUP,
        )
    post = stored.reading
    # The smaller of the box clock's age and the server's own (a skewed box clock must
    # neither hide a dead timer nor flag a live one); a reading dated in the future is
    # judged by the server's clock alone.
    served = max(0, int((now - stored.received_at).total_seconds()))
    boxed = int((now - post.read_at).total_seconds())
    age_s = served if boxed < -60 else min(served, max(0, boxed))
    stale = age_s > cfg.stale_minutes * 60
    needs: list[tuple[float, Need]] = []

    def add(
        score: float,
        key: str,
        area: str,
        tone: Literal["amber", "red"],
        text: str,
        detail: str = "",
        owner: str | None = None,
    ) -> None:
        needs.append(
            (score, Need(key=key, area=area, tone=tone, text=text, detail=detail, owner=owner))
        )

    if stale:
        add(
            1e6,
            "stale",
            "system",
            "amber",
            f"Last reading {ago_words(age_s)}; the box timer may have stopped",
            "The values below are from the last reading the box posted.",
        )

    areas: dict[str, Area] = {}
    details: dict[str, Any] = {}

    # Heat
    cpu_c = cpu_temp(post)
    nvme_c = nvme_temp(post)
    heat_tone: Tone = "green"
    heat_note = ""
    if cpu_c is not None:
        window = _window(stored.history, post.read_at.timestamp(), cfg.cpu_sustained_minutes)
        temps = [s.cpu_c for s in window if s.cpu_c is not None]
        sustained = len(temps) >= 3 and all(t >= cfg.cpu_hot_c for t in temps)
        sustained_red = len(temps) >= 3 and all(t >= cfg.cpu_hot_c + 5 for t in temps)
        if cpu_c >= 98 or sustained_red:
            heat_tone = "red"
            add(
                cpu_c / cfg.cpu_hot_c,
                "heat:cpu",
                "heat",
                "red",
                f"CPU {cpu_c:.0f}C",
                f"CPU temperature {cpu_c:.0f}C"
                + (", hot for the whole window" if sustained_red else "")
                + "; the chip throttles near 100C.",
            )
        elif sustained:
            heat_tone = "amber"
            add(
                cpu_c / cfg.cpu_hot_c,
                "heat:cpu",
                "heat",
                "amber",
                f"CPU {cpu_c:.0f}C for {cfg.cpu_sustained_minutes} m",
                f"Every reading in the last {cfg.cpu_sustained_minutes} minutes was "
                f"{cfg.cpu_hot_c:.0f}C or more.",
            )
        elif cpu_c >= cfg.cpu_hot_c:
            heat_note = "; one hot reading, not sustained"
    if nvme_c is not None:
        nvme_tone = _band(nvme_c, cfg.nvme_warn_c, cfg.nvme_warn_c + 8)
        if nvme_tone in ("amber", "red"):
            add(
                nvme_c / cfg.nvme_warn_c,
                "heat:nvme",
                "heat",
                nvme_tone,  # type: ignore[arg-type]
                f"SSD {nvme_c:.0f}C",
                f"The SSD is at {nvme_c:.0f}C; it slows itself near 70C.",
            )
        heat_tone = _worse(heat_tone, nvme_tone)
    chassis = next((s.c for s in post.sensors if s.chip.startswith("acpitz")), None)
    gpu = next(
        (s.c for s in post.sensors if s.chip.startswith("amdgpu") and s.label == "edge"), None
    )
    if cpu_c is None and nvme_c is None:
        heat_tone = "grey"
    heat_value = (
        " · ".join(
            part
            for part in (
                f"CPU {cpu_c:.0f}C" if cpu_c is not None else "",
                f"SSD {nvme_c:.0f}C" if nvme_c is not None else "",
            )
            if part
        )
        or "No sensors read"
    )
    heat_title = (
        ", ".join(
            part
            for part in (
                f"CPU {cpu_c:.0f}C" if cpu_c is not None else "",
                f"chassis {chassis:.0f}C" if chassis is not None else "",
                f"SSD {nvme_c:.0f}C" if nvme_c is not None else "",
                f"GPU {gpu:.0f}C" + (f" at {post.gpu_power_w:.0f} W" if post.gpu_power_w else "")
                if gpu is not None
                else "",
            )
            if part
        )
        + heat_note
    )
    areas["heat"] = Area(
        id="heat",
        label="Heat",
        tone=heat_tone,
        value=_clip(heat_value),
        title=heat_title or heat_value,
    )
    details["heat"] = {
        "cpu_c": _round(cpu_c),
        "nvme_c": _round(nvme_c),
        "chassis_c": _round(chassis),
        "gpu_c": _round(gpu),
        "gpu_power_w": _round(post.gpu_power_w),
        "sensors": [
            {"chip": s.chip, "label": s.label, "c": _round(s.c)} for s in post.sensors[:16]
        ],
    }

    # CPU
    cpu = post.cpu
    if cpu is None:
        areas["cpu"] = Area(
            id="cpu", label="CPU", tone="grey", value="not read", title="No CPU reading"
        )
    else:
        per_thread = cpu.load15 / cpu.threads
        cpu_tone = _band(per_thread, 1.0, 2.0)
        if cpu_tone in ("amber", "red"):
            add(
                per_thread / 1.0,
                "cpu:load",
                "cpu",
                cpu_tone,  # type: ignore[arg-type]
                f"Load {cpu.load15:.0f} of {cpu.threads}",
                f"The 15-minute load is {cpu.load15:.1f} on {cpu.threads} threads.",
            )
        areas["cpu"] = Area(
            id="cpu",
            label="CPU",
            tone=cpu_tone,
            value=f"Load {cpu.load1:.0f} of {cpu.threads}",
            title=(
                f"Load {cpu.load1:.1f}, {cpu.load5:.1f}, {cpu.load15:.1f} on {cpu.threads} threads"
                + (f"; busy {cpu.busy_pct:.0f}%" if cpu.busy_pct is not None else "")
                + (
                    f"; pressure {cpu.psi_some_avg60:.0f}%"
                    if cpu.psi_some_avg60 is not None
                    else ""
                )
            ),
        )
        details["cpu"] = cpu.model_dump()

    # Memory
    mem = post.memory
    mem_used_pct = memory_pct(post)
    if mem is None or mem_used_pct is None:
        areas["memory"] = Area(
            id="memory", label="Memory", tone="grey", value="not read", title="No memory reading"
        )
    else:
        mem_tone = _band(mem_used_pct, cfg.mem_warn_pct, max(97.0, cfg.mem_warn_pct))
        used = mem.total_b - mem.available_b
        if mem_tone in ("amber", "red"):
            add(
                mem_used_pct / cfg.mem_warn_pct,
                "memory",
                "memory",
                mem_tone,  # type: ignore[arg-type]
                f"Memory {mem_used_pct:.0f}%",
                f"{size_words(used)} of {size_words(mem.total_b)} in use, tmpfs included.",
            )
        tmpfs = [
            {"path": m.path, "used_b": m.used_b}
            for m in post.mounts
            if m.fstype == "tmpfs" and m.used_b > 0
        ]
        areas["memory"] = Area(
            id="memory",
            label="Memory",
            tone=mem_tone,
            value=f"{_gib(used)} of {_gib(mem.total_b)} GiB",
            title=(
                f"{size_words(used)} of {size_words(mem.total_b)} in use ({mem_used_pct:.0f}%); "
                f"tmpfs {size_words(sum(t['used_b'] for t in tmpfs))}"
            ),
        )
        details["memory"] = {
            **mem.model_dump(),
            "used_b": used,
            "used_pct": _round(mem_used_pct),
            "tmpfs": tmpfs[:6],
        }

    # Storage
    mount_rows: list[dict[str, Any]] = []
    storage_tone: Tone = "green" if post.mounts else "grey"
    worst: tuple[float, str] | None = None
    for mount in post.mounts:
        space, inodes = mount_pcts(mount)
        mount_rows.append(
            {
                "path": mount.path,
                "fstype": mount.fstype,
                "size_b": mount.size_b,
                "used_b": mount.used_b,
                "space_pct": _round(space),
                "inodes": mount.inodes,
                "inodes_used": mount.inodes_used,
                "inodes_pct": _round(inodes),
            }
        )
        for kind, pct in (("space", space), ("inodes", inodes)):
            if pct is None:
                continue
            if worst is None or pct > worst[0]:
                worst = (
                    pct,
                    f"{mount.path} {pct:.0f}% {'inodes' if kind == 'inodes' else ''}".strip(),
                )
            tone = _band(pct, cfg.disk_warn_pct, cfg.disk_crit_pct)
            storage_tone = _worse(storage_tone, tone)
            if tone in ("amber", "red"):
                detail = (
                    f"{mount.inodes_used:,} of {mount.inodes:,} inodes used"
                    if kind == "inodes" and mount.inodes
                    else f"{size_words(mount.used_b)} of {size_words(mount.size_b)} used"
                )
                add(
                    pct / cfg.disk_warn_pct,
                    f"mount:{mount.path}:{kind}",
                    "storage",
                    tone,  # type: ignore[arg-type]
                    f"{_clip(mount.path, 20)} {kind} {pct:.0f}%",
                    f"{mount.path}: {detail}.",
                )
    if worst is None:
        storage_value = "not read"
    else:
        storage_value = _clip(worst[1])
    areas["storage"] = Area(
        id="storage",
        label="Storage",
        tone=storage_tone,
        value=storage_value,
        title="Fullest mount: " + (worst[1] if worst else "none read"),
    )
    details["mounts"] = mount_rows[:12]

    # Bloat: Claude temp, /tmp leftovers, Docker, containers
    slow = post.slow
    bloat_tone: Tone = "grey" if slow is None else "green"
    users_out: list[dict[str, Any]] = []
    stale_total = 0
    litter_total = 0
    if slow is not None:
        days_words = f"{slow.stale_days:g}"
        for user in slow.users:
            owner = user.label
            who = f"{_possessive(owner)} container" if user.kind == "container" else owner
            if user.kind == "container" and user.running is False:
                bloat_tone = _worse(bloat_tone, "amber")
                add(
                    5e5,
                    f"container:{owner}:down",
                    "bloat",
                    "amber",
                    f"{who} is not running",
                    "The box's container for this user is stopped.",
                    owner,
                )
            if user.writable_b is not None:
                layer_gb = user.writable_b / GIB
                if layer_gb >= cfg.container_layer_warn_gb:
                    bloat_tone = _worse(bloat_tone, "amber")
                    add(
                        layer_gb / cfg.container_layer_warn_gb,
                        f"container:{owner}:layer",
                        "bloat",
                        "amber",
                        f"{who}: writable layer {size_words(user.writable_b)}",
                        "Files written outside the home volume live on the root disk.",
                        owner,
                    )
            for root in user.claude:
                if root.stale_sessions >= 1:
                    stale_total += root.stale_sessions
                    # systemd-tmpfiles-clean runs once a day, so folders that just crossed
                    # the stale age wait up to a day for it. Blame the cleanup only when
                    # the oldest folder is a full day past the line (2026-10-29, live check).
                    overdue = (
                        root.oldest_days is not None and root.oldest_days > slow.stale_days + 1
                    )
                    if not overdue:
                        continue
                    bloat_tone = _worse(bloat_tone, "amber")
                    add(
                        float(root.stale_sessions) / 100 + 1,
                        f"claude:{owner}:{root.where}:{user.kind}",
                        "bloat",
                        "amber",
                        f"{who}: {root.stale_sessions} Claude temp folders older than "
                        f"{days_words} days",
                        f"{_where_words(root.where)}: {root.stale_sessions} of {root.sessions} "
                        f"session folders, {size_words(root.stale_bytes)} in "
                        f"{count_words(root.stale_files)} items. "
                        "The two-day cleanup may not be running.",
                        owner,
                    )
            stale_litter = sum(g.stale for g in user.litter)
            litter_total += stale_litter
            if stale_litter >= cfg.litter_warn:
                bloat_tone = _worse(bloat_tone, "amber")
                add(
                    stale_litter / cfg.litter_warn,
                    f"litter:{owner}:{user.kind}",
                    "bloat",
                    "amber",
                    f"{who}: {count_words(stale_litter)} old /tmp entries",
                    f"Top-level /tmp entries older than {days_words} days.",
                    owner,
                )
            users_out.append(_user_details(user))
        if slow.docker is not None:
            reclaim = slow.docker.images_reclaim_b + slow.docker.build_reclaim_b
            if reclaim / GIB >= cfg.docker_reclaim_warn_gb:
                bloat_tone = _worse(bloat_tone, "amber")
                add(
                    reclaim / GIB / cfg.docker_reclaim_warn_gb,
                    "docker",
                    "bloat",
                    "amber",
                    f"Docker can free {size_words(reclaim)}",
                    "Unused images and build cache.",
                )
        bloat_value = (
            f"{count_words(stale_total)} stale · {count_words(litter_total)} in /tmp"
            if stale_total or litter_total
            else "Nothing stale"
        )
        bloat_title = (
            f"{stale_total} Claude temp folders and {litter_total} /tmp entries older than "
            f"{days_words} days"
        )
        slow_age = int((now - slow.read_at).total_seconds())
        if slow_age > 3 * slow.slow_minutes * 60:
            bloat_title += f" (scans from {ago_words(slow_age)})"
    else:
        bloat_value, bloat_title = "not read", "The box has not posted its slow scans yet"
    areas["bloat"] = Area(
        id="bloat", label="Bloat", tone=bloat_tone, value=_clip(bloat_value), title=bloat_title
    )
    details["users"] = users_out
    details["docker"] = slow.docker.model_dump() if slow and slow.docker else None
    details["dirs"] = [d.model_dump() for d in slow.dirs] if slow else []
    details["journal_b"] = slow.journal_b if slow else None
    details["stale_days"] = slow.stale_days if slow else None

    # The Claude temp guard
    guard = post.guard
    if guard is not None:
        details["guard"] = guard.model_dump()
        if not guard.running:
            bloat_tone = _worse(bloat_tone, "amber")
            areas["bloat"] = areas["bloat"].model_copy(update={"tone": bloat_tone})
            add(
                6e5,
                "guard",
                "bloat",
                "amber",
                "Claude temp guard is not running",
                "Without it a session running longer than the stale age can lose temp files. "
                "Start ae-claude-tmp-guard.service.",
            )
        else:
            if guard.claude_unmatched > 0 and (
                guard.unmatched_readings >= 3 or guard.unmatched_minutes >= 15
            ):
                bloat_tone = _worse(bloat_tone, "amber")
                areas["bloat"] = areas["bloat"].model_copy(update={"tone": bloat_tone})
                add(
                    5.5e5,
                    "guard:unmatched",
                    "bloat",
                    "amber",
                    f"{guard.claude_unmatched} Claude session"
                    f"{'s' if guard.claude_unmatched != 1 else ''} not protected",
                    "The guard cannot tie these sessions to a temp folder, so a long run "
                    "can lose temp files. A newer Claude Code may keep its folder differently.",
                )
            note = f"{guard.locked} live session{'s' if guard.locked != 1 else ''} protected"
            areas["bloat"] = areas["bloat"].model_copy(
                update={"title": f"{areas['bloat'].title}; {note}"}
            )

    # Failed units
    units = [*post.failed_units, *post.failed_user_units]
    other_units = post.failed_units_other + post.failed_user_units_other
    if units or other_units:
        total = len(units) + other_units
        shown = ", ".join(units[:3])
        rest = len(units) - min(3, len(units))
        tail = f" +{rest}" if rest else ""
        others = (
            f"{' ' if shown else ''}{'and ' if shown else ''}{other_units} other"
            if other_units
            else ""
        )
        add(
            0.5,
            "units",
            "system",
            "amber",
            f"{total} failed unit{'s' if total != 1 else ''}: {shown}{tail}{others}",
            "systemd reports these units failed.",
        )
    details["system"] = {
        "uptime_s": post.uptime_s,
        "failed_units": post.failed_units,
        "failed_user_units": post.failed_user_units,
        "failed_units_other": post.failed_units_other,
        "failed_user_units_other": post.failed_user_units_other,
    }

    # Parts the relay could not read
    unread = [p for p in post.failed if p != "units"] if post.failed else []
    for part in unread:
        # Only the fast parts grey their area; the Bloat area greys when the slow scan is
        # missing altogether (2026-10-28, review of PR 389), not for one failed part of it.
        area = _PART_AREA.get(part)
        if area in areas:
            areas[area] = areas[area].model_copy(update={"tone": "grey", "value": "not read"})
    if post.failed:
        add(
            4e5,
            "failed",
            "system",
            "amber",
            f"The box could not read: {', '.join(post.failed[:4])}",
            "The rest of the reading is current. The relay's journal says why.",
        )
    details["failed_parts"] = list(post.failed)

    ordered = _sorted_needs(needs)
    ordered = ordered[:12]
    ordering = ["heat", "cpu", "memory", "storage", "bloat"]
    area_list = [areas[a] for a in ordering if a in areas]
    overall: Tone = "green"
    for area in area_list:
        if area.tone != "grey":
            overall = _worse(overall, area.tone)
    for need in ordered:
        overall = _worse(overall, need.tone)
    if stale:
        overall = _worse(overall, "amber")
    return BoxHealthView(
        state="stale" if stale else "ok",
        tone=overall,
        read_at=post.read_at,
        age_s=age_s,
        slow_read_at=slow.read_at if slow else None,
        slow_age_s=int((now - slow.read_at).total_seconds()) if slow else None,
        host=post.host,
        needs=ordered,
        areas=area_list,
        details=details,
        history=_downsample(stored.history),
        settings=settings,
    )


_AREAS = (
    ("heat", "Heat"),
    ("cpu", "CPU"),
    ("memory", "Memory"),
    ("storage", "Storage"),
    ("bloat", "Bloat"),
)


def _user_details(user: UserHealth) -> dict[str, Any]:
    return {
        "label": user.label,
        "kind": user.kind,
        "running": user.running,
        "writable_b": user.writable_b,
        "home_b": user.home_b,
        "claude": [
            {k: v for k, v in root.model_dump().items() if v is not None} for root in user.claude
        ],
        "pytest": user.pytest.model_dump() if user.pytest else None,
        "litter": [
            {k: v for k, v in group.model_dump().items() if v is not None} for group in user.litter
        ],
        "worktrees": user.worktrees.model_dump() if user.worktrees else None,
    }


def _setting_rows(stored: Stored | None) -> list[SettingRow]:
    """Every setting with its hint; the server's with the value in force, the box's with
    what the box posted (``AE_BOX_ADMINS`` only as a count, never the logins)."""
    env = os.environ
    posted = stored.reading.slow.settings if stored and stored.reading.slow else {}
    rows: list[SettingRow] = []
    for setting in SETTINGS:
        if setting.side == "server":
            if setting.name == "AE_BOX_ADMINS":
                count = len(admins(env))
                value = f"{count} login{'s' if count != 1 else ''}"
            else:
                value = (env.get(setting.name) or "").strip() or setting.default or "default"
        else:
            value = posted.get(setting.name, "set on the box")
        rows.append(
            SettingRow(name=setting.name, side=setting.side, value=value, hint=setting.hint)
        )
    return rows


def json_size(model: BaseModel) -> int:
    """The characters the extension bridge would count for *model*'s JSON answer."""
    return len(json.dumps(model.model_dump(mode="json")))


__all__ = [
    "HINT_MAX",
    "HISTORY_MAX",
    "SETTINGS",
    "BoxHealthPost",
    "BoxHealthStore",
    "BoxHealthView",
    "Config",
    "Setting",
    "admins",
    "config",
    "is_box_admin",
    "reset_store",
    "store",
    "view",
]
