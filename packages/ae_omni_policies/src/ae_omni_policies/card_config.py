"""Card settings: the one place a reader asks for a value that the Cards panel can also hold.

Why (2026-11-07, card setup PR 2): a new install had to edit ``server.env`` and restart the
stack to set up a card. The panel now saves non-secret settings in the card state file
(:mod:`ae_omni_policies.admin_cards`). The readers (Git, Databricks credit, Azure spend,
Fabric price) ask :func:`value` instead of the environment.

**The environment always wins** (decided 2026-11-07): a variable that is set and not blank is
used as it is and the panel shows that field read only. A server configured by hand therefore
changes nothing when it upgrades; the saved value is used only when the environment has none.

A saved value is honoured only for a field the catalog marks editable, and only when the caller
reads the process environment (``environ`` omitted, or ``os.environ`` itself). A caller that
passes its own mapping, as the tests do, gets exactly that mapping.

This module also validates what the panel sends (:func:`normalize`) and drops a card's cached
answer after a save (:func:`invalidate`). Error text states the rule and quotes at most
:data:`SHOWN_MAX` characters of the offending part, so a pasted token or a huge value is never
echoed back.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Mapping
from datetime import date

from ae_omni_policies import admin_cards

_logger = logging.getLogger(__name__)

VALUE_MAX = 2_000
LIST_MAX = 50
SHOWN_MAX = 60
"""How much of a bad item an error quotes; far under the 200-character echo limit."""

_REPO = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_PATH = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_WORKFLOW = re.compile(r"^[A-Za-z0-9._-]{1,100}\.ya?ml$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_BAD_REF = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]")


class InvalidValue(ValueError):
    """A value the field cannot take; the message states the rule, never the whole value."""


def _shown(text: str) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= SHOWN_MAX else text[:SHOWN_MAX] + "..."


# ── reading ───────────────────────────────────────────────────────────────────────────────────


def value(env_name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The environment value when set and not blank, else the value saved in the panel, else None.

    The environment always wins (see the module note). A damaged state file answers ``None``.
    """
    env = os.environ if environ is None else environ
    raw = env.get(env_name)
    if raw is not None and raw.strip():
        return raw
    if env is not os.environ:
        return None
    card_id = admin_cards.EDITABLE.get(env_name)
    if card_id is None:
        return None
    return admin_cards.store().saved_value(card_id, env_name)


# ── validating ────────────────────────────────────────────────────────────────────────────────


def _items(raw: str) -> list[str]:
    seen: dict[str, None] = {}
    for part in raw.split(","):
        item = part.strip()
        if item:
            seen.setdefault(item, None)
    items = list(seen)
    if len(items) > LIST_MAX:
        raise InvalidValue(f"has {len(items)} items; at most {LIST_MAX} are allowed")
    return items


def _repo(item: str) -> None:
    owner, _, name = item.partition("/")
    if not _REPO.match(item) or set(name) <= {"."}:
        raise InvalidValue(f"{_shown(item)!r} is not owner/repo (letters, digits, dot, dash)")
    if owner.startswith("-") or owner.endswith("-"):
        raise InvalidValue(f"{_shown(item)!r} is not a valid GitHub owner")


def _ref(branch: str) -> bool:
    """Git's ref-name rules for a branch (git check-ref-format, trimmed to what a name needs)."""
    if not branch or len(branch) > 200 or branch == "@" or _BAD_REF.search(branch):
        return False
    if branch.startswith(("/", "-")) or branch.endswith(("/", ".", ".lock")):
        return False
    if ".." in branch or "@{" in branch or "//" in branch:
        return False
    return not any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))


def _repo_list(raw: str) -> str:
    items = _items(raw)
    for item in items:
        _repo(item)
    return ",".join(items)


def _keep_branches(raw: str) -> str:
    items = _items(raw)
    for item in items:
        repo, sep, branch = item.partition(":")
        if not sep:
            raise InvalidValue(f"{_shown(item)!r} is not owner/repo:branch")
        _repo(repo)
        if not _ref(branch):
            raise InvalidValue(f"the branch in {_shown(item)!r} is not a valid git branch name")
    return ",".join(items)


def _sweep_config(raw: str) -> str:
    repo, sep, path = raw.partition(":")
    if not sep:
        raise InvalidValue("is not owner/repo:path")
    _repo(repo)
    if path.startswith("/") or ".." in path.split("/") or not _PATH.match(path):
        raise InvalidValue(
            "the path must be relative, with no '..', using letters, digits, dot, dash and slash"
        )
    if "" in path.split("/"):
        raise InvalidValue("the path has an empty folder name")
    return f"{repo}:{path}"


def _subscriptions(raw: str) -> str:
    items = _items(raw)
    out: list[str] = []
    for item in items:
        sid, sep, name = item.partition("=")
        sid, name = sid.strip(), name.strip()
        if not _GUID.match(sid):
            raise InvalidValue(f"{_shown(item)!r} does not start with a subscription id (a GUID)")
        if sep and (not name or len(name) > 100 or re.search(r"[\x00-\x1f\x7f]", name)):
            raise InvalidValue(f"the name in {_shown(item)!r} must be 1 to 100 plain characters")
        out.append(f"{sid.lower()}={name}" if sep else sid.lower())
    return ",".join(out)


def _decimal(raw: str, *, low: float, high: float, inclusive_high: bool, label: str) -> str:
    if not re.match(r"^\d{1,9}(?:\.\d{1,4})?$", raw):
        raise InvalidValue(f"must be a plain number such as 100 or 0.18, {label}")
    number = float(raw)
    if (
        not math.isfinite(number)
        or number <= low
        or number > high
        or (not inclusive_high and number >= high)
    ):
        raise InvalidValue(f"must be a number {label}")
    return raw


def _usd(raw: str) -> str:
    return _decimal(
        raw, low=0, high=10_000_000, inclusive_high=True, label="above 0 and at most 10,000,000"
    )


def _rate(raw: str) -> str:
    return _decimal(raw, low=0, high=100, inclusive_high=False, label="above 0 and under 100")


def _day(raw: str) -> str:
    if not _DATE.match(raw):
        raise InvalidValue("must be a date written YYYY-MM-DD")
    try:
        date.fromisoformat(raw)
    except ValueError:
        raise InvalidValue("is not a real calendar date (use YYYY-MM-DD)") from None
    return raw


def _cadence(raw: str) -> str:
    lowered = raw.lower()
    if lowered not in {"monthly", "once"}:
        raise InvalidValue("must be monthly or once")
    return lowered


def _workflow(raw: str) -> str:
    if not _WORKFLOW.match(raw):
        raise InvalidValue("must be a workflow file name ending in .yml or .yaml")
    return raw


def _flag(raw: str) -> str:
    lowered = raw.lower()
    if lowered in {"1", "true", "on", "yes"}:
        return "1"
    if lowered in {"0", "false", "off", "no"}:
        return "0"
    raise InvalidValue("must be on or off")


_RULES = {
    "AE_GIT_SWEEP_CONFIG": _sweep_config,
    "AE_GIT_SWEEP_WORKFLOW": _workflow,
    "AE_GIT_REPOS": _repo_list,
    "AE_GIT_VIEW_ONLY": _repo_list,
    "AE_GIT_KEEP_BRANCHES": _keep_branches,
    "AE_DATABRICKS_CREDIT_USD": _usd,
    "AE_DATABRICKS_CREDIT_CADENCE": _cadence,
    "AE_DATABRICKS_CREDIT_START": _day,
    "AE_DATABRICKS_CREDIT_END": _day,
    "AE_AZURE_COST_SUBSCRIPTIONS": _subscriptions,
    "AE_FABRIC_USD_PER_CU_HOUR": _rate,
}
"""One validator per editable variable; a catalog test pins that every editable field has one."""

_BOOL_KIND = _flag


def normalize(item: admin_cards.Field_, raw: str) -> str | None:
    """The value to save for *item*, or ``None`` for a blank one (which clears it).

    :raises InvalidValue: With a message that states the rule and quotes little of *raw*.
    """
    text = raw.strip()
    if not text:
        return None
    if len(text) > VALUE_MAX:
        raise InvalidValue(f"is {len(text):,} characters; the limit is {VALUE_MAX:,}")
    rule = _RULES.get(item.env) or (_BOOL_KIND if item.kind == "bool" else None)
    if rule is None:
        raise InvalidValue("has no rule here, so it cannot be changed")
    return rule(text)


def check_credit_window(start: str | None, end: str | None) -> None:
    """The credit's first day must not be after its last (both effective values)."""
    if start and end and start > end:
        raise InvalidValue("the credit start must not be after the credit end")


# ── dropping cached answers ───────────────────────────────────────────────────────────────────


def invalidate(card_id: str) -> None:
    """Drop the card's cached answer so the next read uses the value just saved.

    Imports are local: the readers import this module for :func:`value`.
    """
    if card_id == "git":
        from ae_omni_policies import git_center

        git_center.invalidate()
    elif card_id == "credit":
        from ae_omni_policies import databricks

        databricks.invalidate()
    elif card_id == "azure":
        from ae_omni_policies import azure_cost

        azure_cost.invalidate()
    elif card_id == "compute":
        from ae_omni_policies import fabric

        fabric.invalidate_rate()
    else:
        _logger.debug("ae cards: %s has no cached answer to drop", card_id)


__all__ = [
    "InvalidValue",
    "check_credit_window",
    "invalidate",
    "normalize",
    "value",
]
