"""The ``/v1/ae/claude-accounts/shared`` routes over :mod:`ae_omni_policies.shared_accounts`.

Added 2026-10-20, so a second AE user's host can use company-paid Claude
accounts that the admin manages, without copying tokens between hosts.

=================================================  ==========  =====================
Route                                              Who         Answers
=================================================  ==========  =====================
``GET  /claude-accounts/shared``                   signed in   names and labels, never
                                                               a token; ``can_manage``
``POST /claude-accounts/shared``                   admin       adds one (name, label,
                                                               token)
``POST /claude-accounts/shared/{name}/label``      admin       relabels one
``POST /claude-accounts/shared/{name}/delete``     admin       removes one
``GET  /claude-accounts/shared/{name}/token``      a host      the token, for one launch
=================================================  ==========  =====================

POST, not PATCH or DELETE, because the Admin Center reaches the server
through patch P2's ``server.request``, which allows only GET and POST under
``/v1/ae/``.

Admin is the server's own answer, ``GET /v1/me`` asked as the caller (the
``users.is_admin`` flag or the ``admins:`` list in ``config/server.yaml``),
so it holds in every auth mode. A single-user runtime has one user, who is
the admin.

The token route is for hosts, never browsers. A Claude session's runner asks
for it at launch with the bearer its host gave it (the session owner's own
identity, so a host owned by a user who is not an admin works), and keeps
the answer in memory for that launch. Two checks keep it away from a
browser, even one running a script on a signed-in page: a request carrying
any ``Sec-Fetch-*`` header is refused (every current browser sends them on
every fetch and a page cannot remove them; httpx sends none), and the
identity is read with the cookie removed, so only a bearer token (or a
trusted proxy's identity header) names the caller. A request that passes both
is then confirmed against ``GET /v1/me`` (the app's own provider, which refuses
a revoked device grant or a deleted account that this package's provider would
still accept). Each fetch is logged with who and which account, never the token.

Limits, stated plainly (2026-10-20 dual review): browsers send ``Sec-Fetch-*``
only to https or localhost origins, so on a plain-http, header-auth deployment
the browser check does not hold and the proxy's identity header alone names the
caller; the box runs OIDC over https. Any signed-in user (or an agent in their
session) can read the token the host receives, and a fetched token stays valid
until revoked at claude.ai; deleting the account here does not revoke it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import JSONResponse
from omnigent.errors import ErrorCode
from omnigent.server.auth import RESERVED_USER_LOCAL, resolve_auth_header
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from starlette.requests import Request as StarletteRequest

from ae_omni_policies import shared_accounts
from ae_omni_policies.auth import provider, require_ae_user

_logger = logging.getLogger(__name__)

router = APIRouter()

CODE_ADMIN_ONLY = "admin_only"
CODE_BROWSER = "shared_account_token_browser"
_BROWSER_HEADERS = ("sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest")
_FORWARDED_HEADERS = ("cookie", "authorization", "origin")
_LOOPBACK_TIMEOUT_S = 15.0
_NAME_PATH = Annotated[str, Path(min_length=1, max_length=64)]
_NO_STORE = {"Cache-Control": "no-store"}


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=_NO_STORE,
    )


def _refusal(exc: shared_accounts.SharedAccountsError) -> JSONResponse:
    return _error(exc.status_code, exc.code, str(exc))


async def _me(request: Request, *, cookie: bool = True) -> dict[str, Any] | None:
    """``GET /v1/me`` asked as the caller (in process unless ``AE_SELF_URL`` is set).

    With ``cookie=False`` the cookie is not forwarded, so only a bearer token (or a
    trusted proxy's identity header) names the caller.
    """
    response = await ask_me(request, cookie=cookie)
    if response is None or response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def ask_me(request: Request, *, cookie: bool = True) -> httpx.Response | None:
    """:func:`_me`'s raw answer, ``None`` when the loopback itself failed.

    ``auth.require_ae_user`` needs to tell "the app refused this credential" from
    "the app did not answer", which :func:`_me` folds together.
    """
    return await ask_as_caller(request, "/v1/me", cookie=cookie)


async def ask_as_caller(
    request: Request, path: str, *, cookie: bool = True
) -> httpx.Response | None:
    """``GET`` *path* on this server as the caller; ``None`` when the loopback failed.

    Shared by ``/v1/me`` and, since 2026-10-23, the Key Vault card's people list
    (``/auth/users``), which the extension page cannot call itself.
    """
    names = {*_FORWARDED_HEADERS, resolve_auth_header().lower()}
    if not cookie:
        names.discard("cookie")
    headers = {key: value for key, value in request.headers.items() if key.lower() in names}
    self_url = (os.environ.get("AE_SELF_URL") or "").rstrip("/")
    try:
        if self_url:
            async with httpx.AsyncClient(base_url=self_url, timeout=_LOOPBACK_TIMEOUT_S) as http:
                response = await http.get(path, headers=headers)
        else:
            transport = httpx.ASGITransport(app=request.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://ae-loopback", timeout=_LOOPBACK_TIMEOUT_S
            ) as http:
                response = await http.get(path, headers=headers)
    except httpx.HTTPError as exc:
        _logger.warning("ae shared accounts: could not ask %s: %s", path, exc)
        return None
    return response


async def _probe_admin(request: Request, user: str | None) -> bool:
    if user is None or user == RESERVED_USER_LOCAL:
        return True
    me = await _me(request)
    return bool(me and me.get("user_id") == user and me.get("is_admin") is True)


#: Swapped by tests; production asks ``/v1/me`` as the caller.
admin_probe: Callable[[Request, str | None], Awaitable[bool]] = _probe_admin


async def _probe_session(request: Request, user: str | None) -> bool:
    """Whether the server's own provider still accepts the caller's credential.

    ``auth.provider()`` is this package's own provider, which lacks the two checks
    ``create_app`` installs on the app's (a revoked device grant, a deleted or
    disabled account's older JWT). ``/v1/me`` goes through the app's provider, so
    asking it closes that gap for the one route that hands out a secret.
    """
    if user is None or user == RESERVED_USER_LOCAL:
        return True
    me = await _me(request, cookie=False)
    return bool(me and me.get("user_id") == user)


#: Swapped by tests; production asks ``/v1/me`` as the caller, without the cookie.
session_probe: Callable[[Request, str | None], Awaitable[bool]] = _probe_session


async def _is_admin(request: Request, user: str | None) -> bool:
    return await admin_probe(request, user)


def _who(user: str | None) -> str:
    return (user or "").strip() or RESERVED_USER_LOCAL


def _admin_refusal() -> JSONResponse:
    return _error(
        403,
        CODE_ADMIN_ONLY,
        "Only an OmniGent admin can add, relabel or remove shared Claude accounts.",
    )


@router.get("/claude-accounts/shared")
async def shared_list(
    request: Request, user: Annotated[str | None, Depends(require_ae_user)]
) -> JSONResponse:
    """Every shared account's name and label for any signed-in user; never a token."""
    try:
        views = await asyncio.to_thread(shared_accounts.store().views)
    except shared_accounts.SharedAccountsError as exc:
        return _refusal(exc)
    return JSONResponse(
        {
            "accounts": [view.model_dump() for view in views],
            "can_manage": await _is_admin(request, user),
            "key_configured": shared_accounts.key_configured(),
        },
        headers=_NO_STORE,
    )


@router.post(
    "/claude-accounts/shared",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def shared_add(
    request: Request,
    body: shared_accounts.AddRequest,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> JSONResponse:
    """Add a shared account (admins only); the answer is its listing, without the token."""
    if not await _is_admin(request, user):
        return _admin_refusal()
    who = _who(user)
    try:
        view = await asyncio.to_thread(
            shared_accounts.store().add, body.name, body.label, body.token, who
        )
    except shared_accounts.SharedAccountsError as exc:
        return _refusal(exc)
    _logger.info("shared Claude account %s added by %s", view.name, who)
    return JSONResponse({"account": view.model_dump()}, status_code=201, headers=_NO_STORE)


@router.post(
    "/claude-accounts/shared/{name}/label",
    response_model=None,
    dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
)
async def shared_relabel(
    request: Request,
    name: _NAME_PATH,
    body: shared_accounts.LabelRequest,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> JSONResponse:
    """Change a shared account's label (admins only)."""
    if not await _is_admin(request, user):
        return _admin_refusal()
    who = _who(user)
    try:
        view = await asyncio.to_thread(shared_accounts.store().relabel, name, body.label, who)
    except shared_accounts.SharedAccountsError as exc:
        return _refusal(exc)
    _logger.info("shared Claude account %s relabelled by %s", view.name, who)
    return JSONResponse({"account": view.model_dump()}, headers=_NO_STORE)


@router.post(
    "/claude-accounts/shared/{name}/delete",
    response_model=None,
    dependencies=[Depends(require_trusted_origin)],
)
async def shared_delete(
    request: Request,
    name: _NAME_PATH,
    user: Annotated[str | None, Depends(require_ae_user)],
) -> JSONResponse:
    """Remove a shared account and its token (admins only); any body is ignored."""
    if not await _is_admin(request, user):
        return _admin_refusal()
    who = _who(user)
    try:
        view = await asyncio.to_thread(shared_accounts.store().delete, name)
    except shared_accounts.SharedAccountsError as exc:
        return _refusal(exc)
    _logger.info("shared Claude account %s removed by %s", view.name, who)
    return JSONResponse({"account": view.model_dump()}, headers=_NO_STORE)


def _without_cookie(request: Request) -> StarletteRequest:
    """*request* with its ``Cookie`` header dropped, for reading a bearer-only identity."""
    scope = dict(request.scope)
    scope["headers"] = [pair for pair in request.scope["headers"] if pair[0] != b"cookie"]
    return StarletteRequest(scope)


@router.get("/claude-accounts/shared/{name}/token", response_model=None)
async def shared_token(request: Request, name: _NAME_PATH) -> JSONResponse:
    """One shared account's token for a host's launch; refused to a browser.

    Any signed-in user's host may ask: the accounts are shared on purpose. The
    answer is never cached (``Cache-Control: no-store``) and the host keeps it
    in memory for the one launch.
    """
    if any(request.headers.get(header) for header in _BROWSER_HEADERS):
        return _error(
            403,
            CODE_BROWSER,
            "Shared Claude account tokens go to hosts only, never to a browser.",
        )
    user = provider().get_user_id(_without_cookie(request))
    if user is None:
        return _error(
            401,
            ErrorCode.UNAUTHORIZED,
            "Authentication required: send the host's bearer token, not a browser cookie.",
        )
    if not await session_probe(request, user):
        return _error(
            401,
            ErrorCode.UNAUTHORIZED,
            "The server no longer accepts this host's sign-in. Sign the host in again.",
        )
    try:
        answer = await asyncio.to_thread(shared_accounts.store().token, name)
    except shared_accounts.SharedAccountsError as exc:
        return _refusal(exc)
    _logger.info("shared Claude account %s token fetched by %s for a launch", name, user)
    return JSONResponse(
        {
            "name": answer.name,
            "account": shared_accounts.account_id(answer.name),
            "label": answer.label,
            "token": answer.token,
        },
        headers=_NO_STORE,
    )


def _register_fork_hook() -> bool:
    """Tell upstream (patch 0117's hook) which shared accounts this server holds."""
    try:
        from omnigent.onboarding.ae_shared_claude_accounts import set_server_resolver
    except ImportError:
        return False
    set_server_resolver(shared_accounts.is_known_account)
    return True


#: Whether a fork may pick a shared account (the upstream hook is present).
FORK_HOOK_REGISTERED = _register_fork_hook()

__all__ = [
    "CODE_ADMIN_ONLY",
    "CODE_BROWSER",
    "FORK_HOOK_REGISTERED",
    "admin_probe",
    "ask_as_caller",
    "router",
    "session_probe",
]
