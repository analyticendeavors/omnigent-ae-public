"""The scheduled preset reviewer: its task row and what it reads.

Added 2026-10-24 (preset insights PR 3). When a user switches preset insights on,
:func:`ensure_task` writes one scheduled task, ``Preset review check``, that fires the
``preset-reviewer`` agent every Monday at 07:00 in the server's zone. The agent calls
``preset_insights_read`` (:func:`build_read`) and ``submit_preset_suggestions``
(:mod:`ae_omni_policies.preset_suggestions`).

Why a weekly rule plus a 13-day skip, not ``INTERVAL=2``: the scheduler re-anchors every
rule at midnight of "now" on each arm and on each restart, and the box restarts on most
deploys, so a biweekly rule slips by a week every time. The weekly rule fires reliably and
:func:`build_read` answers ``due: false`` inside :data:`MIN_GAP_DAYS` of the last review, so
the skipped check costs a session that reads and stops.

A fired session is titled with the task name, and retention never prunes a run whose title
changed; a review that submits renames its session ``Preset review <Mon D>``, so it is kept
past ``keep_runs`` and the skipped checks are pruned beyond :data:`KEEP_RUNS`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx

from ae_omni_policies import (
    model_log,
    preset_log,
    preset_runs,
    preset_stats,
    preset_suggestions,
    role_prefs,
    schedules,
)

AGENT_NAME = preset_runs.REVIEWER_AGENT
TASK_NAME = "Preset review check"
RRULE = "FREQ=WEEKLY;BYDAY=MO;BYHOUR=7;BYMINUTE=0"
MIN_GAP_DAYS = 13
MAX_COST_USD = 2.0
KEEP_RUNS = 6
MAX_RUNS = 400
MAX_SUGGESTIONS = 5
MAX_REASON = 120
MAX_TITLE = 80
MAX_USER_TEXT = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
PROMPT = "Run the preset review now. Nobody is present: never ask, never edit a preset."
MANAGED = (*schedules.MANAGED_FIELDS, "model_override", "reasoning_effort")
_DAY = 86400.0


def reviewer_model(state: role_prefs.RolesState) -> tuple[str, str]:
    """The model and effort the reviewer runs on: the default preset's planner when it is a
    fixed Claude model, else Opus high (the harness is claude-sdk, which takes Claude only)."""
    default = next((p for p in state.presets if p.id == state.default_preset), None)
    planner = default.roles.planner if default is not None else None
    if isinstance(planner, role_prefs.FixedRole) and planner.model.startswith("claude-"):
        return planner.model, planner.effort
    return role_prefs.OPUS, "high"


def build_schedule(state: role_prefs.RolesState, zone: str) -> schedules.Schedule:
    """The task row as a :class:`schedules.Schedule` in *zone* (model fields go on the body)."""
    _ = state
    return schedules.Schedule(
        name=TASK_NAME,
        agent=AGENT_NAME,
        rrule=RRULE,
        prompt=PROMPT,
        timezone=zone,
        max_cost_usd=MAX_COST_USD,
    )


def _body(
    state: role_prefs.RolesState, zone: str, agent_id: str, *, create: bool
) -> dict[str, Any]:
    model, effort = reviewer_model(state)
    body = build_schedule(state, zone).body(agent_id)
    body["model_override"] = model
    body["reasoning_effort"] = effort
    if create:
        body["keep_runs"] = KEEP_RUNS
    return body


_locks: dict[tuple[int, str], asyncio.Lock] = {}


def _lock_for(owner: str) -> asyncio.Lock:
    """The lock that serializes task syncs for *owner* (one per event loop)."""
    return _locks.setdefault((id(asyncio.get_running_loop()), owner), asyncio.Lock())


async def find_tasks(http: httpx.AsyncClient, owner: str = "local") -> list[dict[str, Any]]:
    """Every ``Preset review check`` row *owner* holds, oldest first.

    The server's list is already scoped to the caller (``owner_user_id``); a row that names
    another owner is still dropped here, so an API that ever lists across users cannot make
    this delete someone else's task.
    """
    response = await http.get("/v1/scheduled-tasks")
    if response.status_code != 200:
        raise schedules._failure("listing the scheduled tasks", response)
    rows = [
        row
        for row in response.json().get("scheduled_tasks") or []
        if isinstance(row, dict)
        and row.get("name") == TASK_NAME
        and (not row.get("owner_user_id") or row.get("owner_user_id") == owner)
    ]
    return sorted(rows, key=lambda r: str(r.get("created_at") or ""))


async def find_task(http: httpx.AsyncClient, owner: str = "local") -> dict[str, Any] | None:
    """The caller's ``Preset review check`` row, or None."""
    rows = await find_tasks(http, owner)
    return rows[0] if rows else None


async def _delete(http: httpx.AsyncClient, row: dict[str, Any]) -> None:
    response = await http.delete(f"/v1/scheduled-tasks/{row['id']}")
    if response.status_code not in (200, 204):
        raise schedules._failure(f"deleting {TASK_NAME!r}", response)


async def ensure_task(
    http: httpx.AsyncClient,
    state: role_prefs.RolesState | Callable[[], role_prefs.RolesState],
    owner: str = "local",
) -> dict[str, Any]:
    """Create the task row, or patch the managed fields that drifted.

    Never sends ``state``, ``keep_runs`` or ``host_id`` on a patch: pausing a task is the
    person's. *state* may be a callable that reads the roles; it is called once the lock is held.
    Runs under a per-owner lock and re-reads the rows inside it, so two saves at once
    leave one task; extra rows beyond the first are deleted. Answers
    ``{"ok", "action": created|updated|unchanged, "id"}`` or ``{"ok": False, "error"}``.
    """
    try:
        async with _lock_for(owner):
            if callable(state):
                state = state()  # the current roles, read after the lock is held
            agents = await schedules.agents_by_name(http)
            agent_id = agents.get(AGENT_NAME)
            if agent_id is None:
                return {
                    "ok": False,
                    "error": (
                        f"agent {AGENT_NAME!r} is not registered on the server. Add "
                        "/opt/ae/agents/preset-reviewer to OMNIGENT_BUILTIN_AGENT_DIRS and "
                        "restart."
                    ),
                }
            zone, _project = await schedules.server_defaults(http)
            rows = await find_tasks(http, owner)
            existing = rows[0] if rows else None
            for extra in rows[1:]:
                await _delete(http, extra)
            if existing is None:
                response = await http.post(
                    "/v1/scheduled-tasks", json=_body(state, zone, agent_id, create=True)
                )
                if response.status_code != 200:
                    raise schedules._failure(f"creating {TASK_NAME!r}", response)
                return {"ok": True, "action": "created", "id": response.json().get("id")}
            want = _body(state, zone, agent_id, create=False)
            drift = {key: want[key] for key in MANAGED if existing.get(key) != want[key]}
            if not drift:
                return {"ok": True, "action": "unchanged", "id": existing["id"]}
            response = await http.patch(f"/v1/scheduled-tasks/{existing['id']}", json=drift)
            if response.status_code != 200:
                raise schedules._failure(f"updating {TASK_NAME!r}", response)
            return {
                "ok": True,
                "action": "updated",
                "id": existing["id"],
                "changed": sorted(drift),
            }
    except (schedules.SeedError, httpx.HTTPError) as exc:
        return {"ok": False, "error": str(exc) or type(exc).__name__}


async def remove_task(http: httpx.AsyncClient, owner: str = "local") -> dict[str, Any]:
    """Delete every task row of the caller's named ``Preset review check``.

    ``{"ok", "action": deleted|none}``.
    """
    try:
        async with _lock_for(owner):
            rows = await find_tasks(http, owner)
            if not rows:
                return {"ok": True, "action": "none"}
            for row in rows:
                await _delete(http, row)
            return {"ok": True, "action": "deleted", "id": rows[0]["id"]}
    except (schedules.SeedError, httpx.HTTPError) as exc:
        return {"ok": False, "error": str(exc) or type(exc).__name__}


# -- what the reviewer reads


def _parse_iso(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def due_state(
    owner: str,
    now: datetime,
    *,
    suggestions: preset_suggestions.SuggestionStore | None = None,
    roles: Any | None = None,
    consume: bool = False,
) -> dict[str, Any]:
    """``{"due", "why", "last_review", "next_due"}`` for *owner*.

    :param consume: Take the Run now flag when it is what makes the read due.
    """
    sugs = suggestions or preset_suggestions.store()
    roles = roles or role_prefs.store()
    last = sugs.last_review(owner)
    last_at = _parse_iso(last["at"]) if last else None
    next_due = (last_at + timedelta(days=MIN_GAP_DAYS)).isoformat() if last_at else None
    out: dict[str, Any] = {
        "last_review": last_at.isoformat() if last_at else None,
        "next_due": next_due,
    }
    forced = (
        sugs.take_forced(owner, now.timestamp())
        if consume
        else sugs.peek_forced(owner, now.timestamp())
    )
    if forced:
        return {"due": True, "why": "A review was asked for with Run now.", **out}
    insights = roles.get(owner).insights
    if not (insights and insights.enabled):
        return {"due": False, "why": "Preset insights are off for this user.", **out}
    if last_at is None:
        return {"due": True, "why": "There has been no review yet.", **out}
    age = (now - last_at).total_seconds() / _DAY
    if age >= MIN_GAP_DAYS:
        return {"due": True, "why": f"The last review was {int(age)} days ago.", **out}
    return {
        "due": False,
        "why": f"The last review was {int(age)} days ago; the next is due after {MIN_GAP_DAYS}.",
        **out,
    }


def _differs_from_starter(preset: role_prefs.Preset) -> bool:
    starter = next((s for s in role_prefs.STARTERS if s.id == preset.id), None)
    return starter is None or preset.model_dump(mode="json") != starter.model_dump(mode="json")


def _clean_preset(preset: role_prefs.Preset) -> dict[str, Any]:
    """The preset as a dump with every free-text field run through :func:`_clean`: the user's
    own words (title, description, when to use, reviewer agent names) reach the reviewer as
    data, one line each, never as a block that reads like instructions."""
    dump = preset.model_dump(mode="json")
    for key in ("title", "description", "when_to_use"):
        if isinstance(dump.get(key), str):
            dump[key] = _clean(dump[key], MAX_USER_TEXT)
    reviewer = (dump.get("roles") or {}).get("reviewer")
    if isinstance(reviewer, dict):
        for slot in reviewer.get("reviewers") or []:
            if isinstance(slot, dict) and isinstance(slot.get("name"), str):
                slot["name"] = _clean(slot["name"], MAX_USER_TEXT)
    return dump


def _user_presets(state: role_prefs.RolesState) -> dict[str, Any]:
    return {
        "default_preset": state.default_preset,
        "presets": [_clean_preset(p) for p in state.presets if _differs_from_starter(p)],
    }


def _clean(text: Any, limit: int) -> str:
    """*text* as one line without control characters, cut to *limit*: user text in the read."""
    return _CONTROL.sub(" ", str(text)).strip()[:limit]


def _trim_reason(line: dict[str, Any]) -> dict[str, Any]:
    reason = line.get("reason")
    if isinstance(reason, str):
        return {**line, "reason": _clean(reason, MAX_REASON)}
    return line


def build_read(
    owner: str,
    admin: bool,
    now: datetime,
    *,
    suggestions: preset_suggestions.SuggestionStore | None = None,
    roles: Any | None = None,
) -> dict[str, Any]:
    """The reviewer's one read. Consumes the Run now flag when that is why it is due."""
    roles = roles or role_prefs.store()
    state = due_state(owner, now, suggestions=suggestions, roles=roles, consume=True)
    if not state["due"]:
        return state
    insights = roles.get(owner).insights
    weeks = insights.weeks if insights else 2
    last = _parse_iso(state["last_review"]) if state["last_review"] else None
    since = last.timestamp() if last else (now - timedelta(weeks=weeks)).timestamp()
    until = now.timestamp()
    runs = [r for r in preset_stats.read_log_runs(since, until) if admin or r.get("user") == owner]
    runs.sort(key=lambda r: r.get("at") or 0.0, reverse=True)
    truncated = len(runs) > MAX_RUNS
    runs = [
        {**r, "title": _clean(r["title"], MAX_TITLE)} if isinstance(r.get("title"), str) else r
        for r in runs[:MAX_RUNS]
    ]
    ids = {r["session"] for r in runs if r.get("session")}
    raw = [
        line
        for line in preset_stats._read_jsonl(preset_runs.log_path())
        if line.get("session") in ids
    ]
    family = set(ids)
    for line in raw:
        for agent in line.get("agents") or []:
            if isinstance(agent, dict) and agent.get("session"):
                family.add(str(agent["session"]))
    model_changes = [
        _trim_reason(line)
        for line in preset_stats._read_jsonl(model_log.log_path())
        if line.get("session") in family and (line.get("at") or 0) >= since
    ]
    switches = [
        line
        for line in preset_stats._read_jsonl(preset_log.log_path())
        if line.get("session") in ids and (line.get("at") or 0) >= since
    ]
    users: dict[str, Any]
    if admin:
        users = {u: _user_presets(s) for u, s in roles.all_states().items()}
    else:
        users = {owner: _user_presets(roles.get(owner))}
    return {
        **state,
        "scope": "admin" if admin else "own",
        "owner": owner,
        "since": preset_stats._iso(since),
        "runs": runs,
        "truncated": truncated,
        "model_changes": model_changes,
        "preset_switches": switches,
        "presets": {"starters": role_prefs.starters(), "users": users},
        "limits": {"max_suggestions": MAX_SUGGESTIONS},
    }


__all__ = [
    "AGENT_NAME",
    "KEEP_RUNS",
    "MAX_COST_USD",
    "MIN_GAP_DAYS",
    "RRULE",
    "TASK_NAME",
    "build_read",
    "build_schedule",
    "due_state",
    "ensure_task",
    "find_task",
    "find_tasks",
    "remove_task",
    "reviewer_model",
]
