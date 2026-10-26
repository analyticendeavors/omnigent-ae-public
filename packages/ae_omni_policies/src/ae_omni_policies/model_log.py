"""The model log: one JSON line each time a session's model or effort changes (2026-10-24, PR 4).

Mirrors ``preset_log.py``. ``POST /v1/ae/sessions/{id}/model`` writes a line for every
change, whoever made it.

Line shape: ``{"session", "caller", "by", "from": {"model", "effort"}, "to": {...},
"kind", "reason", "approved": "auto" | "reid" | "user", "at": unix seconds}``. The file
is :data:`FILE_ENV` when set (empty: no log), else ``/data/ae-model-log.jsonl`` when
``/data`` is a directory, else nothing is written. Appends hold a lock; past
:data:`MAX_BYTES` the file moves to ``<name>.1`` (one old file kept).

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
from typing import Any

_logger = logging.getLogger(__name__)

FILE_ENV = "AE_MODEL_LOG_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-model-log.jsonl"
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


def append(entry: dict[str, Any], *, now: float | None = None) -> bool:
    """Append *entry* with an ``at`` stamp; ``True`` when written, ``False`` otherwise."""
    path = log_path()
    if path is None:
        return False
    line = json.dumps(
        entry | {"at": int(time.time() if now is None else now)}, separators=(",", ":")
    )
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError as exc:
        _logger.warning("model log not written (%s): %s", path, exc)
        return False
    return True
