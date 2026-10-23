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
    # Per-capacity keys show from the environment instead (test below).
    script = _array("SERVER_OPTIONAL_KEYS")
    fixed = [key for key in script if not key.startswith(secrets_health.PER_CAPACITY)]
    assert list(secrets_health.OPTIONAL) == fixed
    assert fixed != script, "pull-secrets.sh no longer fetches a per-capacity pair"


def test_every_key_says_what_it_is_for() -> None:
    for use in [*secrets_health.REQUIRED.values(), *secrets_health.OPTIONAL.values()]:
        assert use.strip()


# A task-store URL plus OIDC: every REQUIRED key required (AE's box).
ALL_ON = {"AE_TASK_STORE_URL": "https://tasks.example"}
OIDC = "oidc"


def test_conditional_keys_are_required_keys() -> None:
    # A key renamed in REQUIRED but not here would quietly turn required again.
    conditional = [key for keys in secrets_health.CONDITIONAL.values() for key in keys]
    assert set(conditional) <= set(secrets_health.REQUIRED)


def test_present_missing_and_empty() -> None:
    env = dict.fromkeys(secrets_health.REQUIRED, SENTINEL) | ALL_ON
    env["OMNIGENT_OIDC_COOKIE_SECRET"] = "  "
    env["AE_GITHUB_TOKEN"] = SENTINEL
    got = secrets_health.view(env, auth_source=OIDC)
    assert [row.key for row in got.required] == list(secrets_health.REQUIRED)
    assert {row.key: row.present for row in got.required}["POSTGRES_PASSWORD"] is True
    assert got.missing == ["OMNIGENT_OIDC_COOKIE_SECRET"]
    optional = {row.key: row.present for row in got.optional}
    assert optional["AE_GITHUB_TOKEN"] is True
    assert optional["AE_DATABRICKS_CLIENT_ID"] is False
    assert got.optional[0].use == "PR cards, Merge, Deploys release check"


def test_every_feature_on_and_no_secret_is_all_missing() -> None:
    got = secrets_health.view(ALL_ON, auth_source=OIDC)
    assert got.missing == list(secrets_health.REQUIRED)
    assert not any(row.present for row in got.optional)


def test_an_empty_environment_lacks_only_the_database() -> None:
    got = secrets_health.view({}, auth_source="accounts")
    assert got.missing == ["POSTGRES_PASSWORD"]
    assert not any(row.present for row in got.optional)


def test_accounts_mode_without_the_task_store_needs_only_the_database() -> None:
    # The Quick start's server (2026-10-22): no OIDC, no task-store URL.
    got = secrets_health.view({"POSTGRES_PASSWORD": SENTINEL}, auth_source="accounts")
    assert [row.key for row in got.required] == ["POSTGRES_PASSWORD"]
    assert got.missing == []
    optional = [row.key for row in got.optional]
    assert optional[: len(secrets_health.OPTIONAL)] == list(secrets_health.OPTIONAL)
    assert set(optional[len(secrets_health.OPTIONAL) :]) == {
        key for keys in secrets_health.CONDITIONAL.values() for key in keys
    }


OIDC_PAIR = {"OMNIGENT_OIDC_CLIENT_SECRET", "OMNIGENT_OIDC_COOKIE_SECRET"}
CF_PAIR = {"AE_CF_ACCESS_CLIENT_ID", "AE_CF_ACCESS_CLIENT_SECRET"}


@pytest.mark.parametrize(
    ("process_env", "env", "required"),
    [
        ({"OMNIGENT_AUTH_PROVIDER": "OIDC"}, {}, OIDC_PAIR),
        (
            {"OMNIGENT_AUTH_ENABLED": "1", "OMNIGENT_OIDC_ISSUER": "https://login.example"},
            {},
            OIDC_PAIR,
        ),
        # An explicit provider wins over a leftover issuer, and with auth off the
        # issuer means nothing (upstream's resolve_auth_source).
        (
            {
                "OMNIGENT_AUTH_PROVIDER": "accounts",
                "OMNIGENT_OIDC_ISSUER": "https://login.example",
            },
            {},
            set(),
        ),
        (
            {"OMNIGENT_AUTH_ENABLED": "0", "OMNIGENT_OIDC_ISSUER": "https://login.example"},
            {},
            set(),
        ),
        ({}, {"AE_TASK_STORE_URL": "https://tasks.example"}, CF_PAIR),
        ({}, {"AE_TASK_STORE_URL": ""}, set()),
    ],
)
def test_each_pair_is_required_only_when_the_server_uses_it(
    monkeypatch: pytest.MonkeyPatch,
    process_env: dict[str, str],
    env: dict[str, str],
    required: set[str],
) -> None:
    for key in ("OMNIGENT_AUTH_PROVIDER", "OMNIGENT_AUTH_ENABLED", "OMNIGENT_OIDC_ISSUER"):
        monkeypatch.delenv(key, raising=False)
    for key, value in process_env.items():
        monkeypatch.setenv(key, value)
    got = secrets_health.view(env)
    assert set(got.missing) == {"POSTGRES_PASSWORD", *required}


def test_each_fabric_capacity_gets_its_own_rows() -> None:
    env = {
        "AE_FABRIC_WAKE_URL_LAB": SENTINEL,
        "AE_FABRIC_WAKE_URL_PROD": SENTINEL,
        "AE_FABRIC_SUSPEND_KEY_PROD": SENTINEL,
        "AE_FABRIC_SUSPEND_KEY_ORPHAN": SENTINEL,
    }
    got = secrets_health.view(env, auth_source="accounts")
    rows = [(row.key, row.present, row.use) for row in got.optional]
    assert rows[len(secrets_health.OPTIONAL) : len(secrets_health.OPTIONAL) + 4] == [
        ("AE_FABRIC_WAKE_URL_LAB", True, "Fabric Wake, lab"),
        ("AE_FABRIC_SUSPEND_KEY_LAB", False, "Fabric Off, lab"),
        ("AE_FABRIC_WAKE_URL_PROD", True, "Fabric Wake, prod"),
        ("AE_FABRIC_SUSPEND_KEY_PROD", True, "Fabric Off, prod"),
    ]
    orphan = ("AE_FABRIC_SUSPEND_KEY_ORPHAN", True, "Fabric Off, orphan, no wake URL beside it")
    assert orphan in rows


def test_no_capacity_means_no_fabric_rows() -> None:
    got = secrets_health.view({}, auth_source="accounts")
    assert not [row for row in got.optional if row.key.startswith(secrets_health.PER_CAPACITY)]


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
    for key in ("OMNIGENT_AUTH_PROVIDER", "OMNIGENT_OIDC_ISSUER"):
        monkeypatch.delenv(key, raising=False)
    # The task store on, OIDC off (the test server is not in OIDC mode).
    for key, value in ALL_ON.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("POSTGRES_PASSWORD", SENTINEL)
    monkeypatch.setenv("AE_DATABRICKS_CLIENT_SECRET", SENTINEL)
    response = await client.get("/v1/ae/secrets")
    assert response.status_code == 200, response.text
    assert SENTINEL not in response.text
    body = response.json()
    assert body["missing"] == ["AE_CF_ACCESS_CLIENT_ID", "AE_CF_ACCESS_CLIENT_SECRET"]
    assert body["required"][0] == {
        "key": "POSTGRES_PASSWORD",
        "present": True,
        "use": "Database",
    }
    optional = {row["key"]: row["present"] for row in body["optional"]}
    want = {key: key == "AE_DATABRICKS_CLIENT_SECRET" for key in secrets_health.OPTIONAL}
    want |= dict.fromkeys(secrets_health.CONDITIONAL["oidc"], False)
    assert optional == want


async def test_route_needs_an_identity(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    assert (await client.get("/v1/ae/secrets")).status_code == 401
    reid = {"X-Forwarded-Email": "reid@example.com"}
    assert (await client.get("/v1/ae/secrets", headers=reid)).status_code == 200
