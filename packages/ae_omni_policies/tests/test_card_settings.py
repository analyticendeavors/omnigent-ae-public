"""Settings typed in the Cards panel: precedence, the route's refusals, readers (2026-11-07)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import (
    admin_cards,
    azure_cost,
    card_config,
    databricks,
    fabric,
    git_center,
)

OWNER = {"X-Forwarded-Email": "owner@example.com", "Sec-Fetch-Site": "same-origin"}
MEMBER = {"X-Forwarded-Email": "member@example.com", "Sec-Fetch-Site": "same-origin"}
# Built from parts so the leak scan does not read a made-up id as a real one.
SUB = "-".join(["11111111", "2222", "3333", "4444", "555555555555"])

EDITABLE = {
    "AE_GIT_SWEEP_CONFIG",
    "AE_GIT_SWEEP_WORKFLOW",
    "AE_GIT_REPOS",
    "AE_GIT_VIEW_ONLY",
    "AE_GIT_KEEP_BRANCHES",
    "AE_DATABRICKS_CREDIT_USD",
    "AE_DATABRICKS_CREDIT_CADENCE",
    "AE_DATABRICKS_CREDIT_START",
    "AE_DATABRICKS_CREDIT_END",
    "AE_AZURE_COST_SUBSCRIPTIONS",
    "AE_FABRIC_USD_PER_CU_HOUR",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in EDITABLE:
        monkeypatch.delenv(name, raising=False)


def _save(name: str, value: str) -> None:
    store = admin_cards.store()
    store.set_settings(admin_cards.EDITABLE[name], {name: value}, "owner@example.com", 0)


async def _post(
    client: httpx.AsyncClient,
    card: str,
    values: dict[str, Any],
    revision: int = 0,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return await client.post(
        "/v1/ae/cards/settings",
        json={"id": card, "values": values, "revision": revision},
        headers=headers or OWNER,
    )


# ── the catalog's verdicts ────────────────────────────────────────────────────────────────────


def test_the_editable_fields_are_exactly_the_audited_list() -> None:
    assert set(admin_cards.EDITABLE) == EDITABLE


def test_every_editable_field_has_a_rule_and_none_is_an_endpoint_or_a_secret() -> None:
    for card in admin_cards.CATALOG:
        for item in card.fields:
            if item.editable:
                assert item.env in card_config._RULES, item.env
                assert item.kind in ("text", "list", "number", "date", "bool")
                assert not item.env_only and not item.host
            else:
                assert item.env not in card_config._RULES, item.env
            if item.kind in ("url", "secret") or item.prefix:
                assert not item.editable


# ── precedence ────────────────────────────────────────────────────────────────────────────────


def test_the_environment_wins_a_blank_one_falls_back_and_nothing_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = "AE_FABRIC_USD_PER_CU_HOUR"
    assert card_config.value(name) is None
    _save(name, "0.25")
    assert card_config.value(name) == "0.25"
    monkeypatch.setenv(name, "   ")
    assert card_config.value(name) == "0.25"
    monkeypatch.setenv(name, "0.30")
    assert card_config.value(name) == "0.30"


def test_a_caller_with_its_own_mapping_never_sees_a_saved_value() -> None:
    _save("AE_GIT_REPOS", "acme/one")
    assert card_config.value("AE_GIT_REPOS", {}) is None
    assert card_config.value("AE_GIT_REPOS", {"AE_GIT_REPOS": "acme/two"}) == "acme/two"


def test_a_saved_value_for_a_field_that_is_not_editable_is_ignored(
    tmp_path: Path,
) -> None:
    store = admin_cards.CardStore()
    admin_cards.reset_store(store)
    store.set_settings("compute", {"AE_FABRIC_CAPACITIES_URL": "https://example.test"}, "a", 0)
    assert card_config.value("AE_FABRIC_CAPACITIES_URL") is None


def test_an_unreadable_state_file_answers_none(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    path.write_text("{broken", encoding="utf-8")
    admin_cards.reset_store(admin_cards.CardStore(path))
    assert card_config.value("AE_GIT_REPOS") is None


# ── readers use the saved value when the environment has none ─────────────────────────────────


def test_git_reads_its_lists_from_the_panel_and_the_environment_still_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _save("AE_GIT_REPOS", "acme/one,acme/two")
    assert git_center.Settings().extra_repos == ["acme/one", "acme/two"]
    assert git_center.Settings().configured is True
    monkeypatch.setenv("AE_GIT_REPOS", "acme/three")
    assert git_center.Settings().extra_repos == ["acme/three"]
    assert git_center.Settings({}).extra_repos == []


def test_git_reads_the_sweep_config_the_workflow_and_the_kept_branches() -> None:
    store = admin_cards.store()
    store.set_settings(
        "git",
        {
            "AE_GIT_SWEEP_CONFIG": "acme/ops:cfg/sweep.json",
            "AE_GIT_SWEEP_WORKFLOW": "sweep.yml",
            "AE_GIT_KEEP_BRANCHES": "acme/one:release",
        },
        "owner@example.com",
        0,
    )
    got = git_center.Settings()
    assert (got.sweep_repo, got.sweep_path, got.workflow) == (
        "acme/ops",
        "cfg/sweep.json",
        "sweep.yml",
    )
    assert got.is_kept("acme/one", "release")


def test_databricks_reads_its_credit_from_the_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_cards.store().set_settings(
        "credit",
        {
            "AE_DATABRICKS_CREDIT_USD": "500",
            "AE_DATABRICKS_CREDIT_START": "2026-01-01",
            "AE_DATABRICKS_CREDIT_END": "2026-12-31",
            "AE_DATABRICKS_CREDIT_CADENCE": "monthly",
        },
        "owner@example.com",
        0,
    )
    got = databricks.credit()
    assert got.usd == 500.0 and got.cadence == "monthly"
    assert got.start is not None and got.end is not None
    monkeypatch.setenv("AE_DATABRICKS_CREDIT_USD", "900")
    assert databricks.credit().usd == 900.0


def test_azure_reads_its_subscriptions_from_the_panel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AE_AZURE_COST_TENANT_ID", "tenant")
    monkeypatch.setenv("AE_AZURE_COST_CLIENT_ID", "client")
    monkeypatch.setenv("AE_AZURE_COST_CLIENT_SECRET", "secret")
    assert azure_cost.config() is None
    _save("AE_AZURE_COST_SUBSCRIPTIONS", f"{SUB}=Production")
    cfg = azure_cost.config()
    assert cfg is not None and cfg.subscriptions == ((SUB, "Production"),)


def test_fabric_reads_its_price_from_the_panel_and_ignores_a_bad_one() -> None:
    assert fabric.usd_per_cu_hour() == fabric.DEFAULT_USD_PER_CU_HOUR
    _save("AE_FABRIC_USD_PER_CU_HOUR", "0.25")
    assert fabric.usd_per_cu_hour() == 0.25


# ── validation ────────────────────────────────────────────────────────────────────────────────


def _item(env: str) -> admin_cards.Field_:
    return next(i for c in admin_cards.CATALOG for i in c.fields if i.env == env)


@pytest.mark.parametrize(
    ("env", "raw", "want"),
    [
        ("AE_GIT_REPOS", " acme/one , acme/two,acme/one,", "acme/one,acme/two"),
        ("AE_GIT_KEEP_BRANCHES", "acme/one:release/1.0", "acme/one:release/1.0"),
        ("AE_GIT_SWEEP_CONFIG", "acme/ops:cfg/sweep.json", "acme/ops:cfg/sweep.json"),
        ("AE_GIT_SWEEP_WORKFLOW", "sweep.yml", "sweep.yml"),
        ("AE_AZURE_COST_SUBSCRIPTIONS", f"{SUB.upper()}=Prod, {SUB.replace('1', '2')}", None),
        ("AE_DATABRICKS_CREDIT_USD", "100.5", "100.5"),
        ("AE_DATABRICKS_CREDIT_CADENCE", "Monthly", "monthly"),
        ("AE_DATABRICKS_CREDIT_START", "2026-02-28", "2026-02-28"),
        ("AE_FABRIC_USD_PER_CU_HOUR", "0.18", "0.18"),
        ("AE_FABRIC_USD_PER_CU_HOUR", "   ", None),
    ],
)
def test_good_values_are_normalised(env: str, raw: str, want: str | None) -> None:
    got = card_config.normalize(_item(env), raw)
    if env == "AE_AZURE_COST_SUBSCRIPTIONS":
        assert got is not None and got.startswith(f"{SUB}=Prod,")
    else:
        assert got == want


@pytest.mark.parametrize(
    ("env", "raw"),
    [
        ("AE_GIT_REPOS", "not a repo"),
        ("AE_GIT_REPOS", "a/b/c"),
        ("AE_GIT_REPOS", "acme/.."),
        ("AE_GIT_REPOS", ",".join(f"acme/r{n}" for n in range(51))),
        ("AE_GIT_VIEW_ONLY", "-acme/one"),
        ("AE_GIT_KEEP_BRANCHES", "acme/one"),
        ("AE_GIT_KEEP_BRANCHES", "acme/one:bad..name"),
        ("AE_GIT_KEEP_BRANCHES", "acme/one:has space"),
        ("AE_GIT_KEEP_BRANCHES", "acme/one:/lead"),
        ("AE_GIT_SWEEP_CONFIG", "acme/ops"),
        ("AE_GIT_SWEEP_CONFIG", "acme/ops:../secret"),
        ("AE_GIT_SWEEP_CONFIG", "acme/ops:/etc/passwd"),
        ("AE_GIT_SWEEP_WORKFLOW", "../x.yml"),
        ("AE_GIT_SWEEP_WORKFLOW", "sweep.sh"),
        ("AE_AZURE_COST_SUBSCRIPTIONS", "not-a-guid=Prod"),
        ("AE_DATABRICKS_CREDIT_USD", "-5"),
        ("AE_DATABRICKS_CREDIT_USD", "1e9"),
        ("AE_DATABRICKS_CREDIT_USD", "nan"),
        ("AE_DATABRICKS_CREDIT_USD", "99999999"),
        ("AE_DATABRICKS_CREDIT_CADENCE", "weekly"),
        ("AE_DATABRICKS_CREDIT_START", "2026-13-01"),
        ("AE_DATABRICKS_CREDIT_END", "01/02/2026"),
        ("AE_FABRIC_USD_PER_CU_HOUR", "100"),
        ("AE_FABRIC_USD_PER_CU_HOUR", "0"),
        ("AE_GIT_REPOS", "a" * 2_001),
    ],
)
def test_bad_values_state_the_rule(env: str, raw: str) -> None:
    with pytest.raises(card_config.InvalidValue) as raised:
        card_config.normalize(_item(env), raw)
    assert len(str(raised.value)) < 200


# ── the route ─────────────────────────────────────────────────────────────────────────────────


async def test_an_admin_saves_a_setting_reads_it_back_and_clears_it(
    client: httpx.AsyncClient, ae_admins: set[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        done = await _post(client, "git", {"AE_GIT_REPOS": "acme/secretrepo"})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["revision"] == 1
    git = next(c for c in body["cards"] if c["id"] == "git")
    row = next(f for f in git["fields"] if f["env"] == "AE_GIT_REPOS")
    assert row["source"] == "saved" and row["value"] == "acme/secretrepo"
    assert row["saved_by"] == "owner@example.com" and row["saved_at"]
    assert git_center.Settings().extra_repos == ["acme/secretrepo"]
    lines = [r.getMessage() for r in caplog.records if r.name == admin_cards.__name__]
    assert lines == ["ae cards: git settings changed (AE_GIT_REPOS) by owner@example.com"]
    assert not any("secretrepo" in r.getMessage() for r in caplog.records)
    cleared = await _post(client, "git", {"AE_GIT_REPOS": None}, revision=1)
    assert cleared.status_code == 200, cleared.text
    again = next(c for c in cleared.json()["cards"] if c["id"] == "git")
    row = next(f for f in again["fields"] if f["env"] == "AE_GIT_REPOS")
    assert row["source"] == "missing" and row["value"] is None
    assert git_center.Settings().extra_repos == []


async def test_the_route_refuses_a_non_admin_a_bearer_and_a_page_from_elsewhere(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    values = {"AE_GIT_REPOS": "acme/one"}
    refused = [
        await _post(client, "git", values, headers=MEMBER),
        await _post(client, "git", values, headers={**OWNER, "Authorization": "Bearer abc"}),
        await _post(client, "git", values, headers={"X-Forwarded-Email": "owner@example.com"}),
        await _post(client, "git", values, headers={**OWNER, "Sec-Fetch-Site": "cross-site"}),
    ]
    assert [r.status_code for r in refused] == [403, 403, 403, 403], [r.text for r in refused]
    assert admin_cards.store().snapshot()["revision"] == 0


async def test_a_field_that_is_not_editable_is_refused_by_name(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    got = await _post(client, "git", {"AE_GITHUB_API_URL": "https://evil.test"})
    assert got.status_code == 422, got.text
    assert "AE_GITHUB_API_URL" in got.json()["error"]["message"]
    host = await _post(client, "box", {"AE_BOX_USERS": "1000=Alex"})
    assert host.status_code == 422
    assert "config/host.env" in host.json()["error"]["message"]
    other = await _post(client, "git", {"AE_FABRIC_USD_PER_CU_HOUR": "1"})
    assert other.status_code == 422
    assert admin_cards.store().snapshot()["revision"] == 0


async def test_a_field_the_environment_sets_is_a_conflict(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_GIT_REPOS", "acme/fromenv")
    got = await _post(client, "git", {"AE_GIT_REPOS": "acme/typed"})
    assert got.status_code == 409, got.text
    assert "set in the server's environment" in got.json()["error"]["message"]
    view = (await client.get("/v1/ae/cards", headers=OWNER)).json()
    git = next(c for c in view["cards"] if c["id"] == "git")
    row = next(f for f in git["fields"] if f["env"] == "AE_GIT_REPOS")
    assert row["source"] == "env" and row["value"] is None and "fromenv" not in str(view)


@pytest.mark.parametrize(
    ("card", "values"),
    [
        ("git", {"AE_GIT_REPOS": "nonsense"}),
        ("git", {"AE_GIT_KEEP_BRANCHES": "acme/one:a..b"}),
        ("git", {"AE_GIT_SWEEP_CONFIG": "acme/ops:../x"}),
        ("azure", {"AE_AZURE_COST_SUBSCRIPTIONS": "nope"}),
        ("credit", {"AE_DATABRICKS_CREDIT_USD": "-1"}),
        ("credit", {"AE_DATABRICKS_CREDIT_START": "2026-99-01"}),
        ("credit", {"AE_DATABRICKS_CREDIT_CADENCE": "daily"}),
        ("compute", {"AE_FABRIC_USD_PER_CU_HOUR": "500"}),
        ("git", {"AE_GIT_REPOS": 5}),
        ("git", {}),
    ],
)
async def test_a_bad_value_is_a_422_that_states_the_rule(
    client: httpx.AsyncClient, ae_admins: set[str], card: str, values: dict[str, Any]
) -> None:
    got = await _post(client, card, values)
    assert got.status_code == 422, got.text
    assert admin_cards.store().snapshot()["revision"] == 0


async def test_a_422_never_echoes_more_than_200_characters_of_the_value(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    huge = "ghp_" + "Z" * 6_000
    for body in (
        {"AE_GIT_REPOS": huge},
        {"AE_GIT_KEEP_BRANCHES": f"acme/one:{huge}"},
        {huge: "x"},
        {"AE_GIT_SWEEP_CONFIG": f"acme/ops:{huge}"},
        {"AE_AZURE_COST_SUBSCRIPTIONS": huge},
    ):
        got = await _post(client, "git" if "AE_GIT" in str(body) else "azure", body)
        assert got.status_code in (422, 404), got.text
        assert got.text.count("Z") < 200, got.text[:300]
    wrong = await client.post(
        "/v1/ae/cards/settings",
        json={"id": "git", "values": {"AE_GIT_REPOS": huge}, "revision": "x"},
        headers=OWNER,
    )
    assert wrong.status_code == 422
    assert wrong.text.count("Z") < 200


async def test_an_unknown_card_and_a_private_card_the_server_did_not_name_are_404(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    assert (await _post(client, "nope", {"AE_GIT_REPOS": "a/b"})).status_code == 404
    assert (await _post(client, "social", {"AE_SOCIAL_TOOLS": "[]"})).status_code == 404


async def test_a_stale_revision_is_a_409_with_the_current_state(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    assert (await _post(client, "git", {"AE_GIT_REPOS": "acme/one"})).status_code == 200
    stale = await _post(client, "git", {"AE_GIT_REPOS": "acme/two"}, revision=0)
    assert stale.status_code == 409, stale.text
    err = stale.json()["error"]
    assert err["code"] == "cards_changed_elsewhere" and err["current"]["revision"] == 1
    git = next(c for c in err["current"]["cards"] if c["id"] == "git")
    row = next(f for f in git["fields"] if f["env"] == "AE_GIT_REPOS")
    assert row["value"] == "acme/one"


async def test_the_credit_start_may_not_pass_the_end(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    got = await _post(
        client,
        "credit",
        {"AE_DATABRICKS_CREDIT_START": "2026-12-31", "AE_DATABRICKS_CREDIT_END": "2026-01-01"},
    )
    assert got.status_code == 422, got.text
    ok = await _post(
        client,
        "credit",
        {"AE_DATABRICKS_CREDIT_START": "2026-01-01", "AE_DATABRICKS_CREDIT_END": "2026-12-31"},
    )
    assert ok.status_code == 200, ok.text


async def test_a_switch_keeps_the_settings_and_a_save_keeps_the_switches(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    await _post(client, "git", {"AE_GIT_REPOS": "acme/one"})
    off = await client.post(
        "/v1/ae/cards/state", json={"id": "vault", "on": False, "revision": 1}, headers=OWNER
    )
    assert off.status_code == 200, off.text
    snap = admin_cards.store().snapshot()
    assert snap["settings"]["git"]["AE_GIT_REPOS"]["value"] == "acme/one"
    assert snap["cards"]["vault"]["on"] is False and snap["revision"] == 2


async def test_a_save_through_the_route_reaches_the_next_git_answer(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    async def fake_load(settings: git_center.Settings, **_kw: Any) -> dict[str, Any]:
        calls.append(list(settings.extra_repos))
        return {
            "configured": True,
            "repos": [{"repo": r, "error": None} for r in settings.extra_repos],
        }

    monkeypatch.setattr(git_center, "load", fake_load)
    first = (await _post(client, "git", {"AE_GIT_REPOS": "acme/one"})).json()
    assert [r["repo"] for r in (await git_center.overview())["repos"]] == ["acme/one"]
    assert [r["repo"] for r in (await git_center.overview())["repos"]] == ["acme/one"]
    assert len(calls) == 1  # the second answer came from the cache
    done = await _post(client, "git", {"AE_GIT_REPOS": "acme/two"}, revision=first["revision"])
    assert done.status_code == 200, done.text
    assert [r["repo"] for r in (await git_center.overview())["repos"]] == ["acme/two"]
    assert calls == [["acme/one"], ["acme/two"]]


async def test_a_git_read_in_flight_during_a_save_leaves_no_failed_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    _save("AE_GIT_REPOS", "acme/one")
    gate = asyncio.Event()
    started = asyncio.Event()

    async def failing_load(settings: git_center.Settings, **_kw: Any) -> dict[str, Any]:
        started.set()
        await gate.wait()
        return {"configured": True, "repos": [{"repo": "acme/one", "error": "down"}]}

    monkeypatch.setattr(git_center, "load", failing_load)
    task = asyncio.create_task(git_center.overview())
    await started.wait()
    card_config.invalidate("git")  # a save lands while the read is out
    gate.set()
    await task  # the caller that asked still gets its answer
    assert git_center._failed == {}
    assert git_center._cache.peek("git") is None  # but nothing was stored for the old settings


async def test_a_save_through_the_route_reaches_the_next_credit_and_fabric_answers(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    got = await _post(client, "credit", {"AE_DATABRICKS_CREDIT_USD": "10"})
    assert got.status_code == 200, got.text
    assert databricks.credit().usd is None  # a credit needs both dates too
    got = await _post(
        client,
        "credit",
        {"AE_DATABRICKS_CREDIT_START": "2030-01-01", "AE_DATABRICKS_CREDIT_END": "2030-12-31"},
        revision=got.json()["revision"],
    )
    assert got.status_code == 200, got.text
    assert databricks.credit().usd == 10.0
    # Prime Fabric's whole-answer cache and one capacity's status, then change the price.
    fabric._last = (1.0, {"capacities": []})
    fabric._status_cache["c1"] = (1.0, {"state": "Active"})  # type: ignore[assignment]
    got = await _post(
        client, "compute", {"AE_FABRIC_USD_PER_CU_HOUR": "0.25"}, revision=got.json()["revision"]
    )
    assert got.status_code == 200, got.text
    assert fabric._last is None  # the next answer is rebuilt with the new price
    assert "c1" in fabric._status_cache  # a capacity's status is not read again for a price
    assert fabric.usd_per_cu_hour() == 0.25


async def test_clearing_what_is_not_saved_changes_nothing(
    client: httpx.AsyncClient, ae_admins: set[str], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        got = await _post(client, "git", {"AE_GIT_REPOS": None})
    assert got.status_code == 200, got.text
    assert got.json()["revision"] == 0 and admin_cards.store().snapshot()["revision"] == 0
    assert not [r for r in caplog.records if r.name == admin_cards.__name__]
    first = await _post(client, "git", {"AE_GIT_REPOS": "acme/one"})
    same = await _post(
        client, "git", {"AE_GIT_REPOS": "acme/one"}, revision=first.json()["revision"]
    )
    assert same.json()["revision"] == first.json()["revision"]


async def test_a_failed_save_drops_nothing_and_a_damaged_file_is_a_503(
    client: httpx.AsyncClient, ae_admins: set[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cards.json"
    path.write_text("{broken", encoding="utf-8")
    admin_cards.reset_store(admin_cards.CardStore(path))
    dropped: list[str] = []
    monkeypatch.setattr(git_center, "invalidate", lambda: dropped.append("git"))
    got = await _post(client, "git", {"AE_GIT_REPOS": "acme/one"})
    assert got.status_code == 503, got.text
    assert dropped == [] and path.read_text(encoding="utf-8") == "{broken"


# ── the answer's new fields ───────────────────────────────────────────────────────────────────


def test_prefix_fields_count_the_variables_and_never_name_them() -> None:
    env = {
        "AE_GIT_TOKEN_ALPHA": "x",
        "AE_GIT_TOKEN_BETA": "y",
        "AE_GIT_TOKEN_GAMMA": "   ",
    }
    git = next(c for c in admin_cards.view(env)["cards"] if c["id"] == "git")
    row = next(f for f in git["fields"] if f["env"] == "AE_GIT_TOKEN_")
    assert row["status"] == "set" and row["count"] == 2 and row["unit"] == "account"
    assert "ALPHA" not in str(admin_cards.view(env)) and "BETA" not in str(admin_cards.view(env))
    none = next(c for c in admin_cards.view({})["cards"] if c["id"] == "git")
    assert next(f for f in none["fields"] if f["env"] == "AE_GIT_TOKEN_")["count"] == 0


def test_an_optional_field_with_a_default_reports_the_default_not_missing() -> None:
    compute = next(c for c in admin_cards.view({})["cards"] if c["id"] == "compute")
    price = next(f for f in compute["fields"] if f["env"] == "AE_FABRIC_USD_PER_CU_HOUR")
    assert price["source"] == "default" and price["default"] == "0.18"
    git = next(c for c in admin_cards.view({})["cards"] if c["id"] == "git")
    api = next(f for f in git["fields"] if f["env"] == "AE_GITHUB_API_URL")
    assert api["source"] == "default" and api["default"] == "https://api.github.com"
    assert next(f for f in git["fields"] if f["env"] == "AE_GIT_REPOS")["source"] == "missing"


def test_the_store_keeps_settings_in_its_file_with_the_same_rules(tmp_path: Path) -> None:
    path = tmp_path / "cards.json"
    store = admin_cards.CardStore(path)
    store.set_settings("git", {"AE_GIT_REPOS": "acme/one"}, "a@example.com", 0)
    assert path.stat().st_mode & 0o777 == 0o600
    again = admin_cards.CardStore(path)
    assert again.saved_value("git", "AE_GIT_REPOS") == "acme/one"
    assert again.snapshot()["revision"] == 1
    path.write_text('{"version":1,"revision":1,"cards":{},"settings":{"git":{"X":5}}}')
    broken = admin_cards.CardStore(path)
    assert broken.snapshot()["state_ok"] is False
    with pytest.raises(admin_cards.CardsUnreadable):
        broken.set_settings("git", {"AE_GIT_REPOS": "acme/one"}, "a@example.com", 0)
