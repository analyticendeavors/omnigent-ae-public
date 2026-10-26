"""Phone alarms before a Key Vault secret expires, as task-store tasks (2026-10-23).

Why: the Key Vault card shows what expires, but only to someone who opens the
Admin Center. Reid wants his phone to say so first. The task app pushes an alarm
to his phone for any task with a due time (its ``reminder_at``), so the server
files a task with one.

When: right after the box's daily reading lands (``POST /v1/ae/secrets/meta``,
06:15 on the box's timer), so no new timer. Each secret with an effective expiry
(the card's own: the earlier of the vault's date and the Entra or GitHub one)
passes through three stages: 30 days or fewer, 7 days or fewer, expired. A stage
fires once per secret per expiry date; a reading that skips one (the relay was
down a week) fires only the worst.

One task per secret per expiry date (plan page, approved 2026-10-23): the first
stage files it, a later stage moves its alarm and rewrites its first line, so a
secret never has two "Rotate" tasks open. When the expiry moves (the secret was
rotated), the server closes the task with a line saying why. A task someone
ticked done stays done; the next stage files a fresh one.

The alarm is 09:00 on the home clock (``AE_TASK_STORE_TZ``), kept as "My clock"
like the task-store worker's ``create_task``; a reading after 09:00 sets the next
day's. Tasks land in the list named by ``AE_SECRET_ALERT_LIST`` ("AE Work" on
the box, Reid's pick until a DevOps board is chosen) and carry the ``ae`` tag
when the app has it. ``AE_SECRET_ALERTS=off`` turns all of it off, and a server
with no task store configured never files anything.

Idempotent: ``/data/ae-secret-alerts.json`` keeps, per secret, the expiry date,
the stage sent and the task id. Restarts and repeat readings change nothing. If
that file is lost, the open tasks are searched for their "Alert key" line before
anything is filed again, and a create that fails without an answer is re-read,
never resent blind (the rule ``taskstore.park`` follows).

Names and dates only: the task names the secret and links to where it is
rotated; no value, length or hash is ever read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from ae_omni_policies import secret_owners, secrets_meta, taskstore

_logger = logging.getLogger(__name__)

STATE_FILE_ENV = "AE_SECRET_ALERTS_FILE"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-secret-alerts.json"

SWITCH_ENV = "AE_SECRET_ALERTS"
LIST_ENV = "AE_SECRET_ALERT_LIST"
TIME_ENV = "AE_SECRET_ALERT_TIME"
DEFAULT_TIME = "09:00"
TAG = "ae"
MARKER = "Alert key: "

Stage = Literal["30", "7", "expired"]
STAGE_DAYS: tuple[tuple[Stage, int], ...] = (("7", 7), ("30", 30))
_RANK: dict[Stage, int] = {"30": 1, "7": 2, "expired": 3}
_MARKER_LINE = re.compile(
    r"^Alert key: (?P<vault>[0-9A-Za-z-]+)/(?P<name>\S+) (?P<expires>\d{4}-\d{2}-\d{2}) "
    r"stage (?P<stage>30|7|expired)$",
    re.MULTILINE,
)
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_OFF = {"off", "0", "false", "no"}


class Sent(BaseModel):
    """One secret's alert task, as filed."""

    vault: str
    name: str
    expires: str
    """The expiry date (YYYY-MM-DD, UTC) the task is for."""
    stage: Stage
    task_id: str
    alarm_at: str | None = None
    sent_at: str
    list: str | None = None
    done: bool = False
    """Ticked done or dropped in the task app; kept so the stage is remembered."""


class AlertState(BaseModel):
    alerts: dict[str, Sent] = Field(default_factory=dict)
    """By ``<vault>/<name>``."""
    last_run: datetime | None = None
    last_error: str | None = None


class AlertInfo(BaseModel):
    """What the Key Vault card says about a secret's open alert."""

    stage: Stage
    alarm_at: str | None
    sent_at: str
    list: str | None
    expires: str


class NextAlarm(BaseModel):
    vault: str
    name: str
    alarm_at: str


class Summary(BaseModel):
    """The alerts block of ``GET /v1/ae/vault``."""

    on: bool
    off_reason: str | None = None
    list: str | None = None
    time: str = DEFAULT_TIME
    tz: str = "UTC"
    next: NextAlarm | None = None
    open: int = 0
    last_run: str | None = None
    last_error: str | None = None


class AlertStore:
    """The sent state, kept in a JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self._memory = AlertState()

    def read(self) -> AlertState:
        """The stored state; empty when there is none or the file cannot be read."""
        with self._lock:
            if self._path is None:
                return self._memory.model_copy(deep=True)
            try:
                return AlertState.model_validate_json(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return AlertState()

    def write(self, state: AlertState) -> None:
        """:raises OSError: When the file cannot be written."""
        with self._lock:
            if self._path is None:
                self._memory = state.model_copy(deep=True)
                return
            tmp: str | None = None
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".ae-secret-alerts-", dir=str(self._path.parent))
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(state.model_dump_json())
                os.replace(tmp, self._path)
            except OSError:
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp)
                raise


def _default_path() -> Path | None:
    raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
    if raw:
        return Path(raw)
    if STATE_FILE_ENV in os.environ:
        return None
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: AlertStore | None = None
_store_lock = threading.Lock()


def store() -> AlertStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = AlertStore(_default_path())
        return _store


def reset_store(replacement: AlertStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment between cases)."""
    global _store
    with _store_lock:
        _store = replacement


# ── settings ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Config:
    on: bool
    off_reason: str | None
    list_name: str | None
    time: str
    tz: str


def config(environ: dict[str, str] | None = None) -> Config:
    """Whether alerts run, and where and when they land."""
    env = os.environ if environ is None else environ
    store_settings = taskstore.Settings.from_env(env)
    raw_time = (env.get(TIME_ENV) or "").strip() or DEFAULT_TIME
    if not _TIME.match(raw_time):
        _logger.warning("%s=%r is not HH:MM; using %s", TIME_ENV, raw_time, DEFAULT_TIME)
        raw_time = DEFAULT_TIME
    list_name = (env.get(LIST_ENV) or "").strip() or None
    off_reason: str | None = None
    if (env.get(SWITCH_ENV) or "").strip().lower() in _OFF:
        off_reason = f"Turned off ({SWITCH_ENV}=off)."
    elif not store_settings.configured:
        off_reason = "No task store is configured (AE_TASK_STORE_URL and the Access token)."
    return Config(
        on=off_reason is None,
        off_reason=off_reason,
        list_name=list_name,
        time=raw_time,
        tz=store_settings.tz,
    )


# ── what is due ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Current:
    """One secret as the newest reading has it."""

    vault: str
    name: str
    meta: secrets_meta.SecretMeta
    expiry: secrets_meta.Expiry
    box: bool
    twin_vaults: tuple[str, ...]

    @property
    def expires(self) -> str | None:
        return self.expiry.expires[:10] if self.expiry.expires else None


@dataclass(frozen=True)
class Due:
    current: Current
    stage: Stage


def key_of(vault: str, name: str) -> str:
    return f"{vault}/{name}"


def stage_of(expiry: secrets_meta.Expiry) -> Stage | None:
    if expiry.expiry_state == "expired":
        return "expired"
    if expiry.days_left is None:
        return None
    for stage, days in STAGE_DAYS:
        if expiry.days_left <= days:
            return stage
    return None


def currents(reading: secrets_meta.Reading, now: datetime) -> dict[str, Current]:
    """Every secret in the reading, both vaults, with the card's effective expiry."""
    vaults = [(reading.vault, reading.secrets, True)] + [
        (name, secrets, False) for name, secrets in reading.vaults.items()
    ]
    entra_from = reading.carried("entra")
    out: dict[str, Current] = {}
    for vault, secrets, box in vaults:
        for name, meta in secrets.items():
            twins = tuple(other for other, rows, _ in vaults if other != vault and name in rows)
            expiry = secrets_meta.expiry(
                meta,
                now,
                # Only the box vault's copy is the token the server holds (vault_inventory).
                source=box or name != secrets_meta.GITHUB_VAULT_NAME,
                entra_from=entra_from,
            )
            out[key_of(vault, name)] = Current(vault, name, meta, expiry, box, twins)
    return out


def due(found: dict[str, Current]) -> dict[str, Due]:
    """The secrets that should have an alert now, by key; disabled ones never do."""
    out: dict[str, Due] = {}
    for key, current in found.items():
        if not current.meta.enabled or current.expires is None:
            continue
        stage = stage_of(current.expiry)
        if stage is not None:
            out[key] = Due(current, stage)
    return out


def alarm_instant(now: datetime, tz: str, hhmm: str) -> datetime:
    """The next *hhmm* on the home clock after *now*, as a UTC instant."""
    zone = ZoneInfo(tz)
    hour, minute = (int(part) for part in hhmm.split(":"))
    today = now.astimezone(zone).date()
    for day in (today, today + timedelta(days=1)):
        at = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).astimezone(UTC)
        if at > now:
            return at
    raise AssertionError("unreachable: tomorrow's alarm is always after now")


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── what the task says ──────────────────────────────────────────────────────


def title_of(name: str, expires: str) -> str:
    return f"Rotate {name} (expires {expires})"


def _days_text(days: int) -> str:
    return f"{days} day" if days == 1 else f"{days} days"


def notes_of(item: Due, owner: str | None) -> str:
    current = item.current
    expiry = current.expiry
    origin = {
        "entra": "Date from Entra, the app credential's end date.",
        "github": "Date from GitHub, the token's own expiry.",
        "vault": "Date from the Key Vault expiry.",
    }.get(expiry.expiry_from or "vault", "")
    if item.stage == "expired":
        first = f"**Expired** on {current.expires}. {origin}"
    else:
        first = (
            f"**Expires in {_days_text(expiry.days_left or 0)}**, on {current.expires}. {origin}"
        )
    edit = secrets_meta.edit_url(current.vault, current.name)
    rotate_url, hint = secrets_meta.rotate_link(current.meta, current.vault)
    edit_line = f"[Edit in Key Vault: {current.vault} / {current.name}]({edit})"
    owner_line = f"Owner: {secret_owners.display_name(owner)}" if owner else "Owner: not set"
    if rotate_url == edit:
        # No issuer page known: one link, not the same one twice.
        lines = [first, owner_line, "", edit_line, hint]
    else:
        lines = [
            first,
            owner_line,
            "",
            f"[Rotate where it is issued]({rotate_url})",
            hint,
            "",
            edit_line,
        ]
    for other in current.twin_vaults:
        lines.append(f"Also in {other}: rotate both copies together.")
    box_key = next(
        (
            key
            for key, name in (
                *secrets_meta.VAULT_NAMES.items(),
                *secrets_meta.HOST_VAULT_NAMES.items(),
            )
            if name == current.name
        ),
        None,
    )
    if current.box and box_key:
        lines.append(
            f"The box uses this as {box_key}: after saving the new version, run "
            "deploy/pull-secrets.sh on the box and restart the server."
        )
    lines += ["", f"{MARKER}{current.vault}/{current.name} {current.expires} stage {item.stage}"]
    return "\n".join(lines)


def _task_body(item: Due, owner: str | None, alarm: datetime) -> dict[str, Any]:
    at = _iso(alarm)
    return {
        "notes": notes_of(item, owner),
        "due_at": at,
        "due_is_all_day": 0,
        # The alarm is what the app pushes; a due time alone never rings.
        "reminder_at": at,
        "floating": 1,
        "important": 1,
        "urgent": 1 if item.stage != "30" else 0,
    }


# ── the pass ────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    created: list[str] = field(default_factory=list)
    rearmed: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_run_lock = asyncio.Lock()


def _close_reason(sent: Sent, current: Current | None, present: set[str]) -> str | None:
    """Why *sent*'s task no longer stands, or ``None`` while it does."""
    if current is None:
        return f"{sent.name} is no longer in {sent.vault}." if sent.vault in present else None
    if not current.meta.enabled:
        return "the secret was disabled in Key Vault."
    if current.expires is None:
        return "the expiry was removed."
    if current.expires != sent.expires:
        if current.expires > sent.expires:
            return f"the expiry moved to {current.expires}."
        return f"the expiry changed to {current.expires}."
    return None


async def run(
    reading: secrets_meta.Reading | None,
    *,
    now: datetime | None = None,
    cfg: Config | None = None,
    client: taskstore.TaskStoreClient | None = None,
    alerts: AlertStore | None = None,
    owners: dict[str, str] | None = None,
) -> Outcome:
    """File, re-arm and close alert tasks for *reading*. Never raises; failures are logged
    and kept in the state's ``last_error``, and the next reading tries again."""
    cfg = cfg or config()
    out = Outcome()
    if not cfg.on or reading is None:
        return out
    at = now or datetime.now(UTC)
    alerts = alerts or store()
    owner_map = owners if owners is not None else secret_owners.store().read()
    async with _run_lock:
        own_client = client is None
        api = client or taskstore.TaskStoreClient(taskstore.Settings.from_env())
        try:
            await _pass(reading, at, cfg, api, alerts, owner_map, out)
        except Exception as exc:  # a daily background pass must not die silently
            _logger.exception("secret alerts: pass failed")
            out.errors.append(f"the pass failed: {exc!r}")
        finally:
            if own_client:
                await api.aclose()
        state = alerts.read()
        state.last_run = at
        state.last_error = "; ".join(out.errors) or None
        try:
            alerts.write(state)
        except OSError as exc:
            _logger.warning("secret alerts: state not saved: %s", exc)
    if out.created or out.rearmed or out.closed or out.errors:
        _logger.info(
            "secret alerts: created %s, re-armed %s, closed %s, errors %s",
            out.created,
            out.rearmed,
            out.closed,
            out.errors,
        )
    return out


class _Lookups:
    """The app's lists, tags and open tasks, each read at most once per pass."""

    def __init__(self, api: taskstore.TaskStoreClient, cfg: Config) -> None:
        self.api = api
        self.cfg = cfg
        self._bucket: tuple[str | None, str | None] | None = None
        self._tags: list[str] | None = None
        self._open: list[dict[str, Any]] | None = None

    async def bucket(self, out: Outcome) -> tuple[str | None, str | None]:
        """(list id, list name), or (None, None) for no list."""
        if self._bucket is None:
            self._bucket = (None, None)
            if self.cfg.list_name:
                listed = await self.api.list_buckets()
                rows = (listed.data or {}).get("buckets") if listed.ok else None
                want = self.cfg.list_name.lower()
                hits = [
                    row
                    for row in rows or []
                    if isinstance(row, dict)
                    and not row.get("hidden")
                    and str(row.get("name") or "").strip().lower() == want
                ]
                if len(hits) == 1:
                    self._bucket = (hits[0].get("id"), hits[0].get("name"))
                elif listed.ok:
                    out.errors.append(
                        f'no single visible list is called "{self.cfg.list_name}"; '
                        f"filed with no list. Fix {LIST_ENV}."
                    )
                else:
                    out.errors.append(
                        f"could not read the lists ({listed.error}); filed with no list"
                    )
        return self._bucket

    async def tags(self) -> list[str]:
        if self._tags is None:
            listed = await self.api.list_tags()
            names = (listed.data or {}).get("tags") if listed.ok else None
            known = {(row.get("name") if isinstance(row, dict) else row) for row in names or []}
            self._tags = [TAG] if TAG in known else []
        return self._tags

    async def open_tasks(self) -> list[dict[str, Any]] | None:
        if self._open is None:
            listed = await self.api.list_tasks("open")
            if not listed.ok or not isinstance(listed.data, dict):
                return None
            self._open = [
                task
                for task in listed.data.get("tasks") or []
                if isinstance(task, dict)
                and task.get("origin_surface") == taskstore.ORIGIN_SURFACE
            ]
        return self._open


async def _read(api: taskstore.TaskStoreClient, task_id: str) -> tuple[str | None, str]:
    """(status, notes) of the task; status ``"gone"`` when the app has no such task,
    ``None`` when it could not be read."""
    read = await api.get_task(task_id)
    if read.ok and isinstance(read.data, dict):
        return str(read.data.get("status") or "open"), str(read.data.get("notes") or "")
    if read.status == 404:
        return "gone", ""
    return None, ""


async def _pass(
    reading: secrets_meta.Reading,
    now: datetime,
    cfg: Config,
    api: taskstore.TaskStoreClient,
    alerts: AlertStore,
    owners: dict[str, str],
    out: Outcome,
) -> None:
    found = currents(reading, now)
    wanted = due(found)
    # A vault that came back empty is treated as unread: a throttled or refused list
    # must not read as every secret leaving it (review of PR 304, 2026-10-23).
    present = {
        vault
        for vault, rows in [(reading.vault, reading.secrets), *reading.vaults.items()]
        if rows
    }
    state = alerts.read()
    lookups = _Lookups(api, cfg)

    def save() -> None:
        alerts.write(state)

    # 1. Close what no longer stands, and notice what was ticked done.
    for key, sent in list(state.alerts.items()):
        reason = _close_reason(sent, found.get(key), present)
        if reason is None:
            # Ticked done, or reopened after that: the next stage depends on which.
            status, _ = await _read(api, sent.task_id)
            if status is not None and sent.done != (status != "open"):
                sent.done = status != "open"
                save()
            continue
        if not sent.done:
            status, notes = await _read(api, sent.task_id)
            if status is None:
                out.errors.append(f"could not read the task for {key}; closing it next time")
                continue
            if status == "open":
                closed = await api.patch_task(
                    sent.task_id,
                    {
                        "status": "done",
                        "notes": f"{notes}\n\nClosed {now.date().isoformat()}: {reason}".strip(),
                    },
                )
                if not closed.ok:
                    out.errors.append(f"could not close the task for {key} ({closed.error})")
                    continue
                out.closed.append(key)
        del state.alerts[key]
        save()

    # 2. File or re-arm what is due.
    alarm = alarm_instant(now, cfg.tz, cfg.time)
    for key, item in sorted(wanted.items()):
        current = item.current
        sent = state.alerts.get(key)
        if sent is not None and sent.expires != current.expires:
            # Its close failed above; the new date waits until the old task is closed.
            continue
        if sent is not None:
            # Sent already, open or ticked done: only a worse stage files again.
            if _RANK[sent.stage] >= _RANK[item.stage]:
                continue
        else:
            adopted, looked = await _adopt(lookups, current)
            if not looked:
                # Unknown is not "none": filing now could duplicate a task the lost
                # state file knew about (Codex review of PR 304).
                out.errors.append(
                    f"could not read the open tasks; {key} waits for the next reading"
                )
                continue
            sent = adopted
            if sent is not None:
                state.alerts[key] = sent
                save()
                if _RANK[sent.stage] >= _RANK[item.stage]:
                    continue
        owner = owners.get(secret_owners.key_of(current.vault, current.name))
        body = _task_body(item, owner, alarm)
        if sent is not None and not sent.done:
            status, old_notes = await _read(api, sent.task_id)
            if status is None:
                out.errors.append(f"could not read the task for {key}; moving its alarm next time")
                continue
            # Anything a person wrote under the Alert key line stays.
            marker = _MARKER_LINE.search(old_notes)
            tail = old_notes[marker.end() :].rstrip() if marker else ""
            if tail:
                body = {**body, "notes": body["notes"] + tail}
            rearmed = (
                await api.patch_task(sent.task_id, body)
                if status == "open"
                else taskstore.Result(ok=False, status=404, kind="client", error="gone")
            )
            if rearmed.ok:
                sent.stage = item.stage
                sent.alarm_at = body["reminder_at"]
                sent.sent_at = _iso(now)
                save()
                out.rearmed.append(key)
                continue
            if rearmed.status != 404:
                out.errors.append(f"could not move the alarm for {key} ({rearmed.error})")
                continue
            # The task was deleted or closed in the app since this pass read it: file a fresh one.
            body = _task_body(item, owner, alarm)
        made = await _create(lookups, item, body, out)
        if made is None:
            continue
        task_id, list_name = made
        state.alerts[key] = Sent(
            vault=current.vault,
            name=current.name,
            expires=current.expires or "",
            stage=item.stage,
            task_id=task_id,
            alarm_at=body["reminder_at"],
            sent_at=_iso(now),
            list=list_name,
        )
        save()
        out.created.append(key)


async def _adopt(lookups: _Lookups, current: Current) -> tuple[Sent | None, bool]:
    """An open task of ours for *current*'s expiry, found by its "Alert key" line, and
    whether the open tasks could be read at all."""
    tasks = await lookups.open_tasks()
    if tasks is None:
        return None, False
    for task in tasks:
        for match in _MARKER_LINE.finditer(str(task.get("notes") or "")):
            if (
                match["vault"] == current.vault
                and match["name"] == current.name
                and match["expires"] == current.expires
                and isinstance(task.get("id"), str)
            ):
                return Sent(
                    vault=current.vault,
                    name=current.name,
                    expires=match["expires"],
                    stage=match["stage"],  # type: ignore[arg-type]
                    task_id=task["id"],
                    alarm_at=_normal(task.get("reminder_at")),
                    sent_at=str(task.get("created_at") or ""),
                ), True
    return None, True


def _normal(value: object) -> str | None:
    """An app timestamp in this module's own form, so alarms compare as strings."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return _iso(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


async def _create(
    lookups: _Lookups, item: Due, body: dict[str, Any], out: Outcome
) -> tuple[str, str | None] | None:
    """File the task; (task id, list name), or ``None`` with the error noted."""
    current = item.current
    bucket_id, list_name = await lookups.bucket(out)
    title = title_of(current.name, current.expires or "")
    full = {
        **body,
        "title": title,
        "kind": "task",
        "status": "open",
        "tags": await lookups.tags(),
        "origin_surface": taskstore.ORIGIN_SURFACE,
        "bucket_id": bucket_id,
    }
    created = await lookups.api.create_task(full)
    if not created.ok and created.kind in ("transport", "server"):
        # It may have landed (a timeout, or an edge 5xx after the commit): re-read, never resend.
        listed = await lookups.api.list_tasks("open")
        rows = (
            (listed.data or {}).get("tasks") if listed.ok and isinstance(listed.data, dict) else []
        )
        match = next(
            (
                task
                for task in rows or []
                if isinstance(task, dict)
                and task.get("title") == title
                and task.get("origin_surface") == taskstore.ORIGIN_SURFACE
                and task.get("notes") == body["notes"]
            ),
            None,
        )
        if match is not None:
            created = taskstore.Result(ok=True, status=201, data=match)
    key = key_of(current.vault, current.name)
    if not created.ok:
        out.errors.append(f"could not file the task for {key} ({created.error})")
        return None
    task = created.data if isinstance(created.data, dict) else {}
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        out.errors.append(f"the app answered {created.status} without a task id for {key}")
        return None
    return task_id, list_name


# ── what the card shows ─────────────────────────────────────────────────────


def infos(state: AlertState) -> dict[str, AlertInfo]:
    """Each open alert by ``<vault>/<name>``, for the card's rows."""
    return {
        key: AlertInfo(
            stage=sent.stage,
            alarm_at=sent.alarm_at,
            sent_at=sent.sent_at,
            list=sent.list,
            expires=sent.expires,
        )
        for key, sent in state.alerts.items()
        if not sent.done
    }


def summary(state: AlertState, cfg: Config, now: datetime) -> Summary:
    open_alerts = [sent for sent in state.alerts.values() if not sent.done]
    upcoming = sorted(
        (sent for sent in open_alerts if sent.alarm_at and sent.alarm_at > _iso(now)),
        key=lambda sent: sent.alarm_at or "",
    )
    first = upcoming[0] if upcoming else None
    return Summary(
        on=cfg.on,
        off_reason=cfg.off_reason,
        list=cfg.list_name,
        time=cfg.time,
        tz=cfg.tz,
        next=(
            NextAlarm(vault=first.vault, name=first.name, alarm_at=first.alarm_at or "")
            if first
            else None
        ),
        open=len(open_alerts),
        last_run=state.last_run.isoformat() if state.last_run else None,
        last_error=state.last_error,
    )


__all__ = [
    "DEFAULT_TIME",
    "LIST_ENV",
    "MARKER",
    "STATE_FILE_ENV",
    "SWITCH_ENV",
    "TIME_ENV",
    "AlertInfo",
    "AlertState",
    "AlertStore",
    "Config",
    "Outcome",
    "Sent",
    "Summary",
    "alarm_instant",
    "config",
    "currents",
    "due",
    "infos",
    "notes_of",
    "reset_store",
    "run",
    "stage_of",
    "store",
    "summary",
    "title_of",
]
