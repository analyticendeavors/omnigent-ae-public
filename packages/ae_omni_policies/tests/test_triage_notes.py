"""The backfill of the Triage card's saved notes, against a fake server (2026-10-19)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ae_omni_policies import labels, triage_notes
from ae_omni_policies.omni import Settings, http_client
from ae_omni_policies.triage_notes import backfill, missing_notes


def _says(index: int, text: str, role: str = "assistant") -> dict[str, Any]:
    kind = "output_text" if role == "assistant" else "input_text"
    return {
        "id": f"item_{index}",
        "type": "message",
        "role": role,
        "content": [{"type": kind, "text": text}],
    }


class _Server:
    """Sessions and items as the real routes page them, and the PATCHes it took."""

    def __init__(self, sessions: list[dict[str, Any]], items: dict[str, list[dict]]) -> None:
        self.sessions = sessions
        self.items = items
        self.patches: list[tuple[str, dict[str, str]]] = []
        self.item_reads: list[str] = []
        self.patch_status = 200
        self.unreadable: set[str] = set()
        # Pages for /v1/sessions in order, each reached by the previous page's last_id.
        self.session_pages: list[dict[str, Any]] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        if request.method == "GET" and path == "/v1/sessions":
            assert params["include_archived"] == "true"
            assert (params["kind"], params["visibility"]) == ("any", "mine")
            if self.session_pages is None:
                return httpx.Response(200, json={"data": self.sessions, "has_more": False})
            cursors = [None] + [page["last_id"] for page in self.session_pages]
            return httpx.Response(200, json=self.session_pages[cursors.index(params.get("after"))])
        if request.method == "GET" and path.endswith("/items"):
            session_id = path.split("/")[3]
            self.item_reads.append(session_id)
            if session_id in self.unreadable:
                return httpx.Response(500, json={"detail": "store down"})
            rows = self.items.get(session_id, [])
            start = 0
            if "after" in params:
                start = [row["id"] for row in rows].index(params["after"]) + 1
            page = rows[start : start + 2]
            return httpx.Response(
                200,
                json={
                    "data": page,
                    "has_more": start + 2 < len(rows),
                    "last_id": page[-1]["id"] if page else None,
                },
            )
        if request.method == "PATCH" and path.startswith("/v1/sessions/"):
            body = json.loads(request.content)
            self.patches.append((path.rsplit("/", 1)[1], body["labels"]))
            return httpx.Response(self.patch_status, json={"labels": body["labels"]})
        return httpx.Response(404, json={"detail": f"no route {request.method} {path}"})


def _client(server: _Server) -> httpx.AsyncClient:
    return http_client(
        Settings(server_url="http://test", token="tok-1"),
        transport=httpx.MockTransport(server.handle),
    )


def _fixture() -> _Server:
    return _Server(
        sessions=[
            {
                "id": "s_old",
                "title": "Counted before notes",
                "labels": {labels.ABSORBED: "2", labels.UNFILED: "1", "ae.unfiled.1": "kept"},
            },
            {"id": "s_none", "title": "Nothing counted", "labels": {}},
            {
                "id": "s_saved",
                "title": "Already saved",
                "labels": {labels.ABSORBED: "1", "ae.absorbed.1": "saved"},
            },
            {
                "id": "s_odd",
                "title": "Count and history disagree",
                "labels": {labels.ABSORBED: "1"},
            },
        ],
        items={
            "s_old": [
                _says(1, "Absorbed: the padding fix, it blocked the build"),
                _says(2, "Absorbed: a user line never counts", role="user"),
                _says(3, "Plain prose first.\nAbsorbed: the typo, two minutes"),
                _says(4, "UNFILED (target unreachable), file this yourself"),
                _says(5, "Plain prose last."),
            ],
            # Two lines but a count of one: which one was counted cannot be told.
            "s_odd": [_says(1, "Absorbed: first"), _says(2, "Absorbed: second")],
        },
    )


def test_missing_notes_numbers_the_history_and_keeps_a_saved_note() -> None:
    current = {labels.ABSORBED: "2", "ae.absorbed.1": "already saved"}
    found = {labels.ABSORBED: ["first", "second"], labels.UNFILED: ["x"]}
    # Note 1 is kept, and no unfiled line was counted.
    assert missing_notes(current, found) == ({"ae.absorbed.2": "second"}, [])


def test_missing_notes_leaves_a_counter_alone_when_the_history_disagrees() -> None:
    current = {labels.ABSORBED: "2", labels.UNFILED: "1"}
    found = {labels.ABSORBED: ["first", "second", "third"], labels.UNFILED: ["u"]}
    assert missing_notes(current, found) == (
        {"ae.unfiled.1": "u"},
        ["ae.absorbed counts 2 but the history holds 3 such lines"],
    )


def test_missing_notes_stops_at_the_cap_and_names_a_blank_line_by_its_token() -> None:
    count = labels.NOTE_CAP + 5
    found = {labels.ABSORBED: [""] * count}
    updates, skipped = missing_notes({labels.ABSORBED: str(count)}, found)
    assert skipped == []
    assert len(updates) == labels.NOTE_CAP
    assert set(updates.values()) == {"Absorbed"}
    assert labels.note_key(labels.ABSORBED, labels.NOTE_CAP + 1) not in updates


async def test_backfill_writes_only_missing_notes_and_reads_only_sessions_lacking_them() -> None:
    server = _fixture()
    async with _client(server) as http:
        results = await backfill(http)
    # s_old and s_odd lack notes; s_old's five items come in three pages of two.
    assert server.item_reads == ["s_old"] * 3 + ["s_odd"]
    assert server.patches == [
        (
            "s_old",
            {
                "ae.absorbed.1": "the padding fix, it blocked the build",
                "ae.absorbed.2": "the typo, two minutes",
            },
        )
    ]
    assert results == [
        {
            "id": "s_old",
            "title": "Counted before notes",
            "written": ["ae.absorbed.1", "ae.absorbed.2"],
            "skipped": [],
        },
        {
            "id": "s_odd",
            "title": "Count and history disagree",
            "written": [],
            "skipped": ["ae.absorbed counts 1 but the history holds 2 such lines"],
        },
    ]


async def test_backfill_pages_past_a_page_the_list_left_empty() -> None:
    # The session list drops side chats after paging, so a page can be empty with more to come.
    server = _fixture()
    server.session_pages = [
        {"data": [], "has_more": True, "last_id": "side_chat_9"},
        {"data": [server.sessions[0]], "has_more": False, "last_id": "s_old"},
    ]
    async with _client(server) as http:
        results = await backfill(http)
    assert [row["id"] for row in results] == ["s_old"]


async def test_backfill_stops_when_the_cursor_does_not_move() -> None:
    server = _fixture()
    stuck = {"data": [], "has_more": True, "last_id": "x"}
    server.session_pages = [stuck, stuck]
    async with _client(server) as http:
        assert await backfill(http) == []


async def test_a_second_run_writes_nothing() -> None:
    server = _fixture()
    async with _client(server) as http:
        await backfill(http)
        server.sessions[0]["labels"].update(server.patches[0][1])
        assert [row["written"] for row in await backfill(http)] == [[]]
    assert len(server.patches) == 1


async def test_a_dry_run_reports_without_writing() -> None:
    server = _fixture()
    async with _client(server) as http:
        results = await backfill(http, dry_run=True)
    assert server.patches == []
    assert results[0]["written"] == ["ae.absorbed.1", "ae.absorbed.2"]


async def test_a_session_that_cannot_be_read_or_written_is_reported_and_the_run_goes_on() -> None:
    server = _fixture()
    server.patch_status = 403
    server.unreadable = {"s_old"}
    async with _client(server) as http:
        results = await backfill(http)
    assert results[0]["id"] == "s_old"
    assert "answered 500" in results[0]["error"]
    assert results[1]["id"] == "s_odd"
    server.unreadable = set()
    async with _client(server) as http:
        results = await backfill(http)
    assert results[0]["written"] == []
    assert results[0]["error"].startswith("PATCH /v1/sessions/s_old answered 403")


async def test_main_prints_one_line_per_session_written(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _fixture()

    def _fake(settings: Settings) -> httpx.AsyncClient:
        assert settings.server_url == "http://test"
        assert settings.token == "tok-1"
        return http_client(settings, transport=httpx.MockTransport(server.handle))

    monkeypatch.setattr(triage_notes, "http_client", _fake)
    monkeypatch.setenv("OMNIGENT_TOKEN", "tok-1")
    skipped = (
        "left alone on s_odd  Count and history disagree: "
        "ae.absorbed counts 1 but the history holds 2 such lines"
    )
    assert await triage_notes.amain(["http://test/", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "would write 2 note(s) on s_old  Counted before notes",
        skipped,
    ]
    assert await triage_notes.amain(["http://test"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == (
        "wrote 2 note(s) on s_old  Counted before notes"
    )
    server.patch_status = 403
    server.sessions[0]["labels"].pop("ae.absorbed.1", None)
    assert await triage_notes.amain(["http://test"]) == 1
    assert (
        capsys.readouterr()
        .out.splitlines()[0]
        .startswith("failed on s_old  Counted before notes: PATCH /v1/sessions/s_old answered 403")
    )


def test_main_needs_a_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OMNIGENT_SERVER_URL", raising=False)
    assert triage_notes.main([]) == 2
    assert "OMNIGENT_SERVER_URL" in capsys.readouterr().err
