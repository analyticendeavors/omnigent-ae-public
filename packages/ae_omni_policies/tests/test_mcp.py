"""The stdio MCP server: in-process over the memory transport, the HTTP side on the app.

The MCP client is the real one (``mcp.shared.memory``); the server's HTTP
client is upstream's test ``client`` fixture, so every tool call lands on
the real app with the AE routes mounted, and task-store is respx. The one
process boundary the memory transport skips, stdin and stdout, is covered
by ``test_stdio_entry_point`` with a subprocess.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from omnigent.runtime import get_conversation_store
from omnigent.spec import load as load_spec
from omnigent.spec import materialize_bundle
from tests.server.helpers import create_test_session

from ae_omni_policies import labels, notes
from ae_omni_policies import mcp as mcp_module
from ae_omni_policies.mcp import TOOL_NAMES, TOOLS, WRITING_TOOLS, OmniClient, create_server
from ae_omni_policies.omni import SERVER_URL_ENV, Settings
from ae_omni_policies.taskstore import ORIGIN_SURFACE, URL_IS

BASE = "https://tasks.test"
LOT_ID = "01M1PARKINGLOT000000000000"
FIXTURES = Path(__file__).parent / "fixtures"
LOT = json.loads((FIXTURES / "lot.json").read_text(encoding="utf-8"))
AGENTS = Path(__file__).resolve().parents[3] / "agents"
PARK_ARGS: dict[str, Any] = {
    "summary": "EUR currency formatting broken in the pricing table",
    "thread": "eur-currency-formatting",
    "state": "The formatting helper drops the EUR symbol.",
    "next": "Open shared/js/money.js and add the EUR branch.",
    "slot": "never opened",
    "quadrant": "feature",
    "est": "about an hour",
    "from": "pricing padding fix, claude-sdk session, 2026-09-23",
    "repo": "analyticendeavors/website_content",
    "tags": ["Content", "content"],
}
CREATED = {
    "id": "T-NEW",
    "title": "eur-currency-formatting",
    "quadrant": "q3",
    "estimate": "60plus",
}


@pytest.fixture()
def omni(client: httpx.AsyncClient) -> OmniClient:
    """The server's HTTP side, pointed at the app through upstream's client."""
    return OmniClient(Settings(server_url="http://test"), http=client)


def connect(omni: OmniClient) -> AbstractAsyncContextManager[ClientSession]:
    """The real MCP client over the memory transport, on the server over *omni*.

    A helper and not an async fixture: the transport's task group must be
    entered and left by the same task, and pytest-asyncio tears an async
    fixture down from another one.
    """
    return create_connected_server_and_client_session(create_server(omni))


def _payload(result: types.CallToolResult) -> Any:
    assert result.content and isinstance(result.content[0], types.TextContent)
    return json.loads(result.content[0].text)


def _allowlist(name: str, tmp_path: Path) -> list[str]:
    spec = load_spec(materialize_bundle(AGENTS / name, tmp_path / name))
    (server,) = [server for server in spec.mcp_servers if server.name == "ae"]
    assert server.tools is not None
    return list(server.tools)


async def _post_item(client: httpx.AsyncClient, session_id: str, role: str, text: str) -> None:
    block = "output_text" if role == "assistant" else "input_text"
    data = {
        "item_type": "message",
        "item_data": {
            "role": role,
            "content": [{"type": block, "text": text}],
            **({"agent": "claude"} if role == "assistant" else {}),
        },
        "response_id": "resp_test",
    }
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_conversation_item", "data": data},
    )
    assert resp.status_code in (200, 201, 202), resp.text


async def test_lists_the_worker_tools_and_ours(omni: OmniClient) -> None:
    async with connect(omni) as session:
        listed = await session.list_tools()
        assert [tool.name for tool in listed.tools] == list(TOOL_NAMES)
        assert TOOL_NAMES == (
            "park_fork",
            "list_tags",
            "link_issue",
            "list_parked",
            "complete_task",
            "omni_capacity",
            "omni_set_status",
            "omni_transcript",
            "create_session",
            "post_message",
            "schedule_checkin",
            "list_prs",
            "ask_reid",
        )
        by_name = {tool.name: tool for tool in listed.tools}
        park = by_name["park_fork"].inputSchema
        # The worker's zod schema: seven required lines, `from` spelled as the note spells it.
        assert park["required"] == ["thread", "state", "next", "slot", "quadrant", "est", "from"]
        assert park["properties"]["slot"]["enum"] == ["never opened", "freed", "swapped-back"]
        assert park["properties"]["quadrant"]["enum"] == ["hotfix", "feature", "admin"]
        assert set(park["properties"]) == {
            "summary",
            "thread",
            "state",
            "next",
            "slot",
            "quadrant",
            "est",
            "from",
            "checklist",
            "repo",
            "tags",
            "due_date",
            "issue_url",
        }
        assert by_name["link_issue"].inputSchema["required"] == ["task_id", "issue_url"]
        assert by_name["list_parked"].inputSchema["properties"]["status"]["enum"][0] == "open"
        assert by_name["omni_set_status"].inputSchema["properties"]["status"]["enum"] == list(
            labels.STATUS_VALUES
        )


async def test_park_fork_sends_the_worker_body_and_returns_filed(omni: OmniClient) -> None:
    async with connect(omni) as session:
        with respx.mock(base_url=BASE, assert_all_called=True) as mock:
            mock.get("/api/buckets").respond(200, json=LOT["buckets"])
            created = mock.post("/api/tasks").respond(201, json=CREATED)
            stamped = mock.patch("/api/tasks/T-NEW").respond(200, json=CREATED)
            result = await session.call_tool("park_fork", PARK_ARGS)
            sent = json.loads(created.calls[0].request.content)
            restamped = json.loads(stamped.calls[0].request.content)

        note = notes.NoteFields(
            summary=PARK_ARGS["summary"],
            thread=PARK_ARGS["thread"],
            repo="analyticendeavors/website_content",
            state=PARK_ARGS["state"],
            next=PARK_ARGS["next"],
            slot=PARK_ARGS["slot"],
            quadrant=PARK_ARGS["quadrant"],
            est=PARK_ARGS["est"],
            from_=PARK_ARGS["from"],
        )
        due = notes.resurface_date(notes.DEFAULT_TZ, notes.RESURFACE_DAYS["feature"])
        assert sent == {
            "title": "eur-currency-formatting",
            "notes": notes.build_notes(note, None),
            "kind": "task",
            "status": "open",
            "urgent": 0,
            "important": 1,
            "estimate": "60plus",
            "tags": ["parked", "content"],
            "origin_surface": ORIGIN_SURFACE,
            "bucket_id": LOT_ID,
            "due_at": f"{due}T00:00:00Z",
            "due_is_all_day": 1,
        }
        url = f"{BASE}/#/all?list={LOT_ID}&q=eur-currency-formatting"
        assert restamped == {"notes": notes.build_notes(note, None, url)}

        assert result.isError is False
        body = _payload(result)
        assert body == {
            "outcome": "filed",
            "task_id": "T-NEW",
            "title": "eur-currency-formatting",
            "url": url,
            "url_is": URL_IS,
            "list": {"id": LOT_ID, "name": "Parking Lot", "surface": "parking"},
            "quadrant": "q3",
            "estimate": "60plus",
            "tags": None,
            "resurfaces": due,
            "issue_link": None,
            "warnings": [],
        }


async def test_park_fork_reports_unfiled_with_the_note(omni: OmniClient) -> None:
    async with connect(omni) as session:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/api/buckets").respond(302, headers={"location": "https://login.example/"})
            mock.post("/api/tasks").respond(302, headers={"location": "https://login.example/"})
            result = await session.call_tool("park_fork", PARK_ARGS)
        body = _payload(result)
        assert result.isError is False
        assert body["outcome"] == "unfiled"
        assert "Cloudflare Access redirected" in body["reason"]
        assert body["note"].startswith("[analyticendeavors/website_content]")
        assert "THREAD: eur-currency-formatting" in body["note"]
        assert body["instruction"].startswith("Report this as UNFILED")


async def test_park_fork_rejects_a_bad_body_as_text(omni: OmniClient) -> None:
    async with connect(omni) as session:
        broken = {**PARK_ARGS, "quadrant": "q3"}
        result = await session.call_tool("park_fork", broken)
        # The SDK checks the schema before the handler runs and answers with
        # text and isError, which is still an answer and not a crash.
        assert result.isError is True
        assert "'q3' is not one of ['hotfix', 'feature', 'admin']" in result.content[0].text


async def test_every_tool_is_text_when_the_server_is_unset() -> None:
    unset = OmniClient(Settings())
    async with create_connected_server_and_client_session(create_server(unset)) as bare:
        capacity = _payload(await bare.call_tool("omni_capacity", {}))
        assert SERVER_URL_ENV in capacity["error"]
        parked = _payload(await bare.call_tool("park_fork", PARK_ARGS))
        assert parked["outcome"] == "unfiled"
        assert SERVER_URL_ENV in parked["reason"]
        # The note is built locally so the agent can still file by hand.
        assert "NEXT: Open shared/js/money.js" in parked["note"]
        assert parked["instruction"].startswith("Report this as UNFILED")


async def test_list_tags_and_link_issue(omni: OmniClient) -> None:
    async with connect(omni) as session:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/api/tags").respond(
                200,
                json={
                    "tags": [{"name": "ae"}, {"name": "content"}, {"name": "power bi"}],
                    "counts": {"ae": {"tasks": 4}, "content": 9, "power bi": {"tasks": 1}},
                },
            )
            link = mock.post("/api/tasks/T-NEW/links").respond(201, json={"id": "L9"})
            tags = _payload(await session.call_tool("list_tags", {}))
            linked = _payload(
                await session.call_tool(
                    "link_issue",
                    {"task_id": "T-NEW", "issue_url": "https://github.com/a/b/issues/1"},
                )
            )
            assert json.loads(link.calls[0].request.content) == {
                "kind": "url",
                "url": "https://github.com/a/b/issues/1",
                "label": "GitHub issue",
            }
        assert tags == {
            "count": 3,
            "tags": [
                {"name": "content", "tasks": 9, "color": None, "icon": None},
                {"name": "ae", "tasks": 4, "color": None, "icon": None},
                {"name": "power bi", "tasks": 1, "color": None, "icon": None},
            ],
        }
        assert linked == {
            "outcome": "linked",
            "task_id": "T-NEW",
            "issue_url": "https://github.com/a/b/issues/1",
            "link_id": "L9",
        }


async def test_complete_task_posts_to_the_complete_route(omni: OmniClient) -> None:
    task = {"id": "T-NEW", "title": "x", "links": []}
    async with connect(omni) as session:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/api/tasks/T-NEW").respond(200, json=task)
            patched = mock.patch("/api/tasks/T-NEW").respond(200, json=task)
            done = _payload(await session.call_tool("complete_task", {"task_id": "T-NEW"}))
            assert json.loads(patched.calls[0].request.content) == {"status": "done"}
            mock.get("/api/tasks/T-GONE").respond(404, json={"error": "no task T-GONE"})
            gone = _payload(await session.call_tool("complete_task", {"task_id": "T-GONE"}))
    assert done["outcome"] == "completed"
    assert done["status"] == "done"
    assert gone == {
        "outcome": "not_completed",
        "task_id": "T-GONE",
        "reason": "404 no task T-GONE",
    }


async def test_link_issue_not_linked_keeps_the_url(omni: OmniClient) -> None:
    async with connect(omni) as session:
        with respx.mock(base_url=BASE) as mock:
            mock.post("/api/tasks/T-GONE/links").respond(404, json={"error": "no task T-GONE"})
            body = _payload(
                await session.call_tool(
                    "link_issue", {"task_id": "T-GONE", "issue_url": "https://x/1", "label": "PR"}
                )
            )
        assert body["outcome"] == "not_linked"
        assert body["issue_url"] == "https://x/1"
        assert "404" in body["reason"]
        assert body["instruction"].startswith("The task and the issue both exist")


async def test_list_parked_and_its_error(omni: OmniClient) -> None:
    async with connect(omni) as session:
        with respx.mock(base_url=BASE) as mock:
            mock.get("/api/buckets").respond(200, json=LOT["buckets"])
            mock.get("/api/tasks", params={"status": "open"}).respond(200, json=LOT["tasks"])
            body = _payload(await session.call_tool("list_parked", {"limit": 1}))
        assert body["list"] == {"id": LOT_ID, "name": "Parking Lot"}
        assert body["count"] == 2
        assert len(body["tasks"]) == 1
        assert body["tasks"][0]["resurfaces"] == "2026-09-28"
        with respx.mock(base_url=BASE) as mock:
            mock.get("/api/buckets").respond(500, json={"error": "boom"})
            failed = _payload(await session.call_tool("list_parked", {}))
        assert failed["error"].startswith("Could not read the lists: 502")


async def test_omni_capacity_and_transcript(omni: OmniClient, client: httpx.AsyncClient) -> None:
    async with connect(omni) as session:
        working = (
            await create_test_session(
                client, title="Fix pricing padding", labels={labels.STATUS: "working"}
            )
        )["id"]
        await _post_item(client, working, "user", "Fix the padding on the pricing table.")
        await _post_item(client, working, "assistant", "Done: padding is 12px now.")
        await _post_item(client, working, "user", "Ship it.")
        crashed = (
            await create_test_session(client, title="Crashed", labels={labels.STATUS: "working"})
        )["id"]
        get_conversation_store().set_session_live_status(crashed, "failed")

        capacity = _payload(await session.call_tool("omni_capacity", {}))
        assert capacity["slots_used"] == 1, "a failed session holds no slot"
        assert [row["id"] for row in capacity["working"]] == [working]
        assert [row["id"] for row in capacity["blocked"]] == [crashed]
        assert capacity["blocked"][0]["failed"] is True
        assert capacity["timezone"] == notes.DEFAULT_TZ
        assert len(capacity["today"]) == 10

        transcript = _payload(
            await session.call_tool("omni_transcript", {"session_id": working, "last_n": 2})
        )
        assert transcript["session_id"] == working
        assert transcript["count"] == 2
        assert [(row["role"], row["text"]) for row in transcript["items"]] == [
            ("assistant", "Done: padding is 12px now."),
            ("user", "Ship it."),
        ]
        missing = _payload(
            await session.call_tool("omni_transcript", {"session_id": "conv_" + "f" * 32})
        )
        assert missing["error"].startswith("Could not read session")


async def test_omni_set_status_writes_the_label(
    omni: OmniClient, client: httpx.AsyncClient
) -> None:
    async with connect(omni) as session:
        session_id = (await create_test_session(client, title="Renewal note"))["id"]
        body = _payload(
            await session.call_tool(
                "omni_set_status", {"session_id": session_id, "status": "review"}
            )
        )
        assert body == {
            "session_id": session_id,
            "status": "review",
            "labels": {labels.STATUS: "review"},
        }
        conv = get_conversation_store().get_conversation(session_id)
        assert conv is not None and conv.labels[labels.STATUS] == "review"

        bad = await session.call_tool(
            "omni_set_status", {"session_id": session_id, "status": "done"}
        )
        assert "done" in bad.content[0].text
        conv = get_conversation_store().get_conversation(session_id)
        assert conv is not None and conv.labels[labels.STATUS] == "review"


async def test_a_failing_handler_answers_with_text(
    omni: OmniClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connect(omni) as session:

        async def _explode(client: OmniClient, args: dict[str, Any]) -> Any:
            raise RuntimeError("wire cut")

        monkeypatch.setitem(mcp_module.HANDLERS, "omni_capacity", _explode)
        result = await session.call_tool("omni_capacity", {})
        assert result.isError is False
        assert _payload(result) == {"error": "omni_capacity failed: RuntimeError: wire cut"}


# 2026-10-22: an httpx error's text can carry the request URL and its query,
# so the agent gets the class name only.
SECRET_URL = "http://box.test/v1/thing?sig=s3cret-sig"


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(f"refused {SECRET_URL}", request=request)


async def test_a_transport_failure_never_shows_the_error_text() -> None:
    status, body = await _stub(_refuse).call("GET", "/v1/thing")
    assert status == 0
    assert body == {"error": {"message": "http://box.test did not answer: ConnectError"}}


async def test_a_checkin_transport_failure_never_shows_the_error_text() -> None:
    answer = await mcp_module.schedule_checkin(_stub(_refuse), {"stop": True})
    assert answer == {"error": "Could not reach the scheduled tasks: ConnectError"}


async def test_a_handler_httpx_failure_never_shows_the_error_text(
    omni: OmniClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with connect(omni) as session:

        async def _explode(client: OmniClient, args: dict[str, Any]) -> Any:
            raise httpx.ReadTimeout(f"timed out on {SECRET_URL}")

        monkeypatch.setitem(mcp_module.HANDLERS, "omni_capacity", _explode)
        result = await session.call_tool("omni_capacity", {})
        assert _payload(result) == {"error": "omni_capacity failed: ReadTimeout"}


ORCHESTRATOR_TOOLS = [
    "omni_capacity",
    "omni_transcript",
    "omni_set_status",
    "list_parked",
    "create_session",
    "post_message",
    "schedule_checkin",
    "list_prs",
    "ask_reid",
]


def test_the_orchestrator_writes_only_what_it_needs(tmp_path: Path) -> None:
    """Sessions, messages, its check-in and a status; never the lot, never a merge."""
    allowlist = _allowlist("orchestrator", tmp_path)
    assert allowlist == ORCHESTRATOR_TOOLS
    assert set(allowlist) <= set(TOOL_NAMES)
    assert set(allowlist) & WRITING_TOOLS == {
        "create_session",
        "post_message",
        "schedule_checkin",
        "omni_set_status",
    }
    assert "park_fork" not in allowlist and "link_issue" not in allowlist


def test_every_new_writer_is_on_writing_tools() -> None:
    assert {"create_session", "post_message", "schedule_checkin"} <= WRITING_TOOLS
    assert not {"list_prs", "ask_reid"} & WRITING_TOOLS
    assert set(TOOL_NAMES) >= WRITING_TOOLS


@pytest.mark.parametrize(
    ("agent", "allowed"),
    [
        ("steward", ["list_parked", "omni_capacity"]),
        ("shutdown", ["omni_capacity", "omni_transcript"]),
    ],
)
def test_scheduled_agents_cannot_reach_a_writing_tool(
    agent: str, allowed: list[str], tmp_path: Path
) -> None:
    """The server offers every tool; the YAML's allowlist is what the agent sees.

    The filtering itself is upstream's (``server/mcp_pool.py`` and
    ``runner/mcp_manager.py`` honour ``MCPServerConfig.tools``), so what this
    package can prove is that each shipped allowlist names only tools this
    server has and none that writes, and that filtering the server's list by
    it leaves ``park_fork`` out.
    """
    allowlist = _allowlist(agent, tmp_path)
    assert allowlist == allowed
    assert set(allowlist) <= set(TOOL_NAMES)
    assert not set(allowlist) & WRITING_TOOLS
    exposed = [tool.name for tool in TOOLS if tool.name in allowlist]
    assert "park_fork" not in exposed
    assert "omni_set_status" not in exposed
    assert exposed == allowed


async def test_stdio_entry_point(tmp_path: Path) -> None:
    """``python -m ae_omni_policies.mcp`` speaks MCP over stdio, even with nothing configured."""
    # No server anywhere: the variable dropped and HOME (the login files) emptied.
    env = {k: v for k, v in os.environ.items() if k != SERVER_URL_ENV}
    env |= {"HOME": str(tmp_path), "OMNIGENT_DATA_DIR": str(tmp_path)}
    env.pop("OMNIGENT_CONFIG_HOME", None)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "ae_omni_policies.mcp"], env=env
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as remote:
        await remote.initialize()
        listed = await remote.list_tools()
        assert [tool.name for tool in listed.tools] == list(TOOL_NAMES)
        body = _payload(await remote.call_tool("omni_capacity", {}))
    assert SERVER_URL_ENV in body["error"]


# ── 2026-09-25: the orchestrator's tools ──


def _stub(handler: Any) -> OmniClient:
    """An OmniClient whose server is *handler* (an ``httpx.MockTransport`` function)."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://box.test")
    return OmniClient(Settings(server_url="http://box.test", token="t0ken-never-shown"), http=http)


async def test_create_session_answers_the_url_slots_full_and_failures() -> None:
    seen: list[dict[str, Any]] = []
    answers = iter(
        [
            httpx.Response(200, json={"session_id": "conv_1", "path": "/c/conv_1"}),
            httpx.Response(
                409,
                json={
                    "error": {
                        "code": "slots_full",
                        "message": "Slots full: 3 of 3 working.",
                        "working": [{"id": "conv_a", "title": "A"}],
                        "limit": 3,
                    }
                },
            ),
            httpx.Response(
                502,
                json={
                    "error": {
                        "code": "session_brief_not_posted",
                        "message": "session conv_2 started but the brief was not posted",
                        "session_id": "conv_2",
                        "path": "/c/conv_2",
                    }
                },
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("POST", "/v1/ae/sessions")
        seen.append(json.loads(request.content))
        return next(answers)

    async with connect(_stub(handler)) as session:
        args = {"title": "T", "brief": "B", "workspace": "", "ride_unparked": False}
        ok = _payload(await session.call_tool("create_session", args))
        assert ok["url"] == "http://box.test/c/conv_1"
        assert seen[0] == {"title": "T", "brief": "B", "ride_unparked": False}
        full = _payload(await session.call_tool("create_session", {"title": "T", "brief": "B"}))
        assert full["outcome"] == "slots_full"
        assert full["working"] == [{"id": "conv_a", "title": "A"}]
        assert "ask_reid" in full["instruction"]
        half = _payload(await session.call_tool("create_session", {"title": "T", "brief": "B"}))
        assert half["error"].startswith("Could not start the session: 502 session conv_2")
        assert half["url"] == "http://box.test/c/conv_2"
    assert "t0ken" not in json.dumps([ok, full, half])


async def test_create_session_through_the_real_route(
    omni: OmniClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool on the app: the idempotent answer for a key a live session carries."""
    from tests.server.helpers import create_test_session as _create

    async with connect(omni) as session:
        client = omni._client()
        live = (
            await _create(
                client,
                title="Overflow",
                labels={labels.ORCH_KEY: "claude/x", labels.STATUS: "working"},
            )
        )["id"]
        body = _payload(
            await session.call_tool(
                "create_session", {"title": "Overflow", "brief": "Go.", "key": "claude/x"}
            )
        )
    assert (body["session_id"], body["existing"]) == (live, True)
    assert body["url"] == f"http://test/c/{live}"


async def test_post_message_posts_one_user_turn() -> None:
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/conv_ok/events":
            posted.append(json.loads(request.content))
            return httpx.Response(202, json={})
        return httpx.Response(404, json={"error": {"message": "no session conv_gone"}})

    async with connect(_stub(handler)) as session:
        ok = _payload(
            await session.call_tool("post_message", {"session_id": "conv_ok", "text": "Rebase?"})
        )
        gone = _payload(
            await session.call_tool("post_message", {"session_id": "conv_gone", "text": "Hi"})
        )
        empty = _payload(
            await session.call_tool("post_message", {"session_id": "conv_ok", "text": "  "})
        )
    assert ok == {"posted": True, "session_id": "conv_ok", "status": 202}
    assert posted == [
        {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "Rebase?"}]},
        }
    ]
    assert gone["error"] == "Could not post to conv_gone: 404 no session conv_gone"
    assert empty["error"].startswith("text is empty")


ORCHESTRATED = [
    {
        "id": "conv_fix",
        "title": "Overflow fix",
        "status": "working",
        "blocked": False,
        "failed": False,
        "repo": "analyticendeavors/omnigent-ae",
        "branch": "claude/overflow",
    },
    {
        "id": "conv_done",
        "title": "Docs",
        "status": "working",
        "blocked": False,
        "failed": False,
        "repo": "analyticendeavors/omnigent-ae",
        "branch": "claude/docs",
    },
]
SERVER_CARD = {
    "repo": "analyticendeavors/omnigent-ae",
    "number": 50,
    "url": "https://github.com/analyticendeavors/omnigent-ae/pull/50",
    "state": "open",
    "head_ref": "claude/ci",
    "ci": {"state": "failing", "failing": 1, "total": 3},
    "mergeable": {"state": "clean"},
    "review": {"state": "none"},
    "merge": {"allowed": False},
    "updated_at": "2026-09-25T03:00:00Z",
    "sessions": [{"id": "conv_ci", "title": "CI fix", "status": "working"}],
}


def _server(prs_body: dict[str, Any] | None) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ae/sessions":
            return httpx.Response(200, json={"items": ORCHESTRATED, "slots_used": 2, "limit": 3})
        if request.url.path == "/v1/ae/prs" and prs_body is not None:
            return httpx.Response(200, json=prs_body)
        return httpx.Response(500, text="boom")

    return handler


async def test_list_prs_degrades_plainly_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    from ae_omni_policies import gh

    async def _missing() -> dict[str, Any]:
        return {"available": False, "reason": gh.NOT_INSTALLED, "binary": None}

    monkeypatch.setattr(gh, "status", _missing)
    body = {"configured": True, "items": [SERVER_CARD], "errors": []}
    async with connect(_stub(_server(body))) as session:
        answer = _payload(await session.call_tool("list_prs", {}))
    assert answer["gh"] == {"available": False, "reason": gh.NOT_INSTALLED}
    assert "gh auth login" in answer["gh"]["reason"]
    (pr,) = answer["prs"]
    assert pr["url"] == SERVER_CARD["url"]
    assert pr["attention"] == "rebrief_ci"
    assert pr["sessions"][0]["id"] == "conv_ci"
    assert "merge" not in pr, "the merge gate is Reid's, not the orchestrator's"
    assert [row["id"] for row in answer["sessions"]] == ["conv_fix", "conv_done"]

    async with connect(_stub(_server(None))) as session:
        down = _payload(await session.call_tool("list_prs", {}))
    assert down["prs"] == []
    assert down["server_cards"]["configured"] is False
    assert down["server_cards"]["note"].startswith("GET /v1/ae/prs failed: 500")


async def test_list_prs_merges_gh_matches_owners_and_flags_attention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ae_omni_policies import gh

    async def _ready() -> dict[str, Any]:
        return {"available": True, "reason": None, "binary": "/usr/bin/gh"}

    asked: list[tuple[str, str]] = []

    async def _pulls(binary: str, repo: str, *, state: str, limit: int) -> list[dict[str, Any]]:
        asked.append((repo, state))
        if repo == "analyticendeavors/other":
            raise RuntimeError("gh pr list --repo analyticendeavors/other failed: no access")
        base = {"repo": repo, "review": {"state": "none"}, "ci": {"state": "passing"}}
        if state == "merged":
            return [
                base
                | {
                    "url": "https://github.com/analyticendeavors/omnigent-ae/pull/48",
                    "state": "merged",
                    "head_ref": "claude/docs",
                    "mergeable": {"state": "merged"},
                }
            ]
        return [
            base
            | {
                "url": "https://github.com/analyticendeavors/omnigent-ae/pull/49",
                "state": "open",
                "head_ref": "claude/overflow",
                "mergeable": {"state": "conflicted"},
                "updated_at": "2026-09-25T04:00:00Z",
            },
            base
            | {
                "url": SERVER_CARD["url"]
                .upper()
                .replace("HTTPS://GITHUB.COM", "https://github.com"),
                "state": "open",
                "head_ref": "claude/ci",
                "mergeable": {"state": "clean"},
            },
        ]

    monkeypatch.setattr(gh, "status", _ready)
    monkeypatch.setattr(gh, "pull_requests", _pulls)
    body = {"configured": True, "items": [SERVER_CARD], "errors": []}
    async with connect(_stub(_server(body))) as session:
        answer = _payload(
            await session.call_tool(
                "list_prs", {"repos": ["AnalyticEndeavors/Other"], "include_merged": True}
            )
        )
    assert sorted(asked) == [
        ("analyticendeavors/omnigent-ae", "merged"),
        ("analyticendeavors/omnigent-ae", "open"),
        ("analyticendeavors/other", "merged"),
        ("analyticendeavors/other", "open"),
    ]
    by_url = {pr["url"]: pr for pr in answer["prs"]}
    assert len(by_url) == 3, "the server's card and gh's row for #50 are one entry"
    conflict = by_url["https://github.com/analyticendeavors/omnigent-ae/pull/49"]
    assert conflict["attention"] == "rebrief_conflict"
    assert [owner["id"] for owner in conflict["sessions"]] == ["conv_fix"]
    assert conflict["sessions"][0]["url"] == "http://box.test/c/conv_fix"
    merged = by_url["https://github.com/analyticendeavors/omnigent-ae/pull/48"]
    assert merged["attention"] == "close_out"
    assert [owner["id"] for owner in merged["sessions"]] == ["conv_done"]
    assert by_url[SERVER_CARD["url"]]["source"] == ["server", "gh"]
    assert answer["gh"] == {"available": True, "reason": None}
    assert any("no access" in error for error in answer["errors"])


async def test_ask_reid_is_a_pop_up_with_the_recommendation_first(omni: OmniClient) -> None:
    asked: list[types.ElicitRequestParams] = []

    async def pick_second(context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        asked.append(params)
        return types.ElicitResult(action="accept", content={"choice": "Park Docs"})

    async def pick_first(context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        return types.ElicitResult(action="accept", content={"choice": "Wait (Recommended)"})

    async def dismiss(context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        return types.ElicitResult(action="decline")

    args = {
        "question": "Slots are full. What now?",
        "options": ["Wait", "Park Docs", "Run anyway"],
    }
    server = create_server(omni)
    async with create_connected_server_and_client_session(
        server, elicitation_callback=pick_second
    ) as session:
        second = _payload(await session.call_tool("ask_reid", args))
    (params,) = asked
    assert params.message == "Slots are full. What now?"
    assert params.requestedSchema["properties"]["choice"]["enum"] == [
        "Wait (Recommended)",
        "Park Docs",
        "Run anyway",
    ]
    assert second == {"answer": "Park Docs", "action": "accept", "recommended": False}
    async with create_connected_server_and_client_session(
        create_server(omni), elicitation_callback=pick_first
    ) as session:
        first = _payload(await session.call_tool("ask_reid", args))
        # One option is refused by the schema before the handler runs.
        bad = await session.call_tool("ask_reid", {"question": "?", "options": ["one"]})
    assert first == {"answer": "Wait", "action": "accept", "recommended": True}
    assert bad.isError is True
    assert await mcp_module.ask_reid(None, {"question": " ", "options": ["a", "b"]}) == {
        "error": "ask_reid needs a question and two to four options"
    }
    async with create_connected_server_and_client_session(
        create_server(omni), elicitation_callback=dismiss
    ) as session:
        declined = _payload(await session.call_tool("ask_reid", args))
    assert (declined["answer"], declined["action"]) == (None, "decline")
    # A client with no elicitation support: the answer says to ask in chat.
    async with connect(omni) as session:
        fallback = _payload(await session.call_tool("ask_reid", args))
    assert fallback["answer"] is None
    assert fallback["instruction"].startswith("Ask in chat instead")


async def test_list_prs_reads_reported_urls_so_a_merge_is_seen_without_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ae_omni_policies import gh

    async def _missing() -> dict[str, Any]:
        return {"available": False, "reason": gh.NOT_LOGGED_IN, "binary": None}

    monkeypatch.setattr(gh, "status", _missing)
    merged = SERVER_CARD | {
        "url": "https://github.com/analyticendeavors/omnigent-ae/pull/48",
        "number": 48,
        "state": "merged",
        "sessions": [],
    }
    asked: list[str] = []
    base = _server({"configured": True, "items": [SERVER_CARD], "errors": []})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/ae/prs/"):
            asked.append(request.url.path)
            if request.url.path.endswith("/48"):
                return httpx.Response(200, json=merged)
            return httpx.Response(502, json={"error": {"message": "GitHub said 404"}})
        return base(request)

    urls = [
        "https://github.com/AnalyticEndeavors/omnigent-ae/pull/48",
        SERVER_CARD["url"],
        "https://github.com/analyticendeavors/omnigent-ae/pull/47",
        "https://example.com/not/a/pull",
    ]
    async with connect(_stub(handler)) as session:
        answer = _payload(await session.call_tool("list_prs", {"urls": urls}))
    assert asked == [
        "/v1/ae/prs/analyticendeavors/omnigent-ae/48",
        "/v1/ae/prs/analyticendeavors/omnigent-ae/47",
    ], "the open card was not read twice"
    by_url = {pr["url"]: pr for pr in answer["prs"]}
    assert by_url[merged["url"]]["attention"] == "close_out"
    assert any("pull/47: 502 GitHub said 404" in error for error in answer["errors"])
    assert any("not a github.com pull request URL" in error for error in answer["errors"])
