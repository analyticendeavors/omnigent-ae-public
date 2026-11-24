"""Counts and ratios from the Decision-1 shadow log, for ``GET /v1/ae/decision/report``.

Added 2026-11-13. Pure aggregation over :func:`decision_shadow.shadow_path` (the live file and
its rotated ``.1`` copy) inside a window of days. The output holds numbers and enum labels
only: no session id, title, hash or text is ever copied out of a line. Session ids are read
only to join a nudge to the agent question that followed it, and are dropped at once.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from . import decision_shadow as ds

ASK_POINT = "agent_ask"
NUDGE_POINT = "nudge"
#: A nudge counts as followed when the same chat asked within this many seconds.
FOLLOW_S = 120.0
#: ``error_class`` values written for a refused or rate-limited call (never a model answer).
REFUSED_PREFIX = "refused_"
RATE_LIMITED = ("rate_limited", "minute_cap", "http_429", "batch_refused")


def _ts(at: Any) -> float | None:
    if not isinstance(at, str):
        return None
    try:
        return datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except ValueError:
        return None


def _rows(days: int, environ: Mapping[str, str] | None) -> list[tuple[float, dict[str, Any]]]:
    path = ds.shadow_path(environ)
    if path is None:
        return []
    cutoff = ds._now() - days * 86400.0
    out: list[tuple[float, dict[str, Any]]] = []
    for file in (path.with_name(path.name + ".1"), path):
        try:
            with file.open(encoding="utf-8") as handle:  # a line at a time, never the whole file
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    stamp = _ts(row.get("at"))
                    if stamp is not None and stamp >= cutoff:
                        out.append((stamp, row))
        except OSError:
            continue
    out.sort(key=lambda pair: pair[0])
    return out


def _pct(values: list[float], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return int(ordered[max(0, math.ceil(q * len(ordered)) - 1)])


def _label(value: Any) -> str:
    return value if isinstance(value, str) and value else "unknown"


def _features(row: Mapping[str, Any]) -> Mapping[str, Any]:
    feats = row.get("features")
    return feats if isinstance(feats, Mapping) else {}


def report(days: int = 7, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Aggregates for the last *days* days; counts and ratios only."""
    rows = _rows(days, environ)
    asks = [(t, r) for t, r in rows if r.get("point") == ASK_POINT]
    nudges = [(t, r) for t, r in rows if r.get("point") == NUDGE_POINT]

    by_role: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    batches: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    refused: Counter[str] = Counter()
    answered = low = failed = ctx = answered_rows = 0
    latencies: list[float] = []
    ask_times: dict[str, list[float]] = {}
    for stamp, row in asks:
        feats = _features(row)
        by_role[_label(feats.get("role"))] += 1
        session = row.get("session")
        if isinstance(session, str):
            ask_times.setdefault(session, []).append(stamp)  # a refused ask is still an ask
        err = row.get("error_class")
        if isinstance(err, str) and (err.startswith(REFUSED_PREFIX) or err in RATE_LIMITED):
            refused[err] += 1
            continue
        kind = feats.get("kind")
        if isinstance(kind, str):
            by_kind[kind] += 1
        batch = feats.get("batch")
        if isinstance(batch, int) and not isinstance(batch, bool):
            batches[str(batch)] += 1
        answered_rows += 1
        ctx += 1 if feats.get("has_context") is True else 0
        if err:
            failed += 1
            errors[_label(err)] += 1
        elif feats.get("low_confidence") is True or (
            "low_confidence" not in feats and row.get("would_act") is not True
        ):
            low += 1  # the reply's own flag; older lines without it fall back to would_act
        else:
            answered += 1
        lat = row.get("latency_ms")
        if isinstance(lat, int | float) and not isinstance(lat, bool) and lat > 0:
            latencies.append(float(lat))

    moments: Counter[str] = Counter()
    followed: Counter[str] = Counter()
    for stamp, row in nudges:
        moment = _label(_features(row).get("moment"))
        moments[moment] += 1
        session = row.get("session")
        if isinstance(session, str) and any(
            stamp < t <= stamp + FOLLOW_S for t in ask_times.get(session, [])
        ):
            followed[moment] += 1

    usage = ds.usage(environ)
    return {
        "days": days,
        "asks": {
            "total": len(asks),
            "by_role": dict(by_role),
            "by_kind": dict(by_kind),
            "batch_sizes": dict(batches),
            "with_context": round(ctx / answered_rows, 3) if answered_rows else None,
            "answered": answered,
            "low_confidence": low,
            "errors": dict(errors),
            "errors_total": failed,
            "refused": dict(refused),
            "refused_total": sum(refused.values()),
            "latency_ms": {"p50": _pct(latencies, 0.5), "p95": _pct(latencies, 0.95)},
        },
        "nudges": {
            "total": len(nudges),
            "by_moment": dict(moments),
            "followed_by_ask": dict(followed),
            "followed_total": sum(followed.values()),
        },
        # The saved counter (survives a restart), as on GET /v1/ae/decision; usage()'s
        # count is the in-memory cap counter, which a deploy resets (fixed 2026-11-13).
        "calls_today": ds.calls_today(),
        "daily_cap": usage["daily_cap"],
    }
