"""The rules for a lead changing a session's model or effort (2026-10-24, PR 4).

Pure logic, no I/O: ``router.py`` reads the sessions and applies the change, this
module only says what the rules allow.

A move is a **raise** when the model family gets bigger (haiku < sonnet < opus <
fable) or the family stays and the effort goes up (low < medium < high < xhigh <
max < ultra). A bigger family with a lower effort is still a raise. A **lower** move is the
reverse. **Same** means nothing changes. Two ids that cannot be ranked against each
other (an unknown family, or a new version of one family) and an equal effort count
as a raise: the safe reading is the one that asks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from ae_omni_policies import model_log

EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max", "ultra")
FAMILIES: tuple[str, ...] = ("haiku", "sonnet", "opus", "fable")
# A session on its agent's default effort ranks as medium, for comparison only.
DEFAULT_EFFORT = "medium"

Kind = Literal["raise", "lower", "same"]
Status = Literal["changed", "needs_approval", "refused", "unchanged"]
Approved = Literal["auto", "owner", "user"]

_FAMILY = re.compile(r"(haiku|sonnet|opus|fable)", re.IGNORECASE)
# Used when the preset sets no rules (lead-picks): ask about everything, never step down.
DEFAULT_RULES: dict[str, Any] = {
    "raise_or_lower": "ask",
    "lead_steps_down": False,
    "auto_raises": 0,
}


def family(model: str | None) -> str | None:
    """The family name in a catalog id (``claude-opus-5-5[1m]`` gives ``opus``), else ``None``."""
    match = _FAMILY.search(model or "")
    return match.group(1).lower() if match else None


def _effort_rank(effort: str | None) -> int:
    value = effort if effort in EFFORTS else DEFAULT_EFFORT
    return EFFORTS.index(value)


def classify(
    old_model: str | None, old_effort: str | None, new_model: str | None, new_effort: str | None
) -> Kind:
    """Whether moving from the old values to the new ones is a raise, a lower move or no move."""
    if (old_model or None) == (new_model or None) and (old_effort or None) == (new_effort or None):
        return "same"
    old_family, new_family = family(old_model), family(new_model)
    if new_family and not old_family and (old_model or None) != (new_model or None):
        # No model reported yet (or an unranked one): a known family cannot be ranked below it.
        return "raise"
    if old_family and new_family and old_family != new_family:
        return "raise" if FAMILIES.index(new_family) > FAMILIES.index(old_family) else "lower"
    old_rank, new_rank = _effort_rank(old_effort), _effort_rank(new_effort)
    if new_rank != old_rank:
        return "raise" if new_rank > old_rank else "lower"
    return "raise"


def start_value(model: str | None, effort: str | None) -> str:
    """The ``ae.model_start`` label value: ``"<model>|<effort>"``, an empty part when unset."""
    return f"{model or ''}|{effort or ''}"


def parse_start(value: str | None) -> tuple[str | None, str | None] | None:
    """The ``(model, effort)`` in an ``ae.model_start`` value, or ``None`` when unset or bad."""
    if not value or "|" not in value:
        return None
    model, _, effort = value.partition("|")
    return (model or None, effort or None)


LAST_CAP = 256
REASON_CAP = 80
_KIND_SHORT = {"raise": "r", "lower": "l"}
_KIND_LONG = {"r": "raise", "l": "lower"}
_APPROVED_SHORT = {"auto": "a", "owner": "r", "user": "u"}
_APPROVED_LONG = {"a": "auto", "r": "owner", "u": "user"}


def last_change_label(
    *,
    old: tuple[str | None, str | None],
    new: tuple[str | None, str | None],
    kind: str,
    reason: str,
    at: int,
    caller: str | None,
    approved: str | None,
    undo: tuple[str | None, str | None],
    undone: bool,
) -> str | None:
    """The ``ae.model_last`` value (compact short-key JSON under :data:`LAST_CAP`), or ``None``.

    Label values are capped at 256, so keys are one letter, pairs are arrays and an undo
    equal to the old pair is left out;
    :func:`parse_last` expands them. A long ``reason`` is cut until the value fits, and
    the caller goes only when the value is still too long with no reason left. A value that
    still does not fit is ``None``: the store would cut it into invalid JSON, and dropping the
    old or undo pair would let an Undo clear the override instead of restoring it.
    """
    cut = min(len(reason), REASON_CAP)
    while True:
        data: dict[str, Any] = {
            "t": list(new),
            "k": _KIND_SHORT.get(kind, kind),
            "r": reason[:cut],
            "a": at,
            "c": caller,
            "p": _APPROVED_SHORT.get(approved or "", approved),
        }
        # Undo usually equals the old pair; leaving it out saves the room a long reason needs.
        data["f"] = list(old)
        if tuple(undo) != tuple(old):
            data["u"] = list(undo)
        if undone:
            data["x"] = 1
        value = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        if len(value) <= LAST_CAP:
            return value
        if cut > 0:
            cut = max(0, cut - max(1, len(value) - LAST_CAP))
        elif caller is not None:
            caller = None
        else:
            return None


def _pair(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return {"model": value.get("model"), "effort": value.get("effort")}
    if isinstance(value, list) and len(value) == 2:
        return {"model": value[0], "effort": value[1]}
    return None


def parse_last(value: str | None) -> dict[str, Any] | None:
    """The contract object in an ``ae.model_last`` value, or ``None`` when unset or unusable.

    Reads the short-key form, and the long-key form as a safety net.
    """
    if not value:
        return None
    try:
        data = json.loads(value)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    short = "t" in data
    at = data.get("a" if short else "at")
    if isinstance(at, bool) or not isinstance(at, int | float):
        return None
    # A label that dropped its old pair to fit reads as an unknown one.
    unknown = {"model": None, "effort": None}
    old = (_pair(data.get("f")) or unknown) if short else _pair(data.get("from"))
    new = _pair(data.get("t" if short else "to"))
    if old is None or new is None:
        return None
    if not short:
        return data | {"from": old, "to": new, "undo": _pair(data.get("undo"))}
    return {
        "from": old,
        "to": new,
        "kind": _KIND_LONG.get(data.get("k"), data.get("k")),
        "reason": data.get("r") or "",
        "at": at,
        "caller": data.get("c"),
        "approved": model_log.normalize_approved(_APPROVED_LONG.get(data.get("p"), data.get("p"))),
        "undo": _pair(data["u"]) if "u" in data else old,
        "undone": bool(data.get("x")),
    }


@dataclass(frozen=True)
class Decision:
    """What the rules say: the status, whether it counts, and who approved it."""

    status: Status
    counted: bool = False
    approved: Approved | None = None
    refusal: str | None = None


def decide(
    rules: dict[str, Any],
    *,
    kind: Kind,
    is_lead: bool,
    raises: int,
    start_kind: Kind | None,
    codex: bool,
    model_changes: bool,
    confirmed: bool,
    agent_unknown: bool = False,
) -> Decision:
    """Apply the preset's rules to one move.

    :param rules: ``raise_or_lower``, ``lead_steps_down`` and ``auto_raises``.
    :param kind: The move, from :func:`classify`.
    :param is_lead: The target is the lead itself.
    :param raises: Raises already counted on the target.
    :param start_kind: For the lead, the move from its start value to the new one
        (``lower`` or ``same`` means it is going back to where the chat started).
    :param codex: The target is a Codex child.
    :param model_changes: The request changes the model, not only the effort.
    :param confirmed: The owner already approved this exact move.
    :param agent_unknown: The target's agent could not be read, so it might be Codex.
    """
    if kind == "same":
        return Decision("unchanged")
    if codex and model_changes:
        return Decision(
            "refused",
            refusal="A Codex helper has one model on this box; only its effort can change.",
        )
    if agent_unknown and model_changes:
        return Decision(
            "refused",
            refusal="The session's agent could not be read, so only its effort can change now.",
        )
    rule = rules.get("raise_or_lower")
    if rule == "off":
        return Decision(
            "refused", refusal="Your preset does not let the lead change a model or effort."
        )
    if is_lead and kind == "lower" and not rules.get("lead_steps_down"):
        return Decision("refused", refusal="Your preset does not let the lead step itself down.")
    if is_lead and kind == "raise" and start_kind in ("lower", "same"):
        return Decision("changed", approved="auto")
    counted = kind == "raise"
    cap = int(rules.get("auto_raises") or 0)
    asks = rule == "ask" or (counted and raises >= cap)
    if asks and not confirmed:
        return Decision("needs_approval", counted=counted)
    return Decision("changed", counted=counted, approved="owner" if asks else "auto")


def display(model: str | None, effort: str | None) -> str:
    """A short name for a model and effort: ``Opus high``, or the id when it has no family."""
    name = family(model)
    base = name.capitalize() if name else (model or "its current model")
    return f"{base} {effort}" if effort else base


def question(
    kind: Kind, role: str | None, title: str, model: str | None, effort: str | None
) -> str:
    """The pop-up's question, for example ``Raise the builder "Fix CI" to Opus high?``."""
    verb = {"raise": "Raise", "lower": "Lower"}.get(kind, "Change")
    return f'{verb} the {role or "session"} "{title}" to {display(model, effort)}?'


def options(kind: Kind, model: str | None, effort: str | None) -> list[str]:
    """The two choices, recommended first."""
    verb = {"raise": "Raise", "lower": "Lower"}.get(kind, "Change")
    return [f"{verb} it to {display(model, effort)}", "Keep it as it is"]
