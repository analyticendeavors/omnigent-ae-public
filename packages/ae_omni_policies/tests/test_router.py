"""The /v1/ae routes on the real app (upstream's stores, respx for task-store)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from fastapi import FastAPI, Request
from omnigent.runtime import get_conversation_store
from tests.server.helpers import create_test_session

from ae_omni_policies import __version__, auth, deploy_hold, labels, slot_limit
from ae_omni_policies import router as router_module
from ae_omni_policies.taskstore import ORIGIN_SURFACE

BASE = "https://tasks.test"
LOT_ID = "01M1PARKINGLOT000000000000"
LOT = json.loads((Path(__file__).parent / "fixtures" / "lot.json").read_text(encoding="utf-8"))
PARK_BODY: dict[str, Any] = {
    "thread": "eur-currency-formatting",
    "state": "The formatting helper drops the EUR symbol.",
    "next": "Open shared/js/money.js and add the EUR branch.",
    "slot": "never opened",
    "quadrant": "feature",
    "est": "about an hour",
    "from": "pricing padding fix, Claude Code, 2026-09-23",
    "summary": "EUR currency formatting broken in the pricing table",
    "repo": "analyticendeavors/website_content",
    "tags": ["content"],
}
# A well-formed id that no row carries. A malformed one ("conv_missing") never
# reaches the route: upstream's store rejects it and the app answers its own
# 404 ("Not found."), which the gate-reply test relies on for the malformed case.
ABSENT_SESSION = "conv_" + "f" * 32
CREATED = {
    "id": "T-NEW",
    "title": "eur-currency-formatting",
    "quadrant": "q3",
    "estimate": "60plus",
}


async def _session(
    client: httpx.AsyncClient, title: str, seed: dict[str, str] | None = None
) -> str:
    snapshot = await create_test_session(client, title=title, labels=seed)
    return snapshot["id"]


def _labels(session_id: str) -> dict[str, str]:
    conv = get_conversation_store().get_conversation(session_id)
    assert conv is not None
    return dict(conv.labels)


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/ae/health")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "version": __version__,
        "task_store": {"url": BASE, "configured": True},
    }


async def test_identity_is_required_off_the_single_user_runtime(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    assert (await client.get("/v1/ae/health")).status_code == 401
    ok = await client.get("/v1/ae/health", headers={"X-Forwarded-Email": "reid@example.com"})
    assert ok.status_code == 200


async def test_capacity_buckets_every_session(client: httpx.AsyncClient) -> None:
    store = get_conversation_store()
    working = await _session(client, "Fix pricing padding", {labels.STATUS: "working"})
    waiting = await _session(
        client, "Renewal note", {labels.STATUS: "working", labels.NEXT: "send it"}
    )
    store.set_pending_elicitation_count(waiting, 1)
    review = await _session(client, "Done thing", {labels.STATUS: "review"})
    parked = await _session(client, "Parked thing", {labels.STATUS: "parked", labels.TASK: "T1"})
    reference = await _session(client, "Lookup", {labels.STATUS: "reference"})
    running = await _session(client, "Unplaced")
    store.set_session_live_status(running, "running")
    idle = await _session(client, "Idle unplaced")

    response = await client.get("/v1/ae/capacity")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["limit"] == labels.SLOT_LIMIT == 3
    assert body["slots_used"] == 1
    assert [row["id"] for row in body["working"]] == [working]
    assert [row["id"] for row in body["blocked"]] == [waiting]
    assert body["blocked"][0]["next"] == "send it"
    assert body["blocked"][0]["blocked"] is True
    assert [row["id"] for row in body["review"]] == [review]
    assert [row["id"] for row in body["parked"]] == [parked]
    assert body["parked"][0]["task"] == "T1"
    assert [row["id"] for row in body["reference"]] == [reference]
    assert [row["id"] for row in body["unlabelled_running"]] == [running]
    listed = {row["id"] for bucket in body.values() if isinstance(bucket, list) for row in bucket}
    assert idle not in listed
    first = body["working"][0]
    assert first["title"] == "Fix pricing padding"
    assert set(first) >= {
        "id",
        "title",
        "status",
        "elapsed",
        "elapsed_s",
        "started",
        "gate",
        "fork",
    }


async def test_capacity_shows_a_failed_session_as_blocked(client: httpx.AsyncClient) -> None:
    # 2026-09-24: omni_capacity showed 3 of 3 with one session failed for 6h 34m.
    store = get_conversation_store()
    working = await _session(client, "Alive", {labels.STATUS: "working"})
    crashed = await _session(client, "Crashed", {labels.STATUS: "working"})
    store.set_session_live_status(crashed, "failed")

    body = (await client.get("/v1/ae/capacity")).json()
    assert body["slots_used"] == 1
    assert [row["id"] for row in body["working"]] == [working]
    assert [row["id"] for row in body["blocked"]] == [crashed]
    row = body["blocked"][0]
    assert (row["failed"], row["blocked"], row["live_status"]) == (True, False, "failed")
    # Derived, never written: the stored status is still working.
    assert _labels(crashed)[labels.STATUS] == "working"


async def test_capacity_reports_the_configured_limit(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AE_SLOT_LIMIT=4 in config/server.env becomes labels.SLOT_LIMIT at startup;
    # the route reads it per request, and the web UI's counts follow the route.
    monkeypatch.setattr(labels, "SLOT_LIMIT", 4)
    response = await client.get("/v1/ae/capacity")
    assert response.status_code == 200, response.text
    assert response.json()["limit"] == 4


class _SlotCapStore:
    """A policy store holding one enabled slot cap with *limit* (what slot_limit reads)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit

    def list_defaults(self) -> list[Any]:
        return [
            SimpleNamespace(
                handler=slot_limit.HANDLER, enabled=True, factory_params={"limit": self.limit}
            )
        ]


async def test_capacity_reports_the_limit_set_on_the_settings_page(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-10-22: a limit on the slot cap (Settings > Global Policies) replaces
    # AE_SLOT_LIMIT for every count, so the Dashboard and the sidebar match the cap.
    monkeypatch.setattr(labels, "SLOT_LIMIT", 4)
    slot_limit.remember_store(_SlotCapStore(5))
    try:
        capacity = (await client.get("/v1/ae/capacity")).json()
        listed = (await client.get("/v1/ae/sessions")).json()
    finally:
        slot_limit.remember_store(None)
    assert (capacity["limit"], listed["limit"]) == (5, 5)


async def test_lot_maps_the_store(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        mock.get("/api/tasks", params={"status": "open"}).respond(200, json=LOT["tasks"])
        response = await client.get("/v1/ae/lot")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["list"] == {"id": LOT_ID, "name": "Parking Lot"}
    assert body["count"] == 2
    assert body["tasks"][0]["next"].startswith("Ask Alex in Teams:")
    assert body["tasks"][0]["repo"] == "analyticendeavors/example-books"
    assert body["tasks"][1]["repo"] is None


async def test_lot_reports_a_store_failure_as_502(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(302, headers={"location": "https://login.example/"})
        response = await client.get("/v1/ae/lot?status=all&limit=5")
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "task_store_unavailable"
    assert error["kind"] == "auth"
    assert error["status"] == 302
    assert "Cloudflare Access redirected" in error["message"]


async def test_lot_rejects_a_bad_status(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/ae/lot?status=weird")).status_code == 422


async def test_unconfigured_store_is_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_CF_ACCESS_CLIENT_SECRET")
    response = await client.get("/v1/ae/lot")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "task_store_not_configured"
    assert "AE_CF_ACCESS_CLIENT_SECRET" in error["message"]
    health = await client.get("/v1/ae/health")
    assert health.json()["task_store"]["configured"] is False


async def test_park_files_and_marks_the_session(client: httpx.AsyncClient) -> None:
    session_id = await _session(client, "Pricing padding", {labels.STATUS: "working"})
    with respx.mock(base_url=BASE, assert_all_called=True) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        created = mock.post("/api/tasks").respond(201, json=CREATED)
        mock.patch("/api/tasks/T-NEW").respond(200, json=CREATED)
        response = await client.post(
            "/v1/ae/lot/park", json={**PARK_BODY, "session_id": session_id}
        )
        sent = json.loads(created.calls[0].request.content)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "filed"
    assert body["task_id"] == "T-NEW"
    assert body["url"] == f"{BASE}/#/all?list={LOT_ID}&q=eur-currency-formatting"
    assert body["session"] == {
        "id": session_id,
        "labels": {labels.STATUS: "parked", labels.TASK: "T-NEW"},
    }
    assert sent["origin_surface"] == ORIGIN_SURFACE
    assert sent["tags"] == ["parked", "content"]
    assert sent["bucket_id"] == LOT_ID
    after = _labels(session_id)
    assert after[labels.STATUS] == "parked"
    assert after[labels.TASK] == "T-NEW"


async def test_park_without_a_session_touches_no_labels(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        mock.post("/api/tasks").respond(201, json=CREATED)
        mock.patch("/api/tasks/T-NEW").respond(200, json=CREATED)
        response = await client.post("/v1/ae/lot/park", json=PARK_BODY)
    assert response.status_code == 200, response.text
    assert "session" not in response.json()


async def test_park_unknown_session_is_404_before_filing(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        buckets = mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        response = await client.post(
            "/v1/ae/lot/park", json={**PARK_BODY, "session_id": ABSENT_SESSION}
        )
        assert buckets.call_count == 0
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": f"no session {ABSENT_SESSION}"}
    }


async def test_park_unfiled_is_502_with_the_note(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(302, headers={"location": "https://login.example/"})
        mock.post("/api/tasks").respond(302, headers={"location": "https://login.example/"})
        response = await client.post("/v1/ae/lot/park", json=PARK_BODY)
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "task_store_unfiled"
    assert error["outcome"] == "unfiled"
    assert "Cloudflare Access redirected" in error["message"]
    assert error["note"].startswith("[analyticendeavors/website_content]")
    assert error["instruction"].startswith("Report this as UNFILED")


@pytest.mark.parametrize(
    "broken",
    [
        {**PARK_BODY, "thread": ""},
        {key: value for key, value in PARK_BODY.items() if key != "from"},
        {**PARK_BODY, "quadrant": "q3"},
        {**PARK_BODY, "priority": 9},
    ],
)
async def test_park_validates_the_body(client: httpx.AsyncClient, broken: dict[str, Any]) -> None:
    assert (await client.post("/v1/ae/lot/park", json=broken)).status_code == 422


async def test_resume_returns_the_block(client: httpx.AsyncClient) -> None:
    task = LOT["tasks"]["tasks"][0]
    with respx.mock(base_url=BASE) as mock:
        mock.get(f"/api/tasks/{task['id']}").respond(200, json=task)
        response = await client.post(f"/v1/ae/lot/{task['id']}/resume")
        assert [call.request.method for call in mock.calls] == ["GET"]
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["task_id"] == task["id"]
    assert body["block"].startswith("THREAD: contoso-vendor-category-check\n")
    assert body["block"].endswith("&q=contoso-vendor-category-check")
    assert body["fenced"] == f"```\n{body['block']}\n```"


async def test_resume_without_a_block_or_task_is_404(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tasks/plain").respond(200, json={"id": "plain", "notes": "just prose"})
        mock.get("/api/tasks/gone").respond(404, json={"error": "no task with id gone"})
        plain = await client.post("/v1/ae/lot/plain/resume")
        gone = await client.post("/v1/ae/lot/gone/resume")
    assert plain.status_code == 404
    assert plain.json()["error"]["message"] == (
        "task plain has no Resume in Claude block in its notes"
    )
    assert gone.status_code == 404
    assert gone.json()["error"] == {"code": "not_found", "message": "no task gone"}


class _Loopback:
    """Stands in for the in-process post; records what would reach the events route."""

    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def __call__(self, request: Request, path: str, body: dict[str, Any]) -> httpx.Response:
        self.calls.append((path, body, router_module.forwarded_headers(request)))
        return httpx.Response(self.status, json={"ok": self.status < 300})


@pytest.mark.parametrize(("route", "text"), [("A", "A"), ("C", "C"), ("close", "close it")])
async def test_gate_reply_posts_the_letter(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, route: str, text: str
) -> None:
    loopback = _Loopback()
    monkeypatch.setattr(router_module, "_loopback_post", loopback)
    session_id = await _session(client, "Gated", {labels.GATE: "open", labels.STATUS: "working"})

    response = await client.post(
        f"/v1/ae/gate/{session_id}/reply",
        json={"route": route},
        headers={"cookie": "__Host-ap_session=abc", "x-forwarded-email": "reid@example.com"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"posted": True, "route": route, "text": text, "status": 202}
    path, body, headers = loopback.calls[0]
    assert path == f"/v1/sessions/{session_id}/events"
    assert body == {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }
    assert headers["cookie"] == "__Host-ap_session=abc"
    assert headers["x-forwarded-email"] == "reid@example.com"
    assert _labels(session_id)[labels.GATE] == "answered"


async def test_gate_reply_failure_leaves_the_gate_open(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module, "_loopback_post", _Loopback(status=503))
    session_id = await _session(client, "Gated", {labels.GATE: "open"})
    response = await client.post(f"/v1/ae/gate/{session_id}/reply", json={"route": "B"})
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "gate_reply_not_posted"
    assert "posting the reply failed: 503" in error["message"]
    assert _labels(session_id)[labels.GATE] == "open"


async def test_a_transport_failure_never_shows_the_loopback_url(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # httpx names the URL it tried; the caller gets the step and the kind of failure only.
    async def down(*_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("cannot connect to http://127.0.0.1:8000/v1/sessions/x/events")

    monkeypatch.setattr(router_module, "_loopback_post", down)
    session_id = await _session(client, "Gated", {labels.GATE: "open"})
    response = await client.post(f"/v1/ae/gate/{session_id}/reply", json={"route": "B"})
    assert response.status_code == 502
    message = response.json()["error"]["message"]
    assert message.startswith("posting the reply failed: could not reach the server; try again")
    assert "127.0.0.1" not in message


async def test_gate_reply_rejects_unknown_sessions_and_routes(client: httpx.AsyncClient) -> None:
    absent = await client.post(f"/v1/ae/gate/{ABSENT_SESSION}/reply", json={"route": "A"})
    assert absent.status_code == 404
    assert absent.json()["error"]["message"] == f"no session {ABSENT_SESSION}"
    malformed = await client.post("/v1/ae/gate/conv_missing/reply", json={"route": "A"})
    assert malformed.status_code == 404
    assert malformed.json()["error"]["code"] == "not_found"
    session_id = await _session(client, "Gated")
    bad_route = await client.post(f"/v1/ae/gate/{session_id}/reply", json={"route": "D"})
    assert bad_route.status_code == 422


async def test_loopback_reaches_the_app_as_the_caller() -> None:
    echo = FastAPI()

    @echo.post("/v1/echo")
    async def _echo(request: Request) -> dict[str, Any]:
        return {"headers": dict(request.headers), "body": await request.json()}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/ae/gate/x/reply",
        "headers": [
            (b"cookie", b"__Host-ap_session=abc"),
            (b"authorization", b"Bearer t"),
            (b"origin", b"https://ae.example"),
            (b"x-forwarded-email", b"reid@example.com"),
            (b"x-other", b"dropped"),
        ],
        "app": echo,
    }
    response = await router_module._loopback_post(Request(scope), "/v1/echo", {"hello": "there"})
    assert response.status_code == 200
    got = response.json()
    assert got["body"] == {"hello": "there"}
    assert got["headers"]["cookie"] == "__Host-ap_session=abc"
    assert got["headers"]["authorization"] == "Bearer t"
    assert got["headers"]["origin"] == "https://ae.example"
    assert got["headers"]["x-forwarded-email"] == "reid@example.com"
    assert "x-other" not in got["headers"]


@pytest.mark.parametrize("seed", [None, {labels.GATE: "answered"}])
async def test_gate_reply_needs_an_open_gate(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, seed: dict[str, str] | None
) -> None:
    loopback = _Loopback()
    monkeypatch.setattr(router_module, "_loopback_post", loopback)
    session_id = await _session(client, "Not gated", seed)
    response = await client.post(f"/v1/ae/gate/{session_id}/reply", json={"route": "A"})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert error["message"].startswith(f"no open gate on session {session_id}")
    assert loopback.calls == []


# ── Phase 5 (2026-09-23): the two routes the MCP server's worker tools need ──


async def test_capacity_carries_the_clock(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_TASK_STORE_TZ", raising=False)
    response = await client.get("/v1/ae/capacity")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["timezone"] == "UTC", "the fallback since 2026-10-22"
    monkeypatch.setenv("AE_TASK_STORE_TZ", "America/Los_Angeles")
    body = (await client.get("/v1/ae/capacity")).json()
    assert body["timezone"] == "America/Los_Angeles"
    assert body["now"].startswith(body["today"])
    assert len(body["today"]) == 10


async def test_schedule_defaults_name_the_server_clock_and_project(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_TASK_STORE_TZ", raising=False)
    monkeypatch.delenv("OMNIGENT_SCHEDULED_TASK_DEFAULT_PROJECT", raising=False)
    bare = await client.get("/v1/ae/schedule-defaults")
    assert bare.status_code == 200, bare.text
    assert bare.json() == {"timezone": "UTC", "project": None}
    monkeypatch.setenv("AE_TASK_STORE_TZ", "America/Los_Angeles")
    monkeypatch.setenv("OMNIGENT_SCHEDULED_TASK_DEFAULT_PROJECT", " Automation Notes ")
    body = (await client.get("/v1/ae/schedule-defaults")).json()
    assert body == {"timezone": "America/Los_Angeles", "project": "Automation Notes"}


async def test_lot_tags_maps_the_store(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tags").respond(
            200,
            json={
                "tags": [{"name": "ae"}, {"name": "content"}, {"name": "unused"}],
                "counts": {"ae": {"tasks": 4}, "content": 9},
            },
        )
        response = await client.get("/v1/ae/lot/tags")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "count": 3,
        "tags": [
            {"name": "content", "tasks": 9, "color": None, "icon": None},
            {"name": "ae", "tasks": 4, "color": None, "icon": None},
            {"name": "unused", "tasks": None, "color": None, "icon": None},
        ],
    }


async def test_lot_tags_with_icons_carries_the_apps_colours_and_paths(
    client: httpx.AsyncClient,
) -> None:
    # 2026-09-25 (issue 28): the Dashboard paints tags the way the app does.
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tags").respond(
            200,
            json={
                "tags": [
                    {"name": "ae", "icon": "briefcase", "color": "teal"},
                    {"name": "content", "icon": None, "color": "orange"},
                ],
                "colors": ["teal", "orange"],
                "counts": {"ae": {"tasks": 4}, "content": {"tasks": 9}},
            },
        )
        icon_set = mock.get("/tabler-icons.json").respond(
            200, json={"briefcase": ["M3 9h18", "M8 7v-2"], "unused": ["M1 1"]}
        )
        response = await client.get("/v1/ae/lot/tags?icons=true")
        again = await client.get("/v1/ae/lot/tags?icons=true")
        # The set is read once and kept, not a megabyte per refresh.
        assert icon_set.call_count == 1
        assert icon_set.calls[0].request.headers["CF-Access-Client-Id"] == (
            "test-client-id.access"
        )
    assert response.status_code == 200, response.text
    assert response.json() == again.json()
    assert response.json() == {
        "count": 2,
        "tags": [
            {"name": "content", "tasks": 9, "color": "orange", "icon": None},
            {"name": "ae", "tasks": 4, "color": "teal", "icon": "briefcase"},
        ],
        "icons": {"briefcase": ["M3 9h18", "M8 7v-2"]},
    }


async def test_lot_tags_without_the_icon_set_still_colours_the_tags(
    client: httpx.AsyncClient,
) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tags").respond(
            200, json={"tags": [{"name": "ae", "icon": "briefcase", "color": "teal"}]}
        )
        icon_set = mock.get("/tabler-icons.json").respond(302, headers={"location": "/login"})
        response = await client.get("/v1/ae/lot/tags?icons=true")
        await client.get("/v1/ae/lot/tags?icons=true")
        # A failed read is remembered, not retried on every refresh.
        assert icon_set.call_count == 1
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tags"] == [{"name": "ae", "tasks": None, "color": "teal", "icon": "briefcase"}]
    assert body["icons"] == {}
    assert "could not be read" in body["icons_note"]


async def test_lot_tags_reports_a_store_failure_as_502(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tags").respond(503, json={"error": "down"})
        response = await client.get("/v1/ae/lot/tags")
    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "task_store_unavailable"
    assert error["kind"] == "server"
    assert error["message"] == "Could not read the tags: 503 down"


async def test_link_attaches_the_url(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        link = mock.post("/api/tasks/T-NEW/links").respond(201, json={"id": "L1"})
        response = await client.post(
            "/v1/ae/lot/T-NEW/link", json={"url": "https://github.com/a/b/issues/7"}
        )
        sent = json.loads(link.calls[0].request.content)
    assert response.status_code == 200, response.text
    assert sent == {
        "kind": "url",
        "url": "https://github.com/a/b/issues/7",
        "label": "GitHub issue",
    }
    assert response.json() == {
        "outcome": "linked",
        "task_id": "T-NEW",
        "issue_url": "https://github.com/a/b/issues/7",
        "link_id": "L1",
    }


GH_API = "https://api.github.com"
PROSPECT = LOT["tasks"]["tasks"][0]


async def test_complete_marks_done_and_closes_the_issue(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_GITHUB_TOKEN", "gh-test")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    issue = "/repos/analyticendeavors/example-books/issues/148"
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{BASE}/api/tasks/{PROSPECT['id']}").respond(200, json=PROSPECT)
        patched = mock.patch(f"{BASE}/api/tasks/{PROSPECT['id']}").respond(200, json=PROSPECT)
        comment = mock.post(f"{GH_API}{issue}/comments").respond(201, json={"id": 1})
        closed = mock.patch(f"{GH_API}{issue}").respond(200, json={"state": "closed"})
        response = await client.post(
            f"/v1/ae/lot/{PROSPECT['id']}/complete",
            json={"summary": "Reclassified in https://github.com/a/b/pull/154."},
        )
        assert json.loads(patched.calls[0].request.content) == {"status": "done"}
        assert json.loads(comment.calls[0].request.content) == {
            "body": "Completed from OmniGent. Reclassified in https://github.com/a/b/pull/154."
        }
        assert json.loads(closed.calls[0].request.content) == {
            "state": "closed",
            "state_reason": "completed",
        }
    assert response.status_code == 200, response.text
    assert response.json() == {
        "outcome": "completed",
        "task_id": PROSPECT["id"],
        "title": PROSPECT["title"],
        "status": "done",
        "issue": {
            "url": "https://github.com/analyticendeavors/example-books/issues/148",
            "closed": True,
        },
    }


async def test_complete_dropped_closes_as_not_planned(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_GITHUB_TOKEN", "gh-test")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    issue = "/repos/analyticendeavors/example-books/issues/148"
    with respx.mock() as mock:
        mock.get(f"{BASE}/api/tasks/{PROSPECT['id']}").respond(200, json=PROSPECT)
        patched = mock.patch(f"{BASE}/api/tasks/{PROSPECT['id']}").respond(200, json=PROSPECT)
        mock.post(f"{GH_API}{issue}/comments").respond(201, json={"id": 1})
        closed = mock.patch(f"{GH_API}{issue}").respond(200, json={"state": "closed"})
        response = await client.post(
            f"/v1/ae/lot/{PROSPECT['id']}/complete", json={"status": "dropped"}
        )
        assert json.loads(patched.calls[0].request.content) == {"status": "dropped"}
        assert json.loads(closed.calls[0].request.content)["state_reason"] == "not_planned"
    assert response.json()["issue"]["closed"] is True


async def test_complete_falls_back_to_the_sync_when_github_refuses(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_GITHUB_TOKEN", "gh-test")
    monkeypatch.delenv("AE_GITHUB_API_URL", raising=False)
    issue = "/repos/analyticendeavors/example-books/issues/148"
    with respx.mock() as mock:
        mock.get(f"{BASE}/api/tasks/{PROSPECT['id']}").respond(200, json=PROSPECT)
        mock.patch(f"{BASE}/api/tasks/{PROSPECT['id']}").respond(200, json=PROSPECT)
        mock.post(f"{GH_API}{issue}/comments").respond(
            403, json={"message": "Resource not accessible by personal access token"}
        )
        response = await client.post(f"/v1/ae/lot/{PROSPECT['id']}/complete", json={})
    body = response.json()
    assert body["outcome"] == "completed"
    assert body["issue"]["closed"] is False
    assert "403" in body["issue"]["note"]
    assert body["issue"]["note"].endswith(router_module.ISSUE_FALLBACK)


async def test_complete_without_a_token_or_an_issue(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_GITHUB_TOKEN", raising=False)
    bare = {**PROSPECT, "links": []}
    answers = {}
    for name, task in (("no_token", PROSPECT), ("no_issue", bare)):
        with respx.mock(base_url=BASE) as mock:
            mock.get(f"/api/tasks/{PROSPECT['id']}").respond(200, json=task)
            mock.patch(f"/api/tasks/{PROSPECT['id']}").respond(200, json=task)
            answer = await client.post(f"/v1/ae/lot/{PROSPECT['id']}/complete", json={})
            answers[name] = answer.json()
    no_token, no_issue = answers["no_token"], answers["no_issue"]
    assert no_token["issue"]["closed"] is False
    assert no_token["issue"]["note"].startswith("No GitHub token here.")
    assert no_issue["issue"] == {"url": None, "closed": False, "note": "The task links no issue."}


async def test_complete_reports_a_task_the_store_refused(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tasks/T-GONE").respond(404, json={"error": "no task T-GONE"})
        response = await client.post("/v1/ae/lot/T-GONE/complete", json={})
        bad = await client.post("/v1/ae/lot/T-GONE/complete", json={"status": "open"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "outcome": "not_completed",
        "task_id": "T-GONE",
        "reason": "404 no task T-GONE",
    }
    assert bad.status_code == 422


async def test_link_not_linked_is_an_outcome_not_an_error(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.post("/api/tasks/T-GONE/links").respond(404, json={"error": "no task T-GONE"})
        response = await client.post(
            "/v1/ae/lot/T-GONE/link", json={"url": "https://x/1", "label": "PR"}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "not_linked"
    assert body["reason"] == "404 no task T-GONE"
    assert body["issue_url"] == "https://x/1"
    assert body["instruction"].startswith("The task and the issue both exist")
    assert (await client.post("/v1/ae/lot/T-NEW/link", json={"label": "x"})).status_code == 422


# ── 2026-09-24: Resume starts the session itself ──

AGENTS = {
    "data": [
        {"id": "ag_sdk", "name": "claude-sdk-ui"},
        {"id": "ag_native", "name": "claude-native-ui"},
        {"id": "ag_codex", "name": "codex-native-ui"},
    ]
}
BOX = {
    "host_id": "host_box",
    "name": "ae-llm-box",
    "status": "online",
    "sandbox_provider": None,
}


class _App:
    """Stands in for the loopback: answers the four upstream routes Resume calls."""

    def __init__(
        self,
        hosts: list[dict[str, Any]] | None = None,
        create_status: int = 201,
        post_status: int = 202,
    ) -> None:
        self.hosts = [BOX] if hosts is None else hosts
        self.create_status = create_status
        self.post_status = post_status
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def __call__(
        self, request: Request, method: str, path: str, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        self.calls.append((method, path, body))
        if (method, path) == ("GET", "/v1/agents?limit=1000"):
            return httpx.Response(200, json=AGENTS)
        if (method, path) == ("GET", "/v1/hosts"):
            return httpx.Response(200, json={"hosts": self.hosts})
        if (method, path) == ("POST", "/v1/sessions"):
            return httpx.Response(self.create_status, json={"id": "conv_resumed"})
        if (method, path) == ("POST", "/v1/sessions/conv_resumed/events"):
            return httpx.Response(self.post_status, json={})
        return httpx.Response(404, json={"detail": "not stubbed"})


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The host's home directory, without a live host connection."""
    asked: list[str] = []

    async def _home(request: Request, host_id: str) -> str | None:
        asked.append(host_id)
        return "/home/reid"

    monkeypatch.setattr(router_module, "_host_home", _home)
    return asked


def _task_mock(mock: respx.MockRouter) -> dict[str, Any]:
    task = LOT["tasks"]["tasks"][0]
    mock.get(f"/api/tasks/{task['id']}").respond(200, json=task)
    return task


async def test_resume_session_creates_and_posts_the_block(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, home: list[str]
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    with respx.mock(base_url=BASE) as mock:
        task = _task_mock(mock)
        response = await client.post(f"/v1/ae/lot/{task['id']}/session")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == "conv_resumed"
    assert body["host"] == {"id": "host_box", "name": "ae-llm-box"}
    assert body["agent"] == {"id": "ag_native", "name": "claude-native-ui"}
    assert body["workspace"] == "/home/reid"
    assert home == ["host_box"]
    create = app.calls[2]
    assert create[:2] == ("POST", "/v1/sessions")
    assert create[2] == {
        "agent_id": "ag_native",
        "host_id": "host_box",
        "workspace": "/home/reid",
        "labels": {
            labels.TASK: task["id"],
            "omnigent.ui": "terminal",
            "omnigent.wrapper": "claude-code-native-ui",
        },
        "title": task["title"],
    }
    method, path, event = app.calls[3]
    assert (method, path) == ("POST", "/v1/sessions/conv_resumed/events")
    # The fixture task links https://github.com/analyticendeavors/example-books/issues/148.
    closing = (
        f"When this work is finished, call the `complete_task` tool with task_id "
        f"`{task['id']}`. It marks this parked task done and closes "
        "https://github.com/analyticendeavors/example-books/issues/148."
    )
    text = f"{body['fenced']}\n\n{closing}"
    assert event == {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }
    assert body["fenced"].startswith("```\nTHREAD: contoso-vendor-category-check\n")


@pytest.mark.parametrize(
    "links",
    [
        [],
        [{"url": "https://github.com/analyticendeavors/omnigent-ae/pull/41", "label": "PR"}],
        [{"url": "https://gitlab.com/analyticendeavors/omnigent-ae/issues/3", "label": "x"}],
    ],
)
async def test_resume_session_without_an_issue_posts_the_block_alone(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    home: list[str],
    links: list[dict[str, str]],
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    task = {**LOT["tasks"]["tasks"][0], "links": links}
    with respx.mock(base_url=BASE) as mock:
        mock.get(f"/api/tasks/{task['id']}").respond(200, json=task)
        response = await client.post(f"/v1/ae/lot/{task['id']}/session")
    assert response.status_code == 200, response.text
    text = app.calls[3][2]["data"]["content"][0]["text"]
    assert text == (
        f"{response.json()['fenced']}\n\nWhen this work is finished, call the `complete_task` "
        f"tool with task_id `{task['id']}`. It marks this parked task done."
    )


@pytest.mark.parametrize(
    ("hosts", "env", "reason"),
    [
        ([], None, "no host is online"),
        ([{**BOX, "status": "offline"}], None, "no host is online"),
        ([{**BOX, "sandbox_provider": "modal"}], None, "no host is online"),
        ([BOX, {**BOX, "host_id": "host_mac", "name": "mac"}], None, "2 hosts are online"),
        ([BOX], "elsewhere", "host elsewhere (AE_RESUME_HOST) is not online"),
    ],
)
async def test_resume_session_without_a_host_carries_the_block(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    home: list[str],
    hosts: list[dict[str, Any]],
    env: str | None,
    reason: str,
) -> None:
    if env:
        monkeypatch.setenv("AE_RESUME_HOST", env)
    app = _App(hosts=hosts)
    monkeypatch.setattr(router_module, "_loopback", app)
    with respx.mock(base_url=BASE) as mock:
        task = _task_mock(mock)
        response = await client.post(f"/v1/ae/lot/{task['id']}/session")
    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "resume_no_target"
    assert reason in error["message"]
    assert error["fenced"] == f"```\n{error['block']}\n```"
    assert [call[0] for call in app.calls] == ["GET", "GET"]


async def test_resume_session_picks_the_named_host_and_workspace(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, home: list[str]
) -> None:
    monkeypatch.setenv("AE_RESUME_HOST", "ae-llm-box")
    monkeypatch.setenv("AE_RESUME_WORKSPACE", "/home/reid/GitHub")
    app = _App(hosts=[BOX, {**BOX, "host_id": "host_mac", "name": "mac"}])
    monkeypatch.setattr(router_module, "_loopback", app)
    with respx.mock(base_url=BASE) as mock:
        task = _task_mock(mock)
        response = await client.post(f"/v1/ae/lot/{task['id']}/session")
    assert response.status_code == 200, response.text
    assert response.json()["workspace"] == "/home/reid/GitHub"
    assert home == []
    assert app.calls[2][2]["host_id"] == "host_box"


async def test_resume_session_without_a_home_or_agent(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _no_home(request: Request, host_id: str) -> str | None:
        return None

    monkeypatch.setattr(router_module, "_host_home", _no_home)
    monkeypatch.setattr(router_module, "_loopback", _App())
    with respx.mock(base_url=BASE) as mock:
        task = _task_mock(mock)
        no_home = await client.post(f"/v1/ae/lot/{task['id']}/session")
        monkeypatch.setenv("AE_RESUME_AGENT", "missing-agent")
        no_agent = await client.post(f"/v1/ae/lot/{task['id']}/session")
    assert no_home.status_code == 503
    assert "set AE_RESUME_WORKSPACE" in no_home.json()["error"]["message"]
    assert no_agent.status_code == 503
    assert "no built-in agent named missing-agent" in no_agent.json()["error"]["message"]


async def test_resume_session_create_and_post_failures(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, home: list[str]
) -> None:
    with respx.mock(base_url=BASE) as mock:
        task = _task_mock(mock)
        monkeypatch.setattr(router_module, "_loopback", _App(create_status=422))
        not_created = await client.post(f"/v1/ae/lot/{task['id']}/session")
        monkeypatch.setattr(router_module, "_loopback", _App(post_status=409))
        not_posted = await client.post(f"/v1/ae/lot/{task['id']}/session")
    assert not_created.status_code == 502
    created_error = not_created.json()["error"]
    assert created_error["code"] == "resume_session_not_created"
    assert "session_id" not in created_error
    assert created_error["block"]
    assert not_posted.status_code == 502
    posted_error = not_posted.json()["error"]
    assert posted_error["code"] == "resume_block_not_posted"
    assert posted_error["session_id"] == "conv_resumed"
    assert posted_error["block"]


async def test_resume_session_reports_a_missing_task(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tasks/gone").respond(404, json={"error": "no task with id gone"})
        response = await client.post("/v1/ae/lot/gone/session")
    assert response.status_code == 404
    assert response.json()["error"] == {"code": "not_found", "message": "no task gone"}


async def test_host_home_without_a_live_host(client: httpx.AsyncClient) -> None:
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "app": FastAPI()}
    assert await router_module._host_home(Request(scope), "host_box") is None


async def test_loopback_get_reaches_the_app_as_the_caller() -> None:
    echo = FastAPI()

    @echo.get("/v1/echo")
    async def _echo(request: Request) -> dict[str, Any]:
        return {"headers": dict(request.headers)}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/ae/lot/x/session",
        "headers": [(b"cookie", b"__Host-ap_session=abc")],
        "app": echo,
    }
    response = await router_module._loopback(Request(scope), "GET", "/v1/echo")
    assert response.status_code == 200
    headers = response.json()["headers"]
    assert headers["cookie"] == "__Host-ap_session=abc"
    assert "content-type" not in headers


async def test_resume_session_through_the_real_app(client: httpx.AsyncClient) -> None:
    """The real loopback and agents route: the test server seeds no Claude Code agent."""
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        task = _task_mock(mock)
        mock.route(host="ae-loopback").pass_through()
        response = await client.post(f"/v1/ae/lot/{task['id']}/session")
    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "resume_no_target"
    assert "no built-in agent named claude-native-ui" in error["message"]
    assert error["block"].startswith("THREAD: ")


async def test_resume_session_is_idempotent_while_the_first_is_live(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, home: list[str]
) -> None:
    """A second tap (a slow phone) opens the session the first one started."""
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    task = LOT["tasks"]["tasks"][0]
    live = await _session(
        client, "Resumed already", {labels.TASK: task["id"], labels.STATUS: "working"}
    )
    with respx.mock(base_url=BASE) as mock:
        _task_mock(mock)
        again = await client.post(f"/v1/ae/lot/{task['id']}/session")
    assert again.status_code == 200, again.text
    body = again.json()
    assert (body["session_id"], body["existing"], body["task_id"]) == (live, True, task["id"])
    assert body["block"].startswith("THREAD: ")
    assert app.calls == [], "nothing was created or posted"

    # Parked again later: Resume is a fresh start, not the shelved session.
    get_conversation_store().set_labels(live, {labels.STATUS: "parked"})
    with respx.mock(base_url=BASE) as mock:
        _task_mock(mock)
        fresh = await client.post(f"/v1/ae/lot/{task['id']}/session")
    assert fresh.json()["session_id"] == "conv_resumed"


# ── 2026-09-25: sessions the orchestrator starts ──

START = {
    "title": "Dashboard overflow at 375",
    "brief": "Fix the Dashboard overflow. Open a PR into main and reply with its URL.",
    "workspace": "~/repos/omnigent-ae",
    "key": "claude/dashboard-overflow",
    "repo": "AnalyticEndeavors/omnigent-ae",
    "branch": "claude/dashboard-overflow",
    "orchestrator": "conv_orch",
}


async def test_start_session_holds_a_slot_creates_and_briefs(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, home: list[str]
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    monkeypatch.setattr(router_module.time, "time", lambda: 1_800_000_000.0)
    response = await client.post("/v1/ae/sessions", json=START)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == "conv_resumed"
    assert body["existing"] is False
    assert body["path"] == "/c/conv_resumed"
    assert body["workspace"] == "/home/reid/repos/omnigent-ae"
    assert body["overflow"] is False
    assert home == ["host_box"]
    method, path, create = app.calls[2]
    assert (method, path) == ("POST", "/v1/sessions")
    assert create == {
        "agent_id": "ag_native",
        "host_id": "host_box",
        "workspace": "/home/reid/repos/omnigent-ae",
        "title": START["title"],
        "labels": {
            labels.STATUS: "working",
            labels.STARTED: "1800000000",
            labels.ORCH: "conv_orch",
            labels.ORCH_KEY: "claude/dashboard-overflow",
            labels.REPO: "analyticendeavors/omnigent-ae",
            labels.BRANCH: "claude/dashboard-overflow",
            "omnigent.ui": "terminal",
            "omnigent.wrapper": "claude-code-native-ui",
        },
    }
    method, path, event = app.calls[3]
    assert (method, path) == ("POST", "/v1/sessions/conv_resumed/events")
    assert event["data"]["content"][0]["text"] == START["brief"]
    # The hold is gone once the row exists: it would count the session twice.
    held, full, _, pending = router_module.slot_cap.reserve("orch:probe", [])
    assert (held, full, pending) == (True, False, 0)
    router_module.slot_cap.release("orch:probe")


async def test_start_session_codex_absolute_workspace_and_derived_key(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, home: list[str]
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    body = {"title": "Codex review", "brief": "Review it.", "harness": "codex"}
    body |= {"workspace": "/srv/repo/", "host": "ae-llm-box"}
    response = await client.post("/v1/ae/sessions", json=body)
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["agent"] == {"id": "ag_codex", "name": "codex-native-ui"}
    assert answer["workspace"] == "/srv/repo"
    assert answer["key"].startswith("auto-") and len(answer["key"]) == 21
    assert answer["labels"][labels.ORCH] == "1"
    assert answer["labels"]["omnigent.wrapper"] == "codex-native-ui"
    assert home == [], "an absolute workspace needs no home lookup"


async def test_start_session_returns_the_live_one_for_the_same_key(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    live = await _session(
        client,
        "Dashboard overflow at 375",
        {labels.ORCH_KEY: START["key"], labels.ORCH: "conv_orch", labels.STATUS: "working"},
    )
    response = await client.post("/v1/ae/sessions", json=START)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["session_id"], body["existing"], body["status"]) == (live, True, "working")
    assert body["labels"][labels.ORCH_KEY] == START["key"]
    assert app.calls == []

    listed = (await client.get("/v1/ae/sessions")).json()
    assert [row["id"] for row in listed["items"]] == [live]
    assert listed["items"][0]["key"] == START["key"]
    assert listed["items"][0]["path"] == f"/c/{live}"
    assert listed["slots_used"] == 1


async def test_start_session_holds_at_the_limit_set_on_the_settings_page(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, home: list[str]
) -> None:
    # 2026-10-22: the orchestrator's slot hold counts against the page's limit too.
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    for title in ("One", "Two"):
        await _session(client, title, {labels.STATUS: "working"})
    slot_limit.remember_store(_SlotCapStore(2))
    try:
        full = await client.post("/v1/ae/sessions", json=START)
    finally:
        slot_limit.remember_store(None)
    assert full.status_code == 409, full.text
    error = full.json()["error"]
    assert error["message"].startswith("Slots full: 2 of 2 working.")
    assert error["limit"] == 2
    assert app.calls == []


async def test_start_session_at_the_limit_asks_unless_riding(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, home: list[str]
) -> None:
    app = _App()
    monkeypatch.setattr(router_module, "_loopback", app)
    for title in ("One", "Two", "Three"):
        await _session(client, title, {labels.STATUS: "working"})
    failed = await _session(client, "Crashed", {labels.STATUS: "working"})
    get_conversation_store().set_session_live_status(failed, "failed")
    full = await client.post("/v1/ae/sessions", json=START)
    assert full.status_code == 409, full.text
    error = full.json()["error"]
    assert error["code"] == "slots_full"
    assert error["message"].startswith("Slots full: 3 of 3 working.")
    assert "Crashed" not in error["message"], "a failed session holds no slot"
    assert {row["title"] for row in error["working"]} == {"One", "Two", "Three"}
    assert app.calls == []

    riding = await client.post("/v1/ae/sessions", json=START | {"ride_unparked": True})
    assert riding.status_code == 200, riding.text
    assert riding.json()["overflow"] is True
    assert app.calls[2][2]["labels"][labels.OVERFLOW] == "1"


async def test_start_session_failures_name_what_to_do(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, home: list[str]
) -> None:
    monkeypatch.setattr(router_module, "_loopback", _App(hosts=[]))
    no_host = await client.post("/v1/ae/sessions", json=START)
    assert no_host.status_code == 503
    assert no_host.json()["error"]["code"] == "session_no_target"
    assert "no host is online" in no_host.json()["error"]["message"]

    monkeypatch.setattr(router_module, "_loopback", _App())
    other = await client.post("/v1/ae/sessions", json=START | {"host": "mac"})
    assert "host mac is not online" in other.json()["error"]["message"]

    monkeypatch.setattr(router_module, "_loopback", _App(create_status=422))
    not_created = await client.post("/v1/ae/sessions", json=START)
    assert not_created.status_code == 502
    assert not_created.json()["error"]["code"] == "session_not_created"

    monkeypatch.setattr(router_module, "_loopback", _App(post_status=409))
    not_posted = await client.post("/v1/ae/sessions", json=START)
    error = not_posted.json()["error"]
    assert (not_posted.status_code, error["code"]) == (502, "session_brief_not_posted")
    assert error["session_id"] == "conv_resumed"
    assert "post_message" in error["message"]

    async def _no_home(request: Request, host_id: str) -> str | None:
        return None

    monkeypatch.setattr(router_module, "_host_home", _no_home)
    monkeypatch.setattr(router_module, "_loopback", _App())
    relative = await client.post("/v1/ae/sessions", json=START)
    assert "give an absolute path" in relative.json()["error"]["message"]
    bad = await client.post("/v1/ae/sessions", json=START | {"harness": "gemini"})
    assert bad.status_code == 422
    # Every failure let go of its slot hold.
    held, _, _, pending = router_module.slot_cap.reserve("orch:probe", [])
    assert (held, pending) == (True, 0)
    router_module.slot_cap.release("orch:probe")


# ── the lot's stale-while-revalidate cache (2026-09-26) ─────────────────


async def _until(check: Any, timeout: float = 2.0) -> None:
    """Yield to the loop until *check()* holds; background reads finish this way."""
    import asyncio

    for _ in range(int(timeout / 0.01)):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the background read never finished")


async def test_the_lot_is_cached_and_a_park_drops_it_at_once(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        tasks = mock.get("/api/tasks", params={"status": "open"}).respond(200, json=LOT["tasks"])
        mock.post("/api/tasks").respond(201, json=CREATED)
        mock.patch("/api/tasks/T-NEW").respond(200, json=CREATED)
        first = await client.get("/v1/ae/lot")
        again = await client.get("/v1/ae/lot")
        assert tasks.call_count == 1
        assert again.json() == first.json()
        # A park answers only after the cache is gone: the next read is awaited.
        assert (await client.post("/v1/ae/lot/park", json=PARK_BODY)).status_code == 200
        await client.get("/v1/ae/lot")
        assert tasks.call_count == 2
        # A link is an AE write to the lot too.
        mock.post("/api/tasks/T-NEW/links").respond(201, json={"id": "L1"})
        await client.post("/v1/ae/lot/T-NEW/link", json={"url": "https://github.com/a/b/issues/7"})
        await client.get("/v1/ae/lot")
        assert tasks.call_count == 3


async def test_a_stale_lot_is_served_while_one_read_replaces_it(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module._lot_cache, "ttl_s", 0.0)
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        tasks = mock.get("/api/tasks", params={"status": "open"}).respond(200, json=LOT["tasks"])
        first = (await client.get("/v1/ae/lot")).json()
        assert first["count"] == 2
        tasks.respond(200, json={"tasks": []})
        # Served the held lot; the refresh runs behind it.
        stale = (await client.get("/v1/ae/lot")).json()
        assert stale == first
        await _until(lambda: tasks.call_count == 2)
        await _until(lambda: not router_module._lot_cache.refreshing(("open", 20)))
        assert (await client.get("/v1/ae/lot")).json()["count"] == 0


async def test_a_failed_lot_refresh_keeps_the_last_lot_and_says_so(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router_module._lot_cache, "ttl_s", 0.0)
    with respx.mock(base_url=BASE) as mock:
        buckets = mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        mock.get("/api/tasks", params={"status": "open"}).respond(200, json=LOT["tasks"])
        first = (await client.get("/v1/ae/lot")).json()
        buckets.respond(503, json={"error": "down"})
        await client.get("/v1/ae/lot")
        await _until(lambda: router_module._lot_cache.last_error(("open", 20)) is not None)
        held = await client.get("/v1/ae/lot")
    assert held.status_code == 200
    body = held.json()
    assert body["tasks"] == first["tasks"]
    assert "503" in body["refresh_error"]


async def test_the_tags_are_cached_and_a_park_drops_them(client: httpx.AsyncClient) -> None:
    with respx.mock(base_url=BASE) as mock:
        tags = mock.get("/api/tags").respond(200, json={"tags": [{"name": "ae"}]})
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        mock.post("/api/tasks").respond(201, json=CREATED)
        mock.patch("/api/tasks/T-NEW").respond(200, json=CREATED)
        await client.get("/v1/ae/lot/tags?icons=true")
        await client.get("/v1/ae/lot/tags?icons=true")
        assert tags.call_count == 1
        await client.post("/v1/ae/lot/park", json=PARK_BODY)
        await client.get("/v1/ae/lot/tags?icons=true")
        assert tags.call_count == 2


# ── the deploy hold (2026-10-19) ───────────────────────────────────────


@pytest.fixture
def hold_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # No hold file: the store stays in memory, so nothing writes to /data.
    monkeypatch.delenv(deploy_hold.STATE_FILE_ENV, raising=False)
    monkeypatch.setattr(deploy_hold, "DATA_DIR", Path("/nonexistent-ae-data"))
    deploy_hold.reset_store()
    yield
    deploy_hold.reset_store()


NOT_HELD = {
    "held": False,
    "until": None,
    "set_at": None,
    "set_by": None,
    "remaining_s": 0,
    "choices": [1, 2, 3],
    "error": None,
}


async def test_deploy_hold_is_empty_at_first(client: httpx.AsyncClient, hold_store: None) -> None:
    response = await client.get("/v1/ae/deploy/hold")
    assert response.status_code == 200, response.text
    assert response.json() == NOT_HELD


@pytest.mark.parametrize("hours", [1, 2, 3])
async def test_deploy_hold_sets_one_to_three_hours(
    client: httpx.AsyncClient, hold_store: None, hours: int
) -> None:
    before = int(time.time())
    response = await client.post("/v1/ae/deploy/hold", json={"hours": hours})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["held"] is True and body["error"] is None
    assert body["set_by"] == "local"
    assert body["choices"] == [1, 2, 3]
    assert hours * 3600 - 5 <= body["remaining_s"] <= hours * 3600
    assert body["until"].endswith("Z") and body["set_at"].endswith("Z")
    hold = deploy_hold.store().read().hold
    assert hold is not None and hold.until - hold.set_at == hours * 3600
    assert hold.set_at >= before
    assert (await client.get("/v1/ae/deploy/hold")).json()["until"] == body["until"]


@pytest.mark.parametrize("body", [{"hours": 0}, {"hours": 4}, {"hours": "2"}, {"hours": 1.5}, {}])
async def test_deploy_hold_refuses_bad_hours(
    client: httpx.AsyncClient, hold_store: None, body: dict[str, Any]
) -> None:
    response = await client.post("/v1/ae/deploy/hold", json=body)
    assert response.status_code == 422
    assert (await client.get("/v1/ae/deploy/hold")).json() == NOT_HELD


async def test_deploy_hold_refuses_extra_fields(
    client: httpx.AsyncClient, hold_store: None
) -> None:
    response = await client.post("/v1/ae/deploy/hold", json={"hours": 1, "until": 0})
    assert response.status_code == 422


async def test_deploy_hold_replaces_the_hold_in_force(
    client: httpx.AsyncClient, hold_store: None
) -> None:
    await client.post("/v1/ae/deploy/hold", json={"hours": 3})
    shorter = (await client.post("/v1/ae/deploy/hold", json={"hours": 1})).json()
    assert shorter["remaining_s"] <= 3600


async def test_deploy_resume_clears_and_is_idempotent(
    client: httpx.AsyncClient, hold_store: None
) -> None:
    await client.post("/v1/ae/deploy/hold", json={"hours": 2})
    cleared = await client.post("/v1/ae/deploy/resume", json={})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == NOT_HELD
    assert (await client.get("/v1/ae/deploy/hold")).json() == NOT_HELD
    again = await client.post("/v1/ae/deploy/resume", json={"ignored": True})
    assert again.status_code == 200, again.text
    assert again.json() == NOT_HELD


async def test_deploy_hold_records_the_user_and_logs_both_posts(
    client: httpx.AsyncClient,
    hold_store: None,
    ae_admins: set[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    reid = {"X-Forwarded-Email": "reid@example.com"}
    assert (await client.get("/v1/ae/deploy/hold")).status_code == 401
    assert (await client.post("/v1/ae/deploy/hold", json={"hours": 1})).status_code == 401
    assert (await client.post("/v1/ae/deploy/resume", json={})).status_code == 401
    with caplog.at_level(logging.INFO, logger=router_module.__name__):
        held = await client.post("/v1/ae/deploy/hold", json={"hours": 2}, headers=reid)
        assert held.json()["set_by"] == "reid@example.com"
        until = held.json()["until"][11:16] + "Z"
        await client.post("/v1/ae/deploy/resume", json={}, headers=reid)
        await client.post("/v1/ae/deploy/resume", json={}, headers=reid)
    lines = [r.getMessage() for r in caplog.records if r.name == router_module.__name__]
    assert f"deploy hold set for 2h until {until} by reid@example.com" in lines
    assert f"deploy hold cleared by reid@example.com (was until {until})" in lines
    assert "deploy hold cleared by reid@example.com (none was on)" in lines


async def test_deploy_hold_is_admin_only(
    client: httpx.AsyncClient, hold_store: None, ae_admins: set[str]
) -> None:
    steve = {"X-Forwarded-Email": "steve@example.com"}
    reid = {"X-Forwarded-Email": "reid@example.com"}
    held = await client.post("/v1/ae/deploy/hold", json={"hours": 2}, headers=reid)
    assert held.status_code == 200, held.text
    refusals = [
        await client.get("/v1/ae/deploy/hold", headers=steve),
        await client.post("/v1/ae/deploy/hold", json={"hours": 1}, headers=steve),
        await client.post("/v1/ae/deploy/resume", json={}, headers=steve),
    ]
    for refused in refusals:
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "admin_only"
    # Nothing a non-admin sent changed the hold.
    after = (await client.get("/v1/ae/deploy/hold", headers=reid)).json()
    assert after["held"] is True and after["until"] == held.json()["until"]


async def test_azure_cost_is_admin_only(client: httpx.AsyncClient, ae_admins: set[str]) -> None:
    # Tenant spend and resource names are for admins (2026-10-22).
    refused = await client.get(
        "/v1/ae/azure-cost", headers={"X-Forwarded-Email": "steve@example.com"}
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "admin_only"
    allowed = await client.get(
        "/v1/ae/azure-cost", headers={"X-Forwarded-Email": "reid@example.com"}
    )
    assert allowed.status_code == 200, allowed.text


async def test_deploy_hold_reports_a_corrupt_file_as_not_held(
    client: httpx.AsyncClient, hold_store: None, tmp_path: Path
) -> None:
    path = tmp_path / "hold.json"
    path.write_text("{not json", encoding="utf-8")
    deploy_hold.reset_store(deploy_hold.DeployHoldStore(path))
    body = (await client.get("/v1/ae/deploy/hold")).json()
    assert body["held"] is False
    assert "not valid JSON" in body["error"]
    fixed = (await client.post("/v1/ae/deploy/hold", json={"hours": 1})).json()
    assert fixed["held"] is True and fixed["error"] is None


async def test_deploy_hold_failed_write_is_a_500(
    client: httpx.AsyncClient,
    hold_store: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deploy_hold.reset_store(deploy_hold.DeployHoldStore(tmp_path / "hold.json"))

    def refuse(_src: str, _dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(deploy_hold.os, "replace", refuse)
    failed = await client.post("/v1/ae/deploy/hold", json={"hours": 1})
    assert failed.status_code == 500
    error = failed.json()["error"]
    assert error["code"] == "deploy_hold_not_saved"
    assert "could not write" in error["message"]
    assert (await client.get("/v1/ae/deploy/hold")).json() == NOT_HELD


async def test_deploy_hold_posts_are_json_only_and_origin_checked(
    client: httpx.AsyncClient, hold_store: None
) -> None:
    form = await client.post(
        "/v1/ae/deploy/hold",
        content="hours=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 415
    foreign = await client.post(
        "/v1/ae/deploy/hold", json={"hours": 1}, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    await client.post("/v1/ae/deploy/hold", json={"hours": 1})
    foreign_resume = await client.post(
        "/v1/ae/deploy/resume", json={}, headers={"Origin": "https://evil.example"}
    )
    assert foreign_resume.status_code == 403
    assert (await client.get("/v1/ae/deploy/hold")).json()["held"] is True
