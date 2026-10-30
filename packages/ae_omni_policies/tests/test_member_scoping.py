"""What a signed-in non-admin may read under ``/v1/ae`` (2026-10-28).

The owner and a second box user may both be admins for the box, but neither may see anything
sensitive about the other's work. The second user's chats run on their own host, so every
non-shared login in the limits store, the whole task store and the box's infra
pages belong to the owner. ``ae_admins`` makes owner the only admin; ``member`` is a
signed-in user who is not.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from omnigent.runtime import get_conversation_store
from tests.server.helpers import create_test_session

from ae_omni_policies import env_drift, labels, limits, your_move
from ae_omni_policies.limits import LimitSnapshot

OWNER = {"X-Forwarded-Email": "owner@example.com"}
MEMBER = {"X-Forwarded-Email": "member@example.com"}
NOW = datetime(2026, 10, 28, 16, 0, tzinfo=UTC)
PARK_BODY: dict[str, Any] = {
    "thread": "eur-currency-formatting",
    "state": "The formatting helper drops the EUR symbol.",
    "next": "Open shared/js/money.js and add the EUR branch.",
    "slot": "never opened",
    "quadrant": "feature",
    "est": "about an hour",
    "from": "pricing padding fix, Claude Code, 2026-09-23",
}


def _window(window_id: str, used: float) -> limits.LimitWindow:
    return limits.LimitWindow(id=window_id, label=window_id, used_percent=used, window_minutes=300)


def _claude(*sessions: limits.SessionLimits) -> LimitSnapshot:
    """A host reading with the default login, a backup and a shared account signed in."""
    return LimitSnapshot(
        harness="claude",
        source="statusline+usage",
        observed_at=NOW,
        plan="Max",
        windows=[_window("five_hour", 41), _window("seven_day", 12)],
        credits=limits.Credits(enabled=True),
        hint=limits.PROFILE_HINT,
        contexts=[
            limits.SessionContext(
                conversation_id=s.conversation_id,
                observed_at=NOW,
                model="claude-opus",
                window_size=200_000,
                used_tokens=1234,
            )
            for s in sessions
        ],
        sessions=list(sessions),
        known_accounts=[
            limits.KnownAccount(account=None, label="Default CLI account"),
            limits.KnownAccount(account="Backup", label="Backup label"),
            limits.KnownAccount(account="shared:team", label="Team"),
        ],
    )


def _entry(conversation: str, account: str | None, used: float) -> limits.SessionLimits:
    return limits.SessionLimits(
        conversation_id=conversation,
        observed_at=NOW,
        account=account,
        account_label=f"{account} label" if account else None,
        windows=[_window("five_hour", used)],
    )


async def _post(client: httpx.AsyncClient, snapshot: LimitSnapshot) -> None:
    response = await client.post(
        f"/v1/ae/limits/{snapshot.harness}",
        json=snapshot.model_dump(mode="json"),
        headers=OWNER,
    )
    assert response.status_code == 200, response.text


async def _session(
    client: httpx.AsyncClient, who: dict[str, str], title: str, seed: dict[str, str] | None = None
) -> str:
    client.headers.update(who)
    try:
        return (await create_test_session(client, title=title, labels=seed))["id"]
    finally:
        del client.headers["X-Forwarded-Email"]


# ── infra pages: admin only ─────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/host", "/secrets", "/fabric", "/databricks"])
async def test_the_box_infra_pages_are_admin_only(
    client: httpx.AsyncClient, ae_admins: set[str], path: str
) -> None:
    refused = await client.get(f"/v1/ae{path}", headers=MEMBER)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "admin_only"
    answered = await client.get(f"/v1/ae{path}", headers=OWNER)
    assert answered.status_code == 200, answered.text
    if path != "/secrets":
        assert answered.json()["can_act"] is True


async def test_a_release_hides_the_missing_server_settings_from_a_non_admin(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env_drift, "missing", lambda *_a, **_k: ["AE_SECRET_X"])
    mine = (await client.get("/v1/ae/release", headers=OWNER)).json()
    theirs = (await client.get("/v1/ae/release", headers=MEMBER)).json()
    assert mine["env_missing"] == ["AE_SECRET_X"]
    assert theirs["env_missing"] == []
    assert {k: v for k, v in theirs.items() if k != "env_missing"} == {
        k: v for k, v in mine.items() if k != "env_missing"
    }


# ── the Parking Lot: admin only ─────────────────────────────────────────


async def test_every_lot_route_is_admin_only(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    calls = [
        ("GET", "/v1/ae/lot", None),
        ("GET", "/v1/ae/lot/tags", None),
        ("POST", "/v1/ae/lot/park", PARK_BODY),
        ("POST", "/v1/ae/lot/T1/link", {"url": "https://github.com/a/b/issues/1"}),
        ("POST", "/v1/ae/lot/T1/complete", {"status": "done"}),
        ("POST", "/v1/ae/lot/T1/resume", None),
        ("POST", "/v1/ae/lot/T1/session", None),
    ]
    for method, path, body in calls:
        refused = await client.request(method, path, json=body, headers=MEMBER)
        assert refused.status_code == 403, (path, refused.text)
        assert refused.json()["error"]["code"] == "admin_only", path
        asked = await client.request(method, path, json=body, headers=OWNER)
        assert asked.status_code != 403, (path, asked.text)


async def test_your_move_gives_a_non_admin_no_lot_or_pull_requests(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def lot_source(_today: str) -> dict[str, Any]:
        return {"ok": True, "today": "2026-10-28", "items": [{"id": "T1"}], "note": None}

    async def pulls_source() -> dict[str, Any]:
        return {"ok": True, "configured": True, "items": [{"url": "u"}], "errors": []}

    monkeypatch.setattr(your_move, "lot_source", lot_source)
    monkeypatch.setattr(your_move, "pulls_source", pulls_source)
    mine = (await client.get("/v1/ae/your-move", headers=OWNER)).json()
    theirs = (await client.get("/v1/ae/your-move", headers=MEMBER)).json()
    assert [item["id"] for item in mine["lot"]["items"]] == ["T1"]
    assert len(mine["pulls"]["items"]) == 1
    assert theirs["lot"]["ok"] is True and theirs["lot"]["items"] == []
    assert theirs["pulls"]["ok"] is True and theirs["pulls"]["items"] == []
    assert theirs["total"] == 0


# ── capacity and the orchestrator's session list ────────────────────────


async def test_capacity_counts_the_box_but_lists_only_the_callers_sessions(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    mine = await _session(client, OWNER, "Owner's work", {labels.STATUS: "working"})
    theirs = await _session(client, MEMBER, "Member's work", {labels.STATUS: "working"})
    owner = (await client.get("/v1/ae/capacity", headers=OWNER)).json()
    member = (await client.get("/v1/ae/capacity", headers=MEMBER)).json()
    assert [row["id"] for row in owner["working"]] == [mine]
    assert [row["id"] for row in member["working"]] == [theirs]
    assert owner["slots_used"] == member["slots_used"] == 2
    assert owner["limit"] == member["limit"]
    assert owner["now"][:10] == member["now"][:10]


async def test_the_orchestrated_session_list_is_the_callers_own(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    seed = {labels.STATUS: "working", labels.ORCH: "1", labels.ORCH_KEY: "k"}
    mine = await _session(client, OWNER, "Owner's child", seed)
    await _session(client, MEMBER, "Member's child", seed)
    answer = (await client.get("/v1/ae/sessions", headers=OWNER)).json()
    assert [row["id"] for row in answer["items"]] == [mine]
    store = get_conversation_store()
    assert store.get_conversation(mine) is not None


# ── plan usage ──────────────────────────────────────────────────────────


async def test_accounts_for_a_non_admin_are_the_shared_ones_only(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    await _post(client, _claude())
    mine = (await client.get("/v1/ae/limits/accounts", headers=OWNER)).json()["accounts"]
    theirs = (await client.get("/v1/ae/limits/accounts", headers=MEMBER)).json()["accounts"]
    assert {row["account"] for row in mine} >= {None, "Backup", "shared:team"}
    assert [(row["harness"], row["account"]) for row in theirs] == [("claude", "shared:team")]


@pytest.mark.parametrize("params", [{}, {"account": ""}, {"account": "Backup"}])
async def test_a_non_admin_reads_none_of_the_box_hosts_usage(
    client: httpx.AsyncClient, ae_admins: set[str], params: dict[str, str]
) -> None:
    await _post(client, _claude(_entry("conv_box", None, 41)))
    await _post(
        client,
        LimitSnapshot(harness="codex", source="app-server", observed_at=NOW, plan="pro"),
    )
    body = (await client.get("/v1/ae/limits", params=params, headers=MEMBER)).json()
    claude = body["harnesses"]["claude"]
    assert body["harnesses"]["codex"] is None
    assert claude["windows"] == [] and claude["plan"] is None and claude["credits"] is None
    assert claude["account"] is None and claude["account_label"] is None
    assert claude["contexts"] == [] and claude["sessions"] == []
    assert claude["note"] == limits.NOT_SHARED_NOTE
    owner = (await client.get("/v1/ae/limits", headers=OWNER)).json()["harnesses"]
    assert owner["claude"]["windows"] and owner["codex"] is not None
    assert len(owner["claude"]["sessions"]) == 1


async def test_a_session_the_relay_never_posted_is_blank_not_the_default_login(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    sid = await _session(client, MEMBER, "Member's chat on their own host")
    await _post(client, _claude(_entry("conv_box", None, 41)))
    body = (await client.get("/v1/ae/limits", params={"session_id": sid}, headers=MEMBER)).json()
    claude = body["harnesses"]["claude"]
    assert claude["windows"] == [] and claude["sessions"] == [] and claude["contexts"] == []


async def test_a_relayed_session_the_caller_may_open_keeps_only_its_own_rows(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    sid = await _session(client, MEMBER, "Shared into the box host")
    await _post(
        client,
        _claude(_entry(sid, "Backup", 25), _entry("conv_owner_other", None, 41)),
    )
    body = (await client.get("/v1/ae/limits", params={"session_id": sid}, headers=MEMBER)).json()
    claude = body["harnesses"]["claude"]
    assert claude["windows"][0]["used_percent"] == 25
    assert [row["conversation_id"] for row in claude["sessions"]] == [sid]
    assert [row["conversation_id"] for row in claude["contexts"]] == [sid]


@pytest.mark.parametrize(
    "params", [{"account": "shared:team"}, {"session_id": "temp:x", "account": "shared:team"}]
)
async def test_the_picker_still_reads_a_shared_accounts_windows(
    client: httpx.AsyncClient, ae_admins: set[str], params: dict[str, str]
) -> None:
    team = _entry("conv_team", "shared:team", 77)
    await _post(client, _claude(team))
    body = (await client.get("/v1/ae/limits", params=params, headers=MEMBER)).json()
    claude = body["harnesses"]["claude"]
    assert claude["account"] == "shared:team"
    assert claude["windows"][0]["used_percent"] == 77
    assert claude["sessions"] == []


async def test_the_shared_account_list_stays_open_to_a_non_admin(
    client: httpx.AsyncClient, ae_admins: set[str]
) -> None:
    response = await client.get("/v1/ae/claude-accounts/shared", headers=MEMBER)
    assert response.status_code == 200, response.text
    assert response.json()["can_manage"] is False


def test_member_view_leaves_the_stored_view_alone() -> None:
    store = limits.LimitStore()
    store.put(_claude(_entry("conv_box", None, 41)))
    full = store.view()
    held = {"harnesses": {k: dict(v) if v else v for k, v in full["harnesses"].items()}}
    limits.member_view(full, session_id=None, relayed=False)
    assert full["harnesses"]["claude"]["sessions"] == held["harnesses"]["claude"]["sessions"]
    assert store.has_session_entry("conv_box") and not store.has_session_entry("conv_other")


# ── review round (2026-10-28) ───────────────────────────────────────────


async def test_starting_a_session_leaks_no_other_users_sessions(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    for index in range(3):
        seed = {labels.STATUS: "working", labels.ORCH: "1", labels.ORCH_KEY: f"k{index}"}
        await _session(client, OWNER, f"Owner private {index}", seed)
    body = {"title": "Member's own", "brief": "go", "key": "k0"}
    full = await client.post("/v1/ae/sessions", json=body, headers=MEMBER)
    # A guessed key finds nothing of the owner's, and the full-slot answer names none of it.
    assert full.status_code == 409, full.text
    error = full.json()["error"]
    assert error["code"] == "slots_full"
    assert "Owner private" not in full.text
    assert error["working"] == [] and error["others_working"] == 3
    assert "Plus 3 of another user's." in error["message"]
    mine = await client.post("/v1/ae/sessions", json=body, headers=OWNER)
    assert mine.status_code == 200, mine.text
    assert mine.json()["existing"] is True and mine.json()["title"] == "Owner private 0"


async def test_a_context_only_row_is_not_a_scoped_reading(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    sid = await _session(client, MEMBER, "Context only")
    snapshot = _claude(_entry("conv_other", None, 41))
    snapshot.contexts.append(
        limits.SessionContext(
            conversation_id=sid,
            observed_at=NOW,
            model="m",
            window_size=200_000,
            used_tokens=5,
        )
    )
    await _post(client, snapshot)
    body = (await client.get("/v1/ae/limits", params={"session_id": sid}, headers=MEMBER)).json()
    claude = body["harnesses"]["claude"]
    assert claude["windows"] == [] and claude["plan"] is None
    assert claude["note"] == limits.NOT_SHARED_NOTE


async def test_capacity_counts_other_users_working_sessions_for_an_admin_too(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    mine = await _session(client, OWNER, "Owner's", {labels.STATUS: "working"})
    for title in ("Member one", "Member two"):
        await _session(client, MEMBER, title, {labels.STATUS: "working"})
    owner = (await client.get("/v1/ae/capacity", headers=OWNER)).json()
    assert [row["id"] for row in owner["working"]] == [mine]
    assert owner["slots_used"] == 3 and owner["others_working"] == 2
    member = (await client.get("/v1/ae/capacity", headers=MEMBER)).json()
    assert len(member["working"]) == 2 and member["others_working"] == 1
    assert "Owner's" not in str((await client.get("/v1/ae/capacity", headers=MEMBER)).text)


# ── the admin question can fail ─────────────────────────────────────────


async def test_a_failed_admin_probe_is_a_retriable_503_not_a_refusal(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ae_omni_policies import shared_accounts_routes

    async def broken(_request: Any, _user: str | None) -> bool | None:
        return None

    monkeypatch.setattr(shared_accounts_routes, "admin_probe", broken)
    unknown = await client.get("/v1/ae/secrets", headers=OWNER)
    assert unknown.status_code == 503, unknown.text
    assert unknown.json()["error"]["code"] == "admin_unknown"
    assert "Retry" in unknown.json()["error"]["message"]


async def test_the_last_confirmed_admin_answer_stands_while_the_probe_is_down(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ae_omni_policies import shared_accounts_routes

    clock = [1000.0]
    monkeypatch.setattr(shared_accounts_routes.time, "monotonic", lambda: clock[0])
    assert (await client.get("/v1/ae/secrets", headers=OWNER)).status_code == 200

    async def broken(_request: Any, _user: str | None) -> bool | None:
        return None

    monkeypatch.setattr(shared_accounts_routes, "admin_probe", broken)
    clock[0] += 120  # past the 30 s reuse, inside the 10 minute stale limit
    assert (await client.get("/v1/ae/secrets", headers=OWNER)).status_code == 200
    clock[0] += 600  # now too old to trust
    assert (await client.get("/v1/ae/secrets", headers=OWNER)).status_code == 503


async def test_a_redacted_your_move_never_serves_an_admin_and_unknown_is_not_cached(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ae_omni_policies import shared_accounts_routes

    async def lot_source(_today: str) -> dict[str, Any]:
        return {"ok": True, "today": "2026-10-28", "items": [{"id": "T1"}], "note": None}

    monkeypatch.setattr(your_move, "lot_source", lot_source)
    verdict: list[bool | None] = [None]

    async def probe(_request: Any, _user: str | None) -> bool | None:
        return verdict[0]

    monkeypatch.setattr(shared_accounts_routes, "admin_probe", probe)
    down = (await client.get("/v1/ae/your-move", headers=OWNER)).json()
    assert down["lot"]["items"] == []
    verdict[0] = True
    back = (await client.get("/v1/ae/your-move", headers=OWNER)).json()
    assert [row["id"] for row in back["lot"]["items"]] == ["T1"]


# ── small leaks ─────────────────────────────────────────────────────────


async def test_health_names_the_task_store_url_to_an_admin_only(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_TASK_STORE_URL", "https://tasks.example")
    admin = (await client.get("/v1/ae/health", headers=OWNER)).json()["task_store"]
    member = (await client.get("/v1/ae/health", headers=MEMBER)).json()["task_store"]
    anonymous = await client.get("/v1/ae/health")
    assert "url" in admin and "url" not in member
    # The app gate may refuse an anonymous caller first; if it answers, it names no URL.
    assert anonymous.status_code in (200, 401)
    if anonymous.status_code == 200:
        assert "url" not in anonymous.json()["task_store"]


async def test_a_gate_reply_is_the_same_404_for_a_session_you_cannot_open(
    client: httpx.AsyncClient, ae_permissions: set[str]
) -> None:
    sid = await _session(client, OWNER, "Owner's gate", {labels.GATE: "open"})
    body = {"route": "A"}
    assert (await client.post(f"/v1/ae/gate/{sid}/reply", json=body)).status_code == 401
    theirs = await client.post(f"/v1/ae/gate/{sid}/reply", json=body, headers=MEMBER)
    absent = await client.post(f"/v1/ae/gate/conv_{'f' * 32}/reply", json=body, headers=MEMBER)
    assert theirs.status_code == absent.status_code == 404
    assert theirs.json()["error"]["code"] == absent.json()["error"]["code"]


# ── what counts as an answer about the admin (round 3, 2026-10-28) ──────


@pytest.mark.parametrize(
    ("status", "body", "want"),
    [
        (200, {"user_id": "owner@example.com", "is_admin": True}, True),
        (200, {"user_id": "owner@example.com", "is_admin": False}, False),
        (403, {}, False),
        (401, {}, None),
        (408, {}, None),
        (429, {}, None),
        (502, {}, None),
        (404, {}, None),
    ],
)
async def test_only_a_clean_answer_settles_the_admin_question(
    monkeypatch: pytest.MonkeyPatch, status: int, body: dict[str, Any], want: bool | None
) -> None:
    from ae_omni_policies import shared_accounts_routes as routes

    async def ask(_request: Any, **_kwargs: Any) -> httpx.Response:
        return httpx.Response(status, json=body)

    monkeypatch.setattr(routes, "ask_me", ask)
    assert await routes._probe_admin(None, "owner@example.com") is want  # type: ignore[arg-type]


async def test_a_dropped_loopback_is_unknown_and_a_hiccup_is_never_cached_as_no(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ae_omni_policies import shared_accounts_routes as routes

    async def down(_request: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(routes, "ask_me", down)
    assert await routes._probe_admin(None, "owner@example.com") is None  # type: ignore[arg-type]
    answers: list[httpx.Response | None] = [httpx.Response(429, json={}), None]

    async def next_answer(_request: Any, **_kwargs: Any) -> httpx.Response | None:
        return (
            answers.pop(0)
            if answers
            else httpx.Response(200, json={"user_id": "owner@example.com", "is_admin": True})
        )

    monkeypatch.setattr(routes, "ask_me", next_answer)
    routes.reset_admin_cache()
    assert await routes.admin_state(None, "owner@example.com") is None  # type: ignore[arg-type]
    assert await routes.admin_state(None, "owner@example.com") is None  # type: ignore[arg-type]
    # Neither hiccup was remembered: the next clean answer decides.
    assert await routes.admin_state(None, "owner@example.com") is True  # type: ignore[arg-type]


async def test_the_stale_admin_answer_lasts_ten_minutes_and_then_expires(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ae_omni_policies import shared_accounts_routes

    clock = [1000.0]
    monkeypatch.setattr(shared_accounts_routes.time, "monotonic", lambda: clock[0])
    assert (await client.get("/v1/ae/secrets", headers=OWNER)).status_code == 200

    async def broken(_request: Any, _user: str | None) -> bool | None:
        return None

    monkeypatch.setattr(shared_accounts_routes, "admin_probe", broken)
    clock[0] += 599
    assert (await client.get("/v1/ae/secrets", headers=OWNER)).status_code == 200
    clock[0] += 2  # 601 s after the confirmation
    assert (await client.get("/v1/ae/secrets", headers=OWNER)).status_code == 503


async def test_a_401_from_me_drops_the_cached_admin_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ae_omni_policies import shared_accounts_routes as routes

    routes.reset_admin_cache()
    answers = [
        httpx.Response(200, json={"user_id": "owner@example.com", "is_admin": True}),
        httpx.Response(401, json={}),
    ]

    async def ask(_request: Any, **_kwargs: Any) -> httpx.Response:
        return answers.pop(0)

    monkeypatch.setattr(routes, "ask_me", ask)
    clock = [1000.0]
    monkeypatch.setattr(routes.time, "monotonic", lambda: clock[0])
    assert await routes.admin_state(None, "owner@example.com") is True  # type: ignore[arg-type]
    clock[0] += 60  # past the 30 s reuse, so the probe is asked again and answers 401
    assert await routes.admin_state(None, "owner@example.com") is None  # type: ignore[arg-type]
    assert "owner@example.com" not in routes._admin_seen


async def test_a_box_admin_who_is_not_an_omnigent_admin_keeps_only_the_box_monitor(
    client: httpx.AsyncClient, ae_admins: set[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_BOX_ADMINS", "member@example.com")
    box = await client.get("/v1/ae/box-health", headers=MEMBER)
    access = await client.get("/v1/ae/access", headers=MEMBER)
    assert box.status_code == 200, box.text
    assert access.status_code == 200, access.text
    assert access.json() == {"admin": False, "box_admin": True}
    for path in ("/host", "/secrets", "/fabric", "/databricks", "/lot"):
        refused = await client.get(f"/v1/ae{path}", headers=MEMBER)
        assert refused.status_code == 403, (path, refused.text)
        assert refused.json()["error"]["code"] == "admin_only", path
