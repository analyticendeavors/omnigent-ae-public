"""Identity for the ``/v1/ae`` routes.

Upstream authenticates per route, not in middleware: every router calls
``require_user(request, auth_provider)`` with the provider ``create_app`` handed
its factory. A router mounted through ``debug_router_modules`` gets no such
hand-off and nothing exposes the provider on ``app.state``. So this module
builds its own through the public factory the CLI uses,
:func:`omnigent.server.auth.create_auth_provider`, lazily on the first request
and once per process. The factory reads the environment
(``OMNIGENT_AUTH_PROVIDER``, ``OMNIGENT_AUTH_ENABLED``, the accounts cookie
secret, ``OMNIGENT_LOCAL_SINGLE_USER``), which ``omnigent server`` had already
settled before it built the app's own provider, so both validate the same
cookie or header the same way.

If upstream ever exposes the provider (or drops ``debug_router_modules``), this
module is the one place to change; the handoff keeps a one-line
``extra_routers`` patch as the fallback.

The app's own provider has two checks this one lacks: a revoked device grant,
and a deleted or disabled account's older token. So :func:`require_ae_user`
also asks ``GET /v1/me`` (which goes through the app's provider) as the caller,
and trusts a yes for :data:`SESSION_CHECK_TTL_S` per credential (2026-10-22,
public issue 5). A no is a 401; no answer (the loopback failed or ``/v1/me``
answered 5xx) is a 503 to retry, never a sign-out. Only a yes is cached.

Wiring upstream's own checks (``set_grant_revocation_check``,
``set_account_check``) onto this provider would need the app's device-grant and
account stores, which ``create_app`` does not expose; that takes an upstream
patch, so the loopback stands until one is worth carrying.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import (
    RESERVED_USER_LOCAL,
    AuthProvider,
    create_auth_provider,
    resolve_auth_header,
)
from omnigent.server.routes._auth_helpers import require_user

#: Seconds a credential the app's provider accepted is trusted without asking again,
#: so a revoked or disabled account loses the AE routes within this long.
SESSION_CHECK_TTL_S = 30.0
#: Cached credentials past this count drop their expired entries, then all of them.
_CHECKED_MAX = 1024

_lock = threading.Lock()
_provider: AuthProvider | None = None
_built = False
_checked: dict[str, float] = {}


def provider() -> AuthProvider:
    """The process-wide auth provider, built from the environment on first use.

    :returns: The provider ``create_auth_provider`` returns for this environment.
    :raises RuntimeError: When the environment names an unknown or misconfigured
        source; the request then fails closed with a 500, never open.
    """
    global _provider, _built
    with _lock:
        if not _built:
            _provider = create_auth_provider()
            _built = True
        assert _provider is not None
        return _provider


def reset_provider_cache(replacement: AuthProvider | None = None) -> None:
    """Drop the cached provider, or install *replacement* in its place.

    For tests, which change the auth environment between cases.

    :param replacement: A provider to serve from now on; ``None`` rebuilds
        from the environment on the next request.
    """
    global _provider, _built
    with _lock:
        _provider = replacement
        _built = replacement is not None
        _checked.clear()


async def _app_accepts(request: Request, user: str) -> bool | None:
    """Whether ``/v1/me`` still names *user*; ``None`` when it gave no answer."""
    # Imported here: shared_accounts_routes imports this module.
    from ae_omni_policies import shared_accounts_routes

    response = await shared_accounts_routes.ask_me(request)
    if response is None or response.status_code >= 500:
        return None
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except ValueError:
        return None
    return isinstance(body, dict) and body.get("user_id") == user


#: Swapped by tests; production asks ``/v1/me`` as the caller, cookie included.
session_check: Callable[[Request, str], Awaitable[bool | None]] = _app_accepts


def _credential_key(request: Request, user: str) -> str:
    # Every part that can carry a credential: two bearer tokens of one user
    # never share an entry (2026-10-22 review). Only the session cookie, not the
    # whole header, so an unrelated cookie changing does not miss the cache.
    headers = request.headers
    cookie_name = getattr(provider(), "session_cookie_name", None)
    parts = (
        user,
        headers.get("authorization", ""),
        headers.get(resolve_auth_header(), ""),
        request.cookies.get(cookie_name, "") if cookie_name else headers.get("cookie", ""),
    )
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


async def require_ae_user(request: Request) -> str | None:
    """FastAPI dependency: the caller's identity, or 401 in multi-user mode.

    :param request: The incoming request.
    :returns: The user id, ``"local"`` on a single-user runtime.
    :raises OmnigentError: 401 when the provider finds no identity, or when the
        app's own provider no longer accepts the credential (revoked or disabled).
    :raises HTTPException: 503 when the app's provider could not be asked.
    """
    user = require_user(request, provider())
    if user is None or user == RESERVED_USER_LOCAL:
        return user
    key = _credential_key(request, user)
    now = time.monotonic()
    if _checked.get(key, 0.0) > now:
        return user
    accepted = await session_check(request, user)
    if accepted is None:
        raise HTTPException(
            503, "Could not confirm this sign-in with the server just now. Retry in a moment."
        )
    if not accepted:
        _checked.pop(key, None)
        raise OmnigentError(
            "This sign-in is no longer accepted (revoked or disabled). Sign in again.",
            code=ErrorCode.UNAUTHORIZED,
        )
    if len(_checked) >= _CHECKED_MAX:
        for stale in [k for k, until in _checked.items() if until <= now]:
            del _checked[stale]
        if len(_checked) >= _CHECKED_MAX:
            _checked.clear()
    _checked[key] = now + SESSION_CHECK_TTL_S
    return user


__all__ = ["SESSION_CHECK_TTL_S", "provider", "require_ae_user", "reset_provider_cache"]
