"""The new-model watch's server side: the report, the store, the notes, the reminder."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from ae_omni_policies import cost_targets, model_watch, preset_review, taskstore

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("AE_COST_ALERT_LIST", "AE Work")
    model_watch.reset_store(model_watch.WatchStore())
    yield
    model_watch.reset_store()


class FakeApi:
    """The task store, in a list."""

    def __init__(self, open_tasks: list[dict[str, Any]] | None = None) -> None:
        self.open_tasks = open_tasks or []
        self.created: list[dict[str, Any]] = []

    async def list_buckets(self) -> taskstore.Result:
        rows = [{"id": "b1", "name": "AE Work"}]
        return taskstore.Result(ok=True, status=200, data={"buckets": rows})

    async def list_tags(self) -> taskstore.Result:
        return taskstore.Result(ok=True, status=200, data={"tags": [{"name": "ae"}]})

    async def list_tasks(self, status: str = "open") -> taskstore.Result:
        return taskstore.Result(ok=True, status=200, data={"tasks": self.open_tasks})

    async def create_task(self, fields: dict[str, Any]) -> taskstore.Result:
        self.created.append(fields)
        return taskstore.Result(ok=True, status=201, data={"id": f"t{len(self.created)}"})

    async def aclose(self) -> None:
        return None


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(model_watch.taskstore, "TaskStoreClient", lambda _settings: fake)
    return fake


def _probe(model_id: str = "claude-fable-5-2", **kw: Any) -> dict[str, Any]:
    family, version = model_watch.parse_id(model_id) or ("", (0, 0))
    return {
        "id": model_id,
        "family": family,
        "version": list(version),
        "served": True,
        "detail": "answered ok",
        "probed_at": "2026-11-10T11:59:00Z",
    } | kw


def _report(*probes: dict[str, Any]) -> model_watch.ReportPost:
    return model_watch.ReportPost.model_validate(
        {
            "host": "box",
            "claude_code_version": "2.1.322",
            "scanned_at": "2026-11-10T11:58:00Z",
            "probes": list(probes),
        }
    )


def test_ids_parse_and_read_as_labels() -> None:
    assert model_watch.parse_id("claude-fable-5-2") == ("fable", (5, 2))
    assert model_watch.parse_id("claude-opus-5") == ("opus", (5, 0))
    assert model_watch.parse_id("claude-mythos-5-1") == ("mythos", (5, 1))
    assert model_watch.parse_id("claude-opus-4-20250514") is None
    assert model_watch.parse_id("claude-fable-5-2[1m]") is None
    assert model_watch.parse_id("gpt-6-astra") is None
    assert model_watch.label_of("claude-fable-5-2") == "Fable 5.2"
    assert model_watch.label_of("claude-opus-5") == "Opus 5"


@pytest.mark.parametrize(
    "change",
    [
        {"id": "claude-fable-5-2; rm -rf"},
        {"id": "claude-opus-4-20250514", "family": "opus", "version": [4, 20250514]},
        {"family": "opus"},
        {"version": [5, 3]},
        {"detail": "x" * 201},
        {"extra": 1},
    ],
)
def test_the_report_refuses_a_malformed_probe(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _report(_probe() | change)


def test_the_report_caps_its_size() -> None:
    with pytest.raises(ValidationError):
        _report(*[_probe()] * (model_watch.MAX_PROBES + 1))
    with pytest.raises(ValidationError):
        model_watch.ReportPost.model_validate(
            {"host": "h" * 81, "scanned_at": "2026-11-10T11:58:00Z"}
        )


def test_record_returns_ids_that_start_answering_once() -> None:
    assert model_watch.record(_report(_probe()), NOW) == ["claude-fable-5-2"]
    assert model_watch.record(_report(_probe()), NOW) == []
    assert model_watch.served_ids() == ["claude-fable-5-2"]
    state = model_watch.store().read()
    assert state["last_report"]["host"] == "box"
    assert state["models"]["claude-fable-5-2"]["label"] == "Fable 5.2"


def test_an_errored_probe_never_unserves_an_id() -> None:
    model_watch.record(_report(_probe()), NOW)
    model_watch.record(_report(_probe(served=False, error=True, detail="no answer in 90s")), NOW)
    assert model_watch.served_ids() == ["claude-fable-5-2"]
    model_watch.record(_report(_probe(served=False)), NOW)
    assert model_watch.served_ids() == []


def test_a_new_id_that_does_not_answer_is_listed_but_not_served() -> None:
    assert model_watch.record(_report(_probe("claude-mythos-5-2", served=False)), NOW) == []
    row = model_watch.view(False, NOW)["models"][0]
    assert (row["id"], row["probe"]) == ("claude-mythos-5-2", "not_served")
    assert "notes" not in row and "review" not in row
    assert "notes" in model_watch.view(True, NOW)["models"][0]


def test_the_view_says_when_the_report_is_stale() -> None:
    assert model_watch.view(True, NOW)["report_stale"] is True
    model_watch.record(_report(), NOW)
    assert model_watch.view(True, NOW)["report_stale"] is False
    assert model_watch.view(True, NOW + timedelta(hours=14))["report_stale"] is True


PAGE = "# Models\n\nClaude Fable 5.2 is our newest model.\n\n- Data retention: 30 days.\n"


def _pages(status: int = 200, text: str = PAGE) -> httpx.MockTransport:
    return httpx.MockTransport(lambda _request: httpx.Response(status, text=text))


async def test_notes_come_back_as_a_clean_excerpt_around_the_mention() -> None:
    notes = await model_watch.fetch_notes("claude-fable-5-2", transport=_pages())
    assert notes["status"] == "ok" and notes["url"] == model_watch.DEFAULT_NOTES_URL
    assert "Fable 5.2" in notes["excerpt"] and "\n" not in notes["excerpt"]


async def test_notes_fall_back_when_the_page_is_silent_or_gone() -> None:
    silent = await model_watch.fetch_notes("claude-mythos-5-2", transport=_pages())
    assert (silent["status"], silent["excerpt"]) == ("not_mentioned", None)
    gone = await model_watch.fetch_notes("claude-fable-5-2", transport=_pages(404, "no"))
    assert (gone["status"], gone["excerpt"]) == ("fetch_failed", None)


def test_notes_urls_are_https_only_and_overridable() -> None:
    assert model_watch.notes_urls({}) == [model_watch.DEFAULT_NOTES_URL]
    env = {model_watch.NOTES_URLS_ENV: "https://a.test/x.md, http://b.test/y.md"}
    assert model_watch.notes_urls(env) == ["https://a.test/x.md"]


def _start(model_id: str = "claude-fable-5-2", at: datetime = NOW) -> None:
    model_watch.record(_report(_probe(model_id)), at)

    def apply(state: dict[str, Any]) -> None:
        state["models"][model_id]["notes"] = {
            "url": "https://x.test",
            "excerpt": "Fable 5.2 notes",
            "status": "ok",
        }
        state["models"][model_id]["review"] = {
            "state": "started",
            "at": model_watch._iso(at),
            "owner": "admin",
            "session": "s1",
        }

    model_watch.store().update(apply)


def test_the_review_context_holds_only_recent_started_reviews() -> None:
    _start()
    (ctx,) = model_watch.review_context(NOW + timedelta(minutes=30), "admin")
    assert ctx["id"] == "claude-fable-5-2" and ctx["release_notes_excerpt"] == "Fable 5.2 notes"
    assert model_watch.review_context(NOW + timedelta(hours=2), "admin") == []
    assert model_watch.review_context(NOW + timedelta(minutes=30), "someone-else") == []


async def test_after_review_marks_done_and_files_one_reminder(api: FakeApi) -> None:
    _start()
    review = {"id": "rev1", "count": 2, "summary": "Retention: kept 30 days. Two presets."}
    assert await model_watch.after_review(review, NOW, "admin", "s1") == ["claude-fable-5-2"]
    (task,) = api.created
    assert task["title"] == "New Claude model: Fable 5.2"
    assert "2 preset suggestions are waiting: Admin Center, Spend, New models." in task["notes"]
    assert "Retention: kept 30 days." in task["notes"]
    assert task["notes"].endswith("Model key: claude-fable-5-2")
    assert task["reminder_at"] == "2026-11-10T12:03:00Z" and task["important"] == 1
    assert task["origin_surface"] == taskstore.ORIGIN_SURFACE and task["bucket_id"] == "b1"
    assert task["tags"] == ["ae"]
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "done" and stored["task_id"] == "t1"
    assert await model_watch.after_review(review, NOW, "admin", "s1") == []
    assert len(api.created) == 1


async def test_a_summary_without_retention_gets_the_cautious_line(api: FakeApi) -> None:
    _start()
    await model_watch.after_review(
        {"id": "r", "count": 1, "summary": "Checked."}, NOW, "admin", "s1"
    )
    assert model_watch.RETENTION_FALLBACK in api.created[0]["notes"]


async def test_a_restart_that_lost_the_task_id_files_nothing_twice(api: FakeApi) -> None:
    api.open_tasks = [
        {
            "id": "old",
            "origin_surface": taskstore.ORIGIN_SURFACE,
            "notes": "x\n\nModel key: claude-fable-5-2",
        }
    ]
    model_watch.record(_report(_probe()), NOW)
    assert await model_watch.notify("claude-fable-5-2", "text", NOW) is None
    assert api.created == []
    assert model_watch.store().read()["models"]["claude-fable-5-2"]["task_id"] == "old"


async def test_no_reminder_without_a_task_store(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_CF_ACCESS_CLIENT_ID")
    model_watch.record(_report(_probe()), NOW)
    assert not cost_targets.config().on
    assert await model_watch.notify("claude-fable-5-2", "text", NOW) is None
    assert api.created == []


async def test_handle_new_runs_one_review_after_the_notes() -> None:
    model_watch.record(_report(_probe()), NOW)
    calls: list[int] = []

    async def run_review() -> preset_review.RunNow:
        calls.append(1)
        return preset_review.RunNow(202)

    await model_watch.handle_new(["claude-fable-5-2"], run_review, NOW, transport=_pages())
    assert calls == [1]
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["notes"]["status"] == "ok" and stored["review"]["state"] == "started"


async def test_a_review_that_cannot_start_reminds_at_once_with_the_reason(api: FakeApi) -> None:
    model_watch.record(_report(_probe()), NOW)

    async def run_review() -> preset_review.RunNow:
        return preset_review.RunNow(
            409, "insights_off", "Preset insights are off; switch them on first."
        )

    await model_watch.handle_new(["claude-fable-5-2"], run_review, NOW, transport=_pages(404, ""))
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "not_run" and "insights are off" in stored["review"]["why"]
    assert "did not start: Preset insights are off" in api.created[0]["notes"]
    assert stored["notes"]["status"] == "fetch_failed"


async def test_a_review_that_raises_is_recorded_not_raised(api: FakeApi) -> None:
    model_watch.record(_report(_probe()), NOW)

    async def run_review() -> preset_review.RunNow:
        raise RuntimeError("boom")

    await model_watch.handle_new(["claude-fable-5-2"], run_review, NOW, transport=_pages())
    assert "RuntimeError" in api.created[0]["notes"]


async def test_a_review_still_started_after_two_hours_is_reminded_once(api: FakeApi) -> None:
    _start()
    assert await model_watch.expire_reviews(NOW + timedelta(hours=1)) == []
    assert await model_watch.expire_reviews(NOW + timedelta(hours=2, minutes=1)) == [
        "claude-fable-5-2"
    ]
    assert "did not finish" in api.created[0]["notes"]
    assert await model_watch.expire_reviews(NOW + timedelta(hours=9)) == []
    assert model_watch.store().read()["models"]["claude-fable-5-2"]["review"]["state"] == "not_run"


async def test_another_users_or_sessions_submit_completes_nothing(api: FakeApi) -> None:
    _start()
    review = {"id": "rev1", "count": 2, "summary": "Retention: 30 days."}
    assert await model_watch.after_review(review, NOW, "member", "s1") == []
    assert await model_watch.after_review(review, NOW, "admin", "other-session") == []
    assert api.created == []
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "started"


def test_a_newly_served_id_is_stored_pending_in_the_same_write() -> None:
    model_watch.record(_report(_probe()), NOW, "admin")
    review = model_watch.store().read()["models"]["claude-fable-5-2"]["review"]
    assert (review["state"], review["owner"]) == ("pending", "admin")
    assert "owner" not in model_watch.view(True, NOW)["models"][0]["review"]


async def test_the_sweep_restarts_a_review_left_pending_by_a_restart() -> None:
    model_watch.record(_report(_probe()), NOW, "admin")
    calls: list[int] = []

    async def run_review() -> preset_review.RunNow:
        calls.append(1)
        return preset_review.RunNow(202)

    await model_watch.sweep(NOW + timedelta(minutes=5), run_review, "admin", transport=_pages())
    assert calls == []
    await model_watch.sweep(NOW + timedelta(minutes=11), run_review, "admin", transport=_pages())
    assert calls == [1]
    review = model_watch.store().read()["models"]["claude-fable-5-2"]["review"]
    assert (review["state"], review["owner"]) == ("started", "admin")


async def test_an_id_already_in_flight_is_not_handled_twice() -> None:
    model_watch.record(_report(_probe()), NOW, "admin")
    calls: list[int] = []

    async def run_review() -> preset_review.RunNow:
        calls.append(1)
        await asyncio.sleep(0.05)
        return preset_review.RunNow(202)

    ids = ["claude-fable-5-2"]
    await asyncio.gather(
        model_watch.handle_new(ids, run_review, NOW, owner="admin", transport=_pages()),
        model_watch.handle_new(ids, run_review, NOW, owner="admin", transport=_pages()),
    )
    assert calls == [1]


async def test_a_running_review_keeps_the_model_pending_without_an_alarm(api: FakeApi) -> None:
    model_watch.record(_report(_probe()), NOW, "admin")

    async def busy() -> preset_review.RunNow:
        return preset_review.RunNow(409, "review_running", "A review is already running.")

    await model_watch.handle_new(
        ["claude-fable-5-2"], busy, NOW, owner="admin", transport=_pages()
    )
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "pending" and api.created == []

    async def free() -> preset_review.RunNow:
        return preset_review.RunNow(202)

    later = NOW + timedelta(minutes=11)
    await model_watch.sweep(later, free, "admin", transport=_pages())
    assert model_watch.store().read()["models"]["claude-fable-5-2"]["review"]["state"] == "started"
    assert api.created == []


async def test_a_reminder_that_failed_is_filed_once_by_the_next_sweep(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start()
    real = api.create_task
    boom = {"on": True}

    async def flaky(fields: dict[str, Any]) -> taskstore.Result:
        if boom["on"]:
            raise httpx.ConnectError("down")
        return await real(fields)

    monkeypatch.setattr(api, "create_task", flaky)
    review = {"id": "rev1", "count": 1, "summary": "Retention: 30 days."}
    await model_watch.after_review(review, NOW, "admin", "s1")
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "done" and stored["task_id"] is None
    assert stored["notify"]["status"] == "pending" and stored["notify"]["attempts"] == 1
    assert "ConnectError" in stored["notify"]["last_error"]
    boom["on"] = False

    async def idle() -> preset_review.RunNow:
        return preset_review.RunNow(202)

    await model_watch.sweep(NOW + timedelta(minutes=1), idle, "admin")
    await model_watch.sweep(NOW + timedelta(minutes=2), idle, "admin")
    assert len(api.created) == 1
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["notify"]["status"] == "sent" and stored["task_id"] == "t1"


async def test_two_deliveries_at_once_ring_the_phone_once(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start()
    real = api.create_task

    async def slow(fields: dict[str, Any]) -> taskstore.Result:
        await asyncio.sleep(0.05)
        return await real(fields)

    monkeypatch.setattr(api, "create_task", slow)
    review = {"id": "rev1", "count": 1, "summary": "Retention: 30 days."}

    async def idle() -> preset_review.RunNow:
        return preset_review.RunNow(202)

    await asyncio.gather(
        model_watch.after_review(review, NOW, "admin", "s1"),
        model_watch.sweep(NOW + timedelta(seconds=1), idle, "admin"),
    )
    assert len(api.created) == 1


async def test_no_task_store_stops_the_reminder_retries(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_CF_ACCESS_CLIENT_ID")
    _start()
    review = {"id": "rev1", "count": 1, "summary": "Retention: 30 days."}
    await model_watch.after_review(review, NOW, "admin", "s1")
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["notify"]["status"] == "skipped" and api.created == []


async def test_handle_new_skips_an_id_whose_review_already_moved_on() -> None:
    model_watch.record(_report(_probe()), NOW, "admin")
    model_watch.store().update(
        lambda data: data["models"]["claude-fable-5-2"]["review"].update(state="started")
    )  # another handler already took it
    calls: list[int] = []

    async def run_review() -> preset_review.RunNow:
        calls.append(1)
        return preset_review.RunNow(202)

    await model_watch.handle_new(["claude-fable-5-2"], run_review, NOW, owner="admin")
    assert calls == []


async def test_reminder_retries_stop_after_five_attempts(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start()

    async def down(_fields: dict[str, Any]) -> taskstore.Result:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(api, "create_task", down)
    await model_watch.after_review({"id": "r", "count": 0, "summary": ""}, NOW, "admin", "s1")
    for _ in range(8):
        await model_watch.retry_reminders(NOW)
    slot = model_watch.store().read()["models"]["claude-fable-5-2"]["notify"]
    assert slot["attempts"] == model_watch.MAX_NOTIFY_ATTEMPTS


def test_non_admins_get_no_host_or_version() -> None:
    model_watch.record(_report(_probe()), NOW)
    assert set(model_watch.view(False, NOW)) == {"models", "served_ids"}
    assert model_watch.view(True, NOW)["last_report"]["host"] == "box"


# -- the follow-up reminder (2026-11-11)


def _old_task(prefix: str = model_watch.KEY_PREFIX) -> dict[str, Any]:
    return {
        "id": "t1",
        "origin_surface": taskstore.ORIGIN_SURFACE,
        "notes": f"did not finish\n\n{prefix}claude-fable-5-2",
    }


async def _finish(at: datetime, session: str) -> list[str]:
    model_watch.bind_session("admin", session)
    review = {"id": "rev2", "count": 5, "summary": "Retention: not stated."}
    return await model_watch.after_review(review, at, "admin", session)


async def test_a_rerun_after_not_run_files_one_follow_up(api: FakeApi) -> None:
    _start()
    await model_watch.expire_reviews(NOW + timedelta(hours=3))
    assert len(api.created) == 1 and "did not finish" in api.created[0]["notes"]
    # The first task is still open: it must not stand in for the follow-up.
    api.open_tasks = [_old_task()]
    later = NOW + timedelta(hours=4)
    assert list(model_watch.rerun_not_run("admin", later)) == ["claude-fable-5-2"]
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "started" and stored["review"]["session"] is None
    assert await _finish(later, "s2") == ["claude-fable-5-2"]
    assert len(api.created) == 2
    follow = api.created[1]
    assert follow["title"] == "New Claude model: Fable 5.2 (review done)"
    assert "5 preset suggestions are waiting" in follow["notes"]
    assert follow["notes"].endswith("Model review key: claude-fable-5-2")
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["task_id"] == "t2" and stored["prior_task_id"] == "t1"
    assert stored["notify"]["status"] == "sent" and stored["notify"]["followup"] is True
    # A later rerun that expires again, then one that finishes, ring nothing more.
    model_watch._set_review(
        "claude-fable-5-2", {**stored["review"], "state": "not_run", "session": None}
    )
    model_watch.rerun_not_run("admin", later)
    assert await model_watch.expire_reviews(later + timedelta(hours=3)) == ["claude-fable-5-2"]
    model_watch.rerun_not_run("admin", later + timedelta(hours=4))
    await _finish(later + timedelta(hours=4), "s3")
    assert len(api.created) == 2


async def test_a_slot_stored_before_kinds_existed_still_gets_its_follow_up(api: FakeApi) -> None:
    _start()

    def legacy(state: dict[str, Any]) -> None:
        entry = state["models"]["claude-fable-5-2"]
        entry["review"] = {**entry["review"], "state": "not_run"}
        entry["task_id"] = "old"
        entry["notify"] = {
            "status": "sent",
            "attempts": 0,
            "last_error": None,
            "text": "Claude Fable 5.2 answers on your plan, but the preset review did not finish.",
        }

    model_watch.store().update(legacy)
    model_watch.rerun_not_run("admin", NOW)
    await _finish(NOW, "s2")
    assert len(api.created) == 1 and "(review done)" in api.created[0]["title"]
    assert model_watch.store().read()["models"]["claude-fable-5-2"]["prior_task_id"] == "old"


async def test_a_done_reminder_is_never_followed_up(api: FakeApi) -> None:
    _start()
    await _finish(NOW, "s1")
    assert len(api.created) == 1
    model_watch._set_review(
        "claude-fable-5-2",
        {"state": "started", "at": model_watch._iso(NOW), "owner": "admin", "session": "s9"},
    )
    await model_watch.expire_reviews(NOW + timedelta(hours=3))
    model_watch.rerun_not_run("admin", NOW + timedelta(hours=3))
    await _finish(NOW + timedelta(hours=3), "s9b")
    assert len(api.created) == 1


def test_another_users_run_restarts_nothing() -> None:
    _start()
    model_watch._set_review(
        "claude-fable-5-2", {"state": "not_run", "at": model_watch._iso(NOW), "owner": "admin"}
    )
    assert model_watch.rerun_not_run("someone-else", NOW) == {}
    assert model_watch.rerun_not_run("", NOW) == {}
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "not_run"


async def test_a_restart_that_lost_the_follow_up_task_id_files_nothing_twice(
    api: FakeApi,
) -> None:
    api.open_tasks = [{**_old_task(model_watch.FOLLOWUP_KEY_PREFIX), "id": "t7"}]
    model_watch.record(_report(_probe()), NOW)
    assert await model_watch.notify("claude-fable-5-2", "text", NOW, followup=True) is None
    assert api.created == []
    assert model_watch.store().read()["models"]["claude-fable-5-2"]["task_id"] == "t7"


def test_undo_puts_back_only_reviews_no_reader_bound() -> None:
    _start()
    _start("claude-opus-6")
    for model_id in ("claude-fable-5-2", "claude-opus-6"):
        model_watch._set_review(
            model_id, {"state": "not_run", "at": "2026-11-10T09:00:00Z", "owner": "admin"}
        )
    previous = model_watch.rerun_not_run("admin", NOW)
    model_watch.bind_session("admin", "s5")
    model_watch._set_review(
        "claude-opus-6", {"state": "started", "at": model_watch._iso(NOW), "owner": "admin"}
    )
    model_watch.undo_rerun(previous)
    models = model_watch.store().read()["models"]
    assert models["claude-fable-5-2"]["review"]["session"] == "s5"
    assert models["claude-opus-6"]["review"]["state"] == "not_run"
    assert models["claude-opus-6"]["review"]["at"] == "2026-11-10T09:00:00Z"


async def test_a_review_that_finishes_while_its_not_run_reminder_files_still_follows_up(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start()
    real = api.create_task
    gate = asyncio.Event()

    async def slow(fields: dict[str, Any]) -> taskstore.Result:
        await gate.wait()
        return await real(fields)

    monkeypatch.setattr(api, "create_task", slow)
    expiring = asyncio.create_task(model_watch.expire_reviews(NOW + timedelta(hours=3)))
    await asyncio.sleep(0.01)
    model_watch.rerun_not_run("admin", NOW + timedelta(hours=3))
    finishing = asyncio.create_task(_finish(NOW + timedelta(hours=3), "s2"))
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(expiring, finishing)
    assert [t["title"] for t in api.created] == [
        "New Claude model: Fable 5.2",
        "New Claude model: Fable 5.2 (review done)",
    ]
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["notify"]["status"] == "sent" and stored["task_id"] == "t2"


async def test_a_follow_up_that_failed_is_retried_as_the_follow_up(
    api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    _start()
    await model_watch.expire_reviews(NOW + timedelta(hours=3))
    real = api.create_task
    boom = {"on": True}

    async def flaky(fields: dict[str, Any]) -> taskstore.Result:
        if boom["on"]:
            raise httpx.ConnectError("down")
        return await real(fields)

    monkeypatch.setattr(api, "create_task", flaky)
    later = NOW + timedelta(hours=4)
    model_watch.rerun_not_run("admin", later)
    await _finish(later, "s2")
    assert len(api.created) == 1
    boom["on"] = False
    assert await model_watch.retry_reminders(later) == ["claude-fable-5-2"]
    follow = api.created[1]
    assert follow["title"].endswith("(review done)")
    assert follow["notes"].endswith("Model review key: claude-fable-5-2")


async def test_a_longer_ids_task_never_stands_in_for_a_shorter_id(api: FakeApi) -> None:
    api.open_tasks = [
        {
            "id": "other",
            "origin_surface": taskstore.ORIGIN_SURFACE,
            "notes": "x\n\nModel key: claude-opus-6-1",
        }
    ]
    model_watch.record(_report(_probe("claude-opus-6")), NOW)
    assert await model_watch.notify("claude-opus-6", "text", NOW) == "t1"
