"""The desktop app's own sign-in passes the browser-only gate (2026-11-09, PR 509 follow-up).

Built from upstream's real OIDC router (the IdP mocked as ``upstream/tests/server/
test_oidc_callback.py`` does), its real ``/oauth/token`` and ``/oauth/revoke`` routes and minters,
and a real ``DeviceGrantStore`` on the temp database. Every call that passes the gate carries the
body ``{}`` (422, never a write). The recorded-sign-in file is pointed at ``tmp_path``; nothing
here reaches ``/data`` or a live server.

https://github.com/analyticendeavors/omnigent-ae/pull/509
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.server.oidc import mint_session_cookie
from omnigent.server.routes.device_auth import LOGIN_GRANT_CLIENT_ID, mint_delegated_token
from starlette.requests import Request
from starlette.responses import JSONResponse
from test_browser_proof import GATED, OWNER, Oidc, _set_cookies

from ae_omni_policies import auth, browser_proof, desktop_signin

LOOPBACK = "http://127.0.0.1:53682/callback"
IOS = "ai.omnigent.ios:/oauth/callback"
VERIFIER = "v" * 43
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture(params=[True, False], ids=["https", "http"])
def desk(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    ae_admins: set[str],
    ae_stores: dict[str, Any],
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Oidc]:
    """The production-shaped OIDC stack with both AE middlewares, as the entrypoint adds them."""
    import dataclasses

    from omnigent.server.app import create_app
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.routes.auth import _AUTH_STATE_COOKIE_PLAIN, _AUTH_STATE_COOKIE_SECURE
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
    from tests.server.test_oidc_callback import _IdpKeys, _oidc_config

    monkeypatch.setenv(desktop_signin.STATE_FILE_ENV, str(tmp_path / "signins.json"))
    desktop_signin.reset_store()
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
    permission_store = SqlAlchemyPermissionStore(db_uri)
    app = create_app(**ae_stores, permission_store=permission_store, auth_provider=provider)
    environ = {
        browser_proof.OIDC_SECRET_ENV: secret.hex(),
        "OMNIGENT_OIDC_REDIRECT_URI": config.redirect_uri,
    }
    if desktop_signin.REAUTH_ENV in os.environ:
        environ[desktop_signin.REAUTH_ENV] = os.environ[desktop_signin.REAUTH_ENV]
    assert browser_proof.install(app, environ, "oidc")
    assert desktop_signin.install(app, environ, "oidc", permission_store=permission_store)
    auth.reset_provider_cache(provider)
    base = "https://testserver" if secure else "http://testserver"
    with TestClient(app, base_url=base) as client:
        stack = Oidc(
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
        stack.db_uri = db_uri  # type: ignore[attr-defined]
        yield stack


@pytest.fixture
def base_path_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """A configured base path; request it before ``desk`` so ``create_app`` reads it."""
    monkeypatch.setenv("OMNIGENT_WEB_BASE_PATH", "/proxy/ae")
    return "/proxy/ae"


@pytest.fixture
def reauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-authentication switch on; request it before ``desk``."""
    monkeypatch.setenv(desktop_signin.REAUTH_ENV, "1")


def _challenge(verifier: str = VERIFIER) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _native_code(desk: Oidc, redirect: str = LOOPBACK, email: str = OWNER) -> str:
    response = desk.sign_in(
        email=email,
        native={"redirect_uri": redirect, "state": "native-state", "code_challenge": _challenge()},
    )
    assert response.status_code == 302, response.text
    assert _set_cookies(response).get(desk.session_name) is None  # the callback sets no cookie
    return httpx.URL(response.headers["location"]).params["code"]


def _native_sign_in(
    desk: Oidc,
    redirect: str = LOOPBACK,
    email: str = OWNER,
    *,
    post_redirect: str | None = None,
    path: str = "/auth/native-token",
) -> httpx.Response:
    code = _native_code(desk, redirect, email)
    return desk.client.post(
        path,
        data={
            "code": code,
            "code_verifier": VERIFIER,
            "redirect_uri": post_redirect or redirect,
        },
    )


def _renew(desk: Oidc, refresh: str) -> str:
    response = desk.client.post(
        "/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]  # type: ignore[no-any-return]


def _entries() -> list[dict[str, Any]]:
    return desktop_signin.store().snapshot()


def _sign_in_ok(desk: Oidc) -> dict[str, Any]:
    response = _native_sign_in(desk)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["refresh_token"]
    return body


# ── the desktop sign-in passes ───────────────────────────────────────────────────────────────


def test_a_native_sign_in_passes_the_gate_without_a_proof_and_the_response_is_unchanged(
    desk: Oidc,
) -> None:
    body = _sign_in_ok(desk)
    assert set(body) == {"token", "user_id", "expires_in", "refresh_token"}
    assert len(_entries()) == 1
    assert _entries()[0]["g"] is not None  # its login grant was found and recorded
    for path in GATED:
        assert desk.passes(path, desk.cookie(body["token"]))  # no proof cookie at all
        # Same token, forged headers or a bearer: still refused by the rules that stay.
        desk.refused(path, desk.cookie(body["token"]), site="cross-site")
        desk.refused(
            path,
            desk.cookie(body["token"]),
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        desk.refused(path, desk.cookie(body["token"]), headers={"Authorization": ""})


def test_a_renewal_through_the_real_oauth_token_route_passes(desk: Oidc) -> None:
    body = _sign_in_ok(desk)
    renewed = _renew(desk, body["refresh_token"])
    claims = jwt.decode(renewed, desk.secret, algorithms=["HS256"])
    assert claims["grant_id"] and claims["act"]["client_id"] == LOGIN_GRANT_CLIENT_ID
    assert "scope" not in claims
    for path in GATED:
        assert desk.passes(path, desk.cookie(renewed))
    # And again: login grants do not rotate, so every renewal carries the same recorded grant.
    for path in GATED:
        assert desk.passes(path, desk.cookie(_renew(desk, body["refresh_token"])))


# ── everything else stays refused ────────────────────────────────────────────────────────────


def test_the_cli_ticket_token_and_its_refresh_grant_renewal_are_refused(desk: Oidc) -> None:
    ticket = desk.client.post("/auth/cli-login").json()["ticket"]
    desk.sign_in(ticket=ticket)
    poll = desk.client.get(f"/auth/cli-poll?ticket={ticket}")
    assert poll.status_code == 200
    cli_token, cli_refresh = poll.json()["token"], poll.json()["refresh_token"]
    renewed = _renew(desk, cli_refresh)
    assert jwt.decode(renewed, desk.secret, algorithms=["HS256"])["grant_id"]
    assert _entries() == []
    for token in (cli_token, renewed):
        for path in GATED:
            desk.refused(path, desk.cookie(token))
            desk.refused(path, desk.cookie(token, "f" * 64))
    # A desktop sign-in recorded afterwards does not rescue the CLI's tokens. Not in the same
    # second: two sessions for one user minted in one second are the same JWT (plan section 4,
    # residual; it takes two human sign-ins).
    time.sleep(1.1)
    _sign_in_ok(desk)
    for token in (cli_token, renewed):
        for path in GATED:
            desk.refused(path, desk.cookie(token))


def test_an_unrecorded_scoped_wrong_client_or_wrong_key_grant_token_is_refused(
    desk: Oidc,
) -> None:
    body = _sign_in_ok(desk)
    grant = jwt.decode(_renew(desk, body["refresh_token"]), desk.secret, algorithms=["HS256"])[
        "grant_id"
    ]

    def mint(secret: bytes, grant_id: str, client: str, scope: str | None) -> str:
        return mint_delegated_token(
            OWNER,
            secret,
            600,
            "oidc",
            grant_id=grant_id,
            client_id=client,
            jti="j",
            scope=scope,
        )

    cases = {
        "unrecorded grant": mint(desk.secret, "not-a-recorded-grant", LOGIN_GRANT_CLIENT_ID, None),
        "recorded grant, wrong client": mint(desk.secret, grant, "slack", None),
        "recorded grant, scope": mint(desk.secret, grant, LOGIN_GRANT_CLIENT_ID, "delegated"),
        "recorded grant, wrong key": mint(b"x" * 32, grant, LOGIN_GRANT_CLIENT_ID, None),
    }
    for name, token in cases.items():
        for path in GATED:
            # 403 from the gate, or 401 when upstream's own provider already refuses (scope).
            desk.refused(path, desk.cookie(token), codes=(401, 403))
    # The same recorded grant with the login client and no scope is what a renewal is: passes.
    good = mint(desk.secret, grant, LOGIN_GRANT_CLIENT_ID, None)
    assert desk.passes(GATED[0], desk.cookie(good))
    # A grant recorded under another key (another secret) is not this server's record.
    other = desktop_signin.keyed(desktop_signin.derive_key(b"y" * 32), grant)
    assert other not in {e["g"] for e in _entries()}


def test_the_ios_private_use_redirect_is_not_recorded(desk: Oidc) -> None:
    response = _native_sign_in(desk, IOS)
    assert response.status_code == 200, response.text
    assert _entries() == []
    for path in GATED:
        desk.refused(path, desk.cookie(response.json()["token"]))
        renewed = _renew(desk, response.json()["refresh_token"])
        desk.refused(path, desk.cookie(renewed))


def test_a_revoked_grants_token_is_refused_through_the_apps_own_check(
    desk: Oidc, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def app_check(request: Any, user: str) -> bool:
        # The app's own provider, which create_app wired to the grant store's revocation list.
        return desk.provider.get_user_id(request) == user

    monkeypatch.setattr(auth, "session_check", app_check)
    body = _sign_in_ok(desk)
    renewed = _renew(desk, body["refresh_token"])
    for path in GATED:
        assert desk.passes(path, desk.cookie(renewed))
    revoked = desk.client.post("/oauth/revoke", data={"refresh_token": body["refresh_token"]})
    assert revoked.status_code == 200, revoked.text
    for path in GATED:
        response = desk.refused(path, desk.cookie(renewed), codes=(401, 403))
        assert response.status_code == 401


def test_two_redirect_uri_fields_are_not_recorded(desk: Oidc) -> None:
    code = _native_code(desk)
    form = f"code={code}&code_verifier={VERIFIER}&redirect_uri={LOOPBACK}&redirect_uri={LOOPBACK}"
    response = desk.client.post("/auth/native-token", content=form, headers=FORM)
    assert response.status_code == 200, response.text
    assert _entries() == []
    desk.refused(GATED[0], desk.cookie(response.json()["token"]))


def test_a_non_urlencoded_or_oversized_or_failed_exchange_is_not_recorded(desk: Oidc) -> None:
    code = _native_code(desk)
    big = f"code={code}&code_verifier={VERIFIER}&redirect_uri={LOOPBACK}&pad={'x' * 17000}"
    response = desk.client.post("/auth/native-token", content=big, headers=FORM)
    assert response.status_code == 200, response.text  # upstream is fine with the padding
    assert _entries() == []
    code = _native_code(desk)
    bad = desk.client.post(
        "/auth/native-token",
        data={"code": code, "code_verifier": "w" * 43, "redirect_uri": LOOPBACK},
    )
    assert bad.status_code == 400
    assert _entries() == []
    multipart = desk.client.post(
        "/auth/native-token",
        files={"code": (None, _native_code(desk))},
        data={"code_verifier": VERIFIER, "redirect_uri": LOOPBACK},
    )
    assert multipart.status_code == 200, multipart.text
    assert _entries() == []


def test_only_the_real_route_records_not_a_path_that_ends_the_same_way(desk: Oidc) -> None:
    token = mint_session_cookie(OWNER, desk.secret, 8, "oidc")
    app = FastAPI()

    @app.post("/other/auth/native-token")
    async def fake() -> JSONResponse:
        return JSONResponse({"token": token, "user_id": OWNER})

    app.add_middleware(
        desktop_signin.DesktopSignIn,
        secret=lambda: desk.secret,
        grants=lambda: DeviceGrantStore(desk.db_uri),  # type: ignore[attr-defined]
    )
    form = f"code=c&code_verifier={VERIFIER}&redirect_uri={LOOPBACK}"
    with TestClient(app) as client:
        got = client.post("/other/auth/native-token", content=form, headers=FORM)
    assert got.status_code == 200
    assert _entries() == []
    desk.refused(GATED[0], desk.cookie(token))


# ── the store ────────────────────────────────────────────────────────────────────────────────


def test_the_store_survives_a_restart_and_follows_a_change_from_another_process(
    desk: Oidc, tmp_path: Path
) -> None:
    body = _sign_in_ok(desk)
    path = tmp_path / "signins.json"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    reborn = desktop_signin.SignInStore(path)
    assert reborn.snapshot() == _entries()
    desktop_signin.reset_store(reborn)  # a restarted server reads the same file
    assert desk.passes(GATED[0], desk.cookie(body["token"]))
    # Another writer replaces the file: this process notices through the mtime and reloads.
    other = desktop_signin.SignInStore(path)
    now = int(time.time())
    assert other.record("t" * 64, "g" * 64, "gid", "u" * 64, now + 100, now)
    assert any(e["t"] == "t" * 64 for e in reborn.snapshot())
    path.write_text("{ not json", encoding="utf-8")
    os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert reborn.snapshot() == []  # corrupt reads as empty
    for p in GATED:
        desk.refused(p, desk.cookie(body["token"]))
    path.unlink()
    assert reborn.snapshot() == []  # a missing file reads as empty


def test_a_failed_write_leaves_the_response_alone_and_the_desktop_refused(
    desk: Oidc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(desktop_signin.tempfile, "mkstemp", broken)
    body = _sign_in_ok(desk)  # 200, same body
    assert _entries() == []
    assert not (tmp_path / "signins.json").exists()
    for path in GATED:
        desk.refused(path, desk.cookie(body["token"]))


def test_the_file_holds_no_token_no_grant_id_and_no_user(desk: Oidc, tmp_path: Path) -> None:
    body = _sign_in_ok(desk)
    renewed = _renew(desk, body["refresh_token"])
    grant = jwt.decode(renewed, desk.secret, algorithms=["HS256"])["grant_id"]
    text = (tmp_path / "signins.json").read_text(encoding="utf-8")
    data = json.loads(text)
    assert set(data["entries"][0]) == {"t", "g", "i", "u", "exp", "c"}
    for secret_value in (body["token"], body["refresh_token"], renewed, OWNER, "example"):
        assert secret_value not in text
    assert data["entries"][0]["i"] == grant  # the grant id: an identifier, no authority alone
    key = desktop_signin.derive_key(desk.secret)
    assert data["entries"][0]["t"] == desktop_signin.keyed(key, body["token"])
    assert data["entries"][0]["g"] == desktop_signin.keyed(key, grant)
    assert data["entries"][0]["u"] == desktop_signin.keyed(key, OWNER)


def test_the_record_lands_before_the_response_body_is_sent(
    desk: Oidc, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real = desktop_signin.SignInStore.record

    def spy(self: Any, *args: Any, **kwargs: Any) -> bool:
        events.append("record")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(desktop_signin.SignInStore, "record", spy)
    app = desk.app

    async def watching(scope: Any, receive: Any, send: Any) -> None:
        async def spy_send(message: Any) -> None:
            if message["type"] == "http.response.start" and scope["path"].endswith("native-token"):
                events.append("start")
            if message["type"] == "http.response.body" and scope["path"].endswith("native-token"):
                events.append("body")
            await send(message)

        await app(scope, receive, spy_send)

    code = _native_code(desk)
    with TestClient(watching, base_url=desk.origin) as client:
        got = client.post(
            "/auth/native-token",
            data={"code": code, "code_verifier": VERIFIER, "redirect_uri": LOOPBACK},
        )
    assert got.status_code == 200
    assert events[:3] == ["record", "start", "body"], events


def test_pruning_drops_only_entries_past_both_dates_and_the_cap_is_per_user(
    tmp_path: Path,
) -> None:
    lifetime = desktop_signin.grant_lifetime_s()
    now = 10_000_000
    store = desktop_signin.SignInStore(tmp_path / "s.json")
    # Past both dates: dropped at the next write. Token past exp but grant in date: kept.
    assert store.record("old", "g-old", "i-old", "u1", now - lifetime - 2, now - lifetime - 1)
    assert store.record("grant-live", "g-live", "i-live", "u1", now - 5, now - 10)
    assert store.record("token-live", "g-tl", "i-tl", "u1", now + 50, now - lifetime - 1)
    assert store.record("fresh", "g-fresh", "i-fresh", "u9", now + 99, now)
    kept = {e["t"] for e in store.snapshot()}
    assert "old" not in kept
    assert {"grant-live", "token-live", "fresh"} <= kept
    assert store.grant_id_for_token("token-live", now) == "i-tl"
    assert store.grant_id_for_token("grant-live", now) is None  # token expired, grant in date
    assert store.grant_id_for_grant("g-live", now) == "i-live"
    assert store.grant_id_for_grant("g-old", now) is None
    # The cap: the ninth entry of a user evicts that user's own oldest, never another user's.
    other = desktop_signin.SignInStore(tmp_path / "cap.json")
    assert other.record("keep", "gk", "ik", "u-other", now + 500, now)
    for i in range(desktop_signin.MAX_ENTRIES_PER_USER + 3):
        assert other.record(f"t{i}", f"g{i}", f"i{i}", "u-me", now + 500, now + i + 1)
    rows = other.snapshot()
    mine = [e["t"] for e in rows if e["u"] == "u-me"]
    assert len(mine) == desktop_signin.MAX_ENTRIES_PER_USER
    assert "t0" not in mine and "t10" in mine
    assert any(e["t"] == "keep" for e in rows)


def test_an_entry_with_no_grant_is_pruned_on_the_tokens_expiry_alone_and_never_vouches(
    tmp_path: Path,
) -> None:
    now = 10_000_000
    path = tmp_path / "legacy.json"
    rows = [
        {"t": "gone", "g": None, "i": None, "u": "u", "exp": now - 1, "c": now - 5},
        {"t": "open", "g": None, "i": None, "u": "u", "exp": now + 50, "c": now - 5},
    ]
    path.write_text(json.dumps({"version": 1, "entries": rows}), encoding="utf-8")
    store = desktop_signin.SignInStore(path)
    assert store.grant_id_for_token("open", now) is None  # no grant: nothing to be live
    assert store.record("new", "gn", "in", "u", now + 99, now)
    assert {e["t"] for e in store.snapshot()} == {"open", "new"}


def test_without_a_data_folder_the_store_is_memory_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(desktop_signin.STATE_FILE_ENV)
    monkeypatch.setattr(desktop_signin, "DATA_DIR", Path("/nonexistent-ae-data"))
    desktop_signin.reset_store()
    assert desktop_signin._default_path() is None
    memory = desktop_signin.store()
    now = int(time.time())
    assert memory.record("a", "ga", "ia", "u", now + 60, now)
    assert memory.grant_id_for_token("a", now) == "ia"


# ── the re-authentication switch ─────────────────────────────────────────────────────────────


def _echo_app(**kwargs: Any) -> TestClient:
    app = FastAPI()

    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"query": request.url.query})

    for route in ("/auth/login", "/b/auth/login", "/other/auth/login", "/other/page"):
        app.add_api_route(route, echo, methods=["GET"])

    app.add_middleware(desktop_signin.DesktopSignIn, secret=lambda: b"s" * 32, **kwargs)
    return TestClient(app)


def test_the_reauth_switch_adds_reauth_only_to_the_exact_native_login_and_only_when_on() -> None:
    native = "native_redirect_uri=http%3A%2F%2F127.0.0.1%3A5%2Fcb&native_state=s&code_challenge=c"
    ios = "native_redirect_uri=ai.omnigent.ios%3A%2Foauth%2Fcallback&native_state=s"
    with _echo_app(reauth=True, base_path="/b") as client:
        for path in ("/auth/login", "/b/auth/login"):
            got = client.get(f"{path}?{native}").json()["query"]
            assert "reauth=1" in got and "native_redirect_uri=" in got
        forced = client.get(f"/auth/login?{native}&reauth=0").json()["query"]
        assert forced.count("reauth=") == 1 and "reauth=1" in forced
        assert "reauth" not in client.get("/auth/login?return_to=%2F").json()["query"]
        assert "reauth" not in client.get(f"/other/page?{native}").json()["query"]
        # A suffix route and an iOS private-use redirect are untouched.
        assert "reauth" not in client.get(f"/other/auth/login?{native}").json()["query"]
        assert "reauth" not in client.get(f"/auth/login?{ios}").json()["query"]
        two = f"{native}&native_redirect_uri=http%3A%2F%2F127.0.0.1%3A6%2Fcb"
        assert "reauth" not in client.get(f"/auth/login?{two}").json()["query"]
    with _echo_app() as client:  # off by default
        assert "reauth" not in client.get(f"/auth/login?{native}").json()["query"]


def test_install_reads_the_switch_from_the_environment_and_only_runs_for_oidc(
    db_uri: str,
) -> None:
    secret = {desktop_signin.OIDC_SECRET_ENV: (b"s" * 32).hex()}
    perm = SimpleNamespace(storage_location=db_uri)
    for env, expected in ((secret, False), ({**secret, desktop_signin.REAUTH_ENV: "1"}, True)):
        app = FastAPI()
        assert desktop_signin.install(app, env, "oidc", permission_store=perm)
        mw = [m for m in app.user_middleware if m.cls is desktop_signin.DesktopSignIn]
        assert len(mw) == 1 and mw[0].kwargs["reauth"] is expected
    for source in ("accounts", "header", None):
        assert not desktop_signin.install(
            FastAPI(), secret, source or "header", permission_store=perm
        )
    assert not desktop_signin.install(FastAPI(), {}, "oidc", permission_store=perm)
    bad = {desktop_signin.OIDC_SECRET_ENV: "zz"}
    assert not desktop_signin.install(FastAPI(), bad, "oidc", permission_store=perm)


def test_install_refuses_when_no_grant_store_is_reachable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = {desktop_signin.OIDC_SECRET_ENV: (b"s" * 32).hex()}
    for perm in (None, SimpleNamespace(), SimpleNamespace(storage_location="")):
        app = FastAPI()
        assert desktop_signin.install(app, secret, "oidc", permission_store=perm) is False
        assert not [m for m in app.user_middleware if m.cls is desktop_signin.DesktopSignIn]
    assert "no grant store is reachable" in caplog.text


# ── recording needs a login grant of the same user ───────────────────────────────────────────


class _RaisingGrants:
    def get_by_refresh_hash(self, _hash: str) -> Any:
        raise RuntimeError("database down")


def _record_case(
    db_uri: str,
    tmp_path: Path,
    *,
    refresh: str | None,
    grants: Any,
    user: str = OWNER,
) -> tuple[bool, desktop_signin.SignInStore]:
    secret = bytes(range(32))
    token = mint_session_cookie(user, secret, 8, "oidc")
    body: dict[str, Any] = {"token": token, "user_id": user}
    if refresh is not None:
        body["refresh_token"] = refresh
    sign_ins = desktop_signin.SignInStore(tmp_path / "unit.json")
    recorded = desktop_signin.record_sign_in(
        secret,
        json.dumps(body).encode(),
        (lambda: grants) if grants is not None else None,
        sign_ins,
    )
    return recorded, sign_ins


def test_a_sign_in_is_recorded_only_with_a_refresh_token_for_a_login_grant_of_the_same_user(
    db_uri: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from omnigent.server.device_grant_store import hash_secret
    from omnigent.server.routes.device_auth import issue_login_grant

    secret = bytes(range(32))
    store = DeviceGrantStore(db_uri)
    mine = issue_login_grant(store, user_id=OWNER, cookie_secret=secret)
    theirs = issue_login_grant(store, user_id="other@example.com", cookie_secret=secret)
    store.create_redeemed_grant(
        "third-party-grant",
        user_id=OWNER,
        client_id="slack",
        refresh_token_hash=hash_secret("slack-refresh", secret),
        created_at=int(time.time()),
    )
    cases = {
        "the response has no refresh token": {"refresh": None, "grants": store},
        "no grant store": {"refresh": mine, "grants": None},
        "the grant lookup failed": {"refresh": mine, "grants": _RaisingGrants()},
        "the refresh token maps to no grant": {"refresh": "unknown-refresh", "grants": store},
        "the grant belongs to another user": {"refresh": theirs, "grants": store},
        "the grant is not a login grant": {"refresh": "slack-refresh", "grants": store},
    }
    for message, kwargs in cases.items():
        caplog.clear()
        recorded, sign_ins = _record_case(db_uri, tmp_path, **kwargs)  # type: ignore[arg-type]
        assert recorded is False, message
        assert sign_ins.snapshot() == [], message
        assert message in caplog.text, message
        assert mine not in caplog.text and "slack-refresh" not in caplog.text
    recorded, sign_ins = _record_case(db_uri, tmp_path, refresh=mine, grants=store)
    assert recorded is True
    assert sign_ins.snapshot()[0]["i"]


def test_a_response_without_a_refresh_token_does_not_pass_the_gate(
    desk: Oidc, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ae_omni_policies.docker_entrypoint  # noqa: F401  (the module the install line lives in)

    real = desktop_signin.record_sign_in

    def strip_refresh(secret: Any, body: bytes, grants: Any, *a: Any, **k: Any) -> bool:
        data = json.loads(body)
        data.pop("refresh_token", None)
        return real(secret, json.dumps(data).encode(), grants, *a, **k)

    monkeypatch.setattr(desktop_signin, "record_sign_in", strip_refresh)
    body = _sign_in_ok(desk)
    assert _entries() == []
    for path in GATED:
        desk.refused(path, desk.cookie(body["token"]))


# ── base path ────────────────────────────────────────────────────────────────────────────────


def test_a_configured_base_path_still_records_through_the_exact_mounted_path(
    base_path_env: str, desk: Oidc
) -> None:
    assert desk.app.state.base_path == base_path_env
    middleware = [m for m in desk.app.user_middleware if m.cls is desktop_signin.DesktopSignIn]
    assert middleware[0].kwargs["base_path"] == base_path_env
    # The proxy forwards the prefix: upstream's BasePathMiddleware copies the scope, so the
    # endpoint never reaches ours, and the exact path base + /auth/native-token decides.
    response = _native_sign_in(desk, path=f"{base_path_env}/auth/native-token")
    assert response.status_code == 200, response.text
    assert len(_entries()) == 1
    for path in GATED:
        assert desk.passes(path, desk.cookie(response.json()["token"]))
    # A stripping proxy: no prefix, the endpoint resolves normally.
    time.sleep(1.1)
    second = _native_sign_in(desk)
    assert second.status_code == 200, second.text
    assert len(_entries()) == 2


def test_a_suffix_path_is_not_recorded_even_with_a_base_path_and_the_miss_is_logged_once(
    base_path_env: str, desk: Oidc, caplog: pytest.LogCaptureFixture
) -> None:
    token = mint_session_cookie(OWNER, desk.secret, 8, "oidc")
    app = FastAPI()

    @app.post("/elsewhere/auth/native-token")
    async def fake() -> JSONResponse:
        return JSONResponse({"token": token, "user_id": OWNER, "refresh_token": "r"})

    app.add_middleware(
        desktop_signin.DesktopSignIn,
        secret=lambda: desk.secret,
        grants=lambda: DeviceGrantStore(desk.db_uri),  # type: ignore[attr-defined]
        base_path=base_path_env,
    )
    form = f"code=c&code_verifier={VERIFIER}&redirect_uri={LOOPBACK}"
    desktop_signin._unrecorded_logged = False
    with TestClient(app) as client:
        for _ in range(3):
            assert (
                client.post("/elsewhere/auth/native-token", content=form, headers=FORM).status_code
                == 200
            )
    assert _entries() == []
    assert caplog.text.count("was not recorded") == 1


# ── revocation reaches the first token at once ───────────────────────────────────────────────


def test_revoking_the_grant_refuses_the_first_native_token_and_every_renewal_at_once(
    desk: Oidc,
) -> None:
    # conftest's session_check always answers yes (it stands for the 30 s credential cache), so
    # only the gate's own grant check can refuse here.
    body = _sign_in_ok(desk)
    renewed = _renew(desk, body["refresh_token"])
    for path in GATED:
        assert desk.passes(path, desk.cookie(body["token"]))
        assert desk.passes(path, desk.cookie(renewed))
    revoked = desk.client.post("/oauth/revoke", data={"refresh_token": body["refresh_token"]})
    assert revoked.status_code == 200, revoked.text
    for path in GATED:
        desk.refused(path, desk.cookie(body["token"]))  # the gate's own grant check, no cache
        desk.refused(path, desk.cookie(renewed), codes=(401, 403))


def test_a_vanished_or_aged_grant_stops_vouching(
    desk: Oidc, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _sign_in_ok(desk)
    assert desk.passes(GATED[0], desk.cookie(body["token"]))
    monkeypatch.setattr(desktop_signin, "_lifetime", 1)  # the grant is now older than its life
    time.sleep(1.1)
    desk.refused(GATED[0], desk.cookie(body["token"]))


# ── small store fixes ────────────────────────────────────────────────────────────────────────


def test_a_stat_error_after_a_successful_replace_still_counts_as_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "s.json"
    store = desktop_signin.SignInStore(path)
    real_replace, real_stat = os.replace, Path.stat
    state = {"replaced": False}

    def replace(src: Any, dst: Any) -> None:
        real_replace(src, dst)
        state["replaced"] = True

    def stat(self: Path, *a: Any, **k: Any) -> Any:
        if state["replaced"] and self == path:
            state["replaced"] = False
            raise OSError("vanished")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(desktop_signin.os, "replace", replace)
    monkeypatch.setattr(Path, "stat", stat)
    now = int(time.time())
    assert store.record("t", "g", "i", "u", now + 60, now) is True
    monkeypatch.undo()
    assert [e["t"] for e in store.snapshot()] == ["t"]  # reloaded from the written file


def test_the_grant_lifetime_is_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_GRANT_MAX_LIFETIME_DAYS", "1")
    desktop_signin.reset_store()
    assert desktop_signin.grant_lifetime_s() == 86400
    monkeypatch.setenv("OMNIGENT_GRANT_MAX_LIFETIME_DAYS", "2")
    assert desktop_signin.grant_lifetime_s() == 86400
    desktop_signin.reset_store()
    assert desktop_signin.grant_lifetime_s() == 172800


def test_the_held_start_goes_out_before_any_other_non_body_message(
    tmp_path: Path,
) -> None:
    def native_token() -> None: ...

    native_token.__module__ = "omnigent.server.routes.auth"
    form = f"code=c&redirect_uri={LOOPBACK}".encode()
    sent: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        scope["endpoint"] = native_token
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.debug", "info": {}})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})

    async def out(message: Any) -> None:
        sent.append(message["type"])

    async def receive() -> Any:
        return {"type": "http.request", "body": form, "more_body": False}

    app = desktop_signin.DesktopSignIn(inner, secret=lambda: b"s" * 32, grants=lambda: None)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/auth/native-token",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "query_string": b"",
    }
    import asyncio

    asyncio.run(app(scope, receive, out))
    assert sent == ["http.response.start", "http.response.debug", "http.response.body"]


# ── re-authentication on the real stack ──────────────────────────────────────────────────────


def _login_location(desk: Oidc, redirect: str, path: str = "/auth/login") -> httpx.QueryParams:
    response = desk.client.get(
        path,
        params={
            "native_redirect_uri": redirect,
            "native_state": "native-state",
            "code_challenge": _challenge(),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    desk.client.cookies.clear()
    assert response.status_code == 302, response.text
    return httpx.URL(response.headers["location"]).params


def test_the_reauth_switch_on_the_real_stack_forces_a_fresh_login_for_a_loopback_redirect_only(
    reauth_env: None, desk: Oidc
) -> None:
    params = _login_location(desk, LOOPBACK)
    assert params["prompt"] == "login" and params["max_age"] == "0"
    ios = _login_location(desk, IOS)
    assert "prompt" not in ios and "max_age" not in ios
    # An ordinary browser sign-in is untouched too.
    plain = desk.client.get("/auth/login", follow_redirects=False)
    assert "prompt" not in httpx.URL(plain.headers["location"]).params


def test_the_reauth_switch_is_off_by_default_on_the_real_stack(desk: Oidc) -> None:
    assert "prompt" not in _login_location(desk, LOOPBACK)
