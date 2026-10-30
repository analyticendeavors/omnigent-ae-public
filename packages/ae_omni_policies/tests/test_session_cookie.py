"""The session cookie gets its expiry on page loads, so the iOS app keeps it (2026-10-20)."""

from __future__ import annotations

import time

import jwt
import pytest
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route
from starlette.testclient import TestClient

from ae_omni_policies import session_cookie
from ae_omni_policies.session_cookie import SessionCookieExpiry

SECRET = bytes(range(32))
# Real time: PyJWT rejects an ``iat`` in the future, as upstream's validator does.
NOW = int(time.time())
NAME = "__Host-ap_session"
PAGE = {"sec-fetch-dest": "document", "accept": "text/html"}


def token(exp: int = NOW + 3600, secret: bytes = SECRET, **extra: object) -> str:
    claims = {"sub": "owner@example.com", "iat": NOW, "exp": exp, **extra}
    return jwt.encode(claims, secret, algorithm="HS256")


def page(_request) -> Response:
    return HTMLResponse("<html></html>", headers={"Cache-Control": "no-cache"})


def logout(_request) -> Response:
    response = HTMLResponse("bye")
    response.delete_cookie(NAME, path="/")
    return response


def own_cookie(_request) -> Response:
    response = HTMLResponse("set")
    response.set_cookie(NAME, "fresh", max_age=60)
    return response


def missing(_request) -> Response:
    return HTMLResponse("nope", status_code=401)


def public_html(_request) -> Response:
    return HTMLResponse("cached", headers={"Cache-Control": "public, max-age=3600"})


def image(_request) -> Response:
    return Response(b"\x89PNG", media_type="image/png")


def client(*, secure: bool = True) -> TestClient:
    app = Starlette(
        routes=[
            Route("/", page),
            Route("/c/{sid}", page),
            Route("/auth/login", page),
            Route("/omnigent/auth/login", page),
            Route("/auth/logout", logout),
            Route("/own", own_cookie),
            Route("/v1/me", missing),
            Route("/static.html", public_html),
            Route("/icon.png", image),
        ]
    )
    app.add_middleware(
        SessionCookieExpiry, cookie_secret=SECRET, secure=secure, clock=lambda: float(NOW)
    )
    return TestClient(app, base_url="https://omnigent.test")


def set_cookies(response) -> list[str]:
    return response.headers.get_list("set-cookie")


def test_a_page_load_resends_the_cookie_with_what_is_left_of_its_expiry() -> None:
    value = token()
    response = client().get("/c/abc", headers={**PAGE, "cookie": f"{NAME}={value}"})
    assert set_cookies(response) == [
        f"{NAME}={value}; HttpOnly; Max-Age=3600; Path=/; SameSite=lax; Secure"
    ]


def test_accept_html_counts_when_the_browser_sends_no_fetch_metadata() -> None:
    response = client().get("/", headers={"accept": "text/html", "cookie": f"{NAME}={token()}"})
    assert len(set_cookies(response)) == 1


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/", {"sec-fetch-dest": "empty", "accept": "*/*"}),  # an API call from the page
        ("/auth/login", PAGE),  # auth routes are upstream's
        ("/omnigent/auth/login", PAGE),  # the same under a base path
        ("/auth/logout", PAGE),  # sign-out clears the cookie; never undo it
        ("/own", PAGE),  # the route sets the cookie itself
        ("/v1/me", PAGE),  # not a success
        ("/static.html", PAGE),  # publicly cacheable: a shared cache must never keep a token
        ("/icon.png", PAGE),  # a file opened in a tab, not a page
    ],
)
def test_requests_that_are_left_alone(path: str, headers: dict[str, str]) -> None:
    response = client().get(path, headers={**headers, "cookie": f"{NAME}={token()}"})
    assert not any(c.startswith(f"{NAME}=ey") for c in set_cookies(response))


@pytest.mark.parametrize(
    "value",
    [
        token(secret=b"x" * 32),  # signed with another key
        token(exp=NOW - 1),  # already expired
        token(scope="sessions:read"),  # a delegated or machine token
        token(grant_id="g1"),  # a device grant's token
        "not-a-jwt",
    ],
)
def test_a_cookie_that_is_not_a_live_browser_session_gets_nothing(value: str) -> None:
    response = client().get("/", headers={**PAGE, "cookie": f"{NAME}={value}"})
    assert set_cookies(response) == []


def test_http_servers_use_the_plain_name_without_secure() -> None:
    value = token()
    response = client(secure=False).get("/", headers={**PAGE, "cookie": f"ap_session={value}"})
    assert set_cookies(response) == [
        f"ap_session={value}; HttpOnly; Max-Age=3600; Path=/; SameSite=lax"
    ]


@pytest.mark.parametrize(
    ("environ", "secure"),
    [
        ({"OMNIGENT_OIDC_REDIRECT_URI": " https://omnigent.test/auth/callback"}, True),
        ({"OMNIGENT_DOMAIN": "omnigent.test"}, True),  # upstream derives https://<domain>
        ({"OMNIGENT_OIDC_REDIRECT_URI": "http://localhost:8000/auth/callback"}, False),
        ({}, False),
    ],
)
def test_the_cookie_name_follows_upstreams_secure_rule(
    environ: dict[str, str], secure: bool
) -> None:
    assert session_cookie.secure_cookies(environ) is secure


def test_install_needs_oidc_and_a_hex_cookie_secret() -> None:
    added: list[dict] = []

    class App:
        def add_middleware(self, cls, **kwargs) -> None:
            added.append({"cls": cls, **kwargs})

    env = {
        "OMNIGENT_OIDC_COOKIE_SECRET": SECRET.hex(),
        "OMNIGENT_OIDC_REDIRECT_URI": "https://omnigent.test/auth/callback",
    }
    assert not session_cookie.install(App(), env, auth_source="accounts")
    assert not session_cookie.install(App(), {}, auth_source="oidc")
    assert not session_cookie.install(
        App(), {"OMNIGENT_OIDC_COOKIE_SECRET": "zz"}, auth_source="oidc"
    )
    assert session_cookie.install(App(), env, auth_source="oidc")
    assert added == [{"cls": SessionCookieExpiry, "cookie_secret": SECRET, "secure": True}]
