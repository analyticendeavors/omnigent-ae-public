"""The auto-archive sweep (2026-10-24): when it archives, when it must not."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from omnigent.server.routes._sessions.common import (
    _LAST_TASK_ERROR_CODE_LABEL_KEY,
    _LAST_TASK_ERROR_MESSAGE_LABEL_KEY,
)

from ae_omni_policies import auto_archive, auto_resume, labels
from ae_omni_policies.auto_archive import StateFile, Sweeper

WAIT = 600.0


@dataclass
class Conv:
    id: str
    labels: dict[str, str] = field(default_factory=lambda: {labels.STATUS: labels.REFERENCE})
    parent_conversation_id: str | None = None
    kind: str = "default"
    archived: bool = False
    pending_elicitation_count: int = 0
    title: str = "done chat"
    updated_at: int = 0
    live_status: str | None = None


class Rig:
    def __init__(self, tmp_path: Path | None = None, path: Path | None = None) -> None:
        self.convs: dict[str, Conv] = {}
        self.live: dict[str, str] = {}
        self.archived: list[str] = []
        self.now = 1000.0
        self.accept = True
        self.shared: set[str] = set()
        self.state = StateFile(path)
        self.sweeper = Sweeper(
            store=self,
            archive=self._archive,
            state=self.state,
            wait_s=WAIT,
            list_rows=self._rows,
            live=self._live,
            clock=lambda: self.now,
            shared=lambda session_id: session_id in self.shared,
        )

    def add(self, conv: Conv) -> Conv:
        self.convs[conv.id] = conv
        return conv

    def _rows(self) -> list[Conv]:
        # What the session list returns: top-level, non-archived rows.
        return [
            c for c in self.convs.values() if not c.archived and c.parent_conversation_id is None
        ]

    def get_conversation(self, session_id: str) -> Conv | None:
        return self.convs.get(session_id)

    def list_conversations(
        self, *, parent_conversation_id: str | None = None, kind: str | None = None, **_kw: Any
    ) -> Any:
        data = [
            c
            for c in self.convs.values()
            if not c.archived
            and (
                c.parent_conversation_id == parent_conversation_id
                if parent_conversation_id is not None
                else c.kind == kind
            )
        ]
        return SimpleNamespace(data=data, has_more=False, last_id=None)

    async def _live(self, _store: Any, conv: Conv) -> str:
        return self.live.get(conv.id, "idle")

    async def _archive(self, session_id: str) -> bool:
        if not self.accept:
            return False
        self.convs[session_id].archived = True
        self.archived.append(session_id)
        return True

    async def sweep(self, advance: float = 0.0) -> list[str]:
        self.now += advance
        return await self.sweeper.sweep()


async def test_archives_after_the_wait_and_not_before() -> None:
    rig = Rig()
    rig.add(Conv("a"))
    assert await rig.sweep() == []  # first sight starts the clock
    assert await rig.sweep(WAIT - 1) == []
    assert await rig.sweep(1) == ["a"]
    assert rig.convs["a"].archived
    assert "a" not in rig.sweeper.since
    assert await rig.sweep(60) == []  # archived: gone from the list, never archived twice


async def test_a_flip_out_of_reference_resets_the_clock() -> None:
    rig = Rig()
    conv = rig.add(Conv("a"))
    await rig.sweep()
    await rig.sweep(WAIT - 60)
    conv.labels = {labels.STATUS: labels.WORKING}
    await rig.sweep(30)
    assert "a" not in rig.sweeper.since
    conv.labels = {labels.STATUS: labels.REFERENCE}
    await rig.sweep(30)
    assert await rig.sweep(WAIT - 1) == []
    assert await rig.sweep(1) == ["a"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rig, c: rig.live.__setitem__(c.id, "running"),
        lambda rig, c: setattr(c, "pending_elicitation_count", 1),
        lambda rig, c: c.labels.__setitem__(labels.GATE, labels.GATE_OPEN),
        lambda rig, c: setattr(c, "parent_conversation_id", "parent"),
        lambda rig, c: setattr(c, "kind", "task"),
        lambda rig, c: setattr(c, "archived", True),
    ],
    ids=["running", "approval", "gate", "child", "other-kind", "archived"],
)
async def test_what_it_leaves_alone(mutate: Any) -> None:
    rig = Rig()
    conv = rig.add(Conv("a"))
    mutate(rig, conv)
    await rig.sweep()
    assert await rig.sweep(WAIT * 5) == []
    assert rig.archived == []


async def test_a_session_reopened_after_the_list_was_read_is_left_alone() -> None:
    rig = Rig()
    conv = rig.add(Conv("a"))
    await rig.sweep()
    rows = rig._rows

    def stale_list() -> list[Conv]:
        listed = rows()
        conv.labels = {labels.STATUS: labels.WORKING}  # reopened between list and archive
        return listed

    rig.sweeper._list_rows = stale_list
    assert await rig.sweep(WAIT) == []
    assert rig.archived == []


async def test_a_refused_archive_is_retried_next_sweep() -> None:
    rig = Rig()
    rig.add(Conv("a"))
    await rig.sweep()
    rig.accept = False
    assert await rig.sweep(WAIT) == []
    rig.accept = True
    assert await rig.sweep(60) == ["a"]


async def test_one_failing_session_does_not_stop_the_sweep() -> None:
    rig = Rig()
    rig.add(Conv("bad"))
    rig.add(Conv("good"))
    await rig.sweep()
    original = rig._archive

    async def archive(session_id: str) -> bool:
        if session_id == "bad":
            raise RuntimeError("boom")
        return await original(session_id)

    rig.sweeper._archive = archive
    assert await rig.sweep(WAIT) == ["good"]


async def test_the_state_file_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    rig = Rig(path=path)
    rig.add(Conv("a"))
    await rig.sweep()
    assert json.loads(path.read_text())["since"] == {"a": 1000.0}
    # A restart keeps the clock: the next sweep archives without waiting again.
    again = Rig(path=path)
    again.add(Conv("a"))
    again.now = 1000.0 + WAIT
    assert await again.sweeper.sweep() == ["a"]
    assert json.loads(path.read_text())["since"] == {}
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_a_corrupt_or_unwritable_state_file_never_crashes(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert StateFile(path).load() == {}
    path.write_text(json.dumps({"since": {"a": "x", "b": 5, "c": True}}))
    assert StateFile(path).load() == {"b": 5.0}
    blocked = tmp_path / "file"
    blocked.write_text("x")
    state = StateFile(blocked / "state.json")  # parent is a file: mkdir fails
    state.save({"a": 1.0})
    assert state._memory == {"a": 1.0}


def test_state_file_rejects_absurd_numbers(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        '{"since": {"a": 5, "inf": Infinity, "nan": NaN, "neg": -1, "big": 1e400, '
        '"huge": 99999999999999999999999999999999999999999999999999999999, "ok": 7.5}}'
    )
    assert StateFile(path).load() == {"a": 5.0, "ok": 7.5}


def test_memory_only_without_a_path() -> None:
    state = StateFile(None)
    assert state.load() == {}
    state.save({"a": 2.0})
    assert state.load() == {"a": 2.0}


async def test_a_clock_that_ran_backwards_restarts_the_wait() -> None:
    rig = Rig()
    rig.add(Conv("a"))
    rig.sweeper._since = {"a": 10**9}  # a "first seen" in the future
    assert await rig.sweep() == []
    assert rig.sweeper.since["a"] == rig.now


def test_settings_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert auto_archive.enabled({}) is True
    for off in ("0", "false", "OFF", "no"):
        assert auto_archive.enabled({"AE_AUTO_ARCHIVE": off}) is False
    assert auto_archive.wait_seconds({}) == 600.0
    assert auto_archive.wait_seconds({"AE_AUTO_ARCHIVE_MINUTES": "2"}) == 120.0
    assert auto_archive.wait_seconds({"AE_AUTO_ARCHIVE_MINUTES": "junk"}) == 600.0
    assert auto_archive.wait_seconds({"AE_AUTO_ARCHIVE_MINUTES": "-3"}) == 600.0
    assert auto_archive.state_path({"AE_AUTO_ARCHIVE_FILE": "/x/y.json"}) == Path("/x/y.json")
    monkeypatch.setattr(auto_archive, "DATA_DIR", tmp_path)
    assert auto_archive.state_path({}) == tmp_path / "ae-auto-archive.json"
    monkeypatch.setattr(auto_archive, "DATA_DIR", tmp_path / "missing")
    assert auto_archive.state_path({}) is None


async def test_off_switch_starts_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    monkeypatch.setenv("AE_AUTO_ARCHIVE", "0")
    assert auto_archive.start(FastAPI()) is None


async def test_mounted_in_debug_routers_and_lifespan_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    from ae_omni_policies.router import DEBUG_ROUTERS

    assert any(entry[0] is auto_archive.lifespan_router for entry in DEBUG_ROUTERS)
    events: list[str] = []

    async def run(self: Sweeper) -> None:
        events.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("stopped")

    monkeypatch.setattr(Sweeper, "run", run)
    monkeypatch.setattr(auto_archive, "state_path", lambda: None)
    monkeypatch.setenv("AE_AUTO_ARCHIVE", "1")
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: object())
    app = FastAPI()
    app.include_router(auto_archive.lifespan_router)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert events == ["started"]
    assert events == ["started", "stopped"]


async def test_a_resumed_reference_chat_waits_again_from_its_last_activity() -> None:
    rig = Rig()
    conv = rig.add(Conv("a"))
    await rig.sweep()
    await rig.sweep(WAIT - 60)
    conv.updated_at = int(rig.now)  # a follow-up turn just ended
    assert await rig.sweep(60) == []
    assert await rig.sweep(WAIT - 61) == []
    assert await rig.sweep(61) == ["a"]


async def test_a_busy_sweep_restarts_the_clock() -> None:
    rig = Rig()
    rig.add(Conv("a"))
    await rig.sweep()
    rig.live["a"] = "running"
    await rig.sweep(WAIT - 1)
    rig.live["a"] = "idle"
    assert await rig.sweep(WAIT - 1) == []
    assert await rig.sweep(1) == ["a"]


async def test_a_shared_session_is_never_archived() -> None:
    rig = Rig()
    rig.add(Conv("a"))
    rig.shared.add("a")
    await rig.sweep()
    assert await rig.sweep(WAIT * 3) == []
    rig.shared.clear()
    assert await rig.sweep(60) == ["a"]


def test_has_other_grants() -> None:
    grants = [SimpleNamespace(user_id="owner")]

    class Perms:
        def list_for_session(self, _id: str, limit: int = 0) -> tuple[list[Any], None]:
            return grants, None

    identity = SimpleNamespace(_permission_store=lambda: Perms(), owner=lambda _id: "owner")
    assert auto_archive.has_other_grants(identity, "s") is False  # type: ignore[arg-type]
    grants.append(SimpleNamespace(user_id="editor"))
    assert auto_archive.has_other_grants(identity, "s") is True  # type: ignore[arg-type]
    none = SimpleNamespace(_permission_store=lambda: None, owner=lambda _id: None)
    assert auto_archive.has_other_grants(none, "s") is False  # type: ignore[arg-type]

    def boom() -> Any:
        raise RuntimeError("db down")

    broken = SimpleNamespace(_permission_store=boom, owner=lambda _id: None)
    assert auto_archive.has_other_grants(broken, "s") is True  # type: ignore[arg-type]


async def test_start_never_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI

    def broken() -> Any:
        raise RuntimeError("no store")

    monkeypatch.setenv("AE_AUTO_ARCHIVE", "1")
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", broken)
    assert auto_archive.start(FastAPI()) is None


async def test_an_unarchived_session_is_not_archived_again_until_it_leaves_reference() -> None:
    rig = Rig()
    conv = rig.add(Conv("a"))
    await rig.sweep()
    assert await rig.sweep(WAIT) == ["a"]
    assert rig.sweeper.archived_by_sweep == {"a"}
    conv.archived = False  # the owner unarchives it; still in reference
    assert await rig.sweep(WAIT * 3) == []
    assert rig.sweeper.archived_by_sweep == {"a"}
    conv.labels = {labels.STATUS: labels.WORKING}
    await rig.sweep(60)
    assert rig.sweeper.archived_by_sweep == set()
    conv.labels = {labels.STATUS: labels.REFERENCE}
    await rig.sweep(60)
    assert await rig.sweep(WAIT) == ["a"]


async def test_the_unarchive_record_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    rig = Rig(path=path)
    rig.add(Conv("a"))
    await rig.sweep()
    await rig.sweep(WAIT)
    assert json.loads(path.read_text())["archived_by_sweep"] == ["a"]
    assert StateFile(path).load_archived() == {"a"}


class PagedStore:
    """A store whose list API pages like upstream's (cursor = last id)."""

    def __init__(self, count: int) -> None:
        self.rows = [Conv(f"c{i:04d}") for i in range(count)]

    def list_conversations(self, *, limit: int, after: str | None, **_kw: Any) -> Any:
        start = 0 if after is None else int(after[1:]) + 1
        data = self.rows[start : start + limit]
        more = start + limit < len(self.rows)
        return SimpleNamespace(data=data, has_more=more, last_id=data[-1].id if data else None)

    def get_conversation(self, session_id: str) -> Conv | None:
        return next((c for c in self.rows if c.id == session_id), None)


async def test_every_page_is_read_and_no_clock_drops_for_paging() -> None:
    store = PagedStore(450)
    assert len(auto_archive.list_all_top_level(store)) == 450
    now = 1000.0
    sweeper = Sweeper(
        store=store,
        archive=lambda _id: asyncio.sleep(0, result=True),
        state=StateFile(None),
        wait_s=WAIT,
        live=lambda _s, _c: asyncio.sleep(0, result="idle"),
        clock=lambda: now,
    )
    await sweeper.sweep()
    assert len(sweeper.since) == 450
    store.rows = store.rows[:300]  # 150 rows archived or deleted elsewhere
    await sweeper.sweep()
    assert len(sweeper.since) == 300


async def test_a_session_missing_from_the_list_keeps_its_clock_until_the_store_says_it_left() -> (
    None
):
    rig = Rig()
    conv = rig.add(Conv("a"))
    await rig.sweep()
    rig.sweeper._list_rows = list  # not in this read, but still in reference
    await rig.sweep(60)
    assert "a" in rig.sweeper.since
    conv.labels = {labels.STATUS: labels.WORKING}
    await rig.sweep(60)
    assert "a" not in rig.sweeper.since


async def test_a_permanent_failure_stops_after_three_tries_and_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    conv = rig.add(Conv("a"))
    rig.accept = False
    await rig.sweep()
    with caplog.at_level(logging.WARNING, logger=auto_archive.__name__):
        for _ in range(6):
            assert await rig.sweep(WAIT) == []
    gave_up = [r for r in caplog.records if "gave up" in r.getMessage()]
    assert len(gave_up) == 1
    assert "archive it by hand" in gave_up[0].getMessage()
    attempts = rig.sweeper._failures["a"]
    assert attempts == 3
    rig.accept = True
    assert await rig.sweep(60) == []  # still given up
    conv.labels = {labels.STATUS: labels.WORKING}
    await rig.sweep(60)
    conv.labels = {labels.STATUS: labels.REFERENCE}
    await rig.sweep(60)
    assert await rig.sweep(WAIT) == ["a"]


def test_only_one_process_holds_the_sweep_lock(tmp_path: Path) -> None:
    first = auto_archive.SweepLock.beside(tmp_path / "state.json")
    second = auto_archive.SweepLock.beside(tmp_path / "state.json")
    assert first.path == tmp_path / "state.json.lock"
    assert first.acquire() is True
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()
    assert auto_archive.SweepLock.beside(None).path == auto_archive._LOCK_FALLBACK


async def test_a_sweeper_without_the_lock_does_not_sweep(tmp_path: Path) -> None:
    rig = Rig()
    rig.add(Conv("a"))
    held = auto_archive.SweepLock.beside(tmp_path / "s.json")
    assert held.acquire()
    rig.sweeper._lock = auto_archive.SweepLock.beside(tmp_path / "s.json")
    assert await rig.sweep(WAIT * 3) == []
    assert rig.sweeper.since == {}
    held.release()
    await rig.sweep()
    assert "a" in rig.sweeper.since
    rig.sweeper._lock.release()


def test_an_empty_state_file_setting_means_memory_only() -> None:
    assert auto_archive.state_path({"AE_AUTO_ARCHIVE_FILE": ""}) is None
    assert auto_archive.state_path({"AE_AUTO_ARCHIVE_FILE": "  "}) is None


class FakeIdentity:
    def __init__(self, headers: dict[str, str] | None) -> None:
        self._headers = headers

    async def headers(self, _session_id: str) -> dict[str, str] | None:
        return self._headers


async def test_archive_live_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    from fastapi import FastAPI

    app = FastAPI()
    assert await auto_archive.archive_live(app, FakeIdentity(None), "s") is False  # type: ignore[arg-type]

    outcome: dict[str, Any] = {}

    async def call(*_a: Any, **_kw: Any) -> Any:
        result = outcome["value"]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(auto_archive.merge_watch, "_call", call)
    identity = FakeIdentity({"authorization": "x"})
    outcome["value"] = SimpleNamespace(status_code=200, text="")
    assert await auto_archive.archive_live(app, identity, "s") is True  # type: ignore[arg-type]
    outcome["value"] = SimpleNamespace(status_code=403, text="owner only")
    assert await auto_archive.archive_live(app, identity, "s") is False  # type: ignore[arg-type]
    outcome["value"] = httpx.ConnectError("down")
    assert await auto_archive.archive_live(app, identity, "s") is False  # type: ignore[arg-type]


async def test_an_archived_leads_idle_children_archive_with_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = Rig()
    rig.add(Conv("lead"))
    working = {labels.STATUS: labels.WORKING}
    rig.add(Conv("member", {**working, labels.ORCH: "lead"}, title="helper"))
    rig.add(Conv("grandmember", {**working, labels.ORCH: "member"}))
    rig.add(Conv("codex", {}, parent_conversation_id="lead", kind="sub_agent", title="Review 1"))
    rig.add(Conv("busy", {}, parent_conversation_id="lead", kind="sub_agent"))
    rig.add(Conv("grandchild", {}, parent_conversation_id="codex", kind="sub_agent"))
    rig.add(Conv("other", working))
    rig.live["busy"] = "running"
    caplog.set_level(logging.INFO, logger=auto_archive.__name__)
    await rig.sweep()
    assert await rig.sweep(WAIT) == ["lead"]
    assert set(rig.archived) == {"lead", "member", "grandmember", "codex", "grandchild"}
    assert not rig.convs["busy"].archived and not rig.convs["other"].archived
    assert "archived child member 'helper' with its lead lead" in caplog.text
    # The running child is picked up once it goes idle; the lead stays watched until then.
    rig.live["busy"] = "idle"
    await rig.sweep(60)
    assert rig.convs["busy"].archived


async def test_a_failed_reference_session_is_archived_after_the_clock() -> None:
    rig = Rig()
    rig.add(Conv("a"))
    rig.live["a"] = "failed"
    assert await rig.sweep() == []
    assert await rig.sweep(WAIT - 1) == []
    assert await rig.sweep(1) == ["a"]
    assert rig.convs["a"].archived


async def test_a_failed_child_of_an_archived_lead_archives_with_it() -> None:
    rig = Rig()
    rig.add(Conv("lead"))
    rig.add(Conv("dead", {labels.STATUS: labels.WORKING, labels.ORCH: "lead"}))
    rig.add(Conv("busy", {labels.STATUS: labels.WORKING, labels.ORCH: "lead"}))
    rig.live["dead"] = "failed"
    rig.live["busy"] = "running"
    await rig.sweep()
    assert await rig.sweep(WAIT) == ["lead"]
    assert rig.convs["dead"].archived
    assert not rig.convs["busy"].archived


async def test_children_of_a_lead_archived_by_hand_archive_too() -> None:
    rig = Rig()
    rig.add(Conv("lead", archived=True))
    rig.add(Conv("member", {labels.STATUS: labels.WORKING, labels.ORCH: "lead"}))
    rig.add(Conv("kid", {}, parent_conversation_id="lead", kind="sub_agent"))
    assert await rig.sweep() == []
    assert rig.archived == ["member", "kid"]


async def test_a_lead_archived_by_hand_is_found_through_a_plain_child() -> None:
    rig = Rig()
    rig.add(Conv("lead", archived=True))
    rig.add(Conv("codex", {}, parent_conversation_id="lead", kind="sub_agent"))
    rig.add(Conv("codex2", {}, parent_conversation_id="lead", kind="sub_agent"))
    rig.add(Conv("grand", {}, parent_conversation_id="codex", kind="sub_agent"))
    rig.add(Conv("busy", {}, parent_conversation_id="lead", kind="sub_agent"))
    rig.add(Conv("live", {}, parent_conversation_id="live-lead", kind="sub_agent"))
    rig.add(Conv("live-lead", {labels.STATUS: labels.WORKING}))
    rig.live["busy"] = "running"
    lookups: list[str] = []
    real = rig.get_conversation
    rig.get_conversation = lambda sid: (lookups.append(sid), real(sid))[1]  # type: ignore[method-assign]
    assert await rig.sweep() == []
    assert set(rig.archived) == {"codex", "codex2", "grand"}
    assert not rig.convs["live"].archived and not rig.convs["busy"].archived
    # Each distinct parent is looked up once in the plain-child pass.
    assert lookups.count("lead") <= 2


async def test_a_lead_archived_by_hand_is_found_through_a_failed_plain_child() -> None:
    rig = Rig()
    rig.add(Conv("lead", {labels.STATUS: labels.WORKING}))
    rig.add(
        Conv("dead", {}, parent_conversation_id="lead", kind="sub_agent", live_status="failed")
    )
    rig.convs["lead"].archived = True
    rig.live["dead"] = "failed"  # agrees with Conv.live_status above
    assert await rig.sweep() == []
    assert rig.archived == ["dead"]


async def test_running_and_waiting_plain_children_are_not_archived() -> None:
    rig = Rig()
    rig.add(Conv("lead", archived=True))
    for name in ("run", "wait"):
        rig.add(Conv(name, {}, parent_conversation_id="lead", kind="sub_agent", live_status=name))
    rig.live["run"] = "running"
    rig.live["wait"] = "running"  # live_state reads waiting as running
    await rig.sweep()
    assert rig.archived == []


def _failed(code: str) -> dict[str, str]:
    return {
        _LAST_TASK_ERROR_CODE_LABEL_KEY: code,
        _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "boom",
    }


@pytest.mark.parametrize("code", ["runner_disconnected", "runner_failed_to_start"])
async def test_a_resumable_failure_waits_in_both_paths_until_past_the_window(code: str) -> None:
    rig = Rig()
    ref = rig.add(Conv("ref", live_status="failed"))
    ref.labels.update(_failed(code))
    rig.add(Conv("lead"))
    kid = rig.add(Conv("kid", {labels.STATUS: labels.WORKING, labels.ORCH: "lead"}))
    kid.live_status = "failed"
    kid.labels.update(_failed(code))
    rig.live["ref"] = rig.live["kid"] = "failed"
    rig.convs["lead"].live_status = "idle"
    rig.convs["lead"].archived = True
    ref.updated_at = kid.updated_at = int(rig.now)  # touched just now
    await rig.sweep()
    assert await rig.sweep(WAIT * 2) == []
    assert rig.archived == []
    # Past auto_resume.MAX_AGE_S nothing would revive them any more.
    await rig.sweep(auto_resume.MAX_AGE_S)
    assert set(rig.archived) == {"ref", "kid"}


async def test_a_failure_with_another_error_code_is_archived() -> None:
    rig = Rig()
    conv = rig.add(Conv("a", live_status="failed"))
    conv.labels.update(_failed("model_error"))
    rig.live["a"] = "failed"
    conv.updated_at = int(rig.now)
    await rig.sweep()
    assert await rig.sweep(WAIT) == ["a"]


async def test_the_walk_goes_down_through_a_busy_or_archived_child() -> None:
    rig = Rig()
    rig.add(Conv("lead", archived=True))
    rig.add(Conv("mid", {}, parent_conversation_id="lead", kind="sub_agent"))
    rig.add(Conv("under-mid", {}, parent_conversation_id="mid", kind="sub_agent"))
    rig.add(Conv("mid2", {}, parent_conversation_id="lead", kind="sub_agent", archived=True))
    rig.add(Conv("under-mid2", {}, parent_conversation_id="mid2", kind="sub_agent"))
    rig.add(Conv("member", {labels.STATUS: labels.WORKING, labels.ORCH: "lead"}))
    rig.live["mid"] = "running"
    await rig.sweep()
    assert set(rig.archived) == {"under-mid", "under-mid2", "member"}
    assert not rig.convs["mid"].archived
