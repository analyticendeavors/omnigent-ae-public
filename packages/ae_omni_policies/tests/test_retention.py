"""Scheduled-run retention: which run sessions a firing deletes, and which it keeps.

Real stores throughout (SQLite, upstream's fixtures): a scheduled task, its run
rows, and the sessions they point at, labelled the way the box labels them. The
last test fires a real task through upstream's fire path (patch P29's seam) with
an injected launch, so the first firing after deploy clearing an existing
backlog is shown on the path production takes.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from typing import Any

import pytest
from omnigent.entities import ScheduledTask
from omnigent.server.routes._sessions.common import (
    _session_background_task_count_cache,
    _session_status_cache,
)
from omnigent.server.scheduled import fire as fire_mod
from omnigent.server.scheduled import retention as upstream_retention
from omnigent.server.scheduled.fire import FireDeps, build_on_fire

from ae_omni_policies import labels, retention

TASK_NAME = "AE steward"
REFERENCE = {labels.STATUS: labels.REFERENCE}


class World:
    """A task store, a conversation store and helpers to lay down run sessions."""

    def __init__(self, ae_stores: dict[str, Any], db_uri: str) -> None:
        from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
            SqlAlchemyScheduledTaskStore,
        )

        self.conversations = ae_stores["conversation_store"]
        self.tasks = SqlAlchemyScheduledTaskStore(db_uri)
        self.deps = FireDeps(
            scheduled_task_store=self.tasks,
            agent_store=ae_stores["agent_store"],
            conversation_store=self.conversations,
            permission_store=None,
            host_store=None,
            host_registry=None,
            file_store=ae_stores["file_store"],
            artifact_store=ae_stores["artifact_store"],
        )
        self.clock = 1_700_000_000

    def task(self, name: str = TASK_NAME) -> ScheduledTask:
        return self.tasks.create(
            uuid.uuid4().hex,
            name,
            "run the brief",
            "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0",
            None,
            uuid.uuid4().hex,
            "America/Los_Angeles",
        )

    def run(
        self,
        task: ScheduledTask,
        *,
        title: str | None = None,
        session_labels: dict[str, str] | None = None,
        archived: bool = False,
        live_status: str | None = None,
        pending: int = 0,
    ) -> str:
        """One firing's session and run row, newer than every earlier one."""
        conv = self.conversations.create_conversation(
            title=task.name if title is None else title,
            labels=REFERENCE if session_labels is None else session_labels,
        )
        if archived:
            self.conversations.update_conversation(conv.id, archived=True)
        if live_status is not None:
            self.conversations.set_session_live_status(conv.id, live_status)
        if pending:
            self.conversations.set_pending_elicitation_count(conv.id, pending)
        self.clock += 86_400
        self.tasks.create_run(
            uuid.uuid4().hex,
            task.id,
            "succeeded",
            self.clock,
            conversation_id=conv.id,
            fired_at=self.clock,
        )
        return conv.id

    def exists(self, session_id: str) -> bool:
        return self.conversations.get_conversation(session_id) is not None

    async def prune(self, task: ScheduledTask, just_fired: str, keep: int | None) -> list[str]:
        event = upstream_retention.FireEvent(
            task=replace(task, keep_runs=keep), conversation_id=just_fired, deps=self.deps
        )
        return await retention.prune_after_fire(event)


@pytest.fixture()
def world(ae_stores: dict[str, Any], db_uri: str) -> World:
    return World(ae_stores, db_uri)


@pytest.fixture(autouse=True)
def _clean_caches() -> Any:
    yield
    _session_status_cache.clear()
    _session_background_task_count_cache.clear()


def test_registered_with_the_patched_upstream() -> None:
    import ae_omni_policies

    assert ae_omni_policies.RETENTION_REGISTERED is True
    assert retention.prune_after_fire in upstream_retention.registered_fire_observers()


async def test_keeps_the_newest_n_and_deletes_older_idle_reference_runs(world: World) -> None:
    task = world.task()
    old = [world.run(task) for _ in range(4)]
    recent = [world.run(task) for _ in range(4)]
    just_fired = world.run(task, session_labels={})

    deleted = await world.prune(task, just_fired, keep=5)

    assert sorted(deleted) == sorted(old)
    assert not any(world.exists(sid) for sid in old)
    assert all(world.exists(sid) for sid in [*recent, just_fired])


async def test_archived_runs_without_a_status_are_eligible(world: World) -> None:
    task = world.task()
    archived = world.run(task, session_labels={}, archived=True)
    just_fired = world.run(task)

    assert await world.prune(task, just_fired, keep=1) == [archived]


async def test_only_sessions_created_by_the_same_task(world: World) -> None:
    task = world.task()
    other = world.task()
    other_runs = [world.run(other) for _ in range(3)]
    # A session titled like the task but never fired by it.
    lookalike = world.conversations.create_conversation(title=TASK_NAME, labels=REFERENCE).id
    mine = world.run(task)
    just_fired = world.run(task)

    assert await world.prune(task, just_fired, keep=1) == [mine]
    assert all(world.exists(sid) for sid in [*other_runs, lookalike])


async def test_never_the_run_that_just_fired(world: World) -> None:
    task = world.task()
    just_fired = world.run(task)
    # Even when it ranks outside the window (a newer row points elsewhere).
    newer = world.run(task)

    deleted = await world.prune(task, just_fired, keep=1)

    assert deleted == []
    assert world.exists(just_fired) and world.exists(newer)


@pytest.mark.parametrize("live_status", ["running", "waiting", "failed"])
async def test_never_a_session_that_is_not_idle(world: World, live_status: str) -> None:
    task = world.task()
    busy = world.run(task, live_status=live_status)
    just_fired = world.run(task)

    assert await world.prune(task, just_fired, keep=1) == []
    assert world.exists(busy)


async def test_never_a_session_running_in_the_relay_cache_or_a_background_shell(
    world: World,
) -> None:
    task = world.task()
    cached = world.run(task)
    shells = world.run(task)
    just_fired = world.run(task)
    _session_status_cache[cached] = "running"
    _session_background_task_count_cache[shells] = 1

    assert await world.prune(task, just_fired, keep=1) == []


async def test_never_a_session_with_a_running_sub_agent(world: World) -> None:
    task = world.task()
    parent = world.run(task)
    child = world.conversations.create_conversation(
        title="sub", parent_conversation_id=parent, kind="sub_agent"
    )
    _session_status_cache[child.id] = "running"
    just_fired = world.run(task)

    assert await world.prune(task, just_fired, keep=1) == []
    assert world.exists(parent)


async def test_never_a_session_with_an_outstanding_approval(world: World) -> None:
    task = world.task()
    asking = world.run(task, pending=1)
    just_fired = world.run(task)

    assert await world.prune(task, just_fired, keep=1) == []
    assert world.exists(asking)


async def test_never_a_session_with_an_open_gate(world: World) -> None:
    task = world.task()
    gated = world.run(task, session_labels={**REFERENCE, labels.GATE: labels.GATE_OPEN})
    answered = world.run(task, session_labels={**REFERENCE, labels.GATE: labels.GATE_ANSWERED})
    just_fired = world.run(task)

    assert await world.prune(task, just_fired, keep=1) == [answered]
    assert world.exists(gated)


@pytest.mark.parametrize("status", ["working", "blocked", "review", "parked"])
async def test_never_a_session_in_use(world: World, status: str) -> None:
    task = world.task()
    in_use = world.run(task, session_labels={labels.STATUS: status}, archived=True)
    just_fired = world.run(task)

    assert await world.prune(task, just_fired, keep=1) == []
    assert world.exists(in_use)


async def test_never_a_session_neither_reference_nor_archived(world: World) -> None:
    task = world.task()
    unplaced = world.run(task, session_labels={})
    just_fired = world.run(task)

    assert await world.prune(task, just_fired, keep=1) == []
    assert world.exists(unplaced)


async def test_never_a_session_reid_renamed(world: World) -> None:
    task = world.task()
    renamed = world.run(task, title="Steward brief, keep for the Q4 review")
    just_fired = world.run(task)

    assert await world.prune(task, just_fired, keep=1) == []
    assert world.exists(renamed)


async def test_keep_runs_null_keeps_everything(world: World) -> None:
    task = world.task()
    old = [world.run(task) for _ in range(3)]
    just_fired = world.run(task)

    assert await world.prune(task, just_fired, keep=None) == []
    assert all(world.exists(sid) for sid in old)


async def test_kept_window_counts_only_sessions_that_still_exist(world: World) -> None:
    task = world.task()
    oldest = world.run(task)
    gone = world.run(task)
    await world.conversations.delete_conversation(gone)
    just_fired = world.run(task)

    # keep=2: the just-fired run and the oldest survivor fill the window.
    assert await world.prune(task, just_fired, keep=2) == []
    assert world.exists(oldest)


def test_keep_reason_names_each_rule() -> None:
    from omnigent.entities.conversation import Conversation

    def conv(**kw: Any) -> Conversation:
        base: dict[str, Any] = {
            "id": "c1",
            "created_at": 1,
            "updated_at": 1,
            "root_conversation_id": "c1",
            "title": TASK_NAME,
            "labels": dict(REFERENCE),
        }
        base.update(kw)
        return Conversation(**base)

    def why(c: Conversation, live: str = retention.IDLE) -> str | None:
        return retention.keep_reason(c, task_name=TASK_NAME, just_fired="new", live=live)

    assert why(conv()) is None
    assert why(conv(id="new")) == "the run that just fired"
    assert why(conv(parent_conversation_id="p")) == "not a top-level session"
    assert why(conv(), live=retention.RUNNING) == "running"
    assert why(conv(), live=retention.FAILED) == "not idle (failed)"
    assert why(conv(pending_elicitation_count=2)) == "approval outstanding"
    assert why(conv(labels={**REFERENCE, labels.GATE: "open"})) == "open gate"
    assert why(conv(labels={labels.STATUS: "parked"})) == "status parked"
    assert why(conv(labels={})) == "neither reference nor archived"
    assert why(conv(labels={}, archived=True)) is None
    assert why(conv(title="kept")) == "renamed"


async def test_first_fire_after_deploy_clears_the_backlog(world: World) -> None:
    """A real fire through upstream's fire path prunes runs that predate the deploy."""
    from tests.server.scheduled.test_fire import (
        FakeAgentStore,
        FakeHostRegistry,
        FakeHostStore,
        FakePermissionStore,
        _FakeAgent,
        _FakeHost,
    )

    host_id = uuid.uuid4().hex
    task = world.task()
    task = world.tasks.update(task.id, host_id=host_id, workspace="/home/user")
    backlog = [world.run(task) for _ in range(7)]
    renamed = world.run(task, title="Monday brief, keep")

    async def _launch(conv: Any, fired: Any) -> None:
        return None

    agent_store = FakeAgentStore({task.agent_id: _FakeAgent(task.agent_id)})
    # Since v0.18.0 a fire with an artifact store asks the agent store whether it keeps
    # per-user agent copies; upstream's fake predates that and its tests pass no store.
    agent_store.supports_user_agents = False
    deps = replace(
        world.deps,
        agent_store=agent_store,
        permission_store=FakePermissionStore(),
        host_store=FakeHostStore({host_id: _FakeHost(host_id, "local")}),
        host_registry=FakeHostRegistry(online={host_id}),
    )
    on_fire = build_on_fire(deps, launch_dispatch=_launch)
    assert await on_fire(0, task.id)
    for _ in range(200):
        pending = [t for t in fire_mod._PENDING_FIRES if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)

    fired = world.tasks.get(task.id).last_run_conversation_id
    assert fired is not None and world.exists(fired)
    # Default keep_runs (5): the new run, the renamed one and the three newest
    # backlog runs fill the window; the four older backlog runs go.
    survivors = [sid for sid in backlog if world.exists(sid)]
    assert survivors == backlog[4:]
    assert world.exists(renamed)
