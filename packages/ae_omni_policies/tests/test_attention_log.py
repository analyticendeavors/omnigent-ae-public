"""The Needs you activity log (2026-11-16, D2 phase 2): first seen, events, Done entries, the
caps, an unreadable file and the privacy of what it holds. The rules for what counts as Done
and the routes are in ``test_attention.py``."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ae_omni_policies import attention_log
from ae_omni_policies.attention_log import LogStore, LogUnreadable

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def new_store(tmp_path: Path) -> LogStore:
    return LogStore(tmp_path / "log.json")


def test_a_personal_git_item_is_keyed_by_login_and_nothing_else_is() -> None:
    key = attention_log.key_for
    assert key("git-mine:pr-ready:a/b#1", "alice") == "alice|git-mine:pr-ready:a/b#1"
    assert key("git:pr-ready:a/b#1", "alice") == "git:pr-ready:a/b#1"
    assert key("box:mount:/:x", "alice") == "box:mount:/:x"


async def test_first_seen_is_recorded_once_and_a_known_item_writes_nothing(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    assert await store.note_seen(["a", "b", "a"], T0) == 2
    first = (tmp_path / "log.json").stat().st_mtime_ns
    assert await store.note_seen(["a", "b"], T0 + timedelta(hours=1)) == 0
    assert (tmp_path / "log.json").stat().st_mtime_ns == first  # no write on a fresh read
    snap = await store.snapshot()
    assert snap.first_seen("a") == "2026-11-03T12:00:00Z"
    assert [e["what"] for e in snap.events("a")] == ["first-seen"]
    # Seen again a day later: last_seen moves, first_seen never does.
    await store.note_seen(["a"], T0 + timedelta(days=1))
    snap = await store.snapshot()
    assert snap.first_seen("a") == "2026-11-03T12:00:00Z"
    assert snap.items["a"]["last_seen"] == "2026-11-04T12:00:00Z"


async def test_events_are_appended_in_order_and_unknown_words_are_dropped(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    await store.add_events(
        [("a", "dismissed", "p"), ("a", "bogus", "p"), ("a", "restored", "p")], T0
    )
    snap = await store.snapshot()
    assert [(e["what"], e["by"]) for e in snap.events("a")] == [
        ("first-seen", None),
        ("dismissed", "p"),
        ("restored", "p"),
    ]
    await store.add_events([], T0)


async def test_events_older_than_thirty_days_go_and_an_open_item_keeps_its_first_seen(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    await store.note_seen(["old", "gone"], T0)
    await store.track(
        T0,
        {"old": {"source": "git", "kind": "k", "title": "t", "scope": "s"}},
        {"s"},
        blocked=lambda _i: False,
        off_sources=set(),
    )
    later = T0 + timedelta(days=45)
    await store.add_events([("new", "dismissed", "p")], later)
    snap = await store.snapshot()
    # "old" is still tracked (open for 45 days), so it keeps its first-seen; "gone" is dropped.
    assert snap.first_seen("old") == "2026-11-03T12:00:00Z"
    assert "gone" not in snap.items and "new" in snap.items
    assert snap.items["old"]["events"] == []  # its own 30-day-old event went


async def test_the_event_cap_drops_the_oldest_first(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    total = attention_log.MAX_EVENTS + 25
    batch = [(f"k{n % 7}", "dismissed", "p") for n in range(total)]
    await store.add_events(batch[:1000], T0)
    await store.add_events(batch[1000:], T0 + timedelta(minutes=5))
    snap = await store.snapshot()
    count = sum(len(r["events"]) for r in snap.items.values())
    assert count == attention_log.MAX_EVENTS
    newest = max(e["at"] for r in snap.items.values() for e in r["events"])
    assert newest == "2026-11-03T12:05:00Z"


async def test_the_done_list_keeps_thirty_days(tmp_path: Path) -> None:
    store = new_store(tmp_path)

    async def finish(item_id: str, at: datetime) -> None:
        item = {item_id: {"source": "git", "kind": "k", "title": "t", "scope": "s"}}
        for step, present in enumerate((item, {}, {})):
            await store.track(
                at + timedelta(minutes=step),
                present,
                {"s"},
                blocked=lambda _i: False,
                off_sources=set(),
            )

    await finish("old", T0)
    await finish("new", T0 + timedelta(days=40))
    assert [d["id"] for d in (await store.snapshot()).done] == ["new"]


async def test_a_done_item_listed_again_gets_one_back_event_and_a_new_item_none(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    item = {"a": {"source": "git", "kind": "k", "title": "t", "scope": "s"}}

    async def step(minutes: int, present: dict) -> None:
        await store.track(
            T0 + timedelta(minutes=minutes),
            present,
            {"s"},
            blocked=lambda _i: False,
            off_sources=set(),
        )

    await store.note_seen(["a"], T0)
    await step(0, item)
    assert [e["what"] for e in (await store.snapshot()).events("a")] == ["first-seen"]
    await step(1, {})
    await step(2, {})
    await step(3, item)
    await step(4, item)
    whats = [e["what"] for e in (await store.snapshot()).events("a")]
    assert whats == ["first-seen", "done", "back"]


async def test_an_unreadable_file_is_unknown_never_empty_and_is_left_alone(
    tmp_path: Path,
) -> None:
    path = tmp_path / "log.json"
    store = new_store(tmp_path)
    for bad in ("{not json", json.dumps({"items": [], "done": [], "tracked": {}}), "[]"):
        path.write_text(bad)
        assert (await store.snapshot()).ok is False
        with pytest.raises(LogUnreadable):
            await store.add_events([("a", "dismissed", "p")], T0)
        with pytest.raises(LogUnreadable):
            await store.note_seen(["a"], T0)
        with pytest.raises(LogUnreadable):
            await store.track(T0, {}, set(), blocked=lambda _i: False, off_sources=set())
        assert path.read_text() == bad
    path.write_text("")
    assert (await store.snapshot()).ok is True
    path.unlink()
    assert (await store.snapshot()).ok is True


async def test_the_file_holds_only_ids_labels_times_and_logins(tmp_path: Path) -> None:
    store = new_store(tmp_path)
    await store.note_seen(["box:mount:/:x"], T0)
    await store.add_events([("box:mount:/:x", "dismissed", "owner@example.com")], T0)
    await store.track(
        T0,
        {
            "box:mount:/:x": {
                "source": "box",
                "kind": "mount",
                "title": "Box health: Storage",
                "scope": "box:all",
            }
        },
        {"box:all"},
        blocked=lambda _i: False,
        off_sources=set(),
    )
    saved = json.loads((tmp_path / "log.json").read_text())
    assert set(saved) == {"version", "items", "done", "tracked"}
    assert set(saved["tracked"]["box:mount:/:x"]) == {
        "source",
        "kind",
        "title",
        "scope",
        "misses",
    }
    assert saved["tracked"]["box:mount:/:x"]["title"] == "Box health: Storage"


async def test_memory_only_without_a_path() -> None:
    store = LogStore(None)
    await store.add_events([("a", "dismissed", "p")], T0)
    assert (await store.snapshot()).events("a")[-1]["what"] == "dismissed"


def test_the_path_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(attention_log.FILE_ENV, str(tmp_path / "x.json"))
    assert attention_log._path_from_env() == tmp_path / "x.json"
    monkeypatch.setenv(attention_log.FILE_ENV, "  ")
    assert attention_log._path_from_env() is None


async def test_first_sight_is_not_an_event_so_new_items_never_push_out_people_s_actions(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    await store.add_events([("keep", "dismissed", "p"), ("keep", "snoozed", "p")], T0)
    # A burst of new items, far more than the event cap, written after the actions.
    await store.note_seen(
        [f"new-{n}" for n in range(attention_log.MAX_EVENTS + 500)], T0 + timedelta(minutes=5)
    )
    snap = await store.snapshot()
    assert [e["what"] for e in snap.items["keep"]["events"]] == ["dismissed", "snoozed"]
    assert all(row["events"] == [] for key, row in snap.items.items() if key.startswith("new-"))
    # The history still leads with the first sight, made up from the row's own first_seen.
    assert [e["what"] for e in snap.events("keep")] == ["first-seen", "dismissed", "snoozed"]
    assert snap.events("new-1")[0]["at"] == "2026-11-03T12:05:00Z"


async def test_an_assignment_is_stored_logged_and_cleared_when_the_item_leaves(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    info = {"source": "git", "kind": "k", "title": "t", "scope": "s"}
    value = await store.set_assigned("a", "bo", "al", T0, info)
    assert value == {"to": "bo", "by": "al", "at": "2026-11-03T12:00:00Z"}
    snap = await store.snapshot()
    assert snap.assigned("a") == value
    assert snap.items["a"]["events"] == [
        {"at": "2026-11-03T12:00:00Z", "what": "assigned", "by": "al", "to": "bo"}
    ]
    assert "a" in snap.tracked  # the cleanup follows it even before it ever listed it
    present = {"a": info}
    for minutes in (5, 10):  # listed on a pass: never cleared
        await store.track(
            T0 + timedelta(minutes=minutes),
            present,
            {"s"},
            blocked=lambda _i: False,
            off_sources=set(),
        )
    assert (await store.snapshot()).assigned("a") == value
    # One clean miss keeps it; the second logs Done and ends the assignment.
    for minutes in (15, 20):
        assert (await store.snapshot()).assigned("a") == value or minutes == 20
        await store.track(
            T0 + timedelta(minutes=minutes),
            {},
            {"s"},
            blocked=lambda _i: False,
            off_sources=set(),
        )
    snap = await store.snapshot()
    assert snap.assigned("a") is None
    assert [e["what"] for e in snap.items["a"]["events"]] == ["assigned", "unassigned", "done"]
    assert snap.items["a"]["events"][1]["by"] == attention_log.SYSTEM


async def test_a_silent_exit_and_a_doubtful_read_treat_an_assignment_correctly(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    info = {"source": "git", "kind": "k", "title": "t", "scope": "s"}
    await store.set_assigned("a", "bo", "al", T0, info)
    await store.set_assigned("b", "bo", "al", T0, info)
    # A scope that did not read clean never ends an assignment, however many passes.
    for n in range(4):
        await store.track(
            T0 + timedelta(minutes=5 * n), {}, set(), blocked=lambda _i: False, off_sources=set()
        )
    snap = await store.snapshot()
    assert snap.assigned("a") and snap.assigned("b")
    # Dismissed by someone (silent exit) still ends it; so does Done.
    for n in range(2):
        await store.track(
            T0 + timedelta(hours=1, minutes=n),
            {},
            {"s"},
            blocked=lambda i: i == "a",
            off_sources=set(),
        )
    snap = await store.snapshot()
    assert snap.assigned("a") is None and snap.assigned("b") is None
    assert [d["id"] for d in snap.done] == ["b"]  # "a" left silently, "b" is Done


async def test_unassigning_an_unassigned_item_logs_nothing_and_an_unreadable_file_refuses(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    assert await store.set_assigned("a", None, "al", T0) is None
    assert (await store.snapshot()).items["a"]["events"] == []
    await store.set_assigned("a", "bo", "al", T0)
    assert await store.set_assigned("a", None, "al", T0 + timedelta(minutes=1)) is None
    snap = await store.snapshot()
    assert [e["what"] for e in snap.items["a"]["events"]] == ["assigned", "unassigned"]
    (tmp_path / "log.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LogUnreadable):
        await store.set_assigned("a", "bo", "al", T0)
    assert (tmp_path / "log.json").read_text(encoding="utf-8") == "{not json"


async def test_an_assigned_row_waits_while_its_card_is_off_and_resumes_clean_pass_tracking(
    tmp_path: Path,
) -> None:
    store = new_store(tmp_path)
    info = {"source": "git", "kind": "k", "title": "t", "scope": "s"}
    await store.set_assigned("a", "bo", "al", T0, info)
    await store.set_assigned("plain", None, "al", T0, {**info})
    await store.track(T0, {"a": info}, {"s"}, blocked=lambda _i: False, off_sources=set())
    # The card is off for many passes: the assignment stays and the miss count does not move.
    for n in range(1, 5):
        await store.track(
            T0 + timedelta(minutes=5 * n), {}, set(), blocked=lambda _i: False, off_sources={"git"}
        )
    snap = await store.snapshot()
    assert snap.assigned("a") is not None and snap.tracked["a"]["misses"] == 0
    # Back on: one clean miss keeps it, the second clears it.
    await store.track(
        T0 + timedelta(hours=1), {}, {"s"}, blocked=lambda _i: False, off_sources=set()
    )
    assert (await store.snapshot()).assigned("a") is not None
    await store.track(
        T0 + timedelta(hours=1, minutes=5), {}, {"s"}, blocked=lambda _i: False, off_sources=set()
    )
    snap = await store.snapshot()
    assert snap.assigned("a") is None
    assert snap.items["a"]["events"][-2]["what"] == "unassigned"
