"""What each session family spent, for the Admin Center's Family cost card (2026-10-29).

A family is a lead plus every session it started (``ae.orch`` and parent links, the walk
:class:`preset_runs.Sweeper` uses) and the native subagents under any of them. The owner wants
to see a runaway family at a glance, so ``GET /v1/ae/family-cost?days=7|30`` ranks the window's
spend per family.

**Where the numbers come from.** Every session already keeps ``session_usage`` (cost and token
totals, updated each turn for Claude and Codex). Checked against the Claude transcripts on
2026-10-29 for four families (two with five restarts): token totals matched to within 1.4%, and
the lead's total already includes its native subagents (their rows hold no usage), so a
subagent row adds nothing for a Claude lead. A Codex session's native subagents keep their own
usage and the parent's *cost* already includes them but its *tokens* do not, so a native row
adds tokens and never cost. ``session_usage`` has no cache-write figure: ``input`` here is
the input total, which includes cache writes.

**A window is a subtraction.** A session's total only grows, except when a restarted status line
resets it; the sweep then adds the sample before the fall to a per-session offset, so what a
session has spent is its offset plus its current total. A window's spend is that figure now minus
the figure at the window's start. A sweep every :data:`SWEEP_INTERVAL_S` stores, for each
Pacific-time day (kept :data:`KEEP_DAYS` days, in :data:`FILE_ENV`), the last sample taken before
that day began; a session started inside the window counts from zero, an older one from its entry
in the snapshot of the window's first day (or the oldest snapshot there is, which the answer
reports as ``history_since``). A session that was deleted (not archived) drops out of its
family's totals.

**Never blocks.** The route answers the last sweep's families; before the first sweep has
finished, and with the state file empty, it answers ``{"state": "warming"}`` (the pattern of
:mod:`ae_omni_policies.preset_stats`). Only one server process sweeps (a non-blocking ``flock``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from ae_omni_policies import auto_archive, labels

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI

_logger = logging.getLogger(__name__)
_CAP_WARNED: set[str] = set()

ENABLED_ENV = "AE_FAMILY_COST"
FILE_ENV = "AE_FAMILY_COST_FILE"
DEFAULT_FILE = "/data/ae-family-cost.json"
_LOCK_FALLBACK = Path("/tmp") / "ae-family-cost.lock"
STATE_VERSION = 1
WINDOWS = (7, 30)
SWEEP_INTERVAL_S = 900.0
KEEP_DAYS = 35
#: The answer stays under this many characters; the rest of the ranking is cut.
MAX_CHARS = 30_000
MAX_DEPTH = 5
MAX_FAMILY = 200
#: "Today" and every day boundary are Pacific time, as the box's scheduled tasks are.
TIME_ZONE = ZoneInfo("America/Los_Angeles")
_LIST_PAGE = 100
_LIST_MAX_PAGES = 200
#: Defaults for "Runaway family" (spend today across the family): the unit's own number.
DEFAULT_RUNAWAY_TOKENS = 5_000_000
DEFAULT_RUNAWAY_USD = 25.0

RESUME_COUNT = "ae.claude_native.resume_count"
LAST_CONTEXT = "omnigent.last_context_tokens"
WRAPPER = "omnigent.wrapper"
CODEX_WRAPPER = "codex-native-ui"

#: The roles a row's chips name; the card shows the lead as "lead incl. subagents".
ROLES = ("lead", "builder", "reviewer", "codex", "other")

# [cost_usd, input, output, cache_read] per session.
Totals = dict[str, list[float]]


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return (env.get(ENABLED_ENV) or "1").strip() != "0"


def state_path(environ: Mapping[str, str] | None = None) -> Path | None:
    env = environ if environ is not None else os.environ
    if FILE_ENV in env:
        raw = (env[FILE_ENV] or "").strip()
        return Path(raw) if raw else None
    return Path(DEFAULT_FILE) if Path(DEFAULT_FILE).parent.is_dir() else None


def _num(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if value == value and value >= 0 else 0.0


def _day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, TIME_ZONE).strftime("%Y-%m-%d")


def _day_start(day: str) -> float:
    """Local midnight of *day* (Pacific), as epoch seconds."""
    return datetime.combine(date.fromisoformat(day), datetime.min.time(), TIME_ZONE).timestamp()


def _days_before(day: str, count: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=count)).isoformat()


def usage_of(row: Any) -> list[float]:
    """``[cost, input, output, cache_read]`` from a session row's ``session_usage``."""
    usage = getattr(row, "session_usage", None)
    if not isinstance(usage, dict):
        return [0.0, 0.0, 0.0, 0.0]
    return [
        _num(usage.get("total_cost_usd")),
        _num(usage.get("input_tokens")),
        _num(usage.get("output_tokens")),
        _num(usage.get("cache_read_input_tokens")),
    ]


def _int(value: Any) -> int:
    try:
        return max(0, int(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def role_of(row: Any, *, lead: bool) -> str:
    """The chip a session's spend goes under: lead, builder, reviewer, codex or other."""
    if lead:
        return "lead"
    row_labels = getattr(row, "labels", None) or {}
    if row_labels.get(WRAPPER) == CODEX_WRAPPER:
        return "codex"
    role = labels.label(row_labels, labels.ROLE)
    if role in ("builder", "reviewer"):
        return role
    # An earlier lead of a handoff chain is still a lead.
    if role == "lead" or labels.label(row_labels, labels.HANDOFF_TO):
        return "lead"
    return "other"


# ---------------------------------------------------------------------------
# The state file


class StateFile:
    """The sweep's state: day snapshots, rollover offsets, the last sample and the families.

    ``{"days": {day: {session: totals}}, "offsets": {session: totals}, "eff": {session: totals},
    "last": {session: raw totals}, "at": t, "families": {"7": [...], "30": [...]}}``.
    The file is read again when its modification time changes, so a process that does not hold
    the sweep lock still serves what the sweeping one wrote. A file that cannot be read as this
    version is renamed ``<name>.bad-<timestamp>`` (and logged), never overwritten.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._mtime: int | None = None

    def _stamp(self) -> int | None:
        if self.path is None:
            return None
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def load(self) -> dict[str, Any]:
        with self._lock:
            stamp = self._stamp()
            if self._data is None or (self.path is not None and stamp != self._mtime):
                self._data = self._read()
                self._mtime = self._stamp()
            return self._data

    def _quarantine(self, why: str) -> None:
        if self.path is None:
            return
        target = self.path.with_name(f"{self.path.name}.bad-{int(time.time())}")
        try:
            os.replace(self.path, target)
            _logger.warning("family cost: %s is %s; kept as %s", self.path, why, target)
        except OSError as exc:
            _logger.warning("family cost: %s is %s, not set aside: %s", self.path, why, exc)

    def _read(self) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "days": {},
            "offsets": {},
            "eff": {},
            "last": {},
            "at": None,
            "families": {},
        }
        if self.path is None:
            return empty
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return empty
        try:
            data = json.loads(text)
        except ValueError:
            self._quarantine("not valid JSON")
            return empty
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            self._quarantine("not this version's state")
            return empty

        def table(key: str) -> dict[str, Any]:
            value = data.get(key)
            return value if isinstance(value, dict) else {}

        return {
            "days": table("days"),
            "offsets": table("offsets"),
            "eff": table("eff"),
            "last": table("last"),
            "at": data.get("at") if isinstance(data.get("at"), (int, float)) else None,
            "families": table("families"),
        }

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._data = data
            if self.path is None:
                return
            tmp_name = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    "w",
                    dir=self.path.parent,
                    prefix=self.path.name + ".",
                    delete=False,
                    encoding="utf-8",
                ) as tmp:
                    tmp_name = tmp.name
                    json.dump({"version": STATE_VERSION, **data}, tmp, separators=(",", ":"))
                os.replace(tmp_name, self.path)
                self._mtime = self._stamp()
            except (OSError, TypeError, ValueError) as exc:
                _logger.warning("family cost not saved (%s): %s", self.path, exc)
                if tmp_name:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_name)


_shared: StateFile | None = None


def shared_state() -> StateFile:
    global _shared
    if _shared is None:
        _shared = StateFile(state_path())
    return _shared


def reset(replacement: StateFile | None = None) -> None:
    global _shared
    _shared = replacement


# ---------------------------------------------------------------------------
# The sweep


def _list_rows(store: Any, *, kind: str) -> list[Any]:
    rows: list[Any] = []
    after: str | None = None
    for _ in range(_LIST_MAX_PAGES):
        page = store.list_conversations(
            limit=_LIST_PAGE,
            after=after,
            kind=kind,
            include_archived=True,
            order="desc",
            sort_by="created_at",
            accessible_by=None,
        )
        rows.extend(page.data)
        if not page.has_more or not page.last_id:
            return rows
        after = page.last_id
    raise RuntimeError(f"session list did not end after {_LIST_MAX_PAGES} pages")


def _baseline(
    days: dict[str, Any], today: str, window: int
) -> tuple[Mapping[str, Any], str | None, bool]:
    """``(snapshot, its day, partial)`` for the first day of a *window*-day window.

    The oldest snapshot on or after the window's first day; ``partial`` when that is a later
    day (the store is younger than the window), so older sessions undercount.
    """
    first = _days_before(today, window - 1)
    for day in sorted(days):
        if day >= first and isinstance(days[day], dict):
            return days[day], day, day > first
    return {}, None, True


def _rounded1(values: list[float]) -> list[float]:
    return [round(v, 6) for v in values]


def _rounded(totals: Mapping[str, Any]) -> dict[str, list[float]]:
    return {sid: _rounded1(list(u)) for sid, u in totals.items()}


def window_spend(
    now_totals: Totals,
    base: Mapping[str, Any],
    created: Mapping[str, float],
    start: float,
) -> Totals:
    """Each session's spend since *start*: now minus the snapshot, from zero if it began after."""
    out: Totals = {}
    for sid, cur in now_totals.items():
        prior = base.get(sid)
        zero = created.get(sid, 0.0) >= start or not isinstance(prior, list) or len(prior) != 4
        old = [0.0] * 4 if zero else [_num(v) for v in prior]
        spent = [max(0.0, c - o) for c, o in zip(cur, old, strict=True)]
        if any(spent):
            out[sid] = spent
    return out


class Sweeper:
    def __init__(
        self,
        *,
        store: Any,
        state: StateFile,
        owner: Callable[[str], str | None] = lambda _id: None,
        host_name: Callable[[Any], str] = lambda _host: "",
        list_top: Callable[[], list[Any]] | None = None,
        list_native: Callable[[], list[Any]] | None = None,
        clock: Callable[[], float] = time.time,
        lock: auto_archive.SweepLock | None = None,
    ) -> None:
        self._state = state
        self._owner = owner
        self._host_name = host_name
        self._list_top = list_top or (lambda: _list_rows(store, kind="default"))
        self._list_native = list_native or (lambda: _list_rows(store, kind="sub_agent"))
        self._clock = clock
        self._lock = lock

    def families(self, tops: list[Any], natives: list[Any]) -> dict[str, dict[str, Any]]:
        """``{root id: {row, members: {id: row}, natives: [row], holder: {native: member}}}``.

        Mirrors :meth:`preset_runs.Sweeper._family` from the other end: every session finds its
        root by walking up ``ae.handoff_to`` (an earlier lead sits under its successor), then
        ``ae.orch``, then ``parent_conversation_id``, so a default child linked only by its
        parent, a subagent of a subagent and a handoff chain all land in one family. A session
        whose parent is not loaded (deleted) is its own root. Past :data:`MAX_DEPTH` levels or
        :data:`MAX_FAMILY` sessions a family leaves the rest out (logged).
        """
        rows = {r.id: r for r in [*tops, *natives]}
        native_ids = {r.id for r in natives}

        def parent_of(row: Any) -> str | None:
            heir = labels.label(row.labels, labels.HANDOFF_TO)
            if heir and heir != row.id and heir in rows and heir not in native_ids:
                return heir
            for cand in (labels.orch_parent(row.labels), row.parent_conversation_id):
                if cand and cand != row.id and cand in rows:
                    return cand
            return None

        chains: dict[str, list[str]] = {}
        for rid in rows:
            chain: list[str] = []
            seen: set[str] = set()
            cur: str | None = rid
            while cur is not None and cur not in seen:
                seen.add(cur)
                chain.append(cur)
                cur = parent_of(rows[cur])
            if cur is not None:
                # A link cycle: every member, and every tail leading in, resolves to the cycle's
                # smallest id, so a session is never in two families.
                at = chain.index(cur)
                root = min(chain[at:])
                chain = [*chain[:at], cur, *([root] if cur != root else [])]
            chains[rid] = chain

        grouped: dict[str, list[tuple[int, float, str]]] = {}
        for rid, chain in chains.items():
            grouped.setdefault(chain[-1], []).append(
                (len(chain) - 1, _num(getattr(rows[rid], "created_at", 0)), rid)
            )
        out: dict[str, dict[str, Any]] = {}
        for root_id, found in grouped.items():
            fam: dict[str, Any] = {
                "row": rows[root_id],
                "members": {root_id: rows[root_id]},
                "natives": [],
                "holder": {},
            }
            kept = 1
            for depth, _created, rid in sorted(found):
                if rid == root_id:
                    continue
                if depth > MAX_DEPTH or kept >= MAX_FAMILY:
                    if root_id not in _CAP_WARNED:
                        _CAP_WARNED.add(root_id)
                        _logger.warning(
                            "family cost: %s's family passed %d levels or %d sessions; "
                            "the rest are left out",
                            root_id,
                            MAX_DEPTH,
                            MAX_FAMILY,
                        )
                    continue
                kept += 1
                if rid in native_ids:
                    fam["natives"].append(rows[rid])
                    fam["holder"][rid] = next(
                        (c for c in chains[rid][1:] if c not in native_ids), root_id
                    )
                else:
                    fam["members"][rid] = rows[rid]
            out[root_id] = fam
        return out

    def sweep_sync(self) -> bool:
        """Read every session, store the day's first snapshot and the families; ``False`` if idle.

        Idle means another process holds the sweep lock.
        """
        if self._lock is not None and not self._lock.acquire():
            return False
        now = self._clock()
        tops = self._list_top()
        natives = self._list_native()
        fams = self.families(tops, natives)

        data = self._state.load()
        raw: Totals = {}
        created: dict[str, float] = {}
        native_ids = {row.id for row in natives}
        for row in [*tops, *natives]:
            usage = usage_of(row)
            if row.id in native_ids:
                usage[0] = 0.0  # a native row adds tokens, never cost (see the module doc)
            # A session already seen stays in, even at zero, so a reset is noticed and its
            # offset is kept.
            if any(usage) or row.id in data["last"]:
                raw[row.id] = usage
                created[row.id] = _num(getattr(row, "created_at", 0))

        # A total that falls (a restarted status line) adds the sample before it to an offset,
        # so what a session has spent so far is its offset plus its current total.
        offsets: dict[str, list[float]] = {
            sid: [_num(v) for v in off]
            for sid, off in data["offsets"].items()
            if isinstance(off, list) and len(off) == 4
        }
        last = data["last"]
        totals: Totals = {}
        for sid, cur in raw.items():
            off = offsets.setdefault(sid, [0.0] * 4)
            before = last.get(sid)
            if isinstance(before, list) and len(before) == 4:
                for i in range(4):
                    if cur[i] < _num(before[i]):
                        off[i] += _num(before[i])
            totals[sid] = [o + c for o, c in zip(off, cur, strict=True)]
        # An offset is kept for a session in this sweep or the last, not for a deleted one.
        offsets = {
            sid: off for sid, off in offsets.items() if any(off) and (sid in raw or sid in last)
        }

        days: dict[str, Any] = dict(data["days"])
        today = _day(now)
        prior_at = data["at"]
        if prior_at is None:
            days[today] = _rounded(totals)
        elif _day(prior_at) != today and today not in days:
            # The day starts at the last sample before local midnight, so nothing spent in the
            # minutes after midnight is lost to the day before.
            days[today] = _rounded(data["eff"])
        for day in sorted(days)[: max(0, len(days) - KEEP_DAYS)]:
            del days[day]

        result: dict[str, Any] = {}
        for window in WINDOWS:
            result[str(window)] = self._window(fams, totals, created, days, today, window)
        self._state.save(
            {
                "days": days,
                "offsets": {sid: _rounded1(off) for sid, off in offsets.items()},
                "eff": _rounded(totals),
                "last": raw,
                "at": now,
                "families": result,
            }
        )
        return True

    def _window(
        self,
        fams: Mapping[str, dict[str, Any]],
        totals: Totals,
        created: Mapping[str, float],
        days: dict[str, Any],
        today: str,
        window: int,
    ) -> dict[str, Any]:
        base, base_day, partial = _baseline(days, today, window)
        start = _day_start(_days_before(today, window - 1))
        spent = window_spend(totals, base, created, start)
        spent_today = window_spend(totals, days.get(today, {}), created, _day_start(today))
        families: list[dict[str, Any]] = []
        for lead_id, fam in fams.items():
            ids = [*fam["members"], *(n.id for n in fam["natives"])]
            rows = self._family_row(lead_id, fam, spent, spent_today, ids)
            if rows is not None:
                families.append(rows)
        families.sort(key=lambda f: (-f["tokens"]["total"], f["lead_id"]))
        return {
            "history_since": base_day,
            "partial": partial,
            "families": families,
        }

    def _family_row(
        self,
        lead_id: str,
        fam: Mapping[str, Any],
        spent: Totals,
        spent_today: Totals,
        ids: list[str],
    ) -> dict[str, Any] | None:
        lead = fam["row"]
        sums = [0.0] * 4
        today = [0.0] * 4
        roles: dict[str, dict[str, float]] = {}
        parent_role = {
            sid: role_of(row, lead=sid == lead_id) for sid, row in fam["members"].items()
        }
        owner_of = {nid: parent_role.get(held, "other") for nid, held in fam["holder"].items()}
        sessions = 0
        for sid in ids:
            use = spent.get(sid)
            if sid in spent_today:
                today = [a + b for a, b in zip(today, spent_today[sid], strict=True)]
            if use is None:
                continue
            sums = [a + b for a, b in zip(sums, use, strict=True)]
            role = parent_role.get(sid) or owner_of.get(sid, "other")
            bucket = roles.setdefault(role, {"sessions": 0, "cost_usd": 0.0, "tokens_total": 0.0})
            bucket["cost_usd"] += use[0]
            bucket["tokens_total"] += use[1] + use[2] + use[3]
            if sid in parent_role:
                bucket["sessions"] += 1
                sessions += 1
        total = sums[1] + sums[2] + sums[3]
        if total <= 0 and sums[0] <= 0:
            return None
        restarts = sum(_int(row.labels.get(RESUME_COUNT)) for row in fam["members"].values())
        host = getattr(lead, "host_id", None)
        return {
            "lead_id": lead_id,
            "title": (getattr(lead, "title", None) or "")[:120],
            "host": self._host_name(host) if host else "",
            "owner": self._owner(lead_id) or "",
            "status": labels.label(lead.labels, labels.STATUS) or "",
            "cost_usd": round(sums[0], 4),
            "tokens": {
                "input": int(sums[1]),
                "output": int(sums[2]),
                "cache_read": int(sums[3]),
                "total": int(total),
            },
            "today": {"cost_usd": round(today[0], 4), "tokens_total": int(sum(today[1:]))},
            "by_role": sorted(
                (
                    {
                        "role": role,
                        "sessions": int(b["sessions"]),
                        "cost_usd": round(b["cost_usd"], 4),
                        "tokens_total": int(b["tokens_total"]),
                    }
                    for role, b in roles.items()
                ),
                key=lambda r: (-r["tokens_total"], r["role"]),
            ),
            "sessions": sessions,
            "lead_context_tokens": _int(lead.labels.get(LAST_CONTEXT)) or None,
            "restarts": restarts,
        }

    async def sweep(self) -> bool:
        return await asyncio.to_thread(self.sweep_sync)

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self.sweep()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception("family cost: sweep failed")
                await asyncio.sleep(SWEEP_INTERVAL_S)
        finally:
            if self._lock is not None:
                self._lock.release()


# ---------------------------------------------------------------------------
# The answer


def _echo(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    return {"settings": dict(settings)} if settings else {}


def build_answer(
    data: Mapping[str, Any],
    *,
    days: int,
    viewer: str | None,
    admin: bool,
    unit: str,
    runaway: float,
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The route's answer from the last sweep's *data*: scoped to the viewer, ranked by *unit*.

    *runaway* is the viewer's threshold in *unit* for a family's spend today (0 turns it off);
    *settings* is the viewer's three saved values, echoed so the card can show them.
    """
    entry = (data.get("families") or {}).get(str(days))
    at = data.get("at")
    if not isinstance(entry, dict) or not isinstance(at, (int, float)):
        return {"state": "warming", "days": days, "unit_default": unit, **_echo(settings)}
    dollars = unit == "dollars"
    rows: list[dict[str, Any]] = []
    for fam in entry.get("families", []):
        if not admin and (viewer is None or fam.get("owner") != viewer):
            continue
        spend_today = fam["today"]["cost_usd"] if dollars else fam["today"]["tokens_total"]
        rows.append({**fam, "flagged": runaway > 0 and spend_today >= runaway})
    rows.sort(key=lambda f: -(f["cost_usd"] if dollars else f["tokens"]["total"]))
    totals = {
        "families": len(rows),
        "cost_usd": round(sum(f["cost_usd"] for f in rows), 4),
        "tokens_total": sum(f["tokens"]["total"] for f in rows),
        "flagged": sum(1 for f in rows if f["flagged"]),
    }
    answer: dict[str, Any] = {
        "state": "ready",
        "computed_at": datetime.fromtimestamp(at, UTC).isoformat(),
        "days": days,
        "unit_default": unit,
        "history_since": entry.get("history_since"),
        "partial": bool(entry.get("partial")),
        "families": rows,
        "totals": totals,
        "truncated": False,
        **_echo(settings),
    }
    while rows and len(json.dumps(answer, separators=(",", ":"))) > MAX_CHARS:
        rows.pop()
        answer["truncated"] = True
    return answer


def view(
    days: int,
    *,
    viewer: str | None,
    admin: bool,
    unit: str,
    runaway: float,
    settings: Mapping[str, Any] | None = None,
    state: StateFile | None = None,
) -> dict[str, Any]:
    data = (state or shared_state()).load()
    return build_answer(
        data, days=days, viewer=viewer, admin=admin, unit=unit, runaway=runaway, settings=settings
    )


# ---------------------------------------------------------------------------
# Wiring


def start(app: FastAPI | None = None) -> asyncio.Task[None] | None:
    """Start the sweep on the running loop, or ``None`` when it is off or cannot start."""
    if not enabled():
        _logger.info("family cost off (%s=0)", ENABLED_ENV)
        return None
    try:
        from omnigent.runtime import get_conversation_store

        from ae_omni_policies import merge_watch

        identity = merge_watch.OwnerIdentity()
        names: dict[str, str] = {}

        def host_name(host_id: Any) -> str:
            key = str(host_id)
            if key not in names:
                host_store = getattr(getattr(app, "state", None), "host_store", None)
                host = host_store.get_host(key) if host_store is not None else None
                names[key] = getattr(host, "name", "") or ""
            return names[key]

        state = shared_state()
        lock_path = (
            state.path.with_name(state.path.name + ".lock") if state.path else _LOCK_FALLBACK
        )
        sweeper = Sweeper(
            store=get_conversation_store(),
            state=state,
            owner=identity.owner,
            host_name=host_name,
            lock=auto_archive.SweepLock(lock_path),
        )
        return asyncio.get_running_loop().create_task(sweeper.run(), name="ae-family-cost")
    except Exception:
        _logger.exception("family cost: could not start; the sweep is off until a restart")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    task = start(app)
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
    "build_answer",
    "lifespan_router",
    "view",
]
