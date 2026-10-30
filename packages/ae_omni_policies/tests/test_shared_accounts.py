"""Shared Claude accounts: the store, its encryption, and the /v1/ae/claude-accounts routes."""

from __future__ import annotations

import json
import stat
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from ae_omni_policies import auth, shared_accounts, shared_accounts_routes

TOKEN = "sk-ant-oat01-" + "a" * 40
OTHER_TOKEN = "sk-ant-oat01-" + "b" * 40
OWNER = {"X-Forwarded-Email": "owner@example.com"}
MEMBER = {"X-Forwarded-Email": "member@example.com"}
# What every browser fetch carries and httpx never sends.
BROWSER = {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}


@pytest.fixture(autouse=True)
def _shared_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "shared.json"
    monkeypatch.setenv(shared_accounts.STATE_FILE_ENV, str(path))
    monkeypatch.setenv(shared_accounts.KEY_ENV, Fernet.generate_key().decode())
    shared_accounts.reset_store()
    yield path
    shared_accounts.reset_store()


@pytest.fixture()
def admins(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Answer the admin question from a set, without a user database."""
    allowed = {"owner@example.com"}

    async def probe(_request: Any, user: str | None) -> bool:
        return user in allowed

    async def session_ok(_request: Any, _user: str | None) -> bool:
        return True

    monkeypatch.setattr(shared_accounts_routes, "admin_probe", probe)
    monkeypatch.setattr(shared_accounts_routes, "session_probe", session_ok)
    return allowed


# ── the store ───────────────────────────────────────────────────────────────


def test_encryption_round_trip_and_nothing_plain_on_disk(_shared_store: Path) -> None:
    store = shared_accounts.SharedAccountsStore(_shared_store)
    view = store.add("Team", "Team Max", TOKEN, "owner@example.com")
    assert view.account == "shared:Team"
    text = _shared_store.read_text(encoding="utf-8")
    assert TOKEN not in text
    assert "Team Max" in text
    assert stat.S_IMODE(_shared_store.stat().st_mode) == 0o600
    # A fresh store reads the file back and decrypts with the same key.
    again = shared_accounts.SharedAccountsStore(_shared_store)
    answer = again.token("Team")
    assert (answer.token, answer.label) == (TOKEN, "Team Max")


def test_two_keys_rotate(monkeypatch: pytest.MonkeyPatch, _shared_store: Path) -> None:
    old = Fernet.generate_key().decode()
    monkeypatch.setenv(shared_accounts.KEY_ENV, old)
    shared_accounts.SharedAccountsStore(_shared_store).add("Team", None, TOKEN, "owner")
    monkeypatch.setenv(shared_accounts.KEY_ENV, f"{Fernet.generate_key().decode()},{old}")
    assert shared_accounts.SharedAccountsStore(_shared_store).token("Team").token == TOKEN


def test_the_first_load_reseals_under_the_new_key(
    monkeypatch: pytest.MonkeyPatch, _shared_store: Path
) -> None:
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()
    monkeypatch.setenv(shared_accounts.KEY_ENV, old)
    shared_accounts.SharedAccountsStore(_shared_store).add("Team", None, TOKEN, "owner")
    monkeypatch.setenv(shared_accounts.KEY_ENV, f"{new},{old}")
    assert shared_accounts.SharedAccountsStore(_shared_store).token("Team").token == TOKEN
    monkeypatch.setenv(shared_accounts.KEY_ENV, new)
    assert shared_accounts.SharedAccountsStore(_shared_store).token("Team").token == TOKEN


def test_a_changed_key_says_so(monkeypatch: pytest.MonkeyPatch, _shared_store: Path) -> None:
    shared_accounts.SharedAccountsStore(_shared_store).add("Team", None, TOKEN, "owner")
    monkeypatch.setenv(shared_accounts.KEY_ENV, Fernet.generate_key().decode())
    with pytest.raises(shared_accounts.UnreadableError, match="key"):
        shared_accounts.SharedAccountsStore(_shared_store).token("Team")


def test_missing_key_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(shared_accounts.KEY_ENV)
    store = shared_accounts.SharedAccountsStore(None)
    with pytest.raises(shared_accounts.KeyMissingError, match=shared_accounts.KEY_ENV):
        store.add("Team", None, TOKEN, "owner")
    assert not shared_accounts.key_configured()
    monkeypatch.setenv(shared_accounts.KEY_ENV, "not-a-key")
    with pytest.raises(shared_accounts.KeyInvalidError, match="Fernet"):
        store.add("Team", None, TOKEN, "owner")


@pytest.mark.parametrize(
    ("name", "label", "token"),
    [
        ("shared:x", None, TOKEN),
        ("", None, TOKEN),
        ("Team", "bad/label", TOKEN),
        ("Team", None, "hunter2"),
        ("Team", None, "sk-ant-api03-key"),
    ],
)
def test_bad_input_is_refused(name: str, label: str | None, token: str) -> None:
    with pytest.raises(shared_accounts.SharedAccountsError):
        shared_accounts.SharedAccountsStore(None).add(name, label, token, "owner")


def test_names_and_labels_stay_unique() -> None:
    store = shared_accounts.SharedAccountsStore(None)
    store.add("Team", "Team Max", TOKEN, "owner")
    with pytest.raises(shared_accounts.ExistsError):
        store.add("Team", "Other", OTHER_TOKEN, "owner")
    with pytest.raises(shared_accounts.ExistsError):
        store.add("Second", "team max", OTHER_TOKEN, "owner")
    store.add("Second", None, OTHER_TOKEN, "owner")
    with pytest.raises(shared_accounts.ExistsError):
        store.relabel("Second", "Team Max", "owner")
    assert [view.label for view in store.views()] == ["Second", "Team Max"]


def test_an_unreadable_file_is_never_overwritten(_shared_store: Path) -> None:
    _shared_store.write_text("{not json", encoding="utf-8")
    store = shared_accounts.SharedAccountsStore(_shared_store)
    with pytest.raises(shared_accounts.SaveError, match="could not be read"):
        store.add("Team", None, TOKEN, "owner")
    assert _shared_store.read_text(encoding="utf-8") == "{not json"


def test_known_account_reads_the_id() -> None:
    shared_accounts.store().add("Team", None, TOKEN, "owner")
    assert shared_accounts.is_known_account("shared:Team")
    assert not shared_accounts.is_known_account("shared:Other")
    assert not shared_accounts.is_known_account("Team")


# ── the routes ──────────────────────────────────────────────────────────────


async def _add(client: httpx.AsyncClient, headers: dict[str, str], **body: Any) -> httpx.Response:
    return await client.post(
        "/v1/ae/claude-accounts/shared",
        json={"name": "Team", "label": "Team Max", "token": TOKEN, **body},
        headers=headers,
    )


async def test_admin_adds_relabels_and_deletes(
    client: httpx.AsyncClient, admins: set[str]
) -> None:
    added = await _add(client, OWNER)
    assert added.status_code == 201, added.text
    assert added.json()["account"]["created_by"] == "owner@example.com"
    assert TOKEN not in added.text
    relabelled = await client.post(
        "/v1/ae/claude-accounts/shared/Team/label", json={"label": "Company"}, headers=OWNER
    )
    assert relabelled.status_code == 200, relabelled.text
    assert relabelled.json()["account"]["label"] == "Company"
    listed = (await client.get("/v1/ae/claude-accounts/shared", headers=OWNER)).json()
    assert [row["label"] for row in listed["accounts"]] == ["Company"]
    assert listed["can_manage"] is True
    removed = await client.post("/v1/ae/claude-accounts/shared/Team/delete", headers=OWNER)
    assert removed.status_code == 200, removed.text
    listed = (await client.get("/v1/ae/claude-accounts/shared", headers=OWNER)).json()
    assert listed["accounts"] == []


async def test_a_non_admin_lists_but_cannot_manage(
    client: httpx.AsyncClient, admins: set[str]
) -> None:
    assert (await _add(client, OWNER)).status_code == 201
    listed = await client.get("/v1/ae/claude-accounts/shared", headers=MEMBER)
    assert listed.status_code == 200
    body = listed.json()
    assert body["can_manage"] is False
    assert body["key_configured"] is True
    assert [(row["name"], row["account"]) for row in body["accounts"]] == [("Team", "shared:Team")]
    assert TOKEN not in listed.text
    assert all("token" not in row for row in body["accounts"])
    for response in (
        await _add(client, MEMBER, name="Mine", label="Mine"),
        await client.post(
            "/v1/ae/claude-accounts/shared/Team/label", json={"label": "X"}, headers=MEMBER
        ),
        await client.post("/v1/ae/claude-accounts/shared/Team/delete", headers=MEMBER),
    ):
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "admin_only"
    assert [view.label for view in shared_accounts.store().views()] == ["Team Max"]


async def test_a_non_admins_host_fetches_the_token(
    client: httpx.AsyncClient, admins: set[str]
) -> None:
    assert (await _add(client, OWNER)).status_code == 201
    response = await client.get("/v1/ae/claude-accounts/shared/Team/token", headers=MEMBER)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "name": "Team",
        "account": "shared:Team",
        "label": "Team Max",
        "token": TOKEN,
    }
    assert response.headers["cache-control"] == "no-store"


async def test_a_host_the_server_no_longer_accepts_gets_no_token(
    client: httpx.AsyncClient, admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await _add(client, OWNER)).status_code == 201

    async def revoked(_request: Any, _user: str | None) -> bool:
        return False

    monkeypatch.setattr(shared_accounts_routes, "session_probe", revoked)
    response = await client.get("/v1/ae/claude-accounts/shared/Team/token", headers=MEMBER)
    assert response.status_code == 401
    assert TOKEN not in response.text


async def test_a_browser_never_gets_the_token(client: httpx.AsyncClient, admins: set[str]) -> None:
    assert (await _add(client, OWNER)).status_code == 201
    response = await client.get(
        "/v1/ae/claude-accounts/shared/Team/token", headers={**OWNER, **BROWSER}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "shared_account_token_browser"
    assert TOKEN not in response.text


async def test_the_token_needs_an_identity_without_the_cookie(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, admins: set[str]
) -> None:
    assert (await _add(client, OWNER)).status_code == 201
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    response = await client.get(
        "/v1/ae/claude-accounts/shared/Team/token", headers={"Cookie": "omnigent_session=x"}
    )
    assert response.status_code == 401
    assert TOKEN not in response.text


async def test_an_unknown_account_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/ae/claude-accounts/shared/Nope/token")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "shared_account_not_found"


async def test_missing_key_is_a_clear_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(shared_accounts.KEY_ENV)
    added = await _add(client, {})
    assert added.status_code == 503
    assert added.json()["error"]["code"] == "shared_accounts_key_missing"
    assert shared_accounts.KEY_ENV in added.json()["error"]["message"]
    listed = (await client.get("/v1/ae/claude-accounts/shared")).json()
    assert listed["key_configured"] is False


async def test_a_bad_token_is_400(client: httpx.AsyncClient) -> None:
    response = await _add(client, {}, token="not-a-token")
    assert response.status_code == 400
    assert "setup-token" in response.json()["error"]["message"]


async def test_the_single_user_runtime_is_the_admin(client: httpx.AsyncClient) -> None:
    assert (await _add(client, {})).status_code == 201
    listed = (await client.get("/v1/ae/claude-accounts/shared")).json()
    assert listed["can_manage"] is True


# ── the admin answer, from /v1/me ───────────────────────────────────────────


@pytest.fixture()
async def admin_client(
    ae_stores: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """An app with real header auth and ``admins: [owner@example.com]``."""
    from omnigent.server.app import create_app
    from omnigent.server.auth import create_auth_provider

    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    app: FastAPI = create_app(
        **ae_stores, admins=["owner@example.com"], auth_provider=create_auth_provider()
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_admin_comes_from_the_servers_own_answer(admin_client: httpx.AsyncClient) -> None:
    assert (await _add(admin_client, MEMBER)).status_code == 403
    assert (await _add(admin_client, OWNER)).status_code == 201
    owner = (await admin_client.get("/v1/ae/claude-accounts/shared", headers=OWNER)).json()
    member = (await admin_client.get("/v1/ae/claude-accounts/shared", headers=MEMBER)).json()
    assert (owner["can_manage"], member["can_manage"]) == (True, False)
    assert owner["accounts"] == member["accounts"]
    token = await admin_client.get("/v1/ae/claude-accounts/shared/Team/token", headers=MEMBER)
    assert token.status_code == 200
    assert json.loads(token.text)["token"] == TOKEN
