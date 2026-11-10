"""The browser proof cookie and the gate that requires it (2026-11-08, issue 506).

Built from upstream's own routers and minters, never hand-built claim dicts: the real OIDC
router (the IdP mocked as ``upstream/tests/server/test_oidc_callback.py`` does), the real
accounts router and bootstrap, and the real ``/v1/ae`` gated routes. Nothing here reaches
``/data`` or a live server: the AE state files are pointed at memory by ``conftest.py``, the
IdP is mocked, and every call that passes the gate carries the body ``{}`` (422, never a write).

https://github.com/analyticendeavors/omnigent-ae/issues/506
"""

from __future__ import annotations

import dataclasses
import json
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import mint_session_cookie
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import Receive, Scope, Send

from ae_omni_policies import auth, browser_proof, session_cookie

GATED = ("/v1/ae/cards/state", "/v1/ae/git/mine/check")
OWNER = "owner@example.com"
OTHER = "other@example.com"
ACCOUNTS_PASSWORD = "admin-pw-12345"
REFUSED = browser_proof.REFUSAL


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


def _set_cookies(response: httpx.Response) -> dict[str, str]:
    """Every ``Set-Cookie`` of a response, by cookie name, as the raw header text."""
    out: dict[str, str] = {}
    for raw in response.headers.get_list("set-cookie"):
        out[raw.split("=", 1)[0].strip()] = raw
    return out


def _value(raw: str) -> str:
    return raw.split(";", 1)[0].split("=", 1)[1].strip().strip('"')


def _attrs(raw: str) -> dict[str, str]:
    parts = [p.strip() for p in raw.split(";")[1:]]
    return {p.split("=", 1)[0].lower(): (p.split("=", 1)[1] if "=" in p else "") for p in parts}


@dataclass
class Stack:
    """A production-shaped app (real auth routers, AE layer, proof middleware)."""

    client: TestClient
    provider: UnifiedAuthProvider
    app: FastAPI
    secret: bytes
    secure: bool
    mode: str

    @property
    def origin(self) -> str:
        return ("https" if self.secure else "http") + "://testserver"

    @property
    def session_name(self) -> str:
        return self.provider.session_cookie_name  # type: ignore[no-any-return]

    @property
    def proof_name(self) -> str:
        return browser_proof.proof_cookie_name(self.secure)

    @property
    def key(self) -> bytes:
        return browser_proof.derive_key(self.secret)

    def proof_for(self, token: str, key: bytes | None = None) -> str:
        return browser_proof.expected_proof(key or self.key, token)

    def cookie(
        self, token: str, proof: str | None = None, *, proof_name: str | None = None
    ) -> str:
        text = f"{self.session_name}={token}"
        if proof is not None:
            text += f"; {proof_name or self.proof_name}={proof}"
        return text

    def gated(
        self,
        path: str,
        cookie: str | None,
        *,
        headers: dict[str, str] | None = None,
        site: str = "same-origin",
    ) -> httpx.Response:
        sent = {
            "Sec-Fetch-Site": site,
            "Origin": self.origin,
            "Content-Type": "application/json",
            **(headers or {}),
        }
        if cookie is not None:
            sent["Cookie"] = cookie
        return self.client.post(path, content=b"{}", headers=sent)

    def passes(self, path: str, cookie: str) -> bool:
        """Whether the gate let the call through. ``{}`` can only then fail validation (422)."""
        response = self.gated(path, cookie)
        assert response.status_code in (403, 422), (path, response.status_code, response.text)
        return response.status_code == 422

    def refused(
        self, path: str, cookie: str | None, *, codes: tuple[int, ...] = (403,), **kwargs: Any
    ) -> httpx.Response:
        response = self.gated(path, cookie, **kwargs)
        assert response.status_code in codes, (path, response.status_code, response.text)
        return response


def _install_proof(app: FastAPI, environ: dict[str, str], mode: str) -> None:
    assert browser_proof.install(app, environ, mode) is True


# ── OIDC stack ────────────────────────────────────────────────────────────────────────────────


@dataclass
class Oidc(Stack):
    pending: list[str] = dataclasses.field(default_factory=lambda: [""])
    keys: Any = None
    state_cookie: str = ""

    def sign_in(
        self,
        *,
        email: str = OWNER,
        ticket: str | None = None,
        native: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Drive ``/auth/callback`` with a valid state cookie; redirects are not followed."""
        self.pending[0] = self.keys.sign_id_token({"email": email, "email_verified": True})
        state = "state-token-xyz"
        payload: dict[str, Any] = {
            "state": state,
            "code_verifier": "verifier",
            "return_to": "/",
            "exp": int(time.time()) + 300,
        }
        if ticket:
            payload["ticket"] = ticket
        if native:
            payload["native"] = native
        self.client.cookies.set(
            self.state_cookie, jwt.encode(payload, self.secret, algorithm="HS256")
        )
        response = self.client.get(
            f"/auth/callback?code=auth-code&state={state}", follow_redirects=False
        )
        self.client.cookies.clear()
        return response


@pytest.fixture(params=[True, False], ids=["https", "http"])
def oidc(
    request: pytest.FixtureRequest,
    ae_admins: set[str],
    ae_stores: dict[str, Any],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Oidc]:
    from omnigent.server.app import create_app
    from omnigent.server.routes.auth import _AUTH_STATE_COOKIE_PLAIN, _AUTH_STATE_COOKIE_SECURE
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from tests.server.test_oidc_callback import _IdpKeys, _oidc_config

    secure: bool = request.param
    secret = bytes(range(32))
    config = dataclasses.replace(
        _oidc_config(),
        cookie_secret=secret,
        redirect_uri=f"{'https' if secure else 'http'}://testserver/auth/callback",
    )
    provider = UnifiedAuthProvider("oidc", oidc_config=config, local_single_user=False)
    keys = _IdpKeys()
    pending = [""]

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, json={"id_token": pending[0]})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(
        jwt.PyJWKClient, "get_signing_key_from_jwt", lambda self, token: keys.signing_key
    )
    ae_admins.add(OWNER)
    app = create_app(
        **ae_stores, permission_store=SqlAlchemyPermissionStore(db_uri), auth_provider=provider
    )
    environ = {
        browser_proof.OIDC_SECRET_ENV: secret.hex(),
        "OMNIGENT_OIDC_REDIRECT_URI": config.redirect_uri,
    }
    _install_proof(app, environ, "oidc")
    auth.reset_provider_cache(provider)
    base = "https://testserver" if secure else "http://testserver"
    with TestClient(app, base_url=base) as client:
        yield Oidc(
            client=client,
            provider=provider,
            app=app,
            secret=secret,
            secure=secure,
            mode="oidc",
            pending=pending,
            keys=keys,
            state_cookie=_AUTH_STATE_COOKIE_SECURE if secure else _AUTH_STATE_COOKIE_PLAIN,
        )


def _browser_cookie(stack: Oidc, email: str = OWNER) -> tuple[str, str, httpx.Response]:
    response = stack.sign_in(email=email)
    cookies = _set_cookies(response)
    return _value(cookies[stack.session_name]), _value(cookies[stack.proof_name]), response


def test_an_oidc_redirect_sign_in_sets_the_proof_and_the_gate_passes_it(oidc: Oidc) -> None:
    token, proof, response = _browser_cookie(oidc)
    assert response.status_code == 302
    raw = _set_cookies(response)[oidc.proof_name]
    attrs = _attrs(raw)
    session_attrs = _attrs(_set_cookies(response)[oidc.session_name])
    assert proof == oidc.proof_for(token)
    assert oidc.proof_name == ("__Host-ae_browser_proof" if oidc.secure else "ae_browser_proof")
    assert "httponly" in attrs
    assert attrs["samesite"].lower() == "strict"
    assert attrs["path"] == "/"
    assert ("secure" in attrs) is oidc.secure
    assert attrs["max-age"] == session_attrs["max-age"]
    for path in GATED:
        assert oidc.passes(path, oidc.cookie(token, proof))


def test_the_cli_ticket_page_sets_the_proof_and_the_cli_gets_the_token_without_it(
    oidc: Oidc,
) -> None:
    ticket = oidc.client.post("/auth/cli-login").json()["ticket"]
    response = oidc.sign_in(ticket=ticket)
    assert response.status_code == 200
    cookies = _set_cookies(response)
    assert oidc.proof_name in cookies
    assert _value(cookies[oidc.proof_name]) == oidc.proof_for(_value(cookies[oidc.session_name]))

    poll = oidc.client.get(f"/auth/cli-poll?ticket={ticket}")
    assert poll.status_code == 200
    body = poll.json()
    assert "set-cookie" not in poll.headers  # the poll answer carries no proof, nor any cookie
    cli_token = body["token"]
    assert cli_token == _value(cookies[oidc.session_name])  # the CLI holds the same JWT...
    # ...and replayed as the cookie it is refused, with and without a made-up proof.
    for path in GATED:
        oidc.refused(path, oidc.cookie(cli_token))
        oidc.refused(path, oidc.cookie(cli_token, "0" * 64))


def test_the_native_sign_in_and_token_exchange_set_no_proof(oidc: Oidc) -> None:
    import base64
    import hashlib

    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    native = {
        "redirect_uri": "ai.omnigent.ios:/oauth/callback",
        "state": "native-state",
        "code_challenge": challenge.decode(),
    }
    response = oidc.sign_in(native=native)
    assert response.status_code == 302
    assert _set_cookies(response).get(oidc.proof_name) is None
    assert oidc.session_name not in _set_cookies(response)
    code = httpx.URL(response.headers["location"]).params["code"]
    exchange = oidc.client.post(
        "/auth/native-token",
        data={
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": native["redirect_uri"],
        },
    )
    assert exchange.status_code == 200, exchange.text
    assert "set-cookie" not in exchange.headers
    token = exchange.json()["token"]
    for path in GATED:
        oidc.refused(path, oidc.cookie(token))


def test_a_refresh_grant_token_a_runner_token_and_a_bearer_are_refused(oidc: Oidc) -> None:
    ticket = oidc.client.post("/auth/cli-login").json()["ticket"]
    oidc.sign_in(ticket=ticket)
    body = oidc.client.get(f"/auth/cli-poll?ticket={ticket}").json()
    refresh = body["refresh_token"]
    renewed = oidc.client.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
    ).json()["access_token"]
    assert jwt.decode(renewed, oidc.secret, algorithms=["HS256"])["grant_id"]
    runner = oidc.provider.mint_runner_token(OWNER, 600)
    assert runner
    for token in (renewed, runner):
        for path in GATED:
            oidc.refused(path, oidc.cookie(token))
            # Even a proof made with the right key does not turn a machine token into a browser's
            # when it carries grant_id; and a runner token has no proof the server ever sent.
            oidc.refused(path, oidc.cookie(token, "f" * 64))
    token, proof, _ = _browser_cookie(oidc)
    for path in GATED:
        oidc.refused(path, oidc.cookie(token, proof), headers={"Authorization": f"Bearer {token}"})
        # An empty Authorization header still counts as one.
        oidc.refused(path, oidc.cookie(token, proof), headers={"Authorization": ""})
        oidc.refused(path, oidc.cookie(token, proof), site="same-site")
        oidc.refused(path, oidc.cookie(token, proof), site="cross-site")
    # A refresh-grant token is refused even when its proof is correctly derived.
    path = GATED[0]
    oidc.refused(path, oidc.cookie(renewed, oidc.proof_for(renewed)))


def test_a_proof_for_another_jwt_a_proof_under_the_wrong_key_and_the_plain_name_are_refused(
    oidc: Oidc,
) -> None:
    token, proof, _ = _browser_cookie(oidc)
    _, other_proof, _ = _browser_cookie(oidc, email=OTHER)
    wrong_key = browser_proof.derive_key(b"x" * 32)
    for path in GATED:
        assert oidc.passes(path, oidc.cookie(token, proof))
        oidc.refused(path, oidc.cookie(token, other_proof))
        oidc.refused(path, oidc.cookie(token, oidc.proof_for(token, wrong_key)))
        oidc.refused(path, oidc.cookie(token))
        if oidc.secure:
            plain = browser_proof.PLAIN_PROOF_NAME
            oidc.refused(path, oidc.cookie(token, proof, proof_name=plain))


def test_the_refusal_says_to_sign_in_again_in_a_plain_detail_string(oidc: Oidc) -> None:
    token, _, _ = _browser_cookie(oidc)
    response = oidc.refused(GATED[0], oidc.cookie(token))
    assert response.json() == {"detail": REFUSED}


def test_sign_out_expires_the_proof_cookie(oidc: Oidc) -> None:
    response = oidc.client.get("/auth/logout", follow_redirects=False)
    cookies = _set_cookies(response)
    assert oidc.session_name in cookies
    expired = cookies[oidc.proof_name]
    assert _attrs(expired)["max-age"] == "0"


def test_the_middleware_key_is_the_gates_key_in_oidc_mode(oidc: Oidc) -> None:
    middleware = [m for m in oidc.app.user_middleware if m.cls is browser_proof.BrowserProof]
    assert len(middleware) == 1
    live = browser_proof.BrowserProof(None, provider=auth.provider).settings()  # type: ignore[arg-type]
    assert live is not None
    assert live.key == browser_proof.derive_key(auth._cookie_secret(oidc.provider))
    assert (live.secure, live.accounts) == (oidc.secure, False)


def test_a_page_load_re_send_by_the_cookie_expiry_adds_no_proof(oidc: Oidc) -> None:
    _, proof, _ = _browser_cookie(oidc)

    async def page() -> HTMLResponse:
        return HTMLResponse("<html></html>", headers={"cache-control": "no-store"})

    app = FastAPI()
    app.add_api_route("/page", page, methods=["GET"])
    environ = {
        session_cookie.COOKIE_SECRET_ENV: oidc.secret.hex(),
        session_cookie.REDIRECT_URI_ENV: f"{oidc.origin}/auth/callback",
        browser_proof.OIDC_SECRET_ENV: oidc.secret.hex(),
    }
    assert session_cookie.install(app, environ, "oidc")
    assert browser_proof.install(app, environ, "oidc")
    fresh = mint_session_cookie(
        user_id=OWNER, cookie_secret=oidc.secret, ttl_hours=2, provider="oidc"
    )
    with TestClient(app, base_url=oidc.origin) as client:
        got = client.get(
            "/page", headers={"Accept": "text/html", "Cookie": oidc.cookie(fresh, proof)}
        )
    cookies = _set_cookies(got)
    assert oidc.session_name in cookies  # the expiry module re-sent the session cookie
    assert _value(cookies[oidc.session_name]) == fresh  # the same value, never a new one
    assert oidc.proof_name not in cookies


# ── accounts stack ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def accounts(
    ae_admins: set[str],
    ae_stores: dict[str, Any],
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Stack]:
    from omnigent.server.accounts_store import SqlAlchemyAccountStore
    from omnigent.server.app import create_app
    from omnigent.server.auth import create_auth_provider
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    secret_hex = secrets.token_hex(32)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / ".omnigent"))
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    monkeypatch.delenv("OMNIGENT_OIDC_ISSUER", raising=False)
    monkeypatch.setenv(browser_proof.ACCOUNTS_SECRET_ENV, secret_hex)
    monkeypatch.setenv(browser_proof.ACCOUNTS_BASE_URL_ENV, "http://testserver")
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD", ACCOUNTS_PASSWORD)
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-creds"))
    monkeypatch.setenv("OMNIGENT_ACCOUNTS_AUTO_OPEN", "0")
    provider = create_auth_provider()
    ae_admins.add("admin")
    app = create_app(
        **ae_stores,
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=provider,
        account_store=SqlAlchemyAccountStore(db_uri),
    )
    import os

    _install_proof(app, dict(os.environ), "accounts")
    auth.reset_provider_cache(provider)
    with TestClient(app, base_url="http://testserver") as client:
        yield Stack(
            client=client,
            provider=provider,
            app=app,
            secret=bytes.fromhex(secret_hex),
            secure=False,
            mode="accounts",
        )


def _login(stack: Stack, **extra: Any) -> httpx.Response:
    response = stack.client.post(
        "/auth/login",
        json={"username": "admin", "password": ACCOUNTS_PASSWORD, **extra},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    stack.client.cookies.clear()
    return response


def test_an_accounts_browser_login_sets_the_proof_and_the_gate_passes_it(accounts: Stack) -> None:
    response = _login(accounts)
    assert response.status_code == 200, response.text
    cookies = _set_cookies(response)
    token = _value(cookies[accounts.session_name])
    assert _value(cookies[accounts.proof_name]) == accounts.proof_for(token)
    assert (
        _attrs(cookies[accounts.proof_name])["max-age"]
        == _attrs(cookies[accounts.session_name])["max-age"]
    )
    for path in GATED:
        assert accounts.passes(path, accounts.cookie(token, accounts.proof_for(token)))
    middleware = [m for m in accounts.app.user_middleware if m.cls is browser_proof.BrowserProof]
    assert len(middleware) == 1
    live = browser_proof.BrowserProof(None, provider=auth.provider).settings()  # type: ignore[arg-type]
    assert live is not None
    assert live.key == browser_proof.derive_key(auth._cookie_secret(accounts.provider))
    assert live.accounts is True


def test_an_accounts_login_gets_a_proof_only_same_origin_and_without_a_refresh_token(
    accounts: Stack,
) -> None:
    body = {"username": "admin", "password": ACCOUNTS_PASSWORD}
    for headers in ({}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
        response = accounts.client.post("/auth/login", json=body, headers=headers)
        accounts.client.cookies.clear()
        assert response.status_code == 200
        assert accounts.session_name in _set_cookies(response)
        assert accounts.proof_name not in _set_cookies(response), headers
    refreshed = _login(accounts, issue_refresh=True)
    assert refreshed.status_code == 200
    assert "refresh_token" in refreshed.json()
    assert accounts.session_name in _set_cookies(refreshed)
    assert accounts.proof_name not in _set_cookies(refreshed)
    # The token the CLI keeps from that login is refused as a cookie.
    token = refreshed.json()["token"]
    for path in GATED:
        accounts.refused(path, accounts.cookie(token))


def test_the_bootstrap_token_and_a_runner_token_are_refused_in_accounts_mode(
    accounts: Stack, tmp_path: Path
) -> None:
    from omnigent import cli_auth
    from omnigent.server.accounts_bootstrap import _mint_loopback_cli_token

    login_token = _value(_set_cookies(_login(accounts))[accounts.session_name])
    generation = jwt.decode(login_token, accounts.secret, algorithms=["HS256"]).get(
        "account_generation"
    )
    base_url = "http://127.0.0.1:54321"
    assert _mint_loopback_cli_token(
        "admin",
        base_url=base_url,
        cookie_secret=accounts.secret,
        session_ttl_hours=8,
        account_generation=generation,
    )
    token = cli_auth.load_token(base_url)
    assert token
    assert (tmp_path / ".omnigent" / "auth_tokens.json").exists()  # HOME is the temp dir
    runner = accounts.provider.mint_runner_token("admin", 600)
    assert runner
    for value in (token, runner):
        for path in GATED:
            # A runner token may not even be accepted as a cookie (401); either way it is refused.
            accounts.refused(path, accounts.cookie(value), codes=(401, 403))
            other = accounts.cookie(value, accounts.proof_for("some-other-jwt"))
            accounts.refused(path, other, codes=(401, 403))
    assert accounts.gated(GATED[0], accounts.cookie(token)).status_code == 403


def test_the_magic_link_redeem_sets_no_proof(accounts: Stack) -> None:
    login = _login(accounts)
    token = _value(_set_cookies(login)[accounts.session_name])
    magic = accounts.client.post(
        "/auth/magic",
        headers={"Cookie": accounts.cookie(token, accounts.proof_for(token))},
    )
    assert magic.status_code == 200, magic.text
    redeem = httpx.URL(magic.json()["redeem_url"])
    fresh = accounts.client.get(f"{redeem.path}?{redeem.query.decode()}", follow_redirects=False)
    accounts.client.cookies.clear()
    assert fresh.status_code == 302
    cookies = _set_cookies(fresh)
    assert accounts.session_name in cookies
    assert accounts.proof_name not in cookies
    for path in GATED:
        accounts.refused(path, accounts.cookie(_value(cookies[accounts.session_name])))


def test_invite_and_reset_refuse_a_bearer_and_a_cookie_without_a_proof(accounts: Stack) -> None:
    token = _value(_set_cookies(_login(accounts))[accounts.session_name])
    proof = accounts.proof_for(token)
    routes = [
        ("/auth/invite", {"is_admin": False}),
        ("/auth/users/someone/reset", None),
    ]
    for path, body in routes:
        send: dict[str, Any] = {"json": body} if body is not None else {}
        same = {"Sec-Fetch-Site": "same-origin"}
        for headers in (
            {**same, "Authorization": f"Bearer {token}"},
            {**same, "Cookie": accounts.cookie(token, proof), "Authorization": ""},
            {**same, "Cookie": accounts.cookie(token)},
            {**same, "Cookie": accounts.cookie(token, accounts.proof_for("another-jwt"))},
            {"Cookie": accounts.cookie(token, proof)},
            {"Sec-Fetch-Site": "cross-site", "Cookie": accounts.cookie(token, proof)},
            {**same, "Cookie": accounts.cookie(token, proof), "Authorization": f"Bearer {token}"},
        ):
            response = accounts.client.post(path, headers=headers, **send)
            assert response.status_code == 403, (path, headers, response.text)
            assert response.json() == {"detail": REFUSED}
        accounts.client.cookies.clear()
        ok = accounts.client.post(
            path, headers={**same, "Cookie": accounts.cookie(token, proof)}, **send
        )
        assert ok.json() != {"detail": REFUSED}, (path, ok.text)


# ── the middleware alone, over a stub app ─────────────────────────────────────────────────────


def _stub(secure: bool = False, accounts: bool = True) -> tuple[TestClient, bytes]:
    secret = bytes(range(32))
    name = browser_proof.session_cookie_name(secure)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        body = json.dumps(
            {"token": "t.t.t", "refresh_token": "r"}
            if "refresh" in scope["path"]
            else {"token": "t"}
        )
        response: Response = JSONResponse(json.loads(body))
        if not scope["path"].endswith("/nothing"):
            response.set_cookie(
                name, "jwt.value.here", max_age=600, httponly=True, secure=secure, path="/"
            )
        await response(scope, receive, send)

    wrapped = browser_proof.BrowserProof(
        app, cookie_secret=secret, secure=secure, accounts=accounts
    )
    return TestClient(
        wrapped, base_url="https://testserver" if secure else "http://testserver"
    ), secret


@pytest.mark.parametrize(
    "path", ["/auth/login", "/auth/register", "/auth/setup", "/base/auth/login"]
)
def test_each_accounts_sign_in_path_gets_the_proof_only_same_origin(path: str) -> None:
    client, secret = _stub()
    same = client.post(path, headers={"Sec-Fetch-Site": "same-origin"})
    assert browser_proof.PLAIN_PROOF_NAME in _set_cookies(same)
    assert _value(_set_cookies(same)[browser_proof.PLAIN_PROOF_NAME]) == (
        browser_proof.expected_proof(browser_proof.derive_key(secret), "jwt.value.here")
    )
    assert browser_proof.PLAIN_PROOF_NAME not in _set_cookies(client.post(path))


def test_a_response_with_a_refresh_token_gets_no_proof_and_other_paths_get_none() -> None:
    client, _ = _stub()
    refresh = client.post("/auth/login/refresh", headers={"Sec-Fetch-Site": "same-origin"})
    assert refresh.status_code == 200
    assert browser_proof.PLAIN_SESSION_NAME in _set_cookies(refresh)
    assert browser_proof.PLAIN_PROOF_NAME not in _set_cookies(refresh)
    for path in ("/auth/cli-poll", "/auth/native-token", "/auth/magic/redeem", "/other"):
        response = client.post(path, headers={"Sec-Fetch-Site": "same-origin"})
        assert browser_proof.PLAIN_PROOF_NAME not in _set_cookies(response), path
    get = client.get("/auth/login", headers={"Sec-Fetch-Site": "same-origin"})
    assert browser_proof.PLAIN_PROOF_NAME not in _set_cookies(get)


def test_the_accounts_paths_are_inert_outside_accounts_mode() -> None:
    client, _ = _stub(accounts=False)
    response = client.post("/auth/login", headers={"Sec-Fetch-Site": "same-origin"})
    assert browser_proof.PLAIN_PROOF_NAME not in _set_cookies(response)


def test_the_callback_path_gets_the_proof_under_a_base_path_and_only_on_get() -> None:
    client, _ = _stub(secure=True, accounts=False)
    got = client.get("/omni/auth/callback")
    assert browser_proof.SECURE_PROOF_NAME in _set_cookies(got)
    assert "secure" in _attrs(_set_cookies(got)[browser_proof.SECURE_PROOF_NAME])
    assert browser_proof.SECURE_PROOF_NAME not in _set_cookies(client.post("/omni/auth/callback"))


# ── the two gated routes can only answer 403 or 422 to {} ─────────────────────────────────────


def test_an_empty_body_to_the_cards_and_git_routes_never_writes(oidc: Oidc) -> None:
    from ae_omni_policies import admin_cards

    token, proof, _ = _browser_cookie(oidc)
    before = admin_cards.store().snapshot()
    for path in GATED:
        for cookie in (oidc.cookie(token, proof), oidc.cookie(token)):
            assert oidc.gated(path, cookie).status_code in (403, 422), path
        # No cookie at all is not even a sign-in: 401, still no write.
        assert oidc.gated(path, None).status_code == 401
    assert admin_cards.store().snapshot() == before


def test_a_streamed_accounts_login_body_is_held_whole_and_decided_on_its_content() -> None:
    secret = bytes(range(32))
    name = browser_proof.PLAIN_SESSION_NAME
    parts = [b'{"token": "t.t.t", ', b'"refresh_token": "r"}']
    plain_parts = [b'{"token": "t.t.t", ', b'"user": "u"}']

    def streaming(chunks: list[bytes]) -> TestClient:
        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            cookie = f"{name}=jwt.value.here; Max-Age=600; Path=/; HttpOnly"
            headers = [(b"content-type", b"application/json"), (b"set-cookie", cookie.encode())]
            await send({"type": "http.response.start", "status": 200, "headers": headers})
            for i, chunk in enumerate(chunks):
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": i < len(chunks) - 1,
                    }
                )

        wrapped = browser_proof.BrowserProof(
            app, cookie_secret=secret, secure=False, accounts=True
        )
        return TestClient(wrapped, base_url="http://testserver")

    same = {"Sec-Fetch-Site": "same-origin"}
    with_refresh = streaming(parts).post("/auth/login", headers=same)
    assert with_refresh.content == b"".join(parts)
    assert browser_proof.PLAIN_PROOF_NAME not in _set_cookies(with_refresh)
    without = streaming(plain_parts).post("/auth/login", headers=same)
    assert without.content == b"".join(plain_parts)
    assert _value(_set_cookies(without)[browser_proof.PLAIN_PROOF_NAME]) == (
        browser_proof.expected_proof(browser_proof.derive_key(secret), "jwt.value.here")
    )


def test_the_middleware_follows_the_providers_secret_and_mode_not_the_environment(
    accounts: Stack,
) -> None:
    """The environment names another secret and says OIDC; the live provider is accounts."""
    other_secret = bytes(reversed(range(32)))
    environ = {browser_proof.OIDC_SECRET_ENV: other_secret.hex()}
    token = "jwt.value.here"

    async def login() -> JSONResponse:
        response = JSONResponse({"token": token})
        response.set_cookie(accounts.session_name, token, max_age=600, httponly=True, path="/")
        return response

    async def invite() -> JSONResponse:
        return JSONResponse({"ok": True})

    stub = FastAPI()
    stub.add_api_route("/auth/login", login, methods=["POST"])
    stub.add_api_route("/auth/invite", invite, methods=["POST"])
    assert browser_proof.install(stub, environ, "oidc")
    same = {"Sec-Fetch-Site": "same-origin"}
    with TestClient(stub, base_url="http://testserver") as client:
        signed = client.post("/auth/login", headers=same)
        proof = _value(_set_cookies(signed)[accounts.proof_name])
        # The proof is under the provider's key (the gate's), not the environment's.
        assert proof == accounts.proof_for(token)
        assert proof != browser_proof.expected_proof(browser_proof.derive_key(other_secret), token)
        # The accounts guard is on although the environment said OIDC.
        for headers in (
            {**same, "Authorization": "Bearer x"},
            {**same, "Authorization": "", "Cookie": accounts.cookie(token, proof)},
            {**same, "Cookie": accounts.cookie(token)},
        ):
            refused = client.post("/auth/invite", headers=headers)
            assert refused.status_code == 403
            assert refused.json() == {"detail": REFUSED}
        client.cookies.clear()
        ok = client.post("/auth/invite", headers={**same, "Cookie": accounts.cookie(token, proof)})
        assert ok.status_code == 200


@pytest.mark.parametrize("broken", ["raises", "no_secret"])
def test_unusable_provider_settings_refuse_invite_and_reset_and_issue_no_proof(
    broken: str,
) -> None:
    """Fail closed (fix check of PR 509): when the live provider raises or has no secret, invite
    and reset answer 403 in any mode, and a sign-in response carries no proof."""
    name = browser_proof.session_cookie_name(False)

    class NoSecret:
        session_cookie_name = name
        _source = "accounts"

    def provider() -> object:
        if broken == "raises":
            raise RuntimeError("misconfigured")
        return NoSecret()

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse({"ok": True})
        response.set_cookie(name, "jwt.value.here", max_age=600, httponly=True, path="/")
        await response(scope, receive, send)

    client = TestClient(browser_proof.BrowserProof(app, provider=provider))
    for path in ("/auth/invite", "/base/auth/invite", "/auth/users/u1/reset"):
        assert client.post(path, json={}).status_code == 403, path
    page = {"Sec-Fetch-Site": "same-origin"}
    for response in (
        client.post("/auth/login", json={}, headers=page),
        client.get("/auth/callback"),
    ):
        assert response.status_code == 200
        set_cookies = response.headers.get_list("set-cookie")
        assert any(c.startswith(f"{name}=") for c in set_cookies)
        assert not any("ae_browser_proof" in c for c in set_cookies)
