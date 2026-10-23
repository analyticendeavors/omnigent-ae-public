"""``GET /v1/ae/your-move``: everything waiting on Reid, in one payload.

Why (Reid, 2026-09-24): across a long day he kept asking "what's left", runs
several sessions at once and merges from his phone.
This answers "what is waiting on me right now" from signals that already
exist; it adds no prompt rule (CLAUDE.md rule 4) and writes nothing.

Four sources, each isolated: one that fails answers ``{"ok": false,
"error": {...}}`` in its own slot and the others still render.

- ``sessions``: top-level sessions with an open gate (``ae.gate=open``), an
  outstanding approval or question (``pending_elicitation_count > 0``, what
  the sidebar shows as awaiting), or ``ae.status`` ``blocked`` or ``review``.
  One row per session, the first matching reason as its ``kind``.
- ``lot``: Parking Lot tasks whose resurface date is today or earlier in
  ``AE_TASK_STORE_TZ``, oldest first.
- ``steward``: the newest session of the steward agent (``AE_STEWARD_AGENT``,
  default ``steward``) and its last assistant message.
- ``pulls``: open pull requests by or awaiting ``AE_GITHUB_LOGIN`` across
  ``AE_GITHUB_REPOS``, with CI state (:mod:`ae_omni_policies.github`).

In multi-user mode (2026-10-21, pre-publication audit A2) ``sessions`` lists
only sessions the caller may open (upstream's ``accessible_by``, the filter
``GET /v1/sessions`` applies) and ``steward`` only a steward session the caller
owns (``owned_by``, so a steward run shared by someone else never replaces the
caller's brief). The lot and pull requests are the server's own task-store and
GitHub settings.

The whole answer is cached per caller for :data:`CACHE_TTL_S` seconds, so a
phone and a laptop polling together do not multiply the task-store calls; ``fresh=1``
skips that cache after the page changed something. Pull requests keep their
own :data:`PULLS_TTL_S` cache, which ``fresh`` does not skip: nothing the page
does changes them, and two pages polling every 20 seconds would otherwise
spend a fine-grained token's hourly allowance on check runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from omnigent.entities.conversation import Conversation, MessageData
from omnigent.runtime import get_conversation_store

from ae_omni_policies import github, labels, sessions
from ae_omni_policies.taskstore import Settings, TaskStoreClient, TaskStoreError

_logger = logging.getLogger(__name__)

CACHE_TTL_S = 5.0
PULLS_TTL_S = 60.0
STEWARD_AGENT_ENV = "AE_STEWARD_AGENT"
DEFAULT_STEWARD_AGENT = "steward"
# A brief is a screen or two; anything longer is cut, the session has the rest.
BRIEF_CAP = 8_000
# Newest messages read when looking for the steward's last reply.
_BRIEF_SCAN = 50
LOT_SCAN = 100

# Session kinds, most urgent first: a row takes the first that applies.
KIND_GATE = "gate"
KIND_AWAITING = "awaiting"
KIND_BLOCKED = "blocked"
KIND_REVIEW = "review"
KINDS = (KIND_GATE, KIND_AWAITING, KIND_BLOCKED, KIND_REVIEW)

# Keyed by the caller (``None`` on a single-user runtime), so one user's
# sessions are never served to another.
_cache: dict[str | None, tuple[float, dict[str, Any]]] = {}
_pulls_cache: tuple[float, dict[str, Any]] | None = None


def reset_cache() -> None:
    """Forget the cached answers, pull requests included (tests, a changed setting)."""
    global _pulls_cache
    _cache.clear()
    _pulls_cache = None


def forget_answer() -> None:
    """Forget every caller's cached answer but keep the pull requests (after a gate reply)."""
    _cache.clear()


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


def session_kinds(conv: Conversation) -> list[str]:
    """Every reason *conv* waits on Reid, most urgent first; empty when it does not."""
    kinds: list[str] = []
    if labels.label(conv.labels, labels.GATE) == labels.GATE_OPEN:
        kinds.append(KIND_GATE)
    if sessions.is_blocked(conv):
        kinds.append(KIND_AWAITING)
    status = labels.status_of(conv.labels)
    if status == labels.BLOCKED:
        kinds.append(KIND_BLOCKED)
    if status == labels.REVIEW:
        kinds.append(KIND_REVIEW)
    return kinds


def waiting_sessions(rows: list[Conversation], *, now: float) -> list[dict[str, Any]]:
    """The rows that wait on Reid, grouped by kind in :data:`KINDS` order, oldest wait first."""
    found: list[dict[str, Any]] = []
    for conv in rows:
        kinds = session_kinds(conv)
        if not kinds:
            continue
        found.append(
            {
                "id": conv.id,
                "title": sessions.display_title(conv),
                "kind": kinds[0],
                "kinds": kinds,
                "status": labels.status_of(conv.labels),
                "fork": labels.label(conv.labels, labels.FORK),
                "next": labels.label(conv.labels, labels.NEXT),
                "task": labels.label(conv.labels, labels.TASK),
                "pending": conv.pending_elicitation_count or 0,
                "updated_at": int(conv.updated_at or conv.created_at),
                "since": sessions.format_elapsed(now - (conv.updated_at or conv.created_at)),
            }
        )
    found.sort(key=lambda row: (KINDS.index(row["kind"]), row["updated_at"]))
    return found


async def sessions_source(now: float, user: str | None = None) -> dict[str, Any]:
    rows = await asyncio.to_thread(lambda: sessions.list_top_level(accessible_by=user))
    items = waiting_sessions(rows, now=now)
    counts = {kind: sum(1 for row in items if row["kind"] == kind) for kind in KINDS}
    return {"ok": True, "items": items, "counts": counts}


async def lot_source(today: str) -> dict[str, Any]:
    settings = Settings.from_env()
    if not settings.configured:
        return _error(
            "task_store_not_configured",
            "task-store is not configured: set AE_TASK_STORE_URL, AE_CF_ACCESS_CLIENT_ID "
            "and AE_CF_ACCESS_CLIENT_SECRET in the server's environment.",
        )
    async with TaskStoreClient(settings) as client:
        try:
            lot = await client.list_parked(status="open", limit=LOT_SCAN)
        except TaskStoreError as exc:
            return _error("task_store_unavailable", str(exc), kind=exc.result.kind)
    due = [
        {**task, "overdue": task["resurfaces"] < today}
        for task in lot.get("tasks") or []
        if isinstance(task, dict) and task.get("resurfaces") and task["resurfaces"] <= today
    ]
    due.sort(key=lambda task: (task["resurfaces"], str(task.get("title") or "")))
    return {"ok": True, "today": today, "items": due, "note": lot.get("note")}


def _text(data: MessageData) -> str:
    return "\n".join(
        block["text"]
        for block in data.content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ).strip()


def latest_brief(agent_name: str, user: str | None = None) -> dict[str, Any] | None:
    """The newest session of *agent_name* and its last assistant message, or ``None``.

    With *user*, only a session that user owns (``owned_by``).
    """
    store = get_conversation_store()
    page = store.list_conversations(
        limit=1,
        kind="default",
        agent_name=agent_name,
        order="desc",
        sort_by="created_at",
        owned_by=user,
    )
    if not page.data:
        return None
    conv = page.data[0]
    items = store.list_items(conv.id, limit=_BRIEF_SCAN, order="desc", type="message")
    text: str | None = None
    at: int | None = None
    for item in items.data:
        data = item.data
        if isinstance(data, MessageData) and data.role == "assistant" and not data.is_meta:
            body = _text(data)
            if body:
                text, at = body, int(item.created_at)
                break
    truncated = text is not None and len(text) > BRIEF_CAP
    return {
        "session_id": conv.id,
        "title": sessions.display_title(conv),
        "created_at": int(conv.created_at),
        "live_status": conv.live_status,
        "message_at": at,
        "text": text[:BRIEF_CAP] if text is not None else None,
        "truncated": truncated,
    }


async def steward_source(user: str | None = None) -> dict[str, Any]:
    agent = (os.environ.get(STEWARD_AGENT_ENV) or "").strip() or DEFAULT_STEWARD_AGENT
    brief = await asyncio.to_thread(latest_brief, agent, user)
    if brief is None:
        return {
            "ok": True,
            "agent": agent,
            "brief": None,
            "note": f"No {agent} session yet: it runs weekdays at 08:00 once scheduled "
            "(just schedules-seed).",
        }
    return {"ok": True, "agent": agent, "brief": brief}


async def pulls_source() -> dict[str, Any]:
    """Pull requests, from their own cache when it is under :data:`PULLS_TTL_S` old."""
    global _pulls_cache
    now = time.time()
    if _pulls_cache is not None and now - _pulls_cache[0] < PULLS_TTL_S:
        return _pulls_cache[1]
    answer = await _read_pulls()
    if answer.get("configured"):
        _pulls_cache = (now, answer)
    return answer


async def _read_pulls() -> dict[str, Any]:
    settings = github.GitHubSettings.from_env()
    if not settings.configured or not settings.repos:
        return {
            "ok": True,
            "configured": False,
            "login": settings.login,
            "repos": list(settings.repos),
            "items": [],
            "errors": [],
            "note": github.HOW_TO_ENABLE if not settings.configured else github.NO_REPOS,
            "missing": "token" if not settings.configured else "repos",
        }
    async with github.GitHubClient(settings) as client:
        result = await client.open_pulls()
    return {
        "ok": True,
        "configured": True,
        "login": result["login"],
        "repos": result["repos"],
        "items": result["pulls"],
        "errors": result["errors"],
    }


async def _isolated(name: str, source: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Run one source; any exception becomes that source's error, never the route's."""
    try:
        return await source()
    except Exception as exc:  # noqa: BLE001 (isolation is the point: one source, one error)
        # The class name only: a message could carry a URL with a credential in it.
        _logger.warning("your-move: the %s source failed: %s", name, type(exc).__name__)
        return _error("source_failed", f"{name} could not be read ({type(exc).__name__}).")


async def build(*, fresh: bool = False, user: str | None = None) -> dict[str, Any]:
    """The whole answer, from the cache when it is under :data:`CACHE_TTL_S` old.

    :param fresh: Skip the cached answer.
    :param user: The caller, whose sessions alone are read; ``None`` reads every
        session (a single-user runtime).
    """
    now = time.time()
    held = _cache.get(user)
    if not fresh and held is not None and now - held[0] < CACHE_TTL_S:
        return {**held[1], "cached": True}
    tz = Settings.from_env().tz
    local_now = datetime.fromtimestamp(now, ZoneInfo(tz))
    today = local_now.date().isoformat()
    session_part, lot_part, steward_part, pulls_part = await asyncio.gather(
        _isolated("sessions", lambda: sessions_source(now, user)),
        _isolated("lot", lambda: lot_source(today)),
        _isolated("steward", lambda: steward_source(user)),
        _isolated("pulls", pulls_source),
    )
    total = 0
    if session_part.get("ok"):
        total += len(session_part["items"])
    if lot_part.get("ok"):
        total += len(lot_part["items"])
    if pulls_part.get("ok"):
        total += len(pulls_part["items"])
    answer = {
        "now": local_now.isoformat(timespec="seconds"),
        "today": today,
        "timezone": tz,
        "total": total,
        "sessions": session_part,
        "lot": lot_part,
        "steward": steward_part,
        "pulls": pulls_part,
    }
    _cache[user] = (now, answer)
    return {**answer, "cached": False}


__all__ = [
    "CACHE_TTL_S",
    "KINDS",
    "PULLS_TTL_S",
    "build",
    "forget_answer",
    "latest_brief",
    "reset_cache",
    "session_kinds",
    "waiting_sessions",
]
