"""The Key Vault card, source expiry and last worked (2026-10-23): names and dates, no values."""

from __future__ import annotations

import contextlib
import io
import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import (
    fabric,
    github,
    key_health,
    omni,
    secrets_health,
    secrets_meta,
    secrets_relay,
    session_cookie,
    vault_inventory,
)

SENTINEL = "sentinel-secret-value-7f3a"
REID = {"X-Forwarded-Email": "reid@example.com"}
STEVE = {"X-Forwarded-Email": "steve@example.com"}
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
APP_ID = "7f123a12-dc5d-47ae-97aa-2458d1adabc3"


@pytest.fixture(autouse=True)
def _fresh_health() -> Iterator[None]:
    key_health.reset()
    yield
    key_health.reset()


def _secret(name: str, **fields: Any) -> secrets_meta.SecretMeta:
    return secrets_meta.SecretMeta(name=name, **fields)


# ── the relay ───────────────────────────────────────────────────────────────

BOX_VAULT = [
    {
        "name": "azure-cost-client-secret",
        "enabled": True,
        "expires": None,
        "updated": "2026-10-22T00:00:00+00:00",
        "source": f"entra:{APP_ID}",
        "value": SENTINEL,
    },
    {"name": "omnigent-ae-github-token", "enabled": True, "expires": None, "updated": None},
    {"name": "hubspot-access-token", "updated": "2026-04-23T16:16:01+00:00", "source": "junk"},
]
WEB_VAULT = [{"name": "hubspot-access-token", "updated": "2026-10-19T00:43:54+00:00"}]
APPS = [
    {
        "appId": APP_ID.upper(),
        "displayName": "ae-omnigent-azure-cost",
        "ends": ["2027-01-01T00:00:00Z", "2036-10-22T05:02:07Z"],
        "hint": SENTINEL[:3],
    },
    {
        "appId": "6daa04d7-48f1-4a76-942f-4b92e6ce7542",
        "displayName": "AE Security Auditor",
        "ends": [],
    },
]


def _az(seen: list[Sequence[str]] | None = None, *, apps_fail: bool = False):
    def run(argv: Sequence[str]) -> str:
        if seen is not None:
            seen.append(list(argv))
        assert "show" not in argv
        if argv[1:3] == ["ad", "app"]:
            if apps_fail:
                raise secrets_relay.RelayError("no Graph access")
            return json.dumps(APPS)
        vault = argv[argv.index("--vault-name") + 1]
        return json.dumps(BOX_VAULT if vault == "box-vault" else WEB_VAULT)

    return run


def _host_file(tmp_path: Path, line: str) -> Path:
    path = tmp_path / "host.env"
    path.write_text(f"OTHER=1\n{line}\n", encoding="utf-8")
    return path


async def test_the_relay_posts_both_vaults_tags_entra_dates_and_the_host_key(
    tmp_path: Path,
) -> None:
    seen: list[Sequence[str]] = []
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"kept": 4})

    out = io.StringIO()
    code = await secrets_relay.run(
        vault="box-vault",
        az_bin="az",
        settings=omni.Settings(server_url="https://omni.test", token="t"),
        runner=_az(seen),
        transport=httpx.MockTransport(handler),
        out=out,
        extra_vaults=["web-vault"],
        host_env=_host_file(tmp_path, f"CLAUDE_CODE_OAUTH_TOKEN={SENTINEL}"),
    )
    assert code == 0, out.getvalue()
    body = json.loads(calls[0].content)
    assert SENTINEL not in calls[0].content.decode()
    assert SENTINEL[:3] not in json.dumps(body["secrets"])
    assert body["host"] == {"CLAUDE_CODE_OAUTH_TOKEN": True}
    assert [v["vault"] for v in body["vaults"]] == ["web-vault"]
    cost = next(s for s in body["secrets"] if s["name"] == "azure-cost-client-secret")
    # The latest of the app's two end dates, joined through the tag (case does not matter).
    assert cost["source_expires"].startswith("2036-10-22")
    junk = next(s for s in body["secrets"] if s["name"] == "hubspot-access-token")
    assert junk["source"] is None
    apps_call = next(argv for argv in seen if argv[1:3] == ["ad", "app"])
    assert "hint" not in " ".join(apps_call)
    assert 'tags."ae-source"' in secrets_relay.QUERY
    assert "value" not in secrets_relay.QUERY
    assert "posted 4 from box-vault, web-vault; the server now holds 4 across all vaults" in (
        out.getvalue()
    )


async def test_a_part_that_cannot_be_read_is_left_out_and_the_run_fails(tmp_path: Path) -> None:
    out = io.StringIO()
    code = await secrets_relay.run(
        vault="box-vault",
        az_bin="az",
        settings=omni.Settings(),
        dry_run=True,
        runner=_az(apps_fail=True),
        out=out,
        host_env=tmp_path / "missing.env",
    )
    assert code == 1
    text = out.getvalue()
    assert "no Entra end dates" in text
    assert "keeps its last host reading" in text
    assert '"azure-cost-client-secret"' in text


@pytest.mark.parametrize(
    ("line", "present"),
    [
        (f"CLAUDE_CODE_OAUTH_TOKEN={SENTINEL}", True),
        (f"export CLAUDE_CODE_OAUTH_TOKEN='{SENTINEL}'", True),
        ("CLAUDE_CODE_OAUTH_TOKEN=", False),
        ('CLAUDE_CODE_OAUTH_TOKEN=""', False),
        ("SOMETHING_ELSE=1", False),
    ],
)
def test_the_host_key_check_keeps_a_yes_or_no_only(
    tmp_path: Path, line: str, present: bool
) -> None:
    got = secrets_relay.host_keys(_host_file(tmp_path, line))
    assert got == {"CLAUDE_CODE_OAUTH_TOKEN": present}
    assert secrets_relay.host_keys(tmp_path / "nope.env") is None


def test_the_post_takes_up_to_1000_secrets_and_only_known_host_keys() -> None:
    many = [{"name": f"s-{n}"} for n in range(1000)]
    assert len(secrets_meta.MetaPost.model_validate({"secrets": many}).secrets) == 1000
    with pytest.raises(ValueError):
        secrets_meta.MetaPost.model_validate({"secrets": [*many, {"name": "one-more"}]})
    post = secrets_meta.MetaPost.model_validate(
        {"secrets": [], "host": {"CLAUDE_CODE_OAUTH_TOKEN": True, "SOMETHING": True}}
    )
    assert post.host == {"CLAUDE_CODE_OAUTH_TOKEN": True}


@pytest.fixture(autouse=True)
def _oidc_app(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(secrets_relay.OIDC_APP_ENV, "00000000-0000-0000-0000-000000000001")


def test_tag_proposals_match_known_pairs_and_names_and_skip_tagged_ones() -> None:
    apps = [
        secrets_relay.App(APP_ID, "ae-omnigent-azure-cost", (NOW,)),
        secrets_relay.App("6daa04d7-48f1-4a76-942f-4b92e6ce7542", "AE Security Auditor", ()),
        secrets_relay.App("00000000-0000-0000-0000-000000000001", "AE OmniGent", ()),
    ]
    vaults = {
        "box-vault": [
            {"name": "azure-cost-client-secret", "tags": {"file-encoding": "utf-8"}},
            {"name": "ae-auditor-client-secret", "tags": None},
            {"name": "omnigent-oidc-client-secret", "tags": {}},
            {"name": "omnigent-oidc-cookie-secret", "tags": {}},
            {"name": "ae-client-secret", "tags": {}},
            {"name": "hc-audit-client-id", "tags": {"ae-source": "entra:x"}},
        ]
    }
    proposals, tagged = secrets_relay.propose_tags(vaults, apps)
    names = {p.name: p.app.name for p in proposals}
    assert names == {
        "azure-cost-client-secret": "ae-omnigent-azure-cost",
        "ae-auditor-client-secret": "AE Security Auditor",
        "omnigent-oidc-client-secret": "AE OmniGent",
    }
    assert tagged == ["box-vault/hc-audit-client-id: already ae-source=entra:x"]
    argv = next(p for p in proposals if p.name == "azure-cost-client-secret").argv("az")
    assert argv[:4] == ["az", "keyvault", "secret", "set-attributes"]
    assert "file-encoding=utf-8" in argv and f"ae-source=entra:{APP_ID}" in argv


def test_propose_prints_and_sets_nothing_without_apply() -> None:
    seen: list[Sequence[str]] = []

    def run(argv: Sequence[str]) -> str:
        seen.append(list(argv))
        if argv[1:3] == ["ad", "app"]:
            return json.dumps(APPS)
        return json.dumps([{"name": "azure-cost-client-secret", "tags": {}}])

    out = io.StringIO()
    assert (
        secrets_relay.run_propose(
            vaults=["box-vault"], az_bin="az", apply=False, runner=run, out=out
        )
        == 0
    )
    assert "ends 2036-10-22" in out.getvalue()
    assert not [argv for argv in seen if "set-attributes" in argv]
    assert (
        secrets_relay.run_propose(
            vaults=["box-vault"], az_bin="az", apply=True, runner=run, out=io.StringIO()
        )
        == 0
    )
    assert [argv for argv in seen if "set-attributes" in argv]


# ── source expiry ───────────────────────────────────────────────────────────


def test_the_earlier_of_the_vault_and_the_source_date_wins() -> None:
    soon = NOW + timedelta(days=20)
    later = NOW + timedelta(days=400)
    meta = _secret("x", expires=later, source=f"entra:{APP_ID}", source_expires=soon)
    got = secrets_meta.expiry(meta, NOW)
    assert (got.expiry_from, got.expiry_state, got.days_left) == ("entra", "soon", 20)
    assert got.vault_expires == later.isoformat()
    meta = _secret("x", expires=soon, source=f"entra:{APP_ID}", source_expires=later)
    assert secrets_meta.expiry(meta, NOW).expiry_from == "vault"
    # A source date without the tag that names it does not count.
    assert secrets_meta.expiry(_secret("x", source_expires=soon), NOW).expiry_state == "none"


def test_the_github_header_becomes_the_tokens_source_expiry() -> None:
    reading = secrets_meta.Reading(
        read_at=NOW, secrets={"omnigent-ae-github-token": _secret("omnigent-ae-github-token")}
    )
    assert secrets_meta.words("AE_GITHUB_TOKEN", reading, NOW).expiry_state == "none"
    key_health.note_expiry("AE_GITHUB_TOKEN", NOW + timedelta(days=5))
    got = secrets_meta.words("AE_GITHUB_TOKEN", reading, NOW)
    assert (got.expiry_from, got.expiry_state, got.days_left) == ("github", "soon", 5)


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("2026-12-01 10:00:00 UTC", datetime(2026, 12, 1, 10, tzinfo=UTC)),
        ("2026-12-01 10:00:00 -0800", datetime(2026, 12, 1, 18, tzinfo=UTC)),
        ("soon", None),
        (None, None),
    ],
)
def test_github_expiry_header_parses(header: str | None, expected: datetime | None) -> None:
    assert key_health.parse_github_expiry(header) == expected


async def test_github_calls_record_the_token_and_its_expiry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(
                200,
                json={"login": "reid"},
                headers={key_health.GITHUB_EXPIRY_HEADER: "2026-12-01 10:00:00 UTC"},
            )
        return httpx.Response(401, json={"message": SENTINEL})

    settings = github.GitHubSettings(token="t", repos=("o/r",))
    async with github.GitHubClient(settings, transport=httpx.MockTransport(handler)) as client:
        await client._get("/user")
        assert key_health.get("github").ok  # type: ignore[union-attr]
        assert key_health.expiry("AE_GITHUB_TOKEN") == datetime(2026, 12, 1, 10, tzinfo=UTC)
        with pytest.raises(github.GitHubError):
            await client._get("/repos/o/r")
    outcome = key_health.get("github")
    assert outcome is not None and not outcome.ok and outcome.detail == "401"
    assert outcome.last_ok is not None


# ── last worked ─────────────────────────────────────────────────────────────


def test_the_registry_keeps_a_status_or_a_class_name_only() -> None:
    key_health.record("databricks", True, now=NOW)
    key_health.record("databricks", False, 401, now=NOW + timedelta(minutes=5))
    got = key_health.get("databricks")
    assert got is not None
    assert (got.ok, got.detail, got.last_ok) == (False, "401", NOW)
    key_health.record("databricks", False, TimeoutError("http://x?sig=" + SENTINEL))
    assert key_health.get("databricks").detail == "TimeoutError"  # type: ignore[union-attr]
    for body in (SENTINEL, '{"error": "bad"}', "https://x.test/?sig=abc", "401 Unauthorized"):
        assert key_health.clean_detail(body) == "error"


def test_fabric_records_each_capacitys_pair() -> None:
    capacity = next(
        iter(
            fabric.capacities(
                {
                    "AE_FABRIC_WAKE_URL_LAB": "https://logic.test/x?sig=1",
                    "AE_FABRIC_SUSPEND_KEY_LAB": "a" * 64,
                }
            ).values()
        )
    )
    fabric._note_capacity(capacity, "probe", 200)
    assert key_health.get("fabric_wake:LAB").ok  # type: ignore[union-attr]
    assert key_health.get("fabric_suspend:LAB") is None
    fabric._note_capacity(capacity, "suspend", 403)
    assert key_health.get("fabric_suspend:LAB").detail == "403"  # type: ignore[union-attr]
    assert key_health.get("fabric_wake:LAB").ok  # type: ignore[union-attr]


async def test_the_oidc_callback_records_a_working_exchange_only() -> None:
    middleware = session_cookie.SessionCookieExpiry(
        lambda *_: None,
        cookie_secret=b"k" * 32,
        secure=True,  # type: ignore[arg-type]
    )
    sent: list[dict[str, Any]] = []

    async def send(message: Any) -> None:
        sent.append(message)

    watched = middleware._watch_callback(send)
    # Upstream's 400 is the same for a refused secret, an expired code and an IdP 5xx.
    await watched({"type": "http.response.start", "status": 400, "headers": []})
    await watched({"type": "http.response.body", "body": b'{"error": "Token exchange failed"}'})
    assert key_health.get("oidc") is None
    watched = middleware._watch_callback(send)
    cookie = (b"set-cookie", b"__Host-ap_session=abc; Path=/")
    await watched({"type": "http.response.start", "status": 302, "headers": [cookie]})
    assert key_health.get("oidc").ok  # type: ignore[union-attr]
    assert len(sent) == 3


def test_the_box_list_carries_checks_the_host_row_and_failing_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = secrets_meta.SecretsMetaStore(None)
    store.put(
        secrets_meta.MetaPost(
            secrets=[_secret("claude-code-oauth-token", updated=NOW)],
            host={"CLAUDE_CODE_OAUTH_TOKEN": False},
        )
    )
    secrets_meta.reset_store(store)
    env = dict.fromkeys([*secrets_health.REQUIRED, *secrets_health.OPTIONAL], SENTINEL)
    key_health.record("oidc", False, 400)
    key_health.record("databricks", True)
    view = secrets_health.view(env, auth_source="oidc")
    host = next(row for row in view.required if row.key == "CLAUDE_CODE_OAUTH_TOKEN")
    assert (host.where, host.present, host.vault_name) == (
        "host",
        False,
        "claude-code-oauth-token",
    )
    assert view.host_missing == ["CLAUDE_CODE_OAUTH_TOKEN"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in view.missing
    assert view.failing == ["OMNIGENT_OIDC_CLIENT_SECRET", "OMNIGENT_OIDC_COOKIE_SECRET"]
    dbx = next(row for row in view.optional if row.key == "AE_DATABRICKS_CLIENT_SECRET")
    assert dbx.check == "ok" and dbx.worked_at is not None
    assert SENTINEL not in view.model_dump_json()


def test_no_host_row_before_the_relay_reports_it() -> None:
    secrets_meta.reset_store(secrets_meta.SecretsMetaStore(None))
    view = secrets_health.view({}, auth_source="accounts")
    assert not [row for row in view.required if row.where == "host"]


# ── the inventory ───────────────────────────────────────────────────────────


def _inventory_reading() -> secrets_meta.Reading:
    old = NOW - timedelta(days=190)
    box = {
        "omnigent-ae-github-token": _secret(
            "omnigent-ae-github-token", updated=NOW - timedelta(days=6)
        ),
        "github-pat": _secret("github-pat", updated=NOW - timedelta(days=76)),
        "databricks-cli-client-secret": _secret(
            "databricks-cli-client-secret", expires=NOW + timedelta(days=30), updated=NOW
        ),
        "old-expired": _secret("old-expired", expires=NOW - timedelta(days=2), updated=old),
        "anthropic-api-key": _secret("anthropic-api-key", updated=old),
        "hubspot-access-token": _secret("hubspot-access-token", updated=old),
    }
    web = {
        "hubspot-access-token": _secret("hubspot-access-token", updated=NOW - timedelta(days=4))
    }
    return secrets_meta.Reading(read_at=NOW, secrets=box, vaults={"kv-example-web": web})


def test_the_inventory_counts_groups_badges_and_needs() -> None:
    got = vault_inventory.view(_inventory_reading(), NOW)
    assert [v.vault for v in got.vaults] == ["kv-example-box", "kv-example-web"]
    box, web = got.vaults
    assert box.count == 6 and web.count == 1
    assert box.counts.model_dump() == {
        "expired": 1,
        "soon": 1,
        "no_expiry": 4,
        "untouched": 3,
        "no_owner": 6,
    }
    assert [(n.name, n.kind) for n in box.needs_you] == [
        ("old-expired", "expired"),
        ("databricks-cli-client-secret", "soon"),
        ("hubspot-access-token", "both"),
        ("anthropic-api-key", "untouched"),
    ]
    assert box.needs_you[3].text == "Not changed in 190 days, no expiry set"
    rows = {row.name: row for row in box.secrets}
    assert rows["omnigent-ae-github-token"].on_box
    assert rows["omnigent-ae-github-token"].box_key == "AE_GITHUB_TOKEN"
    assert not rows["github-pat"].on_box
    assert rows["hubspot-access-token"].both_vaults
    assert web.secrets[0].both_vaults and not web.secrets[0].on_box
    assert web.secrets[0].rotate_url is None
    groups = {g.id: (g.label, g.count, g.oldest_days) for g in box.groups}
    assert groups["github"] == ("GitHub", 1, 76)
    assert groups["omnigent"] == ("OmniGent", 1, 6)


def test_the_inventory_only_lets_the_box_copy_take_githubs_date() -> None:
    reading = secrets_meta.Reading(
        read_at=NOW,
        secrets={"omnigent-ae-github-token": _secret("omnigent-ae-github-token")},
        vaults={"other": {"omnigent-ae-github-token": _secret("omnigent-ae-github-token")}},
    )
    key_health.note_expiry("AE_GITHUB_TOKEN", NOW + timedelta(days=10))
    box, other = vault_inventory.view(reading, NOW).vaults
    assert box.secrets[0].expiry_from == "github"
    assert other.secrets[0].expiry_from is None


def test_no_reading_is_an_empty_stale_inventory() -> None:
    got = vault_inventory.view(None, NOW)
    assert got.vaults == [] and got.stale


VALUE_LIKE = {"value", "secret", "hint", "hash", "length", "token", "password"}


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for value in node.values() for k in _keys(value)}
    if isinstance(node, list):
        return {k for value in node for k in _keys(value)}
    return set()


async def test_the_vault_route_is_admin_only_and_carries_no_value(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    store = secrets_meta.SecretsMetaStore(None)
    store.put(
        secrets_meta.MetaPost.model_validate(
            {
                "secrets": [
                    {"name": "omnigent-ae-github-token", "value": SENTINEL},
                    {"name": "anthropic-api-key"},
                ],
                "vaults": [{"vault": "kv-example-web", "secrets": [{"name": "turnstile-secret"}]}],
            }
        )
    )
    secrets_meta.reset_store(store)
    assert (await client.get("/v1/ae/vault", headers=STEVE)).status_code == 403
    got = await client.get("/v1/ae/vault", headers=REID)
    assert got.status_code == 200, got.text
    body = got.json()
    assert SENTINEL not in got.text
    assert not _keys(body) & VALUE_LIKE
    assert [v["count"] for v in body["vaults"]] == [2, 1]


# ── review fixes (2026-10-23) ───────────────────────────────────────────────


def test_changed_is_the_current_versions_creation_not_a_tag_edit() -> None:
    created = NOW - timedelta(days=200)
    tagged = NOW - timedelta(hours=1)
    meta = _secret("ae-client-secret", created=created, updated=tagged)
    reading = secrets_meta.Reading(read_at=NOW, secrets={"ae-client-secret": meta})
    row = vault_inventory.view(reading, NOW).vaults[0].secrets[0]
    assert (row.updated_days, row.untouched) == (200, True)
    # An older reading without ``created`` falls back to ``updated``.
    assert _secret("x", updated=tagged).changed == tagged
    assert "created:attributes.created" in secrets_relay.QUERY


def _post(**fields: Any) -> secrets_meta.MetaPost:
    return secrets_meta.MetaPost.model_validate(fields)


def test_a_failed_part_keeps_its_previous_reading() -> None:
    store = secrets_meta.SecretsMetaStore(None)
    tag = f"entra:{APP_ID}"
    ends = NOW + timedelta(days=300)
    store.put(
        _post(
            secrets=[
                {
                    "name": "azure-cost-client-secret",
                    "source": tag,
                    "source_expires": ends,
                    "source_count": 2,
                }
            ],
            vaults=[{"vault": "kv-example-web", "secrets": [{"name": "turnstile-secret"}]}],
            host={"CLAUDE_CODE_OAUTH_TOKEN": True},
        ),
        now=NOW,
    )
    later = NOW + timedelta(days=1)
    store.put(
        _post(
            secrets=[{"name": "azure-cost-client-secret", "source": tag}],
            failed=["entra", "vault:kv-example-web", "host", "bogus part"],
        ),
        now=later,
    )
    got = store.read()
    assert got is not None
    assert got.secrets["azure-cost-client-secret"].source_expires == ends
    assert list(got.vaults["kv-example-web"]) == ["turnstile-secret"]
    assert got.host == {"CLAUDE_CODE_OAUTH_TOKEN": True}
    assert got.carried("entra") == NOW
    assert got.carried("vault:kv-example-web") == NOW
    assert got.carried("vault:kv-example-box") is None
    words = secrets_meta.expiry(
        got.secrets["azure-cost-client-secret"], later, entra_from=got.carried("entra")
    )
    assert (words.source_count, words.source_from) == (2, NOW.isoformat())
    web = vault_inventory.view(got, later).vaults[1]
    assert web.read_from == NOW.isoformat()
    # A part that is read again is fresh again.
    store.put(_post(secrets=[{"name": "azure-cost-client-secret", "source": tag}]), now=later)
    again = store.read()
    assert again is not None and again.carried("entra") is None
    assert again.secrets["azure-cost-client-secret"].source_expires is None
    assert "kv-example-web" not in again.vaults


async def test_the_relay_names_the_parts_it_could_not_read(tmp_path: Path) -> None:
    def run(argv: Sequence[str]) -> str:
        if argv[1:3] == ["ad", "app"]:
            raise secrets_relay.RelayError("no Graph access")
        vault = argv[argv.index("--vault-name") + 1]
        if vault == "web-vault":
            raise secrets_relay.RelayError("forbidden")
        return json.dumps(BOX_VAULT)

    out = io.StringIO()
    await secrets_relay.run(
        vault="box-vault",
        az_bin="az",
        settings=omni.Settings(),
        dry_run=True,
        runner=run,
        out=out,
        extra_vaults=["web-vault"],
        host_env=tmp_path / "missing.env",
    )
    body = json.loads(out.getvalue()[out.getvalue().index("{") :])
    assert body["failed"] == ["entra", "vault:web-vault", "host"]


def test_an_old_state_file_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "meta.json"
    path.write_text(
        json.dumps(
            {
                "read_at": "2026-10-23T12:00:00+00:00",
                "vault": "kv-example-box",
                "secrets": {
                    "omnigent-ae-github-token": {
                        "name": "omnigent-ae-github-token",
                        "enabled": True,
                        "expires": None,
                        "updated": "2026-10-17T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    got = secrets_meta.SecretsMetaStore(path).read()
    assert got is not None
    assert (got.vaults, got.host, got.parts) == ({}, None, {})
    assert vault_inventory.view(got, NOW).vaults[0].count == 1


REAL_APPS = [
    ("9d9eeebc-93d2-44c6-b9a0-9b713a6adc20", "M365 MCP Server (AE)"),
    ("6daa04d7-48f1-4a76-942f-4b92e6ce7542", "AE Security Auditor"),
    ("00000000-0000-0000-0000-000000000001", "AE OmniGent"),
    ("26e70cab-a5fb-427a-8244-b82f49239f47", "ae-fabric-sandbox-sp"),
    ("b51fea8a-c190-4f31-9cfe-1b0ea64b8f29", "AE Task Bridge"),
    ("e812277e-186b-4d11-bd3c-dcc48857317e", "Cloudflare Access - AE Shortener"),
    (APP_ID, "ae-omnigent-azure-cost"),
    ("8df6aa5d-82ad-48a0-bb70-75515545377d", "AE MCP Connectors"),
    ("c265143e-581e-41e0-a55c-c8483b03549d", "FUAM Service Principal"),
]


def test_the_dry_run_proposes_all_eight_pairs() -> None:
    apps = [secrets_relay.App(app_id, name, (NOW,)) for app_id, name in REAL_APPS]
    names = [
        "ae-auditor-client-secret",
        "azure-cost-client-secret",
        "fabric-sandbox-sp-secret",
        "omnigent-oidc-client-secret",
        "ae-client-secret",
        "devops-bridge-client-secret",
        "cf-access-azure-client-secret",
        "esignatures-oauth-ms-client-secret",
        "hc-client-secret",
        "omnigent-oidc-cookie-secret",
        "youtube-client-secret",
    ]
    proposals, _ = secrets_relay.propose_tags(
        {"kv-example-box": [{"name": name, "tags": {}} for name in names]}, apps
    )
    got = {p.name: p.app.name for p in proposals}
    assert got == {
        "ae-auditor-client-secret": "AE Security Auditor",
        "azure-cost-client-secret": "ae-omnigent-azure-cost",
        "fabric-sandbox-sp-secret": "ae-fabric-sandbox-sp",
        "omnigent-oidc-client-secret": "AE OmniGent",
        "ae-client-secret": "M365 MCP Server (AE)",
        "devops-bridge-client-secret": "AE Task Bridge",
        "cf-access-azure-client-secret": "Cloudflare Access - AE Shortener",
        "esignatures-oauth-ms-client-secret": "AE MCP Connectors",
    }


# ── call sites record the key's outcome ─────────────────────────────────────


def _ws(n: int) -> Any:
    from ae_omni_policies import databricks

    return databricks.Workspace(host=f"https://dbx{n}.test", client_id="id", client_secret="s")


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"access_token": "tok", "expires_in": 3600}, (True, None)),
        (401, {"error": "invalid_client"}, (False, "401")),
        (400, {"error": "invalid_client"}, (False, "400")),
        (400, {"error": "invalid_request"}, None),
        (403, {"error": "insufficient_scope"}, None),
    ],
)
async def test_databricks_token_records_only_a_refused_credential(
    status: int, body: dict[str, Any], expected: tuple[bool, str | None] | None
) -> None:
    from ae_omni_policies import databricks

    transport = httpx.MockTransport(lambda _r: httpx.Response(status, json=body))
    with contextlib.suppress(databricks.DatabricksError):
        await databricks._token(_ws(status * 10 + len(body.get("error", ""))), transport)
    got = key_health.get("databricks")
    assert (None if got is None else (got.ok, got.detail)) == expected


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"access_token": "tok", "expires_in": 3600}, (True, None)),
        (401, {"error": "invalid_client"}, (False, "401")),
        (400, {"error": "invalid_request"}, None),
    ],
)
async def test_azure_cost_token_records_the_principal(
    status: int, body: dict[str, Any], expected: tuple[bool, str | None] | None
) -> None:
    from ae_omni_policies import azure_cost

    cfg = azure_cost.Config(
        tenant=f"t{status}", client_id="c", client_secret="s", subscriptions=()
    )
    transport = httpx.MockTransport(lambda _r: httpx.Response(status, json=body))
    with contextlib.suppress(azure_cost.AzureCostError):
        await azure_cost._token(cfg, transport)
    got = key_health.get("azure_cost")
    assert (None if got is None else (got.ok, got.detail)) == expected


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(200, json={"buckets": []}), (True, None)),
        (
            httpx.Response(302, headers={"location": "https://x.cloudflareaccess.com/login"}),
            (False, "302"),
        ),
        (
            httpx.Response(
                403, text="<html>Forbidden</html>", headers={"content-type": "text/html"}
            ),
            (False, "403"),
        ),
        (httpx.Response(403, json={"error": "not your list"}), (True, None)),
    ],
)
async def test_taskstore_records_only_an_access_refusal(
    response: httpx.Response, expected: tuple[bool, str | None]
) -> None:
    from ae_omni_policies import taskstore

    settings = taskstore.Settings(app_url="https://tasks.test", client_id="i", client_secret="s")
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: response))
    async with taskstore.TaskStoreClient(settings, http=http) as client:
        await client.list_buckets()
    await http.aclose()
    got = key_health.get("taskstore")
    assert got is not None and (got.ok, got.detail) == expected
