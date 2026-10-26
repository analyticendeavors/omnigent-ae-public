"""The preset run log (2026-10-24): when a line is written and what it holds."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ae_omni_policies import labels, model_log, preset_runs, role_prefs
from ae_omni_policies.preset_runs import StateFile, Sweeper

NOW = 100_000.0


@dataclass
class Conv:
    id: str
    labels: dict[str, str] = field(default_factory=dict)
    parent_conversation_id: str | None = None
    kind: str = "default"
    archived: bool = False
    title: str = "a chat"
    created_at: int = int(NOW) - 3000
    updated_at: int = int(NOW) - 60
    live_status: str | None = "idle"
    agent_id: str | None = None
    model_override: str | None = None
    reported_model: str | None = None
    reasoning_effort: str | None = None
    session_usage: dict[str, Any] = field(default_factory=dict)


def status(value: str) -> dict[str, str]:
    return {labels.STATUS: value}


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.log = tmp_path / "runs.jsonl"
        monkeypatch.setenv(preset_runs.FILE_ENV, str(self.log))
        monkeypatch.setenv(model_log.FILE_ENV, str(tmp_path / "model.jsonl"))
        self.convs: dict[str, Conv] = {}
        self.calls: dict[str, list[SimpleNamespace]] = {}
        self.now = NOW
        self.on = True
        self.empty: set[str] = set()
        self.state = StateFile(tmp_path / "state.json")
        self.sweeper = Sweeper(
            store=self,
            state=self.state,
            list_rows=self._rows,
            owner=lambda _id: "reid",
            insights_on=lambda _user: self.on,
            resolve=lambda _conv: ("balanced", "abc1234567"),
            revision=lambda _user: 7,
            agent_name=lambda conv: {
                "codex": "codex-native-ui",
                "reviewer": preset_runs.REVIEWER_AGENT,
            }.get(conv.agent_id or ""),
            clock=lambda: self.now,
        )

    def add(self, conv: Conv) -> Conv:
        self.convs[conv.id] = conv
        return conv

    def _rows(self) -> list[Conv]:
        return [
            c for c in self.convs.values() if not c.archived and c.parent_conversation_id is None
        ]

    def lines(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text().splitlines()]

    # store interface
    def get_conversation(self, session_id: str) -> Conv | None:
        return self.convs.get(session_id)

    def list_conversations(self, **kw: Any) -> Any:
        parent = kw.get("parent_conversation_id")
        data = [c for c in self.convs.values() if c.parent_conversation_id == parent]
        return SimpleNamespace(data=data, has_more=False, last_id=None)

    def list_items(self, conversation_id: str, **kw: Any) -> Any:
        # A chat with no recorded calls still has one message unless it is listed in `empty`.
        default = [] if conversation_id in self.empty else [SimpleNamespace(data=None)]
        return SimpleNamespace(
            data=self.calls.get(conversation_id, default), has_more=False, last_id=None
        )

    def sweep(self) -> list[str]:
        return self.sweeper.sweep_sync()


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def test_native_role_ports_the_web_parse() -> None:
    assert preset_runs.native_role("Builder A: fix the login test") == "builder"
    assert preset_runs.native_role("Role: Reviewer.") == "reviewer"
    assert preset_runs.native_role("role: helper") == "helper"
    assert preset_runs.native_role("Builder: x") == "builder"
    for text in ("Fix the builder test", "Builder ABC: x", "Researcher: x", "", None):
        assert preset_runs.native_role(text) == "unknown"


def test_the_writer_stamps_rotates_and_can_be_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "log.jsonl"
    monkeypatch.setenv(preset_runs.FILE_ENV, str(path))
    assert preset_runs.append({"session": "a"}, now=5)
    assert json.loads(path.read_text()) == {"session": "a", "at": 5}
    path.write_text("x" * (preset_runs.MAX_BYTES + 1))
    assert preset_runs.append({"session": "b"}, now=6)
    assert path.with_name("log.jsonl.1").exists()
    assert json.loads(path.read_text())["session"] == "b"
    monkeypatch.setenv(preset_runs.FILE_ENV, "")
    assert preset_runs.append({"session": "c"}) is False


def test_a_lead_in_review_writes_one_line_once(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.WORKING)))
    assert rig.sweep() == []
    rig.convs["lead"].labels = status(labels.REVIEW)
    assert rig.sweep() == ["lead"]
    assert rig.sweep() == []
    [line] = rig.lines()
    assert line["session"] == "lead" and line["outcome"] == "review"
    assert (line["user"], line["preset"], line["fingerprint"], line["revision"]) == (
        "reid",
        "balanced",
        "abc1234567",
        7,
    )
    assert line["wall_s"] == 2940 and line["active_s"] is None and line["files_touched"] is None
    assert line["at"] == int(NOW)


def test_a_second_end_appends_a_new_line(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.REVIEW)))
    rig.sweep()
    rig.convs["lead"].labels = status(labels.WORKING)
    rig.sweep()
    rig.convs["lead"].labels = status(labels.REFERENCE)
    rig.convs["lead"].updated_at += 30  # a real second end has a later update time
    rig.sweep()
    assert [x["outcome"] for x in rig.lines()] == ["review", "reference"]
    assert len({x["run_key"] for x in rig.lines()}) == 2


def test_a_lead_archived_while_working_writes_archived(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.WORKING)))
    rig.sweep()
    rig.convs["lead"].archived = True
    assert rig.sweep() == ["lead"]
    assert rig.lines()[0]["outcome"] == "archived"
    assert rig.sweep() == []


def test_a_written_lead_archived_later_writes_nothing_more(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.REFERENCE)))
    rig.sweep()
    rig.convs["lead"].archived = True
    rig.sweep()
    assert len(rig.lines()) == 1
    with rig.state.locked() as data:
        assert data["ended"] == {}


def test_old_history_is_not_backfilled(rig: Rig) -> None:
    rig.add(Conv("old", status(labels.REFERENCE), updated_at=int(NOW) - 3 * 86400))
    assert rig.sweep() == [] and rig.lines() == []
    rig.convs["old"].labels = status(labels.WORKING)
    rig.sweep()
    rig.convs["old"].labels = status(labels.REVIEW)
    assert rig.sweep() == ["old"]


def test_insights_off_writes_nothing(rig: Rig) -> None:
    rig.on = False
    rig.add(Conv("lead", status(labels.REVIEW)))
    assert rig.sweep() == [] and rig.lines() == []
    rig.on = True
    rig.convs["lead"].labels = status(labels.WORKING)
    rig.sweep()
    rig.convs["lead"].labels = status(labels.REVIEW)
    assert rig.sweep() == ["lead"]


def test_a_family_child_is_not_a_lead(rig: Rig) -> None:
    rig.add(Conv("kid", status(labels.REVIEW) | {labels.ORCH: "lead"}))
    assert rig.sweep() == [] and rig.lines() == []


def test_agents_cost_and_review_are_collected(rig: Rig) -> None:
    usage = {"by_model": {"opus": {"total_cost_usd": 2.0, "input_tokens": 10}}}
    rig.add(Conv("lead", status(labels.WORKING), session_usage=usage, model_override="opus"))
    rig.add(
        Conv(
            "rev",
            status(labels.REVIEW) | {labels.ORCH: "lead", labels.ROLE: "reviewer"},
            agent_id="codex",
            reported_model="gpt",
            session_usage={
                "total_cost_usd": 0.5,
                "by_model": {"gpt": {"total_cost_usd": 0.5}, "opus": {"total_cost_usd": 1.0}},
            },
        )
    )
    rig.add(
        Conv(
            "sub",
            parent_conversation_id="lead",
            kind="sub_agent",
            labels={
                "omnigent.claude_native.description": "Builder A: fix it",
                "omnigent.claude_native.tool_use_id": "tu1",
            },
        )
    )
    rig.add(Conv("sub2", parent_conversation_id="lead", kind="sub_agent", labels={}))
    rig.calls["lead"] = [
        SimpleNamespace(
            data=SimpleNamespace(
                call_id="tu1", name="Agent", arguments=json.dumps({"model": "sonnet"})
            )
        )
    ]
    rig.sweep()
    model_log.append({"session": "lead", "from": {}, "to": {}, "kind": "raise"}, now=NOW - 10)
    model_log.append({"session": "other", "from": {}, "to": {}}, now=NOW - 10)
    from ae_omni_policies.preset_runs import record_review

    record_review(rig.state, "lead", 2, 5, 4)
    record_review(rig.state, "lead", 1, 1, 1)
    rig.convs["lead"].labels = status(labels.REVIEW)
    rig.sweep()
    [line] = rig.lines()
    agents = {a["session"]: a for a in line["agents"]}
    assert agents["rev"]["kind"] == "codex" and agents["rev"]["role"] == "reviewer"
    assert agents["rev"]["cost_usd"] == 0.5 and agents["rev"]["model"] == "gpt"
    assert agents["sub"] == {
        "session": "sub",
        "kind": "subagent",
        "role": "builder",
        "model": "sonnet",
        "effort": None,
        "seconds": 2940,
        "cost_usd": None,
        "hung": False,
    }
    assert agents["sub2"]["role"] == "unknown" and agents["sub2"]["model"] is None
    assert line["cost"]["by_model"]["opus"]["usd"] == 3.0
    assert line["cost"]["total_usd"] == 3.5
    assert line["review"] == {
        "reported": True,
        "reviewers_run": 3,
        "findings_returned": 6,
        "findings_fixed": 5,
        "reviewer_sessions": 1,
    }
    assert [c["session"] for c in line["model_changes"]] == ["lead"]
    assert line["lead"]["model"] == "opus"


def test_a_running_child_idle_for_fifteen_minutes_is_marked_hung_and_it_sticks(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.WORKING)))
    rig.add(
        Conv(
            "kid",
            status(labels.WORKING) | {labels.ORCH: "lead"},
            live_status="running",
            updated_at=int(NOW) - 899,
        )
    )
    rig.sweep()
    with rig.state.locked() as data:
        assert data["hung"] == {}
    rig.now = NOW + 2
    rig.sweep()
    rig.convs["kid"].live_status = "idle"
    rig.convs["lead"].labels = status(labels.REVIEW)
    rig.sweep()
    [line] = rig.lines()
    assert [a["hung"] for a in line["agents"]] == [True]
    with rig.state.locked() as data:
        assert data["hung"] == {}


def test_a_report_for_a_lead_survives_until_its_line(rig: Rig) -> None:
    preset_runs.record_review(rig.state, "lead", 1, 2, 1)
    rig.add(Conv("lead", status(labels.REVIEW)))
    rig.sweep()
    assert rig.lines()[0]["review"]["findings_returned"] == 2
    with rig.state.locked() as data:
        assert data["reports"] == {}


def test_a_line_without_a_report_says_so(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.REVIEW)))
    rig.sweep()
    review = rig.lines()[0]["review"]
    assert review["reported"] is False and review["reviewers_run"] is None


def test_the_default_resolver_names_custom_and_fingerprints_the_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preset = {"id": "balanced", "roles": {"a": 1}, "rules": {"b": 2}}
    seen: dict[str, Any] = {}

    def resolved(conv: Any, store: Any) -> dict[str, Any]:
        return seen["value"]

    import omnigent.inner.ae_roles as ae_roles

    monkeypatch.setattr(ae_roles, "resolve_roles", resolved)
    conv = Conv("x", {labels.PRESET: "fast"})
    seen["value"] = {"preset": preset, "custom": False}
    pid, finger = preset_runs.resolve_preset(None, conv)
    assert pid == "balanced" and finger == preset_runs.fingerprint({"a": 1}, {"b": 2})
    seen["value"] = {"preset": preset, "custom": True}
    assert preset_runs.resolve_preset(None, conv)[0] == "custom"
    seen["value"] = {"lead_picks": True}
    assert preset_runs.resolve_preset(None, conv) == ("fast", None)


def test_insights_enabled_reads_the_users_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(role_prefs.STATE_FILE_ENV, raising=False)
    monkeypatch.setattr(role_prefs, "DATA_DIR", Path("/nonexistent-ae-data"))
    role_prefs.reset_store()
    try:
        assert preset_runs.insights_enabled("reid") is False
        current = role_prefs.store().get("reid").model_dump()
        body = role_prefs.RolesPut.model_validate(
            {**current, "insights": {"enabled": True, "weeks": 4, "scope": "all"}}
        )
        role_prefs.store().put("reid", body)
        assert preset_runs.insights_enabled("reid") is True
    finally:
        role_prefs.reset_store()


# -- review round 1 (2026-10-24)


def test_a_failed_append_leaves_the_lead_for_a_retry(rig: Rig) -> None:
    results = iter([False, True])
    real = preset_runs.append
    rig.sweeper._write = lambda line, now: next(results) and real(line, now=now)  # type: ignore[assignment]
    rig.add(Conv("lead", status(labels.REVIEW)))
    assert rig.sweep() == [] and rig.lines() == []
    with rig.state.locked() as data:
        assert "lead" not in data["ended"]
    assert rig.sweep() == ["lead"]
    assert len(rig.lines()) == 1


def test_an_exception_in_one_lead_does_not_duplicate_or_stop_the_others(rig: Rig) -> None:
    rig.add(Conv("bad", status(labels.REVIEW)))
    rig.add(Conv("good", status(labels.REVIEW)))
    real = rig.sweeper.build_line

    def build(lead: Any, *args: Any, **kw: Any) -> Any:
        if lead.id == "bad":
            raise RuntimeError("boom")
        return real(lead, *args, **kw)

    rig.sweeper.build_line = build  # type: ignore[method-assign]
    assert rig.sweep() == ["good"]
    assert rig.sweep() == []
    assert [x["session"] for x in rig.lines()] == ["good"]
    rig.sweeper.build_line = real  # type: ignore[method-assign]
    assert rig.sweep() == ["bad"]
    assert sorted(x["session"] for x in rig.lines()) == ["bad", "good"]


def test_a_gone_lead_that_raises_does_not_abort_the_block(rig: Rig) -> None:
    rig.add(Conv("a", status(labels.WORKING)))
    rig.add(Conv("b", status(labels.WORKING)))
    rig.sweep()
    rig.convs["a"].archived = True
    rig.convs["b"].archived = True
    real = rig.get_conversation

    def get(sid: str) -> Any:
        if sid == "a":
            raise RuntimeError("x")
        return real(sid)

    rig.get_conversation = get  # type: ignore[method-assign]
    assert rig.sweep() == ["b"]


def test_a_failed_state_write_does_not_repeat_lines(tmp_path: Path, rig: Rig) -> None:
    (tmp_path / "blocked").write_text("a file, so the state directory cannot be made")
    rig.state.path = tmp_path / "blocked" / "state.json"
    rig.add(Conv("lead", status(labels.REVIEW)))
    assert rig.sweep() == ["lead"]
    assert rig.sweep() == []
    assert len(rig.lines()) == 1


def test_a_lost_state_file_does_not_duplicate_the_line(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.REVIEW)))
    rig.sweep()
    assert rig.state.path is not None
    rig.state.path.unlink()
    rig.state._memory = {}
    assert rig.sweep() == []
    assert len(rig.lines()) == 1
    with rig.state.locked() as data:
        assert "lead" in data["ended"]


def test_a_lead_first_seen_long_ended_is_marked_without_a_line(rig: Rig) -> None:
    rig.add(Conv("old", status(labels.REVIEW), updated_at=int(NOW) - 600))
    assert rig.sweep() == [] and rig.lines() == []
    with rig.state.locked() as data:
        assert "old" in data["ended"]
    rig.add(Conv("new", status(labels.REVIEW), updated_at=int(NOW) - 300))
    assert rig.sweep() == ["new"]


def test_a_late_report_amends_the_line_and_an_old_one_is_dropped(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.REVIEW)))
    rig.sweep()
    out = preset_runs.record_review(rig.state, "lead", 2, 3, 1, now=NOW + 600)
    assert out["status"] == "amended" and out["totals"]["findings_returned"] == 3
    first, second = rig.lines()
    assert first["run_key"] == second["run_key"] and (first["rev"], second["rev"]) == (0, 1)
    assert first["review"]["reported"] is False and second["review"]["reviewers_run"] == 2
    assert rig.sweep() == [] and len(rig.lines()) == 2
    late = preset_runs.record_review(rig.state, "lead", 1, 1, 1, now=NOW + 4000)
    assert late["status"] == "dropped" and len(rig.lines()) == 2


def test_a_report_with_insights_off_writes_and_keeps_nothing(rig: Rig) -> None:
    out = preset_runs.record_review(rig.state, "lead", 1, 1, 1, insights_on=False)
    assert out["status"] == "off"
    with rig.state.locked() as data:
        assert data["reports"] == {}
    assert rig.lines() == []


def test_a_grandchild_is_in_the_agents_list(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.WORKING)))
    rig.add(Conv("rev", status(labels.WORKING) | {labels.ORCH: "lead", labels.ROLE: "reviewer"}))
    rig.add(Conv("fix", status(labels.WORKING) | {labels.ORCH: "rev", labels.ROLE: "builder"}))
    rig.sweep()
    rig.convs["lead"].labels = status(labels.REVIEW)
    rig.sweep()
    [line] = rig.lines()
    assert sorted(a["session"] for a in line["agents"]) == ["fix", "rev"]


def test_the_preset_at_run_start_wins_over_a_later_edit(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.WORKING)))
    rig.sweep()
    rig.sweeper._resolve = lambda _c: ("edited", "ffffffffff")  # type: ignore[method-assign]
    rig.sweeper._revision = lambda _u: 99  # type: ignore[method-assign]
    rig.convs["lead"].labels = status(labels.REVIEW)
    rig.sweep()
    [line] = rig.lines()
    assert (line["preset"], line["fingerprint"], line["revision"]) == ("balanced", "abc1234567", 7)


def test_an_empty_chat_writes_nothing_but_is_marked(rig: Rig) -> None:
    rig.empty.add("blank")
    rig.add(Conv("blank", status(labels.WORKING)))
    rig.sweep()
    rig.convs["blank"].archived = True
    assert rig.sweep() == [] and rig.lines() == []
    with rig.state.locked() as data:
        assert "blank" not in data["live"]


def test_a_preset_reviewer_run_writes_no_line_but_is_marked(rig: Rig) -> None:
    rig.add(Conv("rev", status(labels.WORKING), agent_id="reviewer"))
    rig.add(Conv("work", status(labels.WORKING)))
    rig.sweep()
    rig.convs["rev"].labels = status(labels.REFERENCE)
    rig.convs["work"].labels = status(labels.REVIEW)
    assert rig.sweep() == ["work"]
    assert [line["session"] for line in rig.lines()] == ["work"]
    with rig.state.locked() as data:
        assert "rev" in data["ended"] and "rev" not in data["live"]
    assert rig.sweep() == []


def test_insights_off_owners_cost_no_store_reads(rig: Rig) -> None:
    rig.on = False
    rig.add(Conv("lead", status(labels.WORKING)))
    rig.sweep()
    with rig.state.locked() as data:
        assert data["live"] == {}


def test_a_first_sight_old_end_drops_a_stale_report(rig: Rig) -> None:
    preset_runs.record_review(rig.state, "old", 1, 1, 1)
    rig.add(Conv("old", status(labels.REVIEW), updated_at=int(NOW) - 600))
    rig.sweep()
    with rig.state.locked() as data:
        assert "old" in data["ended"] and data["reports"] == {}


def test_an_empty_run_drops_a_stale_report(rig: Rig) -> None:
    rig.empty.add("lead")
    preset_runs.record_review(rig.state, "lead", 1, 1, 1)
    rig.add(Conv("lead", status(labels.REVIEW)))
    assert rig.sweep() == [] and rig.lines() == []
    with rig.state.locked() as data:
        assert "lead" in data["ended"] and data["reports"] == {}


def test_a_report_after_the_lead_moved_on_waits_for_the_new_run(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.REVIEW)))
    rig.sweep()
    moved = ("working", NOW + 10)
    out = preset_runs.record_review(rig.state, "lead", 1, 2, 1, now=NOW + 20, lead_now=moved)
    assert out["status"] == "stored" and len(rig.lines()) == 1
    later = (labels.REVIEW, NOW + 100)  # a new end, not the written one
    again = preset_runs.record_review(rig.state, "lead", 1, 1, 1, now=NOW + 30, lead_now=later)
    assert again["status"] == "stored" and again["totals"]["calls"] == 2
    same = preset_runs.record_review(rig.state, "other", 1, 1, 1, lead_now=(labels.REVIEW, 1.0))
    assert same["status"] == "stored"


def test_a_repeated_call_id_is_counted_once_before_and_after_the_line(rig: Rig) -> None:
    # Issue 328: a lead called report_review twice for one round and its run read 14
    # reviewers instead of 9.
    rig.add(Conv("lead", status("working")))
    rig.sweep()
    record = preset_runs.record_review
    assert record(rig.state, "lead", 4, 24, 22, call_id="first")["status"] == "stored"
    assert record(rig.state, "lead", 5, 38, 29, call_id="final")["status"] == "stored"
    out = record(rig.state, "lead", 5, 38, 29, call_id="final")
    assert out["status"] == "repeat" and out["totals"]["reviewers_run"] == 9
    assert out["totals"]["calls"] == 2
    assert record(rig.state, "lead", 0, 1, 1)["totals"]["calls"] == 3  # no id: still adds
    rig.convs["lead"].labels = status(labels.REVIEW)
    rig.sweep()
    [line] = rig.lines()
    assert (line["review"]["reviewers_run"], line["review"]["findings_returned"]) == (9, 63)
    late = record(rig.state, "lead", 5, 38, 29, call_id="final", now=NOW + 60)
    assert late["status"] == "repeat" and late["totals"]["reviewers_run"] == 9
    fix = record(rig.state, "lead", 1, 2, 2, call_id="fix check", now=NOW + 60)
    assert fix["status"] == "amended" and fix["totals"]["reviewers_run"] == 10
    again = record(rig.state, "lead", 1, 2, 2, call_id="fix check", now=NOW + 70)
    assert again["status"] == "repeat" and again["totals"]["reviewers_run"] == 10
    assert len(rig.lines()) == 2


def test_round_names_are_trimmed_and_start_over_with_each_run(rig: Rig) -> None:
    rig.add(Conv("lead", status("working")))
    rig.sweep()
    record = preset_runs.record_review
    assert record(rig.state, "lead", 2, 3, 1, call_id="final")["status"] == "stored"
    assert record(rig.state, "lead", 2, 3, 1, call_id=" final ")["status"] == "repeat"
    assert record(rig.state, "lead", 1, 1, 1, call_id="  ")["totals"]["calls"] == 2  # no id
    rig.convs["lead"].labels = status(labels.REVIEW)
    rig.sweep()
    rig.convs["lead"].labels = status("working")  # a new run begins
    rig.sweep()
    with rig.state.locked() as data:
        assert "lead" not in data["ended"] and "lead" not in data["reports"]
    out = record(rig.state, "lead", 2, 3, 1, call_id="final")
    assert out["status"] == "stored" and out["totals"]["calls"] == 1


def test_a_run_remembers_its_last_hundred_round_names(rig: Rig) -> None:
    record = preset_runs.record_review
    for n in range(preset_runs.MAX_CALL_IDS):
        record(rig.state, "lead", 1, 1, 1, call_id=f"round {n}")
    assert record(rig.state, "lead", 1, 1, 1, call_id="round 0")["status"] == "repeat"
    record(rig.state, "lead", 1, 1, 1, call_id="one more")
    assert record(rig.state, "lead", 1, 1, 1, call_id="round 0")["status"] == "stored"


def test_a_report_with_the_lead_still_at_its_end_amends(rig: Rig) -> None:
    rig.add(Conv("lead", status(labels.REVIEW)))
    rig.sweep()
    at_end = (labels.REVIEW, float(int(NOW) - 60 + 3))
    out = preset_runs.record_review(rig.state, "lead", 1, 1, 1, now=NOW + 60, lead_now=at_end)
    assert out["status"] == "amended"


def test_the_store_is_read_outside_the_state_lock(rig: Rig) -> None:
    held: list[bool] = []
    real = rig.list_conversations

    def watched(**kw: Any) -> Any:
        held.append(rig.state._thread._is_owned())  # type: ignore[attr-defined]
        return real(**kw)

    rig.list_conversations = watched  # type: ignore[method-assign]
    rig.add(Conv("lead", status(labels.REVIEW)))
    rig.sweep()
    rig.add(Conv("busy", status("working")))
    rig.sweep()
    assert held and not any(held)


def test_a_concurrent_write_of_the_same_end_does_not_double_the_line(rig: Rig) -> None:
    lead = rig.add(Conv("lead", status(labels.REVIEW)))
    real = rig.sweeper.build_line

    def build(*a: Any, **kw: Any) -> Any:
        line = real(*a, **kw)
        with rig.state.locked() as data:  # another caller marks the end mid-read
            data["ended"]["lead"] = rig.sweeper._ended_entry("lead", labels.REVIEW, NOW, NOW)
        return line

    rig.sweeper.build_line = build  # type: ignore[method-assign]
    assert rig.sweeper._emit(lead, labels.REVIEW, {}, NOW) == preset_runs.SKIPPED
    assert rig.lines() == []


def test_the_family_cap_warns_once_per_lead(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(preset_runs, "MAX_FAMILY", 1)
    preset_runs._CAP_WARNED.discard("lead")
    lead = rig.add(Conv("lead", status(labels.REVIEW)))
    for i in range(3):
        rig.add(Conv(f"kid{i}", parent_conversation_id="lead"))
    with caplog.at_level("WARNING", logger=preset_runs._logger.name):
        for _ in range(3):
            rig.sweeper._family(lead, [], {})
    assert sum("passed" in r.getMessage() for r in caplog.records) == 1


def test_the_run_key_carries_the_outcome_and_survives_a_zero_time(rig: Rig) -> None:
    rig.add(Conv("a", status(labels.REVIEW), updated_at=0))
    rig.sweep()
    with rig.state.locked() as data:
        first = data["ended"]["a"]["run_key"]
    assert first == "a:0:review"
    assert rig.sweeper._ended_entry("a", "archived", 0.0, NOW)["run_key"] == "a:0:archived"
