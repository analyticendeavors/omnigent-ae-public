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
from typing import TYPE_CHECKING, Any

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


def effective_limit() -> int:
    """The slot limit for every count: the Settings page's, else ``AE_SLOT_LIMIT``.

    Reads the policy store (a database query), so async callers run it in a thread.

    :returns: Working sessions allowed at once.
    """
    store = _store
    if store is None:
        return labels.SLOT_LIMIT
    try:
        found = limit_from_policies(store.list_defaults(), labels.SLOT_LIMIT)
    except Exception:  # noqa: BLE001 - an unreadable store must not take the counts down
        log.warning(
            "slot limit: the policy store did not answer; using AE_SLOT_LIMIT", exc_info=True
        )
        return labels.SLOT_LIMIT
    return labels.SLOT_LIMIT if found is None else found
