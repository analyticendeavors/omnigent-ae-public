"""Voice logs sent from the browser: one JSON line per upload, kept per user (2026-11-16).

The web app keeps the last couple of minutes of voice events in memory (mic, sentences,
browser errors). When Voice mode runs into trouble, or the user taps "Send voice log", it
posts that buffer to ``POST /v1/ae/voice-log`` so a phone problem can be read afterwards
without the phone. Nothing here is echoed to the server's own log: the lines can hold
spoken words, so they go only to the files.

Files live in ``/data/ae-voice-logs/`` (``AE_VOICE_LOG_DIR`` overrides; empty turns storing
off). ``/data`` is bind-mounted from ``${AE_DATA_DIR:-/secure/omnigent}/data`` on the host, and
the overlay mounts ``/data/ae-voice-logs`` itself from ``AE_VOICE_LOG_HOST_DIR`` (default: the
same place under that data directory). ``AE_VOICE_LOG_OWNER=uid:gid`` makes the server chown the
directory and every file it writes to that owner (directory 0700, files 0600), so the host user
who reads the logs needs no sudo; a chown the process may not do is skipped.
Each user has an active ``<user>.jsonl``; at about 2 MB it rotates to ``.1`` and so on, and
the active file plus four rotated ones are kept.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import json
import os
import re
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

DATA_DIR = Path("/data")
DIR_ENV = "AE_VOICE_LOG_DIR"
OWNER_ENV = "AE_VOICE_LOG_OWNER"
DIR_NAME = "ae-voice-logs"

REASONS = frozenset(
    {"mic_failed", "mic_deaf", "mic_stalled", "mic_paused", "page_hidden", "voice_end", "manual"}
)
MAX_BODY_BYTES = 64 * 1024
MAX_LINES = 500
MAX_EVENT = 64
MAX_DETAIL = 300
MAX_CHAT_ID = 128
RATE_LIMIT = 12
RATE_WINDOW_S = 600.0
MAX_USERS = 256
_MAX_ID = 2**32 - 2
ROTATE_BYTES = 2 * 1024 * 1024
KEEP_FILES = 5

_lock = threading.Lock()
_hits: dict[str, collections.deque[float]] = {}
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class VoiceLine(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # A finite number only: no strings, NaN or Infinity (the browser sends Date.now()).
    t: Annotated[float, Field(strict=True, allow_inf_nan=False)]
    event: Annotated[str, StringConstraints(max_length=MAX_EVENT)]
    detail: Annotated[str, StringConstraints(max_length=MAX_DETAIL)] | None = None


class VoiceLogUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str
    chat_id: Annotated[str, StringConstraints(max_length=MAX_CHAT_ID)] | None = None
    lines: list[VoiceLine] = Field(max_length=MAX_LINES)


def reset() -> None:
    """Forget the rate counters (tests)."""
    with _lock:
        _hits.clear()


def log_dir() -> Path | None:
    raw = os.environ.get(DIR_ENV)
    if raw is not None:
        return Path(raw.strip()) if raw.strip() else None
    return DATA_DIR / DIR_NAME if DATA_DIR.is_dir() else None


def _owner() -> tuple[int, int] | None:
    """The ``uid:gid`` from AE_VOICE_LOG_OWNER, or None when unset or not two integers."""
    raw = os.environ.get(OWNER_ENV, "").strip()
    uid, sep, gid = raw.partition(":")
    if not sep:
        return None
    try:
        owner = int(uid), int(gid)
    except ValueError:
        return None
    # 2**32 - 1 is the "leave unchanged" value for chown, so it is not a real id.
    if any(not 0 <= n <= _MAX_ID for n in owner):
        return None
    return owner


def _own_fd(fd: int, mode: int, *, chown: bool = True) -> None:
    """Give the open ``fd`` its mode and, when AE_VOICE_LOG_OWNER is set, that owner.

    Works on the descriptor, never on a path, so a name swapped for a symlink after the
    open cannot redirect the change (2026-11-21: the log directory is writable by the host
    user the files are given to). Only a regular file with one link, or a directory, is
    changed. Best effort: a refused or out-of-range change is skipped.
    """
    owner = _owner()
    if owner is None:
        return
    try:
        info = os.fstat(fd)
    except OSError:
        return
    regular = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not (regular or stat.S_ISDIR(info.st_mode)):
        return
    if chown:
        with contextlib.suppress(OSError, OverflowError):
            os.fchown(fd, owner[0], owner[1])
    with contextlib.suppress(OSError):
        os.fchmod(fd, mode)


def _own_dir(path: Path, mode: int, *, created: bool) -> None:
    """Own the log directory: chown only one this call created, else chmod only one the owner has.

    An existing directory can be a bind mount whose host source the host user swapped for a
    symlink (Docker follows it), so it is never chowned and is chmodded only when it already
    belongs to the owner (2026-11-21).
    """
    owner = _owner()
    if owner is None:
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        if created:
            _own_fd(fd, mode)
        else:
            with contextlib.suppress(OSError):
                if os.fstat(fd).st_uid == owner[0]:
                    _own_fd(fd, mode, chown=False)
    finally:
        os.close(fd)


def allow(user: str | None, now: float | None = None) -> bool:
    """Spend one of the user's 12 uploads per 10 minutes; False (nothing spent) when out."""
    clock = time.monotonic() if now is None else now
    who = user or "local"
    with _lock:
        hits = _hits.get(who)
        if hits is None:
            if len(_hits) >= MAX_USERS:
                for old in [w for w, h in _hits.items() if not h or clock - h[-1] > RATE_WINDOW_S]:
                    del _hits[old]
                while len(_hits) >= MAX_USERS:
                    del _hits[min(_hits, key=lambda w: _hits[w][-1] if _hits[w] else 0.0)]
            hits = _hits[who] = collections.deque()
        while hits and clock - hits[0] > RATE_WINDOW_S:
            hits.popleft()
        if len(hits) >= RATE_LIMIT:
            return False
        hits.append(clock)
        return True


def _file_stem(user: str | None) -> str:
    who = user or "local"
    digest = hashlib.sha256(who.encode("utf-8")).hexdigest()[:8]
    return f"{_SAFE.sub('_', who).lstrip('.')[:40]}-{digest}"


def _rotate(active: Path) -> None:
    """Shift ``.jsonl`` to ``.1`` (and ``.1`` to ``.2``...), dropping what passes the cap."""
    oldest = active.with_name(f"{active.name}.{KEEP_FILES - 1}")
    with contextlib.suppress(OSError):
        oldest.unlink()
    for n in range(KEEP_FILES - 2, 0, -1):
        src = active.with_name(f"{active.name}.{n}")
        if src.exists():
            os.replace(src, active.with_name(f"{active.name}.{n + 1}"))
    os.replace(active, active.with_name(f"{active.name}.1"))


def append(user: str | None, upload: VoiceLogUpload, directory: Path | None = None) -> bool:
    """Write one JSON line for this upload; False when no directory is set for storing."""
    base = directory if directory is not None else log_dir()
    if base is None:
        return False
    record: dict[str, Any] = {
        "user": user,
        "received_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "reason": upload.reason,
        "chat_id": upload.chat_id,
        "lines": [line.model_dump(exclude_none=True) for line in upload.lines],
    }
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    base.parent.mkdir(parents=True, exist_ok=True)
    try:
        base.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        created = False
    _own_dir(base, 0o700, created=created)
    active = base / f"{_file_stem(user)}.jsonl"
    with _lock:
        with contextlib.suppress(OSError):
            if active.stat().st_size >= ROTATE_BYTES:
                _rotate(active)
        # O_NONBLOCK: a FIFO planted at this name fails with ENXIO instead of blocking
        # under _lock; anything but a regular file is refused before a byte is written.
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(active, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError(f"voice log {active.name} is not a regular file; remove it")
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            _own_fd(handle.fileno(), 0o600)
            handle.write(text)
    return True
