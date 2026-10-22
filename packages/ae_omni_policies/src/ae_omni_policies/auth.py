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
"""

from __future__ import annotations

import threading

from fastapi import Request
from omnigent.server.auth import AuthProvider, create_auth_provider
from omnigent.server.routes._auth_helpers import require_user

_lock = threading.Lock()
_provider: AuthProvider | None = None
_built = False


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


def require_ae_user(request: Request) -> str | None:
    """FastAPI dependency: the caller's identity, or 401 in multi-user mode.

    :param request: The incoming request.
    :returns: The user id, ``"local"`` on a single-user runtime.
    :raises OmnigentError: 401 when the provider finds no identity.
    """
    return require_user(request, provider())


__all__ = ["provider", "require_ae_user", "reset_provider_cache"]
