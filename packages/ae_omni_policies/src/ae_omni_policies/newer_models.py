"""Preset rows whose model has a newer version in the same family (model nudge, 2026-10-26).

Read-only. :func:`find` is pure: it takes the presets and the host catalogs and returns the
rows that could move to a newer model of the same family. Nothing here changes a preset; the
Admin Center shows the list, and the preset review turns it into suggestions a person applies.

Families:

* Claude ``opus``, ``sonnet``, ``haiku`` and ``fable``, parsed from ids such as
  ``claude-opus-5-5`` (major, then minor; a date suffix such as ``-20250514`` is not a version).
* Codex by the words after the generation number (``gpt-6-astra``: ``astra``; ``gpt-5-astra-mini``:
  ``astra-mini``, its own family) and the generation (``gpt-6-astra`` beats ``gpt-5.6-astra``).

A row set to Auto, a session model, or an id that does not parse is skipped. A row on a variant
id (``claude-opus-5-5[1m]``) moves only to the same variant of the newest model; when the catalog
lists none, the row is reported as not moved.

The catalog comes from ``GET /v1/hosts/<host>/harnesses/<harness>/model-options`` on the online
hosts the caller can see. ``claude-native`` answers with each family's newest model (``model``
holds the full id, ``id`` an alias such as ``opus``); ``codex-native`` answers with ``id`` and
``displayName``. Hosts and harnesses are asked at the same time, the answer is cached by host set,
and any failure or timeout reads as "no catalog", never an error.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx

from . import role_prefs

_logger = logging.getLogger("ae.newer_models")

CACHE_SECONDS = 600.0
CACHE_MAX = 8
MAX_HOSTS = 3
REQUEST_TIMEOUT_S = 4.0
OVERALL_TIMEOUT_S = 5.0
CLAUDE_HARNESSES = ("claude-native", "claude-sdk")
CODEX_HARNESSES = ("codex-native", "codex")

_CLAUDE = re.compile(
    r"(?:^|[-_./])(fable|opus|sonnet|haiku)[-_ ](\d{1,2})(?:[-_.](\d{1,2}))?(?=$|[-_\[\s])",
    re.IGNORECASE,
)
_CODEX = re.compile(
    r"(?:^|[-_./ ])gpt[-_ ](\d{1,2}(?:\.\d{1,2})*)[-_ ]([a-z]+(?:[-_ ][a-z]+)*)(?=$|\[|[-_ ]\d)",
    re.IGNORECASE,
)
_SUFFIX = re.compile(r"\[[^\]]{1,20}\]$")

Version = tuple[int, ...]
Parsed = tuple[str, str, Version]  # (vendor, family, version)


def parse(model: Any) -> Parsed | None:
    """``(vendor, family, version)`` for a model id or label, or None when it does not parse."""
    if not isinstance(model, str):
        return None
    text = model.strip()
    claude = _CLAUDE.search(text)
    if claude:
        major = int(claude.group(2))
        minor = int(claude.group(3)) if claude.group(3) is not None else 0
        return ("claude", claude.group(1).lower(), (major, minor))
    codex = _CODEX.search(text)
    if codex:
        version = tuple(int(part) for part in codex.group(1).split("."))
        family = re.sub(r"[-_ ]+", "-", codex.group(2).lower())
        return ("codex", family, version)
    return None


def split_suffix(model: str) -> tuple[str, str]:
    """``("claude-opus-5-5", "[1m]")`` for ``claude-opus-5-5[1m]``; the suffix is "" when none."""
    match = _SUFFIX.search(model)
    return (model[: match.start()], match.group(0)) if match else (model, "")


def _norm(version: Version) -> Version:
    """*version* with trailing zeros dropped, so ``6.0.0``, ``6.0`` and ``6`` are equal."""
    parts = list(version)
    while parts and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _newer(candidate: Version, current: Version) -> bool:
    return _norm(candidate) > _norm(current)


def _same(a: Version, b: Version) -> bool:
    return _norm(a) == _norm(b)


def name_of(model: str, display: str | None = None) -> str:
    """A short label: ``Opus 5.5``, ``GPT-6-Astra``. The catalog's own name wins for Codex."""
    parsed = parse(model)
    if parsed is None:
        return model
    vendor, family, version = parsed
    if vendor == "claude":
        return f"{family.capitalize()} {version[0]}.{version[1] if len(version) > 1 else 0}"
    if display and display.strip():
        return display.strip()
    words = "-".join(w.capitalize() for w in family.split("-"))
    return f"GPT-{'.'.join(str(p) for p in version)}-{words}"


def _strings(entry: dict[str, Any]) -> list[str]:
    """The id strings a catalog entry answers to, ``model`` first."""
    return [s for s in (entry.get("model"), entry.get("id")) if isinstance(s, str) and s]


def newest(catalog: Iterable[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """The newest catalog entry per ``(vendor, family)``.

    ``{"model", "base", "name", "version", "ids"}``: ``ids`` holds every id string of the
    entries at that version, so a variant (``[1m]``) can be checked against what is listed.
    An alias such as ``opus`` never parses and is never returned as a model; the display name
    is read for Codex entries only.
    """
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in catalog:
        if not isinstance(entry, dict) or entry.get("hidden") is True:
            continue
        strings = _strings(entry)
        parsed = None
        model = None
        for candidate in strings:
            parsed = parse(candidate)
            if parsed is not None:
                model = candidate
                break
        if parsed is None and strings:
            from_name = parse(entry.get("displayName"))
            if from_name is not None and from_name[0] == "codex":
                parsed, model = from_name, strings[0]
        if parsed is None or model is None:
            continue
        vendor, family, version = parsed
        key = (vendor, family)
        held = best.get(key)
        if held is not None and _same(version, held["version"]):
            held["ids"].update(strings)
        elif held is None or _newer(version, held["version"]):
            best[key] = {
                "model": model,
                "base": split_suffix(model)[0],
                "name": name_of(model, entry.get("displayName")),
                "version": version,
                "ids": set(strings),
            }
    return best


def _slots(preset: role_prefs.Preset) -> Iterable[tuple[str, str, Any]]:
    """``(role label, change path, slot)`` for each slot that names a model."""
    roles = preset.roles
    for role in ("planner", "builder", "helper", "lead"):
        slot = getattr(roles, role)
        if getattr(slot, "mode", None) == "fixed" or (
            getattr(slot, "mode", None) == "agent" and getattr(slot, "model", None)
        ):
            yield role.capitalize(), f"roles.{role}", slot
    reviewer = roles.reviewer
    mode = getattr(reviewer, "mode", None)
    if mode == "fixed":
        yield "Reviewer", "roles.reviewer", reviewer
    elif mode == "multi":
        count = len(reviewer.reviewers)
        for index, slot in enumerate(reviewer.reviewers):
            if getattr(slot, "model", None):
                label = "Reviewer" if count == 1 else f"Reviewer {index + 1}"
                yield label, f"roles.reviewer.reviewers.{index}", slot


def _target(top: dict[str, Any], suffix: str) -> str | None:
    """The newest model's id for a row whose id carries *suffix* ("" for none), or None."""
    if suffix:
        wanted = top["base"] + suffix
        return wanted if wanted in top["ids"] else None
    for candidate in sorted(top["ids"], key=lambda s: (split_suffix(s)[1] != "", s)):
        if not split_suffix(candidate)[1] and parse(candidate) is not None:
            return candidate
    return None


def find(
    presets: Iterable[role_prefs.Preset],
    catalog: Iterable[dict[str, Any]],
    not_moved: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The preset rows with a newer model in the same family, one dict per preset.

    Each preset is ``{"id", "title", "rows": [...]}``; a row is ``{"role", "path", "family",
    "current", "current_name", "newer", "newer_name", "effort", "value"}`` where ``value`` is
    the whole slot with only the model changed (what a change at ``path`` would write).
    Presets with no such row are left out. An empty catalog gives an empty list.

    :param not_moved: when given, rows that have a newer model but no matching variant are
        appended as ``{"preset", "title", "role", "reason"}`` and left out of the answer.
    """
    best = newest(catalog)
    if not best:
        return []
    out: list[dict[str, Any]] = []
    for preset in presets:
        rows: list[dict[str, Any]] = []
        for role, path, slot in _slots(preset):
            current = slot.model
            parsed = parse(current)
            if parsed is None:
                continue
            vendor, family, version = parsed
            top = best.get((vendor, family))
            if top is None or not _newer(top["version"], version):
                continue
            suffix = split_suffix(current)[1]
            target = _target(top, suffix)
            if target is None:
                if not_moved is not None:
                    why = (
                        f"{top['name']} has no {suffix} variant in the catalog"
                        if suffix
                        else (f"{top['name']} has no plain id in the catalog")
                    )
                    not_moved.append(
                        {"preset": preset.id, "title": preset.title, "role": role, "reason": why}
                    )
                continue
            value = slot.model_dump(mode="json")
            value["model"] = target
            rows.append(
                {
                    "role": role,
                    "path": path,
                    "family": family,
                    "current": current,
                    "current_name": name_of(current),
                    "newer": target,
                    "newer_name": top["name"],
                    "effort": getattr(slot, "effort", None),
                    "value": value,
                }
            )
        if rows:
            out.append({"id": preset.id, "title": preset.title, "rows": rows})
    return out


def summary(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per newer model: ``{"model", "name", "from": [names], "presets": N}``."""
    grouped: dict[str, dict[str, Any]] = {}
    for preset in found:
        for row in preset["rows"]:
            entry = grouped.setdefault(
                row["newer"],
                {"model": row["newer"], "name": row["newer_name"], "from": [], "_ids": set()},
            )
            if row["current_name"] not in entry["from"]:
                entry["from"].append(row["current_name"])
            entry["_ids"].add(preset["id"])
    return [
        {"model": e["model"], "name": e["name"], "from": e["from"], "presets": len(e["_ids"])}
        for e in grouped.values()
    ]


# -- the review's suggestions


def states_for(owner: str, roles: Any) -> dict[str, list[role_prefs.Preset]]:
    """The presets the review compares: the owner's own.

    Another user's presets are not compared even in an admin run: the catalog read here is the
    caller's hosts, and a model those hosts list may not run on the other user's.
    """
    return {owner: list(roles.get(owner).presets)}


def for_users(
    states: dict[str, list[role_prefs.Preset]],
    catalog: list[dict[str, Any]],
    not_moved: list[dict[str, Any]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """:func:`find` per user, users with nothing newer left out."""
    found = {user: find(presets, catalog, not_moved) for user, presets in states.items()}
    return {user: rows for user, rows in found.items() if rows}


def _run_note(runs: list[dict[str, Any]], owner: str, preset_id: str) -> str:
    mine = [r for r in runs if r.get("preset") == preset_id and r.get("user") == owner]
    if not mine:
        return ""
    trouble = sum(1 for r in mine if r.get("raises") or r.get("step_downs") or r.get("hung_roles"))
    word = "run" if len(mine) == 1 else "runs"
    return (
        f" {len(mine)} {word} on this preset in the last 14 days, "
        f"{trouble} with a raise, step-down or hang."
    )


def step_items(
    found: dict[str, list[dict[str, Any]]],
    *,
    runs: list[dict[str, Any]] | None = None,
    records: Iterable[dict[str, Any]] = (),
    starter_keys: Iterable[tuple[str, str]] = (),
) -> list[dict[str, Any]]:
    """One ``own`` suggestion per preset row family step, as ``Item`` dicts.

    Ordered round-robin by owner (each owner's first step, then each owner's second, and so
    on), so one user cannot take every slot. Skips a row any stored suggestion already moves
    to the same model (open, dismissed or otherwise: a dismissed step is not filed again), and
    a preset that still matches its shipped starter, keyed by ``(owner, preset id)``.

    :param records: every stored suggestion, not only the newest.
    """
    taken = {
        (r.get("owner"), r.get("preset_id"), c.get("path"), (c.get("value") or {}).get("model"))
        for r in records
        for c in r.get("changes") or []
        if isinstance(c, dict) and isinstance(c.get("value"), dict)
    }
    skip = set(starter_keys)
    per_owner: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for owner in sorted(found):
        for preset in sorted(found[owner], key=lambda p: p["id"]):
            if (owner, preset["id"]) in skip:
                continue
            for row in preset["rows"]:
                if (owner, preset["id"], row["path"], row["newer"]) in taken:
                    continue
                effort = row["effort"]
                kept = (
                    f"Effort stays {effort.capitalize()}." if effort else "Effort stays as it is."
                )
                why = (
                    "A newer model in the same family is in the catalog. "
                    + kept
                    + _run_note(runs or [], owner, preset["id"])
                )
                per_owner[owner].append(
                    {
                        "kind": "own",
                        "owner": owner,
                        "preset_id": preset["id"],
                        "title": (
                            f"Move {row['role']} on {preset['title']} "
                            f"from {row['current_name']} to {row['newer_name']}"
                        )[:120],
                        "why": why[:800],
                        "saving": "Newer model in the same family; nothing else changes.",
                        "changes": [{"path": row["path"], "value": row["value"]}],
                    }
                )
    out: list[dict[str, Any]] = []
    queues = [per_owner[o] for o in sorted(per_owner)]
    for rank in range(max((len(q) for q in queues), default=0)):
        out.extend(q[rank] for q in queues if rank < len(q))
    return out


# -- the host catalogs

_cache: dict[tuple[str, ...], tuple[float, list[dict[str, Any]]]] = {}
_inflight: dict[tuple[int, tuple[str, ...]], asyncio.Task[list[dict[str, Any]]]] = {}


def reset_cache() -> None:
    _cache.clear()
    _inflight.clear()


async def _models(http: httpx.AsyncClient, host: str, harness: str) -> list[dict[str, Any]]:
    try:
        response = await http.get(
            f"/v1/hosts/{host}/harnesses/{harness}/model-options", timeout=REQUEST_TIMEOUT_S
        )
        if response.status_code != 200:
            return []
        models = response.json().get("models")
    except (httpx.HTTPError, ValueError, AttributeError):
        return []
    return [m for m in models if isinstance(m, dict)] if isinstance(models, list) else []


ClientFactory = Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]]


async def _read_all(
    http: httpx.AsyncClient, hosts: list[str], factory: ClientFactory | None = None
) -> list[dict[str, Any]]:
    """Read every host's model options. With *factory* the read opens and closes its own client,
    so it can outlive the request that started it."""
    if factory is not None:
        async with factory() as own:
            return await _read_all(own, hosts)
    names = (*CLAUDE_HARNESSES, *CODEX_HARNESSES)
    answers = await asyncio.gather(*(_models(http, h, n) for h in hosts for n in names))
    return [m for models in answers for m in models]


async def fetch_catalog(
    http: httpx.AsyncClient,
    *,
    now: float | None = None,
    factory: ClientFactory | None = None,
) -> list[dict[str, Any]]:
    """Every model the caller's online hosts list.

    Hosts and harnesses are read at the same time. The answer is cached for
    :data:`CACHE_SECONDS` by host set (the catalog belongs to the hosts, not the user), only when
    it is not empty, and at most :data:`CACHE_MAX` sets are kept. Callers that arrive while a
    read is out share it. Fails soft: any error gives ``[]``.

    :param factory: opens a client for the shared read. Give one whenever the caller's own
        client closes with its request: the read then finishes (and warms the cache) after a
        caller that gave up waiting.
    """
    clock = time.monotonic() if now is None else now
    try:
        response = await http.get("/v1/hosts", timeout=REQUEST_TIMEOUT_S)
        hosts = response.json().get("hosts") if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError, AttributeError):
        return []
    online = sorted(
        {
            str(h["host_id"])
            for h in (hosts or [])
            if isinstance(h, dict) and h.get("status") == "online" and h.get("host_id")
        }
    )[:MAX_HOSTS]
    if not online:
        return []
    key = tuple(online)
    held = _cache.get(key)
    if held is not None and clock - held[0] < CACHE_SECONDS:
        return held[1]
    flight = (id(asyncio.get_running_loop()), key)
    task = _inflight.get(flight)
    if task is None:
        task = asyncio.ensure_future(_read_and_keep(http, online, factory, key, clock))
        _inflight[flight] = task
        task.add_done_callback(lambda _t, f=flight: _inflight.pop(f, None))
    try:
        return await asyncio.shield(task)
    except Exception:  # noqa: BLE001 (no catalog is a quiet answer, never an error)
        _logger.warning("could not read the host model catalogs", exc_info=True)
        return []


async def _read_and_keep(
    http: httpx.AsyncClient,
    hosts: list[str],
    factory: ClientFactory | None,
    key: tuple[str, ...],
    clock: float,
) -> list[dict[str, Any]]:
    """The shared read; it fills the cache itself, so a caller that gave up still warms it."""
    models = await _read_all(http, hosts, factory)
    if models:
        _cache[key] = (clock, models)
        while len(_cache) > CACHE_MAX:
            del _cache[min(_cache, key=lambda k: _cache[k][0])]
    return models


async def catalog_soft(
    http: httpx.AsyncClient,
    factory: ClientFactory | None = None,
    *,
    timeout: float = OVERALL_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """:func:`fetch_catalog` with an overall wait: ``[]`` on timeout or any failure."""
    try:
        return await asyncio.wait_for(fetch_catalog(http, factory=factory), timeout)
    except TimeoutError:
        _logger.warning("the host model catalogs did not answer in %s s", timeout)
    except Exception:  # noqa: BLE001 (a hint must never break the page)
        _logger.warning("could not read the host model catalogs", exc_info=True)
    return []


__all__ = [
    "CACHE_SECONDS",
    "catalog_soft",
    "fetch_catalog",
    "find",
    "for_users",
    "name_of",
    "newest",
    "parse",
    "reset_cache",
    "split_suffix",
    "states_for",
    "step_items",
    "summary",
]
