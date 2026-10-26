"""Preset insights (2026-10-24, PR 2): ``preset_stats`` and ``GET /v1/ae/presets``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ae_omni_policies import labels, model_log, preset_runs, preset_stats

NOW = 10_000_000.0
DAY = 86400.0


@pytest.fixture(autouse=True)
def _files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    log = tmp_path / "runs.jsonl"
    monkeypatch.setenv(preset_runs.FILE_ENV, str(log))
    monkeypatch.setenv(model_log.FILE_ENV, str(tmp_path / "model.jsonl"))
    monkeypatch.setenv(preset_stats.STATE_FILE_ENV, str(tmp_path / "stats.json"))
    preset_stats.reset()
    yield log
    preset_stats.reset()


def line(
    n: int,
    *,
    preset: str = "balanced",
    cost: float = 2.0,
    wall: float = 1800,
    age_days: float = 1,
    rev: int = 0,
    agents: list[dict[str, Any]] | None = None,
    changes: list[dict[str, Any]] | None = None,
    run_key: str | None = None,
    review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "session": f"s{n}",
        "user": "reid@example.com",
        "title": f"Run {n}",
        "preset": preset,
        "run_key": run_key or f"s{n}:{n}:review",
        "rev": rev,
        "wall_s": wall,
        "active_s": None,
        "cost": {"total_usd": cost, "by_model": {"claude-opus-5-5": {"usd": cost}}},
        "agents": agents or [],
        "model_changes": changes or [],
        "review": review or {"reported": False, "reviewer_sessions": 0},
        "at": int(NOW - age_days * DAY),
    }


def write(path: Path, lines: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def hung(role: str, kind: str = "session") -> dict[str, Any]:
    return {"session": "x", "kind": kind, "role": role, "hung": True}


# -- log mode


def test_log_mode_aggregates_one_preset(_files: Path) -> None:
    write(
        _files,
        [
            line(1, cost=1.0, wall=600),
            line(2, cost=3.0, wall=1200, agents=[{"role": "builder"}, {"role": "builder"}]),
            line(
                3,
                cost=5.0,
                wall=1800,
                changes=[
                    {"session": "s3", "kind": "raise", "from": {}, "to": {}},
                    {"session": "s3", "kind": "lower", "from": {}, "to": {}},
                    {"session": "s3", "kind": "lower", "from": {}, "to": {}},
                ],
            ),
        ],
    )
    view = preset_stats.compute(30, now=NOW)
    assert view["mode"] == "log" and view["state"] == "ready"
    assert view["kpis"]["runs"] == 3 and view["kpis"]["families"] == 1
    assert view["kpis"]["cost"] == 9.0
    assert view["kpis"]["raises"] == 1 and view["kpis"]["step_downs"] == 2
    (row,) = view["presets"]
    assert row["runs"] == 3 and row["median_cost"] == 3.0 and row["median_wall_s"] == 1200
    assert row["cost_by_model"] == {"claude-opus-5-5": 9.0}
    assert len(row["daily"]) == 30 and sum(row["daily"]) == 9.0
    assert [r["title"] for r in row["recent"]] == ["Run 1", "Run 2", "Run 3"]
    assert row["recent"][1]["agents"] == "builder x2"


def test_highest_rev_wins_and_rotation_file_is_read(_files: Path) -> None:
    write(_files, [line(1, cost=1.0, rev=0, run_key="k"), line(1, cost=9.0, rev=1, run_key="k")])
    write(_files.with_name(_files.name + ".1"), [line(2, cost=4.0)])
    view = preset_stats.compute(30, now=NOW)
    assert view["kpis"]["runs"] == 2
    assert view["kpis"]["cost"] == 13.0


def test_window_filter_and_prior_comparison(_files: Path) -> None:
    write(
        _files,
        [
            line(1, cost=6.0, wall=1200, age_days=2),
            line(2, cost=4.0, wall=600, age_days=10),  # prior window of 7 d
            line(3, cost=50.0, age_days=40),  # outside both
        ],
    )
    view = preset_stats.compute(7, now=NOW)
    assert view["kpis"]["runs"] == 1 and view["kpis"]["cost"] == 6.0
    assert view["kpis"]["cost_change_pct"] == 50
    assert view["kpis"]["wall_change_s"] == 600


def test_review_agents_changes_and_flags_text(_files: Path) -> None:
    agents = [
        {"session": "a", "kind": "session", "role": "planner"},
        {"session": "b", "kind": "session", "role": "builder"},
        {"session": "c", "kind": "codex", "role": "reviewer", "hung": True},
    ]
    changes = [
        {
            "session": "b",
            "kind": "lower",
            "from": {"model": "claude-sonnet-5-5"},
            "to": {"model": "claude-haiku-5-5"},
        }
    ]
    review = {"reported": True, "reviewers_run": 2, "findings_returned": 6, "findings_fixed": 5}
    write(_files, [line(1, agents=agents, changes=changes, review=review)])
    (run,) = preset_stats.compute(30, now=NOW)["presets"][0]["recent"]
    assert run["agents"] == "planner, builder, Codex"
    assert run["model_changes"] == ["builder Sonnet to Haiku"]
    assert run["review"] == "2 reviewers, 6 findings, 5 fixed"
    assert run["flags"] == ["Codex reviewer hung"]
    assert run["active_s"] is None


# -- takeaways


def _runs(preset: str, count: int, start: int, **kw: Any) -> list[dict[str, Any]]:
    return [line(start + i, preset=preset, **kw) for i in range(count)]


def test_cost_takeaway_says_costlier_and_no_faster(_files: Path) -> None:
    write(
        _files,
        _runs("deep", 5, 0, cost=9.8, wall=2460) + _runs("balanced", 5, 10, cost=3.1, wall=2340),
    )
    got = preset_stats.compute(30, now=NOW)["takeaways"]
    assert got[0]["text"].startswith("deep costs 3.2x the rest") or "x the rest" in got[0]["text"]
    assert got[0]["text"].endswith("and finishes no faster")
    assert "5 runs" in got[0]["why"] and "$9.80" in got[0]["why"] and "$3.10" in got[0]["why"]
    assert got[0]["preset"] == "deep"


def test_cost_takeaway_faster_clause(_files: Path) -> None:
    write(
        _files,
        _runs("deep", 5, 0, cost=9.0, wall=800) + _runs("balanced", 5, 10, cost=3.0, wall=1000),
    )
    texts = [t["text"] for t in preset_stats.compute(30, now=NOW)["takeaways"]]
    assert "deep costs 3.0x the rest and finishes 20% faster" in texts


def test_takeaway_thresholds_at_the_edge(_files: Path) -> None:
    # Exactly 25% above is enough; 4 runs of the preset is not.
    write(_files, _runs("a", 5, 0, cost=5.0) + _runs("b", 5, 10, cost=4.0))
    assert any(t["id"] == "cost:a" for t in preset_stats.compute(30, now=NOW)["takeaways"])
    write(_files, _runs("a", 4, 0, cost=5.0) + _runs("b", 5, 10, cost=4.0))
    assert not any(t["id"] == "cost:a" for t in preset_stats.compute(30, now=NOW)["takeaways"])
    write(_files, _runs("a", 5, 0, cost=4.99) + _runs("b", 5, 10, cost=4.0))
    assert not any(t["id"] == "cost:a" for t in preset_stats.compute(30, now=NOW)["takeaways"])


def test_rest_needs_five_runs(_files: Path) -> None:
    write(_files, _runs("a", 5, 0, cost=9.0) + _runs("b", 4, 10, cost=1.0))
    assert not any(t["id"] == "cost:a" for t in preset_stats.compute(30, now=NOW)["takeaways"])


def test_time_rule_fires_only_when_cost_rule_did_not(_files: Path) -> None:
    write(_files, _runs("a", 5, 0, cost=2.0, wall=3000) + _runs("b", 5, 10, cost=2.0, wall=1000))
    ids = [t["id"] for t in preset_stats.compute(30, now=NOW)["takeaways"]]
    assert "time:a" in ids and "cost:a" not in ids
    both = _runs("a", 5, 0, cost=9.0, wall=3000) + _runs("b", 5, 10, cost=2.0, wall=1000)
    write(_files, both)
    ids = [t["id"] for t in preset_stats.compute(30, now=NOW)["takeaways"]]
    assert "cost:a" in ids and "time:a" not in ids


def test_hang_takeaway_needs_three_runs(_files: Path) -> None:
    runs = [
        line(i, preset="p" if i % 2 else "q", agents=[hung("reviewer", "codex")]) for i in range(3)
    ]
    write(_files, runs)
    got = preset_stats.compute(30, now=NOW)["takeaways"]
    assert [t["text"] for t in got] == ["Codex reviewer hung 3 times"]
    assert "p (1)" in got[0]["why"] or "q (2)" in got[0]["why"]
    write(_files, runs[:2])
    assert preset_stats.compute(30, now=NOW)["takeaways"] == []


def test_at_most_three_takeaways_largest_gap_first(_files: Path) -> None:
    lines = _runs("a", 5, 0, cost=20.0, wall=1000) + _runs("b", 5, 10, cost=2.0, wall=1000)
    lines += [
        line(40 + i, preset="b", agents=[hung(r)])
        for i, r in enumerate(["x"] * 3 + ["y"] * 3 + ["z"] * 3)
    ]
    write(_files, lines)
    got = preset_stats.compute(30, now=NOW)["takeaways"]
    assert len(got) == 3
    assert got[0]["id"] == "cost:a"


# -- tokens


def tok_line(n: int, tokens: Any, **kw: Any) -> dict[str, Any]:
    """A run line whose by_model buckets carry *tokens*: an int (total_tokens) or a dict."""
    out = line(n, **kw)
    bucket = {"usd": out["cost"]["total_usd"]}
    bucket.update(tokens if isinstance(tokens, dict) else {"total_tokens": tokens})
    out["cost"]["by_model"] = {"claude-opus-5-5": bucket}
    return out


def test_run_tokens_use_input_plus_output_else_total() -> None:
    both = tok_line(1, 0, preset="a")
    both["cost"]["by_model"] = {
        # a native Claude bucket: total_tokens also holds the cache reads, which are not counted
        "m1": {"total_tokens": 1000, "input_tokens": 1, "output_tokens": 1},
        "m2": {"input_tokens": 300, "output_tokens": 200},
        "m3": {"total_tokens": 700},
        "m4": {"usd": 1.0},
    }
    run = preset_stats.run_from_line(both)
    assert run["tokens"] == 1202
    assert run["tokens_by_model"] == {"m1": 2, "m2": 500, "m3": 700}
    assert preset_stats.run_from_line(line(2))["tokens"] is None


def test_tokens_totals_series_and_change_vs_prior_window(_files: Path) -> None:
    write(
        _files,
        [
            tok_line(1, 1_000_000, age_days=1),
            tok_line(2, 3_000_000, age_days=1),
            tok_line(3, {"input_tokens": 400, "output_tokens": 100}, age_days=3),
            tok_line(4, 2_000_000, age_days=9),  # prior window of the 7 day view
            line(5, age_days=1),  # no tokens recorded
        ],
    )
    view = preset_stats.compute(7, now=NOW)
    assert view["kpis"]["tokens"] == 4_000_500
    assert view["kpis"]["tokens_change_pct"] == 100
    preset = view["presets"][0]
    assert preset["total_tokens"] == 4_000_500
    assert preset["median_tokens"] == 1_000_000
    assert preset["tokens_by_model"] == {"claude-opus-5-5": 4_000_500}
    assert sum(preset["daily_tokens"]) == 4_000_500 and len(preset["daily_tokens"]) == 7
    assert {r["session"]: r["tokens"] for r in preset["recent"]}["s2"] == 3_000_000
    assert {r["session"]: r["tokens"] for r in preset["recent"]}["s5"] is None
    assert view["kpis"]["cost"] > 0  # cost still there beside it


def test_tokens_change_is_none_without_a_prior_window(_files: Path) -> None:
    write(_files, [tok_line(1, 500)])
    assert preset_stats.compute(7, now=NOW)["kpis"]["tokens_change_pct"] is None


def test_tokens_takeaway_and_metric_tags(_files: Path) -> None:
    write(
        _files,
        [tok_line(i, 9_000_000, preset="deep", cost=2.0) for i in range(5)]
        + [tok_line(10 + i, 3_000_000, preset="balanced", cost=2.0) for i in range(5)],
    )
    got = preset_stats.compute(30, now=NOW)["takeaways"]
    tokens = [t for t in got if t["metric"] == "tokens"]
    assert [t["id"] for t in tokens] == ["tokens:deep", "tokens:balanced"] or tokens[0][
        "id"
    ] == "tokens:deep"
    assert tokens[0]["text"] == "deep uses 3.0x the tokens of the rest"
    assert "9.0M tokens against 3.0M" in tokens[0]["why"]
    assert all(t["metric"] in {"cost", "tokens", "time", "hang"} for t in got)


def test_tokens_takeaway_needs_five_runs_with_tokens_each_side(_files: Path) -> None:
    write(
        _files,
        [tok_line(i, 9_000_000, preset="deep") for i in range(5)]
        + [tok_line(10 + i, 3_000_000, preset="balanced") for i in range(4)]
        + [line(20, preset="balanced")],
    )
    got = preset_stats.compute(30, now=NOW)["takeaways"]
    assert not [t for t in got if t["metric"] == "tokens"]


def test_metric_tags_on_cost_time_and_hang(_files: Path) -> None:
    lines = _runs("a", 5, 0, cost=20.0, wall=1000) + _runs("b", 5, 10, cost=2.0, wall=1000)
    lines += [line(40 + i, preset="b", agents=[hung("x")]) for i in range(3)]
    write(_files, lines)
    got = preset_stats.compute(30, now=NOW)["takeaways"]
    tags = {t["id"].split(":")[0]: t["metric"] for t in got}
    assert tags == {"cost": "cost", "hang": "hang"}
    write(_files, _runs("a", 5, 0, wall=3000) + _runs("b", 5, 10, wall=1000))
    assert preset_stats.compute(30, now=NOW)["takeaways"][0]["metric"] == "time"


def test_fallback_mode_carries_tokens(_files: Path) -> None:
    usage = {
        "total_cost_usd": 2.5,
        "by_model": {
            "claude-opus-5-5": {
                "total_cost_usd": 2.5,
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 9000,
            }
        },
    }
    convs = [Conv("a", {labels.PRESET: "balanced"}, session_usage=usage)]
    view = preset_stats.compute(30, now=NOW, store_factory=lambda: _patched(Store(convs)))
    assert view["mode"] == "fallback"
    assert view["kpis"]["tokens"] == 1500
    assert view["presets"][0]["median_tokens"] == 1500
    assert view["presets"][0]["tokens_by_model"] == {"claude-opus-5-5": 1500}
    assert view["presets"][0]["recent"][0]["tokens"] == 1500


# -- fallback


@dataclass
class Conv:
    id: str
    labels: dict[str, str] = field(default_factory=dict)
    updated_at: float = NOW - 100
    created_at: float = NOW - 700
    title: str = "chat"
    session_usage: dict[str, Any] = field(default_factory=dict)


class Store:
    def __init__(self, convs: list[Conv]) -> None:
        self.convs = convs

    def list_conversations(self, **_kw: Any) -> Any:
        return SimpleNamespace(data=self.convs, has_more=False, last_id=None)


def test_fallback_uses_sessions_and_model_log(_files: Path, tmp_path: Path) -> None:
    usage = {"total_cost_usd": 2.5, "by_model": {"claude-opus-5-5": {"total_cost_usd": 2.5}}}
    convs = [
        Conv("a", {labels.PRESET: "balanced"}, session_usage=usage),
        Conv("child", {labels.ORCH: "a"}, session_usage=usage),
        Conv("old", {labels.PRESET: "balanced"}, updated_at=NOW - 90 * DAY),
    ]
    entry = {"session": "a", "kind": "raise", "at": int(NOW - 50)}
    (tmp_path / "model.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
    view = preset_stats.compute(30, now=NOW, store_factory=lambda: _patched(Store(convs)))
    assert view["mode"] == "fallback"
    assert view["kpis"]["runs"] == 1 and view["kpis"]["cost"] == 2.5
    assert view["kpis"]["raises"] == 1 and view["kpis"]["hung_runs"] is None
    assert view["takeaways"] == []
    assert view["presets"][0]["median_wall_s"] == 600


def _patched(store: Store) -> Store:
    return store


@pytest.fixture(autouse=True)
def _resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        preset_runs,
        "resolve_preset",
        lambda _store, conv: (labels.label(conv.labels, labels.PRESET) or "default", None),
    )


# -- cache


def test_no_cache_says_warming_and_starts_one_refresh(
    _files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[int] = []
    monkeypatch.setattr(preset_stats, "_spawn", lambda d, g=0: started.append(d))
    assert preset_stats.overview(30) == {"state": "warming", "days": 30}
    assert preset_stats.overview(30)["state"] == "warming"
    assert started == [30]  # the second call sees the first still running


def test_refresh_fills_cache_and_file_then_serves_it(
    _files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(_files, [line(1)])
    monkeypatch.setattr(
        preset_stats,
        "_spawn",
        lambda d, g=0: preset_stats._refresh(d, lambda n: preset_stats.compute(n, now=NOW), g),
    )
    assert preset_stats.overview(30)["state"] == "warming"
    fresh = preset_stats.overview(30)
    assert fresh["state"] == "ready" and fresh["kpis"]["runs"] == 1
    saved = json.loads(preset_stats.state_path().read_text())  # type: ignore[union-attr]
    assert "30" in saved["windows"]
    # A new process reads the file instead of warming.
    preset_stats.reset()
    monkeypatch.setattr(preset_stats, "_spawn", lambda d, g=0: None)
    assert preset_stats.overview(30)["state"] == "ready"


def test_fresh_cache_does_not_refresh_and_old_cache_does(
    _files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[int] = []
    monkeypatch.setattr(preset_stats, "_spawn", lambda d, g=0: started.append(d))
    now = [1000.0]
    monkeypatch.setattr(preset_stats.time, "time", lambda: now[0])
    preset_stats._state["loaded"] = True
    preset_stats._state["windows"]["30"] = {"at": 1000.0, "view": {"state": "ready", "marker": 1}}
    now[0] = 1000.0 + preset_stats.CACHE_TTL_S - 1
    assert preset_stats.overview(30)["marker"] == 1 and started == []
    now[0] = 1000.0 + preset_stats.CACHE_TTL_S
    assert preset_stats.overview(30)["marker"] == 1  # old answer still served, never waits
    assert started == [30]


def test_failed_refresh_keeps_old_answer_and_backs_off(
    _files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    preset_stats._state["loaded"] = True
    preset_stats._state["windows"]["30"] = {"at": 0.0, "view": {"state": "ready", "marker": 1}}
    preset_stats._state["running"][30] = (0.0, 0)

    def boom(_d: int) -> dict[str, Any]:
        raise RuntimeError("store down")

    preset_stats._refresh(30, boom)
    assert preset_stats._state["windows"]["30"]["view"]["marker"] == 1
    assert 30 not in preset_stats._state["running"]
    started: list[int] = []
    monkeypatch.setattr(preset_stats, "_spawn", lambda d, g=0: started.append(d))
    assert preset_stats.overview(30)["marker"] == 1
    assert started == []  # inside RETRY_S


# -- route


async def test_route_answers_each_callers_own_show(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ae_omni_policies import role_prefs

    monkeypatch.setenv(role_prefs.STATE_FILE_ENV, str(tmp_path / "roles.json"))
    role_prefs.reset_store()
    ae_admins.add("steve@example.com")
    monkeypatch.setattr(preset_stats, "_spawn", lambda d, g=0: None)
    first = role_prefs.starters()[0]
    body = {
        "revision": 0,
        "default_preset": first["id"],
        "apply_start_on": True,
        "presets": [first],
        "insights": {"enabled": True, "weeks": 4, "scope": "own", "show": "tokens"},
    }
    role_prefs.store().put("reid@example.com", role_prefs.RolesPut.model_validate(body))
    reid = await client.get(
        "/v1/ae/presets?days=7", headers={"X-Forwarded-Email": "reid@example.com"}
    )
    steve = await client.get(
        "/v1/ae/presets?days=7", headers={"X-Forwarded-Email": "steve@example.com"}
    )
    role_prefs.reset_store()
    assert reid.json()["show"] == "tokens"
    assert steve.json()["show"] == "both"


async def test_route_is_admin_only_and_validates_days(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preset_stats, "_spawn", lambda d, g=0: None)
    refused = await client.get(
        "/v1/ae/presets", headers={"X-Forwarded-Email": "steve@example.com"}
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "admin_only"
    reid = {"X-Forwarded-Email": "reid@example.com"}
    for bad in ("5", "abc", "0"):
        assert (await client.get(f"/v1/ae/presets?days={bad}", headers=reid)).status_code == 422
    ok = await client.get("/v1/ae/presets?days=7", headers=reid)
    assert ok.status_code == 200
    assert ok.json() == {"state": "warming", "days": 7, "show": "both"}
    default = await client.get("/v1/ae/presets", headers=reid)
    assert default.json()["days"] == 30


# -- review round 1


def test_amended_line_keeps_its_first_at(_files: Path) -> None:
    first = line(1, cost=1.0, rev=0, run_key="k", age_days=20)
    amended = line(1, cost=9.0, rev=1, run_key="k", age_days=1)
    write(_files, [first, amended])
    assert preset_stats.compute(7, now=NOW)["kpis"]["runs"] == 0  # still where it ended
    view = preset_stats.compute(30, now=NOW)
    assert view["kpis"]["cost"] == 9.0  # content from the highest rev
    (run,) = view["presets"][0]["recent"]
    assert run["at"] == preset_stats._iso(first["at"])


def test_recent_runs_carry_started_at(_files: Path) -> None:
    write(_files, [line(1, wall=600), line(2, wall=None)])  # type: ignore[arg-type]
    recent = preset_stats.compute(30, now=NOW)["presets"][0]["recent"]
    by_title = {r["title"]: r for r in recent}
    one = by_title["Run 1"]
    assert one["started_at"] == preset_stats._iso(NOW - DAY - 600)
    assert by_title["Run 2"]["started_at"] is None


def test_started_at_uses_the_latest_revs_own_end_and_wall(_files: Path) -> None:
    first = line(1, wall=600, rev=0, run_key="k", age_days=2)
    amended = line(1, wall=900, rev=1, run_key="k", age_days=1)
    write(_files, [first, amended])
    (run,) = preset_stats.compute(30, now=NOW)["presets"][0]["recent"]
    assert run["started_at"] == preset_stats._iso(amended["at"] - 900)


def test_threshold_counts_only_runs_with_the_value(_files: Path) -> None:
    # Five runs, but only four carry a cost: the cost rule stays quiet.
    mine = _runs("a", 5, 0, cost=9.0)
    for item in mine[:1]:
        item["cost"] = None
    write(_files, mine + _runs("b", 5, 10, cost=2.0))
    ids = [t["id"] for t in preset_stats.compute(30, now=NOW)["takeaways"]]
    assert "cost:a" not in ids
    full = _runs("a", 5, 0, cost=9.0) + _runs("b", 5, 10, cost=2.0)
    write(_files, full)
    (take,) = [t for t in preset_stats.compute(30, now=NOW)["takeaways"] if t["id"] == "cost:a"]
    assert "5 runs" in take["why"]


def test_hang_takeaway_opens_the_preset_with_most_hung_runs(_files: Path) -> None:
    presets = ["p", "q", "q", "q"]
    runs = [line(i, preset=pr, agents=[hung("builder")]) for i, pr in enumerate(presets)]
    write(_files, runs)
    (take,) = preset_stats.compute(30, now=NOW)["takeaways"]
    assert take["preset"] == "q"


def test_fallback_counts_an_archived_session_and_fetches_once(_files: Path) -> None:
    usage = {"total_cost_usd": 1.0}
    convs = [
        Conv("cur", {labels.PRESET: "balanced"}, session_usage=usage, updated_at=NOW - DAY),
        Conv("arch", {labels.PRESET: "balanced"}, session_usage=usage, updated_at=NOW - 2 * DAY),
        Conv("prior", {labels.PRESET: "balanced"}, session_usage=usage, updated_at=NOW - 10 * DAY),
        Conv("gone", {labels.PRESET: "balanced"}, session_usage=usage, updated_at=NOW - 40 * DAY),
    ]
    convs[1].archived = True  # type: ignore[attr-defined]
    seen: list[dict[str, Any]] = []

    class Spy(Store):
        def list_conversations(self, **kw: Any) -> Any:
            seen.append(kw)
            return super().list_conversations(**kw)

    view = preset_stats.compute(7, now=NOW, store_factory=lambda: Spy(convs))
    assert len(seen) == 1 and seen[0]["include_archived"] is True
    assert seen[0]["sort_by"] == "updated_at"
    assert view["kpis"]["runs"] == 2  # the archived one counts
    assert view["kpis"]["cost_change_pct"] == 100  # 2.0 now against 1.0 in the prior window
    assert view["truncated"] is False


def test_fallback_at_the_cap_drops_the_prior_comparison(
    _files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preset_stats, "FALLBACK_MAX_ROWS", 2)
    usage = {"total_cost_usd": 1.0}
    convs = [
        Conv("cur", {labels.PRESET: "balanced"}, session_usage=usage, updated_at=NOW - DAY),
        Conv("prior", {labels.PRESET: "balanced"}, session_usage=usage, updated_at=NOW - 10 * DAY),
    ]
    view = preset_stats.compute(7, now=NOW, store_factory=lambda: Store(convs))
    assert view["truncated"] is True
    assert view["kpis"]["cost_change_pct"] is None
    assert view["kpis"]["runs"] == 1


def test_fallback_fetch_caps_rows_with_a_warning(
    _files: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preset_stats, "FALLBACK_MAX_ROWS", 3)
    monkeypatch.setattr(preset_stats, "_FALLBACK_PAGE", 2)

    class Paged:
        def list_conversations(self, **kw: Any) -> Any:
            rows = [Conv(f"c{i}", updated_at=NOW - i) for i in range(10)]
            start = int(kw["after"][1:]) + 1 if kw["after"] else 0
            data = rows[start : start + kw["limit"]]
            return SimpleNamespace(data=data, has_more=True, last_id=data[-1].id)

    with caplog.at_level("WARNING"):
        rows = preset_stats.fetch_fallback_convs(Paged(), NOW - DAY)
    assert len(rows) == 3 and "fallback stopped" in caplog.text


def test_two_windows_finishing_together_both_survive_in_the_file(_files: Path) -> None:
    import threading

    write(_files, [line(1)])
    gate = threading.Barrier(2)

    def slow(days: int) -> dict[str, Any]:
        view = preset_stats.compute(days, now=NOW)
        gate.wait(timeout=5)
        return view

    threads = [threading.Thread(target=preset_stats._refresh, args=(d, slow)) for d in (7, 30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    saved = json.loads(preset_stats.state_path().read_text())  # type: ignore[union-attr]
    assert set(saved["windows"]) == {"7", "30"}
    assert set(preset_stats._state["windows"]) == {"7", "30"}


def test_a_recompute_stuck_over_five_minutes_is_superseded(
    _files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[int] = []
    gens: list[int] = []
    monkeypatch.setattr(preset_stats, "_spawn", lambda d, g=0: (started.append(d), gens.append(g)))
    clock = [1000.0]
    monkeypatch.setattr(preset_stats.time, "monotonic", lambda: clock[0])
    preset_stats.overview(30)
    clock[0] += preset_stats.STUCK_S - 1
    preset_stats.overview(30)
    assert started == [30]  # still within five minutes
    clock[0] += 2
    preset_stats.overview(30)
    assert started == [30, 30] and gens[1] > gens[0]
    # The stale thread finishing now changes nothing.
    preset_stats._refresh(30, lambda n: {"state": "ready", "marker": "stale"}, gens[0])
    assert "30" not in preset_stats._state["windows"]
    assert 30 in preset_stats._state["running"]


def test_a_run_superseded_while_waiting_for_the_save_lock_writes_nothing(
    _files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preset_stats, "_spawn", lambda d, g=0: None)
    preset_stats.overview(30)
    (old_gen,) = [g for _, g in preset_stats._state["running"].values()]

    def compute_then_lose_the_race(days: int) -> dict[str, Any]:
        # A fresh run takes over between the compute and the save.
        with preset_stats._mu:
            preset_stats._state["running"][days] = (0.0, old_gen + 1)
        return {"state": "ready", "marker": "stale"}

    preset_stats._refresh(30, compute_then_lose_the_race, old_gen)
    assert "30" not in preset_stats._state["windows"]
    assert preset_stats.state_path() is None or not preset_stats.state_path().exists()  # type: ignore[union-attr]


def test_no_cache_and_a_failed_recompute_answers_state_error(
    _files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preset_stats, "_spawn", lambda d, g=0: None)
    assert preset_stats.overview(30)["state"] == "warming"

    def boom(_d: int) -> dict[str, Any]:
        raise RuntimeError("store down")

    preset_stats._refresh(30, boom)
    answer = preset_stats.overview(30)
    assert answer["state"] == "error" and "store down" in answer["note"]


def test_tokens_change_needs_tokens_on_half_the_runs_of_each_window(_files: Path) -> None:
    full = [
        tok_line(1, 1_000_000, age_days=1),
        tok_line(2, 1_000_000, age_days=2),
        tok_line(3, 500_000, age_days=9),
        tok_line(4, 500_000, age_days=10),
    ]
    write(_files, full)
    assert preset_stats.compute(7, now=NOW)["kpis"]["tokens_change_pct"] == 100
    # Half of each window carrying tokens still counts.
    write(_files, [*full, line(5, age_days=1), line(6, age_days=9)])
    assert preset_stats.compute(7, now=NOW)["kpis"]["tokens_change_pct"] is not None
    # A window where under half of the runs carry tokens draws no change.
    many_bare = [line(10 + i, age_days=1) for i in range(3)]
    write(_files, [full[0], *many_bare, full[2], full[3]])
    assert preset_stats.compute(7, now=NOW)["kpis"]["tokens_change_pct"] is None
    prior_bare = [line(20 + i, age_days=9) for i in range(3)]
    write(_files, [full[0], full[1], full[2], *prior_bare])
    assert preset_stats.compute(7, now=NOW)["kpis"]["tokens_change_pct"] is None
