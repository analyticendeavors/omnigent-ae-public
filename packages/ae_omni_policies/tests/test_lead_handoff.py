"""The lead handoff: who is nudged, when, and how a half-finished handoff is completed."""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from ae_omni_policies import labels, lead_handoff, schedules

OLD = "a30d81920fd548eebef40eca969eeac1"
NEW = "b41e92031ae659ffcf51f5fdb7a0fdb2"
CHILD = "75db014279a34d74804c6f228c9699f1"
LEAD_LABELS = {
    "omnigent.wrapper": "claude-code-native-ui",
    "omnigent.last_context_tokens": "400000",
    labels.STATUS: "working",
}


@pytest.fixture(autouse=True)
def _forget_locks() -> None:
    lead_handoff._locks.clear()


def _row(session_id: str, labels_: dict[str, str] | None = None, **fields: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": session_id,
        "labels": dict(labels_ or {}),
        "parent_conversation_id": None,
        "kind": "default",
        "archived": False,
        "updated_at": 0,
        "live_status": None,
    }
    values.update(fields)
    return SimpleNamespace(**values)


def _lead(extra: dict[str, str] | None = None, **fields: Any) -> SimpleNamespace:
    return _row(OLD, LEAD_LABELS | (extra or {}), **fields)


# ── which chats are leads ───────────────────────────────────────────────────


def test_a_top_level_claude_chat_is_a_lead_with_or_without_children() -> None:
    assert lead_handoff.is_lead(_lead())
    assert lead_handoff.is_lead(_lead({labels.ROLE: "lead", labels.ORCH: labels.ORCH_UNKNOWN}))
    assert lead_handoff.is_lead(_lead({labels.STATUS: "blocked"}))


@pytest.mark.parametrize(
    ("extra", "fields"),
    [
        ({labels.ORCH: CHILD}, {}),  # a family child
        ({labels.ROLE: "reviewer"}, {}),
        ({labels.ROLE: "builder"}, {}),
        ({"omnigent.fork.source_id": CHILD}, {}),  # a fork
        ({"omnigent.side_chat": "1"}, {}),
        ({"omnigent.wrapper": "codex-native-ui"}, {}),  # a Codex chat
        ({"omnigent.wrapper": ""}, {}),  # a scheduled run or check-in has no native wrapper
        ({labels.STATUS: "reference"}, {}),
        ({labels.STATUS: "parked"}, {}),
        ({labels.STATUS: "review"}, {}),  # not mid-work: a nudge would only wake it
        ({}, {"parent_conversation_id": CHILD}),  # a native sub-agent
        ({}, {"kind": "sub_agent"}),
        ({}, {"archived": True}),
    ],
)
def test_everything_else_is_left_alone(extra: dict[str, str], fields: dict[str, Any]) -> None:
    assert not lead_handoff.is_lead(_lead(extra, **fields))


# ── what the sweep decides ──────────────────────────────────────────────────


def test_decide_suggests_at_the_threshold_and_not_below_it() -> None:
    assert lead_handoff.decide(_lead(), 299_999, 300_000) is None
    assert lead_handoff.decide(_lead(), 300_000, 300_000) == "suggest"


def test_decide_off_switch_clears_a_mark_and_leaves_a_bare_chat() -> None:
    assert lead_handoff.decide(_lead(), 400_000, 0) is None
    marked = _lead({labels.HANDOFF: "suggest", labels.HANDOFF_AT: "310000"})
    assert lead_handoff.decide(marked, 400_000, 0) == "clear"


def test_decide_one_suggestion_only_and_cleared_when_the_context_falls_back() -> None:
    suggested = _lead({labels.HANDOFF: "suggest", labels.HANDOFF_AT: "310000"})
    assert lead_handoff.decide(suggested, 900_000, 300_000) is None
    assert lead_handoff.decide(suggested, 120_000, 300_000) == "clear"
    # An unknown or stale size changes nothing.
    assert lead_handoff.decide(suggested, None, 300_000) is None


def test_decide_snooze_waits_for_another_100k() -> None:
    snoozed = _lead({labels.HANDOFF: "snoozed", labels.HANDOFF_AT: "320000"})
    assert lead_handoff.decide(snoozed, 419_999, 300_000) is None
    assert lead_handoff.decide(snoozed, 420_000, 300_000) == "suggest"
    assert lead_handoff.decide(snoozed, 100_000, 300_000) == "clear"


def test_decide_rewrites_the_legacy_nudged_mark_to_suggest() -> None:
    legacy = _lead({labels.HANDOFF: "nudged", labels.HANDOFF_AT: "400000"})
    assert lead_handoff.decide(legacy, 400_000, 300_000) == "suggest"
    assert lead_handoff.decide(legacy, 100_000, 300_000) == "clear"


@pytest.mark.parametrize("status", ["review", "parked", "reference"])
def test_decide_clears_a_suggestion_of_a_chat_that_is_no_longer_a_lead(status: str) -> None:
    for mark in ("suggest", "nudged"):
        marked = _lead({labels.HANDOFF: mark, labels.STATUS: status})
        assert lead_handoff.decide(marked, 400_000, 300_000) == "clear"


@pytest.mark.parametrize("status", ["review", "parked", "reference"])
def test_decide_keeps_a_snooze_while_the_chat_is_away_from_work(status: str) -> None:
    snoozed = _lead(
        {labels.HANDOFF: "snoozed", labels.HANDOFF_AT: "340000", labels.STATUS: status}
    )
    assert lead_handoff.decide(snoozed, 400_000, 300_000) is None
    # It still ends when the context falls under the size, or the setting is off.
    assert lead_handoff.decide(snoozed, 100_000, 300_000) == "clear"
    assert lead_handoff.decide(snoozed, 400_000, 0) == "clear"
    bare = _lead({labels.STATUS: status})
    assert lead_handoff.decide(bare, 400_000, 300_000) is None


def test_context_tokens_reads_only_a_positive_number() -> None:
    assert lead_handoff.context_tokens(_lead()) == 400_000
    assert lead_handoff.context_tokens(_row(OLD)) is None
    assert lead_handoff.context_tokens(_lead({"omnigent.last_context_tokens": "x"})) is None
    assert lead_handoff.context_tokens(_lead({"omnigent.last_context_tokens": "0"})) is None


def test_a_long_context_suffix_is_dropped_from_the_model() -> None:
    assert lead_handoff.strip_long_context("claude-opus-5-5[1m]") == "claude-opus-5-5"
    assert lead_handoff.strip_long_context("claude-opus-5-5[1M]") == "claude-opus-5-5"
    assert lead_handoff.strip_long_context("claude-opus-5-5") == "claude-opus-5-5"
    assert lead_handoff.strip_long_context(None) is None


# ── the sweep ───────────────────────────────────────────────────────────────


class _Store:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.writes: list[tuple[str, dict[str, str]]] = []
        self.fail_for: set[str] = set()

    def get_conversation(self, session_id: str) -> SimpleNamespace | None:
        return next((row for row in self.rows if row.id == session_id), None)

    def set_labels(self, session_id: str, updates: dict[str, str]) -> None:
        if session_id in self.fail_for:
            raise RuntimeError("write failed")
        self.writes.append((session_id, dict(updates)))
        for row in self.rows:
            if row.id == session_id:
                row.labels.update(updates)


class _World:
    """A sweeper over *rows* with every outside call stubbed."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[SimpleNamespace],
        *,
        threshold: int = 300_000,
    ) -> None:
        self.store = _Store(rows)
        self.age: float | None = 10.0
        self.repaired: list[tuple[str, str]] = []
        monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda store: rows)
        monkeypatch.setattr(lead_handoff, "label_age", lambda *a, **k: self.age)
        self.sweeper = lead_handoff.Sweeper(
            store=self.store,
            owner=lambda session_id: "owner",
            threshold=lambda user: threshold,
            repair=self._repair,
        )

    async def _repair(self, old: str, new: str) -> bool:
        self.repaired.append((old, new))
        return True


async def test_a_lead_past_the_threshold_gets_the_label_and_nothing_is_posted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch, [_lead()])
    assert not hasattr(world.sweeper, "_post")  # the sweep has no way to post into a chat
    assert (await world.sweeper.sweep())["suggested"] == [OLD]
    assert world.store.writes == [(OLD, {labels.HANDOFF: "suggest", labels.HANDOFF_AT: "400000"})]
    # The next sweep sees the label and says nothing more.
    assert (await world.sweeper.sweep())["suggested"] == []
    assert len(world.store.writes) == 1


async def test_a_busy_lead_is_labelled_without_waiting_for_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(
        monkeypatch, [_lead({"omnigent.live_status": "running"}, live_status="running")]
    )
    assert (await world.sweeper.sweep())["suggested"] == [OLD]


@pytest.mark.parametrize("age", [None, 31 * 60.0])
async def test_a_stale_or_unreadable_context_label_is_skipped(
    monkeypatch: pytest.MonkeyPatch, age: float | None
) -> None:
    world = _World(monkeypatch, [_lead()])
    world.age = age
    assert (await world.sweeper.sweep())["suggested"] == []
    assert world.store.writes == []


async def test_off_switch_and_below_threshold_write_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    off = _World(monkeypatch, [_lead()], threshold=0)
    assert (await off.sweeper.sweep())["suggested"] == []
    below = _World(monkeypatch, [_lead({"omnigent.last_context_tokens": "299999"})])
    assert (await below.sweeper.sweep())["suggested"] == []
    assert off.store.writes == [] and below.store.writes == []


async def test_turning_the_setting_off_clears_a_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _lead({labels.HANDOFF: "suggest", labels.HANDOFF_AT: "400000"})
    world = _World(monkeypatch, [row], threshold=0)
    assert (await world.sweeper.sweep())["cleared"] == [OLD]
    assert labels.label(row.labels, labels.HANDOFF) is None
    assert labels.label(row.labels, labels.HANDOFF_AT) is None


async def test_excluded_chats_are_never_labelled(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row("child", LEAD_LABELS | {labels.ORCH: OLD}),
        _row("fork", LEAD_LABELS | {"omnigent.fork.source_id": OLD}),
        _row("side", LEAD_LABELS | {"omnigent.side_chat": "1"}),
        _row("codex", LEAD_LABELS | {"omnigent.wrapper": "codex-native-ui"}),
        _row("done", LEAD_LABELS | {labels.STATUS: "reference"}),
        _row("handed", LEAD_LABELS | {labels.HANDOFF_TO: NEW, labels.STATUS: "reference"}),
    ]
    world = _World(monkeypatch, rows)
    assert (await world.sweeper.sweep())["suggested"] == []
    assert [w for w in world.store.writes if w[1].get(labels.HANDOFF)] == []


async def test_a_snoozed_chat_that_went_to_review_and_back_stays_snoozed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _lead({labels.HANDOFF: "snoozed", labels.HANDOFF_AT: "340000", labels.STATUS: "review"})
    world = _World(monkeypatch, [row])
    assert (await world.sweeper.sweep())["cleared"] == []
    assert world.store.writes == []
    row.labels[labels.STATUS] = "working"
    assert (await world.sweeper.sweep())["suggested"] == []
    assert row.labels[labels.HANDOFF] == "snoozed"


async def test_a_lead_with_no_resolvable_owner_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = _World(monkeypatch, [_lead()])
    world.sweeper._owner = lambda session_id: None
    assert (await world.sweeper.sweep())["suggested"] == []
    assert world.store.writes == []


async def test_a_skipped_ownerless_lead_is_logged_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lead_handoff._warned.clear()
    world = _World(monkeypatch, [_lead()])
    world.sweeper._owner = lambda session_id: None
    with caplog.at_level(logging.DEBUG, logger="ae_omni_policies.lead_handoff"):
        await world.sweeper.sweep()
        await world.sweeper.sweep()
    skipped = [r for r in caplog.records if "no owner for" in r.getMessage()]
    assert [r.levelno for r in skipped] == [logging.INFO, logging.DEBUG]


async def test_the_sweep_decides_again_inside_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    monkeypatch.setattr(lead_handoff, "LOCK_WAIT_S", 2.0)
    row = _lead()
    world = _World(monkeypatch, [row])
    async with lead_handoff.lead_lock(OLD):
        pending = asyncio.ensure_future(world.sweeper.sweep())
        await asyncio.sleep(0.1)
        # The context fell while the sweep waited for the lock.
        row.labels["omnigent.last_context_tokens"] = "100000"
    assert (await pending)["suggested"] == []
    assert world.store.writes == []


async def test_a_chat_that_left_work_loses_its_suggestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _lead({labels.HANDOFF: "suggest", labels.HANDOFF_AT: "400000", labels.STATUS: "review"})
    world = _World(monkeypatch, [row])
    assert (await world.sweeper.sweep())["cleared"] == [OLD]
    assert labels.label(row.labels, labels.HANDOFF) is None


async def test_a_snoozed_lead_is_suggested_again_past_the_growth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _lead({labels.HANDOFF: "snoozed", labels.HANDOFF_AT: "350000"})
    world = _World(monkeypatch, [row])
    assert (await world.sweeper.sweep())["suggested"] == []
    row.labels["omnigent.last_context_tokens"] = "450000"
    assert (await world.sweeper.sweep())["suggested"] == [OLD]
    assert row.labels[labels.HANDOFF] == "suggest"


async def test_a_legacy_nudged_lead_is_rewritten_to_suggest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _lead({labels.HANDOFF: "nudged", labels.HANDOFF_AT: "400000"})
    world = _World(monkeypatch, [row])
    assert (await world.sweeper.sweep())["suggested"] == [OLD]
    assert row.labels[labels.HANDOFF] == "suggest"


async def test_a_suggested_lead_whose_context_fell_is_cleared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _lead(
        {
            labels.HANDOFF: "suggest",
            labels.HANDOFF_AT: "400000",
            "omnigent.last_context_tokens": "90000",
        }
    )
    world = _World(monkeypatch, [row])
    assert (await world.sweeper.sweep())["cleared"] == [OLD]
    assert labels.label(row.labels, labels.HANDOFF) is None
    # Past the threshold again, it is one suggestion again.
    row.labels["omnigent.last_context_tokens"] = "310000"
    assert (await world.sweeper.sweep())["suggested"] == [OLD]


async def test_the_sweep_repairs_a_lead_whose_children_still_point_at_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = _row(
        "done", {labels.HANDOFF_TO: NEW, labels.STATUS: "reference", labels.HANDOFF_DONE: "1"}
    )
    half = _lead({labels.HANDOFF_TO: NEW, labels.STATUS: "reference"})
    child = _row(CHILD, {labels.ORCH: OLD})
    world = _World(monkeypatch, [done, half, child])
    result = await world.sweeper.sweep()
    assert result["repaired"] == [OLD]
    assert world.repaired == [(OLD, NEW)]
    assert world.store.writes == []


def test_needs_repair_is_the_done_marker_alone() -> None:
    committed = _lead({labels.HANDOFF_TO: NEW, labels.STATUS: "reference"})
    assert lead_handoff.needs_repair(committed, [])
    done = _lead({labels.HANDOFF_TO: NEW, labels.STATUS: "reference", labels.HANDOFF_DONE: "1"})
    # Done stays done, even when the owner later reuses the old chat or a child points at it again.
    assert not lead_handoff.needs_repair(done, [done, _row(CHILD, {labels.ORCH: OLD})])
    assert not lead_handoff.needs_repair(_lead(), [_lead()])


# ── finishing a handoff ─────────────────────────────────────────────────────


class _Http:
    """A scheduled-tasks API with *tasks*; PATCHes are recorded."""

    def __init__(self, tasks: list[dict[str, Any]], patch_status: int = 200) -> None:
        self.tasks = tasks
        self.patched: list[tuple[str, dict[str, Any]]] = []
        self.patch_status = patch_status

    async def get(self, path: str) -> httpx.Response:
        assert path == "/v1/scheduled-tasks"
        return httpx.Response(200, json={"scheduled_tasks": self.tasks})

    async def patch(self, path: str, json: dict[str, Any]) -> httpx.Response:
        self.patched.append((path.rsplit("/", 1)[1], json))
        return httpx.Response(self.patch_status, json={})


def _checkin(note: str | None = "watch PR 412") -> dict[str, Any]:
    return {
        "id": "task_checkin",
        "name": schedules.CHECKIN_NAME,
        "prompt": schedules.checkin_prompt(OLD, note),
    }


async def test_finish_repoints_children_checkin_and_tasks_then_sets_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _row(CHILD, {labels.ORCH: OLD})
    other_child = _row("other", {labels.ORCH: "someone-else"})
    lead = _lead()
    store = _Store([lead, child, other_child])
    monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda s: store.rows)
    mine = {
        "id": "task_mine",
        "name": "Nightly",
        "prompt": f"Post to session_id {OLD} at night, then session {OLD}.",
    }
    unrelated = {"id": "task_x", "name": "Other", "prompt": "No ids here."}
    http = _Http([_checkin(), mine, unrelated])
    result = await lead_handoff.finish(store, http, OLD, NEW)  # type: ignore[arg-type]
    assert result.children == [CHILD] and result.status_set and not result.problems
    assert result.done and lead.labels[labels.HANDOFF_DONE] == "1"
    assert child.labels[labels.ORCH] == NEW
    assert other_child.labels[labels.ORCH] == "someone-else"
    assert lead.labels[labels.STATUS] == labels.REFERENCE
    assert result.checkin == "rewritten" and result.tasks == ["Nightly"]
    patched = dict(http.patched)
    assert patched["task_checkin"] == {"prompt": schedules.checkin_prompt(NEW, "watch PR 412")}
    assert patched["task_mine"] == {
        "prompt": f"Post to session_id {NEW} at night, then session {NEW}."
    }
    assert "task_x" not in patched


async def test_finish_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    child = _row(CHILD, {labels.ORCH: OLD})
    store = _Store([_lead(), child])
    monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda s: store.rows)
    http = _Http([_checkin(None)])
    await lead_handoff.finish(store, http, OLD, NEW)  # type: ignore[arg-type]
    # The check-in on the server now names the new lead, so a second run rewrites nothing.
    http.tasks[0]["prompt"] = schedules.checkin_prompt(NEW, None)
    http.patched.clear()
    again = await lead_handoff.finish(store, http, OLD, NEW)  # type: ignore[arg-type]
    assert again.children == [] and http.patched == [] and again.checkin == "none"


async def test_a_hand_edited_checkin_prompt_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store([_lead()])
    monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda s: store.rows)
    edited = _checkin() | {"prompt": _checkin()["prompt"] + " Also water the plants."}
    http = _Http([edited])
    result = await lead_handoff.finish(store, http, OLD, NEW)  # type: ignore[arg-type]
    assert http.patched == []
    assert result.checkin == "not rewritten"
    assert any("schedule_checkin" in problem for problem in result.problems)
    assert result.status_set, "the other steps still ran"


def test_the_checkin_is_rebuilt_not_string_replaced() -> None:
    prompt = schedules.checkin_prompt(OLD, "Focus on PR 412 and the deploy")
    assert lead_handoff.rebuilt_checkin(prompt, OLD, NEW) == schedules.checkin_prompt(
        NEW, "Focus on PR 412 and the deploy"
    )
    assert lead_handoff.rebuilt_checkin(schedules.checkin_prompt(None, None), OLD, NEW) is None
    assert lead_handoff.rebuilt_checkin(prompt, "0" * 32, NEW) is None


async def test_a_child_that_cannot_be_written_is_reported_and_the_rest_continue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = _row(CHILD, {labels.ORCH: OLD})
    lead = _lead()
    store = _Store([lead, child])
    store.fail_for = {CHILD}
    monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda s: store.rows)
    result = await lead_handoff.finish(store, None, OLD, NEW)
    assert result.children == [] and result.status_set
    assert any(CHILD in problem for problem in result.problems)
    # The sweep sees the child still pointing at the old lead and repairs it later.
    assert not result.done
    assert lead_handoff.needs_repair(
        _lead({labels.HANDOFF_TO: NEW, labels.STATUS: "reference"}), store.rows
    )


def test_label_age_falls_back_to_the_row_age_off_a_sql_store() -> None:
    row = _lead(updated_at=1_000)
    assert lead_handoff.label_age(object(), row, "k", now=1_060.0) == 60.0
    assert lead_handoff.label_age(object(), _lead(updated_at=None), "k", now=1.0) is None


def test_the_summary_dict_names_every_step() -> None:
    summary = lead_handoff.Finished(children=[CHILD], checkin="rewritten", status_set=True)
    assert json.loads(json.dumps(summary.as_dict())) == {
        "children_moved": [CHILD],
        "checkin": "rewritten",
        "tasks_rewritten": [],
        "old_lead_set_to_reference": True,
        "done": False,
        "tasks_naming_the_old_lead_unchanged": [],
        "problems": [],
    }


# ── review round 1 (2026-10-29) ─────────────────────────────────────────────


class _FlakyHttp(_Http):
    """A scheduled-tasks API whose PATCH fails until *fail* is cleared."""

    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        super().__init__(tasks)
        self.fail = True

    async def patch(self, path: str, json: dict[str, Any]) -> httpx.Response:
        if self.fail:
            return httpx.Response(500, json={})
        return await super().patch(path, json)


async def test_a_failed_task_patch_leaves_the_handoff_unfinished_until_a_repair_does_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lead = _lead({labels.HANDOFF_TO: NEW})
    store = _Store([lead])
    monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda s: store.rows)
    http = _FlakyHttp([_checkin()])
    first = await lead_handoff.finish(store, http, OLD, NEW)  # type: ignore[arg-type]
    assert first.status_set and not first.done and first.problems
    assert lead.labels[labels.STATUS] == labels.REFERENCE, "reference was set, done was not"
    assert lead_handoff.needs_repair(lead, store.rows), "the sweep keeps coming back"
    http.fail = False
    second = await lead_handoff.finish(store, http, OLD, NEW)  # type: ignore[arg-type]
    assert second.done and not second.problems and second.checkin == "rewritten"
    assert lead.labels[labels.HANDOFF_DONE] == "1"
    assert not lead_handoff.needs_repair(lead, store.rows)


async def test_a_missing_owner_client_is_a_problem_not_a_done_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda s: [])
    store = _Store([_lead({labels.HANDOFF_TO: NEW})])
    result = await lead_handoff.finish(store, None, OLD, NEW)
    assert not result.done and result.problems


async def _get(store: _Store, session_id: str) -> Any:
    return store.get_conversation(session_id)


@pytest.mark.parametrize(
    ("target", "same_owner", "expected"),
    [
        (_row(NEW), True, "ok"),
        (_row(NEW, archived=True), True, "gone"),
        (None, True, "gone"),
        (_row(NEW), False, "gone"),
        (_row(NEW), None, "unknown"),  # an owner lookup that failed
    ],
)
async def test_the_target_must_exist_be_live_and_have_the_same_owner(
    target: Any, same_owner: bool | None, expected: str
) -> None:
    store = _Store([target] if target is not None else [])

    async def owner(_session_id: str) -> bool | None:
        return same_owner

    assert await lead_handoff.target_ok(lambda i: _get(store, i), owner, NEW) == expected


async def test_a_lookup_that_raises_is_unknown_not_gone() -> None:
    async def boom(_session_id: str) -> Any:
        raise RuntimeError("database is locked")

    async def owner(_session_id: str) -> bool:
        return True

    assert await lead_handoff.target_ok(boom, owner, NEW) == "unknown"

    async def get(_session_id: str) -> Any:
        return _row(NEW)

    async def owner_boom(_session_id: str) -> bool:
        raise RuntimeError("no owner grant read")

    assert await lead_handoff.target_ok(get, owner_boom, NEW) == "unknown"


def test_a_permanent_problem_warns_once_per_lead_then_logs_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lead_handoff._warned.clear()
    with caplog.at_level(logging.DEBUG, logger="ae_omni_policies.lead_handoff"):
        for _ in range(3):
            lead_handoff.warn_once("undo", OLD, "undo of %s left %s", OLD, ["x"])
        lead_handoff.warn_once("undo", "other", "undo of %s left %s", "other", ["x"])
    levels = [record.levelno for record in caplog.records]
    assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG, logging.WARNING]


async def test_clear_commit_puts_the_family_back_and_then_takes_the_marks_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ae_omni_policies import schedules

    lead = _lead(
        {
            labels.HANDOFF_TO: NEW,
            labels.HANDOFF_BRIEF: "posted",
            labels.HANDOFF_DONE: "1",
            labels.HANDOFF_PREV_STATUS: "blocked",
            labels.STATUS: "reference",
        }
    )
    moved = _row(CHILD, {labels.ORCH: NEW})
    other = _row("other", {labels.ORCH: "someone-else"})
    store = _Store([lead, moved, other, _row(NEW, archived=True)])
    monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda s: store.rows)
    checkin = {
        "id": "t1",
        "name": schedules.CHECKIN_NAME,
        "prompt": schedules.checkin_prompt(NEW, "watch PR 9"),
    }
    mine = {"id": "t2", "name": "Nightly", "prompt": f"Post to session_id {NEW}, see {NEW}."}
    http = _Http([checkin, mine])
    result = await lead_handoff.clear_commit(store, http, OLD, NEW)  # type: ignore[arg-type]
    assert result.done and not result.problems
    assert moved.labels[labels.ORCH] == OLD, "the child is back on the old lead"
    assert other.labels[labels.ORCH] == "someone-else"
    assert dict(http.patched) == {
        "t1": {"prompt": schedules.checkin_prompt(OLD, "watch PR 9")},
        "t2": {"prompt": f"Post to session_id {OLD}, see {NEW}."},
    }
    assert lead.labels[labels.STATUS] == "blocked", "the status it had before the handoff"
    for key in (labels.HANDOFF_TO, labels.HANDOFF_BRIEF, labels.HANDOFF_DONE):
        assert labels.label(lead.labels, key) is None
    # The sweep sees an ordinary working lead again.
    assert lead_handoff.is_lead(lead) and not lead_handoff.needs_repair(lead, store.rows)


async def test_clear_commit_defaults_to_working_and_keeps_the_marks_on_a_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lead = _lead({labels.HANDOFF_TO: NEW, labels.STATUS: "reference"})
    store = _Store([lead, _row(CHILD, {labels.ORCH: NEW})])
    monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda s: store.rows)
    broken = _FlakyHttp([{"id": "t1", "name": "N", "prompt": f"session_id {NEW}"}])
    first = await lead_handoff.clear_commit(store, broken, OLD, NEW)  # type: ignore[arg-type]
    assert first.problems and not first.done
    assert lead_handoff.handoff_target(lead) == NEW, "marks stay so the undo is repeated"
    assert lead.labels[labels.STATUS] == "working"
    broken.fail = False
    second = await lead_handoff.clear_commit(store, broken, OLD, NEW)  # type: ignore[arg-type]
    assert second.done and lead_handoff.handoff_target(lead) is None
    # Without a usable owner client the undo cannot finish either.
    stuck = _lead({labels.HANDOFF_TO: NEW})
    result = await lead_handoff.clear_commit(_Store([stuck]), None, OLD, NEW)
    assert result.problems and not result.done


async def test_a_lead_whose_lock_is_held_is_skipped_until_the_next_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lead_handoff, "LOCK_WAIT_S", 0.05)
    world = _World(monkeypatch, [_lead()])
    async with lead_handoff.lead_lock(OLD):
        assert (await world.sweeper.sweep())["suggested"] == []
    assert world.store.writes == []
    assert (await world.sweeper.sweep())["suggested"] == [OLD]


async def test_two_sweeps_at_once_write_one_label(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    world = _World(monkeypatch, [_lead()])
    first, second = await asyncio.gather(world.sweeper.sweep(), world.sweeper.sweep())
    assert len(world.store.writes) == 1
    assert len(first["suggested"]) + len(second["suggested"]) == 1


async def test_a_task_naming_the_old_lead_in_other_text_is_listed_and_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store([_lead()])
    monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda s: store.rows)
    reads = {
        "id": "t_reads",
        "name": "Reader",
        "prompt": f"Read the transcript of {OLD}, then post to session_id {OLD}.",
    }
    only_text = {"id": "t_text", "name": "Notes", "prompt": f"Summarise what {OLD} did."}
    http = _Http([reads, only_text])
    result = await lead_handoff.finish(store, http, OLD, NEW)  # type: ignore[arg-type]
    assert dict(http.patched) == {
        "t_reads": {"prompt": f"Read the transcript of {OLD}, then post to session_id {NEW}."}
    }
    assert result.unchanged_tasks == ["Reader", "Notes"]
    assert result.tasks == ["Reader"]
    assert result.as_dict()["tasks_naming_the_old_lead_unchanged"] == ["Reader", "Notes"]


def test_children_of_compares_ids_with_or_without_the_conv_prefix() -> None:
    kids = [_row("k1", {labels.ORCH: "conv_" + OLD}), _row("k2", {labels.ORCH: OLD})]
    assert [k.id for k in lead_handoff.children_of(kids, OLD)] == ["k1", "k2"]
    assert [k.id for k in lead_handoff.children_of(kids, "conv_" + OLD)] == ["k1", "k2"]


def test_merge_and_child_wake_posts_follow_the_handoff_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import omnigent.runtime as runtime

    from ae_omni_policies import merge_watch

    chain = {"a": "b", "b": "c", "c": "a"}  # a loop: stops, never spins
    rows = {key: _row(key, {labels.HANDOFF_TO: value}) for key, value in chain.items()} | {
        "z": _row("z")
    }
    store = SimpleNamespace(get_conversation=rows.get)
    monkeypatch.setattr(runtime, "get_conversation_store", lambda: store)
    assert merge_watch._handoff_target("z") == "z"
    assert merge_watch._handoff_target("a") == "c"
    long = {f"s{i}": _row(f"s{i}", {labels.HANDOFF_TO: f"s{i + 1}"}) for i in range(20)}
    store.get_conversation = long.get  # type: ignore[assignment]
    assert merge_watch._handoff_target("s0") == "s5"


class _Identity:
    """An owner lookup that can fail (None) for chosen sessions; the server can always act."""

    def __init__(self, owners: dict[str, str | None]) -> None:
        self.owners = owners

    def owner(self, session_id: str) -> str | None:
        return self.owners.get(session_id)

    async def headers(self, session_id: str) -> dict[str, str] | None:
        return {}


def _repair_for(store: _Store, identity: _Identity, http: _Http) -> Any:
    import contextlib

    @contextlib.asynccontextmanager
    async def client_for(_app: Any, _headers: dict[str, str]) -> Any:
        yield http

    return lead_handoff.make_repair(store, None, identity, client_for)


async def test_a_failed_owner_lookup_undoes_nothing_and_the_next_sweep_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lead = _lead({labels.HANDOFF_TO: NEW, labels.STATUS: "working"})
    child = _row(CHILD, {labels.ORCH: OLD})
    store = _Store([lead, child, _row(NEW)])
    monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda s: store.rows)
    http = _Http([_checkin(None)])
    # The old lead's owner is known, the new lead's lookup fails (None): not a verdict.
    identity = _Identity({OLD: "owner", NEW: None})
    repair = _repair_for(store, identity, http)
    assert await repair(OLD, NEW) is False
    assert lead_handoff.handoff_target(lead) == NEW, "the commit is kept"
    assert child.labels[labels.ORCH] == OLD and http.patched == []
    assert lead.labels[labels.STATUS] == "working", "the status is not touched either"
    # The lookup works again: the same repair finishes the handoff.
    identity.owners[NEW] = "owner"
    assert await repair(OLD, NEW) is True
    assert child.labels[labels.ORCH] == NEW and lead.labels[labels.HANDOFF_DONE] == "1"
    assert lead.labels[labels.STATUS] == labels.REFERENCE


async def test_a_different_confirmed_owner_still_undoes(monkeypatch: pytest.MonkeyPatch) -> None:
    lead = _lead({labels.HANDOFF_TO: NEW, labels.STATUS: "reference"})
    child = _row(CHILD, {labels.ORCH: NEW})
    store = _Store([lead, child, _row(NEW)])
    monkeypatch.setattr(lead_handoff.sessions, "list_top_level", lambda s: store.rows)
    repair = _repair_for(store, _Identity({OLD: "owner", NEW: "someone-else"}), _Http([]))
    assert await repair(OLD, NEW) is False
    assert lead_handoff.handoff_target(lead) is None and child.labels[labels.ORCH] == OLD
