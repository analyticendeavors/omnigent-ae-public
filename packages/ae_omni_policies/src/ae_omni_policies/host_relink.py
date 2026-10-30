"""The box host's reconnect: the Admin Center's Box host card and the CLI ``host-relink.sh`` calls.

Added 2026-10-20, after the Microsoft sign-in cutover. When the server rejects
the box host's credential, the host (``ae-omnigent-host.service``) retries
forever and logs ``Connection refused (HTTP 403): the host tunnel was
rejected``. The only fix was by hand: run ``omnigent login`` on the box, get
the printed sign-in link to the owner within five minutes (the server's login
ticket lasts 300 s), then restart the host, which keeps its credential in
memory. The credential lasts 720 h and renews itself, so this is for rare
failures, but each one meant a terminal on the box.

The server cannot fix a rejected host itself: the host is the one that dials
in, and it is the part being turned away. So the work is split. The box half
is ``deploy/host-relink.sh watch``, a user unit that follows the host's
journal, runs the login loop once the host reports 30 straight rejections or
its unit fails (or when the tile's button asks), sends the link as a
task-store reminder and restarts the host once the sign-in lands. This module
is the server half: the two files the halves share, the routes' answer, and a
CLI the script runs through ``deploy/compose.sh exec -T omnigent python -m
ae_omni_policies.host_relink``, because the files sit on
the server's ``/data`` volume, which only root can read from the box itself
(the same reason ``deploy_hold.py`` has a CLI).

The two files, in :data:`DIR_ENV` when set, else ``/data`` when it is a
directory, else nowhere (memory only, so a dev server or a test never writes
to ``/``):

- ``ae-host-relink-status.json``: what the helper last reported, written by
  ``host_relink status <phase> key=value...``. It holds a phase, counts and
  epoch times, never the sign-in link (the link is a five-minute credential and
  goes only into the reminder).
- ``ae-host-relink-request.json``: a pending ask from the tile's button,
  written by ``POST /v1/ae/host/relink`` and removed by ``host_relink take``.

The CLI prints exactly one line on stdout per command, so the script reads only
the last line (the import may print first), and exits 2 on bad input.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel

DIR_ENV = "AE_HOST_RELINK_DIR"
DATA_DIR = Path("/data")
STATUS_FILE_NAME = "ae-host-relink-status.json"
REQUEST_FILE_NAME = "ae-host-relink-request.json"
CODE_CONNECTED = "host_connected"
CODE_BUSY = "host_relink_busy"
CODE_NOT_SAVED = "host_relink_not_saved"
CODE_NO_HOST = "host_unknown"
# The login loop's attempts, each one sign-in link (deploy/host-relink.sh).
MAX_ATTEMPTS = 5
# A "sending" or "restarting" report older than this is a helper that died
# mid-step; the tile then offers the button again. A "rejected" report is
# refreshed at every 30th rejection (about every 90 s) while one lasts, so one
# this old is an incident that ended without a connect line.
STALE_STEP_S = 10 * 60
# A "failed" report older than this is an old incident, not today's.
STALE_FAILED_S = 6 * 3600
# A request the helper has not taken within this long: the tile says the
# watcher may not be running.
REQUEST_UNTAKEN_S = 3 * 60

Phase = Literal["rejected", "sending", "sent", "restarting", "failed", "connected"]
PHASES: tuple[str, ...] = ("rejected", "sending", "sent", "restarting", "failed", "connected")
State = Literal[
    "connected", "rejected", "sending", "sent", "restarting", "failed", "offline", "unknown"
]
_INT_KEYS = ("streak", "since", "attempt", "attempts", "expires_at")
# Why a loop ended in "failed": every link lapsed, the sign-in worked but the
# host did not restart, or no reminder could be sent.
REASONS: tuple[str, ...] = ("lapsed", "restart", "push")
_SPACE = re.compile(r"\s+")


class HostView(BaseModel):
    """What ``GET`` and ``POST /v1/ae/host...`` answer: the tile's whole state."""

    host: dict[str, str] | None
    state: State
    streak: int
    since: str | None
    attempt: int
    attempts: int
    expires_at: str | None
    expires_in_s: int
    message: str | None
    reason: str | None
    updated_at: str | None
    requested: bool
    requested_at: str | None
    request_untaken: bool
    can_request: bool
    error: str | None


@dataclass(frozen=True)
class Status:
    """The helper's last report. Times are epoch seconds, 0 when not given."""

    phase: str
    updated_at: int
    streak: int = 0
    since: int = 0
    attempt: int = 0
    attempts: int = 0
    expires_at: int = 0
    message: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Request:
    """A pending ask from the tile's button."""

    requested_at: int
    requested_by: str


class HostRelinkSaveError(OSError):
    """A file could not be written or removed; what was there is unchanged."""


def _iso(epoch: int) -> str | None:
    if epoch <= 0:
        return None
    return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


def _whole(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key, 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} is {value!r}, not a whole number")
    return value


def _parse_status(raw: object) -> Status:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("not a host relink status file (expected version 1)")
    phase = raw.get("phase")
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    message = raw.get("message")
    if message is not None and not isinstance(message, str):
        raise ValueError(f"message is {message!r}, not text")
    reason = raw.get("reason")
    if reason is not None and reason not in REASONS:
        raise ValueError(f"unknown reason {reason!r}")
    return Status(
        phase=phase,
        updated_at=_whole(raw, "updated_at"),
        message=message,
        reason=reason,
        **{key: _whole(raw, key) for key in _INT_KEYS},
    )


def _parse_request(raw: object) -> Request:
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise ValueError("not a host relink request file (expected version 1)")
    who = raw.get("requested_by")
    if not isinstance(who, str) or not who.strip():
        raise ValueError(f"requested_by is {who!r}, not a user name")
    return Request(requested_at=_whole(raw, "requested_at"), requested_by=who)


class HostRelinkStore:
    """The status and the request, each a JSON file in *folder* (memory only without one).

    With a folder every read goes to the file: the CLI runs in another process.
    """

    def __init__(self, folder: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._folder = folder
        self._status: Status | None = None
        self._request: Request | None = None

    @property
    def folder(self) -> Path | None:
        return self._folder

    def _path(self, name: str) -> Path:
        assert self._folder is not None
        return self._folder / name

    def _load(self, name: str) -> object | None:
        path = self._path(name)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(f"could not read {path}: {exc}") from exc
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ValueError(f"{path} is not valid JSON ({exc})") from exc

    def _write(self, name: str, body: dict[str, Any]) -> None:
        path = self._path(name)
        tmp: str | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-host-relink-", dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError as exc:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            raise HostRelinkSaveError(
                f"could not write {path} ({exc}); check that the folder is writable and has "
                "space, then retry."
            ) from exc

    def read_status(self) -> Status | None:
        """The last report; raises ValueError for a corrupt file."""
        with self._lock:
            if self._folder is None:
                return self._status
            raw = self._load(STATUS_FILE_NAME)
            if raw is None:
                return None
            try:
                return _parse_status(raw)
            except ValueError as exc:
                raise ValueError(f"{self._path(STATUS_FILE_NAME)}: {exc}") from exc

    def write_status(self, status: Status) -> None:
        with self._lock:
            if self._folder is None:
                self._status = status
                return
            body: dict[str, Any] = {"version": 1, "phase": status.phase}
            body["updated_at"] = status.updated_at
            body.update({key: getattr(status, key) for key in _INT_KEYS})
            body["message"] = status.message
            body["reason"] = status.reason
            self._write(STATUS_FILE_NAME, body)

    def read_request(self) -> Request | None:
        """The pending request; raises ValueError for a corrupt file."""
        with self._lock:
            if self._folder is None:
                return self._request
            raw = self._load(REQUEST_FILE_NAME)
            if raw is None:
                return None
            try:
                return _parse_request(raw)
            except ValueError as exc:
                raise ValueError(f"{self._path(REQUEST_FILE_NAME)}: {exc}") from exc

    def request(self, user: str, *, now: int | None = None) -> Request:
        """Ask the helper for a reconnect link, replacing any request not yet taken."""
        asked = Request(requested_at=_now(now), requested_by=user.strip() or "local")
        with self._lock:
            if self._folder is None:
                self._request = asked
            else:
                self._write(
                    REQUEST_FILE_NAME,
                    {
                        "version": 1,
                        "requested_at": asked.requested_at,
                        "requested_by": asked.requested_by,
                    },
                )
        return asked

    def take(self) -> Request | None:
        """Remove and return the pending request. A corrupt one is removed too, as a request:
        whatever wrote it meant to ask, and the helper's own guards decide what runs."""
        with self._lock:
            try:
                asked = self.read_request()
            except ValueError:
                asked = Request(requested_at=0, requested_by="unknown")
            if self._folder is None:
                self._request = None
                return asked
            path = self._path(REQUEST_FILE_NAME)
            if asked is None and not path.exists():
                return None
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                raise HostRelinkSaveError(f"could not remove {path} ({exc})") from exc
            return asked


def real_hosts(hosts: list[Any]) -> list[dict[str, Any]]:
    """The rows of ``GET /v1/hosts`` a person can pick: sandbox hosts are launch
    targets the server provisions, never a choice (upstream web/src/hooks/useHosts.ts)."""
    return [host for host in hosts if isinstance(host, dict) and not host.get("sandbox_provider")]


def named(hosts: list[dict[str, Any]], wanted: str) -> dict[str, Any] | None:
    """The host whose id or name is *wanted* (``AE_RESUME_HOST``), if any."""
    for host in hosts:
        if wanted in (host.get("host_id"), host.get("name")):
            return host
    return None


def pick_host(hosts: list[Any], wanted: str | None) -> dict[str, Any] | None:
    """The box host among *hosts* (``GET /v1/hosts``), chosen as Resume chooses
    (``router._resume_host``) but online or not: *wanted* by id or name, else the
    only online one, else the only one; ``None`` when that is ambiguous."""
    real = real_hosts(hosts)
    if wanted:
        return named(real, wanted)
    online = [host for host in real if host.get("status") == "online"]
    if len(online) == 1:
        return online[0]
    if not online and len(real) == 1:
        return real[0]
    return None


# A login loop is running: a second request would race it (review of PR 156, 2026-10-20).
BUSY_STATES = ("sending", "sent", "restarting")


def _state(online: bool | None, status: Status | None, now: int) -> State:
    if online:
        return "connected"
    if status is not None:
        age = now - status.updated_at
        if status.phase == "sent" and status.expires_at > now:
            return "sent"
        if status.phase in ("sending", "restarting", "rejected") and age < STALE_STEP_S:
            return status.phase  # type: ignore[return-value]
        if status.phase == "failed" and age < STALE_FAILED_S:
            return "failed"
    return "offline" if online is False else "unknown"


def view(
    host: dict[str, Any] | None,
    status: Status | None,
    asked: Request | None,
    *,
    error: str | None = None,
    now: int | None = None,
) -> HostView:
    """The routes' answer. *host* is the ``/v1/hosts`` row (``None`` when there is none or
    the list failed, with the reason in *error*)."""
    at = _now(now)
    online = None if host is None else host.get("status") == "online"
    state = _state(online, status, at)
    shown = status if status is not None and state not in ("connected", "offline") else None
    busy = state in BUSY_STATES
    expires = shown.expires_at if shown is not None and state == "sent" else 0
    return HostView(
        host=None
        if host is None
        else {
            "id": str(host.get("host_id") or ""),
            "name": str(host.get("name") or host.get("host_id") or ""),
            "status": "online" if online else "offline",
        },
        state=state,
        streak=shown.streak if shown is not None else 0,
        since=_iso(shown.since) if shown is not None else None,
        attempt=shown.attempt if shown is not None else 0,
        attempts=(shown.attempts or MAX_ATTEMPTS) if shown is not None else MAX_ATTEMPTS,
        expires_at=_iso(expires),
        expires_in_s=max(0, expires - at),
        message=shown.message if shown is not None else None,
        reason=shown.reason if shown is not None and state == "failed" else None,
        updated_at=_iso(status.updated_at) if status is not None else None,
        requested=asked is not None and not online,
        requested_at=_iso(asked.requested_at) if asked is not None and not online else None,
        request_untaken=asked is not None
        and not online
        and at - asked.requested_at > REQUEST_UNTAKEN_S,
        # Hidden while connected: a relink restarts the host, which ends every session on it.
        # Hidden while a loop runs too: a second login would race the first.
        can_request=online is False and not busy and asked is None,
        error=error,
    )


def _default_folder() -> Path | None:
    raw = (os.environ.get(DIR_ENV) or "").strip()
    if raw:
        return Path(raw)
    if DATA_DIR.is_dir():
        return DATA_DIR
    return None


_store: HostRelinkStore | None = None
_store_lock = threading.Lock()


def store() -> HostRelinkStore:
    """The process-wide store, built on first use from :data:`DIR_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = HostRelinkStore(_default_folder())
        return _store


def reset_store(replacement: HostRelinkStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


# ── the CLI deploy/host-relink.sh runs ────────────────────────────────────────


def _cli_status(args: list[str], now: int) -> int:
    """``status <phase> [key=value ...]``: record the helper's report, replacing the last."""
    if not args or args[0] not in PHASES:
        print(f"ae host relink: status needs a phase, one of {', '.join(PHASES)}", file=sys.stderr)
        return 2
    status = Status(phase=args[0], updated_at=now)
    for pair in args[1:]:
        key, sep, value = pair.partition("=")
        if not sep:
            print(f"ae host relink: {pair!r} is not key=value", file=sys.stderr)
            return 2
        if key == "message":
            status = replace(status, message=value.strip() or None)
        elif key == "reason" and value in REASONS:
            status = replace(status, reason=value)
        elif key in _INT_KEYS and value.isdigit():
            status = replace(status, **{key: int(value)})
        else:
            print(f"ae host relink: unknown or bad field {pair!r}", file=sys.stderr)
            return 2
    try:
        store().write_status(status)
    except HostRelinkSaveError as exc:
        print(f"ae host relink: {exc}", file=sys.stderr)
        return 2
    print(f"ok {status.phase}")
    return 0


def _cli_take() -> int:
    """``take``: ``request <epoch> <user>`` and the request removed, or ``none``."""
    try:
        asked = store().take()
    except HostRelinkSaveError as exc:
        print(f"ae host relink: {exc}", file=sys.stderr)
        return 2
    if asked is None:
        print("none")
    else:
        print(f"request {asked.requested_at} {_SPACE.sub('_', asked.requested_by.strip())}")
    return 0


async def _remind(title: str, notes: str, now: int) -> bool:
    """A task-store reminder due in 45 s (ae-task-reminders pushes it within a minute)."""
    from ae_omni_policies.taskstore import Settings, TaskStoreClient

    settings = Settings.from_env()
    if not settings.configured:
        print("ae host relink: the task-store service token is not set", file=sys.stderr)
        return False
    at = dt.datetime.fromtimestamp(now + 45, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with TaskStoreClient(settings) as client:
        result = await client.create_task(
            {
                "title": title,
                "notes": notes,
                "kind": "task",
                "status": "open",
                "tags": ["ae"],
                "origin_surface": "omnigent-host-relink",
                "due_at": at,
                "due_is_all_day": 0,
                "reminder_at": at,
            }
        )
    if not result.ok:
        print(f"ae host relink: the reminder was refused: {result.error}", file=sys.stderr)
    return result.ok


def _clock(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).strftime("%H:%MZ")


def public_link(link: str, environ: Mapping[str, str] | None = None) -> str:
    """*link* moved to the host Microsoft calls back to (``OMNIGENT_OIDC_REDIRECT_URI``).

    ``omnigent login`` prints the server URL it was given, the tailnet one, which
    the owner's phone may not reach; the sign-in has to finish on the callback host
    anyway (the 2026-10-20 cutover rewrote it by hand). Without the setting the
    link is unchanged.
    """
    env = os.environ if environ is None else environ
    callback = urlsplit((env.get("OMNIGENT_OIDC_REDIRECT_URI") or "").strip())
    if callback.scheme != "https" or not callback.netloc:
        return link
    parts = urlsplit(link)
    return urlunsplit(("https", callback.netloc, parts.path, parts.query, parts.fragment))


def _cli_push(args: list[str], now: int) -> int:
    """``push <link> <expires epoch>`` sends the sign-in link; ``giveup <n>`` says it failed."""
    kind = args[0]
    if kind == "push":
        if len(args) != 3 or not args[1].startswith("https://") or not args[2].isdigit():
            print("ae host relink: push needs <https link> <expires epoch>", file=sys.stderr)
            return 2
        title = "Tap to reconnect the OmniGent box (5 min)"
        notes = (
            "The OmniGent server is rejecting the box host. Open this link and sign in "
            f"before {_clock(int(args[2]))}; the host restarts by itself once you do, and a "
            f"new link follows if this one lapses. {public_link(args[1])}"
        )
    else:
        if len(args) != 2 or not args[1].isdigit():
            print("ae host relink: giveup needs <attempts>", file=sys.stderr)
            return 2
        title = "OmniGent box host still disconnected"
        notes = (
            f"{args[1]} sign-in links lapsed without a sign-in. Open the Admin Center and tap "
            "Send me a reconnect link to try again."
        )
    sent = asyncio.run(_remind(title, notes, now))
    print("ok" if sent else "failed")
    return 0 if sent else 1


def main(argv: list[str] | None = None, *, now: int | None = None) -> int:
    """``status``, ``take``, ``push`` or ``giveup`` (see the module docstring)."""
    args = sys.argv[1:] if argv is None else argv
    at = _now(now)
    if not args:
        print("ae host relink: give a command: status, take, push or giveup", file=sys.stderr)
        return 2
    if args[0] == "status":
        return _cli_status(args[1:], at)
    if args[0] == "take" and len(args) == 1:
        return _cli_take()
    if args[0] in ("push", "giveup"):
        return _cli_push(args, at)
    print(f"ae host relink: unknown command {' '.join(args)!r}", file=sys.stderr)
    return 2


__all__ = [
    "CODE_CONNECTED",
    "CODE_NOT_SAVED",
    "CODE_NO_HOST",
    "DATA_DIR",
    "DIR_ENV",
    "MAX_ATTEMPTS",
    "PHASES",
    "REASONS",
    "HostRelinkSaveError",
    "HostRelinkStore",
    "HostView",
    "Request",
    "Status",
    "main",
    "named",
    "pick_host",
    "public_link",
    "real_hosts",
    "reset_store",
    "store",
    "view",
]


if __name__ == "__main__":
    sys.exit(main())
