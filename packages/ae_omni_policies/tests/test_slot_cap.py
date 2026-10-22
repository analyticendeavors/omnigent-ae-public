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
    assert reason.startswith("Slots full: 3 of 3 working. ")
    assert "1. Fix pricing padding (next: verify light mode)" in reason
    assert "2. Renewal note (elapsed 10m)" in reason
    assert "3. CSV export header (elapsed under a minute)" in reason
    assert "Accept to run one more (marked overflow)" in reason
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
    assert result["reason"].startswith("Slots full: 4 of 3 working.")
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
    assert held["reason"].startswith("Slots full: 3 of 3 working.")
    assert "1 more claimed a slot a moment ago." in held["reason"]

    # A's own re-evaluation never counts its pending claim against itself.
    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_a")
    assert policy(_request())["result"] == "ALLOW"

    # Once the row shows the claim, the pending entry is dropped, not double counted.
    store.rows = [*THREE[:2], _working("conv_a", "A", NOW)]
    monkeypatch.setattr(sessions, "current_session_id", lambda: "conv_b")
    assert policy(_request())["reason"].startswith("Slots full: 3 of 3 working.")

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
    # Reid chose to run one more: held, and flagged as past the limit.
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


def test_a_first_prompt_in_the_merge_watcher_words_still_counts(store: _Store) -> None:
    # Review, 2026-10-21: a session with no status yet gets the normal count.
    store.rows = THREE
    result = slot_cap(mode="warn")(_request(MERGE_NOTE))
    assert result is not None and result["result"] == "ASK"
