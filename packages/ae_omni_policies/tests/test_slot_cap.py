"""slot_cap and the sessions helper against a stub conversation store."""

from __future__ import annotations

from typing import Any

import pytest
from omnigent.entities import PagedList
from omnigent.entities.conversation import Conversation
from sqlalchemy.exc import OperationalError

from ae_omni_policies import labels, sessions
from ae_omni_policies import slot_cap as slot_cap_module
from ae_omni_policies.slot_cap import slot_cap

NOW = 1_800_000_000
OWN = "conv_own"


def _conv(
    id: str,
    title: str | None = None,
    labels_: dict[str, str] | None = None,
    *,
    created_at: int = NOW - 3_600,
    pending: int = 0,
    parent: str | None = None,
) -> Conversation:
    return Conversation(
        id=id,
        created_at=created_at,
        updated_at=created_at,
        root_conversation_id=parent or id,
        title=title,
        kind="sub_agent" if parent else "default",
        parent_conversation_id=parent,
        labels=dict(labels_ or {}),
        pending_elicitation_count=pending,
    )


def _working(id: str, title: str, started: int, **extra: str) -> Conversation:
    return _conv(id, title, {labels.STATUS: "working", labels.STARTED: str(started), **extra})


class _Store:
    """Pages like the real store: ``after`` is the id to continue past."""

    def __init__(self, rows: list[Conversation], *, fail: Exception | None = None) -> None:
        self.rows = rows
        self.own: dict[str, Conversation] = {}
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        if self.fail is not None:
            raise self.fail
        if conversation_id in self.own:
            return self.own[conversation_id]
        return next((row for row in self.rows if row.id == conversation_id), None)

    def list_conversations(self, **kwargs: Any) -> PagedList[Conversation]:
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        start = 0
        after = kwargs.get("after")
        if after is not None:
            start = next(i for i, row in enumerate(self.rows) if row.id == after) + 1
        limit = kwargs["limit"]
        page = self.rows[start : start + limit]
        return PagedList(
            data=page,
            first_id=page[0].id if page else None,
            last_id=page[-1].id if page else None,
            has_more=start + limit < len(self.rows),
        )


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    stub = _Store([])
    stub.own[OWN] = _conv(OWN, "Fresh")
    monkeypatch.setattr(sessions, "get_conversation_store", lambda: stub)
    monkeypatch.setattr(sessions, "current_session_id", lambda: OWN)
    monkeypatch.setattr(slot_cap_module, "_now", lambda: float(NOW))
    slot_cap_module.reset_pending_claims()
    yield stub
    slot_cap_module.reset_pending_claims()


def _request(text: str = "hello", own: dict[str, str] | None = None) -> dict[str, Any]:
    return {"type": "request", "data": {"user_content": text}, "context": {"labels": own or {}}}


THREE = [
    _working("c1", "Fix pricing padding", NOW - 7_800, **{labels.NEXT: "verify light mode"}),
    _working("c2", "Renewal note", NOW - 600),
    _working("c3", "CSV export header", NOW - 30),
]


def test_abstains_off_the_request_phase(store: _Store) -> None:
    policy = slot_cap()
    assert policy({"type": "tool_call", "data": {"name": "Bash"}, "context": {}}) is None
    assert store.calls == []


def test_first_prompt_claims_a_slot(store: _Store) -> None:
    store.rows = THREE[:2]
    result = slot_cap()(_request())
    assert result is not None and result["result"] == "ALLOW"
    writes = result["set_labels"]
    assert writes[labels.STATUS] == "working"
    assert writes[labels.STARTED] == str(NOW)
    assert labels.OVERFLOW not in writes
    assert store.calls[0]["kind"] == "default"
    assert store.calls[0]["include_archived"] is False
    assert store.calls[0]["order"] == "desc"


def test_warn_mode_asks_with_the_three_titles(store: _Store) -> None:
    store.rows = THREE
    result = slot_cap(mode="warn")(_request())
    assert result is not None and result["result"] == "ASK"
    reason = result["reason"]
    assert reason.startswith("Slots full: 3 of 3 taken. Run this as the 4th? ")
    assert "1. Fix pricing padding (next: verify light mode)" in reason
    assert "2. Renewal note (elapsed 10m)" in reason
    assert "3. CSV export header (elapsed under a minute)" in reason
    assert "To park one instead, the longest running: Fix pricing padding or Renewal" in reason
    assert "Accept to run it as overflow" in reason
    assert result["set_labels"][labels.OVERFLOW] == "1"
    assert result["set_labels"][labels.STATUS] == "working"


def test_block_mode_denies_and_writes_nothing(store: _Store) -> None:
    store.rows = THREE
    result = slot_cap(mode="block", lot_url="/lot")(_request())
    assert result is not None and result["result"] == "DENY"
    assert "Park one in the Parking Lot (/lot)" in result["reason"]
    assert 'resend starting with "ride unparked"' in result["reason"]
    assert "set_labels" not in result


def test_a_blocked_session_frees_its_slot(store: _Store) -> None:
    waiting = _conv("c2", "Renewal note", THREE[1].labels, pending=1)
    store.rows = [THREE[0], waiting, THREE[2]]
    result = slot_cap(mode="block")(_request())
    assert result is not None and result["result"] == "ALLOW"
    assert result["set_labels"][labels.STATUS] == "working"


def test_a_failed_session_frees_its_slot(store: _Store) -> None:
    # Seen live 2026-09-24: three of three used, one of them failed for 6h 34m.
    crashed = _working("c2", "Renewal note", NOW - 600)
    crashed.live_status = sessions.FAILED
    store.rows = [THREE[0], crashed, THREE[2]]
    result = slot_cap(mode="block")(_request())
    assert result is not None and result["result"] == "ALLOW"
    assert result["set_labels"][labels.STATUS] == "working"
    assert labels.OVERFLOW not in result["set_labels"]


@pytest.mark.parametrize("status", ["working", "reference"])
def test_working_and_reference_pass_without_a_count(store: _Store, status: str) -> None:
    store.rows = THREE
    store.own[OWN] = _conv(OWN, "Placed", {labels.STATUS: status})
    assert slot_cap(mode="block")(_request()) == {"result": "ALLOW"}
    assert store.calls == []


@pytest.mark.parametrize("status", ["blocked", "parked", "review"])
def test_other_statuses_go_through_the_count_again(store: _Store, status: str) -> None:
    store.rows = THREE
    store.own[OWN] = _conv(OWN, "Placed", {labels.STATUS: status})
    assert slot_cap(mode="block")(_request())["result"] == "DENY"
    store.rows = THREE[:1]
    result = slot_cap(mode="block")(_request())
    assert result["result"] == "ALLOW"
    assert result["set_labels"][labels.STATUS] == "working"


def test_the_row_beats_the_context_snapshot(store: _Store) -> None:
    # The engine's label snapshot can lag a write; the row is read fresh.
    store.rows = THREE
    store.own[OWN] = _conv(OWN, "Placed", {labels.STATUS: "working"})
    assert slot_cap(mode="block")(_request(own={}))["result"] == "ALLOW"
    assert store.calls == []


def test_without_an_ambient_id_the_snapshot_is_used(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "current_session_id", lambda: None)
    store.rows = THREE
    assert slot_cap(mode="block")(_request(own={labels.STATUS: "working"})) == {"result": "ALLOW"}
    assert slot_cap(mode="block")(_request())["result"] == "DENY"


def test_a_sub_agent_abstains(store: _Store) -> None:
    store.rows = THREE
    store.own[OWN] = _conv(OWN, "helper", parent="c1")
    assert slot_cap(mode="block")(_request()) is None
    assert store.calls == []


@pytest.mark.parametrize(
    "text", ["ride unparked", "  Ride Unparked, this one is quick", "ride  unparked\nsecond line"]
)
def test_ride_unparked_runs_a_fourth(store: _Store, text: str) -> None:
    store.rows = THREE
    result = slot_cap(mode="block")(_request(text))
    assert result is not None and result["result"] == "ALLOW"
    assert result["set_labels"][labels.OVERFLOW] == "1"
    assert result["set_labels"][labels.STATUS] == "working"


def test_ride_unparked_below_the_limit_is_a_plain_claim(store: _Store) -> None:
    store.rows = THREE[:1]
    result = slot_cap(mode="block")(_request("ride unparked"))
    assert result["result"] == "ALLOW"
    assert labels.OVERFLOW not in result["set_labels"]
    assert result["set_labels"][labels.STATUS] == "working"


def test_ride_unparked_must_start_the_message(store: _Store) -> None:
    store.rows = THREE
    assert slot_cap(mode="block")(_request("please ride unparked"))["result"] == "DENY"
    assert slot_cap(mode="block")(_request("ride unparkedly"))["result"] == "DENY"


def test_native_hook_bare_string_data(store: _Store) -> None:
    store.rows = THREE
    event = {"type": "request", "data": "ride unparked", "context": {"labels": {}}}
    assert slot_cap(mode="block")(event)["result"] == "ALLOW"


def test_a_store_error_fails_open(store: _Store) -> None:
    store.fail = OperationalError("select", {}, Exception("database is away"))
    assert slot_cap(mode="block")(_request()) == {"result": "ALLOW"}


def test_a_programming_error_propagates(store: _Store) -> None:
    # A renamed store method after a sync must not silently disable the cap.
    store.fail = TypeError("list_conversations() got an unexpected keyword argument")
    with pytest.raises(TypeError):
        slot_cap(mode="block")(_request())


def test_pages_through_the_store(store: _Store) -> None:
    store.rows = [_conv(f"x{i}", f"row {i}") for i in range(250)] + THREE
    result = slot_cap(mode="block")(_request())
    assert result["result"] == "DENY"
    assert len(store.calls) == 3
    assert store.calls[1]["after"] == "x99"


def test_overflow_counts_past_the_limit(store: _Store) -> None:
    store.rows = [*THREE, _working("c4", "Fourth", NOW - 30, **{labels.OVERFLOW: "1"})]
    result = slot_cap(mode="block")(_request())
    assert result["reason"].startswith("Slots full: 4 of 3 taken; this one would make 5 of 3.")
    assert "4. Fourth" in result["reason"]


def test_a_claim_counts_before_its_row_shows_it(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.rows = THREE[:2]
    store.own["conv_a"] = _conv("conv_a", "A")
    store.own["conv_b"] = _conv("conv_b", "B")
    policy = slot_cap(mode="block")

    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_a")
    assert policy(_request())["result"] == "ALLOW"

    # The rows have not caught up with A's claim; B must still be held.
    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_b")
    held = policy(_request())
    assert held["result"] == "DENY"
    assert held["reason"].startswith("Slots full: 3 of 3 taken; this one would make 4 of 3.")
    assert "1 more claimed a slot a moment ago." in held["reason"]

    # A's own re-evaluation never counts its pending claim against itself.
    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_a")
    assert policy(_request())["result"] == "ALLOW"

    # Once the row shows the claim, the pending entry is dropped, not double counted.
    store.rows = [*THREE[:2], _working("conv_a", "A", NOW)]
    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_b")
    assert policy(_request())["reason"].startswith(
        "Slots full: 3 of 3 taken; this one would make 4 of 3."
    )

    # And a claim that never lands expires.
    store.rows = THREE[:2]
    monkeypatch.setattr(slot_cap_module, "_now", lambda: float(NOW + 61))
    assert policy(_request())["result"] == "ALLOW"


def test_factory_validates_its_parameters() -> None:
    with pytest.raises(ValueError, match="mode"):
        slot_cap(mode="loud")
    with pytest.raises(ValueError, match="limit"):
        slot_cap(limit=0)


def test_summary_and_elapsed() -> None:
    row = _working("c9", "  Titled  ", NOW - 90_000, **{labels.TASK: "T1", labels.GATE: "open"})
    summary = sessions.summarize(row, now=NOW)
    assert summary.title == "Titled"
    assert summary.elapsed == "1d 1h"
    assert summary.task == "T1"
    assert summary.gate == "open"
    assert summary.overflow is False
    assert summary.as_dict()["elapsed"] == "1d 1h"
    untitled = _conv("abcdefghij", None, {labels.STARTED: "not a number"}, created_at=NOW - 125)
    assert sessions.display_title(untitled) == "abcdefgh"
    assert sessions.started_at(untitled) == NOW - 125
    assert sessions.summarize(untitled, now=NOW).elapsed == "2m"
    assert sessions.format_elapsed(7_800) == "2h 10m"
    assert sessions.format_elapsed(-5) == "under a minute"
    assert sessions.is_top_level(untitled)
    assert not sessions.is_top_level(_conv("kid", parent="abcdefghij"))


def test_working_sessions_sorted_by_start() -> None:
    rows = [THREE[2], THREE[0], THREE[1], _conv("p", "parked", {labels.STATUS: "parked"})]
    assert [row.id for row in sessions.working_sessions(rows)] == ["c1", "c2", "c3"]


# ── 2026-09-25: failed holds no slot; the orchestrator's reserve ──


def test_a_failed_session_holds_no_slot() -> None:
    failed = _working("conv_f", "Crashed", NOW - 60)
    failed.live_status = sessions.FAILED
    alive = _working("conv_a", "Alive", NOW - 30)
    assert sessions.is_failed(failed) and not sessions.is_failed(alive)
    assert [row.id for row in sessions.working_sessions([failed, alive])] == ["conv_a"]
    assert sessions.summarize(failed, now=NOW).failed is True


def test_reserve_counts_like_the_policy_and_release_frees_it() -> None:
    rows = [_working("conv_1", "One", NOW - 90), _working("conv_2", "Two", NOW - 60)]
    held, full, working, pending = slot_cap_module.reserve("orch:a", rows, 3)
    assert (held, full, [row.id for row in working], pending) == (
        True,
        False,
        ["conv_1", "conv_2"],
        0,
    )
    # The held claim counts: a second create in the same instant finds the slots full.
    held, full, _, pending = slot_cap_module.reserve("orch:b", rows, 3)
    assert (held, full, pending) == (False, True, 1)
    # The owner chose to run one more: held, and flagged as past the limit.
    held, full, _, _ = slot_cap_module.reserve("orch:b", rows, 3, riding=True)
    assert (held, full) == (True, True)
    slot_cap_module.release("orch:a")
    slot_cap_module.release("orch:b")
    held, full, _, pending = slot_cap_module.reserve("orch:c", rows, 3)
    assert (held, full, pending) == (True, False, 0)


MERGE_NOTE = (
    "Merge watcher: PR #196 (Add an Accounts card) merged at 17:00Z as 11c89f51 by octocat. "
    "https://github.com/octo/repo/pull/196 Continue: delete its branch and worktree, "
    "then check the deploy."
)


@pytest.mark.parametrize("mode", ["warn", "block"])
def test_a_merge_watcher_note_passes_full_slots_without_a_claim(store: _Store, mode: str) -> None:
    # 2026-10-21: the note continues a session in review; it must wake it, not ASK.
    store.rows = THREE
    own = {labels.STATUS: "review"}
    store.own[OWN] = _conv(OWN, "Ship the card", own)
    assert slot_cap(mode=mode)(_request(MERGE_NOTE, own)) == {"result": "ALLOW"}
    assert store.calls == []
    # The same slots still hold a typed prompt, and a note's words mid-message are not a note.
    typed = slot_cap(mode=mode)(_request("Merged it, Merge watcher: PR #196 (x)", own))
    assert typed is not None and typed["result"] in ("ASK", "DENY")


@pytest.mark.parametrize("mode", ["warn", "block"])
def test_the_old_lead_handoff_words_get_no_exemption(store: _Store, mode: str) -> None:
    # The sweep no longer posts that note, so a prompt starting with its words is ordinary.
    store.rows = THREE
    own = {labels.STATUS: "review"}
    store.own[OWN] = _conv(OWN, "Audit", own)
    typed = slot_cap(mode=mode)(
        _request("Lead handoff: this chat's context is about 412,000 tokens.", own)
    )
    assert typed is not None and typed["result"] in ("ASK", "DENY")


def test_a_first_prompt_in_the_merge_watcher_words_still_counts(store: _Store) -> None:
    # Review, 2026-10-21: a session with no status yet gets the normal count.
    store.rows = THREE
    result = slot_cap(mode="warn")(_request(MERGE_NOTE))
    assert result is not None and result["result"] == "ASK"


# ── families (2026-10-23) ────────────────────────────────────────────────


def _child(id: str, title: str, parent: str, started: int, **extra: str) -> Conversation:
    return _working(id, title, started, **{labels.ORCH: parent, **extra})


def test_family_key_follows_the_orch_chain_to_its_top() -> None:
    lead = _working("lead", "Role routing build", NOW - 900)
    child = _child("kid", "Families PR", "lead", NOW - 600)
    grandchild = _child("grand", "Its helper", "kid", NOW - 300)
    orphan = _child("orphan", "Archived lead's child", "gone", NOW - 200)
    unknown = _child("solo", "Started without an id", labels.ORCH_UNKNOWN, NOW - 100)
    keys = sessions.family_keys([lead, child, grandchild, orphan, unknown])
    assert keys == {
        "lead": "lead",
        "kid": "lead",
        "grand": "lead",
        "orphan": "gone",
        "solo": "solo",
    }
    loop = [_child("a", "A", "b", NOW), _child("b", "B", "a", NOW)]
    assert set(sessions.family_keys(loop).values()) <= {"a", "b"}


def test_a_family_holds_one_slot() -> None:
    rows = [
        _working("lead", "Lead", NOW - 900),
        _child("k1", "One", "lead", NOW - 600),
        _child("k2", "Two", "lead", NOW - 500),
        _working("other", "Other", NOW - 400),
    ]
    keys = sessions.family_keys(rows)
    assert sessions.slots_held(sessions.working_sessions(rows)) == 2
    kids = sessions.family_children_working("lead", sessions.working_sessions(rows), keys)
    assert [row.id for row in kids] == ["k1", "k2"]


def test_reserve_for_a_family_that_holds_a_slot_takes_none() -> None:
    rows = [_working("lead", "Lead", NOW - 900), *THREE[:2]]
    held, full, _, _ = slot_cap_module.reserve("orch:x", rows, 3, family="lead")
    assert (held, full) == (True, False)
    # Nothing was claimed, so a session outside the family still finds the last slot.
    held, full, _, _ = slot_cap_module.reserve("orch:y", rows, 4)
    assert (held, full) == (True, False)
    slot_cap_module.release("orch:y")
    # A family member never needs a slot, even when its lead holds none (2026-10-24).
    held, full, _, _ = slot_cap_module.reserve("orch:z", rows, 3, family="elsewhere")
    assert (held, full) == (True, False)


def test_a_create_from_a_lead_in_review_skips_the_slot_check_but_not_the_family_cap() -> None:
    lead = _conv("lead", "Lead", {labels.STATUS: "review"})
    rows = [lead, *THREE, _child("k1", "One", "lead", NOW - 600)]
    rows[-1].live_status = "running"
    held, full, _, _ = slot_cap_module.reserve("orch:a", rows, 3, family="lead", family_cap=3)
    assert (held, full) == (True, False)
    slot_cap_module.release("orch:a")
    with pytest.raises(slot_cap_module.FamilyFull):
        slot_cap_module.reserve("orch:b", rows, 3, family="lead", family_cap=1)
    # A create with no family still counts slots.
    held, full, _, _ = slot_cap_module.reserve("orch:c", rows, 3)
    assert (held, full) == (False, True)


def test_the_event_names_the_session_even_without_the_ambient_id(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.rows = THREE
    store.own["child"] = _conv("child", "Codex reviewer", parent="c1")
    event = {**_request(), "context": {"labels": {}, "conversation_id": "child"}}
    for ambient in (None, OWN):
        monkeypatch.setattr(sessions, "current_session_id", lambda ambient=ambient: ambient)
        assert slot_cap(mode="block")(event) is None
    assert store.calls == []


def test_a_lead_whose_family_holds_a_slot_rides_in_it(store: _Store) -> None:
    lead = _conv(OWN, "Lead in review", {labels.STATUS: "review"})
    store.own[OWN] = lead
    store.rows = [
        lead,
        _child("k1", "Working child", OWN, NOW - 600),
        *THREE[:2],
    ]
    store.rows[1].live_status = "running"
    # Three families hold slots: the lead's own, c1 and c2. Held, so it rides.
    for mode in ("warn", "block"):
        result = slot_cap(mode=mode)(_request())
        assert result is not None and result["result"] == "ALLOW"
        assert labels.OVERFLOW not in result["set_labels"]
    # With no working child its family holds nothing, and a full set of slots asks.
    store.rows = [lead, *THREE]
    held = slot_cap()(_request())
    assert held is not None and held["result"] == "ASK"


def test_a_family_member_prompts_through_full_slots(store: _Store) -> None:
    lead = _working("lead", "Lead", NOW - 900)
    store.rows = [lead, *THREE[:2]]
    fork = _conv(OWN, "Fork of the lead's child", {labels.ORCH: "lead"})
    store.own[OWN] = fork
    store.rows.append(fork)
    result = slot_cap()(_request())
    assert result is not None and result["result"] == "ALLOW"
    assert labels.OVERFLOW not in result["set_labels"]
    # Outside the family the same three slots are full.
    store.own[OWN] = _conv(OWN, "Fresh")
    store.rows[-1] = store.own[OWN]
    held = slot_cap()(_request())
    assert held is not None and held["result"] == "ASK"


def test_concurrent_family_reserves_stop_at_the_cap() -> None:
    # Two creates from one lead in the same instant: the second sees the first's claim.
    rows = [
        _working("lead", "Lead", NOW - 900),
        _child("k1", "One", "lead", NOW - 600),
        _child("k2", "Two", "lead", NOW - 500),
    ]
    held, full, _, claims = slot_cap_module.reserve(
        "orch:k3", rows, 3, family="lead", family_cap=3
    )
    assert (held, full, claims) == (True, False, 0)
    with pytest.raises(slot_cap_module.FamilyFull) as caught:
        slot_cap_module.reserve("orch:k4", rows, 3, family="lead", family_cap=3)
    assert caught.value.cap == 3
    assert [row.id for row in caught.value.children] == ["k1", "k2"]
    # A claim riding in the lead's slot takes no slot from anyone else.
    held, full, _, claims = slot_cap_module.reserve("orch:other", rows, 2)
    assert (held, full, claims) == (True, False, 0)
    slot_cap_module.release("orch:k3")
    slot_cap_module.reserve("orch:k4", rows, 3, family="lead", family_cap=3)


def test_a_family_member_past_the_cap_is_allowed_and_marked_overflow(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        slot_cap_module.slot_limit,
        "resolve",
        lambda *_: slot_cap_module.slot_limit.SlotConfig(3, False, 2),
    )
    fork = _conv(OWN, "Fork of a child", {labels.ORCH: "lead"})
    store.own[OWN] = fork
    store.rows = [
        _working("lead", "Lead", NOW - 900),
        _child("k1", "One", "lead", NOW - 600),
        _child("k2", "Two", "lead", NOW - 500),
        fork,
    ]
    for mode in ("warn", "block"):
        result = slot_cap(mode=mode)(_request())
        assert result is not None and result["result"] == "ALLOW"
        assert result["set_labels"][labels.STATUS] == labels.WORKING
        assert result["set_labels"][labels.OVERFLOW] == "1"


def test_a_member_whose_lead_is_not_working_never_asks_for_a_slot(store: _Store) -> None:
    member = _conv(OWN, "Helper", {labels.ORCH: "lead"})
    store.own[OWN] = member
    store.rows = [
        _conv("lead", "Lead", {labels.STATUS: "review"}),
        *THREE,
        member,
    ]
    for mode in ("warn", "block"):
        result = slot_cap(mode=mode)(_request())
        assert result is not None and result["result"] == "ALLOW"
        assert labels.OVERFLOW not in result["set_labels"]
        assert result["set_labels"][labels.STATUS] == labels.WORKING


def test_an_idle_family_member_does_not_count_toward_the_family_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(slot_cap_module, "_now", lambda: float(NOW))
    idle = _child("k1", "One", "lead", NOW - 600)
    idle.live_status = sessions.IDLE
    busy = _child("k2", "Two", "lead", NOW - 500)
    busy.live_status = "running"
    rows = [_working("lead", "Lead", NOW - 900), idle, busy]
    keys = sessions.family_keys(rows)
    kids = sessions.family_children_working(
        "lead", sessions.working_sessions(rows), keys, float(NOW)
    )
    assert [row.id for row in kids] == ["k2"]
    # A cap of one: the idle helper leaves room, the running one does not.
    held, _, _, _ = slot_cap_module.reserve("orch:a", rows[:2], 3, family="lead", family_cap=1)
    assert held
    slot_cap_module.release("orch:a")
    with pytest.raises(slot_cap_module.FamilyFull):
        slot_cap_module.reserve("orch:b", rows, 3, family="lead", family_cap=1)


def test_a_just_created_idle_helper_still_counts_as_busy() -> None:
    fresh = _child("k1", "New", "lead", NOW - sessions.FRESH_HELPER_S + 1)
    fresh.live_status = sessions.IDLE
    old = _child("k2", "Done", "lead", NOW - sessions.FRESH_HELPER_S)
    old.live_status = sessions.IDLE
    rows = [_working("lead", "Lead", NOW - 900), fresh, old]
    keys = sessions.family_keys(rows)
    working = sessions.working_sessions(rows)
    kids = sessions.family_children_working("lead", working, keys, float(NOW))
    assert [row.id for row in kids] == ["k1"]
    kids = sessions.family_children_working("lead", working, keys, float(NOW) + 60)
    assert kids == []


def test_the_slots_full_reason_counts_families() -> None:
    working = [
        sessions.summarize(_working("lead", "Lead", NOW - 900), now=NOW),
        sessions.summarize(_child("k1", "One", "lead", NOW - 600), now=NOW),
    ]
    text = slot_cap_module.reason_text(working, 1, "block", None, used=1)
    assert text.startswith("Slots full: 1 of 1 taken; this one would make 2 of 1. 1. Lead")


# --- "Helper sessions count toward the session limit" (Settings, 2026-10-26) ---


def _helpers_rows() -> list[Conversation]:
    return [
        _working("lead", "Lead", NOW - 900),
        _child("k1", "Builder", "lead", NOW - 600),
        _child("k2", "Reviewer", "lead", NOW - 500),
        _conv("done", "Finished", {labels.STATUS: "review", labels.ORCH: "lead"}),
        _child("orphan", "Lead parked or gone", "gone", NOW - 400),
        _working("other", "Other", NOW - 300),
    ]


def test_helpers_off_children_and_orphans_never_count() -> None:
    working = sessions.working_sessions(_helpers_rows())
    assert sessions.slots_held(working) == 2
    assert sessions.held_keys(working) == {"lead", "other"}


def test_helpers_on_each_working_child_counts_finished_ones_do_not() -> None:
    working = sessions.working_sessions(_helpers_rows())
    assert sessions.slots_held(working, helpers_count=True) == 5
    assert "done" not in sessions.held_keys(working, helpers_count=True)


def test_a_child_whose_lead_is_parked_holds_nothing_when_off() -> None:
    lead = _conv("lead", "Lead", {labels.STATUS: "parked"})
    rows = [lead, _child("k1", "Builder", "lead", NOW - 600), *THREE[:2]]
    assert sessions.slots_held(sessions.working_sessions(rows)) == 2
    held, full, _, _ = slot_cap_module.reserve("orch:a", rows, 3)
    assert (held, full) == (True, False)


def test_reserve_for_a_child_takes_a_slot_only_when_helpers_count() -> None:
    rows = [_working("lead", "Lead", NOW - 900), *THREE[:2]]
    held, full, _, _ = slot_cap_module.reserve("orch:x", rows, 3, family="lead")
    assert (held, full) == (True, False)
    slot_cap_module.release("orch:x")
    held, full, _, _ = slot_cap_module.reserve(
        "orch:y", rows, 3, family="lead", helpers_count=True
    )
    assert (held, full) == (False, True)
    held, full, _, _ = slot_cap_module.reserve(
        "orch:z", rows, 3, family="lead", riding=True, helpers_count=True
    )
    assert (held, full) == (True, True)
    slot_cap_module.release("orch:z")


def test_pending_child_claims_count_only_when_helpers_count() -> None:
    rows = [_working("lead", "Lead", NOW - 900)]
    slot_cap_module.reset_pending_claims()
    slot_cap_module.reserve("orch:a", rows, 3, family="lead")
    _, _, _, pending = slot_cap_module.reserve("orch:c", rows, 3)
    assert pending == 0
    slot_cap_module.reset_pending_claims()
    slot_cap_module.reserve("orch:a", rows, 3, family="lead", helpers_count=True)
    _, _, _, pending = slot_cap_module.reserve("orch:c", rows, 3, helpers_count=True)
    assert pending == 1
    slot_cap_module.reset_pending_claims()


def test_a_family_member_prompt_asks_at_full_slots_when_helpers_count(store: _Store) -> None:
    store.rows = [_working("lead", "Lead", NOW - 900), *THREE[:2]]
    member = _conv(OWN, "Helper", {labels.ORCH: "lead"})
    store.own[OWN] = member
    store.rows.append(member)
    free = slot_cap()(_request())
    assert free is not None and free["result"] == "ALLOW"
    counted = slot_cap(helpers_count=True)(_request())
    assert counted is not None and counted["result"] == "ASK"
    store.rows[0] = _conv("lead", "Lead", {labels.STATUS: "parked"})
    counted = slot_cap(helpers_count=True)(_request())
    assert counted is not None and counted["result"] == "ALLOW"


def test_the_setting_reads_from_enabled_slot_caps_only() -> None:
    from types import SimpleNamespace

    from ae_omni_policies import slot_limit

    def cap(enabled: bool, **params: object) -> SimpleNamespace:
        return SimpleNamespace(handler=slot_limit.HANDLER, enabled=enabled, factory_params=params)

    assert slot_limit.helpers_count_from_policies([]) is False
    assert slot_limit.helpers_count_from_policies([cap(True)]) is False
    assert slot_limit.helpers_count_from_policies([cap(False, helpers_count=True)]) is False
    assert slot_limit.helpers_count_from_policies([cap(True, helpers_count=True)]) is True


# --- one resolved rule set, review fixes (2026-10-26) ---


def _cap_policy(enabled: bool = True, **params: object) -> Any:
    from types import SimpleNamespace

    from ae_omni_policies import slot_limit

    return SimpleNamespace(handler=slot_limit.HANDLER, enabled=enabled, factory_params=params)


def test_resolve_takes_all_three_from_the_cap_with_the_lowest_limit() -> None:
    from ae_omni_policies import slot_limit

    # Cap A: limit 3, helpers off. Cap B: limit 5, helpers on. Taking the flag from any
    # cap would make a rule stricter than both (limit 3 with helpers counted).
    a = _cap_policy(limit=3, helpers_count=False, family_children=2)
    b = _cap_policy(limit=5, helpers_count=True, family_children=4)
    assert slot_limit.resolve_from_policies([a, b], 9) == (3, False, 2)
    assert slot_limit.resolve_from_policies([b, a], 9) == (3, False, 2)
    # Tied on the limit: the one that counts helpers is the stricter.
    tied = _cap_policy(limit=3, helpers_count=True, family_children=0)
    assert slot_limit.resolve_from_policies([a, tied], 9) == (3, True, 2)
    # A disabled cap takes no part; none at all gives the defaults.
    assert slot_limit.resolve_from_policies([_cap_policy(False, limit=1), b], 9) == (5, True, 4)
    assert slot_limit.resolve_from_policies([], 9) == (9, False, 3)


def test_two_caps_never_make_a_rule_stricter_than_both() -> None:
    rows = [
        _working("l1", "Lead one", NOW - 900),
        _working("l2", "Lead two", NOW - 800),
        _child("k1", "Helper", "l1", NOW - 700),
    ]
    held_a = len(sessions.held_keys(sessions.working_sessions(rows), helpers_count=False))
    held_b = len(sessions.held_keys(sessions.working_sessions(rows), helpers_count=True))
    assert (held_a, held_b) == (2, 3)  # passes cap A (3, off) and cap B (5, on)
    from ae_omni_policies import slot_limit

    limit, helpers, _ = slot_limit.resolve_from_policies(
        [_cap_policy(limit=3), _cap_policy(limit=5, helpers_count=True)], 9
    )
    held, full, _, _ = slot_cap_module.reserve(
        "orch:mixed", rows, limit, family="l1", helpers_count=helpers
    )
    assert (held, full) == (True, False)
    slot_cap_module.release("orch:mixed")


def test_resolve_reads_the_store_once() -> None:
    from ae_omni_policies import slot_limit

    class Flip:
        calls = 0

        def list_defaults(self) -> list[Any]:
            Flip.calls += 1
            # A save lands between reads: the second read would be a different config.
            if Flip.calls == 1:
                return [_cap_policy(limit=3, helpers_count=True, family_children=2)]
            return [_cap_policy(limit=8, helpers_count=False, family_children=6)]

    slot_limit.remember_store(Flip())  # type: ignore[arg-type]
    try:
        assert slot_limit.resolve() == (3, True, 2)
        assert Flip.calls == 1
    finally:
        slot_limit.remember_store(None)


def test_stored_helpers_count_values_that_are_not_booleans_mean_off() -> None:
    from ae_omni_policies import slot_limit

    for stored in ("true", "True", 1, "1", None, [True]):
        assert slot_limit.resolve_from_policies([_cap_policy(helpers_count=stored)], 3)[1] is False
    assert slot_limit.resolve_from_policies([_cap_policy(helpers_count=True)], 3)[1] is True


def test_the_family_cap_still_applies_to_a_member_prompt_when_helpers_count(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ae_omni_policies import slot_limit

    monkeypatch.setattr(slot_cap_module, "_now", lambda: float(NOW))
    slot_limit.remember_store(
        type("S", (), {"list_defaults": lambda self: [_cap_policy(limit=9, family_children=2)]})()
    )
    try:
        store.rows = [
            _working("lead", "Lead", NOW - 900),
            _child("k1", "One", "lead", NOW - 600),
            _child("k2", "Two", "lead", NOW - 500),
        ]
        member = _conv(OWN, "Third helper", {labels.ORCH: "lead"})
        store.own[OWN] = member
        store.rows.append(member)
        for helpers in (False, True):
            slot_cap_module.reset_pending_claims()
            verdict = slot_cap(limit=9, helpers_count=helpers)(_request())
            assert verdict is not None and verdict["result"] == "ALLOW"
            assert verdict["set_labels"].get(labels.OVERFLOW) == "1", helpers
    finally:
        slot_limit.remember_store(None)


def test_a_working_helper_whose_lead_is_archived_holds_its_own_slot_when_on() -> None:
    rows = [_child("k1", "Orphan helper", "gone", NOW - 600), *THREE[:2]]
    working = sessions.working_sessions(rows)
    assert sessions.slots_held(working) == 2
    assert sessions.slots_held(working, helpers_count=True) == 3
    held, full, _, _ = slot_cap_module.reserve("orch:o", rows, 3, helpers_count=True)
    assert (held, full) == (False, True)


def test_a_self_parent_or_orch_cycle_counts_as_a_lead() -> None:
    selfish = _working("a", "Self parent", NOW - 600, **{labels.ORCH: "a"})
    ring_a = _working("r1", "Ring one", NOW - 500, **{labels.ORCH: "r2"})
    ring_b = _working("r2", "Ring two", NOW - 400, **{labels.ORCH: "r1"})
    working = sessions.working_sessions([selfish, ring_a, ring_b])
    assert sessions.held_keys(working) == {"a", "r1", "r2"}
    # A row that only feeds into a loop is still a helper.
    tail = _working("t", "Tail", NOW - 300, **{labels.ORCH: "r1"})
    assert sessions.is_helper(tail, {row.id: row for row in [ring_a, ring_b, tail]})


def test_slots_held_takes_helpers_count_by_keyword_only() -> None:
    with pytest.raises(TypeError):
        sessions.slots_held([], {}, True)  # type: ignore[misc]


def test_the_card_lists_slot_holders_only_and_trims_long_titles() -> None:
    long_title = "Builder: account switch cascades to child chats and every open tab"
    rows = [
        _working("lead", long_title, NOW - 900),
        _child("k1", "Child one", "lead", NOW - 600),
        _child("k2", "Child two", "lead", NOW - 300),
    ]
    working = sessions.working_sessions(rows)
    held = sessions.held_keys(working, by_id={row.id: row for row in rows})
    shown = [sessions.summarize(row, now=NOW) for row in working if row.id in held]
    text = slot_cap_module.reason_text(shown, 1, "warn", None, used=len(held))
    assert text.startswith("Slots full: 1 of 1 taken. Run this as the 2nd? 1. Builder: account")
    assert "..." in text and long_title not in text
    assert "Child one" not in text and "Child two" not in text


def test_ordinals() -> None:
    ordinal = slot_cap_module._ordinal
    assert [ordinal(n) for n in (1, 2, 3, 4, 7, 11, 12, 13, 21, 22, 112)] == [
        "1st",
        "2nd",
        "3rd",
        "4th",
        "7th",
        "11th",
        "12th",
        "13th",
        "21st",
        "22nd",
        "112th",
    ]


def test_the_park_suggestion_names_one_session_when_only_one_is_listed() -> None:
    working = [sessions.summarize(_working("lead", "Lead", NOW - 900), now=NOW)]
    text = slot_cap_module.reason_text(working, 1, "warn", None, used=1)
    assert "To park one instead, the longest running: Lead." in text
    assert slot_cap_module.reason_text([], 1, "warn", None, used=1).count("longest") == 0


def test_the_warn_text_keeps_the_shape_the_web_card_parses() -> None:
    # web/src/lib/aeSlotsFull.ts splits this text on these exact sentences (2026-10-29);
    # reword one here and that parser, and its fixtures, change with it.
    working = [
        sessions.summarize(_working("a", "Alpha (draft). Two", NOW - 100_800), now=NOW),
        sessions.summarize(_working("b", "Beta", NOW - 600, **{labels.NEXT: "do x"}), now=NOW),
    ]
    text = slot_cap_module.reason_text(
        working, 2, "warn", "https://lot.example/p", pending=1, used=2, others=1
    )
    assert text == (
        "Slots full: 3 of 2 taken. Run this as the 4th? "
        "1. Alpha (draft). Two (elapsed 1d 4h) 2. Beta (next: do x) "
        "Plus 1 of another user's. 1 more claimed a slot a moment ago. "
        "To park one instead, the longest running: Alpha (draft). Two or Beta. "
        "Accept to run it as overflow, or decline, then "
        "park one in the Parking Lot (https://lot.example/p) or flip one to review."
    )
