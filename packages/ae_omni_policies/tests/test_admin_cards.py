"""The Admin Center card registry: catalog, switch, private cards, off routes (2026-11-07)."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from ae_omni_policies import (
    admin_cards,
    attention,
    auth,
    azure_cost,
    cost_targets,
    databricks,
    fabric,
    git_center,
    secrets_health,
)
from ae_omni_policies import router as router_module

WEB = Path(__file__).resolve().parents[2] / "ae_omni_extension" / "web" / "src"
OWNER = {"X-Forwarded-Email": "owner@example.com", "Sec-Fetch-Site": "same-origin"}
MEMBER = {"X-Forwarded-Email": "member@example.com", "Sec-Fetch-Site": "same-origin"}


# ── the catalog against its neighbours ────────────────────────────────────────────────────────


def test_catalog_ids_equal_the_boards_card_ids() -> None:
    text = (WEB / "board.ts").read_text(encoding="utf-8")
    match = re.search(r"export type CardId = ([^;]+);", text)
    assert match is not None
    ids = set(re.findall(r'"([a-z-]+)"', match.group(1)))
    assert {card.id for card in admin_cards.CATALOG} == ids
    assert len(admin_cards.CATALOG) == len(ids) == 12


def test_catalog_groups_match_the_tiles_domains() -> None:
    text = (WEB / "tiles.ts").read_text(encoding="utf-8")
    block = text[text.index("export const DOMAINS") :]
    found = re.findall(r'id: "(\w+)",.*?cards: \[([^\]]*)\]', block, re.S)
    by_group = {
        group: re.findall(r'"([a-z-]+)"', cards)
        for group, cards in found
        if group in {"ship", "box", "cloud", "money"}
    }
    for card in admin_cards.CATALOG:
        assert card.id in by_group[card.group]
    # Order inside each group is the catalog's order.
    for group, ids in by_group.items():
        assert [c.id for c in admin_cards.CATALOG if c.group == group] == ids


def test_sources_are_attention_sources_and_secrets_belongs_to_no_card() -> None:
    owned = [source for card in admin_cards.CATALOG for source in card.sources]
    assert set(owned) <= set(attention.SOURCES)
    assert "secrets" not in owned
    assert len(owned) == len(set(owned))


def test_secret_keys_exist_in_the_secrets_health_lists() -> None:
    known = set(secrets_health.REQUIRED) | set(secrets_health.OPTIONAL)
    for card in admin_cards.CATALOG:
        assert set(card.secret_keys) <= known, card.id
        assert set(card.secret_prefixes) <= set(secrets_health.PER_CAPACITY), card.id


def test_every_field_is_plain_and_endpoints_never_take_a_saved_value() -> None:
    for card in admin_cards.CATALOG:
        assert card.description
        for item in card.fields:
            assert 0 < len(item.hint) <= admin_cards.HINT_MAX
            assert "--" not in item.hint and "—" not in item.hint
            if item.kind in ("url", "secret"):
                assert item.env_only, item.env
    fabric_fields = {
        item.env: item.kind
        for card in admin_cards.CATALOG
        for item in card.fields
        if item.env.startswith("AE_FABRIC_")
    }
    assert fabric_fields["AE_FABRIC_WAKE_URL_"] == "secret"
    assert fabric_fields["AE_FABRIC_SUSPEND_KEY_"] == "secret"


# ── the store ─────────────────────────────────────────────────────────────────────────────────


def test_a_switch_is_remembered_and_the_revision_moves(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    store = admin_cards.CardStore(path)
    snap = store.set_on("git", False, "owner@example.com", 0)
    assert snap["revision"] == 1 and snap["cards"]["git"]["on"] is False
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = admin_cards.CardStore(path).snapshot()
    assert again["revision"] == 1 and again["cards"]["git"]["by"] == "owner@example.com"
    assert json.loads(path.read_text())["version"] == 1


def test_a_stale_revision_conflicts_and_carries_the_current_state() -> None:
    store = admin_cards.CardStore(None)
    store.set_on("git", False, "a@example.com", 0)
    with pytest.raises(admin_cards.CardsConflict) as caught:
        store.set_on("vault", False, "b@example.com", 0)
    assert caught.value.state["revision"] == 1
    assert "vault" not in caught.value.state["cards"]


@pytest.mark.parametrize(
    "text", ["{not json", '{"version": 2}', "[]", '{"version":1,"revision":"x","cards":{}}']
)
def test_an_unreadable_file_is_unknown_and_never_overwritten(tmp_path: Path, text: str) -> None:
    path = tmp_path / "cards.json"
    path.write_text(text, encoding="utf-8")
    store = admin_cards.CardStore(path)
    assert store.snapshot() == {"revision": 0, "state_ok": False, "cards": {}, "settings": {}}
    with pytest.raises(admin_cards.CardsUnreadable):
        store.set_on("git", False, "owner@example.com", 0)
    assert path.read_text(encoding="utf-8") == text


def test_a_failed_write_restores_the_held_state(tmp_path: Path) -> None:
    folder = tmp_path / "ro"
    folder.mkdir()
    store = admin_cards.CardStore(folder / "cards.json")
    store.set_on("git", False, "a@example.com", 0)
    folder.chmod(0o500)
    try:
        if os.access(folder, os.W_OK):
            pytest.skip("running as a user that ignores directory permissions")
        with pytest.raises(admin_cards.CardsSaveError):
            store.set_on("vault", False, "a@example.com", 1)
    finally:
        folder.chmod(0o700)
    snap = store.snapshot()
    assert snap["revision"] == 1 and "vault" not in snap["cards"]


def test_each_switch_logs_one_line_with_the_card_state_and_login(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = admin_cards.CardStore(None)
    with caplog.at_level(logging.INFO, logger=admin_cards.__name__):
        store.set_on("vault", False, "owner@example.com", 0)
    lines = [r.getMessage() for r in caplog.records if r.name == admin_cards.__name__]
    assert lines == ["ae cards: vault turned off by owner@example.com"]


def test_the_default_path_follows_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(admin_cards.STATE_FILE_ENV, str(tmp_path / "x.json"))
    assert admin_cards._default_path() == tmp_path / "x.json"
    monkeypatch.setenv(admin_cards.STATE_FILE_ENV, "")
    assert admin_cards._default_path() is None


# ── private cards ─────────────────────────────────────────────────────────────────────────────


def test_social_exists_only_when_the_server_opts_in(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not admin_cards.exists("social")
    assert not admin_cards.is_on("social")
    assert "social" not in [card.id for card in admin_cards.visible()]
    assert "social" not in [row["id"] for row in admin_cards.view()["cards"]]
    monkeypatch.setenv("AE_PRIVATE_CARDS", "git, social")
    assert admin_cards.exists("social") and admin_cards.is_on("social")
    assert "social" in [row["id"] for row in admin_cards.view()["cards"]]


# ── the routes ────────────────────────────────────────────────────────────────────────────────


async def test_cards_view_is_admin_only_and_returns_no_value(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_AZURE_COST_CLIENT_SECRET", "sentinel-secret-value")
    monkeypatch.setenv("AE_VAULT_NAME", "sentinel-vault-name")
    assert (await client.get("/v1/ae/cards", headers=MEMBER)).status_code == 403
    got = await client.get("/v1/ae/cards", headers=OWNER)
    assert got.status_code == 200, got.text
    assert "sentinel" not in got.text
    body = got.json()
    assert body["state_ok"] is True and body["revision"] == 0
    row = next(c for c in body["cards"] if c["id"] == "azure")
    status = {f["env"]: f["status"] for f in row["fields"]}
    assert status["AE_AZURE_COST_CLIENT_SECRET"] == "set"
    assert status["AE_AZURE_COST_CLIENT_ID"] == "missing"
    assert {"social"}.isdisjoint(c["id"] for c in body["cards"])


async def test_spaces_only_value_counts_as_missing(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_VAULT_NAME", "   ")
    body = (await client.get("/v1/ae/cards", headers=OWNER)).json()
    vault = next(c for c in body["cards"] if c["id"] == "vault")
    assert vault["fields"][0]["status"] == "missing"


async def test_an_admin_in_a_browser_can_switch_a_card_and_a_stale_revision_conflicts(
    client: httpx.AsyncClient, ae_admins: set[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        done = await client.post(
            "/v1/ae/cards/state", json={"id": "vault", "on": False, "revision": 0}, headers=OWNER
        )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["revision"] == 1
    assert next(c for c in body["cards"] if c["id"] == "vault")["on"] is False
    assert (
        next(c for c in body["cards"] if c["id"] == "vault")["changed_by"] == "owner@example.com"
    )
    stale = await client.post(
        "/v1/ae/cards/state", json={"id": "git", "on": False, "revision": 0}, headers=OWNER
    )
    assert stale.status_code == 409, stale.text
    err = stale.json()["error"]
    assert err["code"] == "cards_changed_elsewhere" and err["current"]["revision"] == 1
    lines = [r.getMessage() for r in caplog.records if r.name == admin_cards.__name__]
    assert lines == ["ae cards: vault turned off by owner@example.com"]
    assert "revision" not in " ".join(lines)


async def test_the_switch_refuses_a_non_admin_a_bearer_and_a_page_that_is_not_same_origin(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    body = {"id": "vault", "on": False, "revision": 0}
    refused = [
        await client.post("/v1/ae/cards/state", json=body, headers=MEMBER),
        await client.post(
            "/v1/ae/cards/state", json=body, headers={**OWNER, "Authorization": "Bearer abc"}
        ),
        await client.post(
            "/v1/ae/cards/state",
            json=body,
            headers={"X-Forwarded-Email": "owner@example.com"},
        ),
        await client.post(
            "/v1/ae/cards/state",
            json=body,
            headers={**OWNER, "Sec-Fetch-Site": "cross-site"},
        ),
    ]
    assert [r.status_code for r in refused] == [403, 403, 403, 403], [r.text for r in refused]
    after = (await client.get("/v1/ae/cards", headers=OWNER)).json()
    assert after["revision"] == 0


async def test_a_private_card_is_refused_with_404_unless_opted_in(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    body = {"id": "social", "on": True, "revision": 0}
    assert (await client.post("/v1/ae/cards/state", json=body, headers=OWNER)).status_code == 404
    monkeypatch.setenv("AE_PRIVATE_CARDS", "social")
    assert (await client.post("/v1/ae/cards/state", json=body, headers=OWNER)).status_code == 200
    unknown = {"id": "nope", "on": True, "revision": 1}
    assert (
        await client.post("/v1/ae/cards/state", json=unknown, headers=OWNER)
    ).status_code == 404


async def test_extra_body_fields_are_refused(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    got = await client.post(
        "/v1/ae/cards/state",
        json={"id": "vault", "on": False, "revision": 0, "by": "someone"},
        headers=OWNER,
    )
    assert got.status_code == 422


async def test_an_unreadable_state_file_refuses_the_switch_and_reads_as_defaults(
    client: httpx.AsyncClient, ae_admins: set[str], tmp_path: Path
) -> None:
    path = tmp_path / "cards.json"
    path.write_text("{broken", encoding="utf-8")
    admin_cards.reset_store(admin_cards.CardStore(path))
    seen = (await client.get("/v1/ae/cards", headers=OWNER)).json()
    assert seen["state_ok"] is False and all(c["on"] for c in seen["cards"])
    refused = await client.post(
        "/v1/ae/cards/state", json={"id": "vault", "on": False, "revision": 0}, headers=OWNER
    )
    assert refused.status_code == 503
    assert path.read_text(encoding="utf-8") == "{broken"


# ── require_browser_caller ────────────────────────────────────────────────────────────────────


def _request(headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


SECRET = "test-cookie-secret-0123456789abcdef0123456789"


class _CookieProvider:
    session_cookie_name = "ae_session"
    cookie_secret = SECRET


def _token(**claims: Any) -> str:
    import jwt

    return jwt.encode({"sub": "owner@example.com", **claims}, SECRET, algorithm="HS256")


def _cookies(
    name: str, token: str, secret: str | bytes = SECRET, proof_name: str | None = None
) -> str:
    """A Cookie header with the session token and the browser proof a sign-in would set."""
    from ae_omni_policies import browser_proof

    proof = browser_proof.expected_proof(browser_proof.derive_key(secret), token)
    proof_name = proof_name or browser_proof.proof_cookie_name(name.startswith("__Host-"))
    return f"{name}={token}; {proof_name}={proof}"


async def test_a_cookie_caller_passes_and_a_cookieless_multi_user_caller_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def who(_request: Request) -> str:
        return "owner@example.com"

    monkeypatch.setattr(auth, "require_ae_user", who)
    auth.reset_provider_cache(_CookieProvider())  # type: ignore[arg-type]
    page = {"Sec-Fetch-Site": "same-origin", "Cookie": _cookies("ae_session", _token())}
    assert await auth.require_browser_caller(_request(page)) == "owner@example.com"
    for bad in (
        {"Sec-Fetch-Site": "same-origin"},
        {**page, "Authorization": "Bearer x"},
        {**page, "Authorization": ""},
        {**page, "Sec-Fetch-Site": "same-site"},
        {"Cookie": _cookies("ae_session", _token())},
        {**page, "Cookie": "ae_session=not-a-jwt"},
        # The session cookie alone, with no proof: a CLI token replayed as a cookie.
        {"Sec-Fetch-Site": "same-origin", "Cookie": f"ae_session={_token()}"},
    ):
        with pytest.raises(HTTPException) as caught:
            await auth.require_browser_caller(_request(bad))
        assert caught.value.status_code == 403


async def test_a_single_user_runtime_needs_no_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    async def who(_request: Request) -> str:
        return "local"

    monkeypatch.setattr(auth, "require_ae_user", who)
    got = await auth.require_browser_caller(_request({"Sec-Fetch-Site": "same-origin"}))
    assert got == "local"


# ── off routes make no outside call ───────────────────────────────────────────────────────────


def _forbid(name: str) -> Any:
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError(f"{name} was called while its card is off")

    return boom


async def _switch_off(client: httpx.AsyncClient, *ids: str) -> None:
    revision = 0
    for card in ids:
        done = await client.post(
            "/v1/ae/cards/state",
            json={"id": card, "on": False, "revision": revision},
            headers=OWNER,
        )
        assert done.status_code == 200, done.text
        revision = done.json()["revision"]


async def test_each_off_route_answers_off_and_calls_nothing(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_PRIVATE_CARDS", "social")
    monkeypatch.setattr(git_center, "overview", _forbid("git_center.overview"))
    monkeypatch.setattr(azure_cost, "overview", _forbid("azure_cost.overview"))
    monkeypatch.setattr(fabric, "overview", _forbid("fabric.overview"))
    monkeypatch.setattr(databricks, "overview", _forbid("databricks.overview"))
    monkeypatch.setattr(router_module, "_vault_inventory", _forbid("_vault_inventory"))
    monkeypatch.setattr(router_module, "_social_read", _forbid("_social_read"))
    monkeypatch.setattr(cost_targets, "load_rules", lambda *a, **k: pytest.fail("load_rules"))
    await _switch_off(client, "git", "azure", "compute", "credit", "vault", "social")
    paths = {
        "/v1/ae/git": "git",
        "/v1/ae/azure-cost": "azure",
        "/v1/ae/azure-cost/targets": "azure",
        "/v1/ae/fabric": "compute",
        "/v1/ae/databricks": "compute",
        "/v1/ae/vault": "vault",
        "/v1/ae/vault/rows?vault=kv-example-box": "vault",
        "/v1/ae/vault/people": "vault",
        "/v1/ae/social": "social",
        "/v1/ae/social/week": "social",
    }
    for path, card in paths.items():
        got = await client.get(path, headers=OWNER)
        assert got.status_code == 200, (path, got.text)
        assert got.json() == {"off": True, "card": card}, path
    # A non-admin still gets the admin refusal, not the off answer.
    assert (await client.get("/v1/ae/git", headers=MEMBER)).status_code == 403


async def test_databricks_stays_up_while_either_of_its_cards_is_on(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def overview() -> dict[str, Any]:
        return {"warehouses": []}

    monkeypatch.setattr(databricks, "overview", overview)
    await _switch_off(client, "compute")
    got = await client.get("/v1/ae/databricks", headers=OWNER)
    assert got.json().get("off") is None and got.json()["warehouses"] == []


async def test_routes_hosts_and_sessions_depend_on_keep_answering_when_off(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    await _switch_off(client, "deploys", "box", "accounts", "presets", "decision")
    got = await client.get("/v1/ae/deploy/hold", headers=OWNER)
    assert got.status_code == 200 and got.json().get("off") is None


# ── Needs you and Box health follow the switch ────────────────────────────────────────────────


async def test_attention_skips_an_off_cards_source_and_never_skips_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    called: list[str] = []

    def reader(source: str) -> Any:
        async def read(_now: Any, _login: Any, **_k: Any) -> attention.Reading:
            called.append(source)
            return attention.Reading(source)

        return read

    monkeypatch.setattr(attention, "_READERS", {s: reader(s) for s in attention.SOURCES})
    admin_cards.reset_store(admin_cards.CardStore(None))
    admin_cards.store().set_on("git", False, "owner@example.com", 0)
    admin_cards.store().set_on("box", False, "owner@example.com", 1)
    await attention.read_sources(attention.SOURCES, datetime.now(UTC))
    assert "git" not in called and "box" not in called and "host" not in called
    assert "secrets" in called and "vault" in called


async def test_the_cleanup_starts_no_git_refresh_while_git_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def due() -> bool:
        raise AssertionError("refresh_due was asked while Git is off")

    monkeypatch.setattr(git_center, "refresh_due", due)
    monkeypatch.setattr(attention, "_last_kick", None)
    seen: list[bool] = []

    async def fake_read(
        sources: Any, now: Any, *, kick: bool = False, login: Any = None
    ) -> list[Any]:
        seen.append(kick)
        return []

    monkeypatch.setattr(attention, "read_sources", fake_read)
    admin_cards.store().set_on("git", False, "owner@example.com", 0)
    await attention.tidy_once()
    assert seen == [False]


def _view(env: dict[str, str]) -> secrets_health.SecretsView:
    return secrets_health.view(env, auth_source="header")


def test_off_drops_only_the_missing_rows_of_keys_no_live_card_uses() -> None:
    before = _view({})
    assert "AE_AZURE_COST_CLIENT_ID" in [r.key for r in before.optional]
    admin_cards.store().set_on("azure", False, "owner@example.com", 0)
    after = _view({})
    keys = [r.key for r in after.optional]
    assert not {
        "AE_AZURE_COST_TENANT_ID",
        "AE_AZURE_COST_CLIENT_ID",
        "AE_AZURE_COST_CLIENT_SECRET",
    } & set(keys)
    # A key the Azure card does not own stays.
    assert "AE_DECISION_API_KEY" in keys


def test_a_present_key_keeps_its_row_when_its_card_is_off() -> None:
    admin_cards.store().set_on("azure", False, "owner@example.com", 0)
    env = {"AE_AZURE_COST_CLIENT_SECRET": "sentinel-value"}
    rows = {r.key: r for r in _view(env).optional}
    assert rows["AE_AZURE_COST_CLIENT_SECRET"].present is True
    assert "AE_AZURE_COST_CLIENT_ID" not in rows
    assert "sentinel-value" not in _view(env).model_dump_json()


def test_a_key_two_cards_share_is_dropped_only_when_both_are_off() -> None:
    store = admin_cards.store()
    store.set_on("git", False, "owner@example.com", 0)
    assert "AE_GITHUB_TOKEN" in [r.key for r in _view({}).optional]
    store.set_on("deploys", False, "owner@example.com", 1)
    assert "AE_GITHUB_TOKEN" not in [r.key for r in _view({}).optional]


async def test_a_machine_token_in_the_cookie_slot_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent can put its device-login token in the cookie and add the header itself."""

    async def who(_request: Request) -> str:
        return "owner@example.com"

    monkeypatch.setattr(auth, "require_ae_user", who)
    auth.reset_provider_cache(_CookieProvider())  # type: ignore[arg-type]
    for claims in ({"grant_id": "g1"}, {"scope": "x"}, {"grant_id": "g1", "scope": "x"}):
        headers = {
            "Sec-Fetch-Site": "same-origin",
            "Cookie": _cookies("ae_session", _token(**claims)),
        }
        with pytest.raises(HTTPException) as caught:
            await auth.require_browser_caller(_request(headers))
        assert caught.value.status_code == 403
    plain = {
        "Sec-Fetch-Site": "same-origin",
        "Cookie": _cookies("ae_session", _token(account_generation="1")),
    }
    assert await auth.require_browser_caller(_request(plain)) == "owner@example.com"


async def test_a_browser_session_on_a_real_oidc_server_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built from upstream's own OIDC config and cookie minting, whose secret is bytes.

    Why (2026-11-07): the gate read only a text secret, so on the live Entra server it found
    none and refused every browser, Reid's iPhone first, as "a machine token". The tests above
    used a text secret and passed.
    """
    import dataclasses

    import jwt
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
    # Any other required field upstream adds later gets an empty value (False, or None).
    for field in dataclasses.fields(OIDCConfig):
        if field.name in values or field.default is not dataclasses.MISSING:
            continue
        if field.default_factory is dataclasses.MISSING:
            values[field.name] = False if field.type in (bool, "bool") else None
    config = OIDCConfig(**values)
    prov = UnifiedAuthProvider("oidc", oidc_config=config, local_single_user=False)

    async def who(_request: Request) -> str:
        return "owner@example.com"

    monkeypatch.setattr(auth, "require_ae_user", who)
    auth.reset_provider_cache(prov)
    name = prov.session_cookie_name
    assert name
    session = mint_session_cookie(
        user_id="owner@example.com", cookie_secret=secret, ttl_hours=1, provider="oidc"
    )
    page = {"Sec-Fetch-Site": "same-origin", "Cookie": _cookies(name, session, secret)}
    # Upstream's own sign-in check accepts this cookie, and so must the gate.
    assert prov._check_cookie(_request(page)) == "owner@example.com"
    assert await auth.require_browser_caller(_request(page)) == "owner@example.com"
    for value in (
        jwt.encode({"sub": "owner@example.com", "grant_id": "g1"}, secret, algorithm="HS256"),
        jwt.encode(
            {"sub": "owner@example.com", "scope": "sessions:read"}, secret, algorithm="HS256"
        ),
        mint_session_cookie(
            user_id="owner@example.com", cookie_secret=b"x" * 32, ttl_hours=1, provider="oidc"
        ),
    ):
        with pytest.raises(HTTPException) as caught:
            await auth.require_browser_caller(
                _request(
                    {"Sec-Fetch-Site": "same-origin", "Cookie": _cookies(name, value, secret)}
                )
            )
        assert caught.value.status_code == 403
    with pytest.raises(HTTPException):
        await auth.require_browser_caller(_request({**page, "Authorization": "Bearer x"}))


def test_one_snapshot_builds_the_whole_answer() -> None:
    store = admin_cards.store()
    store.set_on("git", False, "a@example.com", 0)
    old = store.snapshot()
    store.set_on("vault", False, "a@example.com", 1)
    # A switch landed after `old` was taken: the answer still agrees with `old` alone.
    answer = admin_cards.view(snap=old)
    assert answer["revision"] == 1
    on = {row["id"]: row["on"] for row in answer["cards"]}
    assert on["git"] is False and on["vault"] is True
    assert admin_cards.view()["revision"] == 2


async def test_the_switch_answer_carries_the_writes_own_revision(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = admin_cards.CardStore.set_on

    def set_then_race(self: Any, *a: Any, **k: Any) -> Any:
        snap = real(self, *a, **k)
        real(self, "git", False, "other@example.com", snap["revision"])  # a second admin, at once
        return snap

    monkeypatch.setattr(admin_cards.CardStore, "set_on", set_then_race)
    done = await client.post(
        "/v1/ae/cards/state", json={"id": "vault", "on": False, "revision": 0}, headers=OWNER
    )
    body = done.json()
    assert body["revision"] == 1
    on = {row["id"]: row["on"] for row in body["cards"]}
    assert on["vault"] is False and on["git"] is True  # the write's own snapshot, not a mix


def test_a_damaged_file_is_retried_at_most_every_30_seconds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "cards.json"
    path.write_text("{broken", encoding="utf-8")
    store = admin_cards.CardStore(path)
    clock = [1000.0]
    monkeypatch.setattr(admin_cards.time, "monotonic", lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger=admin_cards.__name__):
        for _ in range(11):
            assert store.snapshot()["state_ok"] is False
    warned = [r for r in caplog.records if r.name == admin_cards.__name__]
    assert len(warned) == 1
    clock[0] += 31
    store.snapshot()
    assert len([r for r in caplog.records if r.name == admin_cards.__name__]) == 2
    # Fixing the file is picked up as soon as its mtime changes, without waiting.
    path.write_text('{"version":1,"revision":3,"cards":{}}', encoding="utf-8")
    os.utime(path, (1, 1))
    assert store.snapshot() == {"revision": 3, "state_ok": True, "cards": {}, "settings": {}}


def test_cookie_secret_is_read_from_the_oidc_or_accounts_config() -> None:
    from types import SimpleNamespace

    oidc = SimpleNamespace(
        _source="oidc", _oidc_config=SimpleNamespace(cookie_secret="o" * 40), _accounts_config=None
    )
    accounts = SimpleNamespace(
        _source="accounts",
        _oidc_config=None,
        _accounts_config=SimpleNamespace(cookie_secret="a" * 40),
    )
    assert auth._cookie_secret(oidc) == "o" * 40
    assert auth._cookie_secret(accounts) == "a" * 40
    # Upstream's configs hold it as bytes (hex-decoded); that form must be read too.
    oidc_bytes = SimpleNamespace(
        _source="oidc",
        _oidc_config=SimpleNamespace(cookie_secret=b"o" * 32),
        _accounts_config=None,
    )
    assert auth._cookie_secret(oidc_bytes) == b"o" * 32
    assert auth._cookie_secret(SimpleNamespace(cookie_secret=b"d" * 32)) == b"d" * 32
    assert (
        auth._cookie_secret(
            SimpleNamespace(_source="oidc", _oidc_config=SimpleNamespace(cookie_secret=b""))
        )
        is None
    )
    assert auth._cookie_secret(SimpleNamespace(_source="oidc", _oidc_config=None)) is None
    assert auth._cookie_secret(SimpleNamespace()) is None


def test_a_bad_key_is_a_refusal_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    import jwt

    def bad_key(*_a: Any, **_k: Any) -> Any:
        raise jwt.InvalidKeyError("bad key")

    monkeypatch.setattr(jwt, "decode", bad_key)
    assert auth._machine_token(_token(), SECRET) is True
    monkeypatch.undo()
    assert auth._machine_token("not-a-jwt", SECRET) is True


def test_every_card_row_names_its_sources() -> None:
    rows = {row["id"]: row for row in admin_cards.view()["cards"]}
    assert rows["git"]["sources"] == ["git", "git-mine"]
    assert rows["box"]["sources"] == ["box", "host"]
    assert rows["deploys"]["sources"] == []
    for card in admin_cards.visible():
        assert rows[card.id]["sources"] == list(card.sources)


async def test_box_health_off_reaches_a_box_admin_through_access_and_needs_you(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_BOX_ADMINS", "member@example.com")
    member = {"X-Forwarded-Email": "member@example.com"}
    before = (await client.get("/v1/ae/attention", headers=member)).json()
    assert before["sources"] == ["box"]
    admin_cards.store().set_on("box", False, "owner@example.com", 0)
    access = (await client.get("/v1/ae/access", headers=member)).json()
    assert access == {"admin": False, "box_admin": True, "box_on": False}
    after = (await client.get("/v1/ae/attention", headers=member)).json()
    assert after["sources"] == [] and after["items"] == []
    # A boolean only: nothing about who turned it off.
    assert set(access) == {"admin", "box_admin", "box_on"}
