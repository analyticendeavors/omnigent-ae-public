"""Preset rows with a newer model in the same family (model nudge, 2026-10-26)."""

from __future__ import annotations

import asyncio
import functools
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import newer_models, preset_runs, preset_suggestions, role_prefs
from ae_omni_policies import router as router_module
from ae_omni_policies.role_prefs import RolePrefsStore

OPUS, SONNET = "claude-opus-5-5", "claude-sonnet-5-5"


def _fixed(model: str, effort: str = "high") -> dict[str, Any]:
    return {"mode": "fixed", "model": model, "effort": effort}


def _preset(pid: str = "mine", **roles: Any) -> role_prefs.Preset:
    base: dict[str, Any] = {
        "lead": {"mode": "session"},
        "planner": _fixed(OPUS),
        "reviewer": {"mode": "auto"},
        "builder": _fixed(SONNET, "medium"),
        "helper": {"mode": "auto"},
    }
    base.update(roles)
    return role_prefs.Preset.model_validate(
        {
            "id": pid,
            "title": "Mine",
            "icon": "scale",
            "color": "teal",
            "roles": base,
            "rules": {"raise_or_lower": "tell", "lead_steps_down": True, "auto_raises": 1},
        }
    )


def _claude(*models: str) -> list[dict[str, Any]]:
    return [
        {"id": m.split("-")[1], "model": m, "displayName": m.split("-")[1].title()} for m in models
    ]


def test_new_opus_appears_with_effort_kept() -> None:
    found = newer_models.find([_preset()], _claude("claude-opus-5-6", SONNET))
    assert len(found) == 1
    [row] = found[0]["rows"]
    assert (row["role"], row["current_name"], row["newer_name"]) == (
        "Planner",
        "Opus 5.5",
        "Opus 5.6",
    )
    assert row["value"] == _fixed("claude-opus-5-6")
    assert row["path"] == "roles.planner"


def test_same_version_gives_no_entry() -> None:
    assert newer_models.find([_preset()], _claude(OPUS, SONNET)) == []


def test_major_beats_minor() -> None:
    found = newer_models.find([_preset()], _claude("claude-opus-6-0"))
    assert found[0]["rows"][0]["newer_name"] == "Opus 6.0"


def test_auto_and_session_rows_are_skipped() -> None:
    preset = _preset(planner={"mode": "auto"}, builder={"mode": "auto"})
    assert newer_models.find([preset], _claude("claude-opus-5-9", "claude-sonnet-5-9")) == []


def test_unparseable_id_is_ignored() -> None:
    preset = _preset(planner=_fixed("some-custom-model"))
    found = newer_models.find(
        [preset], [*_claude("claude-opus-5-9"), {"id": "weird", "model": "weird"}]
    )
    assert found == []


def test_other_family_does_not_count() -> None:
    assert newer_models.find([_preset()], _claude("claude-haiku-9-0")) == []


def test_codex_family_step_by_generation() -> None:
    slot = {"mode": "fixed", "model": "gpt-5.6-astra", "effort": "medium"}
    preset = _preset(reviewer=slot)
    catalog = [
        {"id": "gpt-6-astra", "displayName": "GPT-6-Astra"},
        {"id": "gpt-6-sol", "displayName": "GPT-6-Sol"},
    ]
    [row] = newer_models.find([preset], catalog)[0]["rows"]
    assert (row["role"], row["current_name"], row["newer"], row["newer_name"]) == (
        "Reviewer",
        "GPT-5.6-Astra",
        "gpt-6-astra",
        "GPT-6-Astra",
    )


def test_codex_reviewer_agent_slot_keeps_its_fields() -> None:
    slot = {
        "mode": "multi",
        "reviewers": [
            {"mode": "auto"},
            {
                "mode": "agent",
                "agent": "abc",
                "name": "Codex",
                "model": "gpt-5.6-sol",
                "effort": "low",
            },
        ],
    }
    found = newer_models.find(
        [_preset(reviewer=slot)], [{"id": "gpt-6-sol", "displayName": "GPT-6-Sol"}]
    )
    [row] = found[0]["rows"]
    assert row["path"] == "roles.reviewer.reviewers.1"
    assert row["value"]["model"] == "gpt-6-sol" and row["value"]["agent"] == "abc"
    assert row["value"]["effort"] == "low"


def test_codex_builder_agent_slot_gets_an_update_row() -> None:
    slot = {
        "mode": "agent",
        "agent": role_prefs.CODEX_AGENT_ID,
        "agent_name": role_prefs.CODEX_AGENT_NAME,
        "name": "Codex",
        "model": "gpt-5.6-sol",
        "effort": "low",
    }
    found = newer_models.find(
        [_preset(builder=slot)], [{"id": "gpt-6-sol", "displayName": "GPT-6-Sol"}]
    )
    [row] = found[0]["rows"]
    assert (row["role"], row["path"]) == ("Builder", "roles.builder")
    assert row["value"]["model"] == "gpt-6-sol" and row["value"]["mode"] == "agent"


def test_codex_same_generation_no_entry() -> None:
    slot = {"mode": "fixed", "model": "gpt-6-sol", "effort": "low"}
    assert newer_models.find([_preset(reviewer=slot)], [{"id": "gpt-6-sol"}]) == []


def test_empty_catalog_returns_nothing() -> None:
    assert newer_models.find([_preset()], []) == []


def test_summary_counts_presets() -> None:
    found = newer_models.find(
        [_preset("a"), _preset("b", builder={"mode": "auto"})], _claude("claude-opus-5-6")
    )
    assert newer_models.summary(found) == [
        {"model": "claude-opus-5-6", "name": "Opus 5.6", "from": ["Opus 5.5"], "presets": 2}
    ]


# -- parse and compare


def test_date_suffixes_are_not_versions() -> None:
    assert newer_models.parse("claude-opus-4-20250514") == ("claude", "opus", (4, 0))
    assert newer_models.parse("claude-opus-4-1-20250805") == ("claude", "opus", (4, 1))


def test_trailing_zeros_do_not_make_a_newer_version() -> None:
    slot = {"mode": "fixed", "model": "gpt-6.0-astra", "effort": "low"}
    preset = _preset(reviewer=slot)
    assert newer_models.find([preset], [{"id": "gpt-6.0.0-astra"}]) == []
    older = _preset(reviewer={"mode": "fixed", "model": "gpt-5.9-astra", "effort": "low"})
    assert newer_models.find([older], [{"id": "gpt-6.0.0-astra"}])


def test_codex_tie_keeps_the_first_entry() -> None:
    best = newer_models.newest([{"id": "gpt-6-sol"}, {"id": "gpt-6.0-sol"}])
    assert best[("codex", "sol")]["model"] == "gpt-6-sol"


def test_codex_variants_are_their_own_family() -> None:
    mini = {"mode": "fixed", "model": "gpt-5-astra-mini", "effort": "low"}
    catalog = [{"id": "gpt-6-astra"}, {"id": "gpt-6-astra-mini"}]
    [row] = newer_models.find([_preset(reviewer=mini)], catalog)[0]["rows"]
    assert row["newer"] == "gpt-6-astra-mini"
    plain = {"mode": "fixed", "model": "gpt-5-astra", "effort": "low"}
    [row] = newer_models.find([_preset(reviewer=plain)], catalog)[0]["rows"]
    assert row["newer"] == "gpt-6-astra"
    assert newer_models.find([_preset(reviewer=mini)], [{"id": "gpt-6-astra"}]) == []


def test_missing_family_gives_no_entry() -> None:
    assert newer_models.find([_preset()], _claude("claude-fable-9-0")) == []


def test_an_alias_is_never_written_as_a_model() -> None:
    best = newer_models.newest([{"id": "opus", "displayName": "Opus 5.9"}])
    assert best == {}


def test_variant_row_moves_only_to_the_same_variant() -> None:
    preset = _preset(planner=_fixed("claude-opus-5-5[1m]"))
    skipped: list[dict[str, Any]] = []
    assert newer_models.find([preset], _claude("claude-opus-5-6"), skipped) == []
    assert skipped == [
        {
            "preset": "mine",
            "title": "Mine",
            "role": "Planner",
            "reason": "Opus 5.6 has no [1m] variant in the catalog",
        }
    ]
    catalog = [
        {"id": "opus", "model": "claude-opus-5-6"},
        {"id": "claude-opus-5-6[1m]", "model": "claude-opus-5-6"},
    ]
    [row] = newer_models.find([preset], catalog)[0]["rows"]
    assert row["newer"] == "claude-opus-5-6[1m]"
    [plain] = newer_models.find([_preset()], catalog)[0]["rows"]
    assert plain["newer"] == "claude-opus-5-6"


# -- the catalog


def _http(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://t")


def _hosts(*ids: str) -> dict[str, Any]:
    return {"hosts": [{"host_id": i, "status": "online"} for i in ids]}


def test_fetch_catalog_reads_hosts_at_once_and_caches_by_host_set() -> None:
    newer_models.reset_cache()
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/v1/hosts":
            return httpx.Response(
                200,
                json={
                    "hosts": [
                        {"host_id": "h1", "status": "online"},
                        {"host_id": "h2", "status": "offline"},
                    ]
                },
            )
        if "claude-native" in request.url.path:
            return httpx.Response(200, json={"models": _claude("claude-opus-5-6")})
        return httpx.Response(502, json={"detail": "nope"})

    async def run() -> list[dict[str, Any]]:
        async with _http(handler) as http:
            first = await newer_models.fetch_catalog(http, now=100.0)
            again = await newer_models.fetch_catalog(http, now=200.0)
            assert again is first
            return first

    models = asyncio.run(run())
    assert [m["model"] for m in models] == ["claude-opus-5-6"]
    assert not any("h2" in c for c in calls)
    assert sum("model-options" in c for c in calls) == 4  # one host, four harness names


def test_cache_is_shared_between_users_and_bounded() -> None:
    newer_models.reset_cache()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/hosts":
            return httpx.Response(200, json=_hosts("h1"))
        return httpx.Response(200, json={"models": _claude("claude-opus-5-6")})

    async def run() -> None:
        async with _http(handler) as one, _http(handler) as two:
            first = await newer_models.fetch_catalog(one, now=1.0)
            assert await newer_models.fetch_catalog(two, now=2.0) is first

    asyncio.run(run())
    for n in range(newer_models.CACHE_MAX + 4):
        newer_models._cache[(f"h{n}",)] = (float(n), [{"id": "x"}])
        while len(newer_models._cache) > newer_models.CACHE_MAX:
            break
    newer_models.reset_cache()


def test_empty_answers_are_not_cached() -> None:
    newer_models.reset_cache()
    state = {"models": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/hosts":
            return httpx.Response(200, json=_hosts("h1"))
        return httpx.Response(200, json={"models": state["models"]})

    async def run() -> None:
        async with _http(handler) as http:
            assert await newer_models.fetch_catalog(http, now=1.0) == []
            state["models"] = _claude("claude-opus-5-6")
            assert await newer_models.fetch_catalog(http, now=2.0)

    asyncio.run(run())


def test_concurrent_cold_calls_share_one_read() -> None:
    newer_models.reset_cache()
    counts = {"options": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/hosts":
            return httpx.Response(200, json=_hosts("h1"))
        counts["options"] += 1
        return httpx.Response(200, json={"models": _claude("claude-opus-5-6")})

    async def run() -> None:
        async with _http(handler) as http:
            await asyncio.gather(*(newer_models.fetch_catalog(http, now=1.0) for _ in range(5)))

    asyncio.run(run())
    assert counts["options"] == 4


def test_catalog_down_returns_empty_not_error() -> None:
    newer_models.reset_cache()

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    async def run() -> list[dict[str, Any]]:
        async with _http(handler) as http:
            return await newer_models.catalog_soft(http)

    assert asyncio.run(run()) == []


def test_a_slow_catalog_times_out_to_empty() -> None:
    newer_models.reset_cache()

    async def slow(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/hosts":
            return httpx.Response(200, json=_hosts("h1"))
        await asyncio.sleep(5)
        return httpx.Response(200, json={"models": _claude("claude-opus-5-6")})

    async def run() -> list[dict[str, Any]]:
        async with _http(slow) as http:
            return await newer_models.catalog_soft(http, timeout=0.2)

    assert asyncio.run(run()) == []


# -- the review's suggestions


def _found(owner: str, *ids: str, catalog: str = "claude-opus-5-6") -> dict[str, Any]:
    return {owner: newer_models.find([_preset(i) for i in ids], _claude(catalog))}


def test_step_items_wording_and_skips() -> None:
    found = _found("u", "mine", "balanced")
    runs = [
        {"preset": "mine", "user": "u", "raises": 1},
        {"preset": "mine", "user": "u"},
        {"preset": "mine", "user": "other", "raises": 1},
    ]
    items = newer_models.step_items(found, runs=runs, starter_keys={("u", "balanced")})
    assert len(items) == 1
    item = items[0]
    assert item["title"] == "Move Planner on Mine from Opus 5.5 to Opus 5.6"
    assert "Effort stays High." in item["why"]
    assert "2 runs on this preset in the last 14 days, 1 with" in item["why"]
    assert "—" not in item["why"] and "--" not in item["why"]
    assert "runs" not in newer_models.step_items(found)[0]["why"]


def test_open_and_dismissed_records_are_not_filed_again() -> None:
    found = _found("u", "mine")
    [item] = newer_models.step_items(found)
    for status in ("open", "dismissed", "applying", "undone"):
        record = {"owner": "u", "preset_id": "mine", "status": status, "changes": item["changes"]}
        assert newer_models.step_items(found, records=[record]) == []
    other = {"owner": "v", "preset_id": "mine", "status": "dismissed", "changes": item["changes"]}
    assert len(newer_models.step_items(found, records=[other])) == 1


def test_two_owners_do_not_mix_runs_starters_or_slots() -> None:
    found = {**_found("ann", "balanced", "deep"), **_found("bob", "balanced")}
    runs = [
        {"preset": "balanced", "user": "ann"},
        {"preset": "balanced", "user": "ann"},
        {"preset": "balanced", "user": "bob"},
    ]
    items = newer_models.step_items(found, runs=runs, starter_keys={("bob", "balanced")})
    assert [(i["owner"], i["preset_id"]) for i in items] == [
        ("ann", "balanced"),
        ("ann", "deep"),
    ]
    assert "2 runs on this preset" in items[0]["why"]
    both = newer_models.step_items(found, runs=runs)
    assert [(i["owner"], i["preset_id"]) for i in both] == [
        ("ann", "balanced"),
        ("bob", "balanced"),
        ("ann", "deep"),
    ]
    assert "1 run on this preset" in both[1]["why"]


def test_step_item_validates_and_applies() -> None:
    preset = _preset("mine")
    [raw] = newer_models.step_items(_found("u", "mine"))
    item = preset_suggestions.Item.model_validate(raw)
    after = preset_suggestions.apply_changes(preset.model_dump(mode="json"), item.changes)
    assert after.roles.planner.model == "claude-opus-5-6"
    assert after.roles.planner.effort == "high"
    assert after.roles.builder.model == SONNET


# -- the routes


@pytest.fixture
def stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    newer_models.reset_cache()
    role_prefs.reset_store(RolePrefsStore(tmp_path / "roles.json"))
    preset_suggestions.reset_store(preset_suggestions.SuggestionStore(tmp_path / "s.json"))
    monkeypatch.setenv(preset_runs.FILE_ENV, str(tmp_path / "runs.jsonl"))


def _client(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    @asynccontextmanager
    async def client(_request: Any) -> Any:
        async with _http(handler) as http:
            yield http

    monkeypatch.setattr(router_module, "_loopback_client", client)


def _catalog_handler(models: list[dict[str, Any]], status: int = 200) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/hosts":
            return httpx.Response(200, json=_hosts("h"))
        if "claude-native" in request.url.path:
            return httpx.Response(status, json={"models": models})
        return httpx.Response(409)

    return handler


def _body(items: list[dict[str, Any]], summary: str = "ok") -> preset_suggestions.SubmitBody:
    return preset_suggestions.SubmitBody.model_validate({"summary": summary, "suggestions": items})


def _save(user: str, *presets: role_prefs.Preset) -> None:
    role_prefs.store().put(
        user,
        role_prefs.RolesPut.model_validate(
            {
                "presets": [p.model_dump(mode="json") for p in presets],
                "default_preset": presets[0].id,
                "apply_start_on": True,
                "revision": role_prefs.store().get(user).revision,
            }
        ),
    )


def test_route_is_empty_without_a_catalog(monkeypatch: pytest.MonkeyPatch, stores: None) -> None:
    _client(monkeypatch, _catalog_handler([], 502))
    answer = asyncio.run(router_module.roles_newer_models(None, "u"))  # type: ignore[arg-type]
    assert answer == {"presets": [], "models": []}


def test_route_lists_rows_and_times_out_soft(
    monkeypatch: pytest.MonkeyPatch, stores: None
) -> None:
    _client(monkeypatch, _catalog_handler(_claude("claude-opus-5-9")))
    answer = asyncio.run(router_module.roles_newer_models(None, "u"))  # type: ignore[arg-type]
    assert answer["models"][0]["name"] == "Opus 5.9"
    assert answer["presets"][0]["rows"][0]["newer"] == "claude-opus-5-9"
    newer_models.reset_cache()
    monkeypatch.setattr(newer_models, "OVERALL_TIMEOUT_S", 0.1)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return httpx.Response(200, json=_hosts("h"))

    _client(monkeypatch, slow)
    monkeypatch.setattr(
        newer_models,
        "catalog_soft",
        functools.partial(newer_models.catalog_soft, timeout=0.1),
    )
    answer = asyncio.run(router_module.roles_newer_models(None, "u"))  # type: ignore[arg-type]
    assert answer == {"presets": [], "models": []}


def test_admin_review_compares_only_the_owners_presets(
    monkeypatch: pytest.MonkeyPatch, stores: None
) -> None:
    _save("ann", _preset("mine"))
    _save("bob", _preset("theirs"))
    _client(monkeypatch, _catalog_handler(_claude("claude-opus-5-6")))
    found, _skipped, checked = asyncio.run(router_module._newer_for_review(None, "ann"))  # type: ignore[arg-type]
    assert checked is True
    assert list(found) == ["ann"]
    assert [p["id"] for p in found["ann"]] == ["mine"]


def test_body_with_steps_keeps_three_then_the_reviewers_items() -> None:
    preset = _preset("mine", helper=_fixed("claude-haiku-5-5", "low"))
    catalog = _claude("claude-opus-5-6", "claude-sonnet-5-6", "claude-haiku-5-6")
    found = {"u": newer_models.find([preset], catalog)}
    cards = [
        {"kind": "no_change", "preset_id": "mine", "title": f"Keep {n}", "why": "fine"}
        for n in range(5)
    ]
    out = router_module._with_model_steps(_body(cards, "Looked at runs."), found, {"u": [preset]})
    kinds = [i.kind for i in out.suggestions]
    assert kinds == ["own", "own", "own", "no_change", "no_change"]
    assert [i.title for i in out.suggestions[3:]] == ["Keep 0", "Keep 1"]


def test_body_with_four_steps_says_what_is_held_back() -> None:
    first = _preset("a", helper=_fixed("claude-haiku-5-5", "low"))
    second = _preset("b")
    catalog = _claude("claude-opus-5-6", "claude-sonnet-5-6", "claude-haiku-5-6")
    found = {"u": newer_models.find([first, second], catalog)}
    out = router_module._with_model_steps(_body([]), found, {"u": [first, second]})
    assert len(out.suggestions) == 3
    assert out.summary.endswith("2 more model updates next week.")


def test_body_reports_rows_that_could_not_move() -> None:
    preset = _preset("mine", planner=_fixed("claude-opus-5-5[1m]"))
    skipped: list[dict[str, Any]] = []
    found = newer_models.find([preset], _claude("claude-opus-5-6"), skipped)
    assert found == [] and skipped
    out = router_module._with_model_steps(_body([], "Done."), {}, {"u": [preset]}, skipped)
    assert (
        "Not moved: Planner on Mine, Opus 5.6 has no [1m] variant in the catalog." in out.summary
    )


def test_body_skips_a_step_the_reviewer_filed_and_a_dismissed_one(tmp_path: Any) -> None:
    preset = _preset("mine")
    store = preset_suggestions.SuggestionStore(tmp_path / "s.json")
    preset_suggestions.reset_store(store)
    found = {"u": newer_models.find([preset], _claude("claude-opus-5-6"))}
    own = {
        "kind": "own",
        "owner": "u",
        "preset_id": "mine",
        "title": "Use Opus 5.6 for planning",
        "why": "better",
        "saving": "none",
        "changes": [{"path": "roles.planner", "value": _fixed("claude-opus-5-6")}],
    }
    out = router_module._with_model_steps(_body([own]), found, {"u": [preset]})
    assert len(out.suggestions) == 1


def test_a_dismissed_step_stays_dismissed_beyond_the_newest_hundred(tmp_path: Any) -> None:
    store = preset_suggestions.SuggestionStore(tmp_path / "s.json")
    preset_suggestions.reset_store(store)
    old = {
        "id": "sg-old",
        "owner": "u",
        "preset_id": "mine",
        "status": "dismissed",
        "changes": [{"path": "roles.planner", "value": _fixed("claude-opus-5-6")}],
    }
    filler = [
        {"id": f"sg{n}", "owner": "z", "preset_id": "x", "status": "open", "changes": []}
        for n in range(150)
    ]
    store._data["suggestions"].extend([old, *filler])
    assert len(store.list_for(None)[0]) == preset_suggestions.LIST_LIMIT
    assert any(r["id"] == "sg-old" for r in store.all_suggestions())
    preset = _preset("mine")
    found = {"u": newer_models.find([preset], _claude("claude-opus-5-6"))}
    out = router_module._with_model_steps(_body([]), found, {"u": [preset]})
    assert [i.kind for i in out.suggestions] == []


def test_agent_yaml_tells_the_reviewer_about_newer_models() -> None:
    text = (Path(__file__).resolve().parents[3] / "agents/preset-reviewer/agent.yaml").read_text()
    assert '"newer_models"' in text and "Move <role> on <preset> from <old> to <new>" in text
    assert "never propose a model that is not in" in text


def _slow_handler(delay: float, calls: dict[str, int]) -> Any:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/hosts":
            return httpx.Response(200, json=_hosts("h1"))
        calls["options"] += 1
        await asyncio.sleep(delay)
        return httpx.Response(200, json={"models": _claude("claude-opus-5-6")})

    return handler


def test_a_timed_out_caller_still_warms_the_cache() -> None:
    newer_models.reset_cache()
    calls = {"options": 0}
    handler = _slow_handler(0.3, calls)

    @asynccontextmanager
    async def factory() -> Any:
        async with _http(handler) as own:
            yield own

    async def run() -> None:
        async with _http(handler) as first:  # closes when this caller gives up
            assert await newer_models.catalog_soft(first, factory, timeout=0.05) == []
        await asyncio.sleep(0.6)  # the shared read finishes on its own client
        async with _http(handler) as second:
            warm = await newer_models.catalog_soft(second, factory, timeout=0.05)
        assert {m["model"] for m in warm} == {"claude-opus-5-6"}

    asyncio.run(run())
    assert calls["options"] == 4


def test_a_second_caller_joins_the_read_in_flight() -> None:
    newer_models.reset_cache()
    calls = {"options": 0}
    handler = _slow_handler(0.3, calls)

    @asynccontextmanager
    async def factory() -> Any:
        async with _http(handler) as own:
            yield own

    async def run() -> list[dict[str, Any]]:
        async with _http(handler) as first, _http(handler) as second:
            gone = asyncio.create_task(newer_models.catalog_soft(first, factory, timeout=0.05))
            await asyncio.sleep(0.1)
            joined = await newer_models.catalog_soft(second, factory, timeout=2.0)
            assert await gone == []
            return joined

    assert {m["model"] for m in asyncio.run(run())} == {"claude-opus-5-6"}
    assert calls["options"] == 4


def test_review_check_gives_up_quickly_and_says_unchecked(
    monkeypatch: pytest.MonkeyPatch, stores: None
) -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=_hosts("h"))

    _client(monkeypatch, hang)
    assert router_module.SUBMIT_CATALOG_WAIT_S <= 2.0
    found, _moved, checked = asyncio.run(
        router_module._newer_for_review(None, "u", 0.1)  # type: ignore[arg-type]
    )
    assert (found, checked) == ({}, False)
