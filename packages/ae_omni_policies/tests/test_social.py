"""The Social card: planner, score, gaps, routes and the box relay (2026-11-06)."""

from __future__ import annotations

import base64
import io
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import omni, social, social_relay, social_visual

EXAMPLE_TOOLS = [
    {
        "id": "diagram",
        "name": "Diagram tool",
        "url": "https://tools.example.com/diagram",
        "image": "https://tools.example.com/og-card-1200.jpg",
    },
    {
        "id": "skins",
        "name": "Skins tool",
        "url": "https://tools.example.com/skins",
        "image": "https://tools.example.com/og-card-1200.jpg",
        "note": "Page says coming soon; frame posts as a preview",
    },
    {
        "id": "omnigent",
        "name": "Public release",
        "url": "https://tools.example.com/release",
        "image": "https://opengraph.githubassets.com/1/example/repo",
    },
]
EXAMPLE_CHANNEL_IDS = {"ae": "UCexampleChannelOne0000000", "hc": "UCexampleChannelTwo0000000"}


@pytest.fixture(autouse=True)
def _social_is_set_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Social is a private card with no built-in channels or tools (2026-11-07): a test server
    opts in and supplies the example ones the plans below rotate through."""
    monkeypatch.setenv("AE_PRIVATE_CARDS", "social")
    monkeypatch.setenv(social.TOOLS_ENV, json.dumps(EXAMPLE_TOOLS))
    monkeypatch.setenv(social.CHANNEL_IDS_ENV, json.dumps(EXAMPLE_CHANNEL_IDS))
    monkeypatch.setenv(social_visual.IMAGE_HOSTS_ENV, "tools.example.com")


OWNER = {"X-Forwarded-Email": "owner@example.com"}
OTHER_USER = {"X-Forwarded-Email": "user-b@example.com"}
# Wednesday 2026-11-11, 12:00 in Los Angeles; the ISO week starts Monday 2026-11-09.
NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)


def _item(vid: str, kind: str, when: datetime | None, title: str = "A title") -> dict[str, Any]:
    return {
        "video_id": vid,
        "title": title,
        "kind": kind,
        "privacy": "public",
        "public_at": when.isoformat() if when else None,
        "live_state": "upcoming" if kind == "live" else "none",
        "duration_s": 600,
        "url": f"https://youtu.be/{vid}",
    }


def _lister(
    ae: list[dict[str, Any]] | None = None,
    hc: list[dict[str, Any]] | None = None,
    *,
    hc_ok: bool = True,
    read_at: datetime = NOW,
) -> dict[str, Any]:
    return {
        "read_at": read_at.isoformat(),
        "channels": {
            "ae": {"ok": True, "error": None, "items": ae or []},
            "hc": {"ok": hc_ok, "error": None if hc_ok else "invalid_grant", "items": hc or []},
        },
    }


def _state(data: dict[str, Any] | None = None, **rules: Any) -> social.State:
    state = social.State()
    state.start_at = NOW - timedelta(days=200)  # tests that want a first-run start set their own
    if rules:
        social.apply_rules(state, social.RulesPatch(**rules))
    if data is not None:
        social.apply_uploads(state, social.UploadsPost.model_validate(data))
    return state


def _li(state: social.State) -> list[social.Slot]:
    return [s for s in state.slots.values() if s.platform == "linkedin"]


def _post(state: social.State, sid: str, at: datetime) -> None:
    state.slots[sid].state = "posted"
    state.posted[sid] = social.Posted(at=at)


# ── the planner ─────────────────────────────────────────────────────────────


def test_plan_is_idempotent_and_ids_are_stable() -> None:
    data = _lister(
        ae=[
            _item("aaaaaaaaaaa", "long", NOW + timedelta(days=2)),
            _item("bbbbbbbbbbb", "short", NOW - timedelta(days=1)),
            _item("ccccccccccc", "live", NOW + timedelta(days=5)),
        ],
        hc=[_item("ddddddddddd", "long", NOW + timedelta(days=9))],
    )
    once = social.plan(_state(data), NOW)
    assert social.plan(once, NOW) == once
    assert {
        "li-video-ae-aaaaaaaaaaa",
        "yc-video-ae-aaaaaaaaaaa",
        "li-short-ae-bbbbbbbbbbb",
        "li-live-ae-ccccccccccc",
        "li-video-hc-ddddddddddd",
        "yc-video-hc-ddddddddddd",
    } <= set(once.slots)


def test_each_upload_rule_and_its_switch() -> None:
    when = NOW + timedelta(days=2)
    data = _lister(
        ae=[
            _item("aaaaaaaaaaa", "long", when),
            _item("bbbbbbbbbbb", "short", when),
            _item("ccccccccccc", "live", NOW + timedelta(days=3, hours=-1)),
        ]
    )
    planned = social.plan(_state(data), NOW)
    video = planned.slots["li-video-ae-aaaaaaaaaaa"]
    assert (video.platform, video.due, video.skippable) == ("linkedin", when, False)
    assert planned.slots["yc-video-ae-aaaaaaaaaaa"].platform == "yt_community"
    short = planned.slots["li-short-ae-bbbbbbbbbbb"]
    assert short.skippable and short.due == when
    # The livestream post is due the local day before, 09:00 Los Angeles (17:00 UTC).
    live = planned.slots["li-live-ae-ccccccccccc"]
    assert live.due == datetime(2026, 11, 13, 17, 0, tzinfo=UTC)
    off = social.plan(
        _state(data, video_li=False, video_community=False, short_li=False, live_li=False), NOW
    )
    assert not [s for s in off.slots.values() if s.kind in ("video", "short", "live")]


def test_a_planned_slot_without_a_draft_follows_its_upload_and_goes_when_it_goes() -> None:
    state = social.plan(
        _state(_lister(ae=[_item("aaaaaaaaaaa", "long", NOW + timedelta(days=2))])), NOW
    )
    moved = NOW + timedelta(days=4)
    social.apply_uploads(
        state, social.UploadsPost.model_validate(_lister(ae=[_item("aaaaaaaaaaa", "long", moved)]))
    )
    state = social.plan(state, NOW)
    assert state.slots["li-video-ae-aaaaaaaaaaa"].due == moved
    social.apply_uploads(state, social.UploadsPost.model_validate(_lister(ae=[])))
    state = social.plan(state, NOW)
    assert "li-video-ae-aaaaaaaaaaa" not in state.slots


def test_a_posted_drafted_or_rescheduled_slot_is_never_changed() -> None:
    soon = NOW + timedelta(days=2)
    state = social.plan(
        _state(
            _lister(
                ae=[
                    _item("aaaaaaaaaaa", "long", soon),
                    _item("bbbbbbbbbbb", "long", soon),
                    _item("ccccccccccc", "long", soon),
                ]
            )
        ),
        NOW,
    )
    _post(state, "li-video-ae-aaaaaaaaaaa", NOW)
    state.drafts["li-video-ae-bbbbbbbbbbb"] = social.Draft(
        text="mine", at=NOW, by="reid", edited=True
    )
    social.reschedule(state, "li-video-ae-ccccccccccc", NOW + timedelta(days=6), NOW)
    moved = NOW + timedelta(days=4)
    social.apply_uploads(
        state,
        social.UploadsPost.model_validate(
            _lister(
                ae=[
                    _item("aaaaaaaaaaa", "long", moved),
                    _item("bbbbbbbbbbb", "long", moved),
                    _item("ccccccccccc", "long", moved),
                ]
            )
        ),
    )
    after = social.plan(state, NOW)
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        assert after.slots[f"li-video-ae-{vid}"].due == soon
    assert after.slots["li-video-ae-ccccccccccc"].due == NOW + timedelta(days=6)
    # The upload vanishes: the drafted slot stays as an orphan, the posted one is untouched.
    social.apply_uploads(after, social.UploadsPost.model_validate(_lister(ae=[])))
    gone = social.plan(after, NOW)
    assert gone.slots["li-video-ae-aaaaaaaaaaa"].state == "posted"
    assert gone.slots["li-video-ae-bbbbbbbbbbb"].orphan is True
    assert gone.slots["li-video-ae-bbbbbbbbbbb"].id in gone.drafts


def test_a_failed_channel_keeps_its_items_and_its_slots() -> None:
    state = social.plan(
        _state(_lister(hc=[_item("ddddddddddd", "long", NOW + timedelta(days=3))])), NOW
    )
    social.apply_uploads(state, social.UploadsPost.model_validate(_lister(hc_ok=False)))
    after = social.plan(state, NOW)
    assert len(after.uploads["hc"].items) == 1
    assert after.uploads["hc"].ok is False and after.uploads["hc"].error == "invalid_grant"
    assert "li-video-hc-ddddddddddd" in after.slots


def test_the_tool_post_rotates_through_active_tools_and_skips_inactive() -> None:
    state = social.plan(_state(), NOW)
    tools = [s for s in sorted(state.slots.values(), key=lambda s: s.due) if s.kind == "tool"]
    assert tools[0].id == "li-tool-2026w46"
    assert [s.anchor for s in tools][:4] == ["diagram", "skins", "omnigent", "diagram"]
    assert all(s.due.astimezone(social._zone(state.rules)).weekday() == 4 for s in tools)
    state.tools[1].active = False
    other = social.plan(_state(), NOW)
    other.tools[1].active = False
    other.slots = {k: v for k, v in other.slots.items() if v.kind != "tool"}
    other = social.plan(other, NOW)
    assert "skins" not in {s.anchor for s in other.slots.values() if s.kind == "tool"}


def test_a_posted_tool_goes_to_the_back_of_the_line_and_every_two_weeks_halves_the_slots() -> None:
    state = social.plan(_state(), NOW)
    state.slots = {k: v for k, v in state.slots.items() if k != "li-tool-2026w46"}
    _post(state, "li-tool-2026w47", NOW)
    state.slots["li-tool-2026w47"].anchor = "diagram"
    state = social.plan(state, NOW)
    assert state.slots["li-tool-2026w46"].anchor == "skins"
    biweekly = social.plan(_state(tool_every_days=14), NOW)
    weeks = {s.id for s in biweekly.slots.values() if s.kind == "tool"}
    assert len(weeks) == 3  # a 5-week horizon at every other week


def test_expert_slots_fill_a_short_week_to_the_target_on_the_right_days() -> None:
    state = social.plan(_state(), NOW)
    week = date(2026, 11, 16)  # next week: tool Fri, expert Wed, nothing else
    mine = sorted(
        (s for s in _li(state) if social._week_of(s.due, state.rules) == week), key=lambda s: s.due
    )
    assert len(mine) == 3
    days = [s.due.astimezone(social._zone(state.rules)).weekday() for s in mine]
    assert days == [1, 2, 4]  # Tue (fill), Wed (the weekly expert post), Fri (the tool post)
    # This week: Mon and Tue are past, Wed is today; only today or later can be filled.
    this = [s for s in _li(state) if social._week_of(s.due, state.rules) == date(2026, 11, 9)]
    assert len(this) == 3
    assert all(s.due >= NOW - timedelta(hours=12) for s in this)


def test_a_skipped_post_ends_its_obligation_and_adds_no_replacement() -> None:
    state = social.plan(_state(), NOW)
    mine = [s for s in _li(state) if social._week_of(s.due, state.rules) == date(2026, 11, 16)]
    victim = next(s for s in mine if s.kind == "expert")
    victim.state = "skipped"
    again = social.plan(state, NOW)
    week = [s for s in _li(again) if social._week_of(s.due, again.rules) == date(2026, 11, 16)]
    assert len(week) == 3 and sum(1 for s in week if s.state == "skipped") == 1
    assert social.plan(again, NOW) == again


def test_history_older_than_63_days_goes_with_its_draft_and_mark() -> None:
    state = social.plan(_state(), NOW)
    old = social.Slot(
        id="li-expert-2026w01-1",
        kind="expert",
        platform="linkedin",
        due=NOW - timedelta(days=64),
        state="posted",
    )
    state.slots[old.id] = old
    state.drafts[old.id] = social.Draft(text="x", at=NOW)
    state.posted[old.id] = social.Posted(at=NOW - timedelta(days=64))
    gone = social.plan(state, NOW)
    assert old.id not in gone.slots and old.id not in gone.drafts and old.id not in gone.posted


def test_add_expert_uses_a_free_number_and_refuses_the_past() -> None:
    state = social.plan(_state(), NOW)
    sid = social.add_expert(state, date(2026, 11, 20), NOW)
    assert sid.startswith("li-expert-2026w47-") and sid in state.slots
    assert social.add_expert(state, date(2026, 11, 20), NOW) != sid
    with pytest.raises(social.SocialError):
        social.add_expert(state, date(2026, 11, 1), NOW)


# ── the score ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("now", "posted", "pace"),
    [
        (datetime(2026, 11, 9, 20, tzinfo=UTC), 0, True),  # Monday: floor(3 * 1 / 7) = 0
        (datetime(2026, 11, 10, 20, tzinfo=UTC), 0, True),  # Tuesday: floor(6 / 7) = 0
        (datetime(2026, 11, 11, 20, tzinfo=UTC), 0, False),  # Wednesday: floor(9 / 7) = 1
        (datetime(2026, 11, 11, 20, tzinfo=UTC), 1, True),
        (datetime(2026, 11, 15, 20, tzinfo=UTC), 2, False),  # Sunday: floor(21 / 7) = 3
        (datetime(2026, 11, 15, 20, tzinfo=UTC), 3, True),
    ],
)
def test_on_pace_by_weekday(now: datetime, posted: int, pace: bool) -> None:
    state = _state()
    for n in range(posted):
        sid = f"li-expert-2026w46-{n + 1}"
        state.slots[sid] = social.Slot(id=sid, kind="expert", platform="linkedin", due=now)
        _post(state, sid, datetime(2026, 11, 9, 18, tzinfo=UTC))
    got = social.score(state, now)
    assert got["week_posted"] == posted and got["on_pace"] is pace


def _weeks_state(counts: dict[int, int]) -> social.State:
    """LinkedIn posts marked posted per week, keyed by weeks before NOW's week."""
    state = _state()
    monday = datetime(2026, 11, 9, 18, tzinfo=UTC)
    for back, count in counts.items():
        for n in range(count):
            at = monday - timedelta(days=7 * back) + timedelta(hours=n)
            sid = f"li-expert-{social._week_key(at.date())}-{n + 1}"
            state.slots[sid] = social.Slot(id=sid, kind="expert", platform="linkedin", due=at)
            _post(state, sid, at)
    return state


def test_the_streak_counts_full_weeks_and_breaks_on_a_missed_one() -> None:
    # Two full weeks, then a week of 2, then two full weeks further back.
    state = _weeks_state({1: 3, 2: 4, 3: 2, 4: 3, 5: 3})
    assert social.score(state, NOW)["streak"] == 2
    # The current week joins once it hits the target.
    both = _weeks_state({0: 3, 1: 3, 2: 3, 3: 1})
    assert social.score(both, NOW)["streak"] == 3
    assert social.score(_weeks_state({0: 2, 1: 3}), NOW)["streak"] == 1


def test_plan_keeps_the_best_streak() -> None:
    state = social.plan(_weeks_state({1: 3, 2: 3, 3: 3}), NOW)
    assert state.best_streak == 3
    later = social.plan(_state(), NOW)
    later.best_streak = 5
    assert social.score(social.plan(later, NOW), NOW)["best_streak"] == 5


def test_promoted_counts_posted_and_skipped_and_waits_out_the_short_window() -> None:
    state = social.plan(
        _state(
            _lister(
                ae=[
                    _item("aaaaaaaaaaa", "long", NOW - timedelta(days=5)),  # posted both: promoted
                    _item(
                        "bbbbbbbbbbb", "long", NOW - timedelta(days=6)
                    ),  # nothing posted: missed
                    _item(
                        "ccccccccccc", "short", NOW - timedelta(days=2)
                    ),  # inside the 3-day window
                    _item(
                        "ddddddddddd", "short", NOW - timedelta(days=4)
                    ),  # skipped: counts as done
                    _item(
                        "eeeeeeeeeee", "short", NOW - timedelta(days=5)
                    ),  # window passed, unposted
                    _item("fffffffffff", "long", NOW - timedelta(days=40)),
                ]  # older than 30 days
            )
        ),
        NOW,
    )
    _post(state, "li-video-ae-aaaaaaaaaaa", NOW)
    _post(state, "yc-video-ae-aaaaaaaaaaa", NOW)
    state.slots["li-short-ae-ddddddddddd"].state = "skipped"
    got = social.score(state, NOW)
    assert (got["promoted"], got["promoted_of"], got["missed"]) == (2, 4, 2)


def test_on_time_and_community_numbers() -> None:
    state = social.plan(
        _state(
            _lister(
                ae=[
                    _item("aaaaaaaaaaa", "long", NOW - timedelta(days=3)),
                    _item("bbbbbbbbbbb", "long", NOW - timedelta(days=4)),
                ]
            )
        ),
        NOW,
    )
    _post(
        state, "li-video-ae-aaaaaaaaaaa", NOW - timedelta(days=3) + timedelta(hours=23)
    )  # on time
    _post(state, "li-video-ae-bbbbbbbbbbb", NOW - timedelta(days=4) + timedelta(hours=25))  # late
    _post(state, "yc-video-ae-aaaaaaaaaaa", NOW - timedelta(days=3))
    got = social.score(state, NOW)
    assert (got["on_time_pct"], got["on_time_of"]) == (67, 3)
    assert got["community_30d"] == {"posted": 1, "total": 2}
    assert len(got["weeks"]) == 8 and got["weeks"][-1]["week"] == "2026w46"
    assert social.score(_state(), NOW)["on_time_pct"] is None


# ── the gaps ────────────────────────────────────────────────────────────────


def test_gaps_per_channel() -> None:
    state = _state(
        _lister(
            ae=[_item("aaaaaaaaaaa", "long", NOW + timedelta(days=3))],
            hc=[_item("ddddddddddd", "long", NOW + timedelta(days=9))],
            read_at=NOW - timedelta(hours=7),
        )
    )
    got = social.gaps(state, NOW)
    assert got["ae"] == {"read_failed": False, "error": None, "stale": True, "upload_gap": False}
    assert got["hc"]["upload_gap"] is True and got["hc"]["stale"] is True
    failed = _state(_lister(hc_ok=False))
    assert social.gaps(failed, NOW)["hc"] == {
        "read_failed": True,
        "error": "invalid_grant",
        "stale": False,
        "upload_gap": False,
    }
    assert not any(social.gaps(social.State(), NOW)["ae"].values())


# ── the store ───────────────────────────────────────────────────────────────


def test_the_store_round_trips_through_its_file_and_refuses_a_bad_one(tmp_path: Path) -> None:
    path = tmp_path / "ae-social.json"
    store = social.SocialStore(path)
    store.mutate(
        lambda s: social.apply_uploads(
            s,
            social.UploadsPost.model_validate(
                _lister(ae=[_item("aaaaaaaaaaa", "long", NOW + timedelta(days=2))])
            ),
        ),
        now=NOW,
    )
    again = social.SocialStore(path).read()
    assert "li-video-ae-aaaaaaaaaaa" in again.slots and again.version == 1
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(social.SocialSaveError):
        store.mutate(lambda s: None, now=NOW)
    assert path.read_text() == "not json"


# ── the routes ──────────────────────────────────────────────────────────────


@pytest.fixture()
def admin(ae_admins: set[str]) -> dict[str, str]:
    return OWNER


async def _seed(client: httpx.AsyncClient, data: dict[str, Any] | None = None) -> None:
    when = datetime.now(UTC) + timedelta(days=2)
    body = data or _lister(ae=[_item("aaaaaaaaaaa", "long", when, "Real video")])
    got = await client.post("/v1/ae/social/uploads", json=body, headers=OWNER)
    assert got.status_code == 200, got.text


ROUTES: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/v1/ae/social", None),
    ("GET", "/v1/ae/social/week", None),
    ("GET", "/v1/ae/social/week-redraft", None),
    ("GET", "/v1/ae/social/visual/li-tool-2026w46/0", None),
    ("POST", "/v1/ae/social/slot", {"id": "li-tool-2026w46"}),
    ("POST", "/v1/ae/social/uploads", _lister()),
    ("POST", "/v1/ae/social/posted", {"id": "li-tool-2026w46"}),
    ("POST", "/v1/ae/social/reopen", {"id": "li-tool-2026w46"}),
    ("POST", "/v1/ae/social/skip", {"id": "li-tool-2026w46"}),
    ("POST", "/v1/ae/social/reschedule", {"id": "li-tool-2026w46", "due": "2030-01-01T00:00:00Z"}),
    ("POST", "/v1/ae/social/draft", {"id": "li-tool-2026w46", "text": "x"}),
    (
        "POST",
        "/v1/ae/social/expert",
        {"date": (datetime.now(UTC).date() + timedelta(days=3)).isoformat()},
    ),
    ("POST", "/v1/ae/social/rules", {"li_weekly_target": 4}),
    ("POST", "/v1/ae/social/tools", {"tools": []}),
    ("POST", "/v1/ae/social/drafts", {"drafts": [], "errors": []}),
]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
async def test_every_route_is_admin_only(
    client: httpx.AsyncClient, admin: dict[str, str], method: str, path: str, body: Any
) -> None:
    refused = await client.request(method, path, json=body, headers=OTHER_USER)
    assert refused.status_code == 403, refused.text
    allowed = await client.request(method, path, json=body, headers=OWNER)
    assert allowed.status_code in (200, 404), allowed.text


async def test_posts_check_the_origin_and_the_content_type(client: httpx.AsyncClient) -> None:
    body = {"id": "li-tool-2026w46"}
    foreign = await client.post(
        "/v1/ae/social/skip", json=body, headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403, foreign.text
    plain = await client.post(
        "/v1/ae/social/skip", content=json.dumps(body), headers={"Content-Type": "text/plain"}
    )
    assert plain.status_code in (400, 415, 422), plain.text


async def test_mark_posted_changes_the_score_and_reopen_undoes_it(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    await _seed(client)
    page = (await client.get("/v1/ae/social", headers=OWNER)).json()
    assert page["score"]["week_posted"] == 0
    open_slot = next(s for s in page["slots"] if s["platform"] == "linkedin")
    got = await client.post("/v1/ae/social/posted", json={"id": open_slot["id"]}, headers=OWNER)
    assert got.status_code == 200, got.text
    after = (await client.get("/v1/ae/social", headers=OWNER)).json()
    assert after["score"]["on_time_of"] == 1
    row = next(s for s in after["slots"] if s["id"] == open_slot["id"])
    assert row["state"] == "posted" and row["posted"]["via"] == "manual"
    await client.post("/v1/ae/social/reopen", json={"id": open_slot["id"]}, headers=OWNER)
    undone = (await client.get("/v1/ae/social", headers=OWNER)).json()
    assert undone["score"]["on_time_of"] == 0
    bad = await client.post(
        "/v1/ae/social/posted", json={"id": open_slot["id"], "url": "http://x"}, headers=OWNER
    )
    assert bad.status_code == 422
    missing = await client.post("/v1/ae/social/posted", json={"id": "li-nope-nope"}, headers=OWNER)
    assert missing.status_code == 404


async def test_draft_edit_skip_reschedule_expert_rules_and_tools(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    await _seed(client)
    sid = "li-video-ae-aaaaaaaaaaa"
    edit = await client.post(
        "/v1/ae/social/draft", json={"id": sid, "text": "My words", "topic": "t"}, headers=OWNER
    )
    assert edit.status_code == 200, edit.text
    one = (await client.post("/v1/ae/social/slot", json={"id": sid}, headers=OWNER)).json()
    assert (
        one["draft"]["by"] == "reid"
        and one["draft"]["edited"] is True
        and one["draft"]["text"] == "My words"
    )
    due = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    moved = await client.post(
        "/v1/ae/social/reschedule", json={"id": sid, "due": due}, headers=OWNER
    )
    assert moved.status_code == 200
    assert (
        await client.post("/v1/ae/social/skip", json={"id": sid}, headers=OWNER)
    ).status_code == 200
    cannot = await client.post(
        "/v1/ae/social/reschedule", json={"id": sid, "due": due}, headers=OWNER
    )
    assert cannot.status_code == 409
    today = datetime.now(UTC).date()
    made = await client.post(
        "/v1/ae/social/expert",
        json={"date": (today + timedelta(days=3)).isoformat()},
        headers=OWNER,
    )
    assert made.status_code == 200 and made.json()["id"].startswith("li-expert-")
    past = await client.post("/v1/ae/social/expert", json={"date": "2020-01-01"}, headers=OWNER)
    assert past.status_code == 422
    rules = await client.post(
        "/v1/ae/social/rules", json={"li_weekly_target": 5, "alarm_hhmm": "07:30"}, headers=OWNER
    )
    assert rules.json()["rules"]["li_weekly_target"] == 5
    for body in ({"alarm_hhmm": "25:00"}, {"tz": "Mars/Base"}, {"nope": 1}, {"short_li_days": 99}):
        assert (
            await client.post("/v1/ae/social/rules", json=body, headers=OWNER)
        ).status_code == 422
    tools = [{"id": "t1", "name": "One", "url": "https://example.com/1", "active": True}]
    assert (
        await client.post("/v1/ae/social/tools", json={"tools": tools}, headers=OWNER)
    ).status_code == 200
    dupe = {"tools": tools + tools}
    assert (await client.post("/v1/ae/social/tools", json=dupe, headers=OWNER)).status_code == 422
    insecure = {"tools": [{**tools[0], "url": "http://example.com"}]}
    assert (
        await client.post("/v1/ae/social/tools", json=insecure, headers=OWNER)
    ).status_code == 422


async def test_the_drafter_writes_drafts_but_never_over_reids(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    await _seed(client)
    week = (await client.get("/v1/ae/social/week", headers=OWNER)).json()
    ids = {s["id"] for s in week["slots"]}
    assert "li-video-ae-aaaaaaaaaaa" in ids
    ctx = next(s for s in week["slots"] if s["id"] == "li-video-ae-aaaaaaaaaaa")["context"]
    assert ctx["title"] == "Real video" and ctx["channel"] == "ae"
    mine = "li-video-ae-aaaaaaaaaaa"
    got = await client.post(
        "/v1/ae/social/drafts",
        headers=OWNER,
        json={
            "drafts": [{"id": mine, "text": "auto text", "topic": "x", "audit_ok": True}],
            "errors": [{"id": "yc-video-ae-aaaaaaaaaaa", "error": "model said no"}],
        },
    )
    assert got.json() == {"written": [mine], "kept": [], "errors_set": ["yc-video-ae-aaaaaaaaaaa"]}
    again = await client.post(
        "/v1/ae/social/drafts",
        headers=OWNER,
        json={"drafts": [{"id": mine, "text": "auto text 2", "audit_ok": True}], "errors": []},
    )
    assert again.json()["written"] == [mine]  # an auto draft may be replaced
    await client.post(
        "/v1/ae/social/draft", json={"id": mine, "text": "Reid wrote this"}, headers=OWNER
    )
    third = await client.post(
        "/v1/ae/social/drafts",
        headers=OWNER,
        json={"drafts": [{"id": mine, "text": "auto text 3", "audit_ok": True}], "errors": []},
    )
    assert third.json() == {"written": [], "kept": [mine], "errors_set": []}
    kept = (await client.post("/v1/ae/social/slot", json={"id": mine}, headers=OWNER)).json()
    assert kept["draft"]["text"] == "Reid wrote this"
    assert mine not in {
        s["id"] for s in (await client.get("/v1/ae/social/week", headers=OWNER)).json()["slots"]
    }


async def test_upload_validation_rejects_bad_ids_and_oversized_lists_and_drops_unknown_fields(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    when = NOW + timedelta(days=2)
    bad_id = _lister(ae=[_item("short", "long", when)])
    assert (
        await client.post("/v1/ae/social/uploads", json=bad_id, headers=OWNER)
    ).status_code == 422
    big = _lister(ae=[_item(f"{n:011d}", "long", when) for n in range(301)])
    assert (await client.post("/v1/ae/social/uploads", json=big, headers=OWNER)).status_code == 422
    long_title = _lister(ae=[_item("aaaaaaaaaaa", "long", when, "x" * 201)])
    assert (
        await client.post("/v1/ae/social/uploads", json=long_title, headers=OWNER)
    ).status_code == 422
    odd = _item("aaaaaaaaaaa", "long", when)
    odd.update({"evil": "x", "url": "https://phish.example/x"})
    body = _lister(ae=[odd])
    body["extra"] = 1
    assert (
        await client.post("/v1/ae/social/uploads", json=body, headers=OWNER)
    ).status_code == 200
    # The link is rebuilt from the id, never taken from the post.
    stored = social.store().read().uploads["ae"].items[0]
    assert stored.url == "https://youtu.be/aaaaaaaaaaa"
    notime = _lister(ae=[{**_item("bbbbbbbbbbb", "long", when), "public_at": "tomorrow"}])
    assert (
        await client.post("/v1/ae/social/uploads", json=notime, headers=OWNER)
    ).status_code == 422


async def test_a_slot_row_carries_the_link_the_starter_text_needs(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    # The phone editor builds its starter text from the row (2026-11-06): the upload's link,
    # rebuilt from its id, or the tool's own link; an expert post has none.
    now = datetime.now(UTC)
    await _seed(
        client,
        _lister(
            ae=[
                _item("aaaaaaaaaaa", "long", now + timedelta(days=2), "Real video"),
                _item("bbbbbbbbbbb", "short", now + timedelta(days=1), "A short"),
            ]
        ),
    )
    page = (await client.get("/v1/ae/social", headers=OWNER)).json()
    by_id = {row["id"]: row for row in page["slots"]}
    assert by_id["li-video-ae-aaaaaaaaaaa"]["url"] == "https://youtu.be/aaaaaaaaaaa"
    assert by_id["li-short-ae-bbbbbbbbbbb"]["url"] == "https://www.youtube.com/shorts/bbbbbbbbbbb"
    tools = {t["id"]: t["url"] for t in page["tools"]}
    tool_rows = [row for row in page["slots"] if row["kind"] == "tool"]
    assert tool_rows and all(row["url"] == tools[row["anchor"]] for row in tool_rows)
    assert all(row["url"] is None for row in page["slots"] if row["kind"] == "expert")
    detail = (
        await client.post(
            "/v1/ae/social/slot", json={"id": "li-video-ae-aaaaaaaaaaa"}, headers=OWNER
        )
    ).json()
    assert detail["url"] == "https://youtu.be/aaaaaaaaaaa"


async def test_a_draft_row_carries_a_revision_that_follows_the_whole_text(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    # The compact card caches the full draft by this; the preview stops at 160 characters, so an
    # edit past it must still change the revision (2026-11-06, review round 1).
    await _seed(client)
    sid = "li-video-ae-aaaaaaaaaaa"

    async def row() -> dict[str, Any]:
        page = (await client.get("/v1/ae/social", headers=OWNER)).json()
        return next(r for r in page["slots"] if r["id"] == sid)

    before = await row()
    assert "draft_rev" not in before or before["has_draft"]
    text = "x" * 600
    await client.post("/v1/ae/social/draft", json={"id": sid, "text": text}, headers=OWNER)
    first = await row()
    await client.post(
        "/v1/ae/social/draft", json={"id": sid, "text": text[:-1] + "y"}, headers=OWNER
    )
    second = await row()
    assert first["draft_preview"] == second["draft_preview"]
    assert first["draft_rev"] != second["draft_rev"] and len(first["draft_rev"]) == 8
    other = next(
        r
        for r in (await client.get("/v1/ae/social", headers=OWNER)).json()["slots"]
        if not r["has_draft"]
    )
    assert "draft_rev" not in other


async def test_the_page_names_the_channels_from_the_env_and_falls_back(
    client: httpx.AsyncClient, admin: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The starter text says the channel's name; the second name is private, so it comes from the
    # env and the default is the short code (2026-11-06).
    await _seed(client)
    page = (await client.get("/v1/ae/social", headers=OWNER)).json()
    assert page["channel_names"] == {"ae": "Channel 1", "hc": "Channel 2"}
    monkeypatch.setenv(
        social.CHANNEL_NAMES_ENV, json.dumps({"hc": "Second Co", "ae": "", "x": "y"})
    )
    page = (await client.get("/v1/ae/social", headers=OWNER)).json()
    assert page["channel_names"] == {"ae": "Channel 1", "hc": "Second Co"}
    monkeypatch.setenv(social.CHANNEL_NAMES_ENV, "not json")
    page = (await client.get("/v1/ae/social", headers=OWNER)).json()
    assert page["channel_names"] == {"ae": "Channel 1", "hc": "Channel 2"}


async def test_a_worst_case_page_stays_under_the_bridge_cap(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    now = datetime.now(UTC)
    items = [
        _item(f"{n:011d}", "long", now + timedelta(days=(n % 27) - 6), "T" * 200)
        for n in range(300)
    ]
    await _seed(
        client, _lister(ae=items, hc=[{**i, "video_id": f"h{i['video_id'][1:]}"} for i in items])
    )

    def fill(state: social.State) -> None:
        for sid in state.slots:
            state.drafts[sid] = social.Draft(text="d" * 3000, topic="t" * 120, at=now)
        state.tools = [
            social.Tool(
                id=f"t{n}", name="N" * 80, url="https://example.com/" + "u" * 200, note="n" * 200
            )
            for n in range(12)
        ]

    social.store().mutate(fill)
    got = await client.get("/v1/ae/social", headers=OWNER)
    assert got.status_code == 200
    assert len(got.text) < social.RESPONSE_CAP, len(got.text)
    page = got.json()
    # Every row carries the new "url" field, and the page still fits (2026-11-06).
    assert all("url" in row for row in page["slots"])
    # And "draft_rev" on every row that has a draft (all of them here, at full size).
    assert all(len(row["draft_rev"]) == 8 for row in page["slots"])
    # The Scorecard's lists ride along and are bounded (2026-11-06, part 2).
    assert len(page["score"]["unposted"]) <= social.SCORECARD_ROWS
    assert len(page["score"]["rotation"]) == 12
    assert set(page["score"]["pace"]) == {"ae", "hc"}
    assert page["truncated"] is True and page["slot_count"] > len(page["slots"])
    # Open posts are kept before closed ones when the page has to cut.
    assert all(s["state"] == "planned" for s in page["slots"])


# ── the relay ───────────────────────────────────────────────────────────────


def test_the_relay_keeps_only_the_contracts_fields() -> None:
    odd = _item("aaaaaaaaaaa", "long", NOW)
    odd["secret"] = "x"
    body = social_relay.build_body(_lister(ae=[odd]))
    assert "secret" not in json.dumps(body.model_dump(mode="json"))
    with pytest.raises(social_relay.RelayError):
        social_relay.build_body({"read_at": "now", "channels": {}})


async def test_dry_run_prints_the_plan_and_the_counts_and_posts_nothing(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    fixture = tmp_path / "lister.json"
    fixture.write_text(
        json.dumps(
            _lister(
                ae=[
                    _item("aaaaaaaaaaa", "long", now + timedelta(days=2)),
                    _item("bbbbbbbbbbb", "short", now + timedelta(days=1)),
                ],
                hc=[_item("ddddddddddd", "live", now + timedelta(days=4))],
                read_at=now,
            )
        ),
        encoding="utf-8",
    )
    out = io.StringIO()
    code = await social_relay.run(
        script=tmp_path / "unused.js",
        from_file=fixture,
        dry_run=True,
        settings=omni.Settings(server_url="https://omni.test", token="t"),
        out=out,
    )
    assert code == 0, out.getvalue()
    text = out.getvalue()
    assert '"slots_by_kind"' in text and '"video": 2' in text and '"short": 1' in text


async def test_the_relay_posts_the_reading_and_exits_nonzero_when_every_channel_failed(
    tmp_path: Path,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"slots": 4, "uploads": {}})

    fixture = tmp_path / "lister.json"
    fixture.write_text(
        json.dumps(_lister(ae=[_item("aaaaaaaaaaa", "long", NOW)])), encoding="utf-8"
    )
    out = io.StringIO()
    code = await social_relay.run(
        script=tmp_path / "x.js",
        from_file=fixture,
        out=out,
        settings=omni.Settings(server_url="https://omni.test", token="secret-token-1"),
        transport=httpx.MockTransport(handler),
    )
    assert code == 0 and calls[0].url.path == "/v1/ae/social/uploads"
    assert "secret-token-1" not in out.getvalue()
    dead = {
        "read_at": NOW.isoformat(),
        "channels": {
            "ae": {"ok": False, "error": "e", "items": []},
            "hc": {"ok": False, "error": "e", "items": []},
        },
    }
    fixture.write_text(json.dumps(dead), encoding="utf-8")
    code = await social_relay.run(
        script=tmp_path / "x.js",
        from_file=fixture,
        out=io.StringIO(),
        settings=omni.Settings(server_url="https://omni.test", token="t"),
        transport=httpx.MockTransport(handler),
    )
    assert code == 1


def test_the_lister_runs_in_the_skill_dir_and_accepts_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "skill" / "scripts" / "list-uploads.js"
    script.parent.mkdir(parents=True)
    script.write_text("//", encoding="utf-8")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/node")
    seen: dict[str, Any] = {}

    class Done:
        returncode = 1
        stdout = json.dumps(_lister(hc_ok=False))
        stderr = ""

    def fake(argv: list[str], **kwargs: Any) -> Done:
        seen.update(kwargs, argv=argv)
        return Done()

    got = social_relay.run_lister(script, runner=fake)
    assert got["channels"]["hc"]["ok"] is False
    assert seen["cwd"] == str(tmp_path / "skill") and seen["timeout"] == 120
    Done.returncode = 2
    with pytest.raises(social_relay.RelayError):
        social_relay.run_lister(script, runner=fake)


# ── review round 1 (2026-11-06) ─────────────────────────────────────────────


def _week16(state: social.State, kind: str | None = None) -> list[social.Slot]:
    return sorted(
        (
            s
            for s in state.slots.values()
            if social._week_of(s.due, state.rules) == date(2026, 11, 16)
            and (kind is None or s.kind == kind)
        ),
        key=lambda s: s.due,
    )


def _weekday(state: social.State, slot: social.Slot) -> int:
    return slot.due.astimezone(social._zone(state.rules)).weekday()


def test_week_context_stays_under_the_cap_overdue_first_and_says_truncated() -> None:
    items = [
        _item(f"{n:011d}", "long", NOW - timedelta(days=n % 40), "T" * 200) for n in range(300)
    ]
    state = social.plan(
        _state(_lister(ae=items, hc=[{**i, "video_id": f"h{i['video_id'][1:]}"} for i in items])),
        NOW,
    )
    page = social.week_context(state, NOW)
    assert len(json.dumps(page, ensure_ascii=False)) < social.RESPONSE_CAP
    assert page["truncated"] is True and page["slot_count"] > len(page["slots"]) > 20
    dues = [row["due"] for row in page["slots"]]
    assert dues == sorted(dues)
    assert set(page["slots"][0]) == {"id", "kind", "platform", "due", "draft_error", "context"}
    small = social.week_context(social.plan(_state(), NOW), NOW)
    assert small["truncated"] is False


def test_lowering_the_target_or_expert_count_drops_unfrozen_future_slots_only() -> None:
    state = social.plan(_state(), NOW)
    keep_drafted = _week16(state, "expert")[0]
    state.drafts[keep_drafted.id] = social.Draft(text="mine", at=NOW, by="reid", edited=True)
    social.apply_rules(state, social.RulesPatch(li_weekly_target=1, expert_per_week=0))
    after = social.plan(state, NOW)
    left = [s.id for s in _week16(after, "expert")]
    assert left == [keep_drafted.id]
    assert sorted(s.kind for s in _week16(after)) == ["expert", "tool"]
    assert social.plan(after, NOW) == after


def test_changing_the_expert_weekday_or_alarm_moves_unfrozen_slots_and_not_frozen_ones() -> None:
    state = social.plan(_state(), NOW)
    base = next(s for s in _week16(state, "expert") if social._expert_n(s.id) == 1)
    state.slots[base.id].rescheduled = True
    other = next(s for s in _week16(state, "expert") if s.id != base.id)
    frozen_due = base.due
    social.apply_rules(state, social.RulesPatch(expert_weekday=3, alarm_hhmm="07:30"))
    after = social.plan(state, NOW)
    assert after.slots[base.id].due == frozen_due
    moved = [s for s in _week16(after, "expert") if social._expert_n(s.id) == 1]
    assert moved[0].due == frozen_due
    tool = _week16(after, "tool")[0]
    assert tool.due.astimezone(social._zone(after.rules)).strftime("%H:%M") == "07:30"
    other_now = after.slots.get(other.id)
    if other_now is not None:
        assert other_now.due.astimezone(social._zone(after.rules)).strftime("%H:%M") == "07:30"
    # An unfrozen baseline slot follows the weekday: week 3 (Nov 23) moves from Wed to Thu.
    thursday = [
        s
        for s in after.slots.values()
        if s.kind == "expert"
        and social._week_of(s.due, after.rules) == date(2026, 11, 23)
        and social._expert_n(s.id) == 1
    ]
    assert _weekday(after, thursday[0]) == 3


def test_a_slower_tool_cadence_deletes_unfrozen_tool_slots_off_the_cadence() -> None:
    state = social.plan(_state(), NOW)
    weeks = {s.id for s in state.slots.values() if s.kind == "tool"}
    first = sorted(weeks)[1]
    state.slots[first].rescheduled = True
    social.apply_rules(state, social.RulesPatch(tool_every_days=14))
    after = social.plan(state, NOW)
    kept = {s.id for s in after.slots.values() if s.kind == "tool"}
    assert first in kept and len(kept) < len(weeks)
    assert social.plan(after, NOW) == after


def test_an_inactive_tool_is_replaced_in_unfrozen_tool_slots_and_none_active_deletes_them() -> (
    None
):
    state = social.plan(_state(), NOW)
    target = _week16(state, "tool")[0]
    state.tools[next(i for i, t in enumerate(state.tools) if t.id == target.anchor)].active = False
    gone = target.anchor
    after = social.plan(state, NOW)
    assert after.slots[target.id].anchor not in (None, gone)
    for tool in after.tools:
        tool.active = False
    none = social.plan(after, NOW)
    assert not [s for s in none.slots.values() if s.kind == "tool"]
    # A drafted tool slot is frozen: it keeps its tool even when that tool is switched off.
    state2 = social.plan(_state(), NOW)
    keep = _week16(state2, "tool")[0]
    state2.drafts[keep.id] = social.Draft(text="x", at=NOW)
    anchor = keep.anchor
    for tool in state2.tools:
        tool.active = False
    assert social.plan(state2, NOW).slots[keep.id].anchor == anchor


def test_a_manual_expert_slot_survives_a_rules_change() -> None:
    state = social.plan(_state(), NOW)
    sid = social.add_expert(state, date(2026, 11, 20), NOW)
    social.apply_rules(state, social.RulesPatch(expert_per_week=0, li_weekly_target=0))
    assert sid in social.plan(state, NOW).slots


def _counted(state: social.State, weeks_back: int, count: int) -> None:
    monday = social._week_of(NOW, state.rules) - timedelta(days=7 * weeks_back)
    state.week_posted[monday.isoformat()] = count


def test_a_twelve_week_streak_survives_the_pruned_posted_marks() -> None:
    state = _state()
    for back in range(1, 13):
        _counted(state, back, 3)
    _counted(state, 13, 1)
    got = social.score(state, NOW)
    assert got["streak"] == 12
    assert [w["posted"] for w in got["weeks"]][:7] == [3] * 7
    assert social.plan(state, NOW).best_streak == 12
    assert social.plan(state, NOW).week_posted  # kept through a plan


def test_marking_and_reopening_keeps_the_weekly_count() -> None:
    state = social.plan(_state(), NOW)
    sid = _week16(state, "expert")[0].id
    social.mark_posted(state, sid, None, NOW)
    week = social._week_of(NOW, state.rules).isoformat()
    assert state.week_posted[week] == 1
    social.mark_posted(state, sid, None, NOW)  # a second click counts once
    assert state.week_posted[week] == 1
    social.reopen(state, sid)
    assert state.week_posted[week] == 0
    yc = (
        next(s for s in state.slots.values() if s.platform == "yt_community")
        if any(s.platform == "yt_community" for s in state.slots.values())
        else None
    )
    assert yc is None  # no uploads here; community posts never count toward LinkedIn


async def test_reschedule_and_upload_validation_and_stale_clock(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    await _seed(client)
    sid = "li-video-ae-aaaaaaaaaaa"
    now = datetime.now(UTC)
    for due in (now - timedelta(days=3), now + timedelta(days=200)):
        got = await client.post(
            "/v1/ae/social/reschedule", json={"id": sid, "due": due.isoformat()}, headers=OWNER
        )
        assert got.status_code == 422, got.text
    fine = now + timedelta(days=3)
    got = await client.post(
        "/v1/ae/social/reschedule", json={"id": sid, "due": fine.isoformat()}, headers=OWNER
    )
    assert got.status_code == 200
    typo = {"read_at": now.isoformat(), "channels": {"typo": {"ok": True, "items": []}}}
    assert (
        await client.post("/v1/ae/social/uploads", json=typo, headers=OWNER)
    ).status_code == 422
    future = _lister(
        ae=[_item("aaaaaaaaaaa", "long", now + timedelta(days=2))], read_at=now + timedelta(days=3)
    )
    assert (
        await client.post("/v1/ae/social/uploads", json=future, headers=OWNER)
    ).status_code == 200
    page = (await client.get("/v1/ae/social", headers=OWNER)).json()
    assert datetime.fromisoformat(page["uploads_meta"]["ae"]["read_at"]) <= datetime.now(UTC)


def test_the_first_post_with_sixty_days_of_history_plans_nothing_in_the_past() -> None:
    history = [_item(f"{n:011d}", "long", NOW - timedelta(days=n)) for n in range(1, 61)]
    shorts = [_item(f"s{n:010d}", "short", NOW - timedelta(days=n)) for n in range(1, 40)]
    live_soon = _item(
        "lllllllllll", "live", NOW + timedelta(hours=10)
    )  # announcement was due before the start
    ahead = _item("fffffffffff", "long", NOW + timedelta(days=3))
    state = social.State()
    social.apply_uploads(
        state,
        social.UploadsPost.model_validate(_lister(ae=[*history, *shorts, live_soon, ahead])),
        NOW,
    )
    planned = social.plan(state, NOW)
    assert planned.start_at == NOW
    assert all(s.due >= NOW - timedelta(hours=12) for s in planned.slots.values())
    assert "li-live-ae-lllllllllll" not in planned.slots
    assert "li-video-ae-fffffffffff" in planned.slots
    got = social.score(planned, NOW)
    assert (got["promoted"], got["promoted_of"], got["missed"]) == (0, 0, 0)
    only_history = social.State()
    social.apply_uploads(only_history, social.UploadsPost.model_validate(_lister(ae=history)), NOW)
    quiet = social.plan(only_history, NOW)
    assert not [s for s in quiet.slots.values() if s.kind in ("video", "short", "live")]
    assert social.score(quiet, NOW)["promoted_of"] == 0
    assert social.plan(quiet, NOW) == quiet


def test_weeks_wholly_before_the_start_neither_count_nor_break_the_streak() -> None:
    state = _state()
    state.start_at = NOW - timedelta(days=22)  # inside the week of Oct 19, three weeks back
    _counted(state, 1, 3)
    _counted(state, 2, 3)
    # Three weeks back is the first (partial) week and four back is wholly before the start:
    # with no posts there neither may break the streak, and neither counts.
    assert social.score(state, NOW)["streak"] == 2
    state.start_at = None
    assert social.score(state, NOW)["streak"] == 2
    _counted(state, 3, 3)
    assert social.score(state, NOW)["streak"] == 3


def test_nothing_is_planned_due_before_the_start_instant() -> None:
    # Friday 2026-11-06 19:00Z is noon in Los Angeles: that day's 09:00 tool slot is already past.
    start = datetime(2026, 11, 6, 19, 0, tzinfo=UTC)
    state = social.State()
    social.apply_uploads(state, social.UploadsPost.model_validate(_lister(read_at=start)), start)
    planned = social.plan(state, start)
    assert planned.start_at == start
    assert all(s.due >= start for s in planned.slots.values())
    tools = sorted(s.id for s in planned.slots.values() if s.kind == "tool")
    assert "li-tool-2026w45" not in tools and "li-tool-2026w46" in tools
    assert social.plan(planned, start) == planned


def test_start_at_is_set_only_by_a_post_with_a_channel_that_read_ok() -> None:
    dead = {
        "read_at": NOW.isoformat(),
        "channels": {
            "ae": {"ok": False, "error": "e", "items": []},
            "hc": {"ok": False, "error": "e", "items": []},
        },
    }
    state = social.State()
    social.apply_uploads(state, social.UploadsPost.model_validate(dead), NOW)
    assert state.start_at is None
    social.apply_uploads(state, social.UploadsPost.model_validate(_lister(hc_ok=False)), NOW)
    assert state.start_at == NOW
    later = NOW + timedelta(days=1)
    social.apply_uploads(state, social.UploadsPost.model_validate(_lister()), later)
    assert state.start_at == NOW


def test_an_all_failed_first_post_plans_nothing_and_the_first_ok_post_starts_clean() -> None:
    dead = {
        "read_at": NOW.isoformat(),
        "channels": {
            "ae": {"ok": False, "error": "e", "items": []},
            "hc": {"ok": False, "error": "e", "items": []},
        },
    }
    state = social.State()
    social.apply_uploads(state, social.UploadsPost.model_validate(dead), NOW)
    assert social.plan(state, NOW).slots == {}
    later = NOW + timedelta(days=2, hours=5)
    social.apply_uploads(state, social.UploadsPost.model_validate(_lister()), later)
    planned = social.plan(state, later)
    assert planned.start_at == later and planned.slots
    assert all(s.due >= later for s in planned.slots.values())


def _first_week_state() -> social.State:
    state = _state()
    state.start_at = NOW - timedelta(days=1)  # Tuesday of this week: this is the first week
    return state


def test_the_week_with_start_at_is_a_first_week_and_never_counts_toward_the_streak() -> None:
    state = _first_week_state()
    state.week_posted[social._week_of(NOW, state.rules).isoformat()] = 3  # target hit
    got = social.score(state, NOW)
    assert got["first_week"] is True
    assert got["streak"] == 0 and got["best_streak"] == 0
    # The next week is a normal one: it counts, and the first week still adds nothing.
    later = NOW + timedelta(days=7)
    state.week_posted[social._week_of(later, state.rules).isoformat()] = 3
    after = social.score(state, later)
    assert after["first_week"] is False and after["streak"] == 1


def test_the_chart_marks_weeks_before_the_start_and_the_first_week() -> None:
    got = social.score(_first_week_state(), NOW)
    states = [row.get("state") for row in got["weeks"]]
    assert states == ["before_start"] * 7 + ["first"]
    assert social.score(_state(), NOW)["first_week"] is False


def test_the_scorecard_lists_missed_uploads_tool_rotation_and_upload_pace() -> None:
    state = _state()
    state.start_at = NOW - timedelta(days=40)
    social.apply_uploads(
        state,
        social.UploadsPost.model_validate(
            _lister(
                ae=[
                    _item("aaaaaaaaaaa", "long", NOW - timedelta(days=5), "Gone public"),
                    _item("bbbbbbbbbbb", "short", NOW + timedelta(days=2)),
                    _item("ccccccccccc", "live", NOW + timedelta(days=13)),
                    _item("ddddddddddd", "long", NOW + timedelta(days=20)),
                ]
            )
        ),
        NOW,
    )
    planned = social.plan(state, NOW)
    card = social.score(planned, NOW)
    assert [row["title"] for row in card["unposted"]] == ["Gone public", "Gone public"]
    assert card["unposted_total"] == 2
    assert card["pace"] == {
        "ae": {"long": 0, "short": 1, "live": 1},
        "hc": {"long": 0, "short": 0, "live": 0},
    }
    assert [row["id"] for row in card["rotation"]] == ["diagram", "skins", "omnigent"]
    assert all(row["last"] is None for row in card["rotation"])
    # A post marked late leaves the list.
    social.mark_posted(planned, card["unposted"][0]["id"], None, NOW)
    assert social.score(planned, NOW)["unposted_total"] == 1
    tool = next(s for s in planned.slots.values() if s.kind == "tool")
    social.mark_posted(planned, tool.id, None, NOW)
    row = next(r for r in social.score(planned, NOW)["rotation"] if r["id"] == tool.anchor)
    assert row["last"] is not None


def test_an_overdue_open_post_older_than_last_monday_stays_in_the_view() -> None:
    state = _state(_lister(ae=[_item("aaaaaaaaaaa", "long", NOW - timedelta(days=10))]))
    planned = social.plan(state, NOW)
    page = social.view(planned, NOW)
    ids = {row["id"] for row in page["slots"]}
    assert "li-video-ae-aaaaaaaaaaa" in ids and "yc-video-ae-aaaaaaaaaaa" in ids
    assert page["truncated"] is False
    # Closed posts that old stay out; only open ones are carried back.
    social.mark_posted(planned, "li-video-ae-aaaaaaaaaaa", None, NOW)
    social.skip(planned, "yc-video-ae-aaaaaaaaaaa")
    ids = {row["id"] for row in social.view(planned, NOW)["slots"]}
    assert not ids & {"li-video-ae-aaaaaaaaaaa", "yc-video-ae-aaaaaaaaaaa"}


def test_a_livestream_is_not_missed_before_it_goes_public() -> None:
    now = datetime(2026, 11, 13, 3, 0, tzinfo=UTC)  # Thursday evening in Los Angeles
    live = _item("lllllllllll", "live", datetime(2026, 11, 13, 16, 0, tzinfo=UTC))
    state = _state(_lister(ae=[live], read_at=now))
    planned = social.plan(state, now)
    announce = planned.slots["li-live-ae-lllllllllll"]
    assert announce.due < now  # the announcement is due Thursday and still open
    card = social.score(planned, now)
    assert card["unposted"] == [] and card["unposted_total"] == 0
    assert card["missed"] == 0
    # Once the stream is public the missing announcement is listed, dated by the upload.
    later = datetime(2026, 11, 13, 18, 0, tzinfo=UTC)
    row = social.score(planned, later)["unposted"][0]
    assert row["id"] == "li-live-ae-lllllllllll"
    assert row["due"] == "2026-11-13T16:00:00Z"


def test_unset_or_bad_channel_ids_and_tools_are_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(social.CHANNEL_IDS_ENV)
    monkeypatch.delenv(social.TOOLS_ENV)
    assert social.channel_ids() == {}
    assert social.default_tools() == []
    monkeypatch.setenv(social.CHANNEL_IDS_ENV, "not json")
    monkeypatch.setenv(social.TOOLS_ENV, "not json")
    assert social.channel_ids() == {}
    assert social.default_tools() == []
    monkeypatch.setenv(social.CHANNEL_IDS_ENV, json.dumps({"ae": "UCone", "hc": 5, "x": "y"}))
    monkeypatch.setenv(social.TOOLS_ENV, json.dumps([{"id": "Bad Id"}, 3, EXAMPLE_TOOLS[0]]))
    assert social.channel_ids() == {"ae": "UCone"}
    assert [tool.id for tool in social.default_tools()] == ["diagram"]


# ── the visual, the tool image and the redraft feed (2026-11-07) ─────────────────────────────

JPEG = b"\xff\xd8\xff" + bytes(range(256)) * 400  # about 100 KB
VIDEO = "aaaaaaaaaaa"
MAXRES = f"https://i.ytimg.com/vi/{VIDEO}/maxresdefault.jpg"
HQ = f"https://i.ytimg.com/vi/{VIDEO}/hqdefault.jpg"
TOOL_IMG = "https://tools.example.com/og-card-1200.jpg"


class FakeImages:
    """Answers the server's image fetches from a table of url -> response; counts the calls."""

    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        return self.routes.get(url, httpx.Response(404))


def _ok(body: bytes = JPEG, kind: str = "image/jpeg") -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": kind})


@pytest.fixture()
def images() -> Any:
    fake = FakeImages({MAXRES: _ok(), TOOL_IMG: _ok()})
    social_visual.reset_cache(httpx.MockTransport(fake))
    yield fake
    social_visual.reset_cache(None)


async def _visual(client: httpx.AsyncClient, slot: str, part: int = 0) -> httpx.Response:
    return await client.get(f"/v1/ae/social/visual/{slot}/{part}", headers=OWNER)


async def _tool_slot(client: httpx.AsyncClient) -> str:
    page = (await client.get("/v1/ae/social", headers=OWNER)).json()
    return next(row["id"] for row in page["slots"] if row["kind"] == "tool")


async def test_slot_rows_say_which_have_a_picture_and_why_not(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    now = datetime.now(UTC)
    later = _item("bbbbbbbbbbb", "long", now + timedelta(days=2), "Later")
    later["privacy"] = "private"
    await _seed(
        client, _lister(ae=[_item(VIDEO, "long", now + timedelta(days=1), "Live now"), later])
    )
    page = (await client.get("/v1/ae/social", headers=OWNER)).json()
    by_id = {row["id"]: row for row in page["slots"]}
    assert by_id[f"li-video-ae-{VIDEO}"]["visual"] == {"kind": "thumbnail", "key": VIDEO}
    assert "visual_note" not in by_id[f"li-video-ae-{VIDEO}"]
    # Private with a go-public time still ahead: no thumbnail yet, and the card says so.
    waiting = by_id["li-video-ae-bbbbbbbbbbb"]
    assert "visual" not in waiting
    assert waiting["visual_note"] == "The thumbnail shows once the video is public."
    # Private, but its go-public time has passed: public now.
    past = social.Item.model_validate(
        {**_item("ccccccccccc", "long", now - timedelta(hours=1)), "privacy": "private"}
    )
    assert social._public_now(past, now) and not social._public_now(
        past.model_copy(update={"public_at": now + timedelta(hours=1)}), now
    )
    tools = [row for row in page["slots"] if row["kind"] == "tool"]
    assert tools and all(
        row["visual"]["kind"] == "tool" and row["visual"]["key"].startswith("https://")
        for row in tools
    )
    experts = [row for row in page["slots"] if row["kind"] == "expert"]
    assert experts and all("visual" not in row and "visual_note" not in row for row in experts)


def test_tools_carry_an_optional_https_image_from_the_environment() -> None:
    state = social.State()
    images = {t.id: t.image for t in state.tools}
    assert images["diagram"] == images["skins"] == TOOL_IMG
    assert images["omnigent"] == "https://opengraph.githubassets.com/1/example/repo"
    # No built-in picture: a tool with no env entry shares text only (2026-11-07).
    assert social.Tool(id="mine", name="M", url="https://x.test/m").image is None
    for bad in (
        "http://tools.example.com/i.jpg",
        "https://tools.example.com/" + "a" * 300,
        "https://tools.example.com/a b",
        "https://evil.test/i.jpg",
    ):
        with pytest.raises(ValueError, match="image"):
            social.Tool(id="mine", name="M", url="https://x.test/m", image=bad)


async def test_a_tool_image_off_the_allowed_hosts_is_refused_when_saved(
    client: httpx.AsyncClient, admin: dict[str, str], images: FakeImages
) -> None:
    await _seed(client)
    tool = {"id": "mine", "name": "M", "url": "https://x.test/m"}
    refused = await client.post(
        "/v1/ae/social/tools",
        headers=OWNER,
        json={"tools": [{**tool, "image": "https://evil.test/i.jpg"}]},
    )
    assert refused.status_code == 422 and "AE_SOCIAL_IMAGE_HOSTS" in refused.text
    saved = await client.post(
        "/v1/ae/social/tools",
        headers=OWNER,
        json={"tools": [{**tool, "image": TOOL_IMG}, {**tool, "id": "plain", "image": None}]},
    )
    assert saved.status_code == 200, saved.text
    page = (await client.get("/v1/ae/social", headers=OWNER)).json()
    assert {t["id"]: t["image"] for t in page["tools"]} == {"mine": TOOL_IMG, "plain": None}


async def test_the_visual_route_serves_the_thumbnail_in_chunks_under_the_cap(
    client: httpx.AsyncClient, admin: dict[str, str], images: FakeImages
) -> None:
    await _seed(client)
    slot = f"li-video-ae-{VIDEO}"
    first = await _visual(client, slot, 0)
    assert first.status_code == 200, first.text
    head = first.json()
    assert head["type"] == "image/jpeg" and head["size"] == len(JPEG) and head["part"] == 0
    assert head["parts"] > 1
    pieces = [head["data"]]
    for part in range(1, head["parts"]):
        got = await _visual(client, slot, part)
        assert len(got.text) < social.RESPONSE_CAP
        pieces.append(got.json()["data"])
    assert all(len(p) <= social_visual.CHUNK_CHARS for p in pieces)
    # Every part decodes on its own, and together they are the original bytes.
    assert b"".join(base64.b64decode(p, validate=True) for p in pieces) == JPEG
    assert (await _visual(client, slot, head["parts"])).status_code == 400
    # One fetch served every part (the cache), and only maxres was asked for.
    assert images.calls == [MAXRES]


async def test_the_visual_route_falls_back_to_hqdefault_on_a_404(
    client: httpx.AsyncClient, admin: dict[str, str], images: FakeImages
) -> None:
    images.routes = {HQ: _ok(b"\xff\xd8small")}
    await _seed(client)
    got = (await _visual(client, f"li-video-ae-{VIDEO}")).json()
    assert got["size"] == 7 and got["parts"] == 1
    assert images.calls == [MAXRES, HQ]


async def test_the_visual_route_serves_a_tool_image(
    client: httpx.AsyncClient, admin: dict[str, str], images: FakeImages
) -> None:
    await _seed(client)
    got = await _visual(client, await _tool_slot(client))
    assert got.status_code == 200, got.text
    assert images.calls and images.calls[0].startswith("https://")


async def test_the_visual_route_is_a_404_without_a_picture(
    client: httpx.AsyncClient, admin: dict[str, str], images: FakeImages
) -> None:
    now = datetime.now(UTC)
    private = _item("bbbbbbbbbbb", "long", now + timedelta(days=2))
    private["privacy"] = "private"
    await _seed(client, _lister(ae=[private]))
    page = (await client.get("/v1/ae/social", headers=OWNER)).json()
    expert = next(row["id"] for row in page["slots"] if row["kind"] == "expert")
    for slot in ("li-video-ae-bbbbbbbbbbb", expert, "li-nothing-here"):
        got = await _visual(client, slot)
        assert got.status_code == 404, (slot, got.text)
    assert (
        (await _visual(client, "li-video-ae-bbbbbbbbbbb"))
        .json()["error"]["message"]
        .startswith("The thumbnail shows once")
    )
    assert images.calls == []  # nothing was fetched for a post that has no picture


async def test_the_visual_route_follows_a_redirect_only_within_the_allowed_hosts(
    client: httpx.AsyncClient, admin: dict[str, str], images: FakeImages
) -> None:
    await _seed(client)
    slot = f"li-video-ae-{VIDEO}"
    inside = f"https://i.ytimg.com/vi/{VIDEO}/moved.jpg"
    images.routes = {MAXRES: httpx.Response(302, headers={"location": inside}), inside: _ok()}
    assert (await _visual(client, slot)).status_code == 200
    social_visual.reset_cache(httpx.MockTransport(images))
    images.routes = {MAXRES: httpx.Response(302, headers={"location": "https://evil.test/x.jpg"})}
    off = await _visual(client, slot)
    assert off.status_code == 502 and "redirects off" in off.text
    assert "https://evil.test/x.jpg" not in images.calls  # never requested
    social_visual.reset_cache(httpx.MockTransport(images))
    loop = httpx.Response(302, headers={"location": MAXRES})
    images.routes = {MAXRES: loop}
    assert (await _visual(client, slot)).status_code == 502


@pytest.mark.parametrize(
    ("response", "words"),
    [
        (_ok(b"x" * (social_visual.MAX_BYTES + 1)), "larger than 3 MB"),
        (_ok(b"GIF89a", "image/gif"), "not a jpeg or png"),
        (_ok(b"", "image/jpeg"), "empty"),
        (httpx.Response(500), "answered 500"),
    ],
)
async def test_the_visual_route_refuses_a_bad_image(
    client: httpx.AsyncClient,
    admin: dict[str, str],
    images: FakeImages,
    response: httpx.Response,
    words: str,
) -> None:
    images.routes = {MAXRES: response}
    await _seed(client)
    got = await _visual(client, f"li-video-ae-{VIDEO}")
    assert got.status_code == 502 and words in got.text, got.text


def test_the_cache_holds_twenty_pictures_for_an_hour() -> None:
    social_visual.reset_cache()
    pic = social_visual.Picture("image/jpeg", 1, "AAAA")
    for n in range(25):
        social_visual._remember(f"k{n}", pic, 1000.0)
    assert len(social_visual._cache) == social_visual.CACHE_ENTRIES
    assert social_visual._cached("k0", 1000.0) is None  # the oldest went first
    assert social_visual._cached("k24", 1000.0 + social_visual.CACHE_TTL_S) is pic
    assert social_visual._cached("k24", 1001.0 + social_visual.CACHE_TTL_S) is None  # expired
    social_visual.reset_cache()


def test_only_allowed_https_hosts_pass() -> None:
    ok = social_visual.host_allowed
    hosts = social_visual.tool_image_hosts()
    assert ok("https://tools.example.com/a.jpg", hosts)
    assert ok("https://opengraph.githubassets.com/1/a/b", hosts)
    for bad in (
        "http://tools.example.com/a.jpg",
        "https://tools.example.com.evil.test/a.jpg",
        "https://tools.example.com" + "\x40evil.test/a.jpg",  # userinfo, not a host
        "https://evil.test" + "\x40tools.example.com/a.jpg",
        "https://tools.example.com:8443/a.jpg",
        "https://sub.tools.example.com/a.jpg",
    ):
        assert not ok(bad, hosts), bad
    assert social_visual.thumbnail_urls("short") == []


async def test_week_redraft_adds_unedited_auto_drafts_and_apply_keeps_the_rest(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    await _seed(client)
    auto, reid, edited = (
        f"li-video-ae-{VIDEO}",
        f"yc-video-ae-{VIDEO}",
        "li-tool-redraft-edited",
    )
    now = datetime.now(UTC)

    def drafts(state: social.State) -> None:
        state.slots[edited] = social.Slot(
            id=edited, kind="expert", platform="linkedin", due=now + timedelta(days=1), manual=True
        )
        state.drafts[auto] = social.Draft(text="old auto", by="auto", at=now)
        state.drafts[reid] = social.Draft(text="mine", by="reid", at=now, edited=True)
        state.drafts[edited] = social.Draft(text="polished", by="auto", at=now, edited=True)

    social.store().mutate(drafts)
    plain = (await client.get("/v1/ae/social/week", headers=OWNER)).json()
    assert auto not in {s["id"] for s in plain["slots"]}  # the plain feed never lists a draft
    redraft = (await client.get("/v1/ae/social/week-redraft", headers=OWNER)).json()
    rows = {s["id"]: s for s in redraft["slots"]}
    assert rows[auto]["redraft"] is True and rows[auto]["context"]["title"] == "Real video"
    assert reid not in rows and edited not in rows
    assert all("redraft" not in row for sid, row in rows.items() if sid != auto)
    # Writing them all back replaces only the unedited auto draft.
    put = await client.post(
        "/v1/ae/social/drafts",
        headers=OWNER,
        json={
            "drafts": [{"id": sid, "text": "new"} for sid in (auto, reid, edited)],
            "errors": [],
        },
    )
    assert put.json() == {"written": [auto], "kept": [reid, edited], "errors_set": []}
    texts = {sid: d.text for sid, d in social.store().read().drafts.items()}
    assert texts[auto] == "new" and texts[reid] == "mine" and texts[edited] == "polished"


async def test_the_relay_week_redraft_flag_asks_the_redraft_feed() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"slots": []})

    settings = omni.Settings(server_url="https://omni.test", token="t")
    for flag, path in ((False, "/v1/ae/social/week"), (True, "/v1/ae/social/week-redraft")):
        out = io.StringIO()
        code = await social_relay.run_week(
            settings, transport=httpx.MockTransport(handler), out=out, redraft=flag
        )
        assert code == 0, out.getvalue()
        assert seen[-1] == path


def test_redraft_leaves_out_the_topics_of_the_slots_it_redrafts() -> None:
    now = datetime.now(UTC)
    state = social.State()
    for sid, by, edited, topic in (
        ("li-expert-a-1", "auto", False, "Old topic"),
        ("li-expert-b-1", "reid", True, "Kept topic"),
    ):
        state.slots[sid] = social.Slot(
            id=sid, kind="expert", platform="linkedin", due=now + timedelta(days=1), manual=True
        )
        state.drafts[sid] = social.Draft(text="t", topic=topic, by=by, at=now, edited=edited)
    plain = social.week_context(state, now)
    assert plain["recent_topics"] == ["Old topic", "Kept topic"]
    again = social.week_context(state, now, redraft=True)
    assert [s["id"] for s in again["slots"]] == ["li-expert-a-1"]
    assert again["recent_topics"] == ["Kept topic"]


def test_image_hosts_are_github_previews_plus_the_environments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(social_visual.IMAGE_HOSTS_ENV)
    assert social_visual.tool_image_hosts() == ("opengraph.githubassets.com",)
    assert not social_visual.tool_image_ok("https://tools.example.com/a.jpg")
    monkeypatch.setenv(social_visual.IMAGE_HOSTS_ENV, " Tools.Example.com , cdn.example.com ,")
    assert social_visual.tool_image_hosts()[1:] == ("tools.example.com", "cdn.example.com")
    assert social_visual.tool_image_ok("https://cdn.example.com/a.jpg")


def test_a_saved_tool_without_an_image_takes_the_env_picture_by_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved = {"id": "diagram", "name": "D", "url": "https://x.test/d"}
    # Saved before the field existed: picks up the env entry's picture, no re-save needed.
    assert social.Tool.model_validate(saved).image == TOOL_IMG
    # Its own picture wins, and an explicit null (cleared by the owner) stays cleared.
    own = "https://opengraph.githubassets.com/1/own/pic"
    assert social.Tool.model_validate({**saved, "image": own}).image == own
    assert social.Tool.model_validate({**saved, "image": None}).image is None
    # A whole saved state benefits the same way.
    state = social.State.model_validate({"tools": [saved, {**saved, "id": "mine"}]})
    assert [t.image for t in state.tools] == [TOOL_IMG, None]
    # An env picture off the allowed hosts is ignored.
    monkeypatch.setenv(
        social.TOOLS_ENV,
        json.dumps([{**EXAMPLE_TOOLS[0], "image": "https://evil.test/i.jpg"}]),
    )
    assert social.Tool.model_validate(saved).image is None
    monkeypatch.setenv(social_visual.IMAGE_HOSTS_ENV, "")
    monkeypatch.setenv(social.TOOLS_ENV, json.dumps(EXAMPLE_TOOLS))
    assert social.Tool.model_validate(saved).image is None
    assert (
        social.Tool.model_validate({**saved, "id": "omnigent"}).image
        == "https://opengraph.githubassets.com/1/example/repo"
    )
