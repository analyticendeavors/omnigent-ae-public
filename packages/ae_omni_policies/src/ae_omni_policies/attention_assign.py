"""Who can be assigned a Needs you item (2026-11-16, Admin Center D2 phase 3).

An assignment names an OmniGent admin. The list of current admins is OmniGent's own member
list (``GET /auth/users``), asked as the caller the way ``shared_accounts_routes.admin_probe``
asks ``/v1/me``, because the extension page's bridge reaches only ``/v1/ae``. It is kept for
:data:`ADMINS_TTL_S` seconds. A list that cannot be read is **unknown**: :func:`current_admins`
returns ``None`` and the routes answer a retriable 503, never a guess and never a stale list.

OmniGent has no display name for a member, so the name shown is the id.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Request
from omnigent.server.auth import RESERVED_USER_LOCAL

from ae_omni_policies import shared_accounts_routes

#: A read list of admins is reused this long.
ADMINS_TTL_S = 60.0
#: How many members one ask takes (upstream's ceiling).
ROSTER_LIMIT = 1000

CODE_ADMINS_UNREADABLE = "attention_admins_unreadable"
ADMINS_UNREADABLE = (
    "The list of OmniGent admins could not be read, so nothing was assigned. Try again in a "
    "few seconds."
)

#: Cached per caller, so one caller is never answered from another caller's request.
_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
#: Most callers kept; the oldest go first.
_CACHE_MAX = 64


def reset_cache() -> None:
    """Forget the cached admin list (tests)."""
    _cache.clear()


async def current_admins(request: Request, caller: str | None) -> list[dict[str, Any]] | None:
    """The current admins as ``[{id, name}]`` sorted by id, or ``None`` when they cannot be read.

    A single-user runtime (no sign-in) has one admin, its own user. Otherwise ``/auth/users`` is
    asked as *caller*; only a 200 with a ``users`` list is an answer, and only rows with
    ``is_admin`` true are kept.
    """
    if caller is None or caller == RESERVED_USER_LOCAL:
        return [{"id": RESERVED_USER_LOCAL, "name": RESERVED_USER_LOCAL}]
    now = time.monotonic()
    held = _cache.get(caller)
    if held is not None and now - held[0] < ADMINS_TTL_S:
        return [dict(row) for row in held[1]]
    response = await shared_accounts_routes.ask_as_caller(
        request, f"/auth/users?limit={ROSTER_LIMIT}"
    )
    if response is None or response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    rows = body.get("users") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return None
    admins = sorted(
        (
            {"id": row["id"], "name": row["id"]}
            for row in rows
            if isinstance(row, dict)
            and isinstance(row.get("id"), str)
            and row["id"]
            and row.get("is_admin") is True
        ),
        key=lambda row: row["id"],
    )
    if len(_cache) >= _CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[caller] = (now, admins)
    return [dict(row) for row in admins]
