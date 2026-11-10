"""Decision points that run a judge in shadow first, and switch on one at a time.

Added 2026-11-06. A decision point is one place where a rule decides something and a judge
model could decide it better. Each point has a mode: ``off`` (nothing happens), ``shadow`` (the
judge is asked, the answer is logged next to the rule's baseline, the rule still acts) or
``live`` (the judge acts when it passes the point's threshold). :func:`ask` never raises and
falls back to the baseline on every failure, so a point can never make a turn worse than the
rule it started from.

No decision point is wired into any hook or route yet; the points arrive in a later change.

Modes: ``AE_DECISION_POINTS`` (for example ``agent_ask:off``) is each point's ceiling;
a missing point is off. A promotions file (``AE_DECISION_POINTS_FILE``, a JSON object of point
to mode) may raise a point only up to its ceiling. Without a promotion a point runs in shadow at
most, so ``live`` needs both the ceiling and the owner's promotion. A broken file caps every
point at shadow. ``AE_DECISION_ENABLED`` off means every point is off.

Data rule: a point declares the feature keys it uses and their types or enum values with
:func:`register_point`. A feature that is not declared, or has the wrong type, is dropped and
counted, never logged. The shadow log holds numbers, booleans and enum labels only; a guarded
summary may be sent to the judge but is never written to the log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import decision_advisor as da
from . import preset_runs
from .judge import GuardedSummary, Judge, RuleJudge, ValidatedState, Verdict, valid_probs

_logger = logging.getLogger(__name__)

POINTS_ENV = "AE_DECISION_POINTS"
POINTS_FILE_ENV = "AE_DECISION_POINTS_FILE"
SHADOW_FILE_ENV = "AE_DECISION_SHADOW_FILE"
TIMEOUT_ENV = "AE_DECISION_POINT_TIMEOUT_S"
DAILY_CAP_ENV = "AE_DECISION_DAILY_CAP"
MINUTE_CAP_ENV = "AE_DECISION_MINUTE_CAP"
DATA_DIR = preset_runs.DATA_DIR
POINTS_FILE_NAME = "ae-decision-points.json"
SHADOW_FILE_NAME = "ae-decision-shadow.jsonl"
MAX_BYTES = preset_runs.MAX_BYTES
RETENTION = timedelta(days=30)
PRUNE_EVERY_S = 86400.0

OFF, SHADOW, LIVE = "off", "shadow", "live"
_RANK = {OFF: 0, SHADOW: 1, LIVE: 2}
METHODS = ("yes_no", "choice", "score")

CONCURRENCY = 4
BREAKER_ERRORS = 5
BREAKER_OPEN_S = 600.0
DEFAULT_TIMEOUT_S = 3.0
DEFAULT_DAILY_CAP = 400
DEFAULT_MINUTE_CAP = 30

#: A feature spec: ``bool``, ``int``, ``float``, a tuple of allowed enum strings, or
#: :data:`~ae_omni_policies.judge.GuardedSummary`.
FeatureSpec = Any


@dataclass(frozen=True)
class Point:
    name: str
    kind: str
    features: Mapping[str, FeatureSpec]
    threshold: float = 0.6
    #: State keys are chosen by the caller (any lower-case key; number, boolean or guarded
    #: string values); ``features`` then declares only what the shadow log may keep.
    dynamic: bool = False
    #: The mode when ``AE_DECISION_POINTS`` does not name the point.
    default: str = "off"


_points: dict[str, Point] = {}


def register_point(
    name: str,
    kind: str,
    features: Mapping[str, FeatureSpec],
    *,
    threshold: float = 0.6,
    dynamic: bool = False,
    default: str = "off",
) -> Point:
    """Declare a decision point. *kind* is ``yes_no``, ``choice`` or ``score``."""
    if kind not in METHODS:
        raise ValueError(f"unknown kind {kind!r}")
    point = Point(name, kind, dict(features), threshold, dynamic, default)
    _points[name] = point
    return point


def get_point(name: str) -> Point | None:
    return _points.get(name)


def value_ok(spec: FeatureSpec, value: Any) -> bool:
    """Whether *value* fits *spec*: the only shapes a judge may see."""
    if spec is bool:
        return isinstance(value, bool)
    if spec is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if spec is float:
        return (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        )
    if spec is GuardedSummary:
        return isinstance(value, str) and da.is_guarded(value)
    if isinstance(spec, tuple):
        return isinstance(value, str) and value in spec
    return False


_DYNAMIC_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")


def _dynamic_ok(key: str, value: Any) -> bool:
    if not _DYNAMIC_KEY.fullmatch(key):
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, int | float):
        return math.isfinite(value)
    return isinstance(value, str) and da.is_guarded(value)


def validate_state(point: str, state: Mapping[str, Any]) -> bool:
    """True when *point* is registered and every key and value in *state* is declared and fits."""
    registered = _points.get(point)
    if registered is None:
        return False
    if registered.dynamic:
        return all(_dynamic_ok(k, v) for k, v in state.items())
    return all(
        key in registered.features and value_ok(registered.features[key], value)
        for key, value in state.items()
    )


def filter_state(point: Point, state: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    """``(kept, dropped_count)``: only declared keys with fitting values survive."""
    if point.dynamic:
        kept = {k: v for k, v in state.items() if _dynamic_ok(k, v)}
        return kept, len(state) - len(kept)
    kept = {
        k: v for k, v in state.items() if k in point.features and value_ok(point.features[k], v)
    }
    return kept, len(state) - len(kept)


# ---------------------------------------------------------------------------
# Modes


def _data_dir() -> Path | None:
    return DATA_DIR if DATA_DIR.is_dir() else None


def points_file(environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    if POINTS_FILE_ENV in env:
        raw = (env[POINTS_FILE_ENV] or "").strip()
        return Path(raw) if raw else None
    base = _data_dir()
    return base / POINTS_FILE_NAME if base else None


def shadow_path(environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    if SHADOW_FILE_ENV in env:
        raw = (env[SHADOW_FILE_ENV] or "").strip()
        return Path(raw) if raw else None
    base = _data_dir()
    return base / SHADOW_FILE_NAME if base else None


def ceilings(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Each point's ceiling from ``AE_DECISION_POINTS``; a bad entry is off."""
    env = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for part in (env.get(POINTS_ENV) or "").split(","):
        name, _, mode = part.partition(":")
        name, mode = name.strip(), mode.strip().lower()
        if name:
            out[name] = mode if mode in _RANK else OFF
    return out


_promo_cache: dict[str, Any] = {"key": None, "data": None}


def promotions(environ: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """The promotions file as ``{point: mode}``: ``{}`` when absent, ``None`` when broken.

    Re-read only when the file's mtime or size changes. A file that cannot be read or is not a
    JSON object of strings is broken.
    """
    path = points_file(environ)
    if path is None:
        return {}
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _promo_cache["key"] == key:
        return _promo_cache["data"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        data = (
            {str(k): str(v).lower() for k, v in raw.items()}
            if isinstance(raw, dict) and all(isinstance(v, str) for v in raw.values())
            else None
        )
    except (OSError, ValueError):
        data = None
    _promo_cache.update(key=key, data=data)
    return data


def _lower(a: str, b: str) -> str:
    return a if _RANK[a] <= _RANK[b] else b


def mode_for(point: str, environ: Mapping[str, str] | None = None) -> str:
    """The point's effective mode: ``off``, ``shadow`` or ``live``."""
    env = os.environ if environ is None else environ
    if not da._flag(env.get("AE_DECISION_ENABLED")):
        return OFF
    registered = _points.get(point)
    default = registered.default if registered else OFF
    named = ceilings(env)
    ceiling = named.get(point, default)
    if ceiling == OFF:
        return OFF
    if default == LIVE and ceiling == LIVE:
        # Live by default (owner-approved), implicit or named live: a missing or broken
        # promotions file never lowers it; only a valid entry in the file can.
        want = (promotions(env) or {}).get(point)
        return _lower(want, LIVE) if want in _RANK else LIVE
    capped = _lower(ceiling, SHADOW)  # no promotion, or a broken file: shadow at most
    promoted = promotions(env)
    if promoted is None or point not in promoted:
        return capped
    want = promoted[point]
    if want not in _RANK:
        return capped
    return _lower(want, ceiling)


# ---------------------------------------------------------------------------
# Limits: daily cap, per-minute cap, circuit breaker, concurrency


class _Limits:
    def __init__(self) -> None:
        self.day = ""
        self.day_count = 0
        self.recent: deque[float] = deque()
        self.errors = 0
        self.open_until = 0.0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.sem: asyncio.Semaphore | None = None

    def semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self.sem is None or self.loop is not loop:
            self.sem, self.loop = asyncio.Semaphore(CONCURRENCY), loop
        return self.sem

    def admit(self, environ: Mapping[str, str]) -> str | None:
        """``None`` and count the call, or a short reason it was refused."""
        now_mono = _mono()
        if self.open_until:
            if now_mono < self.open_until:
                return "breaker_open"
            self.open_until, self.errors = 0.0, 0
        day = datetime.fromtimestamp(_now(), UTC).strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.day_count = day, 0
        daily = int(da._number(environ.get(DAILY_CAP_ENV), DEFAULT_DAILY_CAP))
        per_minute = int(da._number(environ.get(MINUTE_CAP_ENV), DEFAULT_MINUTE_CAP))
        while self.recent and now_mono - self.recent[0] >= 60.0:
            self.recent.popleft()
        if self.day_count >= daily:
            return "daily_cap"
        if len(self.recent) >= per_minute:
            return "minute_cap"
        self.day_count += 1
        self.recent.append(now_mono)
        return None

    def record(self, *, failed: bool) -> None:
        if not failed:
            self.errors = 0
            return
        self.errors += 1
        if self.errors >= BREAKER_ERRORS:
            self.open_until = _mono() + BREAKER_OPEN_S


_limits = _Limits()
_now = time.time
_mono = time.monotonic
_tasks: set[asyncio.Task[Any]] = set()


def reset() -> None:
    """Forget limits, caches and registered points (tests)."""
    global _limits
    _limits = _Limits()
    _promo_cache.update(key=None, data=None)
    _points.clear()


def point_names() -> list[str]:
    """Every registered point, in registration order."""
    return list(_points)


def usage(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Today's call count, the daily cap and when an open breaker closes (read only).

    ``paused_until`` is an ISO UTC time while the circuit breaker is open, else ``None``.
    Added 2026-11-07 for the Admin Center's Decision model card.
    """
    env = os.environ if environ is None else environ
    today = datetime.fromtimestamp(_now(), UTC).strftime("%Y-%m-%d")
    lim = _limits
    paused = None
    if lim.open_until and lim.open_until > _mono():
        paused = _iso(_now() + (lim.open_until - _mono()))
    return {
        "calls_today": lim.day_count if lim.day == today else 0,
        "daily_cap": int(da._number(env.get(DAILY_CAP_ENV), DEFAULT_DAILY_CAP)),
        "paused_until": paused,
    }


def shadow_counts(days: int = 7, environ: Mapping[str, str] | None = None) -> dict[str, int]:
    """Lines and would-act lines in the shadow log from the last *days* days.

    Reads the live file and its rotated ``.1`` copy; a missing file counts as zero and a line
    that is not valid JSON is skipped.
    """
    path = shadow_path(environ)
    out = {"would_act_7d": 0, "lines_7d": 0}
    if path is None:
        return out
    cutoff = _iso(_now() - days * 86400.0)
    for file in (path.with_name(path.name + ".1"), path):
        try:
            text = file.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            at = row.get("at") if isinstance(row, dict) else None
            if not isinstance(at, str) or at < cutoff:
                continue
            out["lines_7d"] += 1
            if row.get("would_act") is True:
                out["would_act_7d"] += 1
    return out


# ---------------------------------------------------------------------------
# The shadow log

_lock = threading.Lock()
_last_prune = 0.0
_SAFE = re.compile(r"[A-Za-z0-9_.\-]{1,40}")


def _safe_label(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return round(value, 4) if math.isfinite(value) else None
    if isinstance(value, str) and _SAFE.fullmatch(value):
        return value
    return "?" if value is not None else None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prune(path: Path, now: float) -> None:
    """Drop lines older than :data:`RETENTION` (and any line that is not valid) in place."""
    cutoff = _iso(now - RETENTION.total_seconds())
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    keep: list[str] = []
    for line in lines:
        try:
            at = json.loads(line).get("at")
        except (ValueError, AttributeError):
            continue
        if isinstance(at, str) and at >= cutoff:
            keep.append(line)
    if len(keep) == len(lines):
        return
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("".join(f"{ln}\n" for ln in keep), encoding="utf-8")
    os.replace(tmp, path)


def write_line(entry: dict[str, Any], path: Path | None = None) -> bool:
    """Append one JSON line; rotate past :data:`MAX_BYTES`; prune at rotation or once a day."""
    global _last_prune
    path = path if path is not None else shadow_path()
    if path is None:
        return False
    line = json.dumps(entry, separators=(",", ":"))
    now = _now()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            old = path.with_name(path.name + ".1")
            if path.exists() and path.stat().st_size > MAX_BYTES:
                os.replace(path, old)
                _prune(old, now)
                _last_prune = now
            elif now - _last_prune >= PRUNE_EVERY_S:
                if path.exists():
                    _prune(path, now)
                _last_prune = now
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError as exc:
        _logger.warning("decision shadow log not written (%s): %s", path, type(exc).__name__)
        return False
    return True


# ---------------------------------------------------------------------------
# Asking


@dataclass(frozen=True)
class Outcome:
    """What a point gets back. Use :attr:`value` for the answer to act on."""

    verdict: Verdict | None
    baseline: Any
    would_act: bool
    acted: bool
    mode: str = OFF
    error: str | None = None

    @property
    def value(self) -> Any:
        """The judge's answer when it acted, else the baseline."""
        return self.verdict.answer if self.acted and self.verdict else self.baseline


def _confidence(verdict: Verdict) -> float:
    if verdict.answer is None:
        return 0.0
    if not verdict.probs:  # a rule has no probabilities and is certain of itself
        return 1.0
    key = (
        ("yes" if verdict.answer else "no") if isinstance(verdict.answer, bool) else verdict.answer
    )
    return verdict.probs.get(str(key), 0.0)


def _log_features(point: Point, state: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in state.items() if point.features[k] is not GuardedSummary}


async def ask(
    point: str,
    judge: Judge,
    method: str,
    state: Mapping[str, Any],
    *,
    baseline: Any,
    session: str | None = None,
    environ: Mapping[str, str] | None = None,
    log_features: Mapping[str, Any] | None = None,
    order: Sequence[str] | None = None,
    **args: Any,
) -> Outcome:
    """Ask *judge* at decision point *point*. Never raises; any failure returns the baseline.

    *order* is the option keys or levels in the order the caller gave them; a dynamic point logs
    the answer as an index into it and the probabilities as a list in that order, never the
    names (they are written by the agent).
    """
    mode = OFF
    try:
        env = os.environ if environ is None else environ
        mode = mode_for(point, env)
        registered = _points.get(point)
        if mode == OFF or registered is None:
            return Outcome(None, baseline, False, False, OFF)
        kept, dropped = filter_state(registered, state)
        # Checked once, here, before any wait: the adapter takes it as is.
        checked = ValidatedState(kept)
        shown = kept
        if registered.dynamic:  # the log keeps only the declared summary features
            shown, extra = filter_state(replace(registered, dynamic=False), log_features or {})
            dropped += extra
        error: str | None = None
        verdict: Verdict | None = None
        if method not in METHODS or (not registered.dynamic and method != registered.kind):
            error = "bad_method"
        else:
            error = _limits.admit(env)
        if error is None:
            timeout = da._number(env.get(TIMEOUT_ENV), DEFAULT_TIMEOUT_S)

            async def call() -> Verdict:
                async with _limits.semaphore():  # the permit is released if this is cancelled
                    return await getattr(judge, method)(point, checked, **args)

            try:
                # One deadline for the wait for a permit and the judge call together.
                verdict = await asyncio.wait_for(call(), timeout)
            except TimeoutError:
                error = "timeout"
            except Exception as exc:  # noqa: BLE001 (a decision point never breaks its caller)
                error = type(exc).__name__
            # Only a RuleJudge may answer with certainty and no probabilities; a model verdict
            # with empty, NaN or out-of-range probabilities never reaches a log or a reply.
            rule_certain = (
                isinstance(judge, RuleJudge) and verdict is not None and not verdict.probs
            )
            if verdict is not None and not rule_certain:
                answered = verdict.answer is not None or bool(verdict.probs)
                if answered and not valid_probs(verdict.probs):
                    verdict, error = None, "bad_shape"
            if verdict is not None and verdict.error:
                error = verdict.error
            # bad_state is our bug, not the service failing, so it does not trip the breaker
            _limits.record(failed=error is not None and error != "bad_state")
        failed = error is not None
        would_act = (
            not failed
            and verdict is not None
            and verdict.answer is not None
            and _confidence(verdict) >= registered.threshold
        )
        acted = would_act and mode == LIVE
        _log(
            registered,
            judge,
            mode,
            session,
            shown,
            dropped,
            verdict,
            error,
            would_act,
            acted,
            baseline,
            env,
            order,
        )
        return Outcome(None if failed else verdict, baseline, would_act, acted, mode, error)
    except Exception:  # noqa: BLE001 (never raises)
        return Outcome(None, baseline, False, False, mode)


def _log(
    point: Point,
    judge: Judge,
    mode: str,
    session: str | None,
    kept: Mapping[str, Any],
    dropped: int,
    verdict: Verdict | None,
    error: str | None,
    would_act: bool,
    acted: bool,
    baseline: Any,
    env: Mapping[str, str],
    order: Sequence[str] | None = None,
) -> None:
    try:
        answer: Any
        probs: Any
        if point.dynamic:  # agent-written names: indexes and a list, never the names
            names = list(order or [])
            raw = verdict.answer if verdict else None
            if isinstance(raw, bool):
                answer = 0 if raw else 1  # yes, no
            else:
                answer = names.index(raw) if isinstance(raw, str) and raw in names else None
            probs = [round((verdict.probs if verdict else {}).get(n, 0.0), 3) for n in names]
        else:
            answer = _safe_label(verdict.answer) if verdict else None
            probs = {
                str(_safe_label(k)): round(v, 4)
                for k, v in (verdict.probs if verdict else {}).items()
            }
        entry = {
            "at": _iso(_now()),
            "point": point.name,
            "session": _safe_label(session),
            "judge": _safe_label(getattr(judge, "name", "")),
            "judge_ver": _safe_label(getattr(judge, "version", "")),
            "mode": mode,
            "features": {k: _safe_label(v) for k, v in _log_features(point, kept).items()},
            "answer": answer,
            "probs": probs,
            "threshold": point.threshold,
            "would_act": would_act,
            "acted": acted,
            "baseline": _safe_label(baseline),
            "latency_ms": verdict.latency_ms if verdict else 0,
            "error_class": _safe_label(error),
            "dropped_features": dropped,
        }
        write_line(entry, shadow_path(env))
    except Exception:  # noqa: BLE001 (logging never breaks a decision)
        _logger.warning("decision shadow entry not written")


def fire(
    point: str,
    judge: Judge,
    method: str,
    state: Mapping[str, Any],
    *,
    baseline: Any,
    **kwargs: Any,
) -> asyncio.Task[Any] | None:
    """Run :func:`ask` in the background and return at once; ``None`` with no running loop.

    For hook paths that must never wait. The task is kept in a set until it finishes so it is
    not garbage collected mid-flight. Never raises.
    """
    try:
        task = asyncio.get_running_loop().create_task(
            ask(point, judge, method, state, baseline=baseline, **kwargs)
        )
    except Exception:  # noqa: BLE001 (a hook never fails because a shadow call could not start)
        return None
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
