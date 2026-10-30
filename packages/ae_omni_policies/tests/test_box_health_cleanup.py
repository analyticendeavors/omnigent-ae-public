"""The server side of the Box health cleanup buttons: lifecycle, routes, privacy (2026-10-29)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import box_health, box_health_cleanup
from ae_omni_policies.box_health import (
    BoxHealthPost,
    ClaudeRoot,
    Cleanup,
    Cpu,
    Guard,
    Memory,
    OfferGroup,
    OfferItem,
    SlowScan,
    UserHealth,
)
from ae_omni_policies.box_health_cleanup import CleanupStore, Record, Result

ADMIN_A = {"X-Forwarded-Email": "admin-a@example.com"}
ADMIN_B = {"X-Forwarded-Email": "admin-b@example.com"}
GIB = 1024**3
SECRET = "ZQX-client-acme"


def now() -> datetime:
    return datetime.now(UTC)


def offers() -> list[OfferItem]:
    return [
        OfferItem(action="claude_scratch", owner="UserA", count=268, files=190_000, bytes=9 * GIB),
        OfferItem(
            action="tmp_litter",
            owner="UserA",
            count=9500,
            bytes=GIB,
            groups=[
                OfferGroup(group="sec-appr-*", count=7000),
                OfferGroup(group="sec-push-*", count=2500),
            ],
        ),
        OfferItem(action="review_checkouts", owner="UserA", count=6),
        OfferItem(action="claude_scratch", owner="UserB", where="container", count=12, bytes=GIB),
        OfferItem(action="docker_prune", owner="all", where="all", count=1, bytes=6 * GIB),
    ]


def make_post(at: datetime | None = None, enabled: bool = True) -> BoxHealthPost:
    return BoxHealthPost(
        host="box-example",
        read_at=at or now(),
        uptime_s=5000,
        cpu=Cpu(threads=32, load1=1, load5=1, load15=1),
        memory=Memory(total_b=128 * GIB, available_b=100 * GIB),
        guard=Guard(running=True, locked=3, failed=0, age_s=20),
        slow=SlowScan(
            read_at=at or now(),
            users=[
                UserHealth(
                    label="UserA",
                    uid=1000,
                    claude=[
                        ClaudeRoot(
                            where="tmp", sessions=300, stale_sessions=268, oldest_days=5, files=1
                        )
                    ],
                )
            ],
            cleanup=Cleanup(enabled=enabled, actions=offers() if enabled else []),
        ),
    )


def body(**kw: Any) -> dict[str, Any]:
    return json.loads(make_post(**kw).model_dump_json())


ASK = {"action": "claude_scratch", "owner": "UserA", "count": 268}


async def seed(client: httpx.AsyncClient, **kw: Any) -> None:
    assert (
        await client.post("/v1/ae/box-health", json=body(**kw), headers=ADMIN_A)
    ).status_code == 200


@pytest.fixture
def box_admin_b(monkeypatch: pytest.MonkeyPatch, ae_admins: set[str]) -> None:
    ae_admins.add("admin-a@example.com")  # the shared fixture's one admin is another login
    monkeypatch.setenv("AE_BOX_ADMINS", "admin-b@example.com")


# ── the state machine ───────────────────────────────────────────────────────


def record(at: datetime, **kw: Any) -> Record:
    return Record(
        id="a" * 32,
        action="claude_scratch",
        owner="UserA",
        count=1,
        by="admin-a@example.com",
        requested_at=at,
        **kw,
    )


def test_every_state_and_timeout() -> None:
    t = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
    state = box_health_cleanup.state_of
    assert state(record(t), t + timedelta(minutes=3)) == "queued"
    assert state(record(t), t + timedelta(minutes=11)) == "untaken"
    assert state(record(t), t + timedelta(minutes=16)) == "expired"
    assert state(record(t), t + timedelta(hours=26)) is None
    taken = record(t, taken_at=t)
    assert state(taken, t + timedelta(minutes=10)) == "running"
    assert state(taken, t + timedelta(minutes=31)) == "lost"
    ok = Result(id="a" * 32, ok=True, removed=3, finished_at=t)
    assert state(record(t, taken_at=t, result=ok, finished_at=t), t + timedelta(hours=1)) == "done"
    bad = Result(id="a" * 32, ok=False, errors=["EIO"], finished_at=t)
    assert state(record(t, taken_at=t, result=bad, finished_at=t), t) == "failed"
    assert state(record(t, cancelled_at=t), t) == "cancelled"
    assert state(record(t, taken_at=t, result=ok, finished_at=t), t + timedelta(hours=25)) is None


def test_only_one_request_is_active_and_expired_ones_do_not_block(tmp_path: Path) -> None:
    store = CleanupStore(tmp_path / "c.json")
    t = now()
    offer = offers()[0]
    first = store.request(action="claude_scratch", owner="UserA", offer=offer, by="r", now=t)
    assert first is not None
    assert (
        store.request(action="claude_scratch", owner="UserA", offer=offer, by="r", now=t) is None
    )
    later = t + timedelta(minutes=61)
    assert store.request(action="tmp_litter", owner="UserA", offer=offer, by="r", now=later)


def test_cancel_works_until_the_box_takes_it(tmp_path: Path) -> None:
    store = CleanupStore(tmp_path / "c.json")
    t = now()
    assert store.cancel(t) == "none"
    store.request(action="claude_scratch", owner="UserA", offer=offers()[0], by="r", now=t)
    assert store.take(t) is not None
    assert store.take(t) is None  # handed over once
    assert store.cancel(t) == "taken"
    store.finish(Result(id=store.read().id, ok=True), t)  # type: ignore[union-attr]
    store.request(action="claude_scratch", owner="UserA", offer=offers()[0], by="r", now=t)
    assert store.cancel(t) == "ok"
    assert store.take(t) is None


def test_a_result_with_the_wrong_id_is_refused(tmp_path: Path) -> None:
    store = CleanupStore(tmp_path / "c.json")
    t = now()
    rec = store.request(action="claude_scratch", owner="UserA", offer=offers()[0], by="r", now=t)
    assert rec is not None
    assert not store.finish(Result(id="b" * 32, ok=True), t)
    assert not store.finish(Result(id=rec.id, ok=True), t)  # never taken by the box
    assert store.take(t) is not None
    assert store.finish(Result(id=rec.id, ok=True), t)
    assert not store.finish(Result(id=rec.id, ok=True), t)


def test_a_request_the_box_did_not_take_in_fifteen_minutes_is_never_handed_over(
    tmp_path: Path,
) -> None:
    store = CleanupStore(tmp_path / "c.json")
    t = now()
    store.request(action="claude_scratch", owner="UserA", offer=offers()[0], by="r", now=t)
    assert store.take(t + timedelta(minutes=14)) is not None
    store2 = CleanupStore(tmp_path / "d.json")
    store2.request(action="claude_scratch", owner="UserA", offer=offers()[0], by="r", now=t)
    assert store2.take(t + timedelta(minutes=16)) is None
    assert box_health_cleanup.state_of(store2.read(), t + timedelta(minutes=16)) == "expired"  # type: ignore[arg-type]


def test_a_result_holds_only_fixed_words() -> None:
    assert Result(id="a", ok=True, skipped={"my-secret-folder": 1}).skipped == {}
    with pytest.raises(ValueError):
        Result(id="a", ok=False, errors=["/ho" + "me/usera/clients/example"])
    with pytest.raises(ValueError):
        Result(id="a", ok=False, errors=["EIO"] * 21)


def test_a_newer_relays_skip_word_is_dropped_not_refused() -> None:
    result = Result(id="a", ok=True, skipped={"over_confirmed": 2, "future_reason": 3})
    assert result.skipped == {"over_confirmed": 2}
    assert Result(id="a", ok=True, skipped={"future_reason": 1}).skipped == {}


def test_an_older_relays_changed_code_still_reads_in_plain_words() -> None:
    rec = record(now())
    rec.taken_at = now()
    rec.result = Result(id=rec.id, ok=False, errors=["CHANGED"], finished_at=now())
    said = box_health_cleanup.message_of(rec, "failed", show_login=False)
    assert "numbers changed since you confirmed" in said


# ── the routes ──────────────────────────────────────────────────────────────


async def test_a_request_is_queued_handed_over_once_and_finished(
    client: httpx.AsyncClient, box_admin_b: None
) -> None:
    await seed(client)
    ask = await client.post("/v1/ae/box-health/cleanup", json=ASK, headers=ADMIN_B)
    assert ask.status_code == 200, ask.text
    rid = ask.json()["id"]
    view = (await client.get("/v1/ae/box-health", headers=ADMIN_B)).json()
    assert view["cleanup"]["state"] == "queued" and view["cleanup"]["can_cancel"]
    assert view["cleanup"]["by"] == "a box admin"  # a box admin who is not an OmniGent admin
    assert all(not a["can_run"] and a["why_not"] for a in view["actions"])
    # The relay's next post carries the request once, and marks it taken.
    handed = (await client.post("/v1/ae/box-health", json=body(), headers=ADMIN_A)).json()
    assert handed["cleanup"] == {
        "id": rid,
        "action": "claude_scratch",
        "owner": "UserA",
        "count": 268,
    }
    again = (await client.post("/v1/ae/box-health", json=body(), headers=ADMIN_A)).json()
    assert "cleanup" not in again
    assert (await client.get("/v1/ae/box-health", headers=ADMIN_A)).json()["cleanup"]["state"] == (
        "running"
    )
    done = await client.post(
        "/v1/ae/box-health/cleanup/result",
        json={"id": rid, "ok": True, "removed": 268, "bytes_freed": 9 * GIB, "skipped": {}},
        headers=ADMIN_A,
    )
    assert done.status_code == 200, done.text
    seen = (await client.get("/v1/ae/box-health", headers=ADMIN_A)).json()
    assert seen["cleanup"]["state"] == "done"
    assert "Removed 268 folders, freed 9.0 GB" in seen["cleanup"]["message"]


async def test_the_login_shows_only_to_omnigent_admins(
    client: httpx.AsyncClient, box_admin_b: None
) -> None:
    await seed(client)
    assert (
        await client.post("/v1/ae/box-health/cleanup", json=ASK, headers=ADMIN_B)
    ).status_code == 200
    mine = (await client.get("/v1/ae/box-health", headers=ADMIN_A)).json()["cleanup"]
    theirs = (await client.get("/v1/ae/box-health", headers=ADMIN_B)).json()["cleanup"]
    assert mine["by"] == "admin-b@example.com" and theirs["by"] == "a box admin"
    assert (
        "admin-b@example.com" not in json.dumps(theirs)
        and "Queued by a box admin" in (theirs["message"])
    )


async def test_the_three_409s_and_the_400(client: httpx.AsyncClient, box_admin_b: None) -> None:
    url = "/v1/ae/box-health/cleanup"
    await seed(client)
    bad = await client.post(url, json={**ASK, "action": "rm_rf"}, headers=ADMIN_B)
    assert bad.status_code == 400
    changed = await client.post(url, json={**ASK, "count": 5}, headers=ADMIN_B)
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "box_health_changed"
    nobody = await client.post(url, json={**ASK, "owner": "Nobody"}, headers=ADMIN_B)
    assert nobody.json()["error"]["code"] == "box_health_changed"
    assert (await client.post(url, json=ASK, headers=ADMIN_B)).status_code == 200
    busy = await client.post(url, json=ASK, headers=ADMIN_B)
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "box_health_busy"


async def test_an_old_reading_is_refused_as_stale(
    client: httpx.AsyncClient, box_admin_b: None
) -> None:
    old = now() - timedelta(hours=2)
    box_health.store().put(make_post(at=old), now=old)
    stale = await client.post("/v1/ae/box-health/cleanup", json=ASK, headers=ADMIN_B)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "box_health_stale"


async def test_cleanup_is_for_box_admins_and_the_result_for_omnigent_admins(
    client: httpx.AsyncClient, box_admin_b: None
) -> None:
    await seed(client)
    stranger = {"X-Forwarded-Email": "mallory@example.com"}
    assert (
        await client.post("/v1/ae/box-health/cleanup", json=ASK, headers=stranger)
    ).status_code == 403
    assert (
        await client.post("/v1/ae/box-health/cleanup/cancel", json={}, headers=stranger)
    ).status_code == 403
    queued = await client.post("/v1/ae/box-health/cleanup", json=ASK, headers=ADMIN_B)
    result = {"id": queued.json()["id"], "ok": True}
    # A box admin who is not an OmniGent admin may not report a result.
    assert (
        await client.post("/v1/ae/box-health/cleanup/result", json=result, headers=ADMIN_B)
    ).status_code == 403
    wrong = await client.post(
        "/v1/ae/box-health/cleanup/result", json={"id": "b" * 32, "ok": True}, headers=ADMIN_A
    )
    assert wrong.status_code == 409
    foreign = await client.post(
        "/v1/ae/box-health/cleanup",
        json=ASK,
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code in (401, 403)


async def test_cancel_then_try_again(client: httpx.AsyncClient, box_admin_b: None) -> None:
    await seed(client)
    assert (
        await client.post("/v1/ae/box-health/cleanup", json=ASK, headers=ADMIN_B)
    ).status_code == 200
    assert (
        await client.post("/v1/ae/box-health/cleanup/cancel", json={}, headers=ADMIN_B)
    ).status_code == 200
    view = (await client.get("/v1/ae/box-health", headers=ADMIN_B)).json()
    assert view["cleanup"]["state"] == "cancelled" and all(a["can_run"] for a in view["actions"])
    # Taken requests cannot be cancelled.
    assert (
        await client.post("/v1/ae/box-health/cleanup", json=ASK, headers=ADMIN_B)
    ).status_code == 200
    await client.post("/v1/ae/box-health", json=body(), headers=ADMIN_A)
    late = await client.post("/v1/ae/box-health/cleanup/cancel", json={}, headers=ADMIN_B)
    assert late.status_code == 409


async def test_a_box_that_switched_cleanup_off_offers_nothing(
    client: httpx.AsyncClient, box_admin_b: None
) -> None:
    await seed(client, enabled=False)
    view = (await client.get("/v1/ae/box-health", headers=ADMIN_B)).json()
    assert view["actions"] == [] and view["details"]["cleanup"]["enabled"] is False
    refused = await client.post("/v1/ae/box-health/cleanup", json=ASK, headers=ADMIN_B)
    assert refused.status_code == 409


async def test_the_offer_carries_confirm_text_and_no_item_names(
    client: httpx.AsyncClient, box_admin_b: None
) -> None:
    await seed(client)
    view = (await client.get("/v1/ae/box-health", headers=ADMIN_B)).json()
    by = {(a["id"], a["owner"]): a for a in view["actions"]}
    scratch = by[("claude_scratch", "UserA")]
    assert scratch["label"] == "Remove 268 folders"
    assert scratch["confirm"].startswith(
        "Remove 268 of UserA's Claude temp folders older than 2 days"
    )
    assert "cannot be undone" in scratch["confirm"]
    container = by[("claude_scratch", "UserB")]["confirm"]
    assert "UserB's container's temp folders (Claude sessions and test runs)" in container
    assert all("The box runs it within 5 minutes." in a["confirm"] for a in view["actions"])
    litter = by[("tmp_litter", "UserA")]["confirm"]
    assert "sec-appr-* 7,000" in litter and "sec-push-* 2,500" in litter
    assert "Docker" in by[("docker_prune", "all")]["confirm"]
    assert all(len(a["hint"]) <= box_health.HINT_MAX for a in view["actions"])
    assert SECRET not in json.dumps(view)


def test_the_audit_line_names_the_admin_the_action_and_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="ae_omni_policies.box_health_cleanup"):
        box_health_cleanup.audit("admin-b@example.com", "tmp_litter", "UserA", 9500)
    line = caplog.records[-1].getMessage()
    assert "admin-b@example.com" in line and "tmp_litter" in line and "UserA" in line
    assert "9500" in line


# ── follow-ups from the live check ──────────────────────────────────────────


def test_folders_that_just_crossed_the_line_do_not_blame_the_cleanup() -> None:
    def view_for(oldest: float) -> box_health.BoxHealthView:
        post = make_post()
        assert post.slow
        post.slow.users[0].claude[0].oldest_days = oldest
        stored = box_health.Stored(reading=post, received_at=now())
        return box_health.view(stored, now())

    quiet = view_for(2.4)
    assert not [n for n in quiet.needs if n.key.startswith("claude:")]
    loud = view_for(3.4)
    need = next(n for n in loud.needs if n.key.startswith("claude:"))
    assert "may not be running" in need.detail


async def test_the_server_refuses_a_label_that_is_not_on_its_list(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await client.post("/v1/ae/box-health", json=body())).status_code == 200
    monkeypatch.setenv("AE_BOX_USER_LABELS", "UserA, UserB")
    assert (await client.post("/v1/ae/box-health", json=body())).status_code == 200
    odd = body()
    odd["slow"]["users"][0]["label"] = "Mallory"
    refused = await client.post("/v1/ae/box-health", json=odd)
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "box_health_label"
    assert "Mallory" not in refused.text


def test_a_needs_line_gets_a_button_only_for_an_offer_of_its_own_owner_and_kind() -> None:
    post = make_post()
    assert post.slow
    post.slow.users.append(
        UserHealth(
            label="UserB",
            uid=1001,
            claude=[ClaudeRoot(where="tmp", sessions=9, stale_sessions=9, oldest_days=5, files=1)],
        )
    )
    post.slow.users.append(
        UserHealth(
            label="UserB",
            kind="container",
            running=True,
            claude=[
                ClaudeRoot(
                    where="container", sessions=9, stale_sessions=12, oldest_days=5, files=1
                )
            ],
        )
    )
    stored = box_health.Stored(reading=post, received_at=now())
    shown = box_health_cleanup.decorate(
        box_health.view(stored, now()), stored, None, now(), show_login=False
    )
    by_key = {n.key: n.action for n in shown.needs}
    assert by_key["claude:UserB:tmp:box"] is None  # his host-side folders: the daily rule
    assert by_key["claude:UserB:container:container"] == "claude_scratch"
    assert by_key["claude:UserA:tmp:box"] == "claude_scratch"


async def test_a_label_in_a_failed_part_is_checked_too(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_BOX_USER_LABELS", "UserA, UserB")
    ok = body()
    ok["failed"] = ["user UserA", "container UserB scan", "docker"]
    assert (await client.post("/v1/ae/box-health", json=ok)).status_code == 200
    odd = body()
    odd["failed"] = ["container Mallory cleanup"]
    refused = await client.post("/v1/ae/box-health", json=odd)
    assert refused.status_code == 422 and "Mallory" not in refused.text
