"""Reminder tools: create_task, list_lists and cancel_task against respx.

The worker's ``create_task`` is the reference (``task-store-worker/src/index.ts``).
Cancel is this fork's own: a drop through PATCH, because the task app refuses DELETE
from a machine credential.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import respx

from ae_omni_policies import notes
from ae_omni_policies.taskstore import (
    ORIGIN_SURFACE,
    ReminderFields,
    Settings,
    TaskStoreClient,
)

BASE = "https://tasks.test"
SETTINGS = Settings(
    app_url=BASE, client_id="test-client-id.access", client_secret="s3", tz="America/Los_Angeles"
)
AE_WORK = {"id": "B1", "name": "AE Work", "kind": "list", "surface": "tasks"}
HIDDEN = {"id": "B2", "name": "AE Work", "kind": "list", "surface": "tasks", "hidden": 1}
OTHER = {"id": "B3", "name": "Home", "kind": "list", "surface": "tasks"}
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _task(**over: Any) -> dict[str, Any]:
    return {
        "id": "T1",
        "title": "Check scan",
        "kind": "task",
        "status": "open",
        "origin_surface": ORIGIN_SURFACE,
        **over,
    }


# ── the home clock ──────────────────────────────────────────────────────


def test_home_clock_instant_winter_and_summer() -> None:
    assert notes.home_clock_instant("2026-11-09", "10:00", "America/Los_Angeles") == (
        "2026-11-09T18:00:00Z"
    )
    assert notes.home_clock_instant("2026-07-01", "10:00", "America/Los_Angeles") == (
        "2026-07-01T17:00:00Z"
    )


def test_home_clock_instant_matches_the_worker_on_both_dst_edges() -> None:
    # Autumn fold: 01:30 happens twice on 2026-11-01; the first (PDT, -7) wins.
    assert notes.home_clock_instant("2026-11-01", "01:30", "America/Los_Angeles") == (
        "2026-11-01T08:30:00Z"
    )
    # Spring gap: 02:30 does not exist on 2026-03-08; it lands an hour later, 03:30 PDT.
    assert notes.home_clock_instant("2026-03-08", "02:30", "America/Los_Angeles") == (
        "2026-03-08T10:30:00Z"
    )


def test_task_helpers() -> None:
    assert notes.normalize_task_tags([" AE ", "ae", "", "Power BI"]) == ["ae", "power bi"]
    assert notes.task_estimate("45") == "45"
    assert notes.task_estimate("about an hour") == "60plus"
    assert notes.task_estimate("5 minutes") == "15"
    assert notes.task_estimate(" ") is None
    assert notes.build_task_notes("Read the log.", ["Open it", "  "]) == (
        "- [ ] Open it\n\nRead the log."
    )
    assert notes.build_task_notes(None, None) == ""
    assert notes.TIME_ONLY.match("23:59") and not notes.TIME_ONLY.match("24:00")


# ── create_task ─────────────────────────────────────────────────────────


async def test_timed_reminder_sets_due_and_alarm_to_the_same_instant() -> None:
    fields = ReminderFields(
        title="Check scan",
        list_name="ae work",
        due_date="2026-11-09",
        due_time="10:00",
        notes="Read the turns line.",
        tags=["AE"],
        quadrant="feature",
        est="minutes",
    )
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json={"buckets": [HIDDEN, AE_WORK, OTHER]})
        post = mock.post("/api/tasks").respond(201, json=_task(quadrant="q3", tags=["ae"]))
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.create_reminder(fields, now=NOW)
    body = json.loads(post.calls[0].request.content)
    assert body["due_at"] == body["reminder_at"] == "2026-11-09T18:00:00Z"
    assert body["due_is_all_day"] == 0
    assert body["floating"] == 1
    assert body["bucket_id"] == "B1"
    assert body["origin_surface"] == ORIGIN_SURFACE
    assert body["tags"] == ["ae"]
    assert body["estimate"] == "15"
    assert (body["urgent"], body["important"]) == (
        notes.QUADRANT_AXES["feature"]["urgent"],
        notes.QUADRANT_AXES["feature"]["important"],
    )
    assert out["outcome"] == "created"
    assert out["reminder"] == "pushed at 2026-11-09 10:00 (America/Los_Angeles)"
    assert out["url"].startswith(f"{BASE}/#/all?list=B1")
    assert out["warnings"] == []


async def test_date_only_is_all_day_with_no_alarm() -> None:
    with respx.mock(base_url=BASE) as mock:
        post = mock.post("/api/tasks").respond(201, json=_task())
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.create_reminder(
                ReminderFields(title="Check scan", due_date="2026-11-09"), now=NOW
            )
    body = json.loads(post.calls[0].request.content)
    assert body["due_at"] == "2026-11-09T00:00:00Z"
    assert body["due_is_all_day"] == 1
    assert body["reminder_at"] is None
    assert body["bucket_id"] is None
    assert "estimate" not in body and "urgent" not in body
    assert out["reminder"] == "none: no time was given"


async def test_bad_input_is_refused_before_any_request() -> None:
    cases = [
        (ReminderFields(title="  "), "A title is required."),
        (ReminderFields(title="x", due_time="10:00"), "due_time needs a due_date."),
        (ReminderFields(title="x", due_date="2026-02-30"), "is not a real YYYY-MM-DD date."),
        (
            ReminderFields(title="x", due_date="2026-11-09", due_time="25:00"),
            "is not HH:MM on a 24-hour clock.",
        ),
        (ReminderFields(title="x", quadrant="urgent"), "is not one of"),
    ]
    with respx.mock(base_url=BASE) as mock:
        async with TaskStoreClient(SETTINGS) as client:
            for fields, reason in cases:
                out = await client.create_reminder(fields, now=NOW)
                assert out["outcome"] == "not_created", fields
                assert reason in out["reason"], (fields, out["reason"])
        assert mock.calls.call_count == 0


async def test_unknown_or_ambiguous_list_comes_back_with_the_real_names() -> None:
    dupe = {**OTHER, "id": "B4", "name": "AE WORK"}
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/api/buckets").respond(200, json={"buckets": [AE_WORK, dupe, OTHER, HIDDEN]})
        post = mock.post("/api/tasks").respond(201, json=_task())
        async with TaskStoreClient(SETTINGS) as client:
            missing = await client.create_reminder(
                ReminderFields(title="x", list_name="Nope"), now=NOW
            )
            twice = await client.create_reminder(
                ReminderFields(title="x", list_name="AE Work"), now=NOW
            )
    assert missing["outcome"] == "not_created"
    assert missing["lists"] == ["AE Work", "AE WORK", "Home"]
    assert "No list is called" in missing["reason"]
    assert "More than one list" in twice["reason"]
    assert post.call_count == 0


async def test_a_time_already_past_warns_but_still_files() -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.post("/api/tasks").respond(201, json=_task())
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.create_reminder(
                ReminderFields(title="x", due_date="2026-11-03", due_time="01:00"), now=NOW
            )
    assert out["outcome"] == "created"
    assert out["warnings"] and "already passed" in out["warnings"][0]


async def test_a_timed_out_create_is_reread_and_never_resent() -> None:
    landed = _task(due_at="2026-11-09T18:00:00Z", id="T-LANDED")
    with respx.mock(base_url=BASE) as mock:
        post = mock.post("/api/tasks").mock(side_effect=httpx.ReadTimeout("slow"))
        mock.get("/api/tasks", params={"status": "open"}).respond(
            200, json={"tasks": [_task(id="T-OLD", due_at=None), landed]}
        )
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.create_reminder(
                ReminderFields(title="Check scan", due_date="2026-11-09", due_time="10:00"),
                now=NOW,
            )
    assert post.call_count == 1
    assert out["outcome"] == "created"
    assert out["task_id"] == "T-LANDED"
    assert "not sent twice" in out["warnings"][0]


async def test_a_5xx_create_that_landed_is_found_and_not_resent() -> None:
    landed = _task(due_at="2026-11-09T18:00:00Z", id="T-LANDED", created_at="2099-01-01T00:00:00Z")
    with respx.mock(base_url=BASE) as mock:
        post = mock.post("/api/tasks").respond(504, json={"error": "gateway timeout"})
        mock.get("/api/tasks", params={"status": "open"}).respond(200, json={"tasks": [landed]})
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.create_reminder(
                ReminderFields(title="Check scan", due_date="2026-11-09", due_time="10:00"),
                now=NOW,
            )
    assert post.call_count == 1
    assert out["outcome"] == "created"
    assert out["task_id"] == "T-LANDED"


async def test_a_5xx_create_that_did_not_land_is_reported_not_resent() -> None:
    with respx.mock(base_url=BASE) as mock:
        post = mock.post("/api/tasks").respond(502, json={"error": "bad gateway"})
        mock.get("/api/tasks", params={"status": "open"}).respond(200, json={"tasks": []})
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.create_reminder(ReminderFields(title="Check scan"), now=NOW)
    assert post.call_count == 1
    assert out["outcome"] == "not_created"
    assert "add by hand" in out["instruction"]


async def test_the_reread_ignores_an_older_or_other_list_look_alike() -> None:
    older = _task(id="T-OLD", created_at="2026-01-01T00:00:00Z")
    elsewhere = _task(id="T-HOME", bucket_id="B3", created_at="2099-01-01T00:00:00Z")
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/api/buckets").respond(200, json={"buckets": [AE_WORK, OTHER]})
        post = mock.post("/api/tasks").mock(side_effect=httpx.ReadTimeout("slow"))
        mock.get("/api/tasks", params={"status": "open"}).respond(
            200, json={"tasks": [older, elsewhere]}
        )
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.create_reminder(
                ReminderFields(title="Check scan", list_name="AE Work"), now=NOW
            )
    assert post.call_count == 1
    assert out["outcome"] == "not_created"


async def test_a_failed_create_tells_the_agent_to_hand_it_to_the_user() -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.post("/api/tasks").respond(400, json={"error": "bad due_at"})
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.create_reminder(ReminderFields(title="x"), now=NOW)
    assert out["outcome"] == "not_created"
    assert "add by hand" in out["instruction"]


# ── list_lists ──────────────────────────────────────────────────────────


async def test_list_lists_leaves_hidden_ones_out_and_reports_a_failure() -> None:
    with respx.mock(base_url=BASE) as mock:
        route = mock.get("/api/buckets")
        route.respond(200, json={"buckets": [AE_WORK, HIDDEN, OTHER]})
        async with TaskStoreClient(SETTINGS) as client:
            ok = await client.list_lists()
        route.respond(403, json={"error": "nope"})
        async with TaskStoreClient(SETTINGS) as client:
            bad = await client.list_lists()
    assert ok["count"] == 2
    assert [row["name"] for row in ok["lists"]] == ["AE Work", "Home"]
    assert bad["error"].startswith("Could not read the lists:")


# ── cancel ──────────────────────────────────────────────────────────────


async def test_cancel_drops_an_open_task_this_server_made() -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/api/tasks/T1").respond(200, json=_task())
        patch = mock.patch("/api/tasks/T1").respond(200, json=_task(status="dropped"))
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.cancel("T1")
        delete = mock.delete("/api/tasks/T1")
    assert json.loads(patch.calls[0].request.content) == {"status": "dropped"}
    assert out["outcome"] == "cancelled"
    assert out["status"] == "dropped"
    assert delete.call_count == 0


async def test_cancel_refuses_what_it_did_not_make_or_is_not_open() -> None:
    cases = [
        _task(origin_surface="todo"),
        _task(origin_surface="claude"),
        _task(status="active"),
        _task(status="done"),
        _task(status="dropped"),
        _task(kind="note"),
        _task(tags=["parked", "ae"]),
        _task(origin_surface=None),
    ]
    for task in cases:
        with respx.mock(base_url=BASE, assert_all_called=False) as mock:
            mock.get("/api/tasks/T1").respond(200, json=task)
            patch = mock.patch("/api/tasks/T1").respond(200, json=task)
            async with TaskStoreClient(SETTINGS) as client:
                out = await client.cancel("T1")
            assert out["outcome"] == "not_cancelled", task
            assert patch.call_count == 0, task


async def test_cancel_reports_a_missing_task_and_a_failed_patch() -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tasks/T-GONE").respond(404, json={"error": "no task T-GONE"})
        mock.get("/api/tasks/T1").respond(200, json=_task())
        mock.patch("/api/tasks/T1").respond(409, json={"error": "stale"})
        async with TaskStoreClient(SETTINGS) as client:
            gone = await client.cancel("T-GONE")
            stale = await client.cancel("T1")
    assert gone == {
        "outcome": "not_cancelled",
        "task_id": "T-GONE",
        "reason": "404 no task T-GONE",
    }
    assert stale["outcome"] == "not_cancelled"
    assert "stale" in stale["reason"]
