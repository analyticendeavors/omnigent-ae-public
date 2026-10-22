"""The deploy hold: a pause on the box's automatic deploys for one, two or three hours.

Added 2026-10-19. A deploy restarts the server and the host, which cuts every
turn in flight short, and ``deploy/auto-update.sh`` only waits for quiet up to
two hours before it goes ahead anyway. Holding a release back for a demo or a
long run meant ``systemctl --user stop ae-omnigent-auto-update.timer`` on the
box by hand: the Admin Center page could not show it, and nobody remembered to
start the timer again, so releases piled up unseen. The hold replaces that: a
tap on the page sets it, it ends on its own, and the page shows who set it and
until when.

This module is the server's half (the store, the routes' body and answer, and
a CLI); ``deploy/auto-update.sh`` is the other half. It reads the hold through
``deploy/compose.sh exec -T omnigent python -m ae_omni_policies.deploy_hold``,
because the file sits on the server's ``/data`` volume, which only root can
read from the box itself. The CLI prints exactly one line on stdout: ``none``,
or ``held <until epoch> <set_by>``; a file it cannot read is a reason on stderr
and exit 2, and the script then deploys as if there were no hold, so a broken
hold never blocks a release.

The file shape is ``{"version": 1, "until": <epoch s>, "set_at": <epoch s>,
"set_by": "<user>"}``; no file means no hold, and clearing removes it. Where it
lives: :data:`STATE_FILE_ENV` when set; else ``/data/ae-deploy-hold.json`` when
``/data`` is a directory (the box bind-mounts it); else nowhere, and the hold
lives in memory only, so a dev server or a test never writes to ``/``. It holds
only times and a user name, never a secret.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt

STATE_FILE_ENV = "AE_DEPLOY_HOLD_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-deploy-hold.json"
CHOICES = (1, 2, 3)
MAX_HOURS = 3
# A stored hold longer than this was not written by set(): the file is corrupt.
# The minute of slack covers a clock that stepped between two reads of it.
_MAX_SPAN_S = MAX_HOURS * 3600 + 60
CODE_NOT_SAVED = "deploy_hold_not_saved"
_SPACE = re.compile(r"\s+")


class HoldRequest(BaseModel):
    """``POST /v1/ae/deploy/hold``: how many hours, whole, 1 to 3.

    Strict like Fabric's keep awake: ``"2"`` or ``2.5`` is a 422.
    """

    model_config = ConfigDict(extra="forbid")

    hours: StrictInt = Field(ge=CHOICES[0], le=MAX_HOURS)


class HoldView(BaseModel):
    """What all three routes answer: the hold as the page shows it."""

    held: bool
    until: str | None
    set_at: str | None
    set_by: str | None
    remaining_s: int
    choices: list[int]
    error: str | None


@dataclass(frozen=True)
class Hold:
    """A hold as stored: epoch seconds and who set it."""

    until: int
    set_at: int
    set_by: str


@dataclass(frozen=True)
class Reading:
    """What a read found: the hold in force (``None`` when there is none or it
    has expired), and why the file could not be read, when it could not."""

    hold: Hold | None
    error: str | None = None


class DeployHoldSaveError(OSError):
    """The hold could not be written or removed; what was there is unchanged."""


# Epoch seconds past this (year 5138) cannot be formatted, so a file holding one is corrupt.
_MAX_EPOCH_S = 10**11


def _iso(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clock(epoch: int) -> str:
    """``04:46Z``: the short UTC time the log lines and the script use."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%H:%MZ")


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


def _parse(raw: object) -> Hold:
    """A stored hold, checked; raises ValueError with the reason when it is not one."""
    if not isinstance(raw, dict):
        raise ValueError("not a deploy hold file (expected a JSON object)")
    if raw.get("version") != 1:
        raise ValueError(f"unknown version {raw.get('version')!r} (expected 1)")
    until, set_at, set_by = raw.get("until"), raw.get("set_at"), raw.get("set_by")
    for name, value in (("until", until), ("set_at", set_at)):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{name} is {value!r}, not whole epoch seconds")
    assert isinstance(until, int) and isinstance(set_at, int)
    for name, value in (("until", until), ("set_at", set_at)):
        if not 0 < value < _MAX_EPOCH_S:
            raise ValueError(f"{name} is {value!r}, not a plausible epoch time")
    if not isinstance(set_by, str) or not set_by.strip():
        raise ValueError(f"set_by is {set_by!r}, not a user name")
    if until - set_at > _MAX_SPAN_S:
        raise ValueError(f"the hold runs {until - set_at} seconds, longer than {MAX_HOURS} hours")
    return Hold(until=until, set_at=set_at, set_by=set_by)


class DeployHoldStore:
    """The one hold, kept in a JSON file (memory only without a path).

    With a file, every read goes to the file: the script's CLI reads it from
    another process, so the file is the only truth and nothing is cached here.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self._memory: Hold | None = None

    @property
    def path(self) -> Path | None:
        """The file the hold is kept in, ``None`` when it is kept in memory."""
        return self._path

    def _stored(self) -> Hold | None:
        """The stored hold, expired or not; raises ValueError for a corrupt file."""
        if self._path is None:
            return self._memory
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(f"could not read {self._path}: {exc}") from exc
        try:
            raw = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"{self._path} is not valid JSON ({exc})") from exc
        try:
            return _parse(raw)
        except ValueError as exc:
            raise ValueError(f"{self._path}: {exc}") from exc

    def read(self, *, now: int | None = None) -> Reading:
        """The hold in force at *now*; an expired one reads as none, a corrupt one as none
        with the reason in ``error``."""
        with self._lock:
            try:
                hold = self._stored()
            except ValueError as exc:
                return Reading(None, error=str(exc))
        if hold is None:
            return Reading(None)
        at = _now(now)
        if hold.until <= at:
            return Reading(None)
        # 2026-10-19: _parse bounds only until - set_at. A clock that jumped back
        # after the set, or a hand-edited file, leaves an until far ahead of now,
        # so the cap is checked against now as well and reads like a corrupt file.
        if hold.until - at > _MAX_SPAN_S:
            where = self._path or "the deploy hold"
            return Reading(
                None,
                error=(
                    f"{where}: the hold ends {hold.until - at} seconds from now, "
                    f"more than {MAX_HOURS} hours away"
                ),
            )
        return Reading(hold)

    def _write(self, hold: Hold) -> None:
        assert self._path is not None
        body = {"version": 1, "until": hold.until, "set_at": hold.set_at, "set_by": hold.set_by}
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-deploy-hold-", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
            os.replace(tmp, self._path)
        except OSError as exc:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            raise DeployHoldSaveError(
                f"could not write {self._path} ({exc}); check that the folder is writable "
                "and has space, then retry."
            ) from exc

    def set(self, hours: int, user: str, *, now: int | None = None) -> Hold:
        """Hold deploys for *hours* from *now*, replacing any hold (a corrupt file included).

        :raises ValueError: When *hours* is not one of :data:`CHOICES`.
        :raises DeployHoldSaveError: When the file could not be written.
        """
        if hours not in CHOICES:
            raise ValueError(f"hours must be one of {CHOICES}, not {hours!r}")
        start = _now(now)
        hold = Hold(until=start + hours * 3600, set_at=start, set_by=user.strip() or "local")
        with self._lock:
            if self._path is None:
                self._memory = hold
            else:
                self._write(hold)
        return hold

    def clear(self, *, now: int | None = None) -> Reading:
        """End the hold now (removing nothing is fine); returns what was in force before.

        :raises DeployHoldSaveError: When the file exists and could not be removed.
        """
        # One lock for the read and the unlink (2026-10-19, hence the RLock): a set
        # landing between them would be deleted without being reported.
        with self._lock:
            before = self.read(now=now)
            if self._path is None:
                self._memory = None
                return before
            try:
                self._path.unlink(missing_ok=True)
            except OSError as exc:
                raise DeployHoldSaveError(
                    f"could not remove {self._path} ({exc}); deploys stay held until it "
                    "expires. Check the folder's permissions, then retry."
                ) from exc
        return before


def view(reading: Reading, *, now: int | None = None) -> HoldView:
    """The routes' answer for *reading*."""
    hold = reading.hold
    if hold is None:
        return HoldView(
            held=False,
            until=None,
            set_at=None,
            set_by=None,
            remaining_s=0,
            choices=list(CHOICES),
            error=reading.error,
        )
    return HoldView(
        held=True,
        until=_iso(hold.until),
        set_at=_iso(hold.set_at),
        set_by=hold.set_by,
        remaining_s=max(0, hold.until - _now(now)),
        choices=list(CHOICES),
        error=reading.error,
    )


def _default_path() -> Path | None:
    raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: DeployHoldStore | None = None
_store_lock = threading.Lock()


def store() -> DeployHoldStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = DeployHoldStore(_default_path())
        return _store


def reset_store(replacement: DeployHoldStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


def main(*, now: int | None = None) -> int:
    """The CLI ``deploy/auto-update.sh`` reads: one line on stdout, exit 0; or exit 2.

    ``none`` when no hold is in force; ``held <until epoch> <set_by>`` while one
    is, with any whitespace in the user name replaced by ``_`` so the line splits
    into exactly three words. A corrupt or unreadable file prints the reason on
    stderr, nothing on stdout, and exits 2.
    """
    reading = store().read(now=now)
    if reading.error is not None:
        print(f"ae deploy hold: {reading.error}", file=sys.stderr)
        return 2
    hold = reading.hold
    if hold is None:
        print("none")
    else:
        print(f"held {hold.until} {_SPACE.sub('_', hold.set_by.strip())}")
    return 0


__all__ = [
    "CHOICES",
    "CODE_NOT_SAVED",
    "DATA_DIR",
    "MAX_HOURS",
    "STATE_FILE_ENV",
    "DeployHoldSaveError",
    "DeployHoldStore",
    "Hold",
    "HoldRequest",
    "HoldView",
    "Reading",
    "clock",
    "main",
    "reset_store",
    "store",
    "view",
]


if __name__ == "__main__":
    sys.exit(main())
