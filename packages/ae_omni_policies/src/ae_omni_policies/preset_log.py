"""The preset log: one JSON line each time a session's role preset is set or changed.

Added 2026-10-24 (spawn by role). ``create_session`` writes a line when it starts a
session (``from`` null) and ``PUT /v1/ae/sessions/{id}/preset`` writes one on a
switch, so the Admin Center Presets card has history from the first day.

Line shape: ``{"session": id, "from": old or null, "to": new, "at": unix seconds,
"by": user}``. The file is :data:`FILE_ENV` when set (empty: no log), else
``/data/ae-preset-log.jsonl`` when ``/data`` is a directory, else nothing is written.
Appends hold a lock; past :data:`MAX_BYTES` the file moves to ``<name>.1`` (one old
file kept) and a new one starts.

A log failure never fails the change it records: it logs a warning and returns.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path

_logger = logging.getLogger(__name__)

FILE_ENV = "AE_PRESET_LOG_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-preset-log.jsonl"
MAX_BYTES = 5 * 1024 * 1024

_lock = threading.Lock()


def log_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """Where the log lives, or ``None`` when it is switched off or there is no ``/data``."""
    env = environ if environ is not None else os.environ
    if FILE_ENV in env:
        raw = (env[FILE_ENV] or "").strip()
        return Path(raw) if raw else None
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


def append(
    session: str, old: str | None, new: str, by: str | None, *, now: float | None = None
) -> bool:
    """Append one line; ``True`` when written, ``False`` when skipped or it failed (logged).

    :param session: The session id.
    :param old: The preset id it had, or ``None`` when it had none.
    :param new: The preset id it has now.
    :param by: The user who made the change.
    :param now: Unix seconds; the clock when omitted.
    """
    path = log_path()
    if path is None:
        return False
    line = json.dumps(
        {
            "session": session,
            "from": old,
            "to": new,
            "at": int(time.time() if now is None else now),
            "by": by,
        },
        separators=(",", ":"),
    )
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError as exc:
        _logger.warning("preset log not written (%s): %s", path, exc)
        return False
    return True
