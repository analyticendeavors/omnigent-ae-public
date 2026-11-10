"""Record the desktop app's own sign-in, so the browser-only gate can accept it.

Why (2026-11-09, follow-up to https://github.com/analyticendeavors/omnigent-ae/pull/509): the
gate :func:`ae_omni_policies.auth.require_browser_caller` wants a browser proof cookie. The
desktop app signs in through ``POST /auth/native-token``, receives its session in a JSON body
and installs the cookie itself, so it can never hold the proof. Every renewal after that is a
token carrying ``grant_id``, which the gate refused outright.

This middleware makes that one sign-in count and nothing else. When the response of upstream's
own ``native_token`` endpoint is a 200 for a loopback sign-in, it records:

* HMAC(Kd, session token) with the token's ``exp``;
* the login grant issued in the same response, found through the grant store by the refresh
  token's hash and required to be a login grant of the token's own user: its id (an identifier
  with no authority; a token needs the cookie secret) and HMAC(Kd, grant id);
* HMAC(Kd, user id), used only to cap entries per user.

No refresh token, or one that maps to no login grant of that user, records nothing.
Kd = HMAC(cookie secret, ``ae-desktop-signin-v1``). On every gated write the gate asks
:func:`allows`: a token without ``grant_id`` passes when its hash is recorded and unexpired, a
token with ``grant_id`` (a renewal) when its grant is recorded and ``act.client_id`` is the login
grant client, and in both cases only while that grant is still live in the grant store (exists,
redeemed, not revoked, inside its lifetime), checked each time with no cache. So signing out in
the app (``/oauth/revoke``) ends the first native token too, at once. The CLI never calls
``/auth/native-token`` (it uses ``/auth/cli-poll``), so a CLI ticket token, a CLI refresh-grant
renewal, a bootstrap token or a runner token is never recorded and stays refused. With no grant
store reachable, :func:`install` installs nothing and the desktop is refused.

Recorded only when all hold: the matched route is upstream's ``native_token`` function (read
from ``scope["endpoint"]`` once routing has run; when a configured base path made upstream copy
the scope, only the exact path base path + ``/auth/native-token``; never a path suffix); the
method is POST; the status is 200; the request body is ``application/x-www-form-urlencoded``,
under 16 KiB, with exactly one ``redirect_uri`` and that one an RFC 8252 loopback address (the
iOS private-use redirect is not recorded). The request body is only watched as the route reads it.

The store is ``/data/ae-desktop-signins.json`` (``AE_DESKTOP_SIGNINS_FILE`` moves it; empty
means memory only, as does a missing ``/data``), mode 0600, written by temp file and
``os.replace`` under one process-wide lock, and reloaded when the file's mtime changed. Fails
closed: a missing or corrupt file reads as empty; a failed write is logged without values and
leaves the response untouched, so the desktop is refused until the next sign-in.

``AE_DESKTOP_SIGNIN_REAUTH=1`` (off by default) adds ``reauth=1`` to ``GET /auth/login``
requests that carry the native sign-in parameters, which makes upstream ask the identity
provider for a fresh authentication. It needs the ``auth_time`` claim in the provider's ID
tokens first, or every native sign-in is refused.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_logger = logging.getLogger(__name__)

KEY_CONTEXT = b"ae-desktop-signin-v1"
OIDC_SECRET_ENV = "OMNIGENT_OIDC_COOKIE_SECRET"
STATE_FILE_ENV = "AE_DESKTOP_SIGNINS_FILE"
REAUTH_ENV = "AE_DESKTOP_SIGNIN_REAUTH"
DATA_DIR = Path("/data")
DATA_FILE_NAME = "ae-desktop-signins.json"

MAX_BODY_BYTES = 16 * 1024
MAX_ENTRIES_PER_USER = 8
NATIVE_TOKEN_SUFFIX = "/auth/native-token"
LOGIN_SUFFIX = "/auth/login"
_FORM_TYPE = "application/x-www-form-urlencoded"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})
_DEFAULT_GRANT_LIFETIME_S = 30 * 24 * 3600


def derive_key(secret: str | bytes) -> bytes:
    """Kd = HMAC-SHA256(cookie secret, ``ae-desktop-signin-v1``)."""
    raw = secret.encode("utf-8") if isinstance(secret, str) else secret
    return hmac.new(raw, KEY_CONTEXT, hashlib.sha256).digest()


def keyed(key: bytes, value: str) -> str:
    """HMAC-SHA256(Kd, value) as hex."""
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


_lifetime: int | None = None


def grant_lifetime_s() -> int:
    """The login grant's absolute lifetime, as upstream reads it (days in its env override).

    Read once per process: upstream also resolves it once, when it mounts the token router.
    """
    global _lifetime
    if _lifetime is None:
        try:
            from omnigent.server.routes.device_auth import _grant_max_lifetime_seconds

            _lifetime = int(_grant_max_lifetime_seconds())
        except Exception:  # noqa: BLE001 (a renamed upstream helper: use its documented default)
            _lifetime = _DEFAULT_GRANT_LIFETIME_S
    return _lifetime


# ── store ────────────────────────────────────────────────────────────────────────────────────

_lock = threading.RLock()


class SignInStore:
    """Recorded desktop sign-ins: keyed hashes, expiry and created time. Nothing reversible."""

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._entries: list[dict[str, Any]] = []
        self._mtime: int | None = None
        self._failed = False

    # Callers hold ``_lock``.
    def _reload(self) -> None:
        if self._path is None:
            return
        try:
            mtime = self._path.stat().st_mtime_ns
        except OSError:
            self._entries, self._mtime = [], None
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            rows = data["entries"]
            clean = [
                {
                    "t": str(r["t"]),
                    "g": None if r.get("g") is None else str(r["g"]),
                    "i": None if r.get("i") is None else str(r["i"]),
                    "u": str(r["u"]),
                    "exp": int(r["exp"]),
                    "c": int(r["c"]),
                }
                for r in rows
            ]
        except (OSError, ValueError, KeyError, TypeError):
            _logger.warning(
                "ae desktop sign-in: %s is unreadable; reading it as empty", self._path
            )
            clean = []
        self._entries = clean

    def _write(self) -> bool:
        if self._path is None:
            return True
        tmp: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".ae-desktop-signins-", dir=str(self._path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "entries": self._entries}, handle, separators=(",", ":"))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError as exc:
            _logger.warning("ae desktop sign-in: could not write %s: %s", self._path, exc)
            if tmp is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
            return False
        try:
            self._mtime = self._path.stat().st_mtime_ns
        except OSError:
            self._mtime = None  # the next read reloads; the write itself succeeded
        return True

    def _prune(self, now: int, lifetime: int) -> None:
        # An entry without a grant (none are written any more) lives by the token's expiry alone.
        self._entries = [
            e
            for e in self._entries
            if now < e["exp"] or (e["g"] is not None and now < e["c"] + lifetime)
        ]

    def record(
        self, token_h: str, grant_h: str, grant_id: str, user_h: str, exp: int, now: int
    ) -> bool:
        """Add one sign-in; False when it could not be saved (the entry is then dropped again)."""
        lifetime = grant_lifetime_s()
        with _lock:
            self._reload()
            before = list(self._entries)
            self._prune(now, lifetime)
            self._entries = [e for e in self._entries if e["t"] != token_h]
            mine = [e for e in self._entries if e["u"] == user_h]
            while len(mine) >= MAX_ENTRIES_PER_USER:
                oldest = min(mine, key=lambda e: e["c"])
                mine.remove(oldest)
                self._entries.remove(oldest)  # only this user's own oldest entry ever goes
            self._entries.append(
                {"t": token_h, "g": grant_h, "i": grant_id, "u": user_h, "exp": exp, "c": now}
            )
            if self._write():
                return True
            self._entries = before
            return False

    def grant_id_for_token(self, token_h: str, now: float) -> str | None:
        """The login grant id recorded with this token, when the token is unexpired."""
        with _lock:
            self._reload()
            for e in self._entries:
                if hmac.compare_digest(e["t"], token_h) and now < e["exp"]:
                    return e["i"]
            return None

    def grant_id_for_grant(self, grant_h: str, now: float) -> str | None:
        """The login grant id of a recorded grant (by its keyed hash), inside its lifetime."""
        lifetime = grant_lifetime_s()
        with _lock:
            self._reload()
            for e in self._entries:
                if (
                    e["g"] is not None
                    and hmac.compare_digest(e["g"], grant_h)
                    and now < e["c"] + lifetime
                ):
                    return e["i"]
            return None

    def snapshot(self) -> list[dict[str, Any]]:
        """A copy of the entries (tests)."""
        with _lock:
            self._reload()
            return [dict(e) for e in self._entries]


def _default_path() -> Path | None:
    if STATE_FILE_ENV in os.environ:
        raw = (os.environ.get(STATE_FILE_ENV) or "").strip()
        return Path(raw) if raw else None
    if DATA_DIR.is_dir():
        return DATA_DIR / DATA_FILE_NAME
    return None


_store: SignInStore | None = None
_store_lock = threading.Lock()


def store() -> SignInStore:
    """The process-wide store, built on first use from :data:`STATE_FILE_ENV` or ``/data``."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SignInStore(_default_path())
        return _store


def reset_store(replacement: SignInStore | None = None) -> None:
    """Drop the process-wide store and the cached lifetime; with no replacement, the grant
    store too (tests)."""
    global _store, _lifetime, _grants_factory
    with _store_lock:
        _store = replacement
        _lifetime = None
        if replacement is None:
            _grants_factory = None


# ── gate helpers ─────────────────────────────────────────────────────────────────────────────


_grants_factory: Callable[[], Any] | None = None


def set_grants(factory: Callable[[], Any] | None) -> None:
    """The grant store the gate asks whether a desktop sign-in's grant is still live."""
    global _grants_factory
    _grants_factory = factory


def _grant_is_live(grant_id: str, now: float) -> bool:
    """Whether the grant exists, is redeemed (not revoked), is a login grant and is in date."""
    factory = _grants_factory
    if factory is None:
        return False
    try:
        grant = factory().get_by_id(grant_id)
    except Exception:  # noqa: BLE001 (no answer is a refusal)
        _logger.warning("ae desktop sign-in: could not read the login grant; refusing")
        return False
    from omnigent.server.routes.device_auth import LOGIN_GRANT_CLIENT_ID

    return (
        grant is not None
        and grant.status == "redeemed"
        and grant.client_id == LOGIN_GRANT_CLIENT_ID
        and now < grant.created_at + grant_lifetime_s()
    )


async def allows(secret: str | bytes, token: str, claims: Mapping[str, Any]) -> bool:
    """Whether a desktop sign-in vouches for *token* right now.

    A renewal (``grant_id`` claim) needs its grant recorded by a native sign-in; a token with no
    ``grant_id`` needs its own hash recorded and unexpired. Either way the recorded login grant
    must still be live in the grant store, asked on every call (the gate's 30 s credential
    cache does not apply), so a revoked grant stops vouching at once, and the first native
    token with it. Runs off the event loop.
    """

    def work() -> bool:
        key = derive_key(secret)
        now = time.time()
        claimed = claims.get("grant_id")
        if claimed is not None:
            recorded = store().grant_id_for_grant(keyed(key, str(claimed)), now)
        else:
            recorded = store().grant_id_for_token(keyed(key, token), now)
        return recorded is not None and _grant_is_live(recorded, now)

    return await run_in_threadpool(work)


# ── middleware ───────────────────────────────────────────────────────────────────────────────


def is_loopback_redirect(raw: str) -> bool:
    """RFC 8252 loopback redirect, as upstream accepts it: ``http://127.0.0.1:<port>/...`` or
    ``http://[::1]:<port>/...`` with an explicit port, no userinfo, query or fragment."""
    if not raw or "?" in raw or "#" in raw or "\\" in raw or any(c.isspace() for c in raw):
        return False
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "http"
        and parts.hostname in _LOOPBACK_HOSTS
        and port is not None
        and port > 0
        and parts.username is None
        and parts.password is None
    )


def _mounted(path: str, route: str, base_path: str) -> bool:
    """Whether *path* is exactly *route*, bare or under the configured base path."""
    return path == route or (bool(base_path) and path == base_path + route)


def _is_native_token_endpoint(scope: Scope, base_path: str = "") -> bool:
    """Whether this request reached upstream's own ``native_token`` function.

    Normally read from the endpoint routing matched. Upstream's ``BasePathMiddleware`` copies
    the scope when it strips a base path, so the endpoint never reaches this middleware's
    scope; then only the exact mounted path counts (never a suffix).
    """
    endpoint = scope.get("endpoint")
    if endpoint is None:
        route = scope.get("route")
        endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return _mounted(scope["path"], NATIVE_TOKEN_SUFFIX, base_path)
    return (
        getattr(endpoint, "__module__", None) == "omnigent.server.routes.auth"
        and getattr(endpoint, "__name__", None) == "native_token"
    )


def request_is_loopback_sign_in(headers: Headers, body: bytes | None) -> bool:
    """Form body, under the cap, exactly one ``redirect_uri``, and a loopback one."""
    if body is None or len(body) > MAX_BODY_BYTES:
        return False
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() != _FORM_TYPE:
        return False
    try:
        pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return False
    uris = [v for k, v in pairs if k == "redirect_uri"]
    return len(uris) == 1 and is_loopback_redirect(uris[0])


def record_sign_in(
    secret: str | bytes,
    response_body: bytes,
    grants: Callable[[], Any] | None,
    sign_ins: SignInStore | None = None,
    now: int | None = None,
) -> bool:
    """Record the sign-in a ``native_token`` 200 body describes; False when nothing was recorded.

    Recorded only when the token verifies under the cookie secret as a plain session token AND
    the response carries a refresh token that maps, through the grant store, to a login grant of
    the token's own user. Any failed check records nothing and logs which one (never a value).
    """
    import jwt

    try:
        body = json.loads(response_body)
        token = body["token"]
        raw_secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        claims = jwt.decode(token, raw_secret, algorithms=["HS256"])
        user = str(claims["sub"])
        exp = int(claims["exp"])
    except (ValueError, KeyError, TypeError, jwt.InvalidTokenError, jwt.InvalidKeyError):
        _logger.warning("ae desktop sign-in: not recorded, the token did not verify")
        return False
    if claims.get("grant_id") is not None or claims.get("scope") is not None:
        _logger.warning("ae desktop sign-in: not recorded, the token is not a plain session")
        return False
    refresh = body.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        _logger.warning("ae desktop sign-in: not recorded, the response has no refresh token")
        return False
    if grants is None:
        _logger.warning("ae desktop sign-in: not recorded, no grant store")
        return False
    try:
        from omnigent.server.device_grant_store import hash_secret
        from omnigent.server.routes.device_auth import LOGIN_GRANT_CLIENT_ID

        grant_store = grants()
        grant = grant_store.get_by_refresh_hash(hash_secret(refresh, raw_secret))
    except Exception:  # noqa: BLE001 (no lookup, no record)
        _logger.warning("ae desktop sign-in: not recorded, the grant lookup failed")
        return False
    if grant is None:
        _logger.warning("ae desktop sign-in: not recorded, the refresh token maps to no grant")
        return False
    if grant.client_id != LOGIN_GRANT_CLIENT_ID:
        _logger.warning("ae desktop sign-in: not recorded, the grant is not a login grant")
        return False
    if grant.user_id != user:
        _logger.warning("ae desktop sign-in: not recorded, the grant belongs to another user")
        return False
    key = derive_key(secret)
    target = sign_ins if sign_ins is not None else store()
    return target.record(
        keyed(key, token),
        keyed(key, grant.id),
        grant.id,
        keyed(key, user),
        exp,
        int(time.time()) if now is None else now,
    )


_unrecorded_logged = False


def _log_unrecorded_once() -> None:
    """One warning per process when a 200 from the native-token path was not recorded."""
    global _unrecorded_logged
    if not _unrecorded_logged:
        _unrecorded_logged = True
        _logger.warning(
            "ae desktop sign-in: a 200 from a path ending in /auth/native-token was not "
            "recorded (not the matched route, not a loopback form, or a failed check); "
            "the desktop app will be refused for gated writes"
        )


class DesktopSignIn:
    """ASGI middleware: record the desktop's native sign-in; optionally force re-authentication."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        secret: Callable[[], str | bytes | None],
        grants: Callable[[], Any] | None = None,
        reauth: bool = False,
        base_path: str = "",
    ) -> None:
        self.app = app
        self._secret = secret
        self._grants = grants
        self._reauth = reauth
        self._base_path = base_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path, method = scope["path"], scope["method"]
        if self._reauth and method == "GET" and _mounted(path, LOGIN_SUFFIX, self._base_path):
            scope = self._with_reauth(scope)
        if method != "POST" or not path.endswith(NATIVE_TOKEN_SUFFIX):
            # Cheap prefilter only; the decision to record is the matched endpoint's.
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        complete = False
        overflow = False

        async def tapped() -> Message:
            nonlocal size, complete, overflow
            message = await receive()
            if message["type"] == "http.request" and not overflow:
                piece = message.get("body", b"")
                size += len(piece)
                if size > MAX_BODY_BYTES:
                    overflow = True
                    chunks.clear()
                else:
                    chunks.append(piece)
                complete = not message.get("more_body", False)
            return message

        start: Message | None = None
        held: list[bytes] = []
        passthrough = False

        async def watched(message: Message) -> None:
            nonlocal start, passthrough
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                headers = Headers(scope=scope)
                recordable = (
                    message["status"] == 200
                    and _is_native_token_endpoint(scope, self._base_path)
                    and complete
                    and not overflow
                    and request_is_loopback_sign_in(headers, b"".join(chunks))
                )
                if not recordable:
                    if message["status"] == 200:
                        _log_unrecorded_once()
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            if message["type"] != "http.response.body":
                # Anything else (trailers, extensions) must not overtake the held start.
                if start is not None:
                    held_start, start = start, None
                    passthrough = True
                    _log_unrecorded_once()
                    await send(held_start)
                    for chunk in held:
                        await send(
                            {"type": "http.response.body", "body": chunk, "more_body": True}
                        )
                await send(message)
                return
            if start is None:
                await send(message)
                return
            held.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            body = b"".join(held)
            if not await self._record(body):
                _log_unrecorded_once()
            await send(start)
            await send({"type": "http.response.body", "body": body, "more_body": False})

        await self.app(scope, tapped, watched)

    async def _record(self, body: bytes) -> bool:
        try:
            secret = self._secret()
            if secret is None:
                return False
            return bool(await run_in_threadpool(record_sign_in, secret, body, self._grants))
        except Exception:  # noqa: BLE001 (recording never changes the sign-in response)
            _logger.warning("ae desktop sign-in: could not record a sign-in")
            return False

    @staticmethod
    def _with_reauth(scope: Scope) -> Scope:
        """``reauth=1`` on a ``/auth/login`` request whose native sign-in redirect is loopback."""
        raw = scope.get("query_string", b"").decode("latin-1")
        pairs = parse_qsl(raw, keep_blank_values=True)
        uris = [v for k, v in pairs if k == "native_redirect_uri"]
        if len(uris) != 1 or not is_loopback_redirect(uris[0]):
            return scope
        from urllib.parse import urlencode

        kept = [(k, v) for k, v in pairs if k != "reauth"] + [("reauth", "1")]
        return {**scope, "query_string": urlencode(kept).encode("latin-1")}


def install(
    app: Any,
    environ: Mapping[str, str],
    auth_source: str | None = None,
    *,
    permission_store: Any = None,
) -> bool:
    """Add the middleware for OIDC sign-in, the only mode with ``/auth/native-token``.

    Accounts mode has no native sign-in route (upstream mounts it only in
    ``create_auth_router``, the OIDC router), so nothing is installed there.

    :param permission_store: The store ``create_app`` was given; its ``storage_location`` is
        where upstream keeps device grants, so a grant store on the same database finds the
        login grant a native sign-in just created.
    :returns: Whether it was added.
    """
    if auth_source is None:
        from omnigent.server.auth import resolve_auth_source

        auth_source = resolve_auth_source()
    if auth_source != "oidc":
        return False
    secret_hex = environ.get(OIDC_SECRET_ENV, "").strip()
    if not secret_hex:
        return False
    try:
        bytes.fromhex(secret_hex)
    except ValueError:
        _logger.warning("%s is not hex; desktop sign-in recording stays off.", OIDC_SECRET_ENV)
        return False
    from ae_omni_policies.auth import _cookie_secret, provider

    def secret() -> str | bytes | None:
        return _cookie_secret(provider())

    location = getattr(permission_store, "storage_location", None)
    grant_store: Any = None
    if location:
        try:
            from omnigent.server.device_grant_store import DeviceGrantStore

            grant_store = DeviceGrantStore(location)
        except Exception:  # noqa: BLE001 (no usable store: install nothing)
            _logger.warning(
                "ae desktop sign-in: the grant store could not be opened", exc_info=False
            )
    if grant_store is None:
        _logger.warning(
            "ae desktop sign-in: no grant store is reachable; nothing is installed and the "
            "desktop app is refused for gated writes"
        )
        return False

    def grants() -> Any:
        return grant_store

    set_grants(grants)
    app.add_middleware(
        DesktopSignIn,
        secret=secret,
        grants=grants,
        reauth=environ.get(REAUTH_ENV, "").strip() == "1",
        base_path=str(getattr(getattr(app, "state", None), "base_path", "") or ""),
    )
    return True
