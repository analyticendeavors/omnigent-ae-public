"""compact_queue.py: the queued Compact runs on the server, judged by real turn ends.

The driver core takes every collaborator as a parameter, so these tests never touch the app:
the posts, the snooze, the pending inputs, the live status and the store listing are stubs,
the clock is two numbers the tests move, and the turn edges and items are fed to the book
the way the fork patch's observers feed them. Added 2026-11-06 with the driver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ae_omni_policies.compact_queue import (
    MSG_NEVER_STARTED,
    MSG_NOT_CLEAN,
    MSG_RESTART,
    MSG_RESTART_PREPARING,
    MSG_TOO_LONG,
    PREP_LIMIT_S,
    PROMPT_STUCK_QUIET_S,
    RESUME_LIMIT_S,
    STABLE_S,
    START_LIMIT_S,
    STEER_GRACE_S,
    UNKNOWN_QUIET_S,
    AlreadyQueued,
    CompactQueue,
    CompactSending,
    EdgeBook,
    StateFile,
)
from ae_omni_policies.continuation import (
    COMPACT_CANCELLED_TEXT,
    COMPACT_PREP_TEXT,
    COMPACT_RESUME_TEXT,
)

SID = "conv_a"
NOW = 1_800_000_000.0


@dataclass
class Item:
    id: str
    type: str
    data: dict[str, Any]
    created_at: float = 0.0


def prep_item(item_id: str = "i_prep", at: float = NOW) -> Item:
    return Item(
        item_id,
        "message",
        {"role": "user", "content": [{"type": "input_text", "text": COMPACT_PREP_TEXT}]},
        at,
    )


def reply_item(item_id: str = "i_reply", at: float = NOW) -> Item:
    return Item(
        item_id,
        "message",
        {"role": "assistant", "content": [{"type": "output_text", "text": "ready"}]},
        at,
    )


def user_item(item_id: str = "i_user", text: str = "go ahead", at: float = NOW) -> Item:
    return Item(
        item_id, "message", {"role": "user", "content": [{"type": "input_text", "text": text}]}, at
    )


def error_item(item_id: str = "i_err", at: float = NOW) -> Item:
    return Item(item_id, "error", {"level": "error", "code": "x", "message": "boom"}, at)


@dataclass
class World:
    """The stubs one queue talks to, and the clock."""

    now: float = NOW
    mono: float = 1000.0
    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    post_answer: tuple[int, str] = (202, "")
    #: Answers for the resume note, taken in order before ``post_answer`` applies.
    resume_answers: list[tuple[int, str]] = field(default_factory=list)
    #: The process dies on the compact POST (the plan stays in ``compacting``).
    crash_on_compact: bool = False
    snoozes: list[str] = field(default_factory=list)
    snooze_answer: Any = 123
    pending: list[str] = field(default_factory=list)
    live: str | None = "idle"
    store_items: list[Item] = field(default_factory=list)
    compactions: list[Item] = field(default_factory=list)
    book: EdgeBook = field(init=False)

    def __post_init__(self) -> None:
        self.book = EdgeBook(mono=lambda: self.mono)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.mono += seconds

    async def _post(self, sid: str, body: dict[str, Any]) -> tuple[int, str]:
        self.posts.append((sid, body))
        if self.crash_on_compact and body["type"] == "compact":
            raise RuntimeError("process died")
        if body["type"] == "message" and self.resume_answers:
            if body["data"]["content"][0]["text"] == COMPACT_RESUME_TEXT:
                return self.resume_answers.pop(0)
        return self.post_answer

    async def _snooze(self, sid: str) -> Any:
        self.snoozes.append(sid)
        if isinstance(self.snooze_answer, Exception):
            raise self.snooze_answer
        return self.snooze_answer

    def _list_items(self, sid: str, **kw: Any) -> list[Item]:
        if kw.get("type") == "compaction":
            return list(self.compactions)
        if kw.get("order") == "desc":
            return list(reversed(self.store_items))[: kw.get("limit", 100)]
        after = kw.get("after")
        rows = self.store_items
        if after is not None:
            ids = [i.id for i in rows]
            rows = rows[ids.index(after) + 1 :] if after in ids else rows
        return list(rows)

    def queue(self, store: StateFile | None = None, boot_id: str | None = None) -> CompactQueue:
        return CompactQueue(
            store=store or StateFile(None),
            book=self.book,
            post=self._post,
            snooze=self._snooze,
            pending=lambda sid: list(self.pending),
            live_status=lambda sid: self.live,
            list_items=self._list_items,
            clock=lambda: self.now,
            mono=lambda: self.mono,
            boot_id=boot_id,
        )

    # edges, the way the fork patch's observer reports them
    def turn(self, completed: bool = True) -> None:
        self.book.note(SID, "running")
        if completed:
            self.book.note(SID, "idle", turn_completed=True)

    def stop(self) -> None:
        self.book.note(SID, "idle", turn_completed=True)


def prompts(world: World) -> list[dict[str, Any]]:
    return [b for _, b in world.posts if b["type"] == "message"]


def compacts(world: World) -> list[dict[str, Any]]:
    return [b for _, b in world.posts if b["type"] == "compact"]


async def settle(world: World, q: CompactQueue, seconds: float = STABLE_S) -> None:
    world.advance(seconds)
    await q.tick()


# ── the happy path ──────────────────────────────────────────────────────


@pytest.mark.parametrize("snooze", [False, True])
async def test_prompt_then_compact_once_and_snooze_when_asked(snooze: bool) -> None:
    world = World()
    q = world.queue()
    world.turn()
    q.queue(SID, snooze=snooze)
    await q.tick()
    assert world.posts == []  # an edge only just arrived
    await settle(world, q)
    assert prompts(world) == [
        {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": COMPACT_PREP_TEXT}],
            },
        }
    ]
    assert q.state(SID)[0] == "preparing"
    await settle(world, q)
    assert len(prompts(world)) == 1 and compacts(world) == []
    world.book.note_items(SID, [prep_item(), reply_item()])
    world.book.note(SID, "running")
    world.stop()
    await q.tick()
    assert compacts(world) == []  # not quiet for 2 s yet
    await settle(world, q)
    assert compacts(world) == [{"type": "compact", "data": {}}]
    assert q.state(SID) == (None, None)
    assert world.snoozes == ([SID] if snooze else [])
    await settle(world, q)
    assert len(compacts(world)) == 1


# ── what counts as a turn end ───────────────────────────────────────────


async def test_a_silent_think_after_a_bare_idle_is_not_a_turn_end() -> None:
    """Attempt A: a watcher-note turn, a PTY idle, then 27 s of nothing."""
    world = World()
    q = world.queue()
    world.stop()
    world.advance(10)
    world.book.note(SID, "running")  # the watcher note's turn
    world.book.note(SID, "idle")  # PTY: bare idle
    q.queue(SID)
    await settle(world, q, 27)
    assert world.posts == []
    world.stop()
    await q.tick()
    await settle(world, q)
    assert len(prompts(world)) == 1


async def test_a_turn_that_ends_before_the_prompt_landed_does_not_count() -> None:
    world = World()
    q = world.queue()
    world.turn()
    q.queue(SID)
    await settle(world, q)  # prompt sent
    world.book.note(SID, "running")
    world.stop()  # the watcher-note turn ends before the prompt item
    await settle(world, q)
    assert compacts(world) == []
    world.book.note_items(SID, [prep_item(), reply_item()])
    world.book.note(SID, "running")
    world.stop()
    await settle(world, q)
    assert len(compacts(world)) == 1


async def test_a_late_stop_after_the_prompt_but_before_a_reply_does_not_compact() -> None:
    world = World()
    q = world.queue()
    world.turn()
    q.queue(SID)
    await settle(world, q)
    world.book.note_items(SID, [prep_item()])
    world.stop()
    await settle(world, q)
    assert compacts(world) == []
    world.book.note_items(SID, [reply_item()])
    world.stop()
    await settle(world, q)
    assert len(compacts(world)) == 1


async def test_a_stop_posted_before_its_own_prompt_and_reply_items_still_compacts() -> None:
    """Live check 2026-11-06: each forwarder poll reads the transcript before the hook file,
    so a fast save turn posted Stop at 21:05:09.86 and its items at 21:05:10.17 to 10.19."""
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note(SID, "running")
    world.stop()
    world.advance(0.3)
    world.book.note_items(SID, [prep_item(), reply_item()])
    await settle(world, q, 1.9)
    assert compacts(world) == []  # the items count as activity: 2 s from them, not the Stop
    await settle(world, q, 0.2)
    assert compacts(world) == [{"type": "compact", "data": {}}]


async def test_a_running_edge_just_after_the_reply_holds_the_compact_for_its_turn() -> None:
    """A watcher turn ends after the send, then the save turn's reply lands a moment before
    its running edge (the runner and the forwarder post on separate paths)."""
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.turn()  # the watcher note's turn, after the send
    world.advance(5)
    world.book.note_items(SID, [prep_item(), reply_item()])
    world.book.note(SID, "running")  # the save turn is still going
    world.live = "running"
    await settle(world, q, UNKNOWN_QUIET_S + 30)
    assert compacts(world) == []  # a turn opened after the last end, and it reads running
    world.live = "idle"
    world.stop()
    await settle(world, q)
    assert len(compacts(world)) == 1


async def test_a_stop_failure_posted_before_the_prompt_item_compacts_nothing() -> None:
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note(SID, "running")
    world.book.note(SID, "failed")
    world.book.note_items(SID, [prep_item(), reply_item()])
    await settle(world, q, 5)
    failure = q.state(SID)[1]
    assert failure is not None and failure.message == MSG_NOT_CLEAN
    assert compacts(world) == []


async def test_a_pending_message_holds_the_send_and_the_compact() -> None:
    world = World()
    q = world.queue()
    world.turn()
    world.pending = ["pi_1"]
    q.queue(SID)
    await settle(world, q, 10)
    assert world.posts == []
    world.pending = []
    await settle(world, q)
    assert len(prompts(world)) == 1
    world.book.note_items(SID, [prep_item(), reply_item()])
    world.stop()
    world.pending = ["pi_2"]  # typed during the save turn
    await settle(world, q, 10)
    assert compacts(world) == []
    world.pending = []
    world.book.note(SID, "running")
    world.stop()
    await settle(world, q)
    assert len(compacts(world)) == 1


async def test_an_unknown_chat_waits_for_a_quiet_minute_with_live_idle() -> None:
    world = World()
    q = world.queue()
    q.queue(SID)
    await settle(world, q, UNKNOWN_QUIET_S - 1)
    assert world.posts == []
    await settle(world, q, 1)
    assert len(prompts(world)) == 1
    world2 = World(live="running")
    q2 = world2.queue()
    q2.queue(SID)
    await settle(world2, q2, UNKNOWN_QUIET_S + 5)
    assert world2.posts == []


# ── restarts ────────────────────────────────────────────────────────────


def _store(tmp_path: Path) -> StateFile:
    return StateFile(tmp_path / "queue.json")


async def test_restart_while_preparing_fails_at_once_and_sends_one_cancelled_note(
    tmp_path: Path,
) -> None:
    world = World()
    q = world.queue(_store(tmp_path), boot_id="boot1")
    world.turn()
    q.queue(SID)
    await settle(world, q)
    assert len(prompts(world)) == 1
    # A fresh process on the same file: the save turn's state died with the old one.
    world2 = World(now=world.now, mono=5.0)
    world2.store_items = [prep_item("i_prep", world.now + 1), reply_item("i_reply", world.now + 5)]
    q2 = world2.queue(_store(tmp_path), boot_id="boot2")
    failure = q2.state(SID)[1]
    assert failure is not None and failure.message == MSG_RESTART_PREPARING
    await settle(world2, q2, 1)
    assert [p["data"]["content"][0]["text"] for p in prompts(world2)] == [COMPACT_CANCELLED_TEXT]
    assert compacts(world2) == []
    assert q2.state(SID)[0] is None
    await settle(world2, q2, UNKNOWN_QUIET_S + 5)
    assert len(prompts(world2)) == 1
    # The restart's plan is not left tracked once it is gone.
    assert SID not in world2.book._tracked


async def test_restart_while_queued_resumes(tmp_path: Path) -> None:
    world = World(live="idle")
    q = world.queue(_store(tmp_path), boot_id="boot1")
    q.queue(SID)
    world2 = World(now=world.now, mono=5.0)
    q2 = world2.queue(_store(tmp_path), boot_id="boot2")
    assert q2.state(SID)[0] == "queued"
    await settle(world2, q2, UNKNOWN_QUIET_S)
    assert len(prompts(world2)) == 1


async def test_restart_while_compacting_never_posts_again(tmp_path: Path) -> None:
    world = World()
    q = world.queue(_store(tmp_path), boot_id="b1")
    q.queue(SID)
    plan = q.plans[SID]
    plan.phase = "compacting"
    plan.compact_sent_at = world.now
    q._save()
    copy = tmp_path / "queue-copy.json"
    copy.write_bytes((tmp_path / "queue.json").read_bytes())

    found = World(now=world.now + 5)
    found.compactions = [Item("c1", "compaction", {}, world.now + 1)]
    q2 = found.queue(_store(tmp_path), boot_id="b2")
    await q2.tick()
    assert found.posts == [] and q2.state(SID) == (None, None)

    lost = World(now=world.now + 5)
    q3 = lost.queue(StateFile(copy), boot_id="b3")
    await q3.tick()
    assert lost.posts == []
    assert q3.state(SID)[1] is not None and q3.state(SID)[1].message == MSG_RESTART


# ── limits and failures ─────────────────────────────────────────────────


async def _until_preparing(world: World, q: CompactQueue) -> None:
    world.turn()
    q.queue(SID)
    await settle(world, q)
    assert q.state(SID)[0] == "preparing"


async def test_thirty_second_start_limit_and_fifteen_minute_limit() -> None:
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.advance(START_LIMIT_S + 1)
    await q.tick()
    assert q.state(SID)[1].message == MSG_NEVER_STARTED  # type: ignore[union-attr]
    assert compacts(world) == []

    world2 = World()
    q2 = world2.queue()
    await _until_preparing(world2, q2)
    world2.book.note_items(SID, [prep_item()])
    world2.advance(PREP_LIMIT_S + 1)
    await q2.tick()
    assert q2.state(SID)[1].message == MSG_TOO_LONG  # type: ignore[union-attr]
    assert compacts(world2) == []


@pytest.mark.parametrize("how", ["failed", "interrupt", "error"])
async def test_a_failed_interrupted_or_errored_save_turn_compacts_nothing(how: str) -> None:
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note_items(SID, [prep_item()])
    if how == "failed":
        world.book.note(SID, "failed")
    elif how == "interrupt":
        world.book.note(SID, "interrupted")
    else:
        world.book.note_items(SID, [error_item()])
    await settle(world, q, STEER_GRACE_S + 1)
    failure = q.state(SID)[1]
    assert failure is not None and failure.message == MSG_NOT_CLEAN
    assert compacts(world) == [] and q.state(SID)[0] is None


async def test_cancel_in_queued_and_preparing_and_conflict_in_compacting() -> None:
    world = World()
    q = world.queue()
    world.turn()
    q.queue(SID)
    with pytest.raises(AlreadyQueued) as err:
        q.queue(SID)
    assert err.value.phase == "queued"
    q.cancel(SID)
    await settle(world, q, 10)
    assert world.posts == [] and q.state(SID) == (None, None)
    q.cancel(SID)  # nothing queued is fine

    q.queue(SID)
    await settle(world, q)
    assert q.state(SID)[0] == "preparing"
    q.cancel(SID)
    world.book.note_items(SID, [prep_item(), reply_item()])
    world.stop()
    await settle(world, q, 5)
    assert compacts(world) == []

    q.queue(SID)
    q.plans[SID].phase = "compacting"
    with pytest.raises(CompactSending):
        q.cancel(SID)


async def test_post_failures_and_the_snooze_outcomes() -> None:
    world = World(post_answer=(500, "nope"))
    q = world.queue()
    world.turn()
    q.queue(SID)
    await settle(world, q)
    failure = q.state(SID)[1]
    assert failure is not None
    assert failure.message == "Compact cancelled: could not ask the chat to save its notes (nope)."
    assert q.state(SID)[0] is None

    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note_items(SID, [prep_item(), reply_item()])
    world.stop()
    world.post_answer = (503, "")
    await settle(world, q)
    message = q.state(SID)[1].message  # type: ignore[union-attr]
    assert message.startswith("Could not compact the session (HTTP 503).")

    for answer, stored in [
        ((409, "handoff_refused", "handed off"), False),
        ((500, "x", "bad"), True),
    ]:
        world = World(snooze_answer=answer)
        q = world.queue()
        world.turn()
        q.queue(SID, snooze=True)
        await settle(world, q)
        world.book.note_items(SID, [prep_item(), reply_item()])
        world.stop()
        await settle(world, q)
        assert len(compacts(world)) == 1
        failure = q.state(SID)[1]
        assert (failure is not None) is stored
        if failure:
            assert "could not be snoozed (bad)" in failure.message


async def test_a_plan_stuck_queued_for_a_day_fails() -> None:
    world = World(live="running")
    q = world.queue()
    q.queue(SID)
    world.advance(86400 + 1)
    await q.tick()
    assert "within a day" in q.state(SID)[1].message  # type: ignore[union-attr]


async def test_ack_clears_the_failure_and_a_new_queue_clears_it_too() -> None:
    world = World(post_answer=(500, "nope"))
    q = world.queue()
    world.turn()
    q.queue(SID)
    await settle(world, q)
    failure = q.state(SID)[1]
    assert failure is not None
    q.ack(SID, "wrong")
    assert q.state(SID)[1] is not None
    q.ack(SID, failure.id)
    assert q.state(SID)[1] is None
    await settle(world, q, 1)


# ── the state file ──────────────────────────────────────────────────────


def test_state_round_trip_and_an_unreadable_file_reads_empty(tmp_path: Path) -> None:
    world = World()
    path = tmp_path / "q.json"
    q = world.queue(StateFile(path))
    q.queue(SID, snooze=True)
    q._store_failure("conv_b", "Compact cancelled: x.")
    plans, failures = StateFile(path).load()
    assert plans[SID].snooze is True and plans[SID].phase == "queued"
    assert failures["conv_b"].message == "Compact cancelled: x."
    path.write_text("{not json", encoding="utf-8")
    assert StateFile(path).load() == ({}, {})
    assert StateFile(tmp_path / "missing.json").load() == ({}, {})
    assert StateFile(None).load() == ({}, {})


def test_the_book_ignores_items_of_chats_nobody_waits_on_and_orders_by_seq() -> None:
    book = EdgeBook()
    book.note_items("conv_x", [prep_item()])
    assert book.watched("conv_x") is None
    book.watch("conv_x", book.seq)
    book.note_items("conv_x", [reply_item(), prep_item(), reply_item("r2")])
    watch = book.watched("conv_x")
    assert watch is not None
    assert watch.prep_item_id == "i_prep"
    assert watch.reply_seq is not None and watch.prep_item_seq is not None
    assert watch.reply_seq > watch.prep_item_seq  # the reply before the prompt did not count


# ── fix round 1 (2026-11-06) ────────────────────────────────────────────


async def test_a_cancel_during_the_tail_read_sends_nothing_and_a_requeue_survives() -> None:
    world = World()
    q = world.queue()
    world.turn()
    q.queue(SID)
    world.advance(STABLE_S)
    original = world._list_items

    def cancelling(sid: str, **kw: Any) -> list[Item]:
        q.cancel(SID)
        q.queue(SID, snooze=True)  # Reid clicks again while the read is in flight
        return original(sid, **kw)

    q._list_items = cancelling
    await q.tick()
    assert world.posts == []
    assert q.state(SID)[0] == "queued" and q.plans[SID].snooze is True


async def test_a_cancel_during_the_settled_read_does_not_compact_or_drop_a_requeue() -> None:
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note_items(SID, [prep_item(), reply_item()])
    world.stop()
    world.book.note(SID, "running")  # a stray edge: settled falls back to the live read
    world.advance(UNKNOWN_QUIET_S)

    def cancelling(_sid: str) -> str:
        q.cancel(SID)
        q.queue(SID)
        return "idle"

    q._live_status = cancelling
    await q.tick()
    assert compacts(world) == []
    assert q.state(SID)[0] == "queued"


class FailingStore(StateFile):
    """A state file whose writes fail once armed."""

    def __init__(self) -> None:
        super().__init__(None)
        self.armed = False

    def save(self, plans: Any, failures: Any) -> bool:
        if self.armed:
            raise OSError("disk full")
        return super().save(plans, failures)


async def test_a_failed_save_aborts_the_prompt_and_the_compact_and_retries() -> None:
    world = World()
    store = FailingStore()
    q = world.queue(store)
    world.turn()
    q.queue(SID)
    store.armed = True
    await settle(world, q)
    assert world.posts == [] and q.state(SID)[0] == "queued"
    store.armed = False
    await settle(world, q, 1)
    assert len(prompts(world)) == 1

    world.book.note_items(SID, [prep_item(), reply_item()])
    world.stop()
    store.armed = True
    await settle(world, q)
    assert compacts(world) == [] and q.state(SID)[0] == "preparing"
    store.armed = False
    await settle(world, q, 1)
    assert len(compacts(world)) == 1


async def test_an_interrupt_ends_the_turn_for_the_settled_check() -> None:
    world = World()
    q = world.queue()
    world.book.note(SID, "running")
    world.book.note(SID, "interrupted")
    q.queue(SID)
    await q.tick()
    assert world.posts == []
    await settle(world, q)
    assert len(prompts(world)) == 1


async def test_a_stray_running_edge_after_a_stop_waits_for_a_quiet_minute() -> None:
    world = World()
    q = world.queue()
    world.turn()
    world.advance(30)
    world.book.note(SID, "running")  # a status-file edge with no real turn behind it
    q.queue(SID)
    await settle(world, q, UNKNOWN_QUIET_S - 1)
    assert world.posts == []
    await settle(world, q, 1)
    assert len(prompts(world)) == 1


# ── prompt edges, the resume note and the cancelled note (2026-11-13) ───


def tool_item(item_id: str = "i_tool", at: float = NOW) -> Item:
    return Item(item_id, "function_call", {"name": "Bash"}, at)


def reply_text_item(text: str, item_id: str = "i_reply", at: float = NOW) -> Item:
    return Item(
        item_id,
        "message",
        {"role": "assistant", "content": [{"type": "output_text", "text": text}]},
        at,
    )


async def test_a_prompt_edge_turn_with_no_running_edge_waits_for_its_stop() -> None:
    # The incident: Stop, then a prompt edge and work items but no running edge; the pending
    # inputs drain mid-turn. The old rule saw "last end after last open" and compacted.
    world = World()
    q = world.queue()
    world.stop()
    q.queue(SID)
    world.pending = ["p1"]
    world.book.note(SID, "prompt")
    world.book.note_items(SID, [tool_item("t1")])
    await settle(world, q, 30)
    world.pending = []
    await settle(world, q, 30)
    await settle(world, q, UNKNOWN_QUIET_S)
    assert world.posts == []
    world.stop()
    await settle(world, q)
    assert len(prompts(world)) == 1


async def test_a_blocked_prompt_with_no_reply_settles_after_a_live_idle_minute() -> None:
    world = World()
    q = world.queue()
    world.stop()
    q.queue(SID)
    world.book.note(SID, "prompt")
    await settle(world, q, STABLE_S)
    assert world.posts == []
    await settle(world, q, UNKNOWN_QUIET_S)
    assert len(prompts(world)) == 1


async def test_a_prep_that_sits_in_claudes_queue_is_not_cancelled_at_thirty_seconds() -> None:
    world = World()
    q = world.queue()
    world.turn()
    q.queue(SID)
    await settle(world, q)
    assert len(prompts(world)) == 1
    world.book.note(SID, "prompt")  # some other prompt reached Claude after the send
    world.advance(START_LIMIT_S + 5)
    await q.tick()
    assert q.state(SID)[0] == "preparing"
    world.book.note_items(SID, [prep_item(), reply_item()])
    world.stop()
    await settle(world, q)
    assert len(compacts(world)) == 1


@pytest.mark.parametrize("asked", [False, True])
async def test_the_resume_note_goes_out_only_after_ready_resume(asked: bool) -> None:
    world = World()
    q = world.queue()
    world.turn()
    q.queue(SID)
    await settle(world, q)
    world.book.note_items(
        SID, [prep_item(), reply_text_item("ready, resume" if asked else "ready")]
    )
    world.stop()
    await settle(world, q)
    assert len(compacts(world)) == 1
    if not asked:
        assert q.state(SID) == (None, None)
        await settle(world, q, 10)
        assert len(prompts(world)) == 1
        return
    assert q.state(SID)[0] == "compacting"  # the page sees resuming as compacting
    await settle(world, q, 10)
    assert len(prompts(world)) == 1  # no compaction item yet
    world.compactions = [Item("c1", "compaction", {}, world.now)]
    world.book.note_items(SID, [world.compactions[0]])
    await settle(world, q, STABLE_S)
    assert [p["data"]["content"][0]["text"] for p in prompts(world)] == [
        COMPACT_PREP_TEXT,
        COMPACT_RESUME_TEXT,
    ]
    assert q.state(SID) == (None, None)
    await settle(world, q, 10)
    assert len(prompts(world)) == 2


async def test_a_resume_that_never_sees_the_compaction_is_dropped_without_a_note() -> None:
    world = World()
    q = world.queue()
    world.turn()
    q.queue(SID)
    await settle(world, q)
    world.book.note_items(SID, [prep_item(), reply_text_item("ready, resume")])
    world.stop()
    await settle(world, q)
    await settle(world, q, RESUME_LIMIT_S + 1)
    assert q.state(SID) == (None, None)
    assert len(prompts(world)) == 1


async def test_restart_in_resuming_posts_at_most_once() -> None:
    world = World()
    store = StateFile(None)
    q = world.queue(store)
    world.turn()
    q.queue(SID)
    await settle(world, q)
    world.book.note_items(SID, [prep_item(), reply_text_item("ready, resume")])
    world.stop()
    await settle(world, q)
    world.compactions = [Item("c1", "compaction", {}, world.now)]
    # The process dies after writing resume_sent_at and before dropping the plan.
    q.plans[SID].resume_sent_at = world.now
    q._save()
    q2 = world.queue(store, boot_id="other")
    await settle(world, q2, 10)
    assert len(prompts(world)) == 1
    assert q2.state(SID) == (None, None)


async def test_a_crash_between_the_compact_post_and_the_resuming_save_still_resumes_once() -> None:
    world = World()
    store = StateFile(None)
    q = world.queue(store)
    world.turn()
    q.queue(SID)
    await settle(world, q)
    world.book.note_items(SID, [prep_item(), reply_text_item("ready, resume")])
    world.stop()
    world.advance(STABLE_S)
    world.crash_on_compact = True
    await q.tick()  # _send_compact saves "compacting", posts, and the process dies
    saved, _ = store.load()
    assert saved[SID].phase == "compacting" and saved[SID].resume is True
    world.crash_on_compact = False
    world.compactions = [Item("c1", "compaction", {}, world.now)]
    q2 = world.queue(store, boot_id="other")
    await settle(world, q2, STABLE_S)
    assert q2.plans[SID].phase == "resuming"
    await settle(world, q2, STABLE_S)
    texts = [p["data"]["content"][0]["text"] for p in prompts(world)]
    assert texts == [COMPACT_PREP_TEXT, COMPACT_RESUME_TEXT]
    assert q2.state(SID) == (None, None)
    await settle(world, q2, 10)
    assert len(prompts(world)) == 2
    assert len(compacts(world)) == 1


def texts_of(world: World) -> list[str]:
    return [p["data"]["content"][0]["text"] for p in prompts(world)]


@pytest.mark.parametrize("how", ["too_long", "failed", "error", "never_started"])
async def test_every_failure_after_the_prep_was_sent_owes_one_cancelled_note(how: str) -> None:
    world = World()
    q = world.queue()
    world.turn()
    q.queue(SID)
    await settle(world, q)
    if how == "failed":
        world.book.note(SID, "failed")
    elif how == "error":
        world.book.note_items(SID, [prep_item(), error_item()])
    elif how == "too_long":
        world.book.note(SID, "prompt")
        world.advance(PREP_LIMIT_S + 1)
    else:
        world.advance(START_LIMIT_S + 1)
    await q.tick()
    assert texts_of(world) == [COMPACT_PREP_TEXT, COMPACT_CANCELLED_TEXT]
    assert q.state(SID)[1] is not None
    await settle(world, q, 10)
    assert texts_of(world).count(COMPACT_CANCELLED_TEXT) == 1
    assert q.state(SID)[0] is None


async def test_a_refused_compact_post_sends_the_cancelled_note_once() -> None:
    world = World()
    q = world.queue()
    world.turn()
    q.queue(SID)
    await settle(world, q)
    world.book.note_items(SID, [prep_item(), reply_item()])
    world.stop()
    world.advance(STABLE_S)

    async def refuse_compact(sid: str, body: dict[str, Any]) -> tuple[int, str]:
        world.posts.append((sid, body))
        return (500, "boom") if body["type"] == "compact" else (202, "")

    q._post = refuse_compact
    await q.tick()
    assert texts_of(world) == [COMPACT_PREP_TEXT, COMPACT_CANCELLED_TEXT]
    failure = q.state(SID)[1]
    assert failure is not None and "Could not compact" in failure.message
    await settle(world, q, 10)
    assert texts_of(world).count(COMPACT_CANCELLED_TEXT) == 1


async def test_a_restart_that_finds_the_cancel_marker_posts_once() -> None:
    world = World()
    store = StateFile(None)
    q = world.queue(store)
    world.turn()
    q.queue(SID)
    await settle(world, q)
    world.book.note(SID, "failed")
    # The process dies before the cancelled note's post: the marker is saved, unsent.
    q._post = None  # type: ignore[assignment]
    await q.tick()
    saved, _ = store.load()
    assert saved[SID].phase == "cancelling" and saved[SID].cancel_sent_at is not None
    # Rewind to "marker written, note not yet sent" and then to "note marked sent".
    saved[SID].cancel_sent_at = None
    store.save(saved, {})
    q2 = world.queue(store, boot_id="other")
    await q2.tick()
    assert texts_of(world).count(COMPACT_CANCELLED_TEXT) == 1
    # A restart that finds it marked sent never posts again.
    saved[SID].cancel_sent_at = world.now
    store.save(saved, {})
    q3 = world.queue(store, boot_id="third")
    await q3.tick()
    assert texts_of(world).count(COMPACT_CANCELLED_TEXT) == 1
    assert q3.state(SID)[0] is None


async def test_no_cancelled_note_after_an_interrupt_or_when_the_prep_was_refused() -> None:
    world = World()
    q = world.queue()
    world.turn()
    q.queue(SID)
    await settle(world, q)
    world.book.note(SID, "prompt")
    world.book.note(SID, "interrupted")
    await settle(world, q, STEER_GRACE_S + 1)
    assert q.state(SID)[1] is not None and q.state(SID)[0] is None
    assert texts_of(world) == [COMPACT_PREP_TEXT]

    world2 = World()
    world2.post_answer = (500, "no")
    q2 = world2.queue()
    world2.turn()
    q2.queue(SID)
    await settle(world2, q2)
    assert texts_of(world2) == [COMPACT_PREP_TEXT]
    assert q2.state(SID)[0] is None


@pytest.mark.parametrize(
    ("reply", "resume"),
    [
        ("ready, resume", True),
        ("Ready; resume.", True),
        ("ready - resume", True),
        ("ready", False),
        ("Ready.", False),
        ("I cannot reply 'ready, resume'", False),
        # Live check 2026-11-13: the chat put a status paragraph after the phrase.
        ("ready, resume\n\nStill in flight: two background jobs.", True),
        ("\n  Ready, resume.\nNotes saved to the handoff.", True),
        ("ready\n\nNothing is in flight; ready, resume is not needed.", False),
        ("Notes saved.\nready, resume", False),
        # Review round 1: markdown, or a note on the same line after the answer.
        ("**ready, resume**", True),
        ("## Ready, resume", True),
        ("ready, resume: notes saved, 2 jobs running", True),
        ("ready, resume - 2 jobs running", True),
        ("ready, resume (2 jobs running)", True),
        ("**ready**: nothing in flight", False),
        ("ready, resume is not needed", False),
    ],
)
def test_only_the_last_reply_starting_with_ready_resume_asks_for_the_resume(
    reply: str, resume: bool
) -> None:
    world = World()
    world.book.watch(SID, 0)
    world.book.note_items(SID, [prep_item(), reply_text_item("working on it", "i_a")])
    world.book.note_items(SID, [reply_text_item(reply, "i_b")])
    watch = world.book.watched(SID)
    assert watch is not None and watch.resume_requested is resume


def test_a_later_plain_ready_withdraws_an_earlier_resume_request() -> None:
    world = World()
    world.book.watch(SID, 0)
    world.book.note_items(
        SID,
        [prep_item(), reply_text_item("ready, resume", "i_a"), reply_text_item("ready", "i_b")],
    )
    watch = world.book.watched(SID)
    assert watch is not None and watch.resume_requested is False


async def _to_resuming(world: World, q: CompactQueue) -> None:
    world.turn()
    q.queue(SID)
    await settle(world, q)
    world.book.note_items(SID, [prep_item(), reply_text_item("ready, resume")])
    world.stop()
    await settle(world, q)
    assert q.plans[SID].phase == "resuming"


async def test_a_resume_that_runs_out_of_time_logs_and_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = World()
    q = world.queue()
    await _to_resuming(world, q)
    assert RESUME_LIMIT_S == 900.0
    with caplog.at_level("WARNING"):
        await settle(world, q, RESUME_LIMIT_S + 1)
    assert q.state(SID) == (None, None)
    assert len(prompts(world)) == 1
    assert any(SID in r.getMessage() and "900" in r.getMessage() for r in caplog.records)


async def test_a_refused_resume_note_is_retried_then_shown_to_the_page() -> None:
    world = World()
    q = world.queue()
    await _to_resuming(world, q)
    world.resume_answers = [(500, "busy"), (500, "busy"), (500, "busy")]
    world.compactions = [Item("c1", "compaction", {}, world.now)]
    world.book.note_items(SID, [world.compactions[0]])
    await settle(world, q, STABLE_S)
    assert texts_of(world).count(COMPACT_RESUME_TEXT) == 1
    await settle(world, q, 10)
    assert texts_of(world).count(COMPACT_RESUME_TEXT) == 1  # not yet 30 s
    await settle(world, q, 25)
    assert texts_of(world).count(COMPACT_RESUME_TEXT) == 2
    await settle(world, q, 31)
    assert texts_of(world).count(COMPACT_RESUME_TEXT) == 3
    assert q.state(SID)[0] is None
    failure = q.state(SID)[1]
    assert failure is not None
    assert failure.message == (
        "Compacted, but could not tell the chat to carry on: busy. Send it a message to continue."
    )
    await settle(world, q, 60)
    assert texts_of(world).count(COMPACT_RESUME_TEXT) == 3


async def test_a_refused_resume_note_that_then_lands_stops_retrying() -> None:
    world = World()
    q = world.queue()
    await _to_resuming(world, q)
    world.resume_answers = [(503, "busy")]
    world.compactions = [Item("c1", "compaction", {}, world.now)]
    world.book.note_items(SID, [world.compactions[0]])
    await settle(world, q, STABLE_S)
    await settle(world, q, 31)
    assert texts_of(world).count(COMPACT_RESUME_TEXT) == 2
    assert q.state(SID) == (None, None)


async def test_a_resume_note_with_no_answer_is_never_retried() -> None:
    world = World()
    q = world.queue()
    await _to_resuming(world, q)
    world.resume_answers = [(0, "timeout")]
    world.compactions = [Item("c1", "compaction", {}, world.now)]
    world.book.note_items(SID, [world.compactions[0]])
    await settle(world, q, STABLE_S)
    await settle(world, q, 60)
    assert texts_of(world).count(COMPACT_RESUME_TEXT) == 1
    assert q.state(SID) == (None, None)


async def test_a_prompt_with_work_but_no_stop_settles_after_ten_idle_minutes() -> None:
    world = World()
    q = world.queue()
    world.stop()
    q.queue(SID)
    world.book.note(SID, "prompt")
    world.book.note_items(SID, [tool_item("t1")])
    await settle(world, q, PROMPT_STUCK_QUIET_S - 5)
    assert world.posts == []
    world.live = "running"
    await settle(world, q, 10)
    assert world.posts == []  # quiet long enough, but the chat does not read idle
    world.live = "idle"
    await settle(world, q, 1)
    assert len(prompts(world)) == 1


@pytest.mark.parametrize("edge", ["failed", "interrupted"])
async def test_a_failed_or_interrupted_edge_after_a_prompt_and_reply_lets_the_plan_go(
    edge: str,
) -> None:
    world = World()
    q = world.queue()
    world.stop()
    q.queue(SID)
    world.book.note(SID, "prompt")
    world.book.note_items(SID, [tool_item("t1")])
    await settle(world, q, 5)
    assert world.posts == []
    world.book.note(SID, edge)
    await settle(world, q, STABLE_S)
    assert len(prompts(world)) == 1


# ── Stop, then a new message: the owner redirects, the Compact stays queued ──


async def test_a_stop_followed_by_a_new_message_queues_the_plan_again() -> None:
    """2026-11-13: Stop, then a short follow-up 0.2 s later, cancelled the Compact."""
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note_items(SID, [prep_item()])
    world.book.note(SID, "interrupted")
    await q.tick()
    assert q.state(SID) == ("preparing", None)  # waiting to see whether a message follows
    world.advance(0.2)
    world.book.note_items(SID, [user_item()])
    world.book.note(SID, "prompt")
    await q.tick()
    assert q.state(SID) == ("queued", None)
    assert texts_of(world) == [COMPACT_PREP_TEXT]  # no cancelled note
    # The redirected turn works quietly: no save prompt until its own Stop.
    world.book.note_items(SID, [reply_item("i_work")])
    await settle(world, q, 30)
    assert texts_of(world) == [COMPACT_PREP_TEXT]
    world.stop()
    await settle(world, q)
    assert texts_of(world) == [COMPACT_PREP_TEXT, COMPACT_PREP_TEXT]
    assert q.state(SID)[0] == "preparing"
    world.book.note_items(SID, [prep_item("i_prep2"), reply_item("i_reply2")])
    world.stop()
    await settle(world, q)
    assert compacts(world) == [{"type": "compact", "data": {}}]
    assert q.state(SID) == (None, None)


async def test_a_message_after_the_grace_does_not_revive_a_cancelled_plan() -> None:
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note_items(SID, [prep_item()])
    world.book.note(SID, "interrupted")
    await settle(world, q, STEER_GRACE_S + 0.5)
    failure = q.state(SID)[1]
    assert failure is not None and failure.message == MSG_NOT_CLEAN
    world.book.note_items(SID, [user_item()])
    await settle(world, q, 10)
    assert q.state(SID)[0] is None and compacts(world) == []


async def test_only_the_save_prompt_after_a_stop_still_cancels() -> None:
    """The save prompt itself landing after the Stop is not a new message from the owner."""
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note(SID, "interrupted")
    world.book.note_items(SID, [prep_item()])
    await settle(world, q, 1)
    assert q.state(SID)[0] == "preparing"
    await settle(world, q, STEER_GRACE_S)
    failure = q.state(SID)[1]
    assert failure is not None and failure.message == MSG_NOT_CLEAN
    assert compacts(world) == []


async def test_a_redirect_message_posted_before_the_interrupt_edge_still_counts() -> None:
    """The forwarder reads the transcript before the hook file: the item can come first."""
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note_items(SID, [prep_item()])
    world.advance(0.5)
    world.book.note_items(SID, [user_item()])
    world.advance(0.5)
    world.book.note(SID, "interrupted")
    await q.tick()
    assert q.state(SID) == ("queued", None)


async def test_after_a_redirect_the_save_prompt_waits_for_that_turn_even_with_no_edges() -> None:
    """The redirected turn's prompt and running edges may lag its user item by seconds."""
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note_items(SID, [prep_item()])
    world.book.note(SID, "interrupted")
    world.book.note_items(SID, [user_item()])
    await q.tick()
    assert q.state(SID)[0] == "queued"
    world.live = "running"
    await settle(world, q, UNKNOWN_QUIET_S + 30)
    assert texts_of(world) == [COMPACT_PREP_TEXT]  # no edge yet, chat reads running: wait
    world.book.note(SID, "prompt")
    world.book.note_items(SID, [reply_item("i_work")])
    await settle(world, q, 30)
    assert texts_of(world) == [COMPACT_PREP_TEXT]
    world.live = "idle"
    world.stop()
    await settle(world, q)
    assert texts_of(world) == [COMPACT_PREP_TEXT, COMPACT_PREP_TEXT]


async def test_a_redirected_turn_whose_stop_came_first_goes_ahead_after_a_long_idle() -> None:
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note_items(SID, [prep_item()])
    world.book.note(SID, "interrupted")
    world.stop()  # a fast redirected turn: its Stop lands before its items
    world.book.note_items(SID, [user_item(), reply_item("i_work")])
    await q.tick()
    assert q.state(SID)[0] == "queued"
    await settle(world, q, UNKNOWN_QUIET_S - 5)
    assert texts_of(world) == [COMPACT_PREP_TEXT]
    await settle(world, q, 10)
    assert texts_of(world) == [COMPACT_PREP_TEXT, COMPACT_PREP_TEXT]


async def test_a_server_note_after_a_stop_is_not_a_redirect() -> None:
    world = World()
    q = world.queue()
    await _until_preparing(world, q)
    world.book.note_items(SID, [prep_item()])
    world.book.note(SID, "interrupted")
    note = "Merge watcher: PR #12 (Some change) merged at 19:21Z as abc by someone."
    world.book.note_items(SID, [user_item("i_note", note)])
    await settle(world, q, STEER_GRACE_S + 1)
    failure = q.state(SID)[1]
    assert failure is not None and failure.message == MSG_NOT_CLEAN


async def test_a_failed_write_keeps_the_redirect_for_the_next_tick() -> None:
    world = World()
    store = FailingStore()
    q = world.queue(store)
    await _until_preparing(world, q)
    world.book.note_items(SID, [prep_item()])
    world.book.note(SID, "interrupted")
    world.book.note_items(SID, [user_item()])
    store.armed = True
    await q.tick()
    assert q.state(SID)[0] == "preparing"
    store.armed = False
    await settle(world, q, 1)
    assert q.state(SID) == ("queued", None)
