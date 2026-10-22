"""Give the OIDC session cookie its expiry on page loads, so the iOS app keeps it.

Why (2026-10-20): after the Microsoft sign-in cutover Reid had to sign in again
through the phone's browser every time he reopened the iOS app. The App Store
build (0.3.1, 2026-09-24) signs in with the ticket flow (``POST /auth/cli-login``,
the browser, ``GET /auth/cli-poll``) and installs the token it receives as a
cookie with no expiry. WebKit treats that as a session cookie and drops it when
the app closes, so the next launch got ``GET /v1/me`` 401 and a fresh sign-in
(four in a row in the box's log at 21:03Z that day). The server's 720 hour
session was never the limit. Upstream's native sign-in, which sets the expiry
itself, landed on 2026-10-07 and is not in an App Store build yet.

The fix: on a page load (a ``GET`` for a document) that carries a valid session
cookie, an HTML response that is not publicly cacheable sets the same cookie
again with ``Max-Age`` set to what is left of the token's ``exp``, and the
attributes ``/auth/callback`` uses. A browser that signed in on the web already
holds it that way, so nothing changes there. The value is never changed and its
lifetime is never extended. A token that fails the signature check, has expired
or carries a ``scope`` or ``grant_id`` (machine and device tokens, never a
browser's) gets nothing; one the server would reject for other reasons, such as
a stale account generation, can be re-sent but stays rejected. ``/auth/``
routes are left alone, so a sign-out that clears the cookie is never undone, and
so is any response that sets the cookie itself or is not a success.

Review fixes (2026-10-20, dual review of
https://github.com/analyticendeavors/omnigent-ae/pull/163): the cookie is never
added to a ``public`` response (a static file opened in a tab is cached for an
hour, and a shared cache must never keep a session token); the cookie name
follows upstream's rule, including the ``OMNIGENT_DOMAIN`` fallback; it is
installed only when the auth source is ``oidc``; ``/auth/`` is matched under a
base path too.

Installed by :mod:`ae_omni_policies.docker_entrypoint` when the server runs
with OIDC. Drop it once an App Store build ships native sign-in (iOS 0.3.2 or
later): check the app's version, then remove this module and its install line.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

import jwt
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_logger = logging.getLogger(__name__)

COOKIE_SECRET_ENV = "OMNIGENT_OIDC_COOKIE_SECRET"
REDIRECT_URI_ENV = "OMNIGENT_OIDC_REDIRECT_URI"
DOMAIN_ENV = "OMNIGENT_DOMAIN"
# Upstream's names (OIDCConfig.session_cookie_name): __Host- on HTTPS only.
SECURE_COOKIE_NAME = "__Host-ap_session"
PLAIN_COOKIE_NAME = "ap_session"


def is_page_load(headers: Headers) -> bool:
    """Whether a request loads a document rather than calling the API."""
    dest = headers.get("sec-fetch-dest")
    if dest is not None:
        return dest == "document"
    return "text/html" in headers.get("accept", "")


def is_auth_path(path: str) -> bool:
    """Whether ``path`` is under ``/auth/``, with or without a base path in front."""
    return "/auth/" in path


def remaining_seconds(token: str, secret: bytes, now: float) -> int | None:
    """Seconds left on a validly signed browser session token, or None for anything else."""
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], options={"verify_exp": False})
    except jwt.InvalidTokenError:
        return None
    if "scope" in payload or "grant_id" in payload or not isinstance(payload.get("sub"), str):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool):
        return None
    left = exp - int(now)
    return left if left > 0 else None


def cookie_header(name: str, token: str, max_age: int, *, secure: bool) -> bytes:
    """The ``Set-Cookie`` value ``/auth/callback`` sends, with ``max_age`` seconds."""
    response = Response()
    response.set_cookie(
        key=name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    return response.raw_headers[-1][1]


def wants_cookie(headers: list[tuple[bytes, bytes]], prefix: bytes) -> bool:
    """Whether a response may carry the session cookie: private HTML that doesn't set it."""
    response = Headers(raw=headers)
    if not response.get("content-type", "").startswith("text/html"):
        return False
    if "public" in response.get("cache-control", "").lower():
        return False
    return not any(v.encode("latin-1").startswith(prefix) for v in response.getlist("set-cookie"))


class SessionCookieExpiry:
    """ASGI middleware that re-sends a valid session cookie with its expiry on page loads."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        cookie_secret: bytes,
        secure: bool,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.app = app
        self.secret = cookie_secret
        self.secure = secure
        self.name = SECURE_COOKIE_NAME if secure else PLAIN_COOKIE_NAME
        self.clock = clock

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "GET"
            or is_auth_path(scope["path"])
            or not is_page_load(Headers(scope=scope))
        ):
            await self.app(scope, receive, send)
            return
        token = HTTPConnection(scope).cookies.get(self.name)
        left = remaining_seconds(token, self.secret, self.clock()) if token else None
        if left is None:
            await self.app(scope, receive, send)
            return
        header = cookie_header(self.name, token, left, secure=self.secure)
        prefix = f"{self.name}=".encode("latin-1")

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] < 400:
                headers = list(message.get("headers", []))
                if wants_cookie(headers, prefix):
                    headers.append((b"set-cookie", header))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cookie)


def secure_cookies(environ: Mapping[str, str]) -> bool:
    """Upstream's ``OIDCConfig.secure_cookies``, with its ``OMNIGENT_DOMAIN`` fallback."""
    redirect = environ.get(REDIRECT_URI_ENV, "").strip()
    if not redirect and environ.get(DOMAIN_ENV, "").strip():
        return True  # upstream derives https://<domain>/auth/callback
    return redirect.startswith("https://")


def install(app: Any, environ: Mapping[str, str], auth_source: str | None = None) -> bool:
    """Add the middleware to ``app`` when the server signs in with OIDC.

    :param auth_source: The server's auth mode; upstream's ``resolve_auth_source()``
        when omitted.
    :returns: Whether it was added; False outside OIDC or without a usable cookie secret.
    """
    if auth_source is None:
        from omnigent.server.auth import resolve_auth_source

        auth_source = resolve_auth_source()
    if auth_source != "oidc":
        return False
    secret_hex = environ.get(COOKIE_SECRET_ENV, "").strip()
    if not secret_hex:
        return False
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError:
        _logger.warning("%s is not hex; the session cookie expiry stays off.", COOKIE_SECRET_ENV)
        return False
    app.add_middleware(SessionCookieExpiry, cookie_secret=secret, secure=secure_cookies(environ))
    return True
