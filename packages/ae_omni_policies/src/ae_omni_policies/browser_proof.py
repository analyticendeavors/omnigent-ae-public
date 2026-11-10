"""A second cookie that proves a session cookie was issued to a browser.

Why (2026-11-08, https://github.com/analyticendeavors/omnigent-ae/issues/506): upstream
mints one plain session JWT for browsers and CLIs alike, so the browser-only gate in
:func:`ae_omni_policies.auth.require_browser_caller` could not tell a browser's cookie
from the token ``omnigent login`` stores in ``~/.omnigent/auth_tokens.json``. An agent
that read that file could send the token as the cookie, add ``Sec-Fetch-Site`` itself,
and pass.

The fix, out of tree (no upstream patch): this middleware appends a second cookie,
``__Host-ae_browser_proof`` (``ae_browser_proof`` on plain HTTP), to a sign-in response
that itself sets the session cookie. Its value is HMAC(K, session JWT) with
K = HMAC(cookie secret, ``ae-browser-proof-v1``). The gate requires a proof that matches
the session cookie it came with. A CLI receives the JWT (through ``/auth/cli-poll`` or a
JSON body) but never the proof, which only ever travels in a ``Set-Cookie`` header.

The proof is added only on:

* ``GET /auth/callback`` (the redirect and the CLI ticket page; the native branch sets no
  session cookie, so it gets none);
* in accounts mode, ``POST /auth/login``, ``/auth/register`` and ``/auth/setup``, when the
  request carried ``Sec-Fetch-Site: same-origin`` and the response body holds no refresh
  token (decided from the response, never from the request JSON).

It is never added on the magic-link redeem, on page-load re-sends by
:mod:`ae_omni_policies.session_cookie`, on ``/auth/cli-poll``, ``/auth/native-token`` or
any other path. A response that clears the session cookie (sign-out) also expires the
proof cookie.

Accounts-mode guard: the same middleware applies the gate's rules (no ``Authorization``
header, a session cookie with a valid proof, ``Sec-Fetch-Site: same-origin``) to
``POST /auth/invite`` and ``POST /auth/users/{id}/reset``. Without it an admin's CLI or
bootstrap token could invite a new admin, or reset its own password, then sign in with a
forged header and receive a proof. The CLI calls neither route.

Fails closed: no middleware, or a sign-in route this module does not know, means no proof,
which means the gate answers 403.

Clients: the iOS app (0.3.1) holds a CLI-style token with no proof, so it loses the gated
writes; the "Sign in again on this device" button may restore them. The desktop app also holds
no proof (it installs its own cookie from ``/auth/native-token``), but since 2026-11-09 its own
sign-in is recorded by :mod:`ae_omni_policies.desktop_signin` and the gate accepts it, so it
keeps the gated writes after one sign-out and sign-in following that deploy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_logger = logging.getLogger(__name__)

KEY_CONTEXT = b"ae-browser-proof-v1"
SECURE_PROOF_NAME = "__Host-ae_browser_proof"
PLAIN_PROOF_NAME = "ae_browser_proof"
SECURE_SESSION_NAME = "__Host-ap_session"
PLAIN_SESSION_NAME = "ap_session"

OIDC_SECRET_ENV = "OMNIGENT_OIDC_COOKIE_SECRET"
ACCOUNTS_SECRET_ENV = "OMNIGENT_ACCOUNTS_COOKIE_SECRET"
ACCOUNTS_BASE_URL_ENV = "OMNIGENT_ACCOUNTS_BASE_URL"

CALLBACK_SUFFIX = "/auth/callback"
ACCOUNTS_SIGN_IN_SUFFIXES = ("/auth/login", "/auth/register", "/auth/setup")
INVITE_SUFFIX = "/auth/invite"
_RESET_PATH = re.compile(r"/auth/users/[^/]+/reset$")

REFUSAL = (
    "This change needs a browser sign-in on this device. Sign out and sign in again, then retry."
)


def derive_key(secret: str | bytes) -> bytes:
    """K = HMAC-SHA256(cookie secret, ``ae-browser-proof-v1``)."""
    raw = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(raw, KEY_CONTEXT, hashlib.sha256).digest()


def expected_proof(key: bytes, session_token: str) -> str:
    """The proof for ``session_token``: HMAC-SHA256(K, token) as hex."""
    return hmac.new(key, session_token.encode("utf-8"), hashlib.sha256).hexdigest()


def proof_matches(key: bytes, session_token: str, proof: str | None) -> bool:
    """Constant-time check that ``proof`` belongs to ``session_token``."""
    if not proof or not session_token:
        return False
    return hmac.compare_digest(expected_proof(key, session_token), proof)


def proof_cookie_name(secure: bool) -> str:
    """The proof cookie's name; ``__Host-`` exactly when the session cookie's is."""
    return SECURE_PROOF_NAME if secure else PLAIN_PROOF_NAME


def session_cookie_name(secure: bool) -> str:
    """Upstream's session cookie name for the same secure flag (``oidc.py`` and accounts)."""
    return SECURE_SESSION_NAME if secure else PLAIN_SESSION_NAME


def proof_set_cookie(name: str, value: str, max_age: int, *, secure: bool) -> bytes:
    """The ``Set-Cookie`` value for the proof: HttpOnly, SameSite=Strict, Path=/."""
    response = Response()
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    return response.raw_headers[-1][1]


def proof_expire_cookie(name: str, *, secure: bool) -> bytes:
    """The ``Set-Cookie`` value that removes the proof cookie."""
    response = Response()
    response.delete_cookie(key=name, path="/", secure=secure, httponly=True, samesite="strict")
    return response.raw_headers[-1][1]


def _parse_set_cookie(raw: bytes) -> tuple[str, str, int | None]:
    """(name, value, max_age) of a ``Set-Cookie`` header value; max_age None when absent."""
    parts = [p.strip() for p in raw.decode("latin-1").split(";")]
    name, _, value = parts[0].partition("=")
    max_age: int | None = None
    for attr in parts[1:]:
        key, _, val = attr.partition("=")
        if key.strip().lower() == "max-age":
            try:
                max_age = int(val.strip())
            except ValueError:
                max_age = None
    return name.strip(), value.strip().strip('"'), max_age


def _session_cookie_in(
    headers: list[tuple[bytes, bytes]], session_name: str
) -> tuple[str, int | None] | None:
    """(value, max_age) of the session ``Set-Cookie`` in a response, or None when absent."""
    for key, raw in headers:
        if key.lower() != b"set-cookie":
            continue
        name, value, max_age = _parse_set_cookie(raw)
        if name == session_name:
            return value, max_age
    return None


def refusal_response() -> JSONResponse:
    """403 in the shape FastAPI uses for an ``HTTPException``."""
    return JSONResponse(status_code=403, content={"detail": REFUSAL})


@dataclass(frozen=True)
class Settings:
    """What the middleware needs: the key, cookie names, secure flag and the accounts mode."""

    key: bytes
    secure: bool
    accounts: bool
    session_name: str
    proof_name: str


def fixed_settings(cookie_secret: bytes, *, secure: bool, accounts: bool) -> Settings:
    """Settings from explicit values (a stub app, or a caller that already knows them)."""
    return Settings(
        key=derive_key(cookie_secret),
        secure=secure,
        accounts=accounts,
        session_name=session_cookie_name(secure),
        proof_name=proof_cookie_name(secure),
    )


def provider_settings(prov: object) -> Settings | None:
    """Settings read from the live auth provider, the one the gate reads; None when unusable.

    The key, the session cookie name (so the secure flag) and the accounts mode all come from
    the provider, never from environment variables that could differ from it.
    """
    from ae_omni_policies.auth import _cookie_secret

    secret = _cookie_secret(prov)
    name = getattr(prov, "session_cookie_name", None)
    if secret is None or not isinstance(name, str) or not name:
        return None
    secure = name.startswith("__Host-")
    return Settings(
        key=derive_key(secret),
        secure=secure,
        accounts=getattr(prov, "_source", None) == "accounts",
        session_name=name,
        proof_name=proof_cookie_name(secure),
    )


class BrowserProof:
    """ASGI middleware: issue the proof cookie on browser sign-ins, guard invite and reset.

    With ``provider`` (a callable returning the live auth provider) every request reads its
    settings from that provider, cached per provider object. Without it, the explicit
    ``cookie_secret``, ``secure`` and ``accounts`` values are used.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        cookie_secret: bytes | None = None,
        secure: bool = False,
        accounts: bool = False,
        provider: Callable[[], object] | None = None,
    ) -> None:
        self.app = app
        self._provider = provider
        self._fixed = (
            fixed_settings(cookie_secret, secure=secure, accounts=accounts)
            if cookie_secret is not None
            else None
        )
        self._cached: tuple[object, Settings | None] | None = None

    def settings(self) -> Settings | None:
        """The settings for this request: fixed, or from the live provider (cached per object)."""
        if self._fixed is not None or self._provider is None:
            return self._fixed
        try:
            prov = self._provider()
        except Exception:  # noqa: BLE001 (a misconfigured provider: no proof is issued,
            return None  # and __call__ refuses invite and reset rather than letting them through)
        if self._cached is None or self._cached[0] is not prov:
            self._cached = (prov, provider_settings(prov))
        return self._cached[1]

    @staticmethod
    def _kind(cfg: Settings, scope: Scope) -> str | None:
        """Which sign-in path this request is: ``callback``, ``accounts``, or None."""
        path, method = scope["path"], scope["method"]
        if method == "GET" and path.endswith(CALLBACK_SUFFIX):
            return "callback"
        if cfg.accounts and method == "POST" and path.endswith(ACCOUNTS_SIGN_IN_SUFFIXES):
            return "accounts"
        return None

    @staticmethod
    def _guard_path(scope: Scope) -> bool:
        """``POST`` to invite or reset, in any auth mode."""
        if scope["method"] != "POST":
            return False
        path = scope["path"]
        return path.endswith(INVITE_SUFFIX) or bool(_RESET_PATH.search(path))

    @classmethod
    def _guarded(cls, cfg: Settings, scope: Scope) -> bool:
        return cfg.accounts and cls._guard_path(scope)

    @staticmethod
    def _guard_ok(cfg: Settings, scope: Scope) -> bool:
        headers = Headers(scope=scope)
        if "authorization" in headers:  # present at all, even empty
            return False
        if headers.get("sec-fetch-site", "").strip().lower() != "same-origin":
            return False
        cookies = HTTPConnection(scope).cookies
        token = cookies.get(cfg.session_name)
        return bool(token) and proof_matches(cfg.key, token, cookies.get(cfg.proof_name))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cfg = self.settings()
        if cfg is None:
            # Unusable settings (no secret, no cookie name, or the provider raised): no proof
            # is issued, so the gate refuses, and invite and reset are refused here too, since
            # whether the guard should be on cannot be known (fail closed, review of
            # https://github.com/analyticendeavors/omnigent-ae/pull/509).
            if self._guard_path(scope):
                _logger.warning(
                    "ae browser proof: no usable auth settings; refused %s", scope["path"]
                )
                await refusal_response()(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        if self._guarded(cfg, scope) and not self._guard_ok(cfg, scope):
            await refusal_response()(scope, receive, send)
            return
        kind = self._kind(cfg, scope)
        if kind == "accounts":
            same_origin = (
                Headers(scope=scope).get("sec-fetch-site", "").strip().lower() == "same-origin"
            )
            if not same_origin:
                kind = None
        if kind == "accounts":
            await self.app(scope, receive, self._buffered(cfg, send))
        else:
            await self.app(scope, receive, self._watch(cfg, send, issue=kind == "callback"))

    @staticmethod
    def _with_proof(cfg: Settings, message: Message, *, issue: bool) -> Message:
        """Add the proof (or its expiry) to a response-start message when the rules say so."""
        headers = list(message.get("headers", []))
        found = _session_cookie_in(headers, cfg.session_name)
        if found is None:
            return message
        value, max_age = found
        if not value or max_age is None or max_age <= 0:
            headers.append((b"set-cookie", proof_expire_cookie(cfg.proof_name, secure=cfg.secure)))
        elif issue and message["status"] < 400:
            headers.append(
                (
                    b"set-cookie",
                    proof_set_cookie(
                        cfg.proof_name,
                        expected_proof(cfg.key, value),
                        max_age,
                        secure=cfg.secure,
                    ),
                )
            )
        else:
            return message
        return {**message, "headers": headers}

    def _watch(self, cfg: Settings, send: Send, *, issue: bool) -> Send:
        async def watched(message: Message) -> None:
            if message["type"] == "http.response.start":
                message = self._with_proof(cfg, message, issue=issue)
            await send(message)

        return watched

    def _buffered(self, cfg: Settings, send: Send) -> Send:
        """Hold an accounts sign-in answer until its body shows whether it has a refresh token."""
        start: Message | None = None
        chunks: list[bytes] = []

        async def buffered(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] != "http.response.body" or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(chunks)
            issue = start["status"] < 400 and not _has_refresh_token(body)
            await send(self._with_proof(cfg, start, issue=issue))
            await send({"type": "http.response.body", "body": body, "more_body": False})

        return buffered


def _has_refresh_token(body: bytes) -> bool:
    """Whether a JSON answer carries refresh material. Anything unreadable counts as yes."""
    try:
        data = json.loads(body)
    except ValueError:
        return True
    return not isinstance(data, dict) or "refresh_token" in data


def install(app: Any, environ: Mapping[str, str], auth_source: str | None = None) -> bool:
    """Add the middleware to ``app`` for OIDC or accounts sign-in.

    The environment only decides whether to add it (OIDC or accounts, with a hex cookie secret);
    what the middleware then uses comes from the live auth provider.

    :param auth_source: The server's auth mode; upstream's ``resolve_auth_source()`` when
        omitted.
    :returns: Whether it was added; False in other modes or without a usable cookie secret.
    """
    if auth_source is None:
        from omnigent.server.auth import resolve_auth_source

        auth_source = resolve_auth_source()
    if auth_source not in ("oidc", "accounts"):
        return False
    env_name = OIDC_SECRET_ENV if auth_source == "oidc" else ACCOUNTS_SECRET_ENV
    secret_hex = environ.get(env_name, "").strip()
    if not secret_hex:
        return False
    try:
        bytes.fromhex(secret_hex)
    except ValueError:
        _logger.warning("%s is not hex; the browser proof cookie stays off.", env_name)
        return False
    from ae_omni_policies.auth import provider

    # Key, names, secure flag and mode come from the live provider at request time, so an
    # environment that differs from it cannot turn the accounts guard off.
    app.add_middleware(BrowserProof, provider=provider)
    return True
