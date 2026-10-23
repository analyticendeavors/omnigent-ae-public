"""The two scheduled tasks: seeded by name, listed by the API, fired by hand.

The app here carries a scheduled-task store and enters its lifespan (the
scheduler and the run-now trigger live on ``app.state``), with the two agents
registered through ``OMNIGENT_BUILTIN_AGENT_DIRS`` exactly as the deployment
registers them. No host is connected, so a manual run records the failed run
upstream promises for that case; a real fire needs the box host.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from omnigent.server import app as server_app
from omnigent.server.scheduled import fire

from ae_omni_policies import schedules
from ae_omni_policies.omni import Settings, http_client

AGENTS = Path(__file__).resolve().parents[3] / "agents"
BOX_TZ = "America/Los_Angeles"


@pytest.fixture(autouse=True)
def _server_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """The box's zone on the in-process server; seed and the check-in read it from
    ``GET /v1/ae/capacity`` (2026-10-22), so every row below lands in it."""
    monkeypatch.setenv("AE_TASK_STORE_TZ", BOX_TZ)


@pytest.fixture()
def sched_app(ae_stores: dict[str, Any], db_uri: str) -> FastAPI:
    """The AE app plus a scheduled-task store; agents seed at lifespan from the env."""
    from omnigent.server.app import create_app
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    return create_app(**ae_stores, scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri))


@asynccontextmanager
async def _lifespan_client(app: FastAPI, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Upstream's ``auth_client`` pattern: lifespan entered so the scheduler exists."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c
    finally:
        set_harness_process_manager(None)
        await pm.shutdown()


@pytest_asyncio.fixture()
async def sched_client(
    sched_app: FastAPI, mock_llm: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """The lifespan client with steward and shutdown registered the deployment's way."""
    monkeypatch.setenv(
        server_app._EXTRA_BUILTIN_AGENTS_ENV,
        os.pathsep.join(str(AGENTS / name) for name in ("steward", "shutdown")),
    )
    async with _lifespan_client(sched_app, tmp_path) as c:
        yield c
    mock_llm.release_all()


@pytest_asyncio.fixture()
async def bare_sched_client(
    sched_app: FastAPI, mock_llm: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """The same app with no extra agents registered."""
    monkeypatch.delenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, raising=False)
    async with _lifespan_client(sched_app, tmp_path) as c:
        yield c
    mock_llm.release_all()


async def _listed(client: httpx.AsyncClient) -> dict[str, dict[str, Any]]:
    response = await client.get("/v1/scheduled-tasks")
    assert response.status_code == 200, response.text
    return {row["name"]: row for row in response.json()["scheduled_tasks"]}


def test_the_decided_times() -> None:
    assert schedules.STEWARD.rrule == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=8;BYMINUTE=0"
    assert schedules.SHUTDOWN.rrule == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR;BYHOUR=17;BYMINUTE=30"
    # The shipped rows carry the fallback; seed() puts the server's zone on them.
    assert {s.timezone for s in schedules.SCHEDULES} == {"UTC"} == {schedules.TIMEZONE}
    assert [s.agent for s in schedules.SCHEDULES] == ["steward", "shutdown"]
    assert all(s.max_cost_usd > 0 for s in schedules.SCHEDULES)


async def test_seed_creates_both_then_changes_nothing(sched_client: httpx.AsyncClient) -> None:
    first = await schedules.seed(sched_client)
    assert [(row["name"], row["action"]) for row in first] == [
        ("AE steward", "created"),
        ("AE shutdown note", "created"),
    ]
    listed = await _listed(sched_client)
    assert set(listed) == {"AE steward", "AE shutdown note"}
    agents = await schedules.agents_by_name(sched_client)
    for schedule in schedules.SCHEDULES:
        row = listed[schedule.name]
        assert row["agent_id"] == agents[schedule.agent]
        assert row["rrule"] == schedule.rrule
        assert row["timezone"] == BOX_TZ
        assert row["prompt"] == schedule.prompt
        assert row["max_cost_usd"] == schedule.max_cost_usd
        assert row["state"] == "active"
        assert row["host_id"] is None and row["workspace"] is None
        assert row["next_run_at"], "the live scheduler armed it"

    second = await schedules.seed(sched_client)
    assert [row["action"] for row in second] == ["unchanged", "unchanged"]
    assert [row["id"] for row in second] == [row["id"] for row in first]
    assert await _listed(sched_client) == listed


async def test_seed_patches_drift_and_leaves_a_pause_alone(
    sched_client: httpx.AsyncClient,
) -> None:
    (steward, _) = await schedules.seed(sched_client)
    paused = await sched_client.patch(
        f"/v1/scheduled-tasks/{steward['id']}",
        json={"state": "paused", "prompt": "an old prompt", "max_cost_usd": 9.5},
    )
    assert paused.status_code == 200, paused.text
    result = await schedules.seed(sched_client)
    assert result[0]["action"] == "updated"
    assert result[0]["changed"] == ["max_cost_usd", "prompt"]
    assert result[1]["action"] == "unchanged"
    row = (await _listed(sched_client))["AE steward"]
    assert row["prompt"] == schedules.STEWARD_PROMPT
    assert row["max_cost_usd"] == schedules.MAX_COST_USD
    assert row["state"] == "paused", "a pause is the person's; the seed never sends state"


async def test_seed_names_a_missing_agent(bare_sched_client: httpx.AsyncClient) -> None:
    """With the agents not registered the error says what to set."""
    with pytest.raises(schedules.SeedError, match="OMNIGENT_BUILTIN_AGENT_DIRS"):
        await schedules.seed(bare_sched_client)
    assert await _listed(bare_sched_client) == {}


async def test_manual_run_records_a_failed_run_without_a_host(
    sched_client: httpx.AsyncClient,
) -> None:
    (steward, _) = await schedules.seed(sched_client)
    response = await sched_client.post(f"/v1/scheduled-tasks/{steward['id']}/run")
    assert response.status_code == 202, response.text
    assert response.json() == {"triggered": True, "id": steward["id"]}
    # The fire is fire-and-forget; wait for it before reading the run rows.
    pending = list(fire._PENDING_FIRES)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    runs = await sched_client.get(f"/v1/scheduled-tasks/{steward['id']}/runs")
    assert runs.status_code == 200, runs.text
    (run,) = runs.json()["runs"]
    assert run["scheduled_task_id"] == steward["id"]
    assert run["status"] == "failed"
    assert run["error_code"] in {"host_registry_unavailable", "no_online_host"}
    assert run["conversation_id"] is None
    assert set(run) >= {"id", "status", "scheduled_at", "fired_at", "finished_at", "error_code"}
    listed = (await _listed(sched_client))["AE steward"]
    assert listed["last_run_status"] == "failed"


async def test_main_prints_one_line_per_schedule(
    sched_client: httpx.AsyncClient,
    sched_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The script's path: URL from the command line, the client from ``omni.http_client``."""

    def _asgi(settings: Settings) -> httpx.AsyncClient:
        assert settings.server_url == "http://test"
        assert settings.token == "tok-1"
        return http_client(settings, transport=httpx.ASGITransport(app=sched_app))

    monkeypatch.setattr(schedules, "http_client", _asgi)
    monkeypatch.setenv("OMNIGENT_TOKEN", "tok-1")
    assert await schedules.amain(["http://test/"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("created   AE steward  id=")
    assert out[1].startswith("created   AE shutdown note  id=")
    assert await schedules.amain(["http://test", "--json"]) == 0
    assert '"action": "unchanged"' in capsys.readouterr().out


def test_main_needs_a_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OMNIGENT_SERVER_URL", raising=False)
    assert schedules.main([]) == 2
    assert "OMNIGENT_SERVER_URL" in capsys.readouterr().err


# ── 2026-09-25: the orchestrator's check-in row ──


def test_checkin_rule_is_hourly_or_slower_and_passes_upstreams_validator() -> None:
    from datetime import date
    from zoneinfo import ZoneInfo

    from omnigent.server.scheduled.rrule import validate_rrule

    assert schedules.checkin_rrule(1) == "FREQ=HOURLY;INTERVAL=1"
    until = schedules.checkin_rrule(2, date(2026, 10, 16), tz=BOX_TZ)
    # 23:59:59 on 2026-10-16 in Los Angeles (PDT, UTC-7) is 06:59:59Z the next day.
    assert until == "FREQ=HOURLY;INTERVAL=2;UNTIL=20261017T065959Z"
    assert schedules.checkin_rrule(2, date(2026, 10, 16)).endswith("UNTIL=20261016T235959Z")
    for rule in (schedules.checkin_rrule(1), schedules.checkin_rrule(24), until):
        validate_rrule(rule, ZoneInfo(BOX_TZ))
    for bad in (0, 25):
        with pytest.raises(ValueError, match="every_hours"):
            schedules.checkin_rrule(bad)


def test_checkin_prompt_hands_off_to_the_named_session() -> None:
    row = schedules.checkin_schedule(1, session_id="conv_orch", note="  omnigent-ae PR 42  ")
    assert row.name == schedules.CHECKIN_NAME == "AE orchestrator check-in"
    assert row.agent == "orchestrator"
    assert row.prompt.startswith("Scheduled orchestrator check-in. Focus: omnigent-ae PR 42 ")
    assert "post_message once with session_id conv_orch" in row.prompt
    assert "Check-in: run your check-in now." in row.prompt
    alone = schedules.checkin_prompt(None, None)
    assert alone == (
        "Scheduled orchestrator check-in. Run the check-in your instructions describe, then stop."
    )
    assert row.body("ag_1")["max_cost_usd"] == schedules.MAX_COST_USD


@pytest_asyncio.fixture()
async def orch_sched_client(
    sched_app: FastAPI, mock_llm: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    """The lifespan client with the orchestrator registered."""
    monkeypatch.setenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, str(AGENTS / "orchestrator"))
    async with _lifespan_client(sched_app, tmp_path) as c:
        yield c
    mock_llm.release_all()


async def test_schedule_checkin_creates_updates_pauses_and_resumes(
    orch_sched_client: httpx.AsyncClient,
) -> None:
    from ae_omni_policies.mcp import OmniClient, schedule_checkin

    omni = OmniClient(Settings(server_url="http://test"), http=orch_sched_client)
    args = {"session_id": "conv_orch", "note": "omnigent-ae orchestrator PR"}
    created = await schedule_checkin(omni, args)
    assert created["action"] == "created", created
    assert created["rrule"] == "FREQ=HOURLY;INTERVAL=1"
    assert created["state"] == "active"
    assert created["next_run_at"], "the live scheduler armed it"
    row = (await _listed(orch_sched_client))[schedules.CHECKIN_NAME]
    agents = await schedules.agents_by_name(orch_sched_client)
    assert row["agent_id"] == agents["orchestrator"]
    assert "session_id conv_orch" in row["prompt"]

    assert (await schedule_checkin(omni, args))["action"] == "unchanged"
    slower = await schedule_checkin(omni, args | {"every_hours": 3, "until": "2026-10-16"})
    assert slower["action"] == "updated"
    assert slower["rrule"] == "FREQ=HOURLY;INTERVAL=3;UNTIL=20261017T065959Z"
    assert slower["id"] == created["id"]

    paused = await schedule_checkin(omni, {"stop": True})
    assert paused == {"name": schedules.CHECKIN_NAME, "id": created["id"], "action": "paused"}
    assert (await _listed(orch_sched_client))[schedules.CHECKIN_NAME]["state"] == "paused"
    again = await schedule_checkin(omni, args | {"every_hours": 3, "until": "2026-10-16"})
    assert (again["action"], again["state"]) == ("updated", "active")

    bad = await schedule_checkin(omni, {"every_hours": 30})
    assert bad["error"].startswith("Nothing was scheduled: every_hours must be 1 to 24")
    bad_date = await schedule_checkin(omni, {"until": "next friday"})
    assert bad_date["error"].startswith("Nothing was scheduled:")


async def test_schedule_checkin_without_the_agent_or_a_row(
    bare_sched_client: httpx.AsyncClient,
) -> None:
    from ae_omni_policies.mcp import OmniClient, schedule_checkin

    omni = OmniClient(Settings(server_url="http://test"), http=bare_sched_client)
    assert await schedule_checkin(omni, {"stop": True}) == {
        "name": schedules.CHECKIN_NAME,
        "action": "none",
        "state": "absent",
    }
    missing = await schedule_checkin(omni, {})
    assert "orchestrator agent is not registered" in missing["error"]
    assert (await schedule_checkin(OmniClient(Settings()), {}))["error"].startswith(
        "No OmniGent server"
    )


# ── Project (the task-project core patch, 2026-10-13) ────────────────────


@pytest_asyncio.fixture()
async def project_client(
    ae_stores: dict[str, Any],
    db_uri: str,
    mock_llm: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    """The scheduled app with a project store, and that store."""
    from omnigent.server.app import create_app
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
    from omnigent.stores.scheduled_task_store.sqlalchemy_store import (
        SqlAlchemyScheduledTaskStore,
    )

    monkeypatch.setenv(
        server_app._EXTRA_BUILTIN_AGENTS_ENV,
        os.pathsep.join(str(AGENTS / name) for name in ("steward", "shutdown")),
    )
    projects = SqlAlchemyProjectStore(db_uri)
    app = create_app(
        **ae_stores,
        scheduled_task_store=SqlAlchemyScheduledTaskStore(db_uri),
        project_store=projects,
    )
    async with _lifespan_client(app, tmp_path) as c:
        yield c, projects
    mock_llm.release_all()


async def test_seed_files_both_into_automation_notes(
    project_client: tuple[httpx.AsyncClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # No project in the rows since 2026-10-22: the server's own default files them.
    client, projects = project_client
    notes = projects.create(uuid.uuid4().hex, "Automation Notes", None)
    monkeypatch.setenv("OMNIGENT_SCHEDULED_TASK_DEFAULT_PROJECT", "Automation Notes")
    await schedules.seed(client)
    rows = await _listed(client)
    assert {rows[s.name]["project_id"] for s in schedules.SCHEDULES} == {notes.id}


async def test_seed_fills_a_missing_project_and_keeps_a_chosen_one(
    project_client: tuple[httpx.AsyncClient, Any],
) -> None:
    # A schedule that names its own project (none of the shipped ones do).
    client, projects = project_client
    named = tuple(replace(s, project="Automation Notes") for s in schedules.SCHEDULES)
    (_, shutdown) = await schedules.seed(client, named)
    rows = await _listed(client)
    assert rows["AE steward"]["project_id"] is None, "no Automation Notes project yet"

    notes = projects.create(uuid.uuid4().hex, "automation notes", None)
    elsewhere = projects.create(uuid.uuid4().hex, "Elsewhere", None)
    moved = await client.patch(
        f"/v1/scheduled-tasks/{shutdown['id']}", json={"project_id": elsewhere.id}
    )
    assert moved.status_code == 200, moved.text

    result = await schedules.seed(client, named)
    assert result[0]["changed"] == ["project_id"]
    assert result[1]["action"] == "unchanged"
    rows = await _listed(client)
    assert rows["AE steward"]["project_id"] == notes.id
    assert rows["AE shutdown note"]["project_id"] == elsewhere.id


@pytest.mark.parametrize(
    ("status", "body", "match"),
    [
        (404, {"detail": "Not Found"}, "reading the server's timezone: 404"),
        (200, {"project": None}, "names no timezone"),
        (200, {"timezone": " ", "project": None}, "names no timezone"),
    ],
)
async def test_seed_stops_when_the_server_names_no_zone(
    status: int, body: dict[str, Any], match: str
) -> None:
    # Never a silent UTC: a reseed in the wrong zone would move every clock.
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(answer), base_url="http://x") as c:
        with pytest.raises(schedules.SeedError, match=match):
            await schedules.seed(c)


async def test_seed_files_rows_made_before_the_default_project_existed(
    project_client: tuple[httpx.AsyncClient, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The server's default names a project nobody has made yet, so the rows are
    # created unfiled; a reseed after it exists files them.
    client, projects = project_client
    monkeypatch.setenv("OMNIGENT_SCHEDULED_TASK_DEFAULT_PROJECT", "Automation Notes")
    await schedules.seed(client)
    assert {row["project_id"] for row in (await _listed(client)).values()} == {None}
    notes = projects.create(uuid.uuid4().hex, "Automation Notes", None)
    result = await schedules.seed(client)
    assert [row["changed"] for row in result] == [["project_id"], ["project_id"]]
    rows = await _listed(client)
    assert {rows[s.name]["project_id"] for s in schedules.SCHEDULES} == {notes.id}
