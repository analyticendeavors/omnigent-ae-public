"""The Admin Center's Secrets section: key names and presence, never values (2026-10-20)."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from ae_omni_policies import auth, secrets_health

PULL_SECRETS = Path(__file__).resolve().parents[3] / "deploy" / "pull-secrets.sh"
SENTINEL = "sentinel-secret-value-7f3a"


def _array(name: str) -> list[str]:
    text = PULL_SECRETS.read_text(encoding="utf-8")
    match = re.search(rf"^{name}=\(([^)]*)\)", text, re.MULTILINE)
    assert match, f"{name}=(...) not found in {PULL_SECRETS}"
    return match.group(1).split()


def test_the_lists_match_pull_secrets() -> None:
    # A key added to the script and not here would never show on the page.
    assert list(secrets_health.REQUIRED) == _array("SERVER_KEYS")
    assert list(secrets_health.OPTIONAL) == _array("SERVER_OPTIONAL_KEYS")


def test_every_key_says_what_it_is_for() -> None:
    for use in [*secrets_health.REQUIRED.values(), *secrets_health.OPTIONAL.values()]:
        assert use.strip()


def test_present_missing_and_empty() -> None:
    env = dict.fromkeys(secrets_health.REQUIRED, SENTINEL)
    env["OMNIGENT_OIDC_COOKIE_SECRET"] = "  "
    env["AE_GITHUB_TOKEN"] = SENTINEL
    got = secrets_health.view(env)
    assert [row.key for row in got.required] == list(secrets_health.REQUIRED)
    assert {row.key: row.present for row in got.required}["POSTGRES_PASSWORD"] is True
    assert got.missing == ["OMNIGENT_OIDC_COOKIE_SECRET"]
    optional = {row.key: row.present for row in got.optional}
    assert optional["AE_GITHUB_TOKEN"] is True
    assert optional["AE_DATABRICKS_CLIENT_ID"] is False
    assert got.optional[0].use == "PR cards, Merge, Deploys release check"


def test_nothing_set_is_all_missing() -> None:
    got = secrets_health.view({})
    assert got.missing == list(secrets_health.REQUIRED)
    assert not any(row.present for row in got.optional)


def test_the_answer_carries_names_only() -> None:
    env = dict.fromkeys([*secrets_health.REQUIRED, *secrets_health.OPTIONAL], SENTINEL)
    body = secrets_health.view(env).model_dump_json()
    assert SENTINEL not in body
    assert str(len(SENTINEL)) not in body
    assert set(secrets_health.SecretKey.model_fields) == {"key", "present", "use"}


async def test_route_reads_the_server_environment(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in [*secrets_health.REQUIRED, *secrets_health.OPTIONAL]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POSTGRES_PASSWORD", SENTINEL)
    monkeypatch.setenv("AE_DATABRICKS_CLIENT_SECRET", SENTINEL)
    response = await client.get("/v1/ae/secrets")
    assert response.status_code == 200, response.text
    assert SENTINEL not in response.text
    body = response.json()
    assert body["missing"] == list(secrets_health.REQUIRED)[1:]
    assert body["required"][0] == {
        "key": "POSTGRES_PASSWORD",
        "present": True,
        "use": "Database",
    }
    optional = {row["key"]: row["present"] for row in body["optional"]}
    want = {key: key == "AE_DATABRICKS_CLIENT_SECRET" for key in secrets_health.OPTIONAL}
    assert optional == want


async def test_route_needs_an_identity(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    assert (await client.get("/v1/ae/secrets")).status_code == 401
    reid = {"X-Forwarded-Email": "reid@example.com"}
    assert (await client.get("/v1/ae/secrets", headers=reid)).status_code == 200
