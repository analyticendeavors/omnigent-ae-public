"""The Git card's company tokens and personal lists: routes, isolation, Needs you (2026-11-08).

GitHub is a ``httpx.MockTransport`` (``git_tokens.TRANSPORT`` and ``git_center.TRANSPORT`` carry
it); every state file is memory only. Names are made up (octo-user, example-org,
owner@example.com).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from starlette.requests import Request

from ae_omni_policies import (
    admin_cards,
    attention,
    auth,
    card_secrets,
    git_center,
    git_personal,
    git_tokens,
)
from ae_omni_policies import router as router_module

OWNER = {"X-Forwarded-Email": "owner@example.com", "Sec-Fetch-Site": "same-origin"}
SECOND = {"X-Forwarded-Email": "second@example.com", "Sec-Fetch-Site": "same-origin"}
MEMBER = {"X-Forwarded-Email": "member@example.com", "Sec-Fetch-Site": "same-origin"}
MARKER = "ZzSecretMarker9"
TOKEN_A = "github_pat_AAAA" + "A1b2C3d4E5F6g7H8i9J0" * 3
TOKEN_B = "github_pat_BBBB" + "B1b2C3d4E5F6g7H8i9J0" * 3
COMPANY = "github_pat_COMPANY" + "C1b2C3d4E5F6g7H8i9J0" * 3
BAD = "github_pat_" + MARKER + "!" * 40
NEW_POSTS = {
    "/git/mine/check",
    "/git/mine/save",
    "/git/mine/repos",
    "/git/mine/clear",
    "/git/users/clear",
    "/git/company/token",
    "/git/company/clear",
}


def repo_data(prs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "defaultBranchRef": {"name": "main", "target": {"oid": "abc123"}},
        "pushedAt": "2026-11-07T10:00:00Z",
        "isArchived": False,
        "pullRequests": {"nodes": prs or []},
        "refs": {"nodes": []},
        "vulnerabilityAlerts": {"nodes": []},
    }


def ready_pr(repo: str, number: int = 7) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Bump x",
        "url": f"https://github.com/{repo}/pull/{number}",
        "isDraft": False,
        "mergeStateStatus": "CLEAN",
        "author": {"login": "octo-user"},
        "updatedAt": "2026-11-07T09:00:00Z",
        "headRefName": "topic",
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
    }


class GitHub:
    """A tiny GitHub: who a token is, what it can list, which repos it can read."""

    def __init__(self) -> None:
        self.accounts: dict[str, tuple[str, str]] = {}
        self.visible: dict[str, list[str]] = {}
        self.readable: dict[str, set[str] | None] = {}
        self.prs: dict[str, list[dict[str, Any]]] = {}
        self.status = 200
        self.seen: list[tuple[str, str, str]] = []
        self.reads: list[tuple[str, str]] = []

    def add(self, token: str, login: str, visible: list[str], reads: set[str] | None) -> None:
        self.accounts[token] = (login, "2026-12-01 00:00:00 UTC")
        self.visible[token] = visible
        self.readable[token] = reads

    def __call__(self, request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        path = request.url.path
        self.seen.append((request.method, request.url.host, path))
        if path == "/user":
            if self.status != 200:
                return httpx.Response(self.status)
            if bearer not in self.accounts:
                return httpx.Response(401, json={"message": "Bad credentials"})
            login, expiry = self.accounts[bearer]
            return httpx.Response(
                200,
                json={"login": login},
                headers={"github-authentication-token-expiration": expiry},
            )
        if path == "/user/repos":
            names = self.visible.get(bearer, [])
            rows = [{"full_name": n, "private": True} for n in names]
            return httpx.Response(200, json=rows if request.url.params["page"] == "1" else [])
        if path == "/graphql":
            variables = json.loads(request.content)["variables"]
            repo = f"{variables['owner']}/{variables['name']}"
            self.reads.append((repo, bearer))
            allowed = self.readable.get(bearer, set())
            if self.status != 200:
                return httpx.Response(self.status)
            if allowed is not None and repo.lower() not in {a.lower() for a in allowed}:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200, json={"data": {"repository": repo_data(self.prs.get(repo))}}
            )
        return httpx.Response(404)


@pytest.fixture()
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    value = Fernet.generate_key().decode()
    monkeypatch.setenv(card_secrets.KEY_ENV, value)
    return value


@pytest.fixture()
def gh(monkeypatch: pytest.MonkeyPatch) -> GitHub:
    fake = GitHub()
    monkeypatch.setattr(git_tokens, "TRANSPORT", httpx.MockTransport(fake))
    monkeypatch.setattr(git_center, "TRANSPORT", httpx.MockTransport(fake))
    # Roster pruning asks the server's own member list; these tests have none.
    monkeypatch.setattr(git_personal, "_roster_asked", 1e12)
    return fake


def save_account(login: str, token: str, account: str, repos: list[str]) -> None:
    """Put a connected account straight into the stores (no route)."""
    card_secrets.put(
        card_secrets.user_id(login, account),
        token,
        saved_by=login,
        github_login=account,
        expires_at=None,
    )
    store = git_personal.store()
    revision, held = store.get(login)
    store.put(
        login,
        [*held, *({"repo": r, "account": account} for r in repos)],
        revision,
    )


def post(client: httpx.AsyncClient, path: str, body: dict[str, Any], headers: Any = OWNER) -> Any:
    return client.post(f"/v1/ae{path}", json=body, headers=headers)


# ── the write gate ────────────────────────────────────────────────────────────────────────────


def test_every_new_post_has_the_browser_gate_and_the_json_and_origin_checks() -> None:
    from omnigent.server.routes._content_type import require_json_content_type
    from omnigent.server.routes._origin import require_trusted_origin

    found = set()
    for route in router_module.router.routes:
        path = getattr(route, "path", "")
        sub = path.removeprefix("/v1/ae") if path.startswith("/v1/ae") else path
        if sub in NEW_POSTS and "POST" in getattr(route, "methods", ()):
            calls = {dep.call for dep in route.dependant.dependencies}  # type: ignore[attr-defined]
            assert {
                auth.require_browser_caller,
                require_json_content_type,
                require_trusted_origin,
            } <= calls, path
            found.add(sub)
    assert found == NEW_POSTS


async def test_a_real_oidc_cookie_passes_the_gate_and_a_bearer_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate the new POSTs depend on, built from upstream's own OIDC objects."""
    import dataclasses

    from fastapi import HTTPException
    from omnigent.server.auth import UnifiedAuthProvider
    from omnigent.server.oidc import OIDCConfig, mint_session_cookie

    secret = bytes(range(32))
    values: dict[str, Any] = {
        "issuer": "https://idp.example.com",
        "client_id": "client",
        "client_secret": "",
        "redirect_uri": "https://omni.example.com/auth/callback",
        "cookie_secret": secret,
        "scopes": "openid email",
        "session_ttl_hours": 12,
        "provider_type": "oidc",
        "authorization_endpoint": "https://idp.example.com/authorize",
        "token_endpoint": "https://idp.example.com/token",
    }
    for field in dataclasses.fields(OIDCConfig):
        if field.name in values or field.default is not dataclasses.MISSING:
            continue
        if field.default_factory is dataclasses.MISSING:
            values[field.name] = False if field.type in (bool, "bool") else None
    prov = UnifiedAuthProvider("oidc", oidc_config=OIDCConfig(**values), local_single_user=False)

    async def who(_request: Request) -> str:
        return "owner@example.com"

    monkeypatch.setattr(auth, "require_ae_user", who)
    auth.reset_provider_cache(prov)
    session = mint_session_cookie(
        user_id="owner@example.com", cookie_secret=secret, ttl_hours=1, provider="oidc"
    )

    def request(headers: dict[str, str]) -> Request:
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "method": "POST", "headers": raw, "path": "/"})

    from ae_omni_policies import browser_proof

    proof = browser_proof.expected_proof(browser_proof.derive_key(secret), session)
    name = prov.session_cookie_name
    proof_name = browser_proof.proof_cookie_name(name.startswith("__Host-"))
    page = {"Sec-Fetch-Site": "same-origin", "Cookie": f"{name}={session}; {proof_name}={proof}"}
    assert await auth.require_browser_caller(request(page)) == "owner@example.com"
    for headers in (
        {**page, "Authorization": f"Bearer {session}"},
        {"Sec-Fetch-Site": "same-origin", "Authorization": f"Bearer {session}"},
        {**page, "Sec-Fetch-Site": "cross-site"},
    ):
        with pytest.raises(HTTPException) as caught:
            await auth.require_browser_caller(request(headers))
        assert caught.value.status_code == 403


BODIES: dict[str, dict[str, Any]] = {
    "/git/mine/check": {"token": TOKEN_A},
    "/git/mine/save": {"token": TOKEN_A, "repos": [], "revision": 0},
    "/git/mine/repos": {"repos": [], "revision": 0},
    "/git/mine/clear": {"account": "octo-user"},
    "/git/users/clear": {"login": "second@example.com"},
    "/git/company/token": {"owner": "example-org", "token": TOKEN_A},
    "/git/company/clear": {"owner": "example-org"},
}


@pytest.mark.parametrize("path", sorted(NEW_POSTS))
async def test_each_new_post_refuses_a_member_a_bearer_and_a_page_that_is_not_same_origin(
    client: httpx.AsyncClient, ae_admins: set[str], gh: GitHub, key: str, path: str
) -> None:
    ae_admins.add("second@example.com")
    body = BODIES[path]
    refused = [
        await post(client, path, body, MEMBER),
        await post(client, path, body, {**OWNER, "Authorization": "Bearer abc"}),
        await post(client, path, body, {"X-Forwarded-Email": "owner@example.com"}),
        await post(client, path, body, {**OWNER, "Sec-Fetch-Site": "cross-site"}),
    ]
    assert [r.status_code for r in refused] == [403] * 4, [r.text for r in refused]
    assert gh.seen == []


async def test_the_new_gets_refuse_a_member(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    for path in ("/git/mine", "/git/users", "/git?scope=mine"):
        assert (await client.get(f"/v1/ae{path}", headers=MEMBER)).status_code == 403


async def test_a_switched_off_git_card_answers_off_and_calls_nothing(
    client: httpx.AsyncClient, ae_admins: set[str], gh: GitHub, key: str
) -> None:
    admin_cards.store().set_on("git", False, "owner@example.com", 0)
    for path, body in BODIES.items():
        got = await post(client, path, body)
        assert got.json() == {"off": True, "card": "git"}, path
    assert (await client.get("/v1/ae/git/mine", headers=OWNER)).json()["off"] is True
    assert gh.seen == []
    assert "git-mine" in admin_cards.off_sources()


# ── connect an account, read with it ──────────────────────────────────────────────────────────


async def test_connect_save_and_read_with_the_admins_own_token_only(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    gh: GitHub,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AE_GITHUB_TOKEN", COMPANY)
    monkeypatch.setenv("AE_GIT_REPOS", "example-org/web")
    gh.add(TOKEN_A, "Octo-User", ["octo-user/one", "example-org/web"], {"octo-user/one"})
    gh.add(COMPANY, "company-bot", [], {"example-org/web"})

    first = (await client.get("/v1/ae/git/mine", headers=OWNER)).json()
    assert first == {"key_configured": True, "revision": 0, "accounts": [], "repos": []}

    checked = await post(client, "/git/mine/check", {"token": TOKEN_A})
    assert checked.status_code == 200, checked.text
    assert checked.json() == {
        "github_login": "Octo-User",
        "expires_at": "2026-12-01T00:00:00Z",
        "repos": [
            {"repo": "octo-user/one", "private": True},
            {"repo": "example-org/web", "private": True},
        ],
        "already": [],
    }
    assert git_personal.store().get("owner@example.com") == (0, [])  # check saves nothing

    saved = await post(
        client, "/git/mine/save", {"token": TOKEN_A, "repos": ["octo-user/one"], "revision": 0}
    )
    assert saved.status_code == 200, saved.text
    state = saved.json()
    assert state["revision"] == 1 and state["key_configured"] is True
    assert state["repos"] == [{"repo": "octo-user/one", "account": "octo-user"}]
    [account] = state["accounts"]
    assert account["account"] == "octo-user" and account["github_login"] == "Octo-User"
    assert account["repos"] == 1 and account["expires_at"] == "2026-12-01T00:00:00Z"
    assert account["unreadable"] is False
    assert "AAAA" not in saved.text and TOKEN_A not in saved.text

    mine = (await client.get("/v1/ae/git?scope=mine", headers=OWNER)).json()
    assert mine["scope"] == "mine" and [r["repo"] for r in mine["repos"]] == ["octo-user/one"]
    assert mine["repos"][0]["scope"] == "mine"
    company = (await client.get("/v1/ae/git?scope=company", headers=OWNER)).json()
    assert company["scope"] == "company" and [r["repo"] for r in company["repos"]] == [
        "example-org/web"
    ]
    assert company["repos"][0]["scope"] == "company"
    # The personal repo was read with the admin's own token and never the company's; the
    # company repo with the company's and never the admin's.
    assert ("octo-user/one", f"{TOKEN_A}") in gh.reads
    assert ("octo-user/one", COMPANY) not in gh.reads
    assert ("example-org/web", TOKEN_A) not in gh.reads
    assert {host for _m, host, _p in gh.seen} == {"api.github.com"}


async def test_the_all_view_shows_a_repo_in_both_lists_once_under_company(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    gh: GitHub,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AE_GITHUB_TOKEN", COMPANY)
    monkeypatch.setenv("AE_GIT_REPOS", "example-org/web")
    gh.add(COMPANY, "company-bot", [], {"example-org/web"})
    gh.add(TOKEN_A, "Octo-User", [], {"octo-user/one", "example-org/web"})
    save_account("owner@example.com", TOKEN_A, "octo-user", ["octo-user/one", "Example-Org/Web"])
    git_center.invalidate("owner@example.com")
    everything = (await client.get("/v1/ae/git", headers=OWNER)).json()
    assert everything["scope"] == "all"
    rows = {r["repo"]: r["scope"] for r in everything["repos"]}
    assert rows == {"example-org/web": "company", "octo-user/one": "mine"}
    assert everything["totals"]["repos"] == 2
    default = (await client.get("/v1/ae/git?scope=all", headers=OWNER)).json()
    assert default["repos"] == everything["repos"]
    assert (await client.get("/v1/ae/git?scope=bogus", headers=OWNER)).status_code == 422


async def test_saving_needs_the_key_a_fresh_revision_and_at_most_thirty_repos(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    gh: GitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh.add(TOKEN_A, "Octo-User", [], None)
    body = {"token": TOKEN_A, "repos": ["octo-user/one"], "revision": 0}
    nokey = await post(client, "/git/mine/save", body)
    assert nokey.status_code == 409
    assert nokey.json()["error"]["code"] == "card_secrets_key_missing"
    assert card_secrets.KEY_ENV in nokey.json()["error"]["message"]
    assert (await client.get("/v1/ae/git/mine", headers=OWNER)).json()["key_configured"] is False
    assert gh.seen == []  # refused before GitHub was asked

    monkeypatch.setenv(card_secrets.KEY_ENV, Fernet.generate_key().decode())
    assert (await post(client, "/git/mine/save", body)).status_code == 200
    stale = await post(client, "/git/mine/save", body)  # revision 0 again
    assert stale.status_code == 409
    current = stale.json()["error"]["current"]
    assert current["revision"] == 1 and current["repos"][0]["repo"] == "octo-user/one"
    many = {**body, "revision": 1, "repos": [f"octo-user/r{n}" for n in range(31)]}
    too_many = await post(client, "/git/mine/save", many)
    assert too_many.status_code == 422 and "30" in too_many.json()["error"]["message"]
    odd = await post(client, "/git/mine/save", {**body, "revision": 1, "repos": ["not a repo"]})
    assert odd.status_code == 422


async def test_repos_can_be_replaced_and_a_typed_repo_needs_a_connected_account(
    client: httpx.AsyncClient, ae_admins: set[str], gh: GitHub, key: str
) -> None:
    gh.add(TOKEN_A, "Octo-User", [], None)
    await post(
        client, "/git/mine/save", {"token": TOKEN_A, "repos": ["octo-user/one"], "revision": 0}
    )
    changed = await post(
        client,
        "/git/mine/repos",
        {
            "repos": [
                {"repo": "octo-user/one", "account": "Octo-User"},
                {"repo": "example-org/typed", "account": "octo-user"},
            ],
            "revision": 1,
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["revision"] == 2 and len(changed.json()["repos"]) == 2
    unknown = await post(
        client,
        "/git/mine/repos",
        {"repos": [{"repo": "example-org/x", "account": "stranger"}], "revision": 2},
    )
    assert unknown.status_code == 422 and "stranger" in unknown.json()["error"]["message"]
    stale = await post(client, "/git/mine/repos", {"repos": [], "revision": 1})
    assert stale.status_code == 409
    removed = await post(client, "/git/mine/repos", {"repos": [], "revision": 2})
    assert removed.json()["repos"] == [] and removed.json()["accounts"][0]["repos"] == 0


async def test_removing_an_account_deletes_its_token_and_its_repos_only(
    client: httpx.AsyncClient, ae_admins: set[str], gh: GitHub, key: str
) -> None:
    save_account("owner@example.com", TOKEN_A, "octo-user", ["octo-user/one"])
    save_account("owner@example.com", TOKEN_B, "example-org", ["example-org/two"])
    got = await post(client, "/git/mine/clear", {"account": "Octo-User"})
    assert got.status_code == 200, got.text
    state = got.json()
    assert [a["account"] for a in state["accounts"]] == ["example-org"]
    assert state["repos"] == [{"repo": "example-org/two", "account": "example-org"}]
    assert git_personal.token_for("owner@example.com", "octo-user") == ""
    assert (await post(client, "/git/mine/clear", {"account": "octo-user"})).status_code == 404
    assert (await post(client, "/git/mine/clear", {"account": "bad name"})).status_code == 422


# ── a pasted token is never echoed ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "extra"),
    [
        ("/git/mine/check", {}),
        ("/git/mine/save", {"repos": [], "revision": 0}),
        ("/git/company/token", {"owner": "example-org"}),
    ],
)
async def test_no_answer_or_log_line_holds_any_part_of_a_bad_token(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    gh: GitHub,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: str,
    extra: dict[str, Any],
) -> None:
    monkeypatch.setenv("AE_GIT_REPOS", "example-org/web")
    gh.add(TOKEN_A, "Octo-User", [], None)
    attempts: list[Any] = [
        BAD,
        "github_pat_" + MARKER + "x" * 400,
        [BAD],
        {"nested": BAD},
        12345,
        # well formed, but GitHub does not know it
        "github_pat_" + MARKER + "Q" * 60,
    ]
    with caplog.at_level(logging.DEBUG):
        for attempt in attempts:
            got = await post(client, path, {"token": attempt, **extra})
            assert got.status_code == 422, (attempt, got.text)
            assert MARKER not in got.text and "!!!!" not in got.text, got.text
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert MARKER not in logged and "!!!!" not in logged
    # Nothing was saved by any of them.
    assert card_secrets.list_meta("") == {}


async def test_a_github_hiccup_is_a_retry_and_saves_nothing(
    client: httpx.AsyncClient, ae_admins: set[str], gh: GitHub, key: str
) -> None:
    gh.status = 503
    got = await post(client, "/git/mine/save", {"token": TOKEN_A, "repos": [], "revision": 0})
    assert got.status_code == 502 and got.json()["error"]["code"] == "github_unavailable"
    assert TOKEN_A not in got.text
    assert card_secrets.list_meta("") == {}


# ── whose list is whose ───────────────────────────────────────────────────────────────────────


async def test_another_admin_sees_nothing_of_a_personal_list_but_a_count(
    client: httpx.AsyncClient, ae_admins: set[str], gh: GitHub, key: str
) -> None:
    ae_admins.add("second@example.com")
    gh.add(TOKEN_A, "Octo-User", [], {"octo-user/one"})
    save_account("owner@example.com", TOKEN_A, "octo-user", ["octo-user/one"])
    theirs = (await client.get("/v1/ae/git/mine", headers=SECOND)).json()
    assert theirs["accounts"] == [] and theirs["repos"] == []
    answer = (await client.get("/v1/ae/git?scope=mine", headers=SECOND)).json()
    assert answer["repos"] == []
    users = await client.get("/v1/ae/git/users", headers=SECOND)
    assert users.json() == {"users": [{"login": "owner@example.com", "accounts": 1, "repos": 1}]}
    assert "octo-user" not in users.text and "one" not in users.text and "AAAA" not in users.text
    assert not [r for r in gh.reads if r[1] != TOKEN_A]


async def test_offboarding_drops_a_login_and_cannot_be_used_on_yourself(
    client: httpx.AsyncClient, ae_admins: set[str], gh: GitHub, key: str
) -> None:
    ae_admins.add("second@example.com")
    save_account("second@example.com", TOKEN_B, "example-org", ["example-org/two"])
    save_account("owner@example.com", TOKEN_A, "octo-user", ["octo-user/one"])
    me = await post(client, "/git/users/clear", {"login": "owner@example.com"})
    assert me.status_code == 422
    nobody = await post(client, "/git/users/clear", {"login": "nobody@example.com"})
    assert nobody.status_code == 404
    done = await post(client, "/git/users/clear", {"login": "second@example.com"})
    assert done.status_code == 200 and done.json() == {"users": []}
    assert git_personal.token_for("second@example.com", "example-org") == ""
    assert git_personal.store().get("second@example.com") == (0, [])
    assert git_personal.token_for("owner@example.com", "octo-user") == TOKEN_A


async def test_one_logins_answer_failure_and_last_good_never_reach_another(
    key: str, gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_GITHUB_TOKEN", COMPANY)
    monkeypatch.setenv("AE_GIT_TOKEN_OCTO_USER", COMPANY)
    a, b, c = "a@example.com", "b@example.com", "c@example.com"
    gh.add(TOKEN_A, "octo-user", [], {"octo-user/one"})
    gh.add(TOKEN_B, "octo-user", [], {"octo-user/two"})
    gh.add(COMPANY, "company-bot", [], None)  # could read anything, if it were ever used
    save_account(a, TOKEN_A, "octo-user", ["octo-user/one"])
    save_account(b, TOKEN_B, "octo-user", ["octo-user/two"])
    mine = git_center.SCOPE_MINE
    first_a = await git_center.overview(scope=mine, login=a)
    first_b = await git_center.overview(scope=mine, login=b)
    assert [r["repo"] for r in first_a["repos"]] == ["octo-user/one"]
    assert [r["repo"] for r in first_b["repos"]] == ["octo-user/two"]
    assert sorted(gh.reads) == [("octo-user/one", TOKEN_A), ("octo-user/two", TOKEN_B)]

    # A's token dies. A is served its own last good read; B is untouched.
    gh.readable[TOKEN_A] = set()
    again_a = await git_center.overview(scope=mine, login=a, fresh=True)
    assert [r["repo"] for r in again_a["repos"]] == ["octo-user/one"]
    assert again_a["error"] and "last good" in again_a["error"]
    again_b = await git_center.overview(scope=mine, login=b)
    assert again_b["error"] is None and [r["repo"] for r in again_b["repos"]] == ["octo-user/two"]

    # A first load that fails for every repo is remembered for that login alone.
    save_account(c, TOKEN_B, "octo-user", ["octo-user/three"])
    failed_c = await git_center.overview(scope=mine, login=c)
    assert failed_c["error"]
    assert set(git_center._failed) == {"git-mine:c@example.com"}
    assert (await git_center.overview(scope=mine, login=b))["error"] is None

    # What is held is keyed by login too.
    held_b, _age, _why = await git_center.cached(kick=False, scope=mine, login=b)
    assert held_b is not None and [r["repo"] for r in held_b["repos"]] == ["octo-user/two"]
    nothing, _age, _why = await git_center.cached(kick=False, scope=mine, login="d@example.com")
    assert nothing is None
    # No read ever carried the company token.
    assert all(bearer != COMPANY for _repo, bearer in gh.reads)


async def test_a_personal_repo_is_never_read_with_a_company_or_another_users_token(
    key: str, gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_GITHUB_TOKEN", COMPANY)
    monkeypatch.setenv("AE_GIT_TOKEN_OCTO_USER", COMPANY)
    gh.add(COMPANY, "company-bot", [], None)
    gh.add(TOKEN_A, "octo-user", [], None)
    save_account("a@example.com", TOKEN_A, "octo-user", ["octo-user/one"])
    # b lists a repo under an account only a has a token for.
    git_personal.store().put(
        "b@example.com", [{"repo": "octo-user/one", "account": "octo-user"}], 0
    )
    answer = await git_center.overview(scope=git_center.SCOPE_MINE, login="b@example.com")
    [row] = answer["repos"]
    assert "No token" in row["error"] and "Cards panel" in row["error"]
    assert answer["accounts"][0]["connected"] is False
    assert gh.reads == [] and not [s for s in gh.seen if s[2] == "/graphql"]


async def test_personal_reads_ignore_the_environments_api_address(
    key: str, gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_GITHUB_API_URL", "https://ghe.example.test/api/v3")
    gh.add(TOKEN_A, "octo-user", [], {"octo-user/one"})
    save_account("a@example.com", TOKEN_A, "octo-user", ["octo-user/one"])
    answer = await git_center.overview(scope=git_center.SCOPE_MINE, login="a@example.com")
    assert [r["repo"] for r in answer["repos"]] == ["octo-user/one"]
    assert {host for _m, host, _p in gh.seen} == {"api.github.com"}


# ── company tokens ────────────────────────────────────────────────────────────────────────────


async def test_a_company_token_is_saved_for_a_listed_account_and_the_environment_wins(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    gh: GitHub,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AE_GIT_REPOS", "example-org/web, fabrikam-labs/api")
    monkeypatch.setenv("AE_GITHUB_TOKEN", COMPANY)
    gh.add(TOKEN_A, "Octo-User", [], {"example-org/web"})
    unlisted = await post(client, "/git/company/token", {"owner": "elsewhere", "token": TOKEN_A})
    assert unlisted.status_code == 422
    saved = await post(client, "/git/company/token", {"owner": "Example-Org", "token": TOKEN_A})
    assert saved.status_code == 200, saved.text
    assert TOKEN_A not in saved.text
    rows = {o["owner"]: o for o in saved.json()["owners"]}
    assert rows["example-org"]["source"] == "saved"
    assert rows["example-org"]["saved_by"] == "owner@example.com"
    assert rows["example-org"]["github_login"] == "Octo-User"
    assert rows["example-org"]["expires_at"] == "2026-12-01T00:00:00Z"
    assert rows["fabrikam-labs"]["source"] == "missing" and rows["fabrikam-labs"]["fallback"]

    # The order: the owner's variable, then the saved token, then AE_GITHUB_TOKEN.
    settings = git_center.Settings()
    assert settings.token_for("example-org") == TOKEN_A
    assert settings.token_for("fabrikam-labs") == COMPANY
    monkeypatch.setenv("AE_GIT_TOKEN_EXAMPLE_ORG", TOKEN_B)
    assert git_center.Settings().token_for("example-org") == TOKEN_B
    clash = await post(client, "/git/company/token", {"owner": "example-org", "token": TOKEN_A})
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "set_in_environment"

    cards = (await client.get("/v1/ae/cards", headers=OWNER)).json()
    git = next(c for c in cards["cards"] if c["id"] == "git")
    field = next(f for f in git["fields"] if f["env"] == "AE_GIT_TOKEN_")
    assert field["typed"] is True and field["key_configured"] is True
    by_owner = {o["owner"]: o["source"] for o in field["owners"]}
    assert by_owner == {"example-org": "env", "fabrikam-labs": "missing"}
    assert TOKEN_A not in json.dumps(cards) and TOKEN_B not in json.dumps(cards)

    monkeypatch.delenv("AE_GIT_TOKEN_EXAMPLE_ORG")
    cleared = await post(client, "/git/company/clear", {"owner": "example-org"})
    assert cleared.status_code == 200
    assert git_center.Settings().token_for("example-org") == COMPANY
    again = await post(client, "/git/company/clear", {"owner": "example-org"})
    assert again.status_code == 404


async def test_a_company_token_needs_the_key(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    gh: GitHub,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AE_GIT_REPOS", "example-org/web")
    got = await post(client, "/git/company/token", {"owner": "example-org", "token": TOKEN_A})
    assert got.status_code == 409 and got.json()["error"]["code"] == "card_secrets_key_missing"
    assert gh.seen == []


# ── Needs you ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def mine_items(key: str, gh: GitHub) -> dict[str, list[dict[str, Any]]]:
    """a@ and b@ each have a ready-to-merge PR on a repo of their own."""
    gh.add(TOKEN_A, "octo-user", [], {"octo-user/one"})
    gh.add(TOKEN_B, "example-org", [], {"example-org/two"})
    gh.prs = {"octo-user/one": [ready_pr("octo-user/one")], "example-org/two": [ready_pr("x/y")]}
    save_account("owner@example.com", TOKEN_A, "octo-user", ["octo-user/one"])
    save_account("second@example.com", TOKEN_B, "example-org", ["example-org/two"])
    return {}


async def _fill(login: str) -> None:
    await git_center.overview(scope=git_center.SCOPE_MINE, login=login)


async def test_personal_items_take_dismiss_only_and_belong_to_their_login(
    mine_items: dict[str, Any],
) -> None:
    await _fill("owner@example.com")
    await _fill("second@example.com")
    now = datetime.now(UTC)
    reading = await attention.read_git_mine(now, "owner@example.com")
    [item] = reading.items
    assert item["id"].startswith("git-mine:owner@example.com:pr-ready:")
    assert item["source"] == "git-mine" and item["kind"] == "pr-ready"
    assert item["actions"] == ["dismiss"] and item["url"].startswith("https://github.com/")
    assert "fix_note" not in item and "fix_mode" not in item
    assert item["scope"] == "git-mine:owner@example.com:repo:octo-user/one"
    assert {s.id for s in reading.scopes} >= {
        "git-mine:owner@example.com:accounts",
        "git-mine:owner@example.com:repo:octo-user/one",
    }
    other = await attention.read_git_mine(now, "second@example.com")
    assert [i["id"].split(":")[1] for i in other.items] == ["second@example.com"]
    # The background cleanup (no login) reads nobody's list.
    nobody = await attention.read_git_mine(now, None)
    assert nobody.items == [] and nobody.scopes == [] and nobody.unknown is False
    # A kind that is dismissible nowhere else offers nothing here when it is not dismissible.
    assert (
        attention._finish(
            {"kind": "pr-ready", "tone": "review", "state": {}, "dismissible": False}, "git-mine"
        )["actions"]
        == []
    )


async def test_the_list_and_every_chat_route_for_a_personal_item(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    mine_items: dict[str, Any],
) -> None:
    ae_admins.add("second@example.com")
    await _fill("owner@example.com")
    await _fill("second@example.com")
    listing = (await client.get("/v1/ae/attention", headers=OWNER)).json()
    mine = [i for i in listing["items"] if i["source"] == "git-mine"]
    assert [i["actions"] for i in mine] == [["dismiss"]]
    assert all("second@example.com" not in i["id"] for i in mine)
    item_id = mine[0]["id"]
    assert "git-mine" in listing["sources"]

    for action in ("review", "fix", "merge"):
        got = await post(client, "/attention/session", {"id": item_id, "action": action})
        assert got.status_code == 422, (action, got.text)
    refused = await post(client, "/attention/session", {"id": item_id, "action": "review"})
    assert "Dismiss" in refused.json()["error"]["message"]
    for body in (
        {"source": "git-mine", "ids": [item_id]},
        {"line": "pr-ready", "ids": [item_id]},
    ):
        got = await post(client, "/attention/batch", body)
        assert got.status_code == 422, (body, got.text)

    # Another admin cannot find, dismiss or restore it; its owner can.
    theirs = await post(client, "/attention/dismiss", {"id": item_id}, SECOND)
    assert theirs.status_code == 404
    done = await post(client, "/attention/dismiss", {"id": item_id})
    assert done.status_code == 200, done.text
    after = (await client.get("/v1/ae/attention", headers=OWNER)).json()
    assert not [i for i in after["items"] if i["source"] == "git-mine"]
    assert [i["id"] for i in after["dismissed"] if i["source"] == "git-mine"] == [item_id]


async def test_the_personal_source_is_the_git_cards_and_goes_off_with_it() -> None:
    git = admin_cards.BY_ID["git"]
    assert git.sources == ("git", "git-mine")
    assert "AE_CARD_SECRETS_KEY" in git.secret_keys
    assert attention.SOURCE_NAMES["git-mine"] == "My Git repos"
    assert "git-mine" in attention.SOURCES and attention.line_source("pr-ready") == "git"
    assert attention.LINES["git-mine"] == attention.LINES["git"]
    admin_cards.store().set_on("git", False, "owner@example.com", 0)
    assert {"git", "git-mine"} <= admin_cards.off_sources()
    assert await attention.kick_personal(datetime.now(UTC)) == []


async def test_the_cleanup_refreshes_recent_admin_lists_staggered_and_capped(
    key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    kicked: list[str] = []
    monkeypatch.setattr(git_center, "_kick", lambda _scope, login: kicked.append(login))
    monkeypatch.setattr(attention, "PERSONAL_STAGGER_S", 0.0)
    monkeypatch.setattr(attention, "_last_personal", {})
    store = git_personal.store()
    for n in range(25):
        store.put(f"user{n:02d}@example.com", [{"repo": "example-org/web", "account": "a"}], 0)
    store.put("nobody@example.com", [], 0)  # a list with no repos is never refreshed
    # A login that has not opened the card in a week is skipped.
    store.drop_user("user24@example.com")
    store._users["user24@example.com"] = {  # type: ignore[attr-defined]
        "revision": 1,
        "repos": [{"repo": "example-org/web", "account": "a"}],
        "seen_at": "2026-10-01T00:00:00Z",
    }
    git_personal._roster = {f"user{n:02d}@example.com": n != 10 for n in range(25)}
    now = datetime(2026, 11, 8, tzinfo=UTC)
    for login in store.logins():
        if login != "user24@example.com":
            store._users[login]["seen_at"] = "2026-11-07T00:00:00Z"  # type: ignore[attr-defined]
    started = await attention.kick_personal(now)
    await asyncio.sleep(0.05)
    assert len(started) == git_personal.MAX_USERS and sorted(kicked) == sorted(started)
    assert "user10@example.com" not in started  # not an admin in the last good roster
    assert "user24@example.com" not in started and "nobody@example.com" not in started
    # 23 logins are eligible and 20 started: the next pass starts only the other 3, and inside
    # fifteen minutes nobody is started twice.
    rest = await attention.kick_personal(now)
    assert len(rest) == 3 and not set(rest) & set(started)
    assert await attention.kick_personal(now) == []


# ── review round: no stock 422, identity-keyed caches, ordered saves, switch re-checks ────────

TOKEN_M = "github_pat_" + MARKER + "A1b2C3d4E5F6g7H8i9J0" * 3
TOKEN_A2 = "github_pat_A2A2" + "A1b2C3d4E5F6g7H8i9J0" * 3
RAW = {**OWNER, "Content-Type": "application/json"}


def _raw(client: httpx.AsyncClient, path: str, content: str | bytes) -> Any:
    data = content.encode() if isinstance(content, str) else content
    return client.post(f"/v1/ae{path}", content=data, headers=RAW)


@pytest.mark.parametrize("path", sorted(NEW_POSTS))
async def test_a_malformed_body_never_brings_a_pasted_token_back(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    gh: GitHub,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    path: str,
) -> None:
    ae_admins.add("second@example.com")
    monkeypatch.setenv("AE_GIT_REPOS", "example-org/web")
    good = {
        "/git/mine/check": {"token": TOKEN_M},
        "/git/mine/save": {"token": TOKEN_M, "repos": [], "revision": 0},
        "/git/mine/repos": {"repos": [], "revision": 0},
        "/git/mine/clear": {"account": "octo-user"},
        "/git/users/clear": {"login": "second@example.com"},
        "/git/company/token": {"owner": "example-org", "token": TOKEN_M},
        "/git/company/clear": {"owner": "example-org"},
    }[path]
    bodies: list[Any] = [
        # a missing field, with the token (when the route takes one) still in the body
        {k: v for k, v in good.items() if k != next((k for k in good if k != "token"), "token")},
        {},
        # an extra field, and an extra field whose NAME is the token
        {**good, "extra": TOKEN_M},
        {**good, TOKEN_M: 1},
        # wrong types, with the token in a neighbour
        {**good, **{k: TOKEN_M for k in good if k != "token"}},
        {**good, **{k: [TOKEN_M] for k in good}},
        # not an object
        [TOKEN_M],
        TOKEN_M,
        None,
    ]
    with caplog.at_level(logging.DEBUG):
        for body in bodies:
            if body == good:
                continue  # a one-field route cannot lose a field and keep one
            got = await _raw(client, path, json.dumps(body))
            # A text in a text field is well formed; the route's own check then refuses it
            # (404 or 422). Whatever the answer, it never carries the token.
            assert got.status_code in (404, 409, 422), (path, body, got.text)
            assert "error" in got.json()
            assert "ZzSecret" not in got.text and "input" not in got.json()["error"], got.text
        # invalid JSON, cut off inside the token
        broken = '{"token": "' + TOKEN_M
        for content in (broken, b"\xff\xfe" + TOKEN_M.encode(), ""):
            got = await _raw(client, path, content)
            assert got.status_code == 422 and "ZzSecret" not in got.text, got.text
    logged = " ".join(record.getMessage() for record in caplog.records)
    assert "ZzSecret" not in logged
    assert gh.seen == [] and card_secrets.list_meta("") == {}


async def test_a_bad_body_still_answers_a_member_403_and_an_off_card_off(
    client: httpx.AsyncClient, ae_admins: set[str], gh: GitHub, key: str
) -> None:
    got = await client.post(
        "/v1/ae/git/mine/save",
        content=b"[1]",
        headers={**MEMBER, "Content-Type": "application/json"},
    )
    assert got.status_code == 403
    admin_cards.store().set_on("git", False, "owner@example.com", 0)
    off = await _raw(client, "/git/mine/save", "[1]")
    assert off.json() == {"off": True, "card": "git"}


# 3. the lower-level caches ----------------------------------------------------------------


def _npm_lock() -> str:
    return json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {"": {"name": "root"}, "node_modules/undici": {"version": "7.30.1"}},
        }
    )


async def test_one_identitys_cached_lockfile_or_404_never_serves_another() -> None:
    from ae_omni_policies.git_center import PersonalSettings, Settings

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bearer = request.headers["authorization"].removeprefix("Bearer ")
        calls.append(bearer)
        if bearer in (TOKEN_A, COMPANY):
            return httpx.Response(200, text=_npm_lock())
        return httpx.Response(404, json={"message": "Not Found"})

    repo, path, oid = "example-org/shared", "package-lock.json", "abc123"
    company = Settings({})
    a = PersonalSettings("a@example.com", [{"repo": repo, "account": "octo-user"}])
    b = PersonalSettings("b@example.com", [{"repo": repo, "account": "octo-user"}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def read(settings: Any, token: str) -> Any:
            return await git_center._read_index(
                client, settings, repo, "example-org", token, "NPM", path, oid
            )

        assert await read(b, TOKEN_B) is None  # B's token cannot see it: a cached 404 for B only
        assert await read(a, TOKEN_A) is not None  # A is not served B's 404
        assert await read(b, TOKEN_B) is None  # and B is not served A's index
        assert await read(company, COMPANY) is not None  # nor the company A's, nor B's 404
        assert calls == [TOKEN_B, TOKEN_A, COMPANY]  # each identity read once, then cached
        # Saving or clearing a token drops that identity's entries only.
        git_center.invalidate("a@example.com")
        assert await read(a, TOKEN_A) is not None
        assert await read(b, TOKEN_B) is None
        assert await read(company, COMPANY) is not None
        assert calls == [TOKEN_B, TOKEN_A, COMPANY, TOKEN_A]
        git_center.invalidate()
        assert await read(company, COMPANY) is not None
        assert calls[-1] == COMPANY and len(calls) == 5

    # The commit-date cache is keyed the same way.
    seen: list[str] = []

    def commits(request: httpx.Request) -> httpx.Response:
        bearer = request.headers["authorization"].removeprefix("Bearer ")
        seen.append(bearer)
        if bearer == TOKEN_B:
            return httpx.Response(404, json={})
        return httpx.Response(
            200, json=[{"commit": {"committer": {"date": "2026-11-01T00:00:00Z"}}}]
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(commits)) as client:
        args = (repo, "example-org")
        assert await git_center._lockfile_commit_date(client, a, *args, TOKEN_A, "main", path, oid)
        assert (
            await git_center._lockfile_commit_date(client, b, *args, TOKEN_B, "main", path, oid)
            is None
        )
        assert await git_center._lockfile_commit_date(client, a, *args, TOKEN_A, "main", path, oid)
        assert seen == [TOKEN_A, TOKEN_B]


# 4. ordered, serialized personal saves ----------------------------------------------------


async def test_a_stale_save_leaves_the_saved_token_alone(
    client: httpx.AsyncClient, ae_admins: set[str], gh: GitHub, key: str
) -> None:
    gh.add(TOKEN_A, "Octo-User", [], None)
    gh.add(TOKEN_A2, "Octo-User", [], None)
    first = await post(
        client, "/git/mine/save", {"token": TOKEN_A, "repos": ["octo-user/one"], "revision": 0}
    )
    assert first.status_code == 200
    stale = await post(
        client, "/git/mine/save", {"token": TOKEN_A2, "repos": ["octo-user/two"], "revision": 0}
    )
    assert stale.status_code == 409
    assert git_personal.token_for("owner@example.com", "octo-user") == TOKEN_A
    assert git_personal.store().get("owner@example.com")[1] == [
        {"repo": "octo-user/one", "account": "octo-user"}
    ]
    assert TOKEN_A2 not in stale.text


async def test_two_overlapping_saves_leave_one_consistent_pair(
    client: httpx.AsyncClient, ae_admins: set[str], gh: GitHub, key: str
) -> None:
    gh.add(TOKEN_A, "Octo-User", [], None)
    gh.add(TOKEN_B, "example-org", [], None)
    one, two = await asyncio.gather(
        post(
            client, "/git/mine/save", {"token": TOKEN_A, "repos": ["octo-user/one"], "revision": 0}
        ),
        post(
            client,
            "/git/mine/save",
            {"token": TOKEN_B, "repos": ["example-org/two"], "revision": 0},
        ),
    )
    assert sorted([one.status_code, two.status_code]) == [200, 409]
    revision, repos = git_personal.store().get("owner@example.com")
    saved = {
        record.rsplit("/", 1)[1] for record in card_secrets.list_meta("user/owner@example.com/")
    }
    assert revision == 1 and len(saved) == 1
    assert {r["account"] for r in repos} == saved  # every pick has its token, and no stray token


async def test_a_list_write_failure_leaves_the_token_alone_and_a_token_failure_restores_the_list(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    gh: GitHub,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh.add(TOKEN_A, "Octo-User", [], None)
    gh.add(TOKEN_A2, "Octo-User", [], None)
    save_account("owner@example.com", TOKEN_A, "octo-user", ["octo-user/one"])
    revision, before = git_personal.store().get("owner@example.com")
    body = {"token": TOKEN_A2, "repos": ["octo-user/two"], "revision": revision}

    real_put = git_personal.PersonalStore.put

    def broken_put(self: Any, *_a: Any, **_k: Any) -> Any:
        raise git_personal.PersonalSaveError("disk full")

    monkeypatch.setattr(git_personal.PersonalStore, "put", broken_put)
    failed = await post(client, "/git/mine/save", body)
    assert failed.status_code == 500
    assert git_personal.token_for("owner@example.com", "octo-user") == TOKEN_A
    monkeypatch.setattr(git_personal.PersonalStore, "put", real_put)

    def broken_secret(*_a: Any, **_k: Any) -> None:
        raise card_secrets.SaveError("could not write")

    monkeypatch.setattr(card_secrets, "put", broken_secret)
    failed = await post(client, "/git/mine/save", body)
    assert failed.status_code == 503 and failed.json()["error"]["code"] == "card_secrets_not_saved"
    assert git_personal.token_for("owner@example.com", "octo-user") == TOKEN_A
    assert git_personal.store().get("owner@example.com")[1] == before  # the old list is back


async def test_a_conflict_whose_state_cannot_be_read_answers_that_error_not_a_500(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    gh: GitHub,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh.add(TOKEN_A, "Octo-User", [], None)
    await post(client, "/git/mine/save", {"token": TOKEN_A, "repos": [], "revision": 0})

    def unreadable(_prefix: str) -> Any:
        raise card_secrets.SaveError("could not read the file")

    monkeypatch.setattr(card_secrets, "list_meta", unreadable)
    got = await post(client, "/git/mine/repos", {"repos": [], "revision": 0})
    assert got.status_code == 503, got.text
    assert got.json()["error"]["code"] == "card_secrets_not_saved"


# 6. removing an account ---------------------------------------------------------------------


async def test_a_removal_that_keeps_conflicting_answers_409_and_removes_nothing(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    gh: GitHub,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_account("owner@example.com", TOKEN_A, "octo-user", ["octo-user/one"])
    calls: list[int] = []

    def conflicting(self: Any, login: str, repos: Any, base: int, **_k: Any) -> Any:
        calls.append(base)
        return git_personal.PutResult(base + 1, [], conflict=True)

    monkeypatch.setattr(git_personal.PersonalStore, "put", conflicting)
    got = await post(client, "/git/mine/clear", {"account": "octo-user"})
    assert got.status_code == 409 and len(calls) == 3
    assert git_personal.token_for("owner@example.com", "octo-user") == TOKEN_A
    assert git_personal.store().get("owner@example.com")[1]


async def test_a_removal_whose_list_write_fails_keeps_the_token(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    gh: GitHub,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_account("owner@example.com", TOKEN_A, "octo-user", ["octo-user/one"])

    def broken(self: Any, *_a: Any, **_k: Any) -> Any:
        raise git_personal.PersonalSaveError("disk full")

    monkeypatch.setattr(git_personal.PersonalStore, "put", broken)
    got = await post(client, "/git/mine/clear", {"account": "octo-user"})
    assert got.status_code == 500
    assert git_personal.token_for("owner@example.com", "octo-user") == TOKEN_A


# 5. the card switch is re-checked when a scheduled refresh runs ----------------------------


async def test_a_refresh_scheduled_before_git_went_off_reads_nothing(
    key: str, gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh.add(TOKEN_A, "octo-user", [], {"octo-user/one"})
    save_account("a@example.com", TOKEN_A, "octo-user", ["octo-user/one"])
    save_account("b@example.com", TOKEN_A, "octo-user", ["octo-user/one"])
    kicked: list[str] = []
    monkeypatch.setattr(git_center, "_kick", lambda _scope, login: kicked.append(login))
    monkeypatch.setattr(attention, "PERSONAL_STAGGER_S", 0.05)
    monkeypatch.setattr(attention, "_last_personal", {})
    started = await attention.kick_personal(datetime.now(UTC))
    assert len(started) == 2
    admin_cards.store().set_on(
        "git", False, "owner@example.com", 0
    )  # between scheduling and running
    await asyncio.sleep(0.3)
    assert kicked == [] and gh.reads == []


async def test_the_kick_itself_does_nothing_while_git_is_off(key: str, gh: GitHub) -> None:
    gh.add(TOKEN_A, "octo-user", [], {"octo-user/one"})
    save_account("a@example.com", TOKEN_A, "octo-user", ["octo-user/one"])
    admin_cards.store().set_on("git", False, "owner@example.com", 0)
    git_center._kick(git_center.SCOPE_MINE, "a@example.com")
    git_center._kick()
    await asyncio.sleep(0.1)
    assert not git_center._kicked and gh.reads == []


# 9. the All view ------------------------------------------------------------------------------


def _row(repo: str, scope: str, error: str | None = None) -> dict[str, Any]:
    return {
        "repo": repo,
        "scope": scope,
        "error": error,
        "view_only": False,
        "prs": [],
        "alerts": [],
        "branches": [],
    }


def test_the_all_view_keeps_the_personal_row_when_only_the_company_one_failed() -> None:
    company = {"repos": [_row("example-org/web", "company", "No token")], "accounts": []}
    mine = {"repos": [_row("Example-Org/Web", "mine"), _row("octo-user/one", "mine")]}
    merged = git_center.merge(company, mine)
    assert {r["repo"]: r["scope"] for r in merged["repos"]} == {
        "Example-Org/Web": "mine",
        "octo-user/one": "mine",
    }
    assert merged["totals"]["repo_errors"] == 0
    # Both fine, or both failed: the company row stays.
    for company_error, mine_error in ((None, None), (None, "x"), ("a", "b")):
        both = git_center.merge(
            {"repos": [_row("example-org/web", "company", company_error)]},
            {"repos": [_row("example-org/web", "mine", mine_error)]},
        )
        assert [r["scope"] for r in both["repos"]] == ["company"]
