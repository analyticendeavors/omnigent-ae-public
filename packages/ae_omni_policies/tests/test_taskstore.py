"""The task-store client against respx: the worker's body, the worker's failure modes."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from ae_omni_policies import notes
from ae_omni_policies.taskstore import (
    ORIGIN_SURFACE,
    ParkFields,
    Settings,
    TaskStoreClient,
    TaskStoreError,
    form_component,
    issue_url_of,
    repo_of,
    view_url,
)

BASE = "https://tasks.test"
LOT_ID = "01M1PARKINGLOT000000000000"
SETTINGS = Settings(app_url=BASE, client_id="test-client-id.access", client_secret="s3")
LOT = json.loads((Path(__file__).parent / "fixtures" / "lot.json").read_text(encoding="utf-8"))

FIELDS = ParkFields(
    thread="eur-currency-formatting",
    state="The formatting helper drops the EUR symbol; the fix lives in shared/js/money.js.",
    next="Open shared/js/money.js and add the EUR branch next to USD.",
    slot="never opened",
    quadrant="feature",
    est="about an hour",
    from_="pricing padding fix, Claude Code, 2026-09-23",
    summary="EUR currency formatting broken in the pricing table",
    repo="https://github.com/analyticendeavors/website_content",
    tags=["Content", "content", " Power BI "],
    checklist=["Open shared/js/money.js", "Add the EUR branch"],
)
NOTE = notes.NoteFields(
    summary=FIELDS.summary or "",
    thread=FIELDS.thread,
    repo="analyticendeavors/website_content",
    state=FIELDS.state,
    next=FIELDS.next,
    slot=FIELDS.slot,
    quadrant=FIELDS.quadrant,
    est=FIELDS.est,
    from_=FIELDS.from_,
)
CREATED = {
    "id": "01M3NEW000000000000000000",
    "title": "eur-currency-formatting",
    "quadrant": "q3",
    "estimate": "60plus",
    "tags": ["parked", "content", "power bi"],
}


def test_form_component_matches_urlsearchparams() -> None:
    assert form_component("a b~*é") == "a+b%7E*%C3%A9"
    assert view_url(BASE, "task-store to DevOps", LOT_ID) == (
        f"{BASE}/#/all?list={LOT_ID}&q=task-store+to+DevOps"
    )
    assert view_url(BASE, "x", None) == f"{BASE}/#/all?q=x"


def test_repo_of_reads_the_repo_line_then_a_github_link() -> None:
    note = "THREAD: x\nREPO: analyticendeavors/omnigent-ae\nNEXT: y"
    assert repo_of(note) == "analyticendeavors/omnigent-ae"
    # No REPO: line: the issue route C linked names the repository.
    links = [
        {"url": "https://example.com/doc"},
        {"url": "https://github.com/analyticendeavors/website_content/issues/7"},
    ]
    assert repo_of("NEXT: y", links) == "analyticendeavors/website_content"
    # A REPO: line that is not owner/name falls through to the links.
    assert repo_of("REPO: nonsense", links) == "analyticendeavors/website_content"
    assert repo_of(None) is None
    assert repo_of("NEXT: y", [{"url": "https://example.com"}, "junk"]) is None


def test_issue_url_of_takes_the_first_github_issue_only() -> None:
    issue = "https://github.com/analyticendeavors/omnigent-ae/issues/28"
    links = [
        {"url": "https://example.com/doc"},
        {"url": "https://github.com/analyticendeavors/omnigent-ae/pull/41"},
        {"url": issue + "/"},
        {"url": "https://github.com/analyticendeavors/omnigent-ae/issues/29"},
    ]
    assert issue_url_of(links) == issue
    for url in (
        "https://github.com/analyticendeavors/omnigent-ae/pull/41",
        "https://github.com/analyticendeavors/omnigent-ae/issues",
        "https://github.com/analyticendeavors/omnigent-ae/issues/28#issuecomment-1",
        "https://gitlab.com/analyticendeavors/omnigent-ae/issues/28",
        "http://github.com/analyticendeavors/omnigent-ae/issues/28",
        "https://github.com.evil.test/a/b/issues/1",
    ):
        assert issue_url_of([{"url": url}]) is None, url
    assert issue_url_of(None) is None
    assert issue_url_of(["junk", {"url": 7}]) is None


def test_settings_from_env() -> None:
    settings = Settings.from_env(
        {
            "AE_TASK_STORE_URL": "https://tasks.example/",
            "AE_CF_ACCESS_CLIENT_ID": "id",
            "AE_CF_ACCESS_CLIENT_SECRET": "secret",
        }
    )
    assert settings.app_url == "https://tasks.example"
    assert settings.configured
    assert settings.tz == notes.DEFAULT_TZ
    assert not Settings.from_env({}).configured
    assert Settings.from_env({}).app_url == "https://tasks.go-ae.org"


async def test_list_parked_maps_the_lot() -> None:
    with respx.mock(base_url=BASE, assert_all_called=True) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        mock.get("/api/tasks", params={"status": "open"}).respond(200, json=LOT["tasks"])
        async with TaskStoreClient(SETTINGS) as client:
            got = await client.list_parked()
        # respx clears its call log when the block exits; read it here.
        request = mock.calls[0].request

    assert got["list"] == {"id": LOT_ID, "name": "Parking Lot"}
    assert got["status"] == "open"
    assert got["count"] == 2
    first, second = got["tasks"]
    assert first["id"] == "01M2PARKEDTASK000000000001"
    assert first["quadrant"] == "q2"
    assert first["estimate"] == "15"
    assert first["resurfaces"] == "2026-09-28"
    assert first["next"].startswith("Ask Alex in Teams:")
    assert first["links"] == ["https://github.com/analyticendeavors/example-books/issues/148"]
    assert first["url"] == f"{BASE}/#/all?list={LOT_ID}&q=contoso-vendor-category-check"
    assert first["repo"] == "analyticendeavors/example-books"
    assert second["resurfaces"] is None
    assert second["links"] == []
    assert second["repo"] is None
    assert request.headers["CF-Access-Client-Id"] == "test-client-id.access"
    assert request.headers["CF-Access-Client-Secret"] == "s3"
    assert request.headers["user-agent"].startswith("omnigent-ae/")
    assert request.headers["accept"] == "application/json"


async def test_list_parked_without_a_lot() -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json={"buckets": LOT["buckets"]["buckets"][:2]})
        async with TaskStoreClient(SETTINGS) as client:
            got = await client.list_parked()
    assert got == {
        "list": None,
        "count": 0,
        "tasks": [],
        "note": "No Parking Lot list exists yet.",
    }


async def test_park_sends_the_worker_body() -> None:
    with respx.mock(base_url=BASE, assert_all_called=True) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        created = mock.post("/api/tasks").respond(201, json=CREATED)
        patched = mock.patch(f"/api/tasks/{CREATED['id']}").respond(200, json=CREATED)
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.park(FIELDS, today=date(2026, 9, 23))

    body = json.loads(created.calls[0].request.content)
    assert body == {
        "title": "eur-currency-formatting",
        "notes": notes.build_notes(NOTE, FIELDS.checklist),
        "kind": "task",
        "status": "open",
        "urgent": 0,
        "important": 1,
        "estimate": "60plus",
        "tags": ["parked", "content", "power bi"],
        "origin_surface": ORIGIN_SURFACE,
        "bucket_id": LOT_ID,
        "due_at": "2026-10-23T00:00:00Z",
        "due_is_all_day": 1,
    }
    assert "priority" not in body
    url = f"{BASE}/#/all?list={LOT_ID}&q=eur-currency-formatting"
    assert json.loads(patched.calls[0].request.content) == {
        "notes": notes.build_notes(NOTE, FIELDS.checklist, url)
    }
    assert out["outcome"] == "filed"
    assert out["task_id"] == CREATED["id"]
    assert out["url"] == url
    assert out["list"] == {"id": LOT_ID, "name": "Parking Lot", "surface": "parking"}
    assert out["resurfaces"] == "2026-10-23"
    assert out["quadrant"] == "q3"
    assert out["issue_link"] is None
    assert out["warnings"] == []


async def test_park_creates_the_lot_and_links_the_issue_last() -> None:
    fields = ParkFields(
        **{**FIELDS.__dict__, "issue_url": "https://github.com/analyticendeavors/x/issues/9"}
    )
    with respx.mock(base_url=BASE, assert_all_called=True) as mock:
        mock.get("/api/buckets").respond(200, json={"buckets": LOT["buckets"]["buckets"][:2]})
        made = mock.post("/api/buckets").respond(
            201, json={"bucket": {"id": "NEWLOT", "name": "Parking Lot", "surface": "parking"}}
        )
        mock.post("/api/tasks").respond(201, json=CREATED)
        mock.patch(f"/api/tasks/{CREATED['id']}").respond(200, json=CREATED)
        linked = mock.post(f"/api/tasks/{CREATED['id']}/links").respond(201, json={"id": "L9"})
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.park(fields, today=date(2026, 9, 23))
        made_body = json.loads(made.calls[0].request.content)
        linked_body = json.loads(linked.calls[0].request.content)
        sequence = [f"{c.request.method} {c.request.url.path}" for c in mock.calls]

    assert made_body == {"name": "Parking Lot", "surface": "parking"}
    assert linked_body == {"kind": "url", "url": fields.issue_url, "label": "GitHub issue"}
    assert sequence == [
        "GET /api/buckets",
        "POST /api/buckets",
        "POST /api/tasks",
        f"PATCH /api/tasks/{CREATED['id']}",
        f"POST /api/tasks/{CREATED['id']}/links",
    ]
    assert out["issue_link"] == fields.issue_url
    assert out["list"]["id"] == "NEWLOT"


@pytest.mark.parametrize(
    ("due_date", "due_at", "all_day", "resurfaces", "warned"),
    [
        (None, "2026-10-23T00:00:00Z", 1, "2026-10-23", False),
        ("none", None, 0, None, False),
        ("NONE ", None, 0, None, False),
        ("2026-12-01", "2026-12-01T00:00:00Z", 1, "2026-12-01", False),
        ("2026-02-30", "2026-10-23T00:00:00Z", 1, "2026-10-23", True),
    ],
)
async def test_park_due_date(
    due_date: str | None, due_at: str | None, all_day: int, resurfaces: str | None, warned: bool
) -> None:
    fields = ParkFields(**{**FIELDS.__dict__, "due_date": due_date})
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        created = mock.post("/api/tasks").respond(201, json=CREATED)
        mock.patch(f"/api/tasks/{CREATED['id']}").respond(200, json=CREATED)
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.park(fields, today=date(2026, 9, 23))
    body = json.loads(created.calls[0].request.content)
    assert body["due_at"] == due_at
    assert body["due_is_all_day"] == all_day
    assert out["resurfaces"] == resurfaces
    assert any("Ignored due_date" in w for w in out["warnings"]) is warned


async def test_park_warns_on_a_bad_repo_and_defaults_the_summary() -> None:
    fields = ParkFields(**{**FIELDS.__dict__, "repo": "not a repo", "summary": None})
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        created = mock.post("/api/tasks").respond(201, json=CREATED)
        mock.patch(f"/api/tasks/{CREATED['id']}").respond(200, json=CREATED)
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.park(fields, today=date(2026, 9, 23))
    body = json.loads(created.calls[0].request.content)
    assert body["notes"].startswith(
        "- [ ] Open shared/js/money.js\n- [ ] Add the EUR branch\n\n"
        "**The formatting helper drops the EUR symbol; the fix lives in shared/js/money.js.**"
    )
    assert "REPO:" not in body["notes"]
    assert any('Ignored repo "not a repo"' in w for w in out["warnings"])


async def test_redirect_is_an_auth_failure() -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(302, headers={"location": "https://login.example/"})
        async with TaskStoreClient(SETTINGS) as client:
            with pytest.raises(TaskStoreError) as excinfo:
                await client.list_parked()
            assert excinfo.value.result.kind == "auth"
            assert "Cloudflare Access redirected" in str(excinfo.value)

            mock.post("/api/tasks").respond(302, headers={"location": "https://login.example/"})
            out = await client.park(FIELDS, today=date(2026, 9, 23))
    assert out["outcome"] == "unfiled"
    assert "Cloudflare Access redirected" in out["reason"]
    assert out["note"] == notes.build_notes(NOTE, FIELDS.checklist)
    assert out["instruction"].startswith("Report this as UNFILED (target unreachable)")


async def test_bot_protection_reads_as_auth_not_allowlist() -> None:
    with respx.mock(base_url=BASE) as mock:
        tags = mock.get("/api/tags").respond(403, text="<html>error code: 1010</html>")
        async with TaskStoreClient(SETTINGS) as client:
            result = await client.list_tags()
        calls = tags.call_count
    assert result.kind == "auth"
    assert "error 1010" in result.error
    assert calls == 1


async def test_5xx_retried_once_and_4xx_never() -> None:
    with respx.mock(base_url=BASE) as mock:
        tags = mock.get("/api/tags")
        tags.side_effect = [
            httpx.Response(500, text="boom"),
            httpx.Response(200, json={"tags": []}),
        ]
        bad = mock.patch("/api/tasks/T").respond(400, json={"error": "no such list"})
        async with TaskStoreClient(SETTINGS) as client:
            ok = await client.list_tags()
            client_error = await client.patch_task("T", {"bucket_id": "gone"})
    assert ok.ok and tags.call_count == 2
    assert client_error.kind == "client"
    assert client_error.error == "400 no such list"
    assert bad.call_count == 1


async def test_create_transport_failure_rereads_by_title_and_never_resends() -> None:
    match = {
        **CREATED,
        "origin_surface": ORIGIN_SURFACE,
        "bucket_id": LOT_ID,
        "notes": notes.build_notes(NOTE, FIELDS.checklist),
    }
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        create = mock.post("/api/tasks").mock(side_effect=httpx.ReadTimeout("slow"))
        mock.get("/api/tasks", params={"status": "open"}).respond(
            200, json={"tasks": [LOT["tasks"]["tasks"][0], match], "synced_at": None}
        )
        mock.patch(f"/api/tasks/{CREATED['id']}").respond(200, json=CREATED)
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.park(FIELDS, today=date(2026, 9, 23))
    assert create.call_count == 1
    assert out["outcome"] == "filed"
    assert out["task_id"] == CREATED["id"]
    assert out["warnings"] == [
        "The create call failed (no answer within 10s) but the task was there on re-read; "
        "not sent twice."
    ]


async def test_create_transport_failure_without_a_match_is_unfiled() -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        mock.post("/api/tasks").mock(side_effect=httpx.ConnectError("down"))
        mock.get("/api/tasks", params={"status": "open"}).respond(200, json=LOT["tasks"])
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.park(FIELDS, today=date(2026, 9, 23))
    assert out["outcome"] == "unfiled"
    assert out["reason"] == "down"


async def test_get_retries_transport_and_stamp_failure_is_a_warning() -> None:
    with respx.mock(base_url=BASE) as mock:
        buckets = mock.get("/api/buckets")
        buckets.side_effect = [
            httpx.ConnectError("blip"),
            httpx.Response(200, json=LOT["buckets"]),
        ]
        mock.post("/api/tasks").respond(201, json=CREATED)
        patched = mock.patch(f"/api/tasks/{CREATED['id']}").respond(500, text="stamp failed")
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.park(FIELDS, today=date(2026, 9, 23))
    assert buckets.call_count == 2
    # A 5xx on the stamp is retried once, then reported; the task is filed.
    assert patched.call_count == 2
    assert out["outcome"] == "filed"
    assert out["warnings"] == [
        "The note is missing its TASK: link (500 did not answer JSON: stamp failed). "
        "Everything else is filed."
    ]


async def test_non_json_success_is_a_parse_failure() -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tags").respond(200, text="<html>login</html>")
        async with TaskStoreClient(SETTINGS) as client:
            result: Any = await client.list_tags()
    assert result.kind == "parse"
    assert result.error.startswith("200 did not answer JSON:")


async def test_settings_fall_back_on_a_bad_timezone() -> None:
    settings = Settings.from_env({"AE_TASK_STORE_TZ": "PST"})
    assert settings.tz == notes.DEFAULT_TZ
    assert Settings.from_env({"AE_TASK_STORE_TZ": "Europe/Oslo"}).tz == "Europe/Oslo"


async def test_a_403_that_merely_mentions_1010_is_the_store_refusing() -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tags").respond(403, json={"error": "task 01M1010 is read-only"})
        async with TaskStoreClient(SETTINGS) as client:
            result = await client.list_tags()
    assert result.kind == "client"
    assert result.error == "403 task 01M1010 is read-only"


async def test_create_5xx_that_landed_is_not_resent() -> None:
    landed = {
        **CREATED,
        "origin_surface": ORIGIN_SURFACE,
        "notes": notes.build_notes(NOTE, FIELDS.checklist),
        "created_at": "2026-09-23T01:00:00.000Z",
    }
    older_same_title = {
        **CREATED,
        "id": "OLD",
        "origin_surface": ORIGIN_SURFACE,
        "notes": "an older note under the same THREAD name",
        "created_at": "2026-09-01T00:00:00.000Z",
    }
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        create = mock.post("/api/tasks").respond(504, text="edge timeout")
        mock.get("/api/tasks", params={"status": "open"}).respond(
            200, json={"tasks": [older_same_title, landed], "synced_at": None}
        )
        patched = mock.patch(f"/api/tasks/{CREATED['id']}").respond(200, json=CREATED)
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.park(FIELDS, today=date(2026, 9, 23))
        assert create.call_count == 1
        assert patched.call_count == 1
    assert out["outcome"] == "filed"
    assert out["task_id"] == CREATED["id"]
    assert out["warnings"] == [
        "The create call failed (504 did not answer JSON: edge timeout) but the task was "
        "there on re-read; not sent twice."
    ]


async def test_create_5xx_with_nothing_landed_is_retried_once() -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        create = mock.post("/api/tasks")
        create.side_effect = [
            httpx.Response(500, json={"error": "locked"}),
            httpx.Response(201, json=CREATED),
        ]
        mock.get("/api/tasks", params={"status": "open"}).respond(200, json=LOT["tasks"])
        mock.patch(f"/api/tasks/{CREATED['id']}").respond(200, json=CREATED)
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.park(FIELDS, today=date(2026, 9, 23))
        assert create.call_count == 2
    assert out["outcome"] == "filed"
    assert out["warnings"] == []


async def test_reread_ignores_an_older_task_with_the_same_title() -> None:
    older_same_title = {
        **CREATED,
        "id": "OLD",
        "origin_surface": ORIGIN_SURFACE,
        "notes": "an older note under the same THREAD name",
    }
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        create = mock.post("/api/tasks").mock(side_effect=httpx.ReadTimeout("slow"))
        mock.get("/api/tasks", params={"status": "open"}).respond(
            200, json={"tasks": [older_same_title], "synced_at": None}
        )
        patched = mock.patch("/api/tasks/OLD").respond(200, json=CREATED)
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.park(FIELDS, today=date(2026, 9, 23))
        assert create.call_count == 1
        assert patched.call_count == 0
    assert out["outcome"] == "unfiled"
    assert out["reason"] == "no answer within 10s"


async def test_create_without_a_task_id_is_unfiled() -> None:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        mock.get("/api/buckets").respond(200, json=LOT["buckets"])
        mock.post("/api/tasks").respond(201, json={"ok": True})
        stamp = mock.patch("/api/tasks/None").respond(404, json={"error": "no task"})
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.park(FIELDS, today=date(2026, 9, 23))
        assert stamp.call_count == 0
    assert out["outcome"] == "unfiled"
    assert out["reason"] == "the store answered 201 without a task id"
    assert out["note"] == notes.build_notes(NOTE, FIELDS.checklist)


# ── Phase 5 (2026-09-23): the worker's list_tags and link_issue, ported ──


async def test_tags_flattens_counts_busiest_first() -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tags").respond(
            200,
            json={
                "tags": [{"name": "ae"}, {"name": "content"}, {"name": "unused"}, "junk"],
                "counts": {"ae": {"tasks": 4}, "content": 9},
            },
        )
        async with TaskStoreClient(SETTINGS) as client:
            out = await client.tags()
    assert out == {
        "count": 3,
        "tags": [
            {"name": "content", "tasks": 9, "color": None, "icon": None},
            {"name": "ae", "tasks": 4, "color": None, "icon": None},
            {"name": "unused", "tasks": None, "color": None, "icon": None},
        ],
    }


async def test_tags_raises_when_the_store_cannot_answer() -> None:
    with respx.mock(base_url=BASE) as mock:
        mock.get("/api/tags").respond(302, headers={"location": "https://login.example/"})
        async with TaskStoreClient(SETTINGS) as client:
            with pytest.raises(TaskStoreError, match="Could not read the tags"):
                await client.tags()


async def test_link_issue_outcomes() -> None:
    with respx.mock(base_url=BASE) as mock:
        ok = mock.post("/api/tasks/T1/links").respond(201, json={"id": "L1"})
        mock.post("/api/tasks/T2/links").respond(404, json={"error": "no task T2"})
        async with TaskStoreClient(SETTINGS) as client:
            linked = await client.link_issue("T1", "https://github.com/a/b/issues/1")
            not_linked = await client.link_issue("T2", "https://x/2", "PR")
        assert json.loads(ok.calls[0].request.content) == {
            "kind": "url",
            "url": "https://github.com/a/b/issues/1",
            "label": "GitHub issue",
        }
    assert linked == {
        "outcome": "linked",
        "task_id": "T1",
        "issue_url": "https://github.com/a/b/issues/1",
        "link_id": "L1",
    }
    assert not_linked["outcome"] == "not_linked"
    assert not_linked["reason"] == "404 no task T2"
    assert not_linked["issue_url"] == "https://x/2"
    assert not_linked["instruction"].startswith("The task and the issue both exist")
