"""The slot limit every count reads: the Settings page first, then ``AE_SLOT_LIMIT``.

Added 2026-10-22. Reid raised his slot cap to four and expected to change it on
Settings > Global Policies. The slot cap policy has taken a ``limit`` there since
patch 0095's Edit, but every count in the web UI read ``AE_SLOT_LIMIT`` through
``GET /v1/ae/capacity``, so a limit set on the page held prompts at one number
while the Dashboard and the sidebar showed another. :func:`effective_limit` makes
the counts follow the same rule the policies enforce: every enabled global slot cap
holds prompts at its own ``limit``, or at ``AE_SLOT_LIMIT``
(:data:`labels.SLOT_LIMIT`) when it sets none, so the lowest of those is the number
that holds first. With no slot cap on, the counts keep ``AE_SLOT_LIMIT``.

The server's policy store reaches this module once, from ``docker_entrypoint``'s
``create_app`` wrapper, the same hand-off the policy seed uses. A server started
without it (``just dev``, the tests' app) keeps the environment's value. Each call
reads the store afresh: a handful of rows, scoped by the store to the caller's
workspace, next to the session list the same routes already read. A store that
cannot be read falls back to the environment.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, NamedTuple

from ae_omni_policies import labels

if TYPE_CHECKING:
    from omnigent.stores.policy_store import PolicyStore

log = logging.getLogger(__name__)

#: The slot cap's handler, as the policy store records it.
HANDLER = "ae_omni_policies.slot_cap.slot_cap"

_store: PolicyStore | None = None


def remember_store(store: PolicyStore | None) -> None:
    """Keep the server's policy store, so the counts can follow the Settings page.

    :param store: The store ``create_app`` received, or ``None`` to forget it (tests).
    """
    global _store
    _store = store


def _whole(value: Any) -> int | None:
    """A whole number of one or more (``5`` or ``5.0``), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 1 or value != int(value):
        return None
    return int(value)


def limit_from_policies(policies: Iterable[Any], default: int) -> int | None:
    """The limit the enabled global slot caps hold at, or ``None`` when none is on.

    A cap without a usable ``limit`` holds at *default*, as the policy's factory
    does, so it takes part in the minimum.

    :param policies: The store's global policies (``list_defaults``).
    :param default: What a cap without a limit enforces (``AE_SLOT_LIMIT``).
    :returns: The lowest limit among the enabled caps, or ``None``.
    """
    limits = []
    for policy in policies:
        if getattr(policy, "handler", None) != HANDLER or not getattr(policy, "enabled", False):
            continue
        own = _whole((getattr(policy, "factory_params", None) or {}).get("limit"))
        limits.append(default if own is None else own)
    return min(limits) if limits else None


#: Children of one family working at once, unless the slot cap policy sets another.
DEFAULT_FAMILY_CHILDREN = 3


def family_children_from_policies(policies: Iterable[Any], default: int) -> int | None:
    """The lowest ``family_children`` among the enabled global slot caps, or ``None``.

    ``0`` lifts the cap. A cap that sets none holds at *default*.
    """
    caps = []
    for policy in policies:
        if getattr(policy, "handler", None) != HANDLER or not getattr(policy, "enabled", False):
            continue
        own = (getattr(policy, "factory_params", None) or {}).get("family_children")
        valid = isinstance(own, int) and not isinstance(own, bool) and own >= 0
        caps.append(own if valid else default)
    lifted = [cap for cap in caps if cap > 0]
    if not caps:
        return None
    return min(lifted) if lifted else 0


def family_children() -> int:
    """How many of one family's sessions, past its lead, may work at once; ``0`` is no cap.

    A family is a session and the ones it started through ``POST /v1/ae/sessions``
    (2026-10-23). It holds one slot, so this keeps one lead from fanning out without
    end. Reads the policy store like :func:`effective_limit`.
    """
    return resolve().family_children


def effective_limit() -> int:
    """The slot limit for every count: the Settings page's, else ``AE_SLOT_LIMIT``.

    Reads the policy store (a database query), so async callers run it in a thread.

    :returns: Working sessions allowed at once.
    """
    return resolve().limit


def helpers_count_from_policies(policies: Iterable[Any]) -> bool:
    """Whether the cap that sets the limit counts helpers (see :func:`resolve_from_policies`)."""
    return resolve_from_policies(policies, labels.SLOT_LIMIT).helpers_count


class SlotConfig(NamedTuple):
    """The slot rules every count and gate reads, all taken from one cap."""

    limit: int
    helpers_count: bool
    family_children: int


def _valid_family_children(value: Any, default: int) -> int:
    valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return value if valid else default


def resolve_from_policies(policies: Iterable[Any], default_limit: int) -> SlotConfig:
    """One ``(limit, helpers_count, family_children)`` from the enabled global slot caps.

    The cap with the lowest limit sets the limit and the helpers flag, so the pair is
    never stricter than every real cap (2026-10-26, review: the limit of one cap with
    the helpers flag of another made ``POST /v1/ae/sessions`` refuse what both caps
    allow). Among caps tied on the limit, one that counts helpers wins.
    ``family_children`` is the lowest nonzero value across every enabled cap (``0``
    lifts a cap; a cap that sets none holds at the default), as before. All three come
    from the one list given. ``helpers_count`` is on only for the
    boolean ``true``; ``"true"`` or ``1`` leave it off. With no cap on: the default
    limit, helpers off, :data:`DEFAULT_FAMILY_CHILDREN`.
    """
    caps: list[SlotConfig] = []
    for policy in policies:
        if getattr(policy, "handler", None) != HANDLER or not getattr(policy, "enabled", False):
            continue
        params = getattr(policy, "factory_params", None) or {}
        own = _whole(params.get("limit"))
        caps.append(
            SlotConfig(
                default_limit if own is None else own,
                params.get("helpers_count") is True,
                _valid_family_children(params.get("family_children"), DEFAULT_FAMILY_CHILDREN),
            )
        )
    if not caps:
        return SlotConfig(default_limit, False, DEFAULT_FAMILY_CHILDREN)
    first = min(caps, key=lambda cap: (cap.limit, not cap.helpers_count))
    nonzero = [cap.family_children for cap in caps if cap.family_children > 0]
    return SlotConfig(first.limit, first.helpers_count, min(nonzero) if nonzero else 0)


def resolve(fallback: SlotConfig | None = None) -> SlotConfig:
    """The slot rules from one read of the policy store (a concurrent save cannot mix two).

    An unreadable store, or none, gives *fallback*: ``AE_SLOT_LIMIT``, helpers off and
    the default family cap unless the caller passes its own (the slot cap policy passes
    the parameters it was built with). Reads the database, so async callers run it in a
    thread.
    """
    if fallback is None:
        fallback = SlotConfig(labels.SLOT_LIMIT, False, DEFAULT_FAMILY_CHILDREN)
    store = _store
    if store is None:
        return fallback
    try:
        return resolve_from_policies(store.list_defaults(), labels.SLOT_LIMIT)
    except Exception:  # noqa: BLE001 - an unreadable store must not take the counts down
        log.warning(
            "slot rules: the policy store did not answer; using the fallback rules %s",
            fallback,
            exc_info=True,
        )
        return fallback


def helpers_count() -> bool:
    """Whether each working helper session holds a slot of its own (Settings, default off)."""
    return resolve().helpers_count
