"""Made pictures for Social posts: store, routes, drafting list and relay (2026-11-15)."""

from __future__ import annotations

import base64
import io
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import omni, social, social_made, social_relay, social_visual

TOOLS = [
    {
        "id": "diagram",
        "name": "Diagram tool",
        "url": "https://tools.example.com/diagram",
        "image": "https://tools.example.com/og-card-1200.jpg",
    }
]
OWNER = {"X-Forwarded-Email": "owner@example.com"}
OTHER_USER = {"X-Forwarded-Email": "user-b@example.com"}


def _jpeg(n: int, size: int = 50_000) -> bytes:
    return b"\xff\xd8\xff" + bytes([n]) * size


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture(autouse=True)
def _set_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AE_PRIVATE_CARDS", "social")
    monkeypatch.setenv(social.TOOLS_ENV, json.dumps(TOOLS))
    monkeypatch.setenv(social.CHANNEL_IDS_ENV, json.dumps({"ae": "UCexample", "hc": "UCother"}))
    monkeypatch.setenv(social_visual.IMAGE_HOSTS_ENV, "tools.example.com")


@pytest.fixture()
def admin(ae_admins: set[str]) -> dict[str, str]:
    return OWNER


async def _seed(client: httpx.AsyncClient) -> dict[str, Any]:
    now = datetime.now(UTC)
    body = {
        "read_at": now.isoformat(),
        "channels": {
            "ae": {"ok": True, "error": None, "items": []},
            "hc": {"ok": True, "error": None, "items": []},
        },
    }
    got = await client.post("/v1/ae/social/uploads", json=body, headers=OWNER)
    assert got.status_code == 200, got.text
    return (await client.get("/v1/ae/social", headers=OWNER)).json()


def _first(page: dict[str, Any], kind: str, *, within_days: int = 7) -> str:
    end = datetime.now(UTC) + timedelta(days=within_days)
    return next(
        row["id"]
        for row in page["slots"]
        if row["kind"] == kind
        and row["state"] == "planned"
        and datetime.fromisoformat(row["due"]) <= end
    )


def _set(n: int = 4) -> list[dict[str, Any]]:
    kinds = ["diagram", "picture", "screenshot", "diagram", "picture"]
    return [
        {
            "kind": kinds[i],
            "label": f"Candidate {i + 1}",
            "full": _b64(_jpeg(i + 1)),
            "preview": _b64(_jpeg(i + 1, 2_000)),
        }
        for i in range(n)
    ]


async def _visual(client: httpx.AsyncClient, slot: str, query: str = "") -> httpx.Response:
    return await client.get(f"/v1/ae/social/visual/{slot}/0{query}", headers=OWNER)


async def _whole(client: httpx.AsyncClient, slot: str, query: str = "") -> bytes:
    head = (await _visual(client, slot, query)).json()
    parts = [head["data"]]
    for part in range(1, head["parts"]):
        got = await client.get(f"/v1/ae/social/visual/{slot}/{part}{query}", headers=OWNER)
        parts.append(got.json()["data"])
    return base64.b64decode("".join(parts))


# ── routes ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/ae/social/visuals", {"id": "li-tool-2026w46", "candidates": _set(1)}),
        ("POST", "/v1/ae/social/visual-choice", {"id": "li-tool-2026w46", "choice": "c1"}),
        ("GET", "/v1/ae/social/visual/li-tool-2026w46/0?c=c1", None),
    ],
)
async def test_the_new_routes_are_admin_only(
    client: httpx.AsyncClient, admin: dict[str, str], method: str, path: str, body: Any
) -> None:
    refused = await client.request(method, path, json=body, headers=OTHER_USER)
    assert refused.status_code == 403, refused.text
    allowed = await client.request(method, path, json=body, headers=OWNER)
    assert allowed.status_code in (200, 404, 409, 422), allowed.text


async def test_an_upload_lists_the_set_best_first_and_shares_the_first(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    page = await _seed(client)
    tool = _first(page, "tool", within_days=35)
    got = await client.post(
        "/v1/ae/social/visuals", json={"id": tool, "candidates": _set(5)}, headers=OWNER
    )
    assert got.status_code == 200, got.text
    assert got.json() == {"ok": True, "id": tool, "candidates": 5}
    row = next(
        r
        for r in (await client.get("/v1/ae/social", headers=OWNER)).json()["slots"]
        if r["id"] == tool
    )
    assert [v["id"] for v in row["visuals"]] == ["c1", "c2", "c3", "c4", "c5"]
    assert [v["kind"] for v in row["visuals"]][:3] == ["diagram", "picture", "screenshot"]
    assert row["visual_choice"] == "c1" and row["visual_choice_by"] == "auto"
    # The made picture replaces the tool's site card.
    assert row["visual"] == {"kind": "made", "key": f"c1:{row['visuals'][0]['key']}"}
    assert await _whole(client, tool) == _jpeg(1)
    assert await _whole(client, tool, "?c=c3") == _jpeg(3)
    assert await _whole(client, tool, "?c=c2&preview=true") == _jpeg(2, 2_000)
    assert (await _visual(client, tool, "?c=c9")).status_code == 422  # off the id pattern
    # A second set for the same post is refused; the drafter treats that as done.
    again = await client.post(
        "/v1/ae/social/visuals", json={"id": tool, "candidates": _set(1)}, headers=OWNER
    )
    assert again.status_code == 409, again.text


async def test_reid_picks_another_or_none(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    page = await _seed(client)
    expert = _first(page, "expert", within_days=35)
    assert (
        await client.post(
            "/v1/ae/social/visuals", json={"id": expert, "candidates": _set(4)}, headers=OWNER
        )
    ).status_code == 200
    pick = await client.post(
        "/v1/ae/social/visual-choice", json={"id": expert, "choice": "c3"}, headers=OWNER
    )
    assert pick.status_code == 200, pick.text
    detail = (await client.post("/v1/ae/social/slot", json={"id": expert}, headers=OWNER)).json()
    assert detail["visual_choice"] == "c3" and detail["visual_choice_by"] == "reid"
    assert detail["visual"]["key"].startswith("c3:")
    assert await _whole(client, expert) == _jpeg(3)
    missing = await client.post(
        "/v1/ae/social/visual-choice", json={"id": expert, "choice": "c5"}, headers=OWNER
    )
    assert missing.status_code == 404, missing.text
    none = await client.post(
        "/v1/ae/social/visual-choice", json={"id": expert, "choice": "none"}, headers=OWNER
    )
    assert none.status_code == 200
    detail = (await client.post("/v1/ae/social/slot", json={"id": expert}, headers=OWNER)).json()
    assert "visual" not in detail and detail["visual_note"] == "No picture: you picked none."
    gone = await _visual(client, expert)
    assert gone.status_code == 404 and "picked none" in gone.json()["error"]["message"]
    # The others stay reachable for the picker.
    assert await _whole(client, expert, "?c=c1") == _jpeg(1)


async def test_uploads_are_checked(client: httpx.AsyncClient, admin: dict[str, str]) -> None:
    page = await _seed(client)
    tool = _first(page, "tool", within_days=35)

    async def post(candidates: list[dict[str, Any]], slot: str = tool) -> httpx.Response:
        return await client.post(
            "/v1/ae/social/visuals", json={"id": slot, "candidates": candidates}, headers=OWNER
        )

    png = [{**_set(1)[0], "full": _b64(b"\x89PNG\r\n" + b"0" * 100)}]
    assert (await post(png)).status_code == 400
    big = [{**_set(1)[0], "full": _b64(_jpeg(1, social_made.MAX_FULL_BYTES))}]
    assert (await post(big)).status_code in (400, 422)
    assert (await post([{**_set(1)[0], "full": "not base64!!"}])).status_code == 400
    assert (await post(_set(5) + _set(1))).status_code == 422  # six is too many
    assert (await post([{**_set(1)[0], "kind": "meme"}])).status_code == 422
    assert (await post(_set(1), "li-nothing-here")).status_code == 404
    # Nothing stuck: a good set still goes in after the refusals.
    assert (await post(_set(2))).status_code == 200


def test_only_open_tool_and_expert_linkedin_posts_take_pictures() -> None:
    now = datetime.now(UTC)
    state = social.State()
    state.slots["li-video-ae-aaaaaaaaaaa"] = social.Slot(
        id="li-video-ae-aaaaaaaaaaa", kind="video", platform="linkedin", due=now
    )
    state.slots["li-tool-2026w47"] = social.Slot(
        id="li-tool-2026w47", kind="tool", platform="linkedin", due=now, state="posted"
    )
    with pytest.raises(social.SocialError) as video:
        social.visuals_allowed(state, "li-video-ae-aaaaaaaaaaa")
    assert video.value.status == 422
    with pytest.raises(social.SocialError) as closed:
        social.visuals_allowed(state, "li-tool-2026w47")
    assert closed.value.status == 409


def test_the_set_goes_with_its_slot_when_history_is_pruned() -> None:
    now = datetime.now(UTC)
    state = social.State()
    state.slots["li-expert-2020w01-1"] = social.Slot(
        id="li-expert-2020w01-1", kind="expert", platform="linkedin", due=now
    )
    social.apply_visuals(state, "li-expert-2020w01-1", [("diagram", "D", "0" * 16)], now)
    state.slots["li-expert-2020w01-1"].due = now - timedelta(days=social.HISTORY_DAYS + 1)
    pruned = social.plan(state, now)
    assert pruned.visuals == {}
    assert social.kept_shas(pruned) == {}


# ── the drafting list ───────────────────────────────────────────────────────


async def test_the_week_lists_posts_that_need_pictures_with_their_text(
    client: httpx.AsyncClient, admin: dict[str, str]
) -> None:
    page = await _seed(client)
    soon = [
        row
        for row in page["slots"]
        if row["kind"] in ("tool", "expert")
        and row["state"] == "planned"
        and datetime.fromisoformat(row["due"]) <= datetime.now(UTC) + timedelta(days=7)
    ]
    assert soon, "the seeded plan has a tool or expert post this week"
    target = soon[0]["id"]
    await client.post(
        "/v1/ae/social/draft", json={"id": target, "text": "The post text"}, headers=OWNER
    )
    week = (await client.get("/v1/ae/social/week", headers=OWNER)).json()
    wanted = {row["id"]: row for row in week["needs_visuals"]}
    assert set(wanted) == {row["id"] for row in soon}
    assert wanted[target]["text"] == "The post text"
    assert all("redraft" not in row for row in wanted.values())
    await client.post(
        "/v1/ae/social/visuals", json={"id": target, "candidates": _set(1)}, headers=OWNER
    )
    week = (await client.get("/v1/ae/social/week", headers=OWNER)).json()
    assert target not in {row["id"] for row in week["needs_visuals"]}
    assert len(json.dumps(week)) < social.RESPONSE_CAP
    assert week["needs_visuals_truncated"] is False


# ── the store on disk ───────────────────────────────────────────────────────


def test_the_disk_store_round_trips_and_sweeps_after_the_grace(tmp_path: Path) -> None:
    store = social_made.MadeStore(tmp_path)
    a = store.put("li-tool-2026w47", _jpeg(1), _jpeg(1, 100))
    b = store.put("li-tool-2026w47", _jpeg(2), _jpeg(2, 100))
    c = store.put("li-expert-2026w47-1", _jpeg(3), _jpeg(3, 100))
    assert store.read("li-tool-2026w47", a).size == len(_jpeg(1))
    assert base64.b64decode(store.read("li-tool-2026w47", b, preview=True).b64) == _jpeg(2, 100)
    # Inside the grace nothing goes, even unreferenced files.
    store.keep_only({"li-tool-2026w47"}, {"li-tool-2026w47": {a}})
    assert store.read("li-expert-2026w47-1", c).size > 0
    later = datetime.now(UTC).timestamp() + social_made.SWEEP_GRACE_S + 5
    store.keep_only({"li-tool-2026w47"}, {"li-tool-2026w47": {a}}, now=later)
    assert store.read("li-tool-2026w47", a).size > 0
    for slot, sha in (("li-tool-2026w47", b), ("li-expert-2026w47-1", c)):
        with pytest.raises(social_made.MadeError) as gone:
            store.read(slot, sha)
        assert gone.value.status == 404
    assert not (tmp_path / "li-expert-2026w47-1").exists()
    with pytest.raises(social_made.MadeError):
        store.read("../etc", a)
    with pytest.raises(social_made.MadeError):
        store.read("li-tool-2026w47", "../../x")


def test_the_sweep_touches_only_its_own_files(tmp_path: Path) -> None:
    store = social_made.MadeStore(tmp_path)
    store.put("li-tool-2026w47", _jpeg(1), _jpeg(1, 100))
    foreign = tmp_path / "postgres"
    foreign.mkdir()
    (foreign / "data.db").write_bytes(b"keep me")
    (foreign / "0123456789abcdef.jpg.bak").write_bytes(b"keep me too")
    (tmp_path / "li-tool-2026w47" / "notes.txt").write_text("not a picture")
    later = datetime.now(UTC).timestamp() + social_made.SWEEP_GRACE_S + 5
    store.keep_only(set(), {}, now=later)
    assert (foreign / "data.db").read_bytes() == b"keep me"
    assert (foreign / "0123456789abcdef.jpg.bak").exists()
    assert (tmp_path / "li-tool-2026w47" / "notes.txt").exists()
    assert not list((tmp_path / "li-tool-2026w47").glob("*.jpg"))


def test_the_memory_store_keeps_a_fresh_upload_through_another_sweep() -> None:
    store = social_made.MadeStore(None)
    sha = store.put("li-tool-2026w47", _jpeg(1), _jpeg(1, 100))
    store.keep_only(set(), {})  # another upload's sweep, before this set is recorded
    assert store.read("li-tool-2026w47", sha).size == len(_jpeg(1))
    later = datetime.now(UTC).timestamp() + social_made.SWEEP_GRACE_S + 5
    store.keep_only(set(), {}, now=later)
    with pytest.raises(social_made.MadeError):
        store.read("li-tool-2026w47", sha)


def test_a_read_is_encoded_once_and_a_new_picture_is_never_served_from_the_cache(
    tmp_path: Path,
) -> None:
    store = social_made.MadeStore(tmp_path)
    sha = store.put("li-tool-2026w47", _jpeg(1), _jpeg(1, 100))
    first = store.read("li-tool-2026w47", sha)
    (tmp_path / "li-tool-2026w47" / f"{sha}.jpg").unlink()
    assert store.read("li-tool-2026w47", sha) is first  # served from the cache
    other = store.put("li-tool-2026w47", _jpeg(2), _jpeg(2, 100))
    assert base64.b64decode(store.read("li-tool-2026w47", other).b64) == _jpeg(2)


def test_the_root_follows_the_env_then_the_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(social_made.VISUALS_DIR_ENV, raising=False)
    assert social_made.default_root(None) is None
    assert social_made.default_root(tmp_path / "ae-social.json") == tmp_path / "social-visuals"
    monkeypatch.setenv(social_made.VISUALS_DIR_ENV, str(tmp_path / "elsewhere"))
    assert social_made.default_root(tmp_path / "ae-social.json") == tmp_path / "elsewhere"


# ── the relay ───────────────────────────────────────────────────────────────


async def test_the_relay_posts_a_visuals_file_and_a_409_is_not_a_failure(tmp_path: Path) -> None:
    (tmp_path / "c1.jpg").write_bytes(_jpeg(1))
    (tmp_path / "c1.prev.jpg").write_bytes(_jpeg(1, 100))
    spec = tmp_path / "visuals.json"
    spec.write_text(
        json.dumps(
            {
                "id": "li-tool-2026w47",
                "candidates": [
                    {
                        "kind": "diagram",
                        "label": "Steps",
                        "full": "c1.jpg",
                        "preview": "c1.prev.jpg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    seen: list[dict[str, Any]] = []
    status = {"code": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if status["code"] == 409:
            return httpx.Response(409, json={"error": {"message": "already has its pictures"}})
        return httpx.Response(200, json={"ok": True, "candidates": 1})

    settings = omni.Settings(server_url="https://omni.test", token="secret-token-2")
    out = io.StringIO()
    code = await social_relay.run_put_visuals(
        spec, settings, transport=httpx.MockTransport(handler), out=out
    )
    assert code == 0
    body = seen[0]
    assert body["id"] == "li-tool-2026w47"
    assert base64.b64decode(body["candidates"][0]["full"]) == _jpeg(1)
    assert "secret-token-2" not in out.getvalue()
    status["code"] = 409
    assert (
        await social_relay.run_put_visuals(
            spec, settings, transport=httpx.MockTransport(handler), out=io.StringIO()
        )
        == 0
    )
    os.remove(tmp_path / "c1.prev.jpg")
    bad = io.StringIO()
    assert (
        await social_relay.run_put_visuals(
            spec, settings, transport=httpx.MockTransport(handler), out=bad
        )
        == 1
    )
    assert "preview" in bad.getvalue()
