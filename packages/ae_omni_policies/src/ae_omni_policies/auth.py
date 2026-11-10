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

from ae_omni_policies import browser_proof, desktop_signin

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


CODE_BROWSER_ONLY = "browser_only"
PROOF_MISSING = "the sign-in has no browser proof"


def _cookie_secret(prov: object) -> str | bytes | None:
    """The secret the provider signs session cookies with, as upstream's ``_check_cookie`` reads
    it: the OIDC or accounts config's ``cookie_secret`` (private attributes, so read leniently).

    Upstream's ``OIDCConfig`` and ``AccountsConfig`` both hold it as ``bytes`` (decoded from
    hex). Why bytes counts (2026-11-07): this read once kept only ``str``, so on a real server it
    found no secret and the card switch refused every browser as holding "a machine token"; the
    tests had used a text secret and passed.
    """
    direct = getattr(prov, "cookie_secret", None)
    if isinstance(direct, (str, bytes)) and direct:
        return direct
    source = getattr(prov, "_source", None)
    config = getattr(prov, "_oidc_config" if source == "oidc" else "_accounts_config", None)
    secret = getattr(config, "cookie_secret", None)
    return secret if isinstance(secret, (str, bytes)) and secret else None


def _decode_session(token: str, secret: str | bytes) -> dict[str, object] | None:
    """The claims of *token* decoded the way upstream's ``_check_cookie`` does (HS256, the
    cookie secret, expiry checked), or None when it does not verify or carries a ``scope``.

    A ``scope`` claim marks device grants and client-credentials tokens; a browser or desktop
    session never carries one, so such a token is refused here even when it was recorded.
    """
    import jwt

    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except (jwt.InvalidTokenError, jwt.InvalidKeyError):
        # InvalidKeyError is a separate family (a bad secret): refuse, never a 500.
        return None
    if not isinstance(payload, dict) or payload.get("scope") is not None:
        return None
    return payload


def _machine_token(token: str, secret: str | bytes) -> bool:
    """True when *token* does not verify, or carries ``scope`` or ``grant_id``.

    Kept for the PR 509 tests; the gate itself uses :func:`_decode_session` and then decides
    on ``grant_id`` by what a desktop sign-in recorded.
    """
    payload = _decode_session(token, secret)
    return payload is None or payload.get("grant_id") is not None


def _login_grant_client(payload: dict[str, object]) -> bool:
    """Whether the token's ``act.client_id`` is upstream's reserved login grant client."""
    from omnigent.server.routes.device_auth import LOGIN_GRANT_CLIENT_ID

    act = payload.get("act")
    return isinstance(act, dict) and act.get("client_id") == LOGIN_GRANT_CLIENT_ID


async def require_browser_caller(request: Request) -> str | None:
    """FastAPI dependency: the caller's identity, refusing the ways an agent usually calls.

    Why (2026-11-07, card setup security review): an agent in an admin's session can hold a
    credential that names that admin and could otherwise switch off the Git or Key Vault card as
    them. The host web app sends plain same-origin fetches that carry the session cookie and no
    ``Authorization`` header (upstream's ``authenticatedFetch`` adds only ``X-Forwarded-Email``,
    and ``server.request`` goes through it). Routes that change server-wide settings (the card
    switch, and later the settings and secret writes) depend on this.

    Refused with 403: a request without ``Sec-Fetch-Site: same-origin``; one with an
    ``Authorization`` header (present at all); a multi-user caller with no session cookie; a
    cookie that does not verify or carries a ``scope``; and a cookie that is neither of the two
    things that count as a sign-in on this device:

    * a browser sign-in: a token without ``grant_id`` plus the proof cookie
      (:mod:`ae_omni_policies.browser_proof`), HMAC(K, the exact session JWT), which the server
      sets only on a browser sign-in response and a CLI never receives;
    * the desktop app's own sign-in (:mod:`ae_omni_policies.desktop_signin`, 2026-11-09,
      https://github.com/analyticendeavors/omnigent-ae/pull/509 follow-up): a token without
      ``grant_id`` whose hash was recorded from a ``POST /auth/native-token`` 200 for a
      loopback sign-in and has not expired, or a renewal (``grant_id`` and
      ``act.client_id`` equal to the login grant client) whose grant was recorded by that same
      sign-in. In both cases the recorded login grant must still be live in the grant store,
      asked on every gated write (the 30 s credential cache of :func:`require_ae_user` does not
      apply), so signing out of the app (``/oauth/revoke``) ends the first native token and its
      renewals at once.

    What this closes (2026-11-08, https://github.com/analyticendeavors/omnigent-ae/issues/506,
    and 2026-11-09): an agent replaying any token a non-desktop, non-browser client received as
    the cookie, with forged ``Sec-Fetch-Site`` and ``Origin`` headers: the token ``omnigent
    login`` stores, a CLI refresh grant and its renewals, the loopback bootstrap token, a runner
    token, a magic-link session, a device grant or a client-credentials token, and the iOS app
    0.3.1's token (it signs in with the CLI ticket flow). In accounts mode this holds together
    with the invite and reset guard in ``browser_proof``.

    What stays open:

    * an agent reading the browser's own cookie store (both cookies sit there), or the desktop
      app's safeStorage file or cookie jar (same user, same class);
    * an agent on the owner's PC that runs its own loopback native sign-in through the system
      browser with single sign-on: it is about as easy as ``omnigent login``, so for an agent
      on the PC the gate is back at CLI strength. Agents on the box cannot do it (their browser
      views use in-memory partitions and refuse loopback, and no browser there is signed in to
      the owner's account);
    * the re-authentication switch: ``AE_DESKTOP_SIGNIN_REAUTH=1`` turns that PC sign-in into
      a password or passkey prompt the owner did not start, but it is off by default, needs
      the ``auth_time`` claim added to the identity provider's ID tokens first, and an agent
      that can decrypt the app's credential file (same user) can still renew from the stored
      grant, so it raises the effort rather than closing the item;
    * an agent that drives the real browser through a sign-in the owner did not start, reads
      the callback URL from history and replays the code at the right endpoint;
    * ``docker exec`` on the box, rewriting ``/data/ae-admin-cards.json``, or reading the cookie
      secret from the container environment;
    * XSS in the web UI (same-origin fetches carry both cookies);
    * a CLI ticket sign-in and a desktop sign-in by the same user in the same second mint
      identical JWTs (no ``jti``); it needs two human sign-ins;
    * admin writes not behind this gate, which stay reachable with a CLI token by design.

    ``Sec-Fetch-Site`` is a header any non-browser client can set, so on its own this is a speed
    bump; with the proof or the recorded sign-in it is a check that the credential came from a
    browser or the desktop app. A single-user runtime (user ``"local"``) has no cookie.

    :raises HTTPException: 403 when any check fails.
    """
    user = await require_ae_user(request)
    why: str | None = None
    if request.headers.get("sec-fetch-site", "").strip().lower() != "same-origin":
        why = "it did not come from a page of this server"
    elif "authorization" in request.headers:
        why = "it carried a bearer credential, which is how an agent calls"
    elif user is not None and user != RESERVED_USER_LOCAL:
        prov = provider()
        cookie_name = getattr(prov, "session_cookie_name", None)
        if cookie_name:
            token = request.cookies.get(cookie_name)
            secret = _cookie_secret(prov)
            payload = _decode_session(token, secret) if token and secret is not None else None
            if not token:
                why = "the sign-in did not come from the session cookie"
            elif payload is None:
                why = "the cookie holds a machine token, not a browser session"
            elif payload.get("grant_id") is not None:
                if not (
                    _login_grant_client(payload)
                    and await desktop_signin.allows(secret, token, payload)
                ):
                    why = "the cookie holds a machine token, not a browser session"
            else:
                secure = cookie_name.startswith("__Host-")
                proof = request.cookies.get(browser_proof.proof_cookie_name(secure))
                if not browser_proof.proof_matches(
                    browser_proof.derive_key(secret), token, proof
                ) and not await desktop_signin.allows(secret, token, payload):
                    why = PROOF_MISSING
    if why is not None:
        if why == PROOF_MISSING:
            raise HTTPException(403, browser_proof.REFUSAL)
        raise HTTPException(
            403,
            f"This change can only be made from the Admin Center page in a browser: {why}. "
            "Open the Admin Center and use the switch there.",
        )
    return user


__all__ = [
    "CODE_BROWSER_ONLY",
    "SESSION_CHECK_TTL_S",
    "provider",
    "require_ae_user",
    "require_browser_caller",
    "reset_provider_cache",
]
