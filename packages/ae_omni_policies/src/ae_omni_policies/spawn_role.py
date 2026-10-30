"""Resolve a ``create_session`` role to a model, effort and harness (2026-10-24).

The lead passes ``role`` instead of restating "Sonnet builds" in every prompt. The
role's setting in the parent's preset decides (``role_prefs.py`` documents the
shapes): ``fixed`` gives its model and effort, ``session`` the parent's own,
``auto`` and a lead-picks preset leave both unset, ``multi`` (reviewer) uses slot 1
(or, when the request asks for ``harness="codex"``, the slot run by Codex, 2026-10-24),
and a slot run by Codex switches the harness to ``codex``. Explicit values in the
request always win; that merge is :func:`resolve`'s last step.

The preset comes from ``omnigent.inner.ae_roles.resolve_roles`` (same server
process), so a session's own ``ae.team`` choice counts. With no parent session the
owner's default preset applies. A standalone chat (a handoff the lead opens for the owner,
2026-10-25) is no one's helper: its role comes from the preset it opens on, and the
opener only lends its own model and effort to a ``session`` role.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from omnigent.runtime import get_conversation_store

from ae_omni_policies import role_prefs

_logger = logging.getLogger(__name__)
# Which native wrapper runs a session (the router's ``_WRAPPER_LABEL``).
_WRAPPER_LABEL = "omnigent.wrapper"


@dataclass
class Spawn:
    """What a new session runs on: unset fields leave the harness default."""

    model: str | None = None
    effort: str | None = None
    harness: str = "claude"
    preset: str = role_prefs.DEFAULT_PRESET


class UnknownPreset(ValueError):
    """The request named a preset the owner does not have."""


def _prefs(user: str) -> tuple[str, list[dict[str, Any]]]:
    state = role_prefs.store().get(user).model_dump()
    return str(state["default_preset"]), [p for p in state["presets"] if isinstance(p, dict)]


def _effective(parent: Any | None, user: str) -> dict[str, Any]:
    """``{"preset": {...}}`` or a lead-picks shape for the parent, else the owner's default."""
    if parent is not None:
        from omnigent.inner import ae_roles

        try:
            return ae_roles.resolve_roles(parent, get_conversation_store())
        except Exception:  # noqa: BLE001 (an unreadable preset must not block a start)
            _logger.warning("spawn role: could not resolve the parent's preset", exc_info=True)
    default_id, presets = _prefs(user)
    chosen = next((p for p in presets if p.get("id") == default_id), presets[0])
    return {"preset": chosen}


def _slot_values(setting: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """``(model, effort, harness)`` a fixed slot or Codex agent slot asks for."""
    mode = setting.get("mode")
    if mode == "fixed":
        return setting.get("model"), setting.get("effort"), None
    if mode == "agent":
        codex = (
            setting.get("agent_name") == role_prefs.CODEX_AGENT_NAME
            or setting.get("agent") == role_prefs.CODEX_AGENT_ID
        )
        if codex:
            return setting.get("model"), setting.get("effort"), "codex"
    return None, None, None


def _is_codex_slot(slot: Any) -> bool:
    return isinstance(slot, dict) and _slot_values(slot)[2] == "codex"


def _harness_of(conv: Any) -> str:
    """``codex`` for a session the Codex wrapper runs, else ``claude``."""
    wrapper = (getattr(conv, "labels", None) or {}).get(_WRAPPER_LABEL)
    return "codex" if wrapper == "codex-native-ui" else "claude"


def _from_role(
    role: str, effective: dict[str, Any], parent: Any | None, harness: str | None = None
) -> Spawn:
    preset = effective.get("preset")
    out = Spawn()
    if not isinstance(preset, dict):
        return out
    setting = (preset.get("roles") or {}).get(role)
    if not isinstance(setting, dict):
        return out
    if setting.get("mode") == "multi":
        slots = setting.get("reviewers") or []
        pick = slots[0] if slots else None
        if harness == "codex":
            # A lead that asks for a Codex reviewer gets the slot Codex runs, not slot 1.
            pick = next((slot for slot in slots if _is_codex_slot(slot)), pick)
        setting = pick if isinstance(pick, dict) else {}
    if setting.get("mode") == "session":
        if parent is not None:
            out.model = getattr(parent, "model_override", None)
            out.effort = getattr(parent, "reasoning_effort", None)
            # The parent's harness comes along, or a Codex parent's model lands on Claude.
            out.harness = _harness_of(parent)
        return out
    out.model, out.effort, harness = _slot_values(setting)
    out.harness = harness or "claude"
    return out


def resolve(
    *,
    parent: Any | None,
    user: str | None,
    role: str | None,
    model: str | None,
    effort: str | None,
    preset: str | None,
    harness: str | None = None,
    standalone: bool = False,
) -> Spawn:
    """The model, effort, harness and preset id for a new session.

    :param parent: The lead's conversation, or ``None``.
    :param user: The caller, ``local`` when there is none.
    :param harness: The harness the request named, ``None`` when it left it out. One that
        differs from what the role's slot implies drops the role's model and effort,
        which belong to the other harness; explicit ``model`` and ``effort`` still apply.
    :param standalone: Take the role from *preset* (else the owner's default) instead of
        from *parent*'s preset; *parent* is then the opener, read only for a ``session``
        role.
    :raises UnknownPreset: When *preset* is not one of the owner's presets.
    """
    owner = user or "local"
    default_id, presets = _prefs(owner)
    if preset:
        validate_preset(preset, presets)
    if standalone:
        wanted = preset or default_id
        chosen = next((p for p in presets if p.get("id") == wanted), None)
        if chosen is None and not preset and presets:
            chosen = presets[0]
        # lead-picks names no preset: the role then leaves model and effort unset.
        effective = {"preset": chosen} if chosen is not None else {}
    else:
        effective = _effective(parent, owner)
    out = _from_role(role, effective, parent, harness) if role else Spawn()
    if harness and harness != out.harness:
        out.model = out.effort = None
    out.harness = harness or out.harness
    if model:
        out.model = model
    if effort:
        out.effort = effort
    # The preset the child really runs under: the one asked for, else the owner's default
    # (its ae.team label is only written when a preset is asked for).
    out.preset = preset or default_id
    return out


def validate_preset(preset: str, presets: list[dict[str, Any]]) -> None:
    """Raise :class:`UnknownPreset` unless *preset* is in *presets* or is ``lead-picks``."""
    if preset != role_prefs.LEAD_PICKS and preset not in {p.get("id") for p in presets}:
        raise UnknownPreset(
            f"preset {preset!r} is not one of your presets; pick an id from Settings > Roles"
        )


def check_preset(user: str | None, preset: str) -> None:
    """:func:`validate_preset` against *user*'s saved presets (the starters when none)."""
    validate_preset(preset, _prefs(user or "local")[1])


def team_label(preset: str) -> str:
    """The ``ae.team`` label that opens a session on *preset* (shape: ``aeTeam.ts``)."""
    return json.dumps(
        {"version": 1, "mode": "auto", "members": [], "roles": {"preset": preset}},
        separators=(",", ":"),
    )
