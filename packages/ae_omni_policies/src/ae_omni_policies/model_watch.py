"""New-model watch: the server's side (omnigent-ae, 2026-11-10).

The box's relay (:mod:`model_watch_relay`, a systemd user timer) looks inside the installed Claude
Code for Claude model ids the plan has not seen, sends each newest candidate a one-word question,
and posts what answered to ``POST /v1/ae/model-watch``. This module keeps that report, and for
each newly served id it:

1. fetches the release notes (the reviewer agent has no web tools, so the server reads them),
2. runs the preset review now, with the notes' excerpt in the reviewer's read (``new_models``),
3. files one phone reminder once the suggestions are stored (:func:`after_review`), or at once
   naming the reason when the review could not start.

Restart and retry safety (2026-11-10 review): :func:`record` stores ``review.state = "pending"`` in
the same write that marks an id served, so a restart before the background work ran leaves a
marker; :func:`sweep` (run on every relay POST) restarts pending reviews older than ten minutes,
expires stuck ones, and retries unsent reminders (``notify.status``, about five attempts). A
review belongs to the admin who started it and to the one reviewer session that read it; only that
session's submit completes it (:func:`after_review`).

Follow-up reminder (2026-11-11): a model gets one reminder, plus one follow-up when that first
one only said the review did not run. A review the admin starts by hand (``POST
/preset-review/run``) restarts their ``not_run`` model reviews (:func:`rerun_not_run`), and when
it finishes the "suggestions are waiting" reminder files as a second task under its own key
(:data:`FOLLOWUP_KEY_PREFIX`), so the first task, still open, does not stand in for it. Before
this, the rerun of Fable 5.2's expired review finished with no phone task.

Nothing here changes a preset. The Admin Center (Spend, New models) and every user's model picker
read ``GET /v1/ae/model-watch``; a person applies any suggestion.

State: ``AE_MODEL_WATCH_FILE``, default ``/data/ae-model-watch.json``, written atomically.
Release-notes pages: ``AE_MODEL_WATCH_NOTES_URLS`` (comma separated https URLs), default
:data:`DEFAULT_NOTES_URL`.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import threading
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import cost_targets, preset_review, taskstore

_logger = logging.getLogger("ae.model_watch")

STATE_FILE_ENV = "AE_MODEL_WATCH_FILE"
STATE_FILE_NAME = "ae-model-watch.json"
NOTES_URLS_ENV = "AE_MODEL_WATCH_NOTES_URLS"
# Anthropic's models overview as markdown (confirmed with a real fetch 2026-11-10: 200, final URL
# unchanged, names Fable 5.2 and claude-fable-5-2).
DEFAULT_NOTES_URL = "https://platform.claude.com/docs/en/models/overview.md"

FAMILIES = ("opus", "sonnet", "haiku", "fable", "mythos")
# The same pattern the relay scans the binary with, anchored for one id. Major and minor are one or
# two digits, so a date suffix (claude-opus-4-20250514) is never read as a version.
ID_RE = re.compile(r"^claude-(opus|sonnet|haiku|fable|mythos)-(\d{1,2})(?:-(\d{1,2}))?$")

MAX_PROBES = 12
MAX_MODELS = 200
MAX_DETAIL = 200
MAX_HOST = 80
MAX_EXCERPT = 4096
MAX_NOTES_BYTES = 3_000_000
NOTES_TIMEOUT_S = 12.0
NOTES_USER_AGENT = "omnigent-ae-model-watch/1.0"
REVIEW_WAIT = timedelta(hours=2)
PENDING_AFTER = timedelta(minutes=10)
MAX_NOTIFY_ATTEMPTS = 5
REMINDER_DELAY = timedelta(minutes=3)
STALE_REPORT = timedelta(hours=13)
KEY_PREFIX = "Model key: "
# Neither key contains the other plus the id, so neither task hides the other.
FOLLOWUP_KEY_PREFIX = "Model review key: "
RETENTION_FALLBACK = "Retention: not stated; check before routing client data."

PROBE_VALUES = ("served", "not_served", "error")
_RETENTION = re.compile(r"Retention:[^\n]*?(?:\.(?=\s|$)|$)", re.IGNORECASE)

Version = tuple[int, int]


def parse_id(model_id: str) -> tuple[str, Version] | None:
    """``("fable", (5, 2))`` for ``claude-fable-5-2``; None for anything else, suffixes too."""
    match = ID_RE.match(model_id)
    if match is None:
        return None
    return match.group(1), (int(match.group(2)), int(match.group(3) or 0))


def label_of(model_id: str) -> str:
    """``Fable 5.2`` for ``claude-fable-5-2``; ``Opus 5`` for ``claude-opus-5``."""
    match = ID_RE.match(model_id)
    if match is None:
        return model_id
    minor = f".{match.group(3)}" if match.group(3) is not None else ""
    return f"{match.group(1).capitalize()} {match.group(2)}{minor}"


# -- the relay's report, validated strictly


class ProbePost(BaseModel):
    """One id the relay asked, and what came back."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=40)
    family: str = Field(max_length=16)
    version: list[int] = Field(min_length=2, max_length=2)
    served: bool
    detail: str = Field(default="", max_length=MAX_DETAIL)
    probed_at: datetime
    # A probe that never reached the model (the CLI could not start, a timeout) is an error, not a
    # verdict on the id.
    error: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if ID_RE.match(value) is None:
            raise ValueError("not a Claude model id")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> ProbePost:
        parsed = parse_id(self.id)
        if parsed is None or parsed[0] != self.family or list(parsed[1]) != self.version:
            raise ValueError("family and version must match the id")
        return self


class ReportPost(BaseModel):
    """``POST /v1/ae/model-watch``: the relay's pass."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(max_length=MAX_HOST)
    claude_code_version: str = Field(default="", max_length=40)
    scanned_at: datetime
    probes: list[ProbePost] = Field(default_factory=list, max_length=MAX_PROBES)


def _clean_line(text: Any, limit: int) -> str:
    return preset_review._clean(text, limit)


# -- the store


def _empty() -> dict[str, Any]:
    return {"version": 1, "last_report": None, "models": {}}


class WatchStore:
    """The watch's state in one JSON file (memory only without a path)."""

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._path = path
        self._memory = _empty()

    def read(self) -> dict[str, Any]:
        with self._lock:
            if self._path is None:
                return _copy(self._memory)
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return _empty()
            if not isinstance(raw, dict) or not isinstance(raw.get("models"), dict):
                return _empty()
            raw.setdefault("last_report", None)
            return raw

    def write(self, state: dict[str, Any]) -> None:
        """:raises OSError: When the file cannot be written."""
        with self._lock:
            if self._path is None:
                self._memory = _copy(state)
                return
            cost_targets._atomic_write(self._path, json.dumps(state), ".ae-model-watch-")

    def update(self, change: Callable[[dict[str, Any]], Any]) -> Any:
        """Run *change* on the state under the lock and save it; its return value comes back."""
        with self._lock:
            state = self.read()
            result = change(state)
            self.write(state)
            return result


def _copy(state: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(state)


_store: WatchStore | None = None
_store_lock = threading.Lock()


def store() -> WatchStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = WatchStore(cost_targets._path_from(STATE_FILE_ENV, STATE_FILE_NAME))
        return _store


def reset_store(replacement: WatchStore | None = None) -> None:
    """Drop the process-wide store (tests change the environment)."""
    global _store
    with _store_lock:
        _store = replacement


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(text: Any) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _blank(model_id: str, family: str, version: Version, now: datetime) -> dict[str, Any]:
    return {
        "id": model_id,
        "family": family,
        "version": list(version),
        "label": label_of(model_id),
        "first_seen": _iso(now),
        "last_probed": None,
        "probe": "not_served",
        "detail": "",
        "notes": None,
        "review": None,
        "task_id": None,
    }


def _pending(now: datetime, owner: str) -> dict[str, Any]:
    return {
        "state": "pending",
        "at": _iso(now),
        "review_id": None,
        "count": None,
        "why": None,
        "owner": owner,
        "session": None,
    }


def record(report: ReportPost, now: datetime, owner: str = "") -> list[str]:
    """Keep the relay's pass; the ids that were not served before and are now.

    A probe that errored keeps the id's earlier verdict (a served id never un-serves because one
    probe timed out), and an id not seen before is stored as an error until it answers. A newly
    served id gets ``review.state = "pending"`` (owned by *owner*, the admin who posted) in this
    same write, so the background work that follows can be resumed after a restart.
    """
    new: list[str] = []

    def apply(state: dict[str, Any]) -> None:
        models = state["models"]
        for probe in report.probes:
            parsed = parse_id(probe.id)
            if parsed is None:
                continue
            if probe.id not in models and len(models) >= MAX_MODELS:
                continue
            entry = models.get(probe.id) or _blank(probe.id, parsed[0], parsed[1], now)
            models[probe.id] = entry
            was = entry.get("probe")
            entry["last_probed"] = _iso(probe.probed_at)
            entry["detail"] = _clean_line(probe.detail, MAX_DETAIL)
            if probe.error:
                if was not in PROBE_VALUES or (
                    was == "not_served" and entry.get("task_id") is None
                ):
                    entry["probe"] = "error"
            else:
                entry["probe"] = "served" if probe.served else "not_served"
            if entry["probe"] == "served" and was != "served":
                new.append(probe.id)
                if not entry.get("review"):
                    entry["review"] = _pending(now, owner)
        state["last_report"] = {
            "at": _iso(now),
            "host": _clean_line(report.host, MAX_HOST),
            "claude_code_version": _clean_line(report.claude_code_version, 40),
            "scanned_at": _iso(report.scanned_at),
        }

    store().update(apply)
    return new


def served_ids() -> list[str]:
    """Ids the relay saw answer on the plan, oldest first seen first."""
    models = store().read()["models"]
    rows = [m for m in models.values() if isinstance(m, dict) and m.get("probe") == "served"]
    rows.sort(key=lambda m: str(m.get("first_seen")))
    return [str(m["id"]) for m in rows if isinstance(m.get("id"), str)]


def view(admin: bool, now: datetime) -> dict[str, Any]:
    """``GET /v1/ae/model-watch``: what every picker needs, and for admins the notes and review."""
    state = store().read()
    rows: list[dict[str, Any]] = []
    for model in state["models"].values():
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            continue
        row = {
            key: model.get(key)
            for key in ("id", "label", "family", "first_seen", "probe", "last_probed")
        }
        if admin:
            row["detail"] = model.get("detail")
            row["notes"] = model.get("notes")
            review = model.get("review")
            row["review"] = (
                {k: v for k, v in review.items() if k not in ("owner", "session")}
                if isinstance(review, dict)
                else review
            )
        rows.append(row)
    rows.sort(key=lambda r: (str(r.get("family")), str(r.get("first_seen"))))
    report = state.get("last_report")
    answer: dict[str, Any] = {"models": rows, "served_ids": served_ids()}
    if admin:
        # The host's name and Claude Code's version are for admins only.
        answer["last_report"] = report
        at = _when((report or {}).get("at"))
        answer["report_stale"] = at is None or now - at > STALE_REPORT
    return answer


# -- release notes


def notes_urls(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    raw = (env.get(NOTES_URLS_ENV) or "").strip()
    urls = [u.strip() for u in raw.split(",") if u.strip()] if raw else [DEFAULT_NOTES_URL]
    return [u for u in urls if u.startswith("https://")][:3]


def _excerpt(text: str, model_id: str) -> str | None:
    """About 4 KB around the first mention of the id or its label, as one clean line each."""
    label = label_of(model_id)
    hits = [
        i
        for i in (
            text.lower().find(model_id.lower()),
            text.lower().find(label.lower()),
        )
        if i >= 0
    ]
    if not hits:
        return None
    centre = min(hits)
    start = max(centre - MAX_EXCERPT // 4, 0)
    window = text[start : start + MAX_EXCERPT]
    lines = [_clean_line(line, 400) for line in window.splitlines()]
    return " ".join(line for line in lines if line)[:MAX_EXCERPT] or None


async def fetch_notes(
    model_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    urls: list[str] | None = None,
) -> dict[str, Any]:
    """``{url, excerpt, status}``: ``ok``, ``not_mentioned`` or ``fetch_failed``.

    A failed fetch never blocks the review; it runs with ``excerpt`` None.
    """
    pages = urls if urls is not None else notes_urls()
    first = pages[0] if pages else DEFAULT_NOTES_URL
    failed = True
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(NOTES_TIMEOUT_S),
        follow_redirects=True,
        max_redirects=3,
        headers={"User-Agent": NOTES_USER_AGENT, "Accept": "text/markdown, text/plain, */*"},
    ) as http:
        for url in pages:
            try:
                response = await http.get(url)
            except httpx.HTTPError:
                continue
            if response.status_code != 200 or len(response.content) > MAX_NOTES_BYTES:
                continue
            failed = False
            excerpt = _excerpt(response.text, model_id)
            if excerpt is not None:
                return {"url": url, "excerpt": excerpt, "status": "ok"}
    return {
        "url": first,
        "excerpt": None,
        "status": "fetch_failed" if failed else "not_mentioned",
    }


# -- the reviewer's read


def review_context(now: datetime, owner: str) -> list[dict[str, Any]]:
    """*owner*'s models whose review started under two hours ago, for ``preset_insights_read``."""
    out: list[dict[str, Any]] = []
    for model in store().read()["models"].values():
        review = model.get("review") if isinstance(model, dict) else None
        started = _when((review or {}).get("at"))
        if not review or review.get("state") != "started" or started is None:
            continue
        if not owner or review.get("owner") != owner:
            continue
        if now - started >= REVIEW_WAIT:
            continue
        notes = model.get("notes") or {}
        out.append(
            {
                "id": model["id"],
                "label": model.get("label"),
                "family": model.get("family"),
                "release_notes_excerpt": notes.get("excerpt"),
                "release_notes_status": notes.get("status") or "fetch_failed",
            }
        )
    return out


def bind_session(owner: str, session_id: str) -> None:
    """Tie each of *owner*'s started reviews to the first reviewer session that read it."""

    def apply(state: dict[str, Any]) -> None:
        for model in state["models"].values():
            review = model.get("review") if isinstance(model, dict) else None
            if (
                isinstance(review, dict)
                and review.get("state") == "started"
                and review.get("owner") == owner
                and not review.get("session")
            ):
                review["session"] = session_id[:120]

    store().update(apply)


def rerun_not_run(owner: str, now: datetime) -> dict[str, dict[str, Any]]:
    """*owner*'s ``not_run`` model reviews become ``started`` again; each id's earlier review.

    Called just before *owner* starts a review by hand (``POST /preset-review/run``), which is
    what the not-run reminder asks for, so that review's reader picks the models up and its
    submit sends the follow-up reminder (:func:`after_review`). Another user's run changes
    nothing. A run that does not start puts the reviews back (:func:`undo_rerun`).
    """
    flipped: dict[str, dict[str, Any]] = {}

    def apply(state: dict[str, Any]) -> None:
        for model_id, model in state["models"].items():
            review = model.get("review") if isinstance(model, dict) else None
            if (
                owner
                and isinstance(review, dict)
                and review.get("state") == "not_run"
                and review.get("owner") == owner
            ):
                flipped[model_id] = dict(review)
                model["review"] = {
                    "state": "started",
                    "at": _iso(now),
                    "review_id": None,
                    "count": None,
                    "why": None,
                    "owner": owner,
                    "session": None,
                }

    store().update(apply)
    return flipped


def undo_rerun(previous: Mapping[str, Mapping[str, Any]]) -> None:
    """Put back the reviews :func:`rerun_not_run` flipped, unless a reader already bound one."""

    def apply(state: dict[str, Any]) -> None:
        for model_id, review in previous.items():
            model = state["models"].get(model_id)
            current = model.get("review") if isinstance(model, dict) else None
            if (
                isinstance(current, dict)
                and current.get("state") == "started"
                and not current.get("session")
            ):
                model["review"] = dict(review)

    store().update(apply)


# -- the phone reminder


def _retention_line(summary: Any) -> str:
    match = _RETENTION.search(str(summary or ""))
    return _clean_line(match.group(0), 220) if match else RETENTION_FALLBACK


async def notify(
    model_id: str,
    text: str,
    now: datetime,
    *,
    client: taskstore.TaskStoreClient | None = None,
    followup: bool = False,
) -> str | None:
    """File one reminder for *model_id* (one per model, plus one *followup*); the task id, or None.

    ``reminder_at`` is three minutes out, because a due time alone never rings. The body carries
    ``Model key: <id>``, so a restart that lost the stored task id finds the task and files
    nothing.
    """
    current = store().read()["models"].get(model_id) or {}
    if current.get("task_id"):
        return None
    cfg = cost_targets.config()
    if not cfg.on:
        _logger.info("model watch: no reminder for %s (%s)", model_id, cfg.off_reason)
        return None
    key = f"{FOLLOWUP_KEY_PREFIX if followup else KEY_PREFIX}{model_id}"
    title = f"New Claude model: {label_of(model_id)}" + (" (review done)" if followup else "")
    api = client or taskstore.TaskStoreClient(taskstore.Settings.from_env())
    try:
        listed = await api.list_tasks("open")
        rows = (
            (listed.data or {}).get("tasks") if listed.ok and isinstance(listed.data, dict) else []
        )
        for task in rows or []:
            if (
                isinstance(task, dict)
                and task.get("origin_surface") == taskstore.ORIGIN_SURFACE
                and key in str(task.get("notes") or "").splitlines()
            ):
                _remember_task(model_id, str(task.get("id") or ""))
                return None
        lookups = cost_targets._Lookups(api, cfg)
        bucket_id, _name = await lookups.bucket(cost_targets.Outcome())
        alarm = _iso(now + REMINDER_DELAY)
        created = await api.create_task(
            {
                "title": title,
                "notes": f"{text}\n\n{key}",
                "kind": "task",
                "status": "open",
                "due_at": alarm,
                "due_is_all_day": 0,
                "reminder_at": alarm,
                "floating": 1,
                "important": 1,
                "urgent": 0,
                "tags": await lookups.tags(),
                "origin_surface": taskstore.ORIGIN_SURFACE,
                "bucket_id": bucket_id,
            }
        )
    finally:
        if client is None:
            await api.aclose()
    task = created.data if created.ok and isinstance(created.data, dict) else {}
    task_id = task.get("id")
    if not isinstance(task_id, str) or not task_id:
        _logger.warning(
            "model watch: could not file the reminder for %s (%s)", model_id, created.error
        )
        raise RuntimeError(
            f"the task store refused the reminder: {created.error or created.status}"
        )
    _remember_task(model_id, task_id)
    return task_id


def _remember_task(model_id: str, task_id: str) -> None:
    def apply(state: dict[str, Any]) -> None:
        entry = state["models"].get(model_id)
        if isinstance(entry, dict) and task_id:
            entry["task_id"] = task_id

    store().update(apply)


def _set_review(model_id: str, review: dict[str, Any]) -> None:
    def apply(state: dict[str, Any]) -> None:
        entry = state["models"].get(model_id)
        if isinstance(entry, dict):
            entry["review"] = review

    store().update(apply)


def _set_notify(model_id: str, change: Callable[[dict[str, Any]], None]) -> None:
    def apply(state: dict[str, Any]) -> None:
        entry = state["models"].get(model_id)
        if not isinstance(entry, dict):
            return
        slot = entry.get("notify")
        if not isinstance(slot, dict):
            slot = entry["notify"] = {
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "text": "",
            }
        change(slot)

    store().update(apply)


def _slot_kind(slot: Mapping[str, Any]) -> str:
    """``done`` or ``not_run``; a slot stored before kinds existed is read from its text."""
    kind = slot.get("kind")
    if kind in ("done", "not_run"):
        return str(kind)
    return "done" if "suggestion" in str(slot.get("text") or "") else "not_run"


def _queue_reminder(model_id: str, text: str, kind: str = "not_run") -> None:
    """Keep the reminder to send. A sent one is replaced once only: a ``done`` reminder after a
    sent ``not_run`` one becomes the follow-up, a second task under its own key."""

    def apply(state: dict[str, Any]) -> None:
        entry = state["models"].get(model_id)
        if not isinstance(entry, dict):
            return
        slot = entry.get("notify")
        if not isinstance(slot, dict):
            slot = entry["notify"] = {
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "text": "",
            }
        if slot.get("status") == "sent":
            if slot.get("followup") or kind != "done" or _slot_kind(slot) != "not_run":
                return
            slot["followup"] = True
            slot["attempts"] = 0
            slot["last_error"] = None
            entry["prior_task_id"] = entry.get("task_id")
            entry["task_id"] = None
        slot["text"] = text
        slot["kind"] = kind
        slot["status"] = "pending"

    store().update(apply)


_delivering: set[str] = set()


async def _deliver(model_id: str, now: datetime) -> None:
    """Try the queued reminder once; the outcome lands on ``notify`` (never raises).

    One delivery per model at a time: ``after_review`` and a sweep's retry could otherwise both
    pass the task-id check before either files, and ring the phone twice.
    """
    if model_id in _delivering:
        return
    _delivering.add(model_id)
    try:
        await _deliver_once(model_id, now)
    finally:
        _delivering.discard(model_id)


async def _deliver_once(model_id: str, now: datetime) -> None:
    slot = (store().read()["models"].get(model_id) or {}).get("notify") or {}
    if slot.get("status") != "pending" or not slot.get("text"):
        return
    sent_text, sent_kind = slot.get("text"), _slot_kind(slot)
    try:
        await notify(model_id, str(slot["text"]), now, followup=bool(slot.get("followup")))
    except Exception as exc:  # noqa: BLE001 (a task store outage must not fail the report)
        _logger.warning("model watch: reminder for %s failed", model_id, exc_info=True)
        error = _clean_line(f"{type(exc).__name__}: {exc}", 160)

        def failed(slot: dict[str, Any]) -> None:
            slot["attempts"] = int(slot.get("attempts") or 0) + 1
            slot["last_error"] = error

        _set_notify(model_id, failed)
        return
    filed = bool((store().read()["models"].get(model_id) or {}).get("task_id"))
    again = False

    def settled(state: dict[str, Any]) -> None:
        nonlocal again
        entry = state["models"].get(model_id)
        slot = entry.get("notify") if isinstance(entry, dict) else None
        if not isinstance(slot, dict):
            return
        if not filed:
            # Nothing filed and nothing failed: no task store is configured. Stop retrying;
            # every relay report would otherwise try again forever.
            slot["status"] = "skipped"
            slot["last_error"] = "no task store configured"
            return
        if (
            slot.get("text") != sent_text
            and sent_kind == "not_run"
            and slot.get("kind") == "done"
            and not slot.get("followup")
        ):
            # The review finished while its not-run reminder was being filed: that task is out,
            # so the queued done reminder becomes the follow-up and is sent next.
            slot["followup"] = True
            slot["attempts"] = 0
            slot["last_error"] = None
            entry["prior_task_id"] = entry.get("task_id")
            entry["task_id"] = None
            again = True
            return
        slot["status"] = "sent"
        slot["last_error"] = None

    store().update(settled)
    if again:
        await _deliver_once(model_id, now)


async def retry_reminders(now: datetime) -> list[str]:
    """Send reminders that are still pending for finished reviews (about five attempts each)."""
    retried: list[str] = []
    for model_id, model in list(store().read()["models"].items()):
        if not isinstance(model, dict):
            continue
        slot = model.get("notify")
        review = model.get("review") or {}
        if (
            isinstance(slot, dict)
            and slot.get("status") == "pending"
            and int(slot.get("attempts") or 0) < MAX_NOTIFY_ATTEMPTS
            and review.get("state") in ("done", "not_run")
        ):
            await _deliver(model_id, now)
            retried.append(model_id)
    return retried


async def after_review(
    review: Mapping[str, Any], now: datetime, owner: str, session_id: str
) -> list[str]:
    """Mark *owner*'s started reviews bound to *session_id* done and queue the reminder.

    Called by ``POST .../preset-suggestions`` once the suggestions are stored. Any other user's
    submit, or another session's, changes nothing. Returns the ids notified.
    """
    waiting = [
        c["id"]
        for c in review_context(now, owner)
        if (store().read()["models"].get(c["id"]) or {}).get("review", {}).get("session")
        == session_id
    ]
    if not waiting:
        return []
    count = int(review.get("count") or 0)
    retention = _retention_line(review.get("summary"))
    done: list[str] = []
    for model_id in waiting:
        _set_review(
            model_id,
            {
                "state": "done",
                "at": _iso(now),
                "review_id": str(review.get("id") or "")[:80],
                "count": count,
                "why": None,
                "owner": owner,
                "session": session_id[:120],
            },
        )
        plural = "suggestion is" if count == 1 else "suggestions are"
        text = (
            f"Claude {label_of(model_id)} answers on your plan. {count} preset {plural} "
            f"waiting: Admin Center, Spend, New models. {retention}"
        )
        _queue_reminder(model_id, text, "done")
        await _deliver(model_id, now)
        done.append(model_id)
    return done


async def expire_reviews(now: datetime) -> list[str]:
    """Reviews still ``started`` after two hours become ``not_run``; each gets a reminder."""
    stale: list[str] = []
    for model_id, model in list(store().read()["models"].items()):
        review = model.get("review") if isinstance(model, dict) else None
        started = _when((review or {}).get("at"))
        if (
            review
            and review.get("state") == "started"
            and started
            and now - started >= REVIEW_WAIT
        ):
            _set_review(
                model_id,
                {**review, "state": "not_run", "why": "the review did not finish in 2 hours"},
            )
            stale.append(model_id)
    for model_id in stale:
        _queue_reminder(
            model_id,
            f"Claude {label_of(model_id)} answers on your plan, but the preset review did not "
            f"finish. Run it from Settings, Roles, then check Admin Center, Spend, New models. "
            f"{RETENTION_FALLBACK}",
        )
        await _deliver(model_id, now)
    return stale


RunReview = Callable[[], Awaitable["preset_review.RunNow"]]


_in_flight: set[str] = set()


async def handle_new(
    ids: list[str],
    run_review: RunReview,
    now: datetime,
    *,
    owner: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """For ids that just started answering: notes, then one review, then the reminder.

    A failure anywhere is recorded and, where it stops the review, reminded at once. A review
    that is already running (the weekly one, ``review_running``) is not a failure: the ids stay
    ``pending`` and :func:`sweep` starts the review again later. An id already being handled in
    this process is skipped.
    """
    models = store().read()["models"]
    ids = [
        i
        for i in ids
        if i not in _in_flight
        and ((models.get(i) or {}).get("review") or {}).get("state") == "pending"
    ]
    if not ids:
        return
    _in_flight.update(ids)
    try:
        await _handle(ids, run_review, now, owner, transport)
    finally:
        _in_flight.difference_update(ids)


async def _handle(
    ids: list[str],
    run_review: RunReview,
    now: datetime,
    owner: str,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    for model_id in ids:
        notes = await fetch_notes(model_id, transport=transport)

        def apply(
            state: dict[str, Any], model_id: str = model_id, notes: dict[str, Any] = notes
        ) -> None:
            entry = state["models"].get(model_id)
            if isinstance(entry, dict):
                entry["notes"] = notes
                entry["review"] = {
                    "state": "started",
                    "at": _iso(now),
                    "review_id": None,
                    "count": None,
                    "why": None,
                    "owner": owner,
                    "session": None,
                }

        store().update(apply)
    try:
        result = await run_review()
    except Exception as exc:  # noqa: BLE001 (any failure to start is reported, never raised)
        _logger.warning("model watch: the review did not start", exc_info=True)
        result = preset_review.RunNow(
            500, "run_failed", f"starting the review failed: {type(exc).__name__}"
        )
    if result.started:
        return
    if result.code == "review_running":
        for model_id in ids:
            _set_review(model_id, _pending(now, owner))
        return
    why = _clean_line(result.message or result.code or "the review did not start", 160)
    for model_id in ids:
        _set_review(
            model_id,
            {
                "state": "not_run",
                "at": _iso(now),
                "review_id": None,
                "count": None,
                "why": why,
                "owner": owner,
                "session": None,
            },
        )
        _queue_reminder(
            model_id,
            f"Claude {label_of(model_id)} answers on your plan. The preset review did not start: "
            f"{why} {RETENTION_FALLBACK}",
        )
        await _deliver(model_id, now)


async def sweep(
    now: datetime,
    run_review: RunReview,
    owner: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Everything a relay POST must finish: expire stuck reviews, restart pending ones, retry
    unsent reminders. Safe to run twice at once (ids in flight are skipped)."""
    await expire_reviews(now)
    due = [
        model_id
        for model_id, model in store().read()["models"].items()
        if isinstance(model, dict)
        and (model.get("review") or {}).get("state") == "pending"
        and (started := _when((model.get("review") or {}).get("at"))) is not None
        and now - started >= PENDING_AFTER
    ]
    if due:
        await handle_new(due, run_review, now, owner=owner, transport=transport)
    await retry_reminders(now)


_background: set[asyncio.Task[None]] = set()


def spawn(coro: Awaitable[None]) -> None:
    """Run *coro* in the background, kept alive until it ends; its errors are logged."""
    task = asyncio.ensure_future(coro)
    _background.add(task)

    def done(finished: asyncio.Task[None]) -> None:
        _background.discard(finished)
        if not finished.cancelled() and finished.exception() is not None:
            _logger.warning("model watch background task failed", exc_info=finished.exception())

    task.add_done_callback(done)
