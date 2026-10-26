"""Cost targets (2026-10-25): rules, seeding, each measure, the alert pass, and the routes.

Nothing here reaches Azure, Fabric or the task app: the Azure reading is a dict handed to
``spend_from`` (or patched over ``azure_cost.snapshot``), Fabric's rows are plain dicts, and the
task app is an in-memory fake.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import azure_cost, cost_targets, taskstore
from ae_omni_policies.cost_targets import Known, Rule, RuleBody, RuleError, Scope

REID = {"X-Forwarded-Email": "reid@example.com"}
STEVE = {"X-Forwarded-Email": "steve@example.com"}
# Wednesday noon UTC; finished days run through 2026-10-20.
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
CFG = cost_targets.Config(
    on=True, off_reason=None, list_name="AE Work", time="09:00", tz="America/Los_Angeles"
)
SUB_SC = "11111111-1111-4111-8111-111111111111"
SUB_25 = "22222222-2222-4222-8222-222222222222"
SUB_EUR = "33333333-3333-4333-8333-333333333333"


class FakeStore:
    """The task app's surface the alert pass uses, in memory."""

    def __init__(self) -> None:
        self.tasks: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.create_fails_after_landing = False
        self.lists = [{"id": "L-AE", "name": "AE Work", "hidden": 0}]

    def _ok(self, data: Any, status: int = 200) -> taskstore.Result:
        return taskstore.Result(ok=True, status=status, data=data)

    async def list_buckets(self) -> taskstore.Result:
        return self._ok({"buckets": self.lists})

    async def list_tags(self) -> taskstore.Result:
        return self._ok({"tags": [{"name": "ae"}]})

    async def list_tasks(self, status: str = "open") -> taskstore.Result:
        self.calls.append(("list_tasks", status))
        return self._ok({"tasks": [t for t in self.tasks.values() if t["status"] == status]})

    async def get_task(self, task_id: str) -> taskstore.Result:
        if task_id not in self.tasks:
            return taskstore.Result(ok=False, status=404, kind="client", error="404 not found")
        return self._ok(dict(self.tasks[task_id]))

    async def create_task(self, fields: dict[str, Any]) -> taskstore.Result:
        self.calls.append(("create_task", fields))
        task_id = f"T{len(self.tasks) + 1}"
        self.tasks[task_id] = {"id": task_id, "created_at": "2026-10-21T12:00:00Z", **fields}
        if self.create_fails_after_landing:
            return taskstore.Result(ok=False, status=0, kind="transport", error="timed out")
        return self._ok(self.tasks[task_id], 201)

    async def patch_task(self, task_id: str, fields: dict[str, Any]) -> taskstore.Result:
        self.calls.append(("patch_task", (task_id, fields)))
        if task_id not in self.tasks:
            return taskstore.Result(ok=False, status=404, kind="client", error="404 not found")
        self.tasks[task_id].update(fields)
        return self._ok(self.tasks[task_id])

    async def aclose(self) -> None:
        return None

    def made(self) -> list[dict[str, Any]]:
        return [body for name, body in self.calls if name == "create_task"]

    def open_titles(self) -> list[str]:
        return [t["title"] for t in self.tasks.values() if t["status"] == "open"]


def _days(first: int, last: int, amount: float, month: str = "2026-10") -> dict[str, float]:
    return {f"{month}-{d:02d}": amount for d in range(first, last + 1)}


def _reading(
    daily: dict[str, float],
    *,
    currency: str | None = "USD",
    services: dict[str, dict[str, float]] | None = None,
    resources: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    return {
        "daily": daily,
        "services": services or {},
        "resources": resources or {},
        "currency": currency,
        "note": None,
    }


def _snapshot(**changes: Any) -> dict[str, Any]:
    rid = f"/subscriptions/{SUB_25}/resourceGroups/rg/providers/Microsoft.Sql/servers/sql-prod-01"
    snap: dict[str, Any] = {
        "subscriptions": [(SUB_SC, "MVP SC"), (SUB_25, "MVP 2025")],
        "readings": {
            SUB_SC: _reading(_days(1, 20, 10.0), services={"2026-10": {"SQL Database": 150.0}}),
            SUB_25: _reading(
                _days(1, 20, 32.0),
                services={"2026-10": {"SQL Database": 400.0, "App Service": 240.0}},
                resources={rid: {**_days(12, 19, 3.0), "2026-10-20": 31.0}},
            ),
        },
        "errors": {},
        "at": NOW - timedelta(minutes=5),
        "limited": False,
        "rateLimit": None,
    }
    snap.update(changes)
    stamp = snap["at"] if isinstance(snap.get("at"), datetime) else NOW
    for reading in snap["readings"].values():
        reading.setdefault("readAt", stamp.isoformat())
    return snap


def _rule(
    rule_id: str,
    measure: str,
    kind: str,
    name: str | None,
    limit: float,
    *,
    enabled: bool = True,
) -> Rule:
    return Rule(
        id=rule_id,
        name=f"Rule {rule_id}",
        enabled=enabled,
        scope=Scope(kind=kind, name=name),  # type: ignore[arg-type]
        measure=measure,  # type: ignore[arg-type]
        limit=limit,
    )


def _ago(**delta: float) -> str:
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _spend(**changes: Any) -> cost_targets.Spend:
    return cost_targets.spend_from(_snapshot(**changes), NOW)


# ── validation ──────────────────────────────────────────────────────────────


def _body(**changes: Any) -> RuleBody:
    base: dict[str, Any] = {
        "name": "MVP 2025 month",
        "enabled": True,
        "scope": {"kind": "subscription", "name": "MVP 2025"},
        "measure": "month_to_date",
        "limit": 600,
    }
    base.update(changes)
    return RuleBody.model_validate(base)


KNOWN = Known(
    subscriptions=("MVP SC", "MVP 2025"),
    services=("SQL Database",),
    resources=("sql-prod-01",),
    capacities=("cap1",),
)


def test_check_accepts_a_good_rule() -> None:
    rule = cost_targets.check("t-1", _body(), KNOWN, [])
    assert rule.id == "t-1" and rule.limit == 600 and rule.scope.name == "MVP 2025"


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"name": "   "}, "Give the target a name"),
        ({"name": "a\nb"}, "Give the target a name"),
        ({"name": "x" * 81}, "Give the target a name"),
        ({"scope": {"kind": "resource", "name": "sql-prod-01"}}, "cannot be set on a resource"),
        ({"scope": {"kind": "all", "name": "MVP SC"}}, "takes no name"),
        ({"scope": {"kind": "subscription"}}, "needs a name"),
        ({"scope": {"kind": "subscription", "name": "Nope"}}, "Pick one from the list"),
        ({"scope": {"kind": "service", "name": "Nope"}}, "Pick one from the list"),
        ({"measure": "fabric_active_hours"}, "cannot be set on a subscription"),
        ({"limit": 20_000_000}, "at most"),
    ],
)
def test_check_rejects_with_a_message_that_says_what_to_do(
    changes: dict[str, Any], fragment: str
) -> None:
    with pytest.raises(RuleError, match=fragment):
        cost_targets.check("t-1", _body(**changes), KNOWN, [])


@pytest.mark.parametrize("limit", [0, -5, float("inf"), float("nan"), "50", True, None])
def test_the_limit_must_be_a_positive_finite_number(limit: Any) -> None:
    with pytest.raises(ValueError):
        RuleBody.model_validate(
            {
                "name": "x",
                "scope": {"kind": "all"},
                "measure": "month_to_date",
                "limit": limit,
            }
        )


def test_check_rejects_a_bad_id_and_a_fifty_first_rule() -> None:
    with pytest.raises(RuleError, match="target id"):
        cost_targets.check("Bad Id", _body(), KNOWN, [])
    full = [_rule(f"r{i}", "month_to_date", "all", None, 1.0) for i in range(50)]
    with pytest.raises(RuleError, match="already 50"):
        cost_targets.check("new", _body(), KNOWN, full)
    # Replacing one of the 50 is fine.
    cost_targets.check("r3", _body(), KNOWN, full)


def test_a_name_cannot_be_checked_before_the_first_reading() -> None:
    known = Known(subscriptions=("MVP SC",))
    body = _body(scope={"kind": "resource", "name": "anything"}, measure="latest_finished_day")
    assert cost_targets.check("t", body, known, []).scope.name == "anything"
    with pytest.raises(RuleError, match="not configured"):
        cost_targets.check("t", _body(), Known(), [])


# ── seeding ─────────────────────────────────────────────────────────────────


def _azure_with(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    snap = _snapshot(
        subscriptions=[(f"{i:08d}-0000-4000-8000-000000000000", n) for i, n in enumerate(names)]
    )
    monkeypatch.setattr(azure_cost, "snapshot", lambda environ=None: snap)


def test_seeding_happens_once_and_never_re_adds_a_deleted_rule(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _azure_with(monkeypatch, ["MVP SC", "MVP 2025", "AnalyticEndeavors", "Other"])
    store = cost_targets.RuleStore(tmp_path / "rules.json")
    first = cost_targets.load_rules(store)
    ids = {r.id for r in first.rules}
    assert ids == {
        "seed-month-mvp-sc",
        "seed-month-mvp-2025",
        "seed-month-analyticendeavors",
        "seed-resource-day",
        "seed-fabric-hours",
    }
    limits = {r.id: r.limit for r in first.rules}
    assert limits["seed-month-mvp-sc"] == 600 and limits["seed-month-analyticendeavors"] == 50
    assert limits["seed-resource-day"] == 25 and limits["seed-fabric-hours"] == 6
    first.rules = [r for r in first.rules if r.id != "seed-resource-day"]
    store.write(first)
    again = cost_targets.load_rules(store)
    assert "seed-resource-day" not in {r.id for r in again.rules}
    # A fresh store on the same file, and even an emptied file, stay unseeded.
    first.rules = []
    store.write(first)
    assert cost_targets.load_rules(cost_targets.RuleStore(tmp_path / "rules.json")).rules == []


def test_no_seed_without_azure_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(azure_cost, "snapshot", lambda environ=None: None)
    store = cost_targets.RuleStore(tmp_path / "rules.json")
    assert cost_targets.load_rules(store).rules == []
    assert not (tmp_path / "rules.json").exists()


def test_a_hand_made_file_without_the_flag_is_not_seeded_over(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _azure_with(monkeypatch, ["MVP SC"])
    path = tmp_path / "rules.json"
    path.write_text('{"rules": []}', encoding="utf-8")
    assert cost_targets.load_rules(cost_targets.RuleStore(path)).rules == []


def test_a_corrupt_rules_file_is_reported_not_overwritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _azure_with(monkeypatch, ["MVP SC"])
    path = tmp_path / "rules.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(cost_targets.RulesFileError, match="Fix or remove it"):
        cost_targets.load_rules(cost_targets.RuleStore(path))
    assert path.read_text(encoding="utf-8") == "{not json"


# ── measures ────────────────────────────────────────────────────────────────


def test_month_to_date_for_a_subscription_all_and_a_service() -> None:
    spend = _spend()
    over = cost_targets.evaluate_spend(
        _rule("a", "month_to_date", "subscription", "MVP 2025", 600), spend
    )
    assert over.status == "over" and over.value == 640.0 and over.hits[0].subject == "MVP 2025"
    assert over.hits[0].period == "2026-10" and over.data_date == "2026-10-20"
    under = cost_targets.evaluate_spend(
        _rule("b", "month_to_date", "subscription", "MVP SC", 600), spend
    )
    assert under.status == "ok" and under.value == 200.0
    total = cost_targets.evaluate_spend(_rule("c", "month_to_date", "all", None, 800), spend)
    assert total.status == "over" and total.hits[0].subject == "Azure" and total.value == 840.0
    service = cost_targets.evaluate_spend(
        _rule("d", "month_to_date", "service", "SQL Database", 500), spend
    )
    assert service.status == "over" and service.value == 550.0


def test_latest_finished_day_for_a_resource_and_for_each_resource() -> None:
    spend = _spend()
    one = cost_targets.evaluate_spend(
        _rule("a", "latest_finished_day", "resource", "sql-prod-01", 25), spend
    )
    assert one.status == "over" and one.hits[0].value == 31.0
    assert one.hits[0].period == "2026-10-20"
    each = cost_targets.evaluate_spend(
        _rule("b", "latest_finished_day", "resource", None, 25), spend
    )
    assert [h.subject for h in each.hits] == ["sql-prod-01"]
    sub = cost_targets.evaluate_spend(
        _rule("c", "latest_finished_day", "subscription", "MVP 2025", 30), spend
    )
    assert sub.status == "over" and sub.hits[0].value == 32.0
    allday = cost_targets.evaluate_spend(_rule("d", "latest_finished_day", "all", None, 42), spend)
    assert allday.status == "ok" and allday.value == 42.0


def test_a_stale_resource_day_is_not_judged() -> None:
    rid = "/s/x/sql-old"
    old = _reading(_days(1, 20, 1.0), resources={rid: {"2026-10-10": 99.0}})
    spend = _spend(readings={SUB_SC: old, SUB_25: _reading(_days(1, 20, 1.0))})
    ev = cost_targets.evaluate_spend(
        _rule("a", "latest_finished_day", "resource", None, 25), spend
    )
    assert ev.status == "ok" and ev.hits == []


def test_same_named_resources_in_two_subscriptions_are_not_summed() -> None:
    def rid(sub: str) -> str:
        return f"/subscriptions/{sub}/resourceGroups/rg/providers/Microsoft.Sql/servers/shared"

    day = {"2026-10-20": 15.0}
    spend = _spend(
        readings={
            SUB_SC: _reading(_days(1, 20, 1.0), resources={rid(SUB_SC): day}),
            SUB_25: _reading(_days(1, 20, 1.0), resources={rid(SUB_25): day}),
        }
    )
    ev = cost_targets.evaluate_spend(
        _rule("a", "latest_finished_day", "resource", "shared", 25), spend
    )
    assert ev.status == "ok" and ev.hits == []


def test_month_end_forecast_matches_the_cards_pace() -> None:
    spend = _spend()
    ev = cost_targets.evaluate_spend(
        _rule("a", "month_end_forecast", "subscription", "MVP 2025", 900), spend
    )
    # 640 spent over 20 days with data, scaled to 31 days.
    assert ev.status == "over" and ev.hits[0].value == 992.0
    early = _spend(
        readings={SUB_SC: _reading(_days(1, 2, 10.0)), SUB_25: _reading(_days(1, 2, 10.0))}
    )
    assert (
        cost_targets.evaluate_spend(_rule("b", "month_end_forecast", "all", None, 1), early).status
        == "cannot-evaluate"
    )


def test_a_subscription_billed_in_another_currency_is_skipped() -> None:
    snap = _snapshot(
        subscriptions=[(SUB_SC, "MVP SC"), (SUB_25, "MVP 2025"), (SUB_EUR, "Euro")],
    )
    snap["readings"][SUB_EUR] = {
        **_reading(_days(1, 20, 500.0), currency="EUR"),
        "readAt": snap["at"].isoformat(),
    }
    spend = cost_targets.spend_from(snap, NOW)
    total = cost_targets.evaluate_spend(_rule("a", "month_to_date", "all", None, 800), spend)
    assert total.status == "over" and total.value == 840.0
    named = cost_targets.evaluate_spend(
        _rule("b", "month_to_date", "subscription", "Euro", 1), spend
    )
    assert named.status == "cannot-evaluate" and "billed in EUR" in (named.reason or "")


def test_a_reading_with_no_read_time_is_too_old_never_the_live_good_time() -> None:
    snap = _snapshot(limited=True, goodAt=NOW - timedelta(minutes=1))
    del snap["readings"][SUB_SC]["readAt"]
    spend = cost_targets.spend_from(snap, NOW)
    assert "MVP SC" in spend.problems and "MVP SC" not in spend.subs
    assert "MVP 2025" in spend.subs


def test_a_missing_limited_old_or_failing_reading_cannot_be_evaluated() -> None:
    rule = _rule("a", "month_to_date", "all", None, 1.0)
    cases = {
        "not configured": cost_targets.spend_from(None, NOW),
        "not been read": _spend(at=None),
        "too old": _spend(
            at=NOW - timedelta(hours=7),
            readings={
                SUB_SC: {**_reading(_days(1, 20, 10.0)), "readAt": _ago(hours=7)},
                SUB_25: {**_reading(_days(1, 20, 32.0)), "readAt": _ago(hours=8)},
            },
        ),
    }
    for fragment, spend in cases.items():
        ev = cost_targets.evaluate_spend(rule, spend)
        assert ev.status == "cannot-evaluate" and fragment in (ev.reason or ""), fragment
    # A failing subscription: a total already over still alerts, one under cannot be trusted.
    failing = _spend(errors={SUB_SC: "403 forbidden"})
    over = cost_targets.evaluate_spend(_rule("b", "month_to_date", "all", None, 100), failing)
    assert over.status == "over"
    under = cost_targets.evaluate_spend(_rule("c", "month_to_date", "all", None, 5000), failing)
    assert under.status == "cannot-evaluate" and "MVP SC could not be read" in (under.reason or "")
    named = cost_targets.evaluate_spend(
        _rule("d", "month_to_date", "subscription", "MVP SC", 1), failing
    )
    assert named.status == "cannot-evaluate"
    unread = _spend(readings={SUB_25: _reading(_days(1, 20, 32.0))})
    assert (
        cost_targets.evaluate_spend(_rule("e", "month_to_date", "all", None, 5000), unread).status
        == "cannot-evaluate"
    )


def test_a_rate_limit_alone_blanks_nothing_and_says_so_in_a_note() -> None:
    limited = _spend(
        limited=True, rateLimit={"retryAt": None, "goodAt": NOW - timedelta(minutes=2)}
    )
    assert limited.unavailable is None
    assert limited.note and "rate limiting" in limited.note and "figures as of" in limited.note
    ev = cost_targets.evaluate_spend(_rule("a", "month_to_date", "all", None, 100), limited)
    assert ev.status == "over" and ev.value == 840.0
    assert _spend().note is None


def test_one_old_subscription_blocks_only_the_rules_that_need_it() -> None:
    old = _spend(
        limited=True,
        readings={
            SUB_SC: {**_reading(_days(1, 20, 10.0)), "readAt": _ago(hours=7)},
            SUB_25: {**_reading(_days(1, 20, 32.0)), "readAt": _ago(minutes=3)},
        },
    )
    assert old.unavailable is None
    assert "MVP SC has not been read since" in old.problems["MVP SC"]
    assert old.problems["MVP SC"].endswith(" UTC")
    own = cost_targets.evaluate_spend(
        _rule("a", "month_to_date", "subscription", "MVP 2025", 600), old
    )
    assert own.status == "over"
    stale = cost_targets.evaluate_spend(
        _rule("b", "month_to_date", "subscription", "MVP SC", 600), old
    )
    assert stale.status == "cannot-evaluate" and "has not been read since" in (stale.reason or "")
    total_under = cost_targets.evaluate_spend(_rule("c", "month_to_date", "all", None, 5000), old)
    assert total_under.status == "cannot-evaluate"
    total_over = cost_targets.evaluate_spend(_rule("d", "month_to_date", "all", None, 100), old)
    assert total_over.status == "over"


def _rows(**states: str) -> list[dict[str, Any]]:
    return [
        {"name": name, "state": state, "resumedAt": None, "hourlyUsd": 0.72}
        for name, state in states.items()
    ]


def test_subscription_names_are_known_while_the_reading_is_unavailable() -> None:
    spend = _spend(at=None)
    assert spend.unavailable and not spend.subs
    known = cost_targets.known_from(spend, [])
    assert known.subscriptions == ("MVP SC", "MVP 2025")
    cost_targets.check("r1", _body(), known, [])


def test_a_capacity_missing_from_a_reading_loses_its_start() -> None:
    state = cost_targets.AlertState()
    cost_targets.track_active(state, _rows(cap1="Active"), NOW)
    cost_targets.track_active(state, _rows(cap2="Paused"), NOW + timedelta(hours=1))
    assert state.active_since == {}
    began = cost_targets.track_active(state, _rows(cap1="Active"), NOW + timedelta(hours=9))
    assert began == ["cap1"] and state.active_since["cap1"] == "2026-10-21T21:00:00Z"


def test_a_pause_and_resume_between_ticks_restarts_the_episode() -> None:
    state = cost_targets.AlertState()
    cost_targets.track_active(state, _rows(cap1="Active"), NOW)
    rows = _rows(cap1="Active")
    rows[0]["resumedAt"] = cost_targets._iso(NOW + timedelta(hours=2))
    cost_targets.track_active(state, rows, NOW + timedelta(hours=3))
    assert state.active_since["cap1"] == "2026-10-21T14:00:00Z"


def test_fabric_hours_run_from_the_first_time_a_capacity_was_seen_active() -> None:
    state = cost_targets.AlertState()
    rows = _rows(cap1="Active", cap2="Paused")
    cost_targets.track_active(state, rows, NOW)
    assert state.active_since == {"cap1": "2026-10-21T12:00:00Z"}
    rule = _rule("f", "fabric_active_hours", "capacity", None, 6)
    soon = cost_targets.evaluate_fabric(
        rule, rows, None, state.active_since, NOW + timedelta(hours=5)
    )
    assert soon.status == "ok" and soon.value == 5.0
    late = cost_targets.evaluate_fabric(
        rule, rows, None, state.active_since, NOW + timedelta(hours=7)
    )
    assert late.status == "over" and late.hits[0].value == 7.0
    assert late.hits[0].period == "2026-10-21T12:00:00Z" and late.hits[0].hourly == 0.72
    # Seen again later: the start does not move. Paused: the episode ends.
    cost_targets.track_active(state, rows, NOW + timedelta(hours=3))
    assert state.active_since["cap1"] == "2026-10-21T12:00:00Z"
    cost_targets.track_active(state, _rows(cap1="Paused"), NOW + timedelta(hours=4))
    assert state.active_since == {}


def test_a_recent_resumed_at_starts_the_episode() -> None:
    state = cost_targets.AlertState()
    rows = _rows(cap1="Active")
    rows[0]["resumedAt"] = "2026-10-21T08:00:00Z"
    cost_targets.track_active(state, rows, NOW)
    assert state.active_since["cap1"] == "2026-10-21T08:00:00Z"
    stale = cost_targets.AlertState()
    rows[0]["resumedAt"] = "2026-09-01T08:00:00Z"
    cost_targets.track_active(stale, rows, NOW)
    assert stale.active_since["cap1"] == "2026-10-21T12:00:00Z"


def test_fabric_rules_that_cannot_be_evaluated() -> None:
    rule = _rule("f", "fabric_active_hours", "capacity", "cap9", 6)
    assert cost_targets.evaluate_fabric(
        rule, None, "Fabric is not configured.", {}, NOW
    ).status == ("cannot-evaluate")
    missing = cost_targets.evaluate_fabric(rule, _rows(cap1="Active"), None, {}, NOW)
    assert missing.status == "cannot-evaluate" and "cap9" in (missing.reason or "")
    broken = [{"name": "cap1", "state": "Unknown", "error": "timed out"}]
    each = _rule("g", "fabric_active_hours", "capacity", None, 6)
    assert cost_targets.evaluate_fabric(each, broken, None, {}, NOW).status == "cannot-evaluate"


# ── the alert pass ──────────────────────────────────────────────────────────


def _alerts(tmp_path: Path | None = None) -> cost_targets.AlertStore:
    return cost_targets.AlertStore(tmp_path / "alerts.json" if tmp_path else None)


async def _spend_run(
    rules: list[Rule],
    fake: FakeStore,
    alerts: cost_targets.AlertStore,
    spend: cost_targets.Spend,
    now: datetime = NOW,
) -> cost_targets.Outcome:
    return await cost_targets.run(
        "spend", rules=rules, spend=spend, now=now, cfg=CFG, client=fake, alerts=alerts
    )  # type: ignore[arg-type]


async def test_a_crossed_target_files_one_task_with_a_morning_alarm() -> None:
    fake, alerts = FakeStore(), _alerts()
    rules = [_rule("mvp", "month_to_date", "subscription", "MVP 2025", 600)]
    out = await _spend_run(rules, fake, alerts, _spend())
    assert len(out.created) == 1 and out.errors == []
    task = fake.made()[0]
    assert task["title"] == "MVP 2025 is at $640 this month (limit $600)"
    assert task["bucket_id"] == "L-AE" and task["tags"] == ["ae"]
    # 09:00 in Los Angeles on the 21st is 16:00 UTC; the pass ran at noon UTC.
    assert task["reminder_at"] == "2026-10-21T16:00:00Z" and task["due_at"] == task["reminder_at"]
    notes = task["notes"]
    assert "Target: Rule mvp" in notes and "Data through 2026-10-20." in notes
    assert "Azure posts costs 8 to 24 hours late" in notes
    assert "Admin Center, Azure card, Targets" in notes
    assert notes.splitlines()[-1] == "Alert key: cost mvp MVP%202025 2026-10"
    dashes = ("-" * 2, chr(0x2014))
    assert not any(dash in task["title"] + notes for dash in dashes)
    # A second pass changes nothing: one alert per rule per period.
    again = await _spend_run(rules, fake, alerts, _spend())
    assert again.created == [] and len(fake.made()) == 1


async def test_the_alert_closes_when_the_value_falls_back_and_re_arms() -> None:
    fake, alerts = FakeStore(), _alerts()
    rules = [_rule("mvp", "month_to_date", "subscription", "MVP 2025", 600)]
    await _spend_run(rules, fake, alerts, _spend())
    cheaper = _snapshot()
    cheaper["readings"][SUB_25] = {**_reading(_days(1, 20, 20.0)), "readAt": NOW.isoformat()}
    out = await _spend_run(rules, fake, alerts, cost_targets.spend_from(cheaper, NOW))
    assert len(out.closed) == 1 and fake.open_titles() == []
    assert "Closed 2026-10-21" in fake.tasks["T1"]["notes"]
    assert alerts.read().alerts == {}
    # Over again in the same month: a new task, not a silent skip.
    again = await _spend_run(rules, fake, alerts, _spend())
    assert len(again.created) == 1 and len(fake.made()) == 2


async def test_a_new_month_forgets_the_old_task_and_leaves_it_open() -> None:
    fake, alerts = FakeStore(), _alerts()
    rules = [_rule("mvp", "month_to_date", "subscription", "MVP 2025", 600)]
    await _spend_run(rules, fake, alerts, _spend())
    november = NOW.replace(month=11, day=2)
    snap = _snapshot(at=november - timedelta(minutes=5))
    snap["readings"][SUB_25] = {
        **_reading({**_days(1, 31, 32.0), **_days(1, 1, 5.0, "2026-11")}),
        "readAt": november.isoformat(),
    }
    out = await _spend_run(rules, fake, alerts, cost_targets.spend_from(snap, november), november)
    assert out.forgotten and out.closed == [] and out.created == []
    assert fake.tasks["T1"]["status"] == "open"
    assert alerts.read().alerts == {}


async def test_the_daily_target_alerts_once_a_day() -> None:
    fake, alerts = FakeStore(), _alerts()
    rules = [_rule("day", "latest_finished_day", "resource", None, 25)]
    await _spend_run(rules, fake, alerts, _spend())
    assert fake.made()[0]["title"] == "sql-prod-01 cost $31 on 2026-10-20 (limit $25 a day)"
    await _spend_run(rules, fake, alerts, _spend())
    assert len(fake.made()) == 1
    next_day = NOW + timedelta(days=1)
    snap = _snapshot(at=next_day - timedelta(minutes=5))
    rid = next(iter(snap["readings"][SUB_25]["resources"]))
    snap["readings"][SUB_25]["resources"][rid]["2026-10-21"] = 40.0
    snap["readings"][SUB_25]["daily"]["2026-10-21"] = 50.0
    out = await _spend_run(rules, fake, alerts, cost_targets.spend_from(snap, next_day), next_day)
    assert len(out.created) == 1 and out.forgotten
    assert "on 2026-10-21" in fake.made()[1]["title"]


async def test_a_rate_limited_or_missing_reading_files_nothing_and_closes_nothing() -> None:
    fake, alerts = FakeStore(), _alerts()
    rules = [_rule("mvp", "month_to_date", "subscription", "MVP 2025", 600)]
    await _spend_run(rules, fake, alerts, _spend())
    for spend in (_spend(at=None), cost_targets.spend_from(None, NOW)):
        out = await _spend_run(rules, fake, alerts, spend)
        assert out.created == [] and out.closed == []
    assert fake.open_titles() != []
    empty = _alerts()
    quiet = await _spend_run(rules, FakeStore(), empty, _spend(at=None))
    assert quiet.created == []


async def test_turning_a_target_off_or_deleting_it_closes_its_task() -> None:
    fake, alerts = FakeStore(), _alerts()
    rule = _rule("mvp", "month_to_date", "subscription", "MVP 2025", 600)
    await _spend_run([rule], fake, alerts, _spend())
    off = rule.model_copy(update={"enabled": False})
    out = await _spend_run([off], fake, alerts, _spend())
    assert len(out.closed) == 1 and "turned off" in fake.tasks["T1"]["notes"]
    await _spend_run([rule], fake, alerts, _spend())
    gone = await _spend_run([], fake, alerts, _spend())
    assert len(gone.closed) == 1 and "deleted" in fake.tasks["T2"]["notes"]


async def test_a_task_ticked_done_is_not_filed_again_for_the_period() -> None:
    fake, alerts = FakeStore(), _alerts()
    rules = [_rule("mvp", "month_to_date", "subscription", "MVP 2025", 600)]
    await _spend_run(rules, fake, alerts, _spend())
    fake.tasks["T1"]["status"] = "done"
    again = await _spend_run(rules, fake, alerts, _spend())
    assert again.created == [] and len(fake.made()) == 1


async def test_lost_state_is_rebuilt_from_the_alert_key_line(tmp_path: Path) -> None:
    fake = FakeStore()
    rules = [_rule("mvp", "month_to_date", "subscription", "MVP 2025", 600)]
    await _spend_run(rules, fake, _alerts(tmp_path), _spend())
    (tmp_path / "alerts.json").unlink()
    rebuilt = _alerts(tmp_path)
    out = await _spend_run(rules, fake, rebuilt, _spend())
    assert out.created == [] and len(fake.made()) == 1
    assert next(iter(rebuilt.read().alerts.values())).task_id == "T1"


async def test_lost_state_does_not_refile_a_task_already_ticked_done(tmp_path: Path) -> None:
    fake = FakeStore()
    rules = [_rule("mvp", "month_to_date", "subscription", "MVP 2025", 600)]
    await _spend_run(rules, fake, _alerts(tmp_path), _spend())
    fake.tasks["T1"]["status"] = "done"
    (tmp_path / "alerts.json").unlink()
    out = await _spend_run(rules, fake, _alerts(tmp_path), _spend())
    assert out.created == [] and len(fake.made()) == 1


def test_the_targets_answer_is_held_under_the_bridge_cap() -> None:
    long = "r" * 300
    rids = {
        f"/subscriptions/{SUB_25}/resourceGroups/rg/providers/X/y/{long}{n}": {"2026-10-20": 99.0}
        for n in range(100)
    }
    spend = _spend(
        readings={
            SUB_SC: _reading(_days(1, 20, 1.0)),
            SUB_25: _reading(_days(1, 20, 1.0), resources=rids),
        }
    )
    rules = [_rule(f"r{n}", "latest_finished_day", "resource", None, 25) for n in range(50)]
    data = cost_targets.RulesFile(rules=rules, seeded=True)
    answer = cost_targets.view(data, cost_targets.AlertState(), spend, CFG, NOW, None)
    assert len(json.dumps(answer, separators=(",", ":"))) <= cost_targets.ANSWER_MAX
    assert answer["truncated"] is True


async def test_a_create_that_landed_without_an_answer_is_not_sent_twice() -> None:
    fake = FakeStore()
    fake.create_fails_after_landing = True
    rules = [_rule("mvp", "month_to_date", "subscription", "MVP 2025", 600)]
    alerts = _alerts()
    out = await _spend_run(rules, fake, alerts, _spend())
    assert len(out.created) == 1 and len(fake.made()) == 1
    assert (await _spend_run(rules, fake, alerts, _spend())).created == []
    assert len(fake.made()) == 1


async def test_nothing_is_filed_when_the_task_store_is_not_configured() -> None:
    fake = FakeStore()
    off = cost_targets.Config(
        on=False, off_reason="no store", list_name="x", time="09:00", tz="UTC"
    )
    rules = [_rule("mvp", "month_to_date", "subscription", "MVP 2025", 600)]
    out = await cost_targets.run(
        "spend", rules=rules, spend=_spend(), now=NOW, cfg=off, client=fake, alerts=_alerts()
    )  # type: ignore[arg-type]
    assert out.created == [] and fake.calls == []


async def test_the_fabric_alert_rings_minutes_out_and_closes_when_the_capacity_stops() -> None:
    fake, alerts = FakeStore(), _alerts()
    rules = [_rule("f", "fabric_active_hours", "capacity", None, 6)]

    async def fabric_run(rows: list[dict[str, Any]], at: datetime) -> cost_targets.Outcome:
        return await cost_targets.run(
            "fabric", rules=rules, rows=rows, now=at, cfg=CFG, client=fake, alerts=alerts
        )  # type: ignore[arg-type]

    first = await fabric_run(_rows(cap1="Active"), NOW)
    assert first.created == [] and alerts.read().active_since == {"cap1": "2026-10-21T12:00:00Z"}
    late = NOW + timedelta(hours=7)
    out = await fabric_run(_rows(cap1="Active"), late)
    assert len(out.created) == 1
    task = fake.made()[0]
    assert task["title"] == "Fabric capacity cap1 has been Active for 7 hours (limit 6)"
    assert task["reminder_at"] == "2026-10-21T19:03:00Z" and task["urgent"] == 1
    assert (
        "about $0.72 an hour" in task["notes"]
        and "Active since 2026-10-21T12:00:00Z" in task["notes"]
    )
    assert (await fabric_run(_rows(cap1="Active"), late + timedelta(hours=1))).created == []
    stopped = await fabric_run(_rows(cap1="Paused"), late + timedelta(hours=2))
    assert len(stopped.closed) == 1 and fake.open_titles() == []
    assert alerts.read().active_since == {}


async def test_fabric_state_lost_mid_episode_keeps_the_open_tasks_start() -> None:
    fake = FakeStore()
    rules = [_rule("f", "fabric_active_hours", "capacity", None, 6)]
    state = _alerts()
    state.write(cost_targets.AlertState(active_since={"cap1": "2026-10-21T12:00:00Z"}))

    async def go(store: cost_targets.AlertStore, at: datetime) -> cost_targets.Outcome:
        return await cost_targets.run(
            "fabric",
            rules=rules,
            rows=_rows(cap1="Active"),
            now=at,
            cfg=CFG,
            client=fake,  # type: ignore[arg-type]
            alerts=store,
        )

    await go(state, NOW + timedelta(hours=7))
    lost = _alerts()
    again = await go(lost, NOW + timedelta(hours=8))
    # The episode did not restart: the task's start was read back, nothing is filed twice.
    assert again.created == [] and len(fake.made()) == 1
    rebuilt = lost.read()
    assert rebuilt.active_since["cap1"] == "2026-10-21T12:00:00Z"
    assert next(iter(rebuilt.alerts.values())).task_id == "T1"


async def test_a_tick_judges_each_new_reading_once_and_the_fabric_when_due(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snap = _snapshot()
    monkeypatch.setattr(azure_cost, "snapshot", lambda environ=None: snap)
    fake = FakeStore()
    seen: list[str] = []

    class Client(FakeStore):
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            self.__dict__ = fake.__dict__

    monkeypatch.setattr(taskstore, "TaskStoreClient", Client)
    monkeypatch.setenv("AE_TASK_STORE_URL", "https://tasks.test")

    async def rows() -> tuple[list[dict[str, Any]] | None, str | None]:
        seen.append("fabric")
        return _rows(cap1="Active"), None

    monkeypatch.setattr(cost_targets, "fabric_reading", rows)
    cost_targets.reset_stores(
        cost_targets.RuleStore(tmp_path / "r.json"), cost_targets.AlertStore(tmp_path / "a.json")
    )
    await cost_targets.tick(fabric_due=False, now=NOW)
    titles = sorted(t["title"] for t in fake.tasks.values())
    # The seeded defaults: MVP 2025 is over 600 for the month and sql-prod-01 over 25 a day.
    assert titles == [
        "MVP 2025 is at $640 this month (limit $600)",
        "sql-prod-01 cost $31 on 2026-10-20 (limit $25 a day)",
    ]
    count = len(fake.made())
    await cost_targets.tick(fabric_due=False, now=NOW)
    assert len(fake.made()) == count and seen == []
    await cost_targets.tick(fabric_due=True, now=NOW)
    assert seen == ["fabric"] and cost_targets.fabric_memory() is not None
    assert cost_targets.alert_store().read().active_since == {"cap1": "2026-10-21T12:00:00Z"}


async def test_the_loop_survives_a_failing_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**_kw: Any) -> None:
        raise RuntimeError("secret-looking text")

    monkeypatch.setattr(cost_targets, "tick", boom)
    monkeypatch.setattr(cost_targets, "STARTUP_DELAY_S", 0.0)
    monkeypatch.setattr(cost_targets, "POLL_S", 0.01)
    import asyncio

    task = asyncio.get_running_loop().create_task(cost_targets._loop())
    await asyncio.sleep(0.1)
    assert not task.done()
    task.cancel()


# ── routes ──────────────────────────────────────────────────────────────────


@pytest.fixture
def azure_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = _snapshot(at=datetime.now(UTC) - timedelta(minutes=5))
    monkeypatch.setattr(azure_cost, "snapshot", lambda environ=None: snap)


async def test_targets_routes_are_admin_only(
    client: httpx.AsyncClient, ae_admins: set[str], azure_snapshot: None
) -> None:
    body = {"name": "x", "scope": {"kind": "all"}, "measure": "month_to_date", "limit": 10}
    refusals = [
        await client.get("/v1/ae/azure-cost/targets", headers=STEVE),
        await client.put("/v1/ae/azure-cost/targets/t1", json=body, headers=STEVE),
        await client.delete("/v1/ae/azure-cost/targets/t1", headers=STEVE),
    ]
    for refused in refusals:
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "admin_only"
    allowed = await client.get("/v1/ae/azure-cost/targets", headers=REID)
    assert allowed.status_code == 200, allowed.text


async def test_targets_crud_and_status(
    client: httpx.AsyncClient, ae_admins: set[str], azure_snapshot: None
) -> None:
    first = await client.get("/v1/ae/azure-cost/targets", headers=REID)
    assert first.status_code == 200, first.text
    listed = first.json()
    ids = {r["id"] for r in listed["rules"]}
    assert {"seed-month-mvp-sc", "seed-month-mvp-2025", "seed-resource-day"} <= ids
    by_id = {r["id"]: r for r in listed["rules"]}
    assert by_id["seed-month-mvp-2025"]["status"] == "over"
    assert by_id["seed-month-mvp-sc"]["status"] == "ok"
    assert by_id["seed-fabric-hours"]["status"] == "cannot-evaluate"
    assert listed["scopes"]["subscriptions"] == ["MVP SC", "MVP 2025"]
    assert "sql-prod-01" in listed["scopes"]["resources"] and listed["max"] == 50

    body = {
        "name": "Whole tenant",
        "enabled": True,
        "scope": {"kind": "all"},
        "measure": "month_to_date",
        "limit": 5000,
    }
    saved = await client.put("/v1/ae/azure-cost/targets/t-all", json=body, headers=REID)
    assert saved.status_code == 200, saved.text
    assert saved.json()["rule"]["status"] == "ok" and saved.json()["rule"]["value"] == 840.0
    replaced = await client.put(
        "/v1/ae/azure-cost/targets/t-all", json={**body, "limit": 100}, headers=REID
    )
    assert replaced.json()["rule"]["status"] == "over"
    again = (await client.get("/v1/ae/azure-cost/targets", headers=REID)).json()
    assert sum(1 for r in again["rules"] if r["id"] == "t-all") == 1

    bad = await client.put(
        "/v1/ae/azure-cost/targets/t-bad",
        json={**body, "scope": {"kind": "subscription", "name": "Nope"}},
        headers=REID,
    )
    assert bad.status_code == 422 and "Pick one from the list" in bad.json()["error"]["message"]
    nan = await client.put(
        "/v1/ae/azure-cost/targets/t-bad", json={**body, "limit": 0}, headers=REID
    )
    assert nan.status_code == 422

    # The extension host sends only GET, POST and PATCH, so the card uses these two.
    via_post = await client.post(
        "/v1/ae/azure-cost/targets/t-post", json={**body, "limit": 77}, headers=REID
    )
    assert via_post.status_code == 200 and via_post.json()["rule"]["limit"] == 77
    deleted_post = await client.post("/v1/ae/azure-cost/targets/t-post/delete", headers=REID)
    assert deleted_post.status_code == 200 and deleted_post.json()["deleted"] == "t-post"
    steve = await client.post("/v1/ae/azure-cost/targets/t-post/delete", headers=STEVE)
    assert steve.status_code == 403
    gone = await client.delete("/v1/ae/azure-cost/targets/t-all", headers=REID)
    assert gone.status_code == 200 and gone.json()["deleted"] == "t-all"
    missing = await client.delete("/v1/ae/azure-cost/targets/t-all", headers=REID)
    assert missing.status_code == 404
    # The seeded defaults stay deleted.
    await client.delete("/v1/ae/azure-cost/targets/seed-resource-day", headers=REID)
    final = (await client.get("/v1/ae/azure-cost/targets", headers=REID)).json()
    assert "seed-resource-day" not in {r["id"] for r in final["rules"]}


async def test_targets_report_an_unreadable_file(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    azure_snapshot: None,
    tmp_path: Path,
) -> None:
    path = tmp_path / "rules.json"
    path.write_text("{oops", encoding="utf-8")
    cost_targets.reset_stores(cost_targets.RuleStore(path))
    response = await client.get("/v1/ae/azure-cost/targets", headers=REID)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "cost_targets_unreadable"
    assert path.read_text(encoding="utf-8") == "{oops"


async def test_the_targets_answer_stays_under_the_bridge_limit_with_a_rate_limit_note(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    long_reason = "x" * 400
    snap = _snapshot(limited=True, rateLimit={"retryAt": None, "goodAt": NOW})
    snap["subscriptions"] = [
        (f"{i:08d}-0000-4000-8000-000000000000", f"Sub{i}") for i in range(12)
    ]
    monkeypatch.setattr(azure_cost, "snapshot", lambda environ=None: snap)
    monkeypatch.setattr(
        cost_targets,
        "evaluate_spend",
        lambda *_a, **_k: cost_targets.Evaluation("cannot-evaluate", long_reason),
    )
    store = cost_targets.RuleStore()
    store.write(
        cost_targets.RulesFile(
            seeded=True,
            rules=[
                _rule(f"r{i}", "month_to_date", "subscription", "S" * 60, 1.0).model_copy(
                    update={"name": "N" * 80}
                )
                for i in range(50)
            ],
        )
    )
    cost_targets.reset_stores(store)
    response = await client.get("/v1/ae/azure-cost/targets", headers=REID)
    assert response.status_code == 200, response.text
    assert len(response.text) < 32_768, len(response.text)
    assert "rate limiting" in response.text


async def test_the_targets_answer_stays_under_the_bridge_limit(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The extension bridge refuses an answer over 32,768 characters: fifty targets that all
    # cannot be checked, with long names and a long reason each, and long pick lists.
    long_reason = "x" * 400
    snap = _snapshot(at=None)
    snap["subscriptions"] = [
        (f"{i:08d}-0000-4000-8000-000000000000", f"Sub{i}") for i in range(12)
    ]
    monkeypatch.setattr(azure_cost, "snapshot", lambda environ=None: snap)
    monkeypatch.setattr(
        cost_targets,
        "evaluate_spend",
        lambda *_a, **_k: cost_targets.Evaluation("cannot-evaluate", long_reason),
    )
    store = cost_targets.RuleStore()
    store.write(
        cost_targets.RulesFile(
            seeded=True,
            rules=[
                _rule(f"r{i}", "month_to_date", "subscription", "S" * 60, 1.0).model_copy(
                    update={"name": "N" * 80}
                )
                for i in range(50)
            ],
        )
    )
    cost_targets.reset_stores(store)
    response = await client.get("/v1/ae/azure-cost/targets", headers=REID)
    assert response.status_code == 200, response.text
    assert len(response.text) < 32_768, len(response.text)
    assert len(response.json()["rules"]) == 50
    assert all(len(r["reason"]) <= 160 for r in response.json()["rules"])
