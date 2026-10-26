"""Secret expiry on the Server secrets list: the box's vault reading, no values (2026-10-23)."""

from __future__ import annotations

import io
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from ae_omni_policies import omni, secrets_health, secrets_meta, secrets_relay

PULL_SECRETS = Path(__file__).resolve().parents[3] / "deploy" / "pull-secrets.sh"
SENTINEL = "sentinel-secret-value-7f3a"
REID = {"X-Forwarded-Email": "reid@example.com"}
STEVE = {"X-Forwarded-Email": "steve@example.com"}
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _script_names() -> dict[str, str]:
    """Env key to vault name, as pull-secrets.sh fetches it (defaults of its KV_* variables)."""
    text = PULL_SECRETS.read_text(encoding="utf-8")
    defaults = dict(re.findall(r'^(KV_\w+)="\$\{\1:-([^}]+)\}"', text, re.MULTILINE))
    fetched = re.findall(r'^\s*fetch(?:_optional)? (\w+) "\$(KV_\w+)"', text, re.MULTILINE)
    assert fetched, "no fetch lines found in pull-secrets.sh"
    return {key: defaults[var] for key, var in fetched if key != "CLAUDE_CODE_OAUTH_TOKEN"}


def test_vault_names_match_pull_secrets() -> None:
    # A secret renamed in the script and not here would show "unknown" forever.
    assert _script_names() == secrets_meta.VAULT_NAMES


def test_every_fixed_server_key_has_a_vault_name() -> None:
    fixed = [*secrets_health.REQUIRED, *secrets_health.OPTIONAL]
    assert [key for key in fixed if key not in secrets_meta.VAULT_NAMES] == []


def _reading(**expires: datetime | None) -> secrets_meta.Reading:
    return secrets_meta.Reading(
        read_at=NOW,
        secrets={
            name: secrets_meta.SecretMeta(name=name, expires=when, updated=NOW - timedelta(days=3))
            for name, when in expires.items()
        },
    )


@pytest.mark.parametrize(
    ("expires", "state", "days"),
    [
        (NOW + timedelta(days=730), "ok", 730),
        (NOW + timedelta(days=59, hours=23), "soon", 59),
        (NOW + timedelta(days=60), "ok", 60),
        (NOW - timedelta(hours=1), "expired", -1),
        (NOW - timedelta(days=4), "expired", -5),
        (None, "none", None),
    ],
)
def test_expiry_states(expires: datetime | None, state: str, days: int | None) -> None:
    got = secrets_meta.words(
        "AE_GITHUB_TOKEN", _reading(**{"omnigent-ae-github-token": expires}), NOW
    )
    assert got.expiry_state == state
    assert got.days_left == days
    assert got.enabled is True
    assert got.updated is not None


def test_a_secret_the_vault_did_not_list_is_unknown_and_a_key_without_a_vault_name_is_blank() -> (
    None
):
    assert secrets_meta.words("AE_GITHUB_TOKEN", _reading(), NOW).expiry_state == "unknown"
    assert secrets_meta.words("AE_GITHUB_TOKEN", None, NOW).expiry_state == "unknown"
    assert secrets_meta.words("AE_FABRIC_WAKE_URL_OTHER", _reading(), NOW) == secrets_meta.Words()


def test_links_name_the_account_and_default_to_the_vault_page() -> None:
    dbx = secrets_meta.words("AE_DATABRICKS_CLIENT_SECRET", None, NOW)
    assert (
        dbx.rotate_url == "https://accounts.cloud.databricks.com/user-management/serviceprincipals"
    )
    assert "136eebed-4c6e-4d6d-98f9-be90875db944" in (dbx.rotate_hint or "")
    plain = secrets_meta.words("POSTGRES_PASSWORD", None, NOW)
    assert plain.edit_url == plain.rotate_url
    assert plain.edit_url.endswith("/secrets/omnigent-ae-postgres-password")
    assert plain.edit_url.startswith("https://portal.azure.com/")
    assert plain.rotate_hint == secrets_meta.DEFAULT_HINT
    for vault_name in secrets_meta.ROTATE:
        assert vault_name in secrets_meta.KNOWN_NAMES


def test_stale_means_old_or_never() -> None:
    assert secrets_meta.stale(None, NOW)
    assert not secrets_meta.stale(_reading(), NOW + timedelta(hours=35))
    assert secrets_meta.stale(_reading(), NOW + timedelta(hours=37))


def test_the_store_keeps_every_name_and_survives_a_bad_file(tmp_path: Path) -> None:
    # Every name since 2026-10-23 (the Key Vault card lists them all).
    path = tmp_path / "sub" / "meta.json"
    store = secrets_meta.SecretsMetaStore(path)
    assert store.read() is None
    post = secrets_meta.MetaPost(
        secrets=[
            secrets_meta.SecretMeta(name="omnigent-ae-github-token", expires=NOW),
            secrets_meta.SecretMeta(name="some-client-secret-we-never-map"),
        ]
    )
    assert store.put(post, now=NOW) == 2
    again = secrets_meta.SecretsMetaStore(path).read()
    assert again is not None
    assert sorted(again.secrets) == ["omnigent-ae-github-token", "some-client-secret-we-never-map"]
    path.write_text("{not json", encoding="utf-8")
    assert store.read() is None


def test_an_empty_file_setting_keeps_the_reading_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(secrets_meta.STATE_FILE_ENV, "")
    secrets_meta.reset_store()
    assert secrets_meta.store().put(secrets_meta.MetaPost(secrets=[]), now=NOW) == 0
    assert secrets_meta.store().read() is not None


def test_the_view_merges_the_reading_and_carries_no_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = secrets_meta.SecretsMetaStore(tmp_path / "meta.json")
    soon = datetime.now(UTC) + timedelta(days=10)
    store.put(
        secrets_meta.MetaPost(
            secrets=[secrets_meta.SecretMeta(name="omnigent-ae-github-token", expires=soon)]
        )
    )
    secrets_meta.reset_store(store)
    env = dict.fromkeys([*secrets_health.REQUIRED, *secrets_health.OPTIONAL], SENTINEL)
    view = secrets_health.view(env, auth_source="oidc")
    row = next(r for r in view.optional if r.key == "AE_GITHUB_TOKEN")
    assert row.expiry_state == "soon"
    assert row.days_left == 9
    assert view.meta_stale is False
    assert view.meta_read_at is not None
    assert SENTINEL not in view.model_dump_json()


async def test_post_is_admin_only_and_checks_the_body(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    body = {"secrets": [{"name": "omnigent-ae-github-token", "expires": NOW.isoformat()}]}
    assert (await client.post("/v1/ae/secrets/meta", json=body, headers=STEVE)).status_code == 403
    ok = await client.post("/v1/ae/secrets/meta", json=body, headers=REID)
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"kept": 1}
    bad = await client.post("/v1/ae/secrets/meta", json={"secrets": "nope"}, headers=REID)
    assert bad.status_code == 422
    seen = await client.get("/v1/ae/secrets", headers=REID)
    row = next(r for r in seen.json()["optional"] if r["key"] == "AE_GITHUB_TOKEN")
    assert row["expires"] is not None


# ── the relay ───────────────────────────────────────────────────────────────

AZ_ANSWER = json.dumps(
    [
        {
            "name": "omnigent-ae-github-token",
            "enabled": True,
            "expires": "2028-10-23T00:00:00+00:00",
            "updated": "2026-10-22T05:02:08+00:00",
            "value": SENTINEL,
        },
        {"name": "unrelated-secret", "enabled": True, "expires": None, "updated": None},
    ]
)


def _runner(answer: str, seen: list[Sequence[str]] | None = None):
    def run(argv: Sequence[str]) -> str:
        if seen is not None:
            seen.append(list(argv))
        return answer

    return run


def test_the_relay_lists_never_shows_and_keeps_every_name() -> None:
    seen: list[Sequence[str]] = []
    body = secrets_relay.read_vault("test-vault", az_bin="az", runner=_runner(AZ_ANSWER, seen))
    assert [item.name for item in body.secrets] == ["omnigent-ae-github-token", "unrelated-secret"]
    assert SENTINEL not in body.model_dump_json()
    assert seen[0][1:4] == ["keyvault", "secret", "list"]
    assert "show" not in seen[0]
    assert "value" not in secrets_relay.QUERY


@pytest.mark.parametrize("answer", ["not json", '{"a": 1}'])
def test_the_relay_refuses_an_answer_that_is_not_the_list(answer: str) -> None:
    with pytest.raises(secrets_relay.RelayError):
        secrets_relay.read_vault("test-vault", az_bin="az", runner=_runner(answer))


async def test_dry_run_prints_and_posts_nothing() -> None:
    out = io.StringIO()
    code = await secrets_relay.run(
        vault="test-vault",
        az_bin="az",
        settings=omni.Settings(),
        dry_run=True,
        runner=_runner(AZ_ANSWER),
        out=out,
    )
    assert code == 0
    assert "omnigent-ae-github-token" in out.getvalue()
    assert SENTINEL not in out.getvalue()


async def test_the_relay_posts_the_body_and_reports_what_the_server_kept() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"kept": 1})

    out = io.StringIO()
    code = await secrets_relay.run(
        vault="test-vault",
        az_bin="az",
        settings=omni.Settings(server_url="https://omni.test", token="t"),
        runner=_runner(AZ_ANSWER),
        transport=httpx.MockTransport(handler),
        out=out,
    )
    assert code == 0, out.getvalue()
    assert calls[0].url.path == "/v1/ae/secrets/meta"
    assert SENTINEL not in calls[0].content.decode()
    assert "the server now holds 1 across all vaults" in out.getvalue()


async def test_the_relay_says_what_to_do_without_az_or_a_server() -> None:
    out = io.StringIO()
    assert (
        await secrets_relay.run(vault="test-vault", az_bin=None, settings=omni.Settings(), out=out)
        == 1
    )
    assert "az login" in out.getvalue()
    out = io.StringIO()
    code = await secrets_relay.run(
        vault="test-vault",
        az_bin="az",
        settings=omni.Settings(),
        runner=_runner(AZ_ANSWER),
        out=out,
    )
    assert code == 1
    assert out.getvalue().strip()


async def test_post_checks_the_origin_like_limits(client: httpx.AsyncClient) -> None:
    body = {"secrets": []}
    foreign = await client.post(
        "/v1/ae/secrets/meta", json=body, headers={"Origin": "https://evil.example"}
    )
    relay = await client.post("/v1/ae/secrets/meta", json=body)
    assert foreign.status_code == 403, foreign.text
    assert relay.status_code == 200, relay.text


def test_links_follow_the_vault_the_relay_read() -> None:
    reading = secrets_meta.Reading(read_at=NOW, vault="other-vault", secrets={})
    got = secrets_meta.words("POSTGRES_PASSWORD", reading, NOW)
    assert (
        f"other-vault{secrets_meta.VAULT_HOST_SUFFIX}/secrets/omnigent-ae-postgres-password"
        in (got.edit_url or "")
    )
    assert secrets_meta.MetaPost(secrets=[]).vault == secrets_meta.VAULT
    with pytest.raises(ValueError):
        secrets_meta.MetaPost(vault="x.evil.example/", secrets=[])
    body = secrets_relay.read_vault("other-vault", az_bin="az", runner=_runner(AZ_ANSWER))
    assert body.vault == "other-vault"


async def test_an_extreme_date_is_a_422_not_a_500(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    for when in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00", "1900-01-01T00:00:00Z"):
        body = {"secrets": [{"name": "omnigent-ae-github-token", "expires": when}]}
        got = await client.post("/v1/ae/secrets/meta", json=body, headers=REID)
        assert got.status_code == 422, (when, got.text)


async def test_a_naive_date_in_the_state_file_reads_as_utc(
    tmp_path: Path, client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    path = tmp_path / "meta.json"
    path.write_text(
        json.dumps(
            {
                "read_at": "2026-10-23T12:00:00",
                "secrets": {
                    "omnigent-ae-github-token": {
                        "name": "omnigent-ae-github-token",
                        "expires": "2030-01-01T00:00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    secrets_meta.reset_store(secrets_meta.SecretsMetaStore(path))
    got = await client.get("/v1/ae/secrets", headers=REID)
    assert got.status_code == 200, got.text
    row = next(r for r in got.json()["optional"] if r["key"] == "AE_GITHUB_TOKEN")
    assert row["expiry_state"] == "ok"


def test_nothing_mapped_in_the_vault_is_refused() -> None:
    other = json.dumps([{"name": "unrelated", "enabled": True, "expires": None, "updated": None}])
    with pytest.raises(secrets_relay.RelayError, match="nothing was posted"):
        secrets_relay.read_vault("test-vault", az_bin="az", runner=_runner(other))
    with pytest.raises(secrets_relay.RelayError):
        secrets_relay.read_vault("test-vault", az_bin="az", runner=_runner("[]"))


async def test_the_relay_exits_nonzero_and_posts_nothing_for_an_empty_match() -> None:
    posted: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        return httpx.Response(200, json={"kept": 0})

    out = io.StringIO()
    code = await secrets_relay.run(
        vault="test-vault",
        az_bin="az",
        settings=omni.Settings(server_url="https://omni.test", token="t"),
        runner=_runner("[]"),
        transport=httpx.MockTransport(handler),
        out=out,
    )
    assert code == 1
    assert posted == []
