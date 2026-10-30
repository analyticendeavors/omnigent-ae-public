"""The Family cost card's server side (2026-10-29): the family walk, windows, scoping, units."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ae_omni_policies import family_cost, labels, role_prefs
from ae_omni_policies.family_cost import StateFile, Sweeper

DAY = 86400.0
# 2026-10-29 12:00 UTC
NOW = 1793275200.0
TODAY = "2026-10-29"


def usage(cost: float, inp: int, out: int, cache: int) -> dict[str, Any]:
    return {
        "total_cost_usd": cost,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_input_tokens": cache,
    }


@dataclass
class Row:
    id: str
    session_usage: dict[str, Any] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    parent_conversation_id: str | None = None
    created_at: float = NOW - 2 * DAY
    title: str = "a chat"
    host_id: str | None = "h1"


class Rig:
    def __init__(self, path: Path | None = None) -> None:
        self.tops: list[Row] = []
        self.natives: list[Row] = []
        self.now = NOW
        self.owners: dict[str, str] = {}
        self.state = StateFile(path)
        self.sweeper = Sweeper(
            store=None,
            state=self.state,
            owner=lambda sid: self.owners.get(sid),
            host_name=lambda host: f"host-{host}",
            list_top=lambda: self.tops,
            list_native=lambda: self.natives,
            clock=lambda: self.now,
        )

    def sweep(self) -> dict[str, Any]:
        assert self.sweeper.sweep_sync() is True
        return self.state.load()

    def families(self, days: int = 7) -> list[dict[str, Any]]:
        return self.state.load()["families"][str(days)]["families"]


def lead_with_children() -> Rig:
    rig = Rig()
    rig.tops = [
        Row(
            "lead",
            usage(10.0, 100, 50, 1000),
            {
                labels.STATUS: "working",
                "omnigent.last_context_tokens": "250000",
                "ae.claude_native.resume_count": "2",
            },
            title="Big job",
        ),
        Row(
            "build",
            usage(4.0, 40, 20, 400),
            {labels.ORCH: "lead", labels.ROLE: "builder", "ae.claude_native.resume_count": "1"},
        ),
        Row("rev", usage(1.0, 10, 5, 100), {labels.ORCH: "lead", labels.ROLE: "reviewer"}),
        Row(
            "cdx",
            usage(2.0, 20, 10, 200),
            {labels.ORCH: "build", labels.ROLE: "reviewer", "omnigent.wrapper": "codex-native-ui"},
        ),
    ]
    rig.owners = {"lead": "a@x.test"}
    return rig


def test_family_walk_follows_orch_links_and_native_rows_add_tokens_not_cost() -> None:
    rig = lead_with_children()
    rig.natives = [
        Row("sub", usage(99.0, 7, 3, 90), {}, parent_conversation_id="lead"),
        Row("sub2", usage(99.0, 1, 1, 1), {}, parent_conversation_id="cdx"),
        Row("stray", usage(5.0, 1, 1, 1), {}, parent_conversation_id="elsewhere"),
    ]
    rig.sweep()
    # The stray native's parent is not loaded, so it is a family of its own (nothing vanishes).
    assert {f["lead_id"] for f in rig.families(30)} == {"lead", "stray"}
    fam = next(f for f in rig.families(30) if f["lead_id"] == "lead")
    assert fam["cost_usd"] == pytest.approx(17.0)
    assert fam["tokens"] == {"input": 178, "output": 89, "cache_read": 1791, "total": 2058}
    roles = {r["role"]: r for r in fam["by_role"]}
    assert set(roles) == {"lead", "builder", "reviewer", "codex"}
    # The native row under the lead counts inside the lead's row.
    assert roles["lead"]["tokens_total"] == 1150 + 100
    assert roles["lead"]["sessions"] == 1
    assert roles["codex"]["tokens_total"] == 230 + 3
    assert fam["sessions"] == 4
    assert fam["host"] == "host-h1"
    assert fam["owner"] == "a@x.test"
    assert fam["lead_context_tokens"] == 250000
    assert fam["status"] == "working"


def test_restarts_are_summed_over_the_family_labels() -> None:
    rig = lead_with_children()
    rig.sweep()
    assert rig.families(30)[0]["restarts"] == 3


def test_window_is_current_minus_the_snapshot_at_its_start(tmp_path: Path) -> None:
    rig = Rig(tmp_path / "fc.json")
    rig.tops = [Row("lead", usage(10.0, 100, 0, 900), {}, created_at=NOW - 40 * DAY)]
    rig.now = NOW - 8 * DAY  # a snapshot that is 8 days old: outside the 7 d window's first day
    rig.sweep()
    rig.now = NOW - 6 * DAY  # the 7 d window starts on this day
    rig.tops = [Row("lead", usage(14.0, 150, 0, 1350), {}, created_at=NOW - 40 * DAY)]
    rig.sweep()
    rig.now = NOW
    rig.tops = [Row("lead", usage(20.0, 200, 0, 1800), {}, created_at=NOW - 40 * DAY)]
    rig.sweep()
    seven = rig.families(7)[0]
    assert seven["cost_usd"] == pytest.approx(10.0)  # 20 - 10, the last sample before that day
    assert seven["tokens"]["total"] == 2000 - 1000
    thirty = rig.families(30)[0]
    assert thirty["cost_usd"] == pytest.approx(10.0)  # 20 - 10, from the oldest snapshot
    assert rig.state.load()["families"]["30"]["history_since"] == _day(NOW - 8 * DAY)
    # The oldest snapshot is on the 30 d window's side of nothing earlier: partial history.
    assert rig.state.load()["families"]["30"]["partial"] is True
    assert rig.state.load()["families"]["7"]["history_since"] == _day(NOW - 6 * DAY)
    assert rig.state.load()["families"]["7"]["partial"] is False


def _day(epoch: float) -> str:
    return family_cost._day(epoch)


def test_a_session_started_inside_the_window_counts_from_zero() -> None:
    rig = Rig()
    rig.tops = [Row("old", usage(5.0, 50, 0, 0), {}, created_at=NOW - 40 * DAY)]
    rig.now = NOW - 3 * DAY
    rig.sweep()
    rig.now = NOW
    rig.tops = [
        Row("old", usage(5.0, 50, 0, 0), {}, created_at=NOW - 40 * DAY),
        Row("new", usage(3.0, 30, 0, 0), {}, created_at=NOW - 2 * DAY),
    ]
    rig.sweep()
    got = {f["lead_id"]: f for f in rig.families(7)}
    assert set(got) == {"new"}  # "old" spent nothing since the snapshot
    assert got["new"]["cost_usd"] == pytest.approx(3.0)


def test_a_total_that_went_down_counts_what_was_spent_after_it() -> None:
    rig = Rig()
    rig.tops = [Row("lead", usage(9.0, 90, 0, 0), {}, created_at=NOW - 40 * DAY)]
    rig.now = NOW - DAY
    rig.sweep()
    rig.now = NOW
    rig.tops = [
        Row("lead", usage(2.0, 20, 0, 0), {}, created_at=NOW - 40 * DAY)
    ]  # the status line total restarted
    rig.sweep()
    # The fall adds the 9.0 before it to the session's offset: 2.0 more was spent, not none.
    [fam] = rig.families(7)
    assert fam["cost_usd"] == pytest.approx(2.0)


def test_store_keeps_35_days_and_the_state_survives_a_reload(tmp_path: Path) -> None:
    path = tmp_path / "fc.json"
    rig = Rig(path)
    rig.tops = [Row("lead", usage(1.0, 10, 0, 0), {})]
    for back in range(40, -1, -1):
        rig.now = NOW - back * DAY
        rig.sweep()
    days = json.loads(path.read_text())["days"]
    assert len(days) == family_cost.KEEP_DAYS
    assert min(days) == _day(NOW - 34 * DAY)
    again = StateFile(path).load()
    assert again["at"] == NOW
    assert again["families"]["7"]["families"] == rig.families(7)


def answer(rig: Rig, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "days": 30,
        "viewer": "a@x.test",
        "admin": False,
        "unit": "tokens",
        "runaway": 0.0,
    }
    args.update(kw)
    return family_cost.view(state=rig.state, **args)


def test_warming_until_a_sweep_has_finished() -> None:
    rig = Rig()
    assert answer(rig) == {"state": "warming", "days": 30, "unit_default": "tokens"}
    rig.sweep()
    assert answer(rig)["state"] == "ready"


def two_owners() -> Rig:
    rig = Rig()
    rig.tops = [
        Row("mine", usage(5.0, 100, 0, 0), {}, title="mine"),
        Row("theirs", usage(30.0, 10, 0, 0), {}, title="theirs"),
    ]
    rig.owners = {"mine": "a@x.test", "theirs": "b@x.test"}
    rig.sweep()
    return rig


def test_a_member_sees_only_their_own_families_and_an_admin_sees_all() -> None:
    rig = two_owners()
    own = answer(rig)
    assert [f["lead_id"] for f in own["families"]] == ["mine"]
    assert own["totals"]["families"] == 1
    everyone = answer(rig, admin=True)
    assert {f["lead_id"] for f in everyone["families"]} == {"mine", "theirs"}
    assert all(f["host"] == "host-h1" for f in everyone["families"])
    assert answer(rig, viewer=None)["families"] == []


def test_rank_follows_the_chosen_unit() -> None:
    rig = two_owners()
    by_tokens = answer(rig, admin=True, unit="tokens")
    by_dollars = answer(rig, admin=True, unit="dollars")
    assert [f["lead_id"] for f in by_tokens["families"]] == ["mine", "theirs"]
    assert [f["lead_id"] for f in by_dollars["families"]] == ["theirs", "mine"]
    assert by_dollars["unit_default"] == "dollars"
    # Both units are in every row, whichever leads.
    row = by_tokens["families"][0]
    assert row["cost_usd"] == pytest.approx(5.0) and row["tokens"]["total"] == 100


def test_runaway_flag_uses_spend_today_in_the_chosen_unit() -> None:
    rig = two_owners()
    # Both were created before today and have no earlier snapshot, so today's spend is zero.
    assert answer(rig, admin=True, unit="tokens", runaway=50)["totals"]["flagged"] == 0
    rig.now = NOW + 3600
    rig.tops = [
        Row("mine", usage(5.0 + 1.0, 100 + 400, 0, 0), {}, title="mine"),
        Row("theirs", usage(30.0 + 40.0, 10 + 5, 0, 0), {}, title="theirs"),
    ]
    rig.sweep()
    tokens = answer(rig, admin=True, unit="tokens", runaway=300)
    assert {f["lead_id"]: f["flagged"] for f in tokens["families"]} == {
        "mine": True,
        "theirs": False,
    }
    dollars = answer(rig, admin=True, unit="dollars", runaway=25)
    assert {f["lead_id"]: f["flagged"] for f in dollars["families"]} == {
        "mine": False,
        "theirs": True,
    }
    assert dollars["totals"]["flagged"] == 1
    off = answer(rig, admin=True, unit="dollars", runaway=0)
    assert not any(f["flagged"] for f in off["families"])


def test_the_answer_is_cut_to_fit_and_says_so() -> None:
    rig = Rig()
    rig.tops = [
        Row(f"lead{i:03d}", usage(1.0 + i, 100 + i, 0, 0), {}, title="t" * 120) for i in range(300)
    ]
    rig.owners = {r.id: "a@x.test" for r in rig.tops}
    rig.sweep()
    got = answer(rig, admin=True)
    assert got["truncated"] is True
    assert 0 < len(got["families"]) < 300
    assert len(json.dumps(got, separators=(",", ":"))) <= family_cost.MAX_CHARS
    # The biggest are the ones kept.
    assert got["families"][0]["lead_id"] == "lead299"
    small = answer(two_owners(), admin=True)
    assert small["truncated"] is False


def test_only_one_process_sweeps(tmp_path: Path) -> None:
    from ae_omni_policies import auto_archive

    holder = auto_archive.SweepLock(tmp_path / "x.lock")
    assert holder.acquire()
    rig = Rig()
    rig.sweeper._lock = auto_archive.SweepLock(tmp_path / "x.lock")
    assert rig.sweeper.sweep_sync() is False
    holder.release()


def test_roles_store_holds_the_cost_settings(tmp_path: Path) -> None:
    store = role_prefs.RolePrefsStore(tmp_path / "roles.json")
    assert store.family_cost_settings("a@x.test") == ("tokens", 5_000_000, 25.0)
    state = store.get("a@x.test")
    body = role_prefs.RolesPut.model_validate(
        {
            **state.model_dump(),
            "family_cost_unit": "dollars",
            "runaway_family_usd": 12.5,
        }
    )
    store.put("a@x.test", body)
    assert store.family_cost_settings("a@x.test") == ("dollars", 5_000_000, 12.5)
    # A later save that leaves the fields out keeps them.
    again = role_prefs.RolesPut.model_validate(
        {
            k: v
            for k, v in store.get("a@x.test").model_dump().items()
            if not k.startswith(("family_cost", "runaway"))
        }
    )
    store.put("a@x.test", again)
    assert store.family_cost_settings("a@x.test")[0] == "dollars"
    with pytest.raises(ValueError):
        role_prefs.RolesPut.model_validate({**state.model_dump(), "family_cost_unit": "euros"})


def test_route_answers_the_last_sweep_for_an_admin(
    monkeypatch: pytest.MonkeyPatch, app: Any
) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(app)
    assert client.get("/v1/ae/family-cost?days=5").status_code == 422
    # Before any sweep: warming, with the caller's own unit.
    monkeypatch.setattr(family_cost, "_shared", Rig().state)
    warm = client.get("/v1/ae/family-cost?days=7")
    assert warm.status_code == 200, warm.text
    assert warm.json()["state"] == "warming" and warm.json()["unit_default"] == "tokens"
    # A single-user runtime's one user is an admin: every family, whoever owns it.
    monkeypatch.setattr(family_cost, "_shared", two_owners().state)
    got = client.get("/v1/ae/family-cost?days=7")
    assert got.status_code == 200, got.text
    body = got.json()
    assert {f["lead_id"] for f in body["families"]} == {"mine", "theirs"}
    assert body["truncated"] is False and body["totals"]["families"] == 2


def test_settings_post_saves_the_unit_and_threshold_and_the_route_reads_them(
    monkeypatch: pytest.MonkeyPatch, app: Any, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    role_prefs.reset_store(role_prefs.RolePrefsStore(tmp_path / "roles.json"))
    client = TestClient(app)
    monkeypatch.setattr(family_cost, "_shared", two_owners().state)
    first = client.get("/v1/ae/family-cost?days=7").json()
    assert first["unit_default"] == "tokens"
    assert first["settings"] == {
        "unit": "tokens",
        "runaway_tokens": 5_000_000,
        "runaway_usd": 25.0,
    }
    saved = client.post(
        "/v1/ae/family-cost/settings",
        json={"family_cost_unit": "dollars", "runaway_family_usd": 12.5},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"unit": "dollars", "runaway_tokens": 5_000_000, "runaway_usd": 12.5}
    after = client.get("/v1/ae/family-cost?days=7").json()
    assert after["unit_default"] == "dollars"
    assert [f["lead_id"] for f in after["families"]] == ["theirs", "mine"]
    # Only the tokens limit changes; the rest is kept.
    again = client.post("/v1/ae/family-cost/settings", json={"runaway_family_tokens": 0})
    assert again.json() == {"unit": "dollars", "runaway_tokens": 0, "runaway_usd": 12.5}
    for bad in ({}, {"family_cost_unit": "euros"}, {"runaway_family_usd": -1}, {"nope": 1}):
        assert client.post("/v1/ae/family-cost/settings", json=bad).status_code == 422, bad
    role_prefs.reset_store()


# ---- round 1 review: the walk, resets, the Pacific day, the state file


def test_a_default_child_linked_only_by_its_parent_joins_the_family() -> None:
    rig = Rig()
    rig.tops = [
        Row("lead", usage(1.0, 10, 0, 0), {}),
        Row("kid", usage(2.0, 20, 0, 0), {}, parent_conversation_id="lead"),
    ]
    rig.sweep()
    [fam] = rig.families(30)
    assert fam["lead_id"] == "lead" and fam["cost_usd"] == pytest.approx(3.0)


def test_a_native_under_a_native_counts_for_the_session_that_holds_it() -> None:
    rig = Rig()
    rig.tops = [
        Row("lead", usage(1.0, 10, 0, 0), {}),
        Row("build", usage(2.0, 20, 0, 0), {labels.ORCH: "lead", labels.ROLE: "builder"}),
    ]
    rig.natives = [
        Row("n1", usage(9.0, 5, 0, 0), {}, parent_conversation_id="build"),
        Row("n2", usage(9.0, 7, 0, 0), {}, parent_conversation_id="n1"),
    ]
    rig.sweep()
    [fam] = rig.families(30)
    assert fam["tokens"]["total"] == 10 + 20 + 5 + 7
    assert fam["cost_usd"] == pytest.approx(3.0)
    roles = {r["role"]: r["tokens_total"] for r in fam["by_role"]}
    assert roles == {"lead": 10, "builder": 32}


def test_a_session_whose_orch_parent_is_missing_becomes_its_own_lead() -> None:
    rig = Rig()
    rig.tops = [Row("orphan", usage(4.0, 40, 0, 0), {labels.ORCH: "deleted-lead"})]
    rig.sweep()
    [fam] = rig.families(30)
    assert fam["lead_id"] == "orphan" and fam["cost_usd"] == pytest.approx(4.0)


def test_a_handoff_chain_is_one_family_under_the_newest_lead() -> None:
    rig = Rig()
    rig.tops = [
        Row("a", usage(1.0, 10, 0, 0), {labels.HANDOFF_TO: "b"}, title="old"),
        Row("b", usage(2.0, 20, 0, 0), {labels.HANDOFF_TO: "c", labels.HANDOFF_FROM: "a"}),
        Row("c", usage(3.0, 30, 0, 0), {labels.HANDOFF_FROM: "b"}, title="newest"),
        Row("kid", usage(4.0, 40, 0, 0), {labels.ORCH: "a", labels.ROLE: "builder"}),
    ]
    rig.sweep()
    [fam] = rig.families(30)
    assert fam["lead_id"] == "c" and fam["title"] == "newest"
    assert fam["cost_usd"] == pytest.approx(10.0)
    roles = {r["role"]: r for r in fam["by_role"]}
    assert roles["lead"]["sessions"] == 3 and roles["builder"]["sessions"] == 1


def test_a_family_deeper_than_the_cap_leaves_the_rest_out(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    depth = family_cost.MAX_DEPTH + 2
    rig.tops = [Row("s0", usage(1.0, 10, 0, 0), {})] + [
        Row(f"s{i}", usage(1.0, 10, 0, 0), {labels.ORCH: f"s{i - 1}"}) for i in range(1, depth)
    ]
    family_cost._CAP_WARNED.clear()
    with caplog.at_level("WARNING"):
        rig.sweep()
    [fam] = rig.families(30)
    assert fam["sessions"] == family_cost.MAX_DEPTH + 1
    assert "left out" in caplog.text


def test_a_family_past_the_size_cap_is_cut() -> None:
    rig = Rig()
    rig.tops = [Row("lead", usage(1.0, 10, 0, 0), {})] + [
        Row(f"k{i}", usage(1.0, 10, 0, 0), {labels.ORCH: "lead"})
        for i in range(family_cost.MAX_FAMILY + 5)
    ]
    rig.sweep()
    [fam] = rig.families(30)
    assert fam["sessions"] == family_cost.MAX_FAMILY


def test_spend_after_a_reset_still_counts_and_two_resets_add_up() -> None:
    rig = Rig()
    old = NOW - 40 * DAY

    def at(when: float, cost: float, tokens: int) -> None:
        rig.now = when
        rig.tops = [Row("lead", usage(cost, tokens, 0, 0), {}, created_at=old)]
        rig.sweep()

    at(NOW - 3 * DAY, 100.0, 1000)  # the baseline
    at(NOW - 2 * DAY, 100.0, 1000)
    at(NOW - 1 * DAY, 0.0, 0)  # reset
    at(NOW, 120.0, 1200)
    [fam] = rig.families(7)
    assert fam["cost_usd"] == pytest.approx(120.0)  # not 20
    assert fam["tokens"]["total"] == 1200
    at(NOW + 600, 30.0, 300)  # a second reset in the same window
    [fam] = rig.families(7)
    assert fam["cost_usd"] == pytest.approx(150.0)
    assert fam["tokens"]["total"] == 1500


def test_the_day_is_pacific_and_starts_at_the_last_sample_before_midnight() -> None:
    from datetime import datetime

    tz = family_cost.TIME_ZONE
    rig = Rig()
    old = NOW - 40 * DAY

    def sample(stamp: datetime, cost: float) -> None:
        rig.now = stamp.timestamp()
        rig.tops = [Row("lead", usage(cost, int(cost * 10), 0, 0), {}, created_at=old)]
        rig.sweep()

    sample(datetime(2026, 10, 28, 23, 50, tzinfo=tz), 10.0)
    sample(datetime(2026, 10, 29, 0, 5, tzinfo=tz), 14.0)  # 4 spent after midnight
    sample(datetime(2026, 10, 29, 9, 0, tzinfo=tz), 20.0)
    days = rig.state.load()["days"]
    assert days["2026-10-29"]["lead"][0] == 10.0  # the 23:50 sample, not the 00:05 one
    assert (
        rig.families(7)
        and family_cost._day(datetime(2026, 10, 29, 0, 5, tzinfo=tz).timestamp()) == "2026-10-29"
    )
    view = answer(rig, days=7, admin=True, runaway=1)
    assert view["families"][0]["today"]["cost_usd"] == pytest.approx(10.0)
    # 06:00 UTC on the 29th is still the 28th in Pacific time.
    assert family_cost._day(datetime(2026, 10, 29, 6, 0, tzinfo=family_cost.UTC).timestamp()) == (
        "2026-10-28"
    )


def test_day_boundaries_hold_across_a_daylight_saving_change() -> None:
    # Clocks go back on 2026-11-01: that Pacific day has 25 hours.
    start = family_cost._day_start("2026-11-01")
    end = family_cost._day_start("2026-11-02")
    assert end - start == 25 * 3600
    assert family_cost._days_before("2026-11-02", 1) == "2026-11-01"
    assert family_cost._day(end - 1) == "2026-11-01" and family_cost._day(end) == "2026-11-02"
    # And a 7 day window across it starts on the right calendar day.
    base, day, _partial = family_cost._baseline(
        {"2026-10-27": {"x": [1, 1, 1, 1]}, "2026-10-26": {"x": [9, 9, 9, 9]}}, "2026-11-02", 7
    )
    assert day == "2026-10-27" and base["x"][0] == 1


def test_another_process_sees_what_the_sweeping_one_wrote(tmp_path: Path) -> None:
    path = tmp_path / "fc.json"
    sweeping = Rig(path)
    reader = StateFile(path)
    assert reader.load()["at"] is None
    sweeping.tops = [Row("lead", usage(1.0, 10, 0, 0), {})]
    sweeping.sweep()
    assert reader.load()["at"] == NOW
    assert (
        family_cost.build_answer(
            reader.load(), days=7, viewer=None, admin=True, unit="tokens", runaway=0
        )["state"]
        == "ready"
    )


def test_an_unreadable_state_file_is_set_aside_not_overwritten(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "fc.json"
    for bad in ("{not json", json.dumps({"version": 99, "days": {"d": {}}})):
        path.write_text(bad)
        with caplog.at_level("WARNING"):
            data = StateFile(path).load()
        assert data["days"] == {} and data["at"] is None
        kept = sorted(tmp_path.glob("fc.json.bad-*"))
        assert kept and kept[-1].read_text() == bad
        assert not path.exists()
        for k in kept:
            k.unlink()
    assert "kept as" in caplog.text


def test_a_save_that_cannot_be_encoded_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = StateFile(tmp_path / "fc.json")
    with caplog.at_level("WARNING"):
        state.save({"days": {1, 2}, "at": 1.0})  # a set is not JSON
    assert "not saved" in caplog.text
    assert not list(tmp_path.glob("fc.json.*"))


def test_settings_post_refuses_a_foreign_origin_and_a_non_json_body(
    app: Any, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    role_prefs.reset_store(role_prefs.RolePrefsStore(tmp_path / "roles.json"))
    client = TestClient(app)
    url = "/v1/ae/family-cost/settings"
    foreign = client.post(
        url, json={"family_cost_unit": "dollars"}, headers={"Origin": "https://evil.test"}
    )
    assert foreign.status_code in (400, 403), foreign.text
    plain = client.post(
        url, content='{"family_cost_unit":"dollars"}', headers={"Content-Type": "text/plain"}
    )
    assert plain.status_code in (400, 415, 422), plain.text
    assert role_prefs.store().family_cost_settings("local")[0] == "tokens"
    role_prefs.reset_store()


def _counted_once(rig: Rig, ids: list[str]) -> list[dict[str, Any]]:
    fams = rig.families(30)
    assert sum(f["sessions"] for f in fams) == len(ids)
    assert sum(f["tokens"]["total"] for f in fams) == 10 * len(ids)
    return fams


def test_a_two_cycle_is_one_family_counted_once() -> None:
    rig = Rig()
    rig.tops = [
        Row("a", usage(1.0, 10, 0, 0), {labels.ORCH: "b"}),
        Row("b", usage(1.0, 10, 0, 0), {labels.ORCH: "a"}),
    ]
    rig.sweep()
    [fam] = _counted_once(rig, ["a", "b"])
    assert fam["lead_id"] == "a"


def test_a_three_cycle_is_one_family_counted_once() -> None:
    rig = Rig()
    rig.tops = [
        Row("c", usage(1.0, 10, 0, 0), {labels.ORCH: "b"}),
        Row("b", usage(1.0, 10, 0, 0), {labels.ORCH: "a"}),
        Row("a", usage(1.0, 10, 0, 0), {}, parent_conversation_id="c"),
    ]
    rig.sweep()
    [fam] = _counted_once(rig, ["a", "b", "c"])
    assert fam["lead_id"] == "a"


def test_a_tail_leading_into_a_cycle_joins_its_family() -> None:
    rig = Rig()
    rig.tops = [
        Row("m", usage(1.0, 10, 0, 0), {labels.ORCH: "z"}),
        Row("z", usage(1.0, 10, 0, 0), {labels.ORCH: "y"}),
        Row("y", usage(1.0, 10, 0, 0), {labels.ORCH: "z"}),
        Row("tail", usage(1.0, 10, 0, 0), {labels.ORCH: "m"}),
    ]
    rig.natives = []
    rig.sweep()
    [fam] = _counted_once(rig, ["m", "z", "y", "tail"])
    assert fam["lead_id"] == "y"


def test_an_offset_for_a_deleted_session_is_dropped() -> None:
    rig = Rig()
    old = NOW - 40 * DAY
    for cost in (50.0, 0.0):  # the second sample is a reset, which makes an offset
        rig.tops = [Row("gone", usage(cost, int(cost), 0, 0), {}, created_at=old)]
        rig.sweep()
    assert "gone" in rig.state.load()["offsets"]
    rig.tops = []
    rig.sweep()  # still in the last sample
    rig.sweep()
    assert "gone" not in rig.state.load()["offsets"]
