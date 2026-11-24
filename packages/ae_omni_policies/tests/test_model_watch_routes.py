"""The model-watch routes: admin-only POST, every user's GET, the review trigger."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from test_preset_review_routes import (
    MEMBER,
    OWNER,
    World,
    _enable,
    _row,
    world,  # noqa: F401 (the fixture)
)

from ae_omni_policies import model_watch, preset_review, preset_suggestions
from ae_omni_policies import router as router_module


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


def _body(*probes: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": "box",
        "claude_code_version": "2.1.322",
        "scanned_at": "2026-11-10T11:58:00Z",
        "probes": list(probes),
    }


@pytest.fixture(autouse=True)
def _store(monkeypatch: pytest.MonkeyPatch) -> Any:
    model_watch.reset_store(model_watch.WatchStore())

    async def notes(model_id: str, **_kw: Any) -> dict[str, Any]:
        return {"url": "https://x.test", "excerpt": f"{model_id} notes", "status": "ok"}

    monkeypatch.setattr(model_watch, "fetch_notes", notes)
    yield
    model_watch.reset_store()


async def _settle() -> None:
    await asyncio.gather(*list(model_watch._background))


async def test_posting_is_admin_only_and_reading_is_for_every_user(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    world: World,  # noqa: F811
) -> None:
    refused = await client.post("/v1/ae/model-watch", json=_body(_probe()), headers=MEMBER)
    assert refused.status_code == 403
    assert model_watch.served_ids() == []
    ok = await client.post("/v1/ae/model-watch", json=_body(_probe()), headers=OWNER)
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"kept": True, "new": ["claude-fable-5-2"]}
    await _settle()
    seen = (await client.get("/v1/ae/model-watch", headers=MEMBER)).json()
    assert seen["served_ids"] == ["claude-fable-5-2"]
    assert seen["models"][0]["label"] == "Fable 5.2"
    assert "notes" not in seen["models"][0] and "report_stale" not in seen
    assert "last_report" not in seen and set(seen) == {"models", "served_ids"}
    admin = (await client.get("/v1/ae/model-watch", headers=OWNER)).json()
    assert admin["models"][0]["notes"]["status"] == "ok" and admin["report_stale"] is False
    assert admin["last_report"]["host"] == "box"


async def test_a_malformed_report_is_refused(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    world: World,  # noqa: F811
) -> None:
    bad = _body(_probe(id="claude-fable-5-2; drop"))
    assert (await client.post("/v1/ae/model-watch", json=bad, headers=OWNER)).status_code == 422
    assert (await client.post("/v1/ae/model-watch", content="x", headers=OWNER)).status_code in (
        415,
        422,
    )


async def test_a_new_id_starts_the_review_and_the_read_carries_the_notes(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    world: World,  # noqa: F811
) -> None:
    await _enable(client, OWNER)
    world.server.rows = [_row(id="task_1")]
    reviewer = world.reviewer(owner="owner@example.com")
    posted = await client.post("/v1/ae/model-watch", json=_body(_probe()), headers=OWNER)
    assert posted.json()["new"] == ["claude-fable-5-2"]
    await _settle()
    assert any(path.endswith("/run") for _m, path, _b in world.server.calls)
    assert preset_suggestions.store().peek_forced(
        "owner@example.com", datetime.now(UTC).timestamp()
    )
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "started"
    read = await client.post(f"/v1/ae/sessions/{reviewer}/preset-insights", json={}, headers=OWNER)
    assert read.status_code == 200, read.text
    (new_model,) = read.json()["new_models"]
    assert new_model["id"] == "claude-fable-5-2"
    assert new_model["release_notes_excerpt"] == "claude-fable-5-2 notes"


async def test_submitting_the_review_marks_it_done_and_files_the_reminder(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filed: list[tuple[str, str]] = []

    async def notify(model_id: str, text: str, _now: datetime, **_kw: Any) -> str:
        filed.append((model_id, text))
        model_watch._remember_task(model_id, "t1")
        return "t1"

    monkeypatch.setattr(model_watch, "notify", notify)
    await _enable(client, OWNER)
    world.server.rows = [_row(id="task_1")]
    reviewer = world.reviewer(owner="owner@example.com")
    await client.post("/v1/ae/model-watch", json=_body(_probe()), headers=OWNER)
    await _settle()
    await client.post(f"/v1/ae/sessions/{reviewer}/preset-insights", json={}, headers=OWNER)
    summary = "Retention: the notes say 30 days. Checked."
    done = await client.post(
        f"/v1/ae/sessions/{reviewer}/preset-suggestions",
        json={"summary": summary, "suggestions": []},
        headers=OWNER,
    )
    assert done.status_code == 200, done.text
    ((model_id, text),) = filed
    assert model_id == "claude-fable-5-2" and "Retention: the notes say 30 days." in text
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "done" and stored["review"]["review_id"]


async def test_a_review_that_cannot_start_is_recorded_with_the_reason(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filed: list[str] = []

    async def notify(_model_id: str, text: str, _now: datetime, **_kw: Any) -> None:
        filed.append(text)

    monkeypatch.setattr(model_watch, "notify", notify)
    # Insights are off for the admin, so run_now refuses.
    await client.post("/v1/ae/model-watch", json=_body(_probe()), headers=OWNER)
    await _settle()
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "not_run" and "off" in stored["review"]["why"]
    assert "did not start" in filed[0]


async def test_the_newer_model_nudge_counts_the_served_ids(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    world: World,  # noqa: F811
) -> None:
    assert "claude-fable-5-9" not in router_module._known_claude()
    await client.post("/v1/ae/model-watch", json=_body(_probe("claude-fable-5-9")), headers=OWNER)
    await _settle()
    assert "claude-fable-5-9" in router_module._known_claude()


async def test_run_now_is_what_the_run_route_answers_with(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    world: World,  # noqa: F811
) -> None:
    off = await client.post("/v1/ae/preset-review/run", json={}, headers=OWNER)
    assert off.status_code == 409 and off.json()["error"]["code"] == "insights_off"
    await _enable(client, OWNER)
    world.server.rows = [_row(id="task_1")]
    on = await client.post("/v1/ae/preset-review/run", json={}, headers=OWNER)
    assert on.status_code == 202 and on.json() == {"started": True}


async def test_a_hand_run_restarts_the_callers_model_reviews_that_did_not_run(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def notify(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(model_watch, "notify", notify)
    # Insights are off, so the model's review does not run.
    await client.post("/v1/ae/model-watch", json=_body(_probe()), headers=OWNER)
    await _settle()
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "not_run"
    await _enable(client, OWNER)
    world.server.rows = [_row(id="task_1")]
    seen: list[str] = []
    real_run = preset_review.run_now

    async def run_now(http: Any, who: str) -> preset_review.RunNow:
        # The reviewer may read as soon as the task fires: the model must be started by then.
        seen.append(model_watch.store().read()["models"]["claude-fable-5-2"]["review"]["state"])
        return await real_run(http, who)

    monkeypatch.setattr(preset_review, "run_now", run_now)
    on = await client.post("/v1/ae/preset-review/run", json={}, headers=OWNER)
    assert on.status_code == 202 and seen == ["started"]
    review = model_watch.store().read()["models"]["claude-fable-5-2"]["review"]
    assert review["state"] == "started" and review["session"] is None and review["why"] is None
    owner = review["owner"]
    model_watch.bind_session(owner, "s-hand")
    done = await model_watch.after_review(
        {"id": "rv", "count": 2, "summary": "Retention: x."}, datetime.now(UTC), owner, "s-hand"
    )
    assert done == ["claude-fable-5-2"]


async def test_a_hand_run_that_does_not_start_puts_the_reviews_back(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def notify(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(model_watch, "notify", notify)
    await client.post("/v1/ae/model-watch", json=_body(_probe()), headers=OWNER)
    await _settle()
    before = model_watch.store().read()["models"]["claude-fable-5-2"]["review"]
    assert before["state"] == "not_run"
    # Insights are still off, so the hand run is refused too.
    off = await client.post("/v1/ae/preset-review/run", json={}, headers=OWNER)
    assert off.status_code == 409
    assert model_watch.store().read()["models"]["claude-fable-5-2"]["review"] == before


def test_run_now_outcomes_say_whether_they_started() -> None:
    assert preset_review.RunNow(202).started
    assert not preset_review.RunNow(409, "insights_off", "off").started


async def test_another_users_submit_cannot_complete_the_admins_model_review(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filed: list[str] = []

    async def notify(model_id: str, _text: str, _now: datetime, **_kw: Any) -> str:
        filed.append(model_id)
        return "t1"

    monkeypatch.setattr(model_watch, "notify", notify)
    await _enable(client, OWNER)
    await _enable(client, MEMBER)
    world.server.rows = [_row(id="task_1")]
    admin_reviewer = world.reviewer("conv_admin", owner="owner@example.com")
    member_reviewer = world.reviewer("conv_member", owner="member@example.com")
    await client.post("/v1/ae/model-watch", json=_body(_probe()), headers=OWNER)
    await _settle()
    member_read = await client.post(
        f"/v1/ae/sessions/{member_reviewer}/preset-insights", json={}, headers=MEMBER
    )
    assert "new_models" not in member_read.json()
    submitted = await client.post(
        f"/v1/ae/sessions/{member_reviewer}/preset-suggestions",
        json={"summary": "Retention: none. Checked.", "suggestions": []},
        headers=MEMBER,
    )
    assert submitted.status_code == 200, submitted.text
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "started" and filed == []
    # The owner's own reviewer session completes it.
    read = await client.post(
        f"/v1/ae/sessions/{admin_reviewer}/preset-insights", json={}, headers=OWNER
    )
    assert read.json()["new_models"][0]["id"] == "claude-fable-5-2"
    done = await client.post(
        f"/v1/ae/sessions/{admin_reviewer}/preset-suggestions",
        json={"summary": "Retention: 30 days. Checked.", "suggestions": []},
        headers=OWNER,
    )
    assert done.status_code == 200, done.text
    assert filed == ["claude-fable-5-2"]
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "done"


async def test_a_running_weekly_review_leaves_the_model_pending_without_an_alarm(
    client: httpx.AsyncClient,
    ae_admins: set[str],
    world: World,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filed: list[str] = []

    async def notify(model_id: str, _text: str, _now: datetime, **_kw: Any) -> None:
        filed.append(model_id)

    async def busy(_http: Any, _who: str) -> preset_review.RunNow:
        return preset_review.RunNow(409, "review_running", "A review is already running.")

    monkeypatch.setattr(model_watch, "notify", notify)
    monkeypatch.setattr(preset_review, "run_now", busy)
    await client.post("/v1/ae/model-watch", json=_body(_probe()), headers=OWNER)
    await _settle()
    stored = model_watch.store().read()["models"]["claude-fable-5-2"]
    assert stored["review"]["state"] == "pending" and filed == []
