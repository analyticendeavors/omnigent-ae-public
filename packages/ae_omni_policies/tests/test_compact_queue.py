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
    STABLE_S,
    START_LIMIT_S,
    UNKNOWN_QUIET_S,
    AlreadyQueued,
    CompactQueue,
    CompactSending,
    EdgeBook,
    StateFile,
)
from ae_omni_policies.continuation import COMPACT_PREP_TEXT

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


def error_item(item_id: str = "i_err", at: float = NOW) -> Item:
    return Item(item_id, "error", {"level": "error", "code": "x", "message": "boom"}, at)


@dataclass
class World:
    """The stubs one queue talks to, and the clock."""

    now: float = NOW
    mono: float = 1000.0
    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    post_answer: tuple[int, str] = (202, "")
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


async def test_restart_while_preparing_fails_at_once_and_posts_nothing(tmp_path: Path) -> None:
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
    assert q2.state(SID)[0] is None
    failure = q2.state(SID)[1]
    assert failure is not None and failure.message == MSG_RESTART_PREPARING
    await settle(world2, q2, UNKNOWN_QUIET_S + 5)
    assert world2.posts == []


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
    await settle(world, q, 5)
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
