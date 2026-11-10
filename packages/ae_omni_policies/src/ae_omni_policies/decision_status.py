"""``GET /v1/ae/decision``: whether the outside judge model is on, and why not (2026-11-07).

One read for the Admin Center's Decision model card and for the MCP server, which hides
``advise_preset`` and ``ask_judge`` unless ``available`` is true. The answer is numbers and
labels only: never the endpoint, the key, the user list or the denylist. Cached for 60 s per
caller, and it never raises: a failed read gives the fields it could fill, plus ``error``.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from typing import Any

from . import agent_judge
from . import decision_advisor as da
from . import decision_shadow as ds

CACHE_S = 60.0
#: The points the card always lists, even before one has been asked (registration is lazy).
#: Only points that call the judge: the lead step-down was dropped after its replay on
#: 2026-11-07 (docs/features/decision-advisor.md), and the review loop stop is a rule.
KNOWN_POINTS = ("agent_ask", "advisor")
RETIRES_ENV = "AE_DECISION_RETIRES"
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def reset() -> None:
    """Forget the cached answers (tests)."""
    _cache.clear()


def reason(caller: str | None, environ: Mapping[str, str] | None = None) -> str | None:
    """Why the advisor would not answer *caller*, or ``None`` when it would."""
    cfg = da.config(environ)
    if not cfg.enabled:
        return "not_enabled"
    if not cfg.endpoint:
        return "no_endpoint"
    if not cfg.api_key:
        return "no_key"
    if not da.load_denylist(cfg.denylist_file):
        return "no_denylist"
    if not cfg.users:
        return "no_users"
    if not caller or caller.lower() not in cfg.users:
        return "not_allowed"
    return None


def _retires(env: Mapping[str, str]) -> str | None:
    raw = (env.get(RETIRES_ENV) or "").strip()
    if not _DATE.fullmatch(raw):
        return None
    try:
        time.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return raw


def _point_names(env: Mapping[str, str]) -> list[str]:
    names = list(KNOWN_POINTS)
    for name in [*ds.point_names(), *ds.ceilings(env)]:
        if name not in names:
            names.append(name)
    return names


def _mode(name: str, why: str | None, env: Mapping[str, str]) -> str:
    """The mode a call would get now, not only once the point has been asked (2026-11-07).

    ``agent_ask`` registers on its first call, so before that the registry did not know its
    live default and the card read Off. The advisor is not a shadow point: it answers
    whenever the model is available to the caller.
    """
    if name == agent_judge.POINT:
        agent_judge.register()
    if name == "advisor":
        return ds.LIVE if why is None else ds.OFF
    return ds.mode_for(name, env)


def _blank() -> dict[str, Any]:
    """The full answer shape with nothing filled in, so a reader never meets a missing field."""
    return {
        "configured": False,
        "set_up": False,
        "enabled": False,
        "available": False,
        "model": None,
        "retires": None,
        "calls_today": 0,
        "daily_cap": 0,
        "paused_until": None,
        "points": [],
        "shadow": {"would_act_7d": 0, "lines_7d": 0},
    }


def build(caller: str | None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The status answer for *caller*, uncached; a failed read gives the fields it could fill."""
    env = os.environ if environ is None else environ
    out = _blank()
    try:
        cfg = da.config(env)
        out["enabled"] = cfg.enabled
        out["model"] = cfg.deployment or None
        out["retires"] = _retires(env)
        out["set_up"] = bool(
            cfg.enabled or cfg.endpoint or cfg.api_key or cfg.denylist_file or cfg.users
        )
        out["configured"] = bool(
            cfg.endpoint and cfg.api_key and da.load_denylist(cfg.denylist_file)
        )
        why = reason(caller, env)
        out["available"] = why is None
        if why is not None:
            out["reason"] = why
        out.update(ds.usage(env))
        out["points"] = [{"name": n, "mode": _mode(n, why, env)} for n in _point_names(env)]
        out["shadow"] = ds.shadow_counts(7, env)
    except Exception as exc:  # noqa: BLE001 (a status read never fails the card)
        out["error"] = type(exc).__name__
    return out


def status(caller: str | None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """:func:`build`, cached for :data:`CACHE_S` seconds per caller."""
    now = time.monotonic()
    key = caller or ""
    hit = _cache.get(key)
    if environ is None and hit and hit[0] > now:
        return hit[1]
    out = build(caller, environ)
    if environ is None:
        _cache[key] = (now + CACHE_S, out)
    return out
