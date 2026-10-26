"""The preset run log: one JSON line each time a lead session's run ends (2026-10-24, PR 1).

Feeds preset insights. The Admin Center card and any report read this file; this module
only writes it.

**When a line is written.** A 60 second sweep (:data:`SWEEP_INTERVAL_S`) looks at every
top-level session that is not a family child (no ``ae.orch`` parent). The first sweep that
sees such a lead at ``ae.status`` ``review`` or ``reference``, or archived after being seen
working, writes one line. A lead that goes back to working and ends again writes a NEW line;
a reader keeps the last line per session. An end is marked written in the state file only after
its line is in the log, so a failure retries on the next sweep; every line carries a ``run_key``
(lead id and the end's update time) and ``rev`` (0, then one more for each amendment), and a
line whose ``run_key`` is already in the last 64 KB of the log is not appended again (a crash
between the append and the state save, or a lost state file). A session first seen already ended
more than :data:`FIRST_SEEN_MAX_AGE_S` ago is marked written without a line, so turning the
feature on does not backfill history. A lead with no cost, no agents and no items (an opened,
then archived, empty chat) gets no line. Nothing is read or written for a user whose
``insights.enabled`` (the roles list, ``role_prefs.Insights``) is not true.

**Line shape** (one object per line, compact JSON): ``session``, ``user``, ``title``,
``preset`` (a preset id, ``custom`` for a per-session customization, ``lead-picks``),
``fingerprint`` (ten hex digits of a hash of the resolved roles and rules, null for
lead-picks), ``revision`` (the user's roles list revision, secondary); all three are taken when
the run is first seen live (so a preset edited mid-run does not change them), else at its end,
``run_key`` and ``rev`` (see above), ``lead`` (``model``, ``effort``), ``wall_s``,
``active_s`` (always null: nothing cheap records it), ``cost``
(``total_usd`` and ``by_model``: the lead plus each session in its family; native subagent cost
is already in the lead's), ``agents`` (the whole family by ``ae.orch`` and parent links, depth 5,
200 rows; a warning is logged when the cap cuts it), ``model_changes`` (from the model log, this
family),
``review`` (the lead's ``report_review`` calls plus ``reviewer_sessions`` counted from
``ae.role`` ``reviewer`` children), ``outcome`` (the ending status, or ``archived``),
``files_touched`` (always null), ``at``.

Each agent: ``session`` (id, null for a native subagent), ``kind`` (``session``, ``codex`` or
``subagent``), ``role``, ``model``, ``effort``, ``seconds``, ``cost_usd`` (null for a native
subagent) and ``hung``. A native subagent's role is read from its Agent tool description by
:func:`native_role` (a port of ``aeAgentRole.ts``), ``unknown`` when it does not match; its
model and effort come from the lead's own Agent or Task call, matched by tool use id, null when
not found (bounded read, :data:`MAX_ITEM_PAGES` pages).

**Hang flag.** Each sweep marks a running child that has not updated for
:data:`HANG_AFTER_S` (900) seconds as hung. The mark is sticky for the run and is recorded
before upstream's 60 minute idle settle hides it.

**Review report.** ``POST /v1/ae/sessions/{id}/review-report`` (the ``report_review`` tool)
stores ``reviewers_run``, ``findings_returned`` and ``findings_fixed`` in the state file until
the run's line is written. Calls in one run add up (a review, a fix round, a second review),
except a call whose ``call_id`` (the review round's name) the run already counted. A
report that arrives within 30 minutes after the line was written appends an amended line
(same ``run_key``, ``rev`` one higher); an older one is dropped, and nothing is kept while the
owner's insights are off.

Files: the log is :data:`FILE_ENV` when set (empty: off), else ``/data/ae-preset-runs.jsonl``
when ``/data`` is a directory (rotated to ``<name>.1`` past 5 MB, as the model log is). The
state is :data:`STATE_FILE_ENV`, else ``/data/ae-preset-runs-state.json``, else memory only.
Only one server worker sweeps (a non-blocking ``flock`` on ``<state>.lock``); reads and writes
of the state share a blocking ``flock`` on ``<state>.io.lock``. ``AE_PRESET_RUNS=0`` turns the
sweep off. A failure never stops the server: it is logged and the sweep goes on.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI

from ae_omni_policies import auto_archive, labels, model_log, role_prefs

_logger = logging.getLogger(__name__)

FILE_ENV = "AE_PRESET_RUNS_LOG_FILE"
STATE_FILE_ENV = "AE_PRESET_RUNS_STATE_FILE"
ENABLED_ENV = "AE_PRESET_RUNS"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-preset-runs.jsonl"
STATE_FILE_NAME = "ae-preset-runs-state.json"
MAX_BYTES = 5 * 1024 * 1024

SWEEP_INTERVAL_S = 60.0
HANG_AFTER_S = 900.0
#: A lead first seen already ended is logged only if it ended this recently (two sweeps and a
#: margin); older ones are marked written without a line, so turning the feature on or losing
#: the state file does not backfill history.
FIRST_SEEN_MAX_AGE_S = 2 * SWEEP_INTERVAL_S + 300.0
#: A report that arrives after its run's line amends that line for this long.
AMEND_WINDOW_S = 1800.0
#: How many review round names (``call_id``) a run remembers to catch a repeated call. A run
#: has a handful of rounds; 100 ids of at most 100 characters keep one lead under 10 KB.
MAX_CALL_IDS = 100
#: How much of the end of the log is checked for a run's ``run_key`` before appending.
TAIL_BYTES = 64 * 1024
#: Family size cap (sessions plus native subagents) and how far down the family is walked.
MAX_FAMILY = 200
MAX_DEPTH = 5
#: Reports for a lead the sweep never saw are dropped after this long.
REPORT_MAX_AGE_S = 24 * 3600.0
#: Item pages (of 100) read from the lead to find native subagents' model and effort.
MAX_ITEM_PAGES = 5
#: Native subagent rows read per run end.
MAX_NATIVE_ROWS = 200
#: Session children remembered per live lead, so archived ones are still found at the end.
MAX_KIDS = 60
_TITLE_MAX = 80
_LOCK_FALLBACK = Path("/tmp") / "ae-preset-runs.lock"
_IO_LOCK_FALLBACK = Path("/tmp") / "ae-preset-runs.io.lock"
_END_STATUSES = (labels.REVIEW, labels.REFERENCE)
_NATIVE_DESCRIPTION = "omnigent.claude_native.description"
_NATIVE_CALL_ID = "omnigent.claude_native.tool_use_id"
_AGENT_TOOLS = frozenset({"Agent", "Task"})
# The scheduled preset reviewer's agent name. Its runs stay out of the run log: they are the
# reviewer reading the log, not work a preset did (2026-10-24, preset insights PR 3).
REVIEWER_AGENT = "preset-reviewer"
UNKNOWN = "unknown"
#: What a visit or an emit did: wrote a line, wrote none (marked or untouched), or must retry.
WROTE, SKIPPED, RETRY = "wrote", "skipped", "retry"
#: Leads whose family-cap warning was already logged in this process.
_CAP_WARNED: set[str] = set()
#: How far a lead's ``updated_at`` may sit past its written end and still count as that end.
END_MARGIN_S = 5.0

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# The log writer (same pattern as model_log.py)


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
        _logger.warning("preset run log not written (%s): %s", path, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Small readers


_NAMES = "|".join(labels.ROLE_IDS)
# Ports of ROLE_PREFIX and ROLE_DESCRIPTION in web/src/lib/aeAgentRole.ts: "Role: Builder", or a
# description opening "Builder A: ..." (at most one 1-2 character token, then a colon).
_ROLE_PREFIX = re.compile(rf"^\s*role:\s*({_NAMES})\b", re.IGNORECASE)
_ROLE_DESCRIPTION = re.compile(
    rf"^\s*(?:role:\s*)?({_NAMES})\b(?:[ \t]+[a-z0-9]{{1,2}})?[ \t]*:", re.IGNORECASE
)


def native_role(description: str | None) -> str:
    """The role a native subagent's Agent tool description opens with, else ``unknown``."""
    text = description or ""
    match = _ROLE_PREFIX.search(text) or _ROLE_DESCRIPTION.search(text)
    if match is None:
        return UNKNOWN
    found = match.group(1).lower()
    return found if found in labels.ROLE_IDS else UNKNOWN


def fingerprint(roles: object, rules: object) -> str:
    """Ten hex digits of a hash of a preset's roles and rules (its behaviour, not its name)."""
    blob = json.dumps({"roles": roles, "rules": rules}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:10]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def _time(conv: Any, name: str) -> float:
    return auto_archive._sane_time(getattr(conv, name, 0)) or 0.0


def _seconds(conv: Any, now: float) -> int:
    return int(max(min(_time(conv, "updated_at"), now) - _time(conv, "created_at"), 0))


def _title(conv: Any) -> str:
    return " ".join(str(getattr(conv, "title", "") or "").split())[:_TITLE_MAX]


def _model_of(conv: Any) -> str | None:
    return getattr(conv, "model_override", None) or getattr(conv, "reported_model", None) or None


def _usage(conv: Any) -> dict[str, Any]:
    usage = getattr(conv, "session_usage", None)
    return usage if isinstance(usage, dict) else {}


def add_cost(into: dict[str, dict[str, Any]], usage: Mapping[str, Any]) -> None:
    """Add a session's ``by_model`` usage into *into*: ``{model: {usd, input_tokens, ...}}``."""
    by_model = usage.get("by_model")
    if not isinstance(by_model, dict):
        return
    for model, bucket in by_model.items():
        if not isinstance(bucket, dict):
            continue
        slot = into.setdefault(str(model), {})
        for key, name in (
            ("total_cost_usd", "usd"),
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            value = _number(bucket.get(key))
            if value is not None:
                slot[name] = slot.get(name, 0) + value


def resolve_preset(store: Any, conv: Any) -> tuple[str | None, str | None]:
    """``(preset id, fingerprint)`` of the roles the lead resolves to now.

    The id is ``custom`` for a per-session customization and ``lead-picks`` when the lead chose;
    both fall back to the ``ae.preset`` label when the roles cannot be resolved.
    """
    label_id = labels.label(conv.labels, labels.PRESET)
    try:
        from omnigent.inner import ae_roles

        resolved = ae_roles.resolve_roles(conv, store)
    except Exception:  # noqa: BLE001 (an unreadable preset must not stop the log)
        _logger.warning("preset runs: could not resolve %s's preset", conv.id, exc_info=True)
        return label_id, None
    if resolved.get("lead_picks"):
        return label_id or "lead-picks", None
    preset = resolved.get("preset")
    if not isinstance(preset, dict):
        return label_id, None
    pid = "custom" if resolved.get("custom") else preset.get("id")
    return (pid if isinstance(pid, str) else label_id), fingerprint(
        preset.get("roles"), preset.get("rules")
    )


def _revision(user: str) -> int | None:
    try:
        return role_prefs.store().get(user).revision
    except Exception:  # noqa: BLE001
        return None


def insights_enabled(user: str | None) -> bool:
    """Whether *user* has switched preset insights on."""
    try:
        insights = role_prefs.store().get(user or "local").insights
    except Exception:  # noqa: BLE001 (cannot tell, so write nothing)
        return False
    return bool(insights and insights.enabled)


# ---------------------------------------------------------------------------
# State


class StateFile:
    """``{"live": {lead: {"kids": [...]}}, "ended": {lead: {...}}, "hung": {child: lead},
    "reports": {lead: {...}}}``.

    Memory only when *path* is None. :meth:`locked` holds a blocking ``flock`` (and a thread lock)
    around a read-modify-write, so the sweep and the review report route never lose each other's
    changes.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._memory: dict[str, Any] = {}
        self._disk_behind = False  # a write failed: the file is older than memory
        self._thread = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"live": {}, "ended": {}, "hung": {}, "reports": {}}

    def _read(self) -> dict[str, Any]:
        raw: Any = self._memory
        if self.path is not None and not self._disk_behind:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                raw = self._memory
            except (OSError, ValueError) as exc:
                _logger.warning("preset runs: unreadable state file %s (%r)", self.path, exc)
                raw = self._memory
        data = self._empty()
        if isinstance(raw, dict):
            for key in data:
                if isinstance(raw.get(key), dict):
                    data[key] = raw[key]
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self._memory = json.loads(json.dumps(data))
        if self.path is None:
            return
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
            os.replace(tmp, self.path)
            tmp = None
            self._disk_behind = False
        except OSError as exc:
            self._disk_behind = True
            _logger.warning("preset runs: cannot write %s (%r); kept in memory", self.path, exc)
        finally:
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)

    def checkpoint(self, data: dict[str, Any]) -> None:
        """Save *data* now (inside :meth:`locked`), so a later failure cannot lose it."""
        self._write(data)

    @contextlib.contextmanager
    def locked(self) -> Iterator[dict[str, Any]]:
        """Yield the state to change in place; it is saved when the block ends, error or not,
        but only if it changed (a partial change is safe: an end is marked only after its line)."""
        with self._thread:
            handle = None
            if self.path is not None:
                try:
                    lock_path = self.path.with_name(self.path.name + ".io.lock")
                    lock_path.parent.mkdir(parents=True, exist_ok=True)
                    handle = open(lock_path, "a")  # noqa: SIM115 (closed below)
                    fcntl.flock(handle, fcntl.LOCK_EX)
                except OSError as exc:
                    _logger.warning("preset runs: cannot lock the state (%r); going on", exc)
                    if handle is not None:
                        handle.close()
                    handle = None
            try:
                data = self._read()
                before = json.dumps(data, sort_keys=True)
                try:
                    yield data
                finally:
                    if json.dumps(data, sort_keys=True) != before:
                        self._write(data)
            finally:
                if handle is not None:
                    handle.close()


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the sweep runs (default on; ``0``, ``false``, ``off``, ``no`` turn it off)."""
    value = (environ if environ is not None else os.environ).get(ENABLED_ENV, "")
    return value.strip().lower() not in {"0", "false", "off", "no"}


def state_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """:data:`STATE_FILE_ENV` (empty: memory only), else the ``/data`` file if ``/data`` exists."""
    env = environ if environ is not None else os.environ
    if STATE_FILE_ENV in env:
        raw = (env[STATE_FILE_ENV] or "").strip()
        return Path(raw) if raw else None
    if DATA_DIR.is_dir():
        return DATA_DIR / STATE_FILE_NAME
    return None


_shared: StateFile | None = None


def shared_state() -> StateFile:
    """The state the sweep and the review report route both use (one object per path)."""
    global _shared
    path = state_path()
    if _shared is None or _shared.path != path:
        _shared = StateFile(path)
    return _shared


def logged_rev(run_key: str) -> int | None:
    """The highest ``rev`` of a line with *run_key* in the last :data:`TAIL_BYTES` of the log."""
    path = log_path()
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(size - TAIL_BYTES, 0))
            text = handle.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    found: int | None = None
    for raw in text.splitlines():
        if run_key not in raw:
            continue
        try:
            entry = json.loads(raw)
        except ValueError:
            continue  # includes the cut first line of the tail
        if isinstance(entry, dict) and entry.get("run_key") == run_key:
            rev = entry.get("rev")
            rev = rev if isinstance(rev, int) else 0
            found = rev if found is None else max(found, rev)
    return found


def _still_at_end(ended: Mapping[str, Any], lead_now: tuple[str | None, float] | None) -> bool:
    """False when the lead's current status or time shows it moved on from the written end."""
    if lead_now is None:
        return True
    status, updated = lead_now
    if status not in (*_END_STATUSES, "archived"):
        return False
    written = _number(ended.get("updated_at"))
    return written is None or updated <= written + END_MARGIN_S


def _sum(old: Any, add: int) -> int:
    return (old if isinstance(old, int) and not isinstance(old, bool) else 0) + add


def _call_ids(holder: Mapping[str, Any]) -> list[str]:
    ids = holder.get("call_ids")
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def _with_call_id(ids: list[str], call_id: str | None) -> list[str]:
    return ids if call_id is None else [*ids, call_id][-MAX_CALL_IDS:]


_REVIEW_KEYS = ("reviewers_run", "findings_returned", "findings_fixed")


def record_review(
    state: StateFile,
    lead_id: str,
    reviewers_run: int,
    findings_returned: int,
    findings_fixed: int,
    *,
    call_id: str | None = None,
    insights_on: bool = True,
    now: float | None = None,
    lead_now: tuple[str | None, float] | None = None,
) -> dict[str, Any]:
    """Add one ``report_review`` call to the lead's run; answers ``{"status", "note", "totals"}``.

    ``status`` is ``stored`` (kept for the run's line), ``amended`` (the run's line was written
    less than :data:`AMEND_WINDOW_S` ago, so a new line for the same ``run_key`` carries the
    review; the reader keeps the last line per session), ``dropped`` (that line is older, or has
    none), ``repeat`` (*call_id* was already counted in this run, so nothing is added) or
    ``off`` (the owner's insights are off, so nothing is kept).

    *lead_now* is the lead's current ``(status, updated_at)``. When it shows the lead has left
    the end the line was written for (a new run began), the report is kept for that new run
    instead of amending the old line.

    *call_id* names the review round. Added 2026-10-25 after a lead called the tool twice for
    one round and its run read 14 reviewers instead of 9
    (https://github.com/analyticendeavors/omnigent-ae/issues/328).
    """
    if not insights_on:
        return {
            "status": "off",
            "note": "preset insights are off for this user; nothing was recorded",
            "totals": None,
        }
    stamp = time.time() if now is None else now
    call_id = (call_id or "").strip() or None  # "final " and "final" are one round
    with state.locked() as data:
        ended = data["ended"].get(lead_id)
        if (
            isinstance(ended, dict)
            and lead_id not in data["live"]
            and _still_at_end(ended, lead_now)
        ):
            line = ended.get("line")
            seen = _call_ids(ended)
            if call_id is not None and call_id in seen:
                old = line.get("review") if isinstance(line, dict) else None
                return {
                    "status": "repeat",
                    "note": f"review round {call_id!r} was already counted; nothing was added",
                    "totals": {k: old.get(k) for k in _REVIEW_KEYS}
                    if isinstance(old, dict)
                    else None,
                }
            if not isinstance(line, dict) or stamp - (_number(ended.get("at")) or 0) >= (
                AMEND_WINDOW_S
            ):
                return {
                    "status": "dropped",
                    "note": "this run's line was written more than 30 minutes ago (or had "
                    "nothing to amend); the report was not recorded",
                    "totals": None,
                }
            old = line.get("review") if isinstance(line.get("review"), dict) else {}
            review = {
                **old,
                "reported": True,
                "reviewers_run": _sum(old.get("reviewers_run"), reviewers_run),
                "findings_returned": _sum(old.get("findings_returned"), findings_returned),
                "findings_fixed": _sum(old.get("findings_fixed"), findings_fixed),
            }
            amended = line | {"review": review, "rev": _sum(line.get("rev"), 1)}
            if not append(amended, now=stamp):
                return {"status": "dropped", "note": "the log was not written", "totals": None}
            ended["line"] = amended
            ended["call_ids"] = _with_call_id(seen, call_id)
            totals = {k: review[k] for k in _REVIEW_KEYS}
            return {"status": "amended", "note": "the run's line was amended", "totals": totals}
        report = data["reports"].get(lead_id)
        if not isinstance(report, dict):
            report = {"reviewers_run": 0, "findings_returned": 0, "findings_fixed": 0, "calls": 0}
        seen = _call_ids(report)
        if call_id is not None and call_id in seen:
            return {
                "status": "repeat",
                "note": f"review round {call_id!r} was already counted; nothing was added",
                "totals": {k: v for k, v in report.items() if k not in ("at", "call_ids")},
            }
        report["call_ids"] = _with_call_id(seen, call_id)
        report["reviewers_run"] += reviewers_run
        report["findings_returned"] += findings_returned
        report["findings_fixed"] += findings_fixed
        report["calls"] += 1
        report["at"] = stamp
        data["reports"][lead_id] = report
        totals = {k: v for k, v in report.items() if k not in ("at", "call_ids")}
        return {"status": "stored", "note": "kept for the run's line", "totals": totals}


# ---------------------------------------------------------------------------
# The sweep


class Sweeper:
    """One sweep per :meth:`sweep_sync`; :meth:`run` repeats it every :data:`SWEEP_INTERVAL_S`."""

    def __init__(
        self,
        *,
        store: Any,
        state: StateFile,
        list_rows: Callable[[], list[Any]] | None = None,
        owner: Callable[[str], str | None] = lambda _id: None,
        insights_on: Callable[[str | None], bool] = insights_enabled,
        resolve: Callable[[Any], tuple[str | None, str | None]] | None = None,
        revision: Callable[[str], int | None] = _revision,
        agent_name: Callable[[Any], str | None] | None = None,
        write: Callable[..., bool] = append,
        model_changes: Callable[[set[str], float], list[dict[str, Any]]] | None = None,
        clock: Callable[[], float] = time.time,
        lock: auto_archive.SweepLock | None = None,
    ) -> None:
        self._store = store
        self._state = state
        self._list_rows = list_rows or (lambda: auto_archive.list_all_top_level(store))
        self._owner = owner
        self._insights_on = insights_on
        self._resolve = resolve or (lambda conv: resolve_preset(store, conv))
        self._revision = revision
        self._agent_name = agent_name or self._default_agent_name
        self._write = write
        self._model_changes = model_changes or read_model_changes
        self._clock = clock
        self._lock = lock

    # -- reading the store

    def _default_agent_name(self, conv: Any) -> str | None:
        agent_id = getattr(conv, "agent_id", None)
        if not agent_id:
            return None
        if agent_id == role_prefs.CODEX_AGENT_ID:
            return role_prefs.CODEX_AGENT_NAME
        try:
            from omnigent.runtime import get_agent_store

            agent = get_agent_store().get(agent_id)
        except Exception:  # noqa: BLE001
            return None
        return agent.name if agent is not None else None

    def _children(self, lead_id: str) -> list[Any]:
        """Rows whose ``parent_conversation_id`` is *lead_id* (subagents, started sessions)."""
        rows: list[Any] = []
        after: str | None = None
        for _ in range(-(-MAX_NATIVE_ROWS // 100)):
            page = self._store.list_conversations(
                limit=100,
                after=after,
                kind=None,
                parent_conversation_id=lead_id,
                include_archived=True,
                order="desc",
                sort_by="created_at",
                accessible_by=None,
            )
            rows.extend(page.data)
            if not page.has_more or not page.last_id or len(rows) >= MAX_NATIVE_ROWS:
                break
            after = page.last_id
        return rows[:MAX_NATIVE_ROWS]

    def _native_calls(self, lead_id: str, wanted: set[str]) -> dict[str, dict[str, Any]]:
        """``{tool_use_id: arguments}`` of the lead's Agent or Task calls, newest first."""
        found: dict[str, dict[str, Any]] = {}
        after: str | None = None
        for _ in range(MAX_ITEM_PAGES):
            try:
                page = self._store.list_items(
                    lead_id, limit=100, after=after, order="desc", type="function_call"
                )
            except Exception:  # noqa: BLE001 (model and effort stay null)
                _logger.warning("preset runs: could not read %s's calls", lead_id, exc_info=True)
                break
            for item in page.data:
                data = getattr(item, "data", None)
                call_id = getattr(data, "call_id", None)
                if call_id not in wanted or getattr(data, "name", None) not in _AGENT_TOOLS:
                    continue
                try:
                    args = json.loads(getattr(data, "arguments", "") or "{}")
                except ValueError:
                    args = {}
                found[call_id] = args if isinstance(args, dict) else {}
            if found.keys() >= wanted or not page.has_more or not page.last_id:
                break
            after = page.last_id
        else:
            _logger.warning(
                "preset runs: read %d item pages of %s and stopped; some native subagents have "
                "no model or effort",
                MAX_ITEM_PAGES,
                lead_id,
            )
        return found

    # -- building a line

    def _agent_row(self, conv: Any, hung: Mapping[str, str], now: float) -> dict[str, Any]:
        codex = self._agent_name(conv) == role_prefs.CODEX_AGENT_NAME
        usd = _number(_usage(conv).get("total_cost_usd"))
        return {
            "session": conv.id,
            "kind": "codex" if codex else "session",
            "role": labels.label(conv.labels, labels.ROLE) or UNKNOWN,
            "model": _model_of(conv),
            "effort": getattr(conv, "reasoning_effort", None) or None,
            "seconds": _seconds(conv, now),
            "cost_usd": usd,
            "hung": conv.id in hung,
        }

    def _native_rows(
        self, lead_id: str, rows: list[Any], hung: Mapping[str, str], now: float
    ) -> list[dict[str, Any]]:
        calls = (
            self._native_calls(
                lead_id, {r.labels[_NATIVE_CALL_ID] for r in rows if r.labels.get(_NATIVE_CALL_ID)}
            )
            if rows
            else {}
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            args = calls.get(row.labels.get(_NATIVE_CALL_ID, ""), {})
            model = args.get("model")
            effort = args.get("effort") or args.get("reasoning_effort")
            out.append(
                {
                    "session": row.id,
                    "kind": "subagent",
                    "role": native_role(row.labels.get(_NATIVE_DESCRIPTION)),
                    "model": model if isinstance(model, str) and model else None,
                    "effort": effort if isinstance(effort, str) and effort else None,
                    "seconds": _seconds(row, now),
                    "cost_usd": None,
                    "hung": row.id in hung,
                }
            )
        return out

    def _family(
        self, lead: Any, remembered: list[str], orch_by_parent: Mapping[str, list[Any]]
    ) -> tuple[list[Any], list[Any]]:
        """``(session descendants, native subagent rows)`` of the lead, found now.

        Breadth first over ``ae.orch`` and ``parent_conversation_id`` to depth :data:`MAX_DEPTH`,
        at most :data:`MAX_FAMILY` rows, so a builder a reviewer started is counted.
        """
        sessions: dict[str, Any] = {}
        natives: dict[str, Any] = {}
        queue: list[tuple[str, int]] = [(lead.id, 0)]
        for kid in remembered:
            if kid not in sessions:
                conv = self._store.get_conversation(kid)
                if conv is not None:
                    sessions[kid] = conv
                    queue.append((kid, 1))
        visited: set[str] = {lead.id}
        capped = False
        while queue:
            node, depth = queue.pop(0)
            if depth >= MAX_DEPTH:
                continue
            if node in visited and node != lead.id:
                continue
            visited.add(node)
            try:
                found = [*orch_by_parent.get(node, []), *self._children(node)]
            except Exception:  # noqa: BLE001 (a partial family beats no line)
                _logger.warning("preset runs: could not read %s's children", node, exc_info=True)
                found = list(orch_by_parent.get(node, []))
            for row in found:
                if row.id == lead.id or row.id in sessions or row.id in natives:
                    continue
                if len(sessions) + len(natives) >= MAX_FAMILY:
                    capped = True
                    break
                native = (
                    getattr(row, "kind", None) == "sub_agent"
                    or _NATIVE_CALL_ID in row.labels
                    or _NATIVE_DESCRIPTION in row.labels
                )
                if native:
                    natives[row.id] = row
                else:
                    sessions[row.id] = row
                    queue.append((row.id, depth + 1))
            if capped:
                break
        if capped and lead.id not in _CAP_WARNED:
            _CAP_WARNED.add(lead.id)  # once per lead per process, not on every retry
            _logger.warning(
                "preset runs: %s's family passed %d sessions; the rest are left out",
                lead.id,
                MAX_FAMILY,
            )
        return list(sessions.values()), list(natives.values())

    def _snapshot(self, lead: Any) -> dict[str, Any] | None:
        """The preset the run starts under: id, fingerprint and the list revision."""
        try:
            preset, finger = self._resolve(lead)
            user = self._owner(lead.id)
            return {
                "preset": preset,
                "fingerprint": finger,
                "revision": self._revision(user or "local"),
            }
        except Exception:  # noqa: BLE001
            _logger.warning("preset runs: no preset snapshot for %s", lead.id, exc_info=True)
            return None

    @staticmethod
    def _review_block(report: Mapping[str, Any], reviewers: int) -> dict[str, Any]:
        return {
            "reported": bool(report),
            "reviewers_run": report.get("reviewers_run"),
            "findings_returned": report.get("findings_returned"),
            "findings_fixed": report.get("findings_fixed"),
            "reviewer_sessions": reviewers,
        }

    def build_line(
        self,
        lead: Any,
        outcome: str,
        *,
        remembered: list[str],
        orch_by_parent: Mapping[str, list[Any]],
        state: Mapping[str, Any],
        snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The run line for *lead* (everything but ``at``).

        The preset, fingerprint and revision come from *snapshot* (taken when the run was first
        seen live); with none they are resolved now, which is the preset at the run's end.
        """
        now = self._clock()
        user = self._owner(lead.id)
        hung: Mapping[str, str] = state.get("hung") or {}
        sessions, natives = self._family(lead, remembered, orch_by_parent)
        started = _number(labels.label(lead.labels, labels.STARTED)) or _time(lead, "created_at")
        end = min(_time(lead, "updated_at"), now)
        cost: dict[str, dict[str, Any]] = {}
        add_cost(cost, _usage(lead))
        for child in sessions:
            add_cost(cost, _usage(child))
        total = sum(v.get("usd", 0) for v in cost.values())
        if snapshot:
            preset, finger = snapshot.get("preset"), snapshot.get("fingerprint")
            revision = snapshot.get("revision")
        else:
            preset, finger = self._resolve(lead)
            revision = self._revision(user or "local")
        report = (state.get("reports") or {}).get(lead.id) or {}
        reviewers = sum(1 for c in sessions if labels.label(c.labels, labels.ROLE) == "reviewer")
        family_ids = {lead.id} | {c.id for c in sessions}
        return {
            "session": lead.id,
            "user": user,
            "title": _title(lead),
            "preset": preset,
            "fingerprint": finger,
            "revision": revision,
            "lead": {
                "model": _model_of(lead),
                "effort": getattr(lead, "reasoning_effort", None) or None,
            },
            "wall_s": int(max(end - started, 0)),
            "active_s": None,
            "cost": {"total_usd": total if cost else None, "by_model": cost},
            "agents": [self._agent_row(c, hung, now) for c in sessions]
            + self._native_rows(lead.id, natives, hung, now),
            "model_changes": self._model_changes(family_ids, started),
            "review": self._review_block(report, reviewers),
            "outcome": outcome,
            "files_touched": None,
        }

    # -- the sweep

    def sweep_sync(self) -> list[str]:
        """Write the lines that are due; returns the lead ids written."""
        if self._lock is not None and not self._lock.acquire():
            return []
        now = self._clock()
        rows = self._list_rows()
        seen = {r.id: r for r in rows}
        orch_by_parent: dict[str, list[Any]] = {}
        for row in rows:
            parent = labels.orch_parent(row.labels)
            if parent is not None:
                orch_by_parent.setdefault(parent, []).append(row)
        on_cache: dict[str | None, bool] = {}

        def owner_on(lead_id: str) -> bool:
            user = self._owner(lead_id)
            if user not in on_cache:
                on_cache[user] = bool(self._insights_on(user))
            return on_cache[user]

        written: list[str] = []
        # Store reads happen outside the state lock; each lead takes it briefly to read and update.
        for lead_id, row in seen.items():
            if labels.orch_parent(row.labels) is not None:
                continue  # a family child, not a lead
            try:
                if not owner_on(lead_id):
                    continue  # insights off: no tracking, no work
                if self._visit(row, orch_by_parent, now) == WROTE:
                    written.append(lead_id)
            except Exception:
                _logger.exception("preset runs: could not process %s", lead_id)
        with self._state.locked() as data:
            tracked = [k for k in (*data["live"], *data["ended"]) if k not in seen]
        for lead_id in tracked:
            try:
                if self._gone(lead_id, orch_by_parent, owner_on) == WROTE:
                    written.append(lead_id)
            except Exception:
                _logger.exception("preset runs: could not process %s", lead_id)
        with self._state.locked() as data:
            # A hang mark outlives its run only until the run's line is written.
            data["hung"] = {c: owner for c, owner in data["hung"].items() if owner in data["live"]}
            for entry in data["ended"].values():  # past the amend window the line is not needed
                if isinstance(entry, dict) and now - (_number(entry.get("at")) or 0) >= (
                    AMEND_WINDOW_S
                ):
                    entry.pop("line", None)
            data["reports"] = {
                k: v
                for k, v in data["reports"].items()
                if now - ((isinstance(v, dict) and _number(v.get("at"))) or now) < REPORT_MAX_AGE_S
            }
        return written

    def _visit(self, row: Any, orch_by_parent: Mapping[str, list[Any]], now: float) -> str:
        lead_id = row.id
        status = labels.status_of(row.labels)
        kids = self._tracked_kids(lead_id, orch_by_parent)
        if status not in _END_STATUSES:
            # Working, blocked, parked or unset: remember it as a run in progress.
            with self._state.locked() as data:
                data["ended"].pop(lead_id, None)
                has_live = lead_id in data["live"]
            snap = None if has_live else self._snapshot(row)
            try:
                rows = self._children(lead_id)
                children = [c for c in rows if not getattr(c, "archived", False)]
            except Exception:  # noqa: BLE001
                _logger.warning("preset runs: no children for %s", lead_id, exc_info=True)
                children = []
            with self._state.locked() as data:
                live = data["live"].get(lead_id)
                if live is None:
                    live = data["live"][lead_id] = {"kids": [], "snap": snap}
                kid_ids = list(live.get("kids") or [])
                for kid in kids:
                    if kid.id not in kid_ids and len(kid_ids) < MAX_KIDS:
                        kid_ids.append(kid.id)
                live["kids"] = kid_ids
                self._mark_hung(data, lead_id, [*kids, *children], now)
            return SKIPPED
        with self._state.locked() as data:
            if data["ended"].get(lead_id) is not None:
                return SKIPPED  # this end is already written
            first_sight = lead_id not in data["live"]
            updated = _time(row, "updated_at")
            if first_sight and now - updated > FIRST_SEEN_MAX_AGE_S:
                # History from before the log (or a lost state file); never backfilled.
                data["ended"][lead_id] = self._ended_entry(lead_id, str(status), updated, now)
                data["reports"].pop(lead_id, None)  # a stale report must not reach the next run
                return SKIPPED
        return self._emit(row, str(status), orch_by_parent, now)

    def _tracked_kids(self, lead_id: str, orch_by_parent: Mapping[str, list[Any]]) -> list[Any]:
        """The lead's descendants through ``ae.orch`` alone (no store reads), for live tracking."""
        out: list[Any] = []
        queue = [(lead_id, 0)]
        seen = {lead_id}
        while queue:
            node, depth = queue.pop(0)
            if depth >= MAX_DEPTH:
                continue
            for kid in orch_by_parent.get(node, []):
                if kid.id not in seen and len(out) < MAX_FAMILY:
                    seen.add(kid.id)
                    out.append(kid)
                    queue.append((kid.id, depth + 1))
        return out

    @staticmethod
    def _ended_entry(lead_id: str, status: str, updated: float, now: float) -> dict[str, Any]:
        return {
            "status": status,
            "updated_at": updated,
            "run_key": f"{lead_id}:{int(updated)}:{status}",
            "at": now,
        }

    def _gone(
        self,
        lead_id: str,
        orch_by_parent: Mapping[str, list[Any]],
        owner_on: Callable[[str], bool],
    ) -> str:
        """A tracked lead left the list: archived (maybe a line due), or deleted."""
        conv = self._store.get_conversation(lead_id)
        if conv is None:
            with self._state.locked() as data:
                data["live"].pop(lead_id, None)
                data["ended"].pop(lead_id, None)
                data["reports"].pop(lead_id, None)
            return SKIPPED
        if not getattr(conv, "archived", False):
            return SKIPPED  # listed rows lag; look again next sweep
        with self._state.locked() as data:
            if lead_id in data["ended"]:
                data["ended"].pop(lead_id, None)  # its line is written
                return SKIPPED
        if not owner_on(lead_id):
            with self._state.locked() as data:
                data["live"].pop(lead_id, None)
            return SKIPPED
        return self._emit(conv, "archived", orch_by_parent, self._clock())

    def _emit(
        self,
        lead: Any,
        outcome: str,
        orch_by_parent: Mapping[str, list[Any]],
        now: float,
    ) -> str:
        """Write the lead's end line once. The end is marked written only after the line is
        durably in the log (or was already there); any failure leaves it for the next sweep.

        The store reads (family walk, items) run outside the state lock; the lock is taken to
        read the lead's state, and again, with the end re-checked, to write and mark it."""
        with self._state.locked() as data:
            if lead.id in data["ended"]:
                return SKIPPED  # a concurrent call already wrote this end
            live = copy.deepcopy(data["live"].get(lead.id) or {})
            view = {"hung": dict(data["hung"]), "reports": copy.deepcopy(data["reports"])}
        entry = self._ended_entry(lead.id, outcome, _time(lead, "updated_at"), now)
        run_key = entry["run_key"]
        line = self.build_line(
            lead,
            outcome,
            remembered=list(live.get("kids") or []),
            orch_by_parent=orch_by_parent,
            state=view,
            snapshot=live.get("snap"),
        )
        line["run_key"] = run_key
        line["rev"] = 0
        empty = self._is_empty(lead, line)
        is_reviewer = self._agent_name(lead) == REVIEWER_AGENT
        with self._state.locked() as data:
            if lead.id in data["ended"]:
                return SKIPPED  # written while the store was being read
            # A report that arrived while the line was built still goes on it.
            report = data["reports"].get(lead.id) or {}
            line["review"] = self._review_block(report, line["review"]["reviewer_sessions"])
            result = WROTE
            if logged_rev(run_key) is not None:
                result = SKIPPED  # a crash between the append and the state save
            elif empty or is_reviewer:
                result = SKIPPED
            elif not self._write(line, now=now):
                _logger.warning("preset runs: line for %s not written; will retry", lead.id)
                return RETRY
            if result == WROTE:
                entry["line"] = line
            if _call_ids(report):  # a repeat after the line is written is still a repeat
                entry["call_ids"] = _call_ids(report)
            data["ended"][lead.id] = entry
            data["live"].pop(lead.id, None)
            data["reports"].pop(lead.id, None)
            for child in [c for c, owner in data["hung"].items() if owner == lead.id]:
                del data["hung"][child]
            self._state.checkpoint(data)
            return result

    def _is_empty(self, lead: Any, line: Mapping[str, Any]) -> bool:
        """An opened-then-archived empty chat: no cost, no agents and no items."""
        if line["agents"] or (line["cost"]["total_usd"] or 0) > 0:
            return False
        try:
            page = self._store.list_items(lead.id, limit=1)
            return not page.data
        except Exception:  # noqa: BLE001 (cannot tell, so log it)
            return False

    @staticmethod
    def _mark_hung(data: dict[str, Any], lead_id: str, candidates: list[Any], now: float) -> None:
        """Mark running children that have not moved for :data:`HANG_AFTER_S` (sticky)."""
        for child in candidates:
            running = getattr(child, "live_status", None) == "running"
            if running and now - _time(child, "updated_at") >= HANG_AFTER_S:
                data["hung"].setdefault(child.id, lead_id)

    async def sweep(self) -> list[str]:
        return await asyncio.to_thread(self.sweep_sync)

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self.sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception("preset runs: sweep failed")
                await asyncio.sleep(SWEEP_INTERVAL_S)
        finally:
            if self._lock is not None:
                self._lock.release()


def read_model_changes(family: set[str], since: float) -> list[dict[str, Any]]:
    """Model log lines for the family since *since*, oldest first (both log files, bounded)."""
    path = model_log.log_path()
    if path is None:
        return []
    out: list[dict[str, Any]] = []
    for file in (path.with_name(path.name + ".1"), path):
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("session") not in family:
                continue
            at = _number(entry.get("at"))
            if at is None or at < since:
                continue
            out.append(
                {
                    key: entry.get(key)
                    for key in ("session", "from", "to", "kind", "approved", "at")
                }
            )
    return out


# ---------------------------------------------------------------------------
# Wiring


def start() -> asyncio.Task[None] | None:
    """Start the sweep on the running loop, or ``None`` when it is off or cannot start."""
    if not enabled():
        _logger.info("preset runs off (%s=0)", ENABLED_ENV)
        return None
    if log_path() is None:
        _logger.info("preset runs: no log file (no /data, %s unset); the sweep is off", FILE_ENV)
        return None
    try:
        from omnigent.runtime import get_conversation_store

        from ae_omni_policies import merge_watch

        identity = merge_watch.OwnerIdentity()
        state = shared_state()
        lock_path = (
            state.path.with_name(state.path.name + ".lock") if state.path else _LOCK_FALLBACK
        )
        sweeper = Sweeper(
            store=get_conversation_store(),
            state=state,
            owner=identity.owner,
            lock=auto_archive.SweepLock(lock_path),
        )
        return asyncio.get_running_loop().create_task(sweeper.run(), name="ae-preset-runs")
    except Exception:
        # Must never stop the server starting; the sweep stays off until a restart.
        _logger.exception("preset runs: could not start; the sweep is off until a restart")
        return None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Run the sweep for the life of the server."""
    task = start()
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


#: No routes; included for its lifespan (see the module docstring).
lifespan_router = APIRouter(lifespan=lifespan)

__all__ = [
    "StateFile",
    "Sweeper",
    "append",
    "enabled",
    "fingerprint",
    "lifespan_router",
    "log_path",
    "native_role",
    "record_review",
    "shared_state",
    "state_path",
]
