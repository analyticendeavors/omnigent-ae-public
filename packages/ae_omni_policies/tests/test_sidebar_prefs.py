"""The manual sidebar order: the store, its file, and ``/v1/ae/sidebar``."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from ae_omni_policies import auth, sidebar_prefs
from ae_omni_policies.sidebar_prefs import SidebarOrderStore, normalize_order

A = "a" * 32
B = "b" * 32
C = "c" * 32


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch: pytest.MonkeyPatch) -> None:
    # No state file: the store stays in memory, so nothing writes to /data.
    monkeypatch.delenv(sidebar_prefs.STATE_FILE_ENV, raising=False)
    monkeypatch.setattr(sidebar_prefs, "DATA_DIR", Path("/nonexistent-ae-data"))
    sidebar_prefs.reset_store()
    yield
    sidebar_prefs.reset_store()


# ── normalize_order ─────────────────────────────────────────────────────


def test_normalize_keeps_the_first_of_duplicates() -> None:
    assert normalize_order([A, B, A, C, B]) == [A, B, C]


@pytest.mark.parametrize("bad", ["", "abc", "A" * 32, "g" * 32, "a" * 31, "a" * 33])
def test_normalize_names_the_bad_id(bad: str) -> None:
    with pytest.raises(ValueError, match="not a session id"):
        normalize_order([A, bad])


def test_normalize_rejects_more_than_500_after_dedupe() -> None:
    ids = [f"{n:032x}" for n in range(501)]
    with pytest.raises(ValueError, match="501"):
        normalize_order(ids)
    assert len(normalize_order(ids[:500] + ids[:500])) == 500


# ── the store ───────────────────────────────────────────────────────────


def test_store_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "state" / "sidebar.json"
    first = SidebarOrderStore(path)
    assert first.get("owner").model_dump() == {"order": [], "revision": 0}
    assert first.put("owner", [A, B], 0).state.revision == 1
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["users"]["owner"]["order"] == [A, B]
    assert raw["users"]["owner"]["revision"] == 1
    assert raw["users"]["owner"]["updated_at"]
    again = SidebarOrderStore(path).get("owner")
    assert again.model_dump() == {"order": [A, B], "revision": 1}


def test_store_moves_a_corrupt_file_aside_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sidebar.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert SidebarOrderStore(path).get("owner").model_dump() == {"order": [], "revision": 0}
    assert "moved it to" in caplog.text
    assert list(tmp_path.glob("sidebar.json.corrupt-*"))


def test_store_skips_malformed_entries(tmp_path: Path) -> None:
    path = tmp_path / "sidebar.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "users": {
                    "bad": {"order": "nope", "revision": 1},
                    "ok": {"order": [A], "revision": 3},
                },
            }
        ),
        encoding="utf-8",
    )
    store = SidebarOrderStore(path)
    assert store.get("bad").revision == 0
    assert store.get("ok").model_dump() == {"order": [A], "revision": 3}


def test_a_corrupt_file_survives_the_next_put(tmp_path: Path) -> None:
    path = tmp_path / "sidebar.json"
    path.write_text("{not json", encoding="utf-8")
    store = SidebarOrderStore(path)
    assert store.put("owner", [A], 0).state.revision == 1
    kept = list(tmp_path.glob("sidebar.json.corrupt-*"))
    assert [p.read_text(encoding="utf-8") for p in kept] == ["{not json"]
    assert SidebarOrderStore(path).get("owner").order == [A]


def test_a_wrong_shape_file_is_also_kept(tmp_path: Path) -> None:
    path = tmp_path / "sidebar.json"
    path.write_text("[1, 2]", encoding="utf-8")
    SidebarOrderStore(path).get("owner")
    assert not path.exists()
    assert len(list(tmp_path.glob("sidebar.json.corrupt-*"))) == 1


def test_a_bad_file_that_cannot_move_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "sidebar.json"
    path.write_text("{not json", encoding="utf-8")

    def refuse(*_: object) -> None:
        raise OSError("read-only")

    monkeypatch.setattr(sidebar_prefs.os, "replace", refuse)
    store = SidebarOrderStore(path)
    assert store.put("owner", [A], 0).state.revision == 1
    assert store.get("owner").order == [A]
    assert path.read_text(encoding="utf-8") == "{not json"


def test_load_cleans_a_hand_edited_order(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "sidebar.json"
    path.write_text(
        json.dumps({"version": 1, "users": {"u": {"order": [A, "A" * 32, A, B], "revision": 2}}}),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        assert SidebarOrderStore(path).get("u").order == [A, B]
    assert "dropped 2" in caplog.text


def _failing_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(sidebar_prefs.tempfile, "mkstemp", refuse)


def test_a_failed_save_rolls_back_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_writes(monkeypatch)
    store = SidebarOrderStore(tmp_path / "sidebar.json")
    with pytest.raises(sidebar_prefs.SidebarSaveError):
        store.put("owner", [A], 0)
    assert store.get("owner").model_dump() == {"order": [], "revision": 0}


def test_store_without_a_path_stays_in_memory(tmp_path: Path) -> None:
    store = SidebarOrderStore(None)
    assert store.put("owner", [A], 0).state.revision == 1
    assert store.get("owner").order == [A]
    assert SidebarOrderStore(None).get("owner").order == []


def test_put_conflicts_on_a_stale_revision() -> None:
    store = SidebarOrderStore(None)
    store.put("owner", [A], 0)
    stale = store.put("owner", [B], 0)
    assert stale.conflict is True
    assert stale.state.model_dump() == {"order": [A], "revision": 1}
    assert store.put("owner", [B, A], 1).state.revision == 2


def test_default_path_follows_env_then_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert sidebar_prefs._default_path() is None
    monkeypatch.setattr(sidebar_prefs, "DATA_DIR", tmp_path)
    assert sidebar_prefs._default_path() == tmp_path / "ae-sidebar.json"
    monkeypatch.setenv(sidebar_prefs.STATE_FILE_ENV, str(tmp_path / "x.json"))
    assert sidebar_prefs._default_path() == tmp_path / "x.json"


# ── the routes ──────────────────────────────────────────────────────────


async def test_routes_round_trip(client: httpx.AsyncClient) -> None:
    empty = await client.get("/v1/ae/sidebar")
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"order": [], "revision": 0}

    put = await client.put("/v1/ae/sidebar", json={"order": [A, B, A], "revision": 0})
    assert put.status_code == 200, put.text
    assert put.json() == {"order": [A, B], "revision": 1}
    assert (await client.get("/v1/ae/sidebar")).json() == {"order": [A, B], "revision": 1}

    second = await client.put("/v1/ae/sidebar", json={"order": [B, A], "revision": 1})
    assert second.json() == {"order": [B, A], "revision": 2}


async def test_stale_revision_is_a_409_with_the_current_state(client: httpx.AsyncClient) -> None:
    await client.put("/v1/ae/sidebar", json={"order": [A], "revision": 0})
    stale = await client.put("/v1/ae/sidebar", json={"order": [B], "revision": 0})
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "conflict"
    assert "another device" in error["message"]
    assert error["order"] == [A]
    assert error["revision"] == 1


async def test_bad_ids_and_too_many_are_400(client: httpx.AsyncClient) -> None:
    bad = await client.put("/v1/ae/sidebar", json={"order": [A, "nope"], "revision": 0})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_input"
    assert "nope" in bad.json()["error"]["message"]
    many = await client.put(
        "/v1/ae/sidebar", json={"order": [f"{n:032x}" for n in range(501)], "revision": 0}
    )
    assert many.status_code == 400
    assert "501" in many.json()["error"]["message"]
    assert (await client.get("/v1/ae/sidebar")).json()["revision"] == 0


async def test_a_trailing_newline_is_not_an_id(client: httpx.AsyncClient) -> None:
    response = await client.put("/v1/ae/sidebar", json={"order": [A + "\n"], "revision": 0})
    assert response.status_code == 400


async def test_2001_ids_are_refused_outright(client: httpx.AsyncClient) -> None:
    ids = [f"{n:032x}" for n in range(2001)]
    assert (
        await client.put("/v1/ae/sidebar", json={"order": ids, "revision": 0})
    ).status_code == 422
    ok = await client.put("/v1/ae/sidebar", json={"order": ids[:2000], "revision": 0})
    assert ok.status_code == 400


async def test_a_failed_write_is_a_503_and_changes_nothing(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_writes(monkeypatch)
    sidebar_prefs.reset_store(SidebarOrderStore(tmp_path / "sidebar.json"))
    failed = await client.put("/v1/ae/sidebar", json={"order": [A], "revision": 0})
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "sidebar_not_saved"
    assert (await client.get("/v1/ae/sidebar")).json() == {"order": [], "revision": 0}


async def test_put_is_json_only_and_origin_checked(client: httpx.AsyncClient) -> None:
    form = await client.put(
        "/v1/ae/sidebar",
        content="order=1",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 415
    foreign = await client.put(
        "/v1/ae/sidebar",
        json={"order": [A], "revision": 0},
        headers={"Origin": "https://evil.example"},
    )
    assert foreign.status_code == 403
    assert (await client.get("/v1/ae/sidebar")).json()["revision"] == 0


async def test_a_negative_revision_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.put("/v1/ae/sidebar", json={"order": [A], "revision": -1})
    assert response.status_code == 422


async def test_routes_need_an_identity_off_single_user(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    assert (await client.get("/v1/ae/sidebar")).status_code == 401
    put = await client.put("/v1/ae/sidebar", json={"order": [A], "revision": 0})
    assert put.status_code == 401


async def test_two_users_are_kept_apart(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    owner = {"X-Forwarded-Email": "owner@example.com"}
    member = {"X-Forwarded-Email": "member@example.com"}
    assert (
        await client.put("/v1/ae/sidebar", json={"order": [A], "revision": 0}, headers=owner)
    ).status_code == 200
    assert (await client.get("/v1/ae/sidebar", headers=member)).json() == {
        "order": [],
        "revision": 0,
    }
    other = await client.put("/v1/ae/sidebar", json={"order": [B], "revision": 0}, headers=member)
    assert other.json() == {"order": [B], "revision": 1}
    assert (await client.get("/v1/ae/sidebar", headers=owner)).json()["order"] == [A]
