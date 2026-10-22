"""Tests for :mod:`ae_omni_policies.auto_resume`.

Real conversation store (SQLite, upstream's fixtures); the relaunch itself is
replaced by a recorder, since a real one needs a live host.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from omnigent.server.host_connect_observers import (
    HostConnectEvent,
    registered_host_connect_observers,
)
from omnigent.server.routes._sessions.common import (
    _LAST_TASK_ERROR_CODE_LABEL_KEY,
    _LAST_TASK_ERROR_MESSAGE_LABEL_KEY,
    _session_status_cache,
)

from ae_omni_policies import auto_resume, labels

HOST = "a" * 32
OTHER_HOST = "b" * 32
AGENT_ID = uuid.uuid4().hex
DISCONNECTED = {
    _LAST_TASK_ERROR_CODE_LABEL_KEY: "runner_disconnected",
    _LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "Runner disconnected unexpectedly.",
}


class World:
    def __init__(self, ae_stores: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        self.store = ae_stores["conversation_store"]
        self.resumed: list[str] = []

        async def _resume(app_state: Any, store: Any, session_id: str) -> dict[str, Any]:
            assert store is self.store
            self.resumed.append(session_id)
            return {"recovered": True}

        monkeypatch.setattr(auto_resume, "resume_interrupted_session", _resume)

        self.connected: set[str] = set()

        async def _alive(conv: Any, event: Any) -> bool:
            return conv.id in self.connected

        monkeypatch.setattr(auto_resume, "runner_alive", _alive)

    def session(
        self,
        *,
        host_id: str = HOST,
        runner_id: str = "runner_old",
        status: str = "failed",
        session_labels: dict[str, str] | None = None,
        parent: str | None = None,
        archived: bool = False,
    ) -> str:
        conv = self.store.create_conversation(
            title="work",
            agent_id=AGENT_ID,
            runner_id=runner_id,
            host_id=host_id,
            workspace="/home/reid/repos/x",
            parent_conversation_id=parent,
            labels=DISCONNECTED if session_labels is None else session_labels,
        )
        self.store.set_session_live_status(conv.id, status)
        _session_status_cache.pop(conv.id, None)
        if archived:
            self.store.update_conversation(conv.id, archived=True)
        return conv.id

    async def connect(self, host_id: str = HOST) -> list[str]:
        event = HostConnectEvent(
            host_id=host_id, owner=None, app_state=object(), conversation_store=self.store
        )
        return await auto_resume.resume_after_host_connect(event)


@pytest.fixture()
def world(ae_stores: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.delenv(auto_resume.ENABLED_ENV, raising=False)
    return World(ae_stores, monkeypatch)


def test_registered_with_the_host_connect_seam() -> None:
    assert auto_resume.REGISTERED
    assert auto_resume.resume_after_host_connect in registered_host_connect_observers()


@pytest.mark.asyncio
async def test_resumes_an_interrupted_session_once_per_runner(world: World) -> None:
    interrupted = world.session()
    mid_turn = world.session(status="running", session_labels={})

    assert sorted(await world.connect()) == sorted([interrupted, mid_turn])
    assert sorted(world.resumed) == sorted([interrupted, mid_turn])
    conv = world.store.get_conversation(interrupted)
    assert conv.labels[labels.AUTORESUME] == "runner_old"

    # A second connect before anything changed does not try again.
    world.resumed.clear()
    assert await world.connect() == []
    # A new runner that dies later gets its own try.
    world.store.replace_runner_id(interrupted, "runner_new")
    assert await world.connect() == [interrupted]


@pytest.mark.asyncio
async def test_leaves_everything_else_alone(world: World) -> None:
    world.session(host_id=OTHER_HOST)
    world.session(status="idle", session_labels={})
    world.session(session_labels={_LAST_TASK_ERROR_CODE_LABEL_KEY: "response_failed"})
    world.session(archived=True)
    parent = world.session(status="idle", session_labels={})
    world.session(parent=parent)

    assert await world.connect() == []
    assert world.resumed == []


@pytest.mark.asyncio
async def test_skips_a_session_untouched_for_six_hours(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.session()
    later = auto_resume.time.time() + auto_resume.MAX_AGE_S + 60
    monkeypatch.setattr(auto_resume.time, "time", lambda: later)
    assert await world.connect() == []


@pytest.mark.asyncio
async def test_off_switch(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.session()
    monkeypatch.setenv(auto_resume.ENABLED_ENV, "0")
    assert await world.connect() == []
    assert world.resumed == []


@pytest.mark.asyncio
async def test_a_failed_resume_is_logged_and_the_next_session_still_tried(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = world.session()
    second = world.session()
    seen: list[str] = []

    async def _flaky(app_state: Any, store: Any, session_id: str) -> dict[str, Any]:
        seen.append(session_id)
        if len(seen) == 1:
            raise RuntimeError("no runner")
        return {"recovered": True}

    monkeypatch.setattr(auto_resume, "resume_interrupted_session", _flaky)
    assert sorted(await world.connect()) == sorted([first, second])
    assert sorted(seen) == sorted([first, second])
    # The failed one is marked too, so it is not retried on every reconnect.
    assert await world.connect() == []


def test_enabled_parses_the_switch() -> None:
    assert auto_resume.enabled({})
    assert auto_resume.enabled({auto_resume.ENABLED_ENV: "1"})
    for off in ("0", "false", "OFF", "no"):
        assert not auto_resume.enabled({auto_resume.ENABLED_ENV: off})


@pytest.mark.asyncio
async def test_a_connected_runner_is_left_alone_and_logged(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    sid = world.session()
    world.connected.add(sid)
    with caplog.at_level(logging.INFO, logger=auto_resume.__name__):
        assert await world.connect() == []
    assert world.resumed == []
    assert labels.AUTORESUME not in (world.store.get_conversation(sid).labels or {})
    lines = [r.getMessage() for r in caplog.records if "skipped" in r.getMessage()]
    assert lines == [f"auto-resume: session {sid} on host {HOST} skipped: runner still alive"]


@pytest.mark.asyncio
async def test_the_deploy_sequence_resumes_on_the_restart_that_kills_the_runner(
    world: World,
) -> None:
    sid = world.session()
    # The old host reconnects to the new server with its runner alive.
    world.connected.add(sid)
    assert await world.connect() == []
    # The host rebuild kills the runner; the new host connects with none.
    world.connected.clear()
    assert await world.connect() == [sid]
    assert world.resumed == [sid]
    assert world.store.get_conversation(sid).labels[labels.AUTORESUME] == "runner_old"


class Fakes:
    """Replaces the upstream seams the real ``runner_alive`` reaches."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import omnigent.server.routes.sessions as facade
        import omnigent.server.routes.sessions.routes_events as routes_events

        self.router: Any = object()
        self.client: Any = None
        self.host_says: Any = None
        self.host_calls: list[str] = []
        self.explode = False

        async def _client(session_id: str, router: Any, **kwargs: Any) -> Any:
            assert "conversation" not in kwargs, "the router must read the current row"
            if self.explode:
                raise RuntimeError("router broke")
            return self.client

        async def _status(conn: Any, registry: Any, runner_id: str) -> Any:
            self.host_calls.append(runner_id)
            return self.host_says

        monkeypatch.setattr(routes_events, "get_server_runner_router", lambda: self.router)
        monkeypatch.setattr(facade, "_get_runner_client", _client)
        monkeypatch.setattr(facade, "_query_host_runner_status", _status)


class FakeRegistry:
    def get(self, host_id: str) -> Any:
        return object() if host_id == HOST else None


def _real_event(store: Any) -> HostConnectEvent:
    app_state = type("State", (), {"host_registry": FakeRegistry()})()
    return HostConnectEvent(
        host_id=HOST, owner=None, app_state=app_state, conversation_store=store
    )


@pytest.fixture()
def real_alive(ae_stores: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real ``runner_alive`` over fakes; returns (world, fakes, check)."""
    real = auto_resume.runner_alive
    world = World(ae_stores, monkeypatch)
    monkeypatch.setattr(auto_resume, "runner_alive", real)
    monkeypatch.delenv(auto_resume.ENABLED_ENV, raising=False)
    monkeypatch.setattr(auto_resume, "_check_failure_logged", False)
    fakes = Fakes(monkeypatch)

    async def check(sid: str) -> bool:
        conv = world.store.get_conversation(sid)
        return await auto_resume.runner_alive(conv, _real_event(world.store))

    return world, fakes, check


@pytest.mark.asyncio
async def test_alive_when_the_router_has_the_runner(real_alive: Any) -> None:
    world, fakes, check = real_alive
    fakes.client = object()
    assert await check(world.session())
    assert fakes.host_calls == []


@pytest.mark.asyncio
async def test_alive_when_the_host_says_so_and_the_router_is_missing(real_alive: Any) -> None:
    world, fakes, check = real_alive
    fakes.router = None
    fakes.host_says = "alive"
    assert await check(world.session())
    # A router that does not have the runner falls through to the host as well.
    fakes.router = object()
    assert await check(world.session())


@pytest.mark.asyncio
@pytest.mark.parametrize("says", ["dead", "unknown", None])
async def test_not_alive_when_the_host_does_not_say_alive(real_alive: Any, says: Any) -> None:
    world, fakes, check = real_alive
    fakes.host_says = says
    assert not await check(world.session())
    assert len(fakes.host_calls) == 1


@pytest.mark.asyncio
async def test_a_failing_check_warns_once_and_resumes_both(
    real_alive: Any, caplog: pytest.LogCaptureFixture
) -> None:
    world, fakes, _ = real_alive
    fakes.explode = True
    first, second = world.session(), world.session()
    with caplog.at_level(logging.WARNING, logger=auto_resume.__name__):
        assert sorted(await world.connect()) == sorted([first, second])
    assert sorted(world.resumed) == sorted([first, second])
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "router broke" in warnings[0].getMessage()
