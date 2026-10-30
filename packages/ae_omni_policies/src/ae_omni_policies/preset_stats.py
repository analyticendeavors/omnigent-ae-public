"""Preset insights for the Admin Center's Presets card (2026-10-24, PR 2).

``GET /v1/ae/presets?days=7|30`` (admins only) answers what the preset run log
(:mod:`ae_omni_policies.preset_runs`) holds for the window: per preset the runs, median time,
median cost, cost by model, raises and step-downs, a daily cost series and the recent runs,
plus fixed-rule takeaways. Nothing here calls a model.

**Never blocks** (the pattern of :mod:`ae_omni_policies.azure_cost`). The route answers the
cache, which lives in memory and in a state file (:data:`STATE_FILE_ENV`, default
``/data/ae-preset-stats.json``), and starts a background thread to recompute when the cached
answer is older than :data:`CACHE_TTL_S`. With no cache at all it answers ``{"state":
"warming"}`` and the card polls again; it never reads the log on the request path. A failed
recompute keeps the old answer and is not retried for :data:`RETRY_S`. The state file is
written (atomically) before the memory cache is replaced, and no lock is held while the log or
the store is read.

**Two modes.** ``"mode": "log"`` reads the run log (both files, the highest ``rev`` per
``run_key``, ``at`` inside the window). When the log has no line in the window (insights off)
the answer is ``"mode": "fallback"``: top-level sessions updated in the window, grouped by
their preset, with cost and wall time from the session itself, raises and step-downs from the
model log, and no hung count, review or takeaways.

**Takeaway rules** (log mode only, at most :data:`MAX_TAKEAWAYS` per view, largest gap first;
each carries ``metric``: cost, tokens, time or hang, and the card hides the metric the viewer did
not pick, so up to six come back). The card prints the text it is given, so the rules live here
and nowhere else:

1. Cost: a preset with at least :data:`MIN_PRESET_RUNS` runs whose median cost is at least
   :data:`GAP` (25%) above or below the median of every other preset's runs, when those other
   presets have at least :data:`MIN_PRESET_RUNS` runs between them. The text adds whether it
   also finished faster (:data:`TIME_NOISE`, 10%, is "no faster").
2. Time: the same rule on median wall time, when rule 1 did not fire for that preset.
3. Tokens: rule 1 on tokens per run (same thresholds; runs without tokens do not count).
4. Hangs: an agent role (Codex counted on its own) hung in at least :data:`MIN_HANG_RUNS`
   runs.

The gap that ranks a takeaway is the larger/smaller ratio for rules 1 and 2 and the hung run
count over :data:`MIN_HANG_RUNS` plus 1 for rule 3.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import statistics
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ae_omni_policies import labels, model_log, preset_runs, role_prefs

_logger = logging.getLogger(__name__)

STATE_FILE_ENV = "AE_PRESET_STATS_FILE"
DEFAULT_STATE_FILE = "/data/ae-preset-stats.json"
STATE_VERSION = 2
WINDOWS = (7, 30)
CACHE_TTL_S = 300.0
RETRY_S = 60.0
#: A takeaway needs this many runs for the preset, and for all the others together.
MIN_PRESET_RUNS = 5
#: How far a preset's median must sit from the rest's to be worth saying (25%).
GAP = 0.25
#: Inside this fraction a time difference reads as "no faster" / "about the same".
TIME_NOISE = 0.10
#: Runs a role must hang in before the hang takeaway fires.
MIN_HANG_RUNS = 3
MAX_TAKEAWAYS = 3
#: Runs kept per preset for the row pop-up (newest first).
MAX_RECENT = 50
CUSTOM_ID = "custom"
LEAD_PICKS_ID = "lead-picks"
_DAY = 86400.0

#: A recompute running longer than this is treated as hung and replaced.
STUCK_S = 300.0

_mu = threading.Lock()
#: Held across the state-file write and the memory update that follows it (never by readers).
_save_mu = threading.Lock()


def _fresh_state() -> dict[str, Any]:
    # running: {days: (monotonic start, generation)}; gen counts recomputes started.
    return {
        "loaded": False,
        "windows": {},
        "running": {},
        "failed": {},
        "errors": {},
        "gen": 0,
    }


_state: dict[str, Any] = _fresh_state()


# ---------------------------------------------------------------------------
# Small readers


def _num(value: Any) -> float | None:
    return preset_runs._number(value)


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _short_model(model: Any) -> str:
    """``claude-opus-5-5`` as ``Opus``; anything else as given."""
    text = str(model or "")
    for name in ("opus", "sonnet", "haiku", "fable"):
        if name in text.lower():
            return name.capitalize()
    return text or "unset"


def _read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    """Every object line of *path*'s rotation then *path* itself, oldest first."""
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
            if isinstance(entry, dict):
                out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Runs: one normalised dict per run, from the log or from sessions


def _run_cost(line: Mapping[str, Any]) -> tuple[float | None, dict[str, float]]:
    cost = line.get("cost") if isinstance(line.get("cost"), dict) else {}
    by_model: dict[str, float] = {}
    by = cost.get("by_model") if isinstance(cost.get("by_model"), dict) else {}
    for model, bucket in by.items():
        usd = _num(bucket.get("usd")) if isinstance(bucket, dict) else None
        if usd:
            by_model[str(model)] = usd
    total = _num(cost.get("total_usd"))
    if total is None and by_model:
        total = sum(by_model.values())
    return total, by_model


def _bucket_tokens(bucket: Any) -> float | None:
    """One by-model bucket's tokens: input plus output, else ``total_tokens``.

    Input plus output first because upstream's ``total_tokens`` is not the same sum everywhere:
    native Claude sessions fold cache reads into it (2026-10-25: 143.1M total, 138.0M of it cache
    reads) while SDK and Codex runs leave them out, so one card would mix the two.
    """
    if not isinstance(bucket, Mapping):
        return None
    parts = [_num(bucket.get("input_tokens")), _num(bucket.get("output_tokens"))]
    if all(p is None for p in parts):
        total = _num(bucket.get("total_tokens"))
        if total is None:
            return None
    else:
        total = sum(p or 0.0 for p in parts)
    return total if total > 0 else None


def _tokens_by_model(by: Any) -> tuple[float | None, dict[str, float]]:
    """``(run tokens, {model: tokens})`` from a ``by_model`` mapping; None when none recorded."""
    out: dict[str, float] = {}
    if isinstance(by, Mapping):
        for model, bucket in by.items():
            tokens = _bucket_tokens(bucket)
            if tokens:
                out[str(model)] = tokens
    return (sum(out.values()) if out else None), out


def _agents(line: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = line.get("agents")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _role_name(agent: Mapping[str, Any]) -> str:
    role = str(agent.get("role") or preset_runs.UNKNOWN)
    return f"Codex {role}" if agent.get("kind") == "codex" else role.capitalize()


def agents_text(agents: list[dict[str, Any]]) -> str:
    """``planner, builder x2, Codex``: roles in first-seen order with a count past one."""
    counts: dict[str, int] = {}
    for agent in agents:
        name = "Codex" if agent.get("kind") == "codex" else str(agent.get("role") or "unknown")
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(n if c == 1 else f"{n} x{c}" for n, c in counts.items()) or "lead only"


def _change_text(change: Mapping[str, Any], roles: Mapping[str, str]) -> str:
    old = change.get("from") if isinstance(change.get("from"), dict) else {}
    new = change.get("to") if isinstance(change.get("to"), dict) else {}
    who = roles.get(str(change.get("session")), "lead")
    if old.get("model") != new.get("model"):
        return f"{who} {_short_model(old.get('model'))} to {_short_model(new.get('model'))}"
    return f"{who} effort {old.get('effort') or 'unset'} to {new.get('effort') or 'unset'}"


def _review_text(review: Any) -> str:
    if not isinstance(review, dict) or not review.get("reported"):
        sessions = review.get("reviewer_sessions") if isinstance(review, dict) else None
        return f"{sessions} reviewers, none reported" if sessions else "none reported"
    ran = int(_num(review.get("reviewers_run")) or 0)
    found = int(_num(review.get("findings_returned")) or 0)
    fixed = int(_num(review.get("findings_fixed")) or 0)
    return f"{ran} reviewer{'' if ran == 1 else 's'}, {found} findings, {fixed} fixed"


def run_from_line(line: Mapping[str, Any]) -> dict[str, Any]:
    """The normalised run for one run-log line (field names are preset_runs.py's)."""
    total, by_model = _run_cost(line)
    cost_block = line.get("cost") if isinstance(line.get("cost"), dict) else {}
    tokens, tokens_by_model = _tokens_by_model(cost_block.get("by_model"))
    agents = _agents(line)
    roles = {
        str(a.get("session")): str(a.get("role") or "agent") for a in agents if a.get("session")
    }
    changes = [c for c in (line.get("model_changes") or []) if isinstance(c, dict)]
    hung = [_role_name(a) for a in agents if a.get("hung")]
    return {
        "session": line.get("session"),
        "title": line.get("title") or "",
        "preset": line.get("preset") or LEAD_PICKS_ID,
        "user": line.get("user"),
        "at": _num(line.get("at")) or 0.0,
        "wall_s": _num(line.get("wall_s")),
        "active_s": _num(line.get("active_s")),
        "cost": total,
        "by_model": by_model,
        "tokens": tokens,
        "tokens_by_model": tokens_by_model,
        "agents": agents_text(agents),
        "has_helpers": bool(agents),
        "raises": sum(1 for c in changes if c.get("kind") == "raise"),
        "step_downs": sum(1 for c in changes if c.get("kind") == "lower"),
        "model_changes": [_change_text(c, roles) for c in changes],
        "review": _review_text(line.get("review")),
        "flags": [f"{name} hung" for name in dict.fromkeys(hung)],
        "hung_roles": list(dict.fromkeys(hung)),
    }


def read_log_runs(since: float, until: float) -> list[dict[str, Any]]:
    """Runs from the run log with ``at`` in ``[since, until]``.

    Each ``run_key`` keeps its highest ``rev``'s content but its earliest ``at``, so a review
    amendment never moves a run between windows or sparkline days.
    """
    best: dict[str, dict[str, Any]] = {}
    for n, line in enumerate(_read_jsonl(preset_runs.log_path())):
        at = _num(line.get("at"))
        if at is None:
            continue
        key = str(line.get("run_key") or f"{line.get('session')}:{n}")
        rev = line.get("rev") if isinstance(line.get("rev"), int) else 0
        old = best.get(key)
        if old is None:
            best[key] = {"_rev": rev, "line": line, "first": at}
            continue
        old["first"] = min(old["first"], at)
        if rev >= old["_rev"]:
            old["_rev"] = rev
            old["line"] = line
    out = []
    for b in best.values():
        if not since <= b["first"] <= until:
            continue
        run = run_from_line(b["line"])
        # The start is the latest rev's own end minus its own wall time, so an amendment
        # that changes wall_s cannot shift it.
        last = _num(b["line"].get("at"))
        run["start_at"] = (
            last - run["wall_s"] if last is not None and run["wall_s"] is not None else None
        )
        run["at"] = b["first"]
        out.append(run)
    return out


#: Most sessions the fallback reads in one recompute.
FALLBACK_MAX_ROWS = 2000
_FALLBACK_PAGE = 200


def fetch_fallback_convs(store: Any, since: float) -> list[Any]:
    """Top-level sessions, archived ones included, updated at or after *since*.

    One bounded query (newest update first, stopping at *since*), capped at
    :data:`FALLBACK_MAX_ROWS` rows with a warning when the cap is hit. Finished runs archive
    themselves after ten minutes, so leaving archived rows out would drop nearly every run.
    """
    rows: list[Any] = []
    after: str | None = None
    for _ in range(-(-FALLBACK_MAX_ROWS // _FALLBACK_PAGE)):
        page = store.list_conversations(
            limit=_FALLBACK_PAGE,
            after=after,
            kind="default",
            include_archived=True,
            order="desc",
            sort_by="updated_at",
            accessible_by=None,
        )
        done = False
        for conv in page.data:
            if preset_runs._time(conv, "updated_at") < since:
                done = True
                break
            rows.append(conv)
        if done or not page.has_more or not page.last_id:
            return rows
        if len(rows) >= FALLBACK_MAX_ROWS:
            break
        after = page.last_id
    _logger.warning(
        "preset stats: fallback stopped at %d sessions; older runs are left out", len(rows)
    )
    return rows[:FALLBACK_MAX_ROWS]


def read_fallback_runs(
    store: Any,
    since: float,
    until: float,
    resolve: Callable[[Any], str | None] | None = None,
    rows: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Runs built from top-level sessions updated in ``[since, until]`` (insights off).

    *rows* is a prior :func:`fetch_fallback_convs` result to split in memory; without it the
    store is queried here.
    """
    resolve = resolve or (lambda conv: preset_runs.resolve_preset(store, conv)[0])
    if rows is None:
        rows = fetch_fallback_convs(store, since)
    convs = []
    for conv in rows:
        if labels.orch_parent(conv.labels) is not None:
            continue
        updated = preset_runs._time(conv, "updated_at")
        if since <= updated <= until:
            convs.append((conv, updated))
    ids = {c.id for c, _ in convs}
    changes: dict[str, list[dict[str, Any]]] = {}
    for entry in _read_jsonl(model_log.log_path()):
        at = _num(entry.get("at"))
        if entry.get("session") in ids and at is not None and since <= at <= until:
            changes.setdefault(str(entry["session"]), []).append(model_log.normalize(entry))
    runs = []
    for conv, updated in convs:
        usage = preset_runs._usage(conv)
        by_model: dict[str, dict[str, Any]] = {}
        preset_runs.add_cost(by_model, usage)
        total = _num(usage.get("total_cost_usd"))
        if total is None and by_model:
            total = sum(v.get("usd", 0) for v in by_model.values())
        started = _num(labels.label(conv.labels, labels.STARTED)) or preset_runs._time(
            conv, "created_at"
        )
        mine = changes.get(conv.id, [])
        try:
            preset = resolve(conv) or LEAD_PICKS_ID
        except Exception:  # noqa: BLE001 (one unreadable preset must not sink the card)
            preset = LEAD_PICKS_ID
        tokens, tokens_by_model = _tokens_by_model(by_model)
        runs.append(
            {
                "session": conv.id,
                "title": preset_runs._title(conv),
                "preset": preset,
                "user": None,
                "at": updated,
                "wall_s": max(updated - started, 0.0),
                "active_s": None,
                "cost": total,
                "by_model": {
                    m: float(v.get("usd", 0)) for m, v in by_model.items() if v.get("usd")
                },
                "tokens": tokens,
                "tokens_by_model": tokens_by_model,
                "agents": "not recorded",
                "has_helpers": False,
                "raises": sum(1 for c in mine if c.get("kind") == "raise"),
                "step_downs": sum(1 for c in mine if c.get("kind") == "lower"),
                "model_changes": [_change_text(c, {}) for c in mine],
                "review": "not recorded",
                "flags": [],
                "hung_roles": [],
            }
        )
    return runs


# ---------------------------------------------------------------------------
# Takeaways


def _money(value: float) -> str:
    return f"${value:,.2f}" if value < 100 else f"${value:,.0f}"


def _minutes(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60:02d} min"


def _times(ratio: float) -> str:
    return f"{ratio:.1f}x"


def _pct(fraction: float) -> str:
    return f"{round(abs(fraction) * 100)}%"


def _mostly_tokens(runs: list[dict[str, Any]]) -> bool:
    """Whether at least half of *runs* carry tokens: a window of mostly untokened runs sums to
    a total that is not comparable with a window that has them, so no change is drawn."""
    return bool(runs) and 2 * sum(1 for r in runs if r.get("tokens") is not None) >= len(runs)


def _tokens_text(value: float) -> str:
    """Compact token count: 950, 12.3K, 4.2M, 1.1B."""
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= limit:
            figure = value / limit
            return f"{figure:.1f}{suffix}" if figure < 100 else f"{figure:.0f}{suffix}"
    return f"{round(value)}"


def _gap_ratio(mine: float, rest: float) -> float:
    return max(mine / rest, rest / mine)


def _outside(mine: float, rest: float) -> bool:
    return rest > 0 and (mine >= rest * (1 + GAP) or mine <= rest * (1 - GAP))


def takeaways(runs: list[dict[str, Any]], names: Mapping[str, str]) -> list[dict[str, Any]]:
    """The fixed-rule takeaways for *runs* (see the module docstring), at most three."""
    by_preset: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        by_preset.setdefault(run["preset"], []).append(run)
    found: list[tuple[float, dict[str, Any]]] = []
    for pid, mine in by_preset.items():
        rest = [r for other, rs in by_preset.items() if other != pid for r in rs]
        name = names.get(pid, pid)
        # Each rule counts only the runs that have the value it compares.
        mine_c = [r["cost"] for r in mine if r["cost"] is not None]
        rest_c = [r["cost"] for r in rest if r["cost"] is not None]
        mine_w = [r["wall_s"] for r in mine if r["wall_s"] is not None]
        rest_w = [r["wall_s"] for r in rest if r["wall_s"] is not None]
        cost_ok = len(mine_c) >= MIN_PRESET_RUNS and len(rest_c) >= MIN_PRESET_RUNS
        time_ok = len(mine_w) >= MIN_PRESET_RUNS and len(rest_w) >= MIN_PRESET_RUNS
        mc = _median(mine_c) if cost_ok else None
        rc = _median(rest_c) if cost_ok else None
        mw = _median(mine_w) if time_ok else None
        rw = _median(rest_w) if time_ok else None
        mine_t = [r["tokens"] for r in mine if r.get("tokens") is not None]
        rest_t = [r["tokens"] for r in rest if r.get("tokens") is not None]
        if len(mine_t) >= MIN_PRESET_RUNS and len(rest_t) >= MIN_PRESET_RUNS:
            mt, rt = _median(mine_t), _median(rest_t)
            if mt and rt and _outside(mt, rt):
                ratio = _gap_ratio(mt, rt)
                if mt > rt:
                    head = (
                        f"{name} uses {_times(ratio)} the tokens of the rest"
                        if ratio >= 2
                        else f"{name} uses {_pct(mt / rt - 1)} more tokens than the rest"
                    )
                else:
                    head = f"{name} uses {_pct(1 - mt / rt)} fewer tokens than the rest"
                found.append(
                    (
                        ratio,
                        {
                            "id": f"tokens:{pid}",
                            "metric": "tokens",
                            "level": "warn" if mt > rt else "info",
                            "text": head,
                            "why": (
                                f"That is {len(mine_t)} runs, median {_tokens_text(mt)} tokens "
                                f"against {_tokens_text(rt)}. The rest is {len(rest_t)} runs."
                            ),
                            "preset": pid,
                        },
                    )
                )
        fired = False
        if mc and rc and _outside(mc, rc):
            fired = True
            ratio = _gap_ratio(mc, rc)
            if mc > rc:
                head = (
                    f"{name} costs {_times(ratio)} the rest"
                    if ratio >= 2
                    else (f"{name} costs {_pct(mc / rc - 1)} more than the rest")
                )
            else:
                head = f"{name} costs {_pct(1 - mc / rc)} less than the rest"
            tail = "and finishes in about the same time"
            if mw and rw:
                if mw <= rw * (1 - TIME_NOISE):
                    tail = f"and finishes {_pct(1 - mw / rw)} faster"
                elif mw >= rw * (1 + TIME_NOISE):
                    tail = f"and takes {_pct(mw / rw - 1)} longer"
                elif mc > rc:
                    tail = "and finishes no faster"
            why = f"That is {len(mine_c)} runs, median {_money(mc)} against {_money(rc)}"
            if mw and rw:
                why += f", and {_minutes(mw)} against {_minutes(rw)}"
            found.append(
                (
                    ratio,
                    {
                        "id": f"cost:{pid}",
                        "metric": "cost",
                        "level": "warn" if mc > rc else "info",
                        "text": f"{head} {tail}",
                        "why": why + f". The rest is {len(rest_c)} runs.",
                        "preset": pid,
                    },
                )
            )
        if not fired and mw and rw and _outside(mw, rw):
            ratio = _gap_ratio(mw, rw)
            if mw > rw:
                head = (
                    f"{name} takes {_times(ratio)} as long as the rest"
                    if ratio >= 2
                    else f"{name} takes {_pct(mw / rw - 1)} longer than the rest"
                )
            else:
                head = f"{name} finishes {_pct(1 - mw / rw)} faster than the rest"
            found.append(
                (
                    ratio,
                    {
                        "id": f"time:{pid}",
                        "metric": "time",
                        "level": "warn" if mw > rw else "info",
                        "text": head,
                        "why": (
                            f"That is {len(mine_w)} runs, median {_minutes(mw)} against "
                            f"{_minutes(rw)}. The rest is {len(rest_w)} runs."
                        ),
                        "preset": pid,
                    },
                )
            )
    hangs: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        for role in run["hung_roles"]:
            hangs.setdefault(role, []).append(run)
    for role, hung_runs in hangs.items():
        if len(hung_runs) < MIN_HANG_RUNS:
            continue
        presets: dict[str, int] = {}
        for run in hung_runs:
            presets[run["preset"]] = presets.get(run["preset"], 0) + 1
        listing = ", ".join(f"{names.get(p, p)} ({n})" for p, n in presets.items())
        found.append(
            (
                len(hung_runs) / MIN_HANG_RUNS + 1,
                {
                    "id": f"hang:{role}",
                    "metric": "hang",
                    "level": "warn",
                    "text": f"{role} hung {len(hung_runs)} times",
                    "why": (
                        f"No update for {int(preset_runs.HANG_AFTER_S // 60)} minutes in "
                        f"{len(hung_runs)} runs. Presets: {listing}."
                    ),
                    "preset": max(presets, key=lambda p: presets[p]),
                },
            )
        )
    found.sort(key=lambda pair: pair[0], reverse=True)
    # The card shows cost, tokens or both, hiding the other metric's takeaways, so keep the top
    # MAX_TAKEAWAYS of each view: a tokens-only viewer is not left with the cost ones' share.
    # With no tokens recorded there is only the one view.
    keep: set[str] = set()
    has_tokens = any(item["metric"] == "tokens" for _, item in found)
    for hidden in ("tokens", "cost") if has_tokens else ("tokens",):
        shown = [item for _, item in found if item["metric"] != hidden]
        keep.update(item["id"] for item in shown[:MAX_TAKEAWAYS])
    return [item for _, item in found if item["id"] in keep]


# ---------------------------------------------------------------------------
# The answer


def _preset_names(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{preset id: {name, color, custom}}`` from the users' saved presets."""
    meta: dict[str, dict[str, Any]] = {
        CUSTOM_ID: {"name": "Per session", "color": "slate", "custom": True},
        LEAD_PICKS_ID: {"name": "Lead picks", "color": "slate", "custom": False},
    }
    seen: dict[str, dict[str, Any]] = {}
    users = {r["user"] or "local" for r in runs}
    for user in sorted(users):
        try:
            state = role_prefs.store().get(user)
        except Exception:  # noqa: BLE001
            continue
        for preset in state.presets:
            seen.setdefault(
                preset.id, {"name": preset.title, "color": preset.color, "custom": False}
            )
    for run in runs:
        pid = run["preset"]
        if pid not in meta:
            meta[pid] = seen.get(pid) or {"name": pid, "color": "slate", "custom": False}
    return meta


def build_view(
    cur: list[dict[str, Any]],
    prior: list[dict[str, Any]],
    *,
    days: int,
    now: float,
    mode: str,
) -> dict[str, Any]:
    """The card's answer for the current window's runs and the prior window's."""
    meta = _preset_names(cur)
    since = now - days * _DAY
    groups: dict[str, list[dict[str, Any]]] = {}
    for run in cur:
        groups.setdefault(run["preset"], []).append(run)
    presets = []
    for pid, runs in groups.items():
        by_model: dict[str, float] = {}
        tok_by_model: dict[str, float] = {}
        daily = [0.0] * days
        daily_tokens = [0.0] * days
        for run in runs:
            for model, usd in run["by_model"].items():
                by_model[model] = by_model.get(model, 0.0) + usd
            for model, count in run["tokens_by_model"].items():
                tok_by_model[model] = tok_by_model.get(model, 0.0) + count
            slot = min(max(int((run["at"] - since) // _DAY), 0), days - 1)
            daily[slot] += run["cost"] or 0.0
            daily_tokens[slot] += run["tokens"] or 0.0
        recent = sorted(runs, key=lambda r: r["at"], reverse=True)[:MAX_RECENT]
        info = meta[pid]
        presets.append(
            {
                "id": pid,
                "name": info["name"],
                "color": info["color"],
                "custom": info["custom"],
                "runs": len(runs),
                "median_wall_s": _median([r["wall_s"] for r in runs if r["wall_s"] is not None]),
                "median_cost": _median([r["cost"] for r in runs if r["cost"] is not None]),
                "cost_by_model": dict(sorted(by_model.items(), key=lambda kv: -kv[1])),
                "median_tokens": _median([r["tokens"] for r in runs if r["tokens"] is not None]),
                "total_tokens": sum(r["tokens"] or 0.0 for r in runs),
                "tokens_by_model": dict(sorted(tok_by_model.items(), key=lambda kv: -kv[1])),
                "daily_tokens": daily_tokens,
                "raises": sum(r["raises"] for r in runs),
                "step_downs": sum(r["step_downs"] for r in runs),
                "hung_runs": sum(1 for r in runs if r["hung_roles"]),
                "daily": daily,
                "recent": [
                    {
                        "at": _iso(r["at"]),
                        "started_at": _iso(r["start_at"])
                        if r.get("start_at") is not None
                        else _iso(r["at"] - r["wall_s"])
                        if r["wall_s"] is not None
                        else None,
                        "session": r["session"],
                        "title": r["title"],
                        "wall_s": r["wall_s"],
                        "active_s": r["active_s"],
                        "cost": r["cost"],
                        "tokens": r["tokens"],
                        "agents": r["agents"],
                        "model_changes": r["model_changes"],
                        "review": r["review"],
                        "flags": r["flags"],
                    }
                    for r in recent
                ],
            }
        )
    presets.sort(key=lambda p: (-p["runs"], p["name"]))
    cost = sum(r["cost"] or 0.0 for r in cur)
    cost_prior = sum(r["cost"] or 0.0 for r in prior)
    tokens = sum(r["tokens"] or 0.0 for r in cur)
    tokens_prior = sum(r["tokens"] or 0.0 for r in prior)
    wall = _median([r["wall_s"] for r in cur if r["wall_s"] is not None])
    wall_prior = _median([r["wall_s"] for r in prior if r["wall_s"] is not None])
    names = {pid: m["name"] for pid, m in meta.items()}
    log_mode = mode == "log"
    return {
        "state": "ready",
        "mode": mode,
        "days": days,
        "asOf": _iso(now),
        "kpis": {
            "runs": len(cur),
            "families": sum(1 for r in cur if r["has_helpers"]),
            "cost": cost,
            "cost_change_pct": round((cost - cost_prior) / cost_prior * 100)
            if prior and cost_prior > 0
            else None,
            "tokens": tokens,
            "tokens_change_pct": round((tokens - tokens_prior) / tokens_prior * 100)
            if prior and tokens_prior > 0 and _mostly_tokens(cur) and _mostly_tokens(prior)
            else None,
            "median_wall_s": wall,
            "wall_change_s": (wall - wall_prior) if wall is not None and wall_prior else None,
            "raises": sum(r["raises"] for r in cur),
            "step_downs": sum(r["step_downs"] for r in cur),
            "hung_runs": sum(1 for r in cur if r["hung_roles"]) if log_mode else None,
        },
        "takeaways": takeaways(cur, names) if log_mode else [],
        "presets": presets,
    }


def compute(
    days: int,
    *,
    now: float | None = None,
    store_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Build the answer for *days* from the log, or from sessions when the log is empty."""
    now = time.time() if now is None else now
    since = now - days * _DAY
    both = read_log_runs(since - days * _DAY, now)
    cur = [r for r in both if r["at"] >= since]
    if cur:
        return build_view(
            cur, [r for r in both if r["at"] < since], days=days, now=now, mode="log"
        )
    if store_factory is None:
        from omnigent.runtime import get_conversation_store

        store_factory = get_conversation_store
    store = store_factory()
    rows = fetch_fallback_convs(store, since - days * _DAY)
    cur = read_fallback_runs(store, since, now, rows=rows)
    # At the cap the oldest rows are the ones dropped, so the prior window is short and a
    # change against it would look inflated: leave the changes out and say so.
    truncated = len(rows) >= FALLBACK_MAX_ROWS
    prior = [] if truncated else read_fallback_runs(store, since - days * _DAY, since, rows=rows)
    view = build_view(cur, prior, days=days, now=now, mode="fallback")
    view["truncated"] = truncated
    return view


# ---------------------------------------------------------------------------
# The cache


def state_path(environ: Mapping[str, str] | None = None) -> Path | None:
    env = environ if environ is not None else os.environ
    if STATE_FILE_ENV in env:
        raw = (env[STATE_FILE_ENV] or "").strip()
        return Path(raw) if raw else None
    return Path(DEFAULT_STATE_FILE) if Path(DEFAULT_STATE_FILE).parent.is_dir() else None


def _load_file() -> dict[str, dict[str, Any]]:
    path = state_path()
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, entry in (data.get("windows") or {}).items():
        if key in {str(d) for d in WINDOWS} and isinstance(entry, dict):
            if _num(entry.get("at")) is not None and isinstance(entry.get("view"), dict):
                out[key] = {"at": float(entry["at"]), "view": entry["view"]}
    return out


def _save_file(windows: Mapping[str, Any]) -> bool:
    path = state_path()
    if path is None:
        return False
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, prefix=path.name + ".", delete=False, encoding="utf-8"
        ) as tmp:
            tmp_name = tmp.name
            json.dump({"version": STATE_VERSION, "windows": windows}, tmp, separators=(",", ":"))
        os.replace(tmp_name, path)
    except OSError as exc:
        _logger.warning("preset stats not saved (%s): %s", path, exc)
        if tmp_name:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
        return False
    return True


def _refresh(
    days: int,
    compute_fn: Callable[[int], dict[str, Any]] | None = None,
    gen: int | None = None,
) -> None:
    """Recompute one window and replace the cache; runs in the background thread.

    *gen* is the generation that started this run; a run that was superseded (it hung past
    :data:`STUCK_S` and a fresh one took over) leaves the cache and the bookkeeping alone.
    """

    def current() -> bool:
        with _mu:
            return gen is None or _state["running"].get(days, (None, None))[1] == gen

    try:
        view = (compute_fn or compute)(days)
        if not current():
            return
        entry = {"at": time.time(), "view": view}
        # One save lock around copy, file write and memory update, so two windows finishing
        # together merge into the file instead of one dropping the other. Readers only take
        # _mu, so they never wait on the disk.
        with _save_mu:
            if not current():  # superseded while waiting for the save lock
                return
            with _mu:
                windows = dict(_state["windows"])
            windows[str(days)] = entry
            _save_file(windows)
            with _mu:
                _state["windows"][str(days)] = entry
                _state["failed"].pop(days, None)
                _state["errors"].pop(days, None)
    except Exception as exc:  # noqa: BLE001 (the card keeps the old answer)
        _logger.warning("preset stats: recompute for %d days failed", days, exc_info=True)
        if current():
            with _mu:
                _state["failed"][days] = time.monotonic()
                _state["errors"][days] = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        if current():
            with _mu:
                _state["running"].pop(days, None)


def _spawn(days: int, gen: int) -> None:
    threading.Thread(
        target=_refresh, args=(days, None, gen), name=f"ae-preset-stats-{days}", daemon=True
    ).start()


def overview(days: int) -> dict[str, Any]:
    """The cache for *days* now (``{"state": "warming"}`` with none), refreshing when old.

    With no cache and a recorded failure the answer is ``{"state": "error", "note": ...}``
    (HTTP 200); the card shows the note and keeps polling at its normal rate.
    """
    with _mu:
        loaded = _state["loaded"]
    if not loaded:
        windows = _load_file()
        with _mu:
            if not _state["loaded"]:
                _state["windows"].update(windows)
                _state["loaded"] = True
    start = False
    gen = 0
    with _mu:
        entry = _state["windows"].get(str(days))
        stale = entry is None or time.time() - entry["at"] >= CACHE_TTL_S
        failed = _state["failed"].get(days)
        recently_failed = failed is not None and time.monotonic() - failed < RETRY_S
        running = _state["running"].get(days)
        stuck = running is not None and time.monotonic() - running[0] >= STUCK_S
        if stale and (running is None or stuck) and not recently_failed:
            _state["gen"] += 1
            gen = _state["gen"]
            _state["running"][days] = (time.monotonic(), gen)
            start = True
        error = _state["errors"].get(days)
    if start:
        try:
            _spawn(days, gen)
        except Exception:  # noqa: BLE001
            with _mu:
                _state["running"].pop(days, None)
            _logger.warning("preset stats: could not start the recompute", exc_info=True)
    if entry is None:
        if error:
            return {"state": "error", "days": days, "note": f"Could not read the runs ({error})"}
        return {"state": "warming", "days": days}
    return dict(entry["view"])


def reset() -> None:
    with _mu:
        _state.clear()
        _state.update(_fresh_state())
