"""Role presets: the store, its file, validation, and ``/v1/ae/roles``."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from ae_omni_policies import auth, role_prefs
from ae_omni_policies.role_prefs import RolePrefsStore, RolesPut


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(role_prefs.STATE_FILE_ENV, raising=False)
    monkeypatch.setattr(role_prefs, "DATA_DIR", Path("/nonexistent-ae-data"))
    role_prefs.reset_store()
    yield
    role_prefs.reset_store()


def _preset(id: str = "mine", **overrides: Any) -> dict[str, Any]:
    raw = copy.deepcopy(role_prefs.starters()[1])
    raw.update(id=id, title=id.title(), pinned=False)
    raw.update(overrides)
    return raw


def _body(revision: int = 0, presets: list[dict[str, Any]] | None = None, **kw: Any) -> dict:
    presets = presets if presets is not None else [_preset("mine")]
    return {
        "revision": revision,
        "default_preset": kw.pop("default_preset", presets[0]["id"] if presets else "x"),
        "apply_start_on": kw.pop("apply_start_on", True),
        "presets": presets,
        **kw,
    }


_CODEX = {
    "mode": "agent",
    "agent": role_prefs.CODEX_AGENT_ID,
    "agent_name": role_prefs.CODEX_AGENT_NAME,
    "name": "Codex",
    "model": None,
    "effort": None,
}
_REVIEW_DEFAULTS = {"review_merge": "merged", "reviewer_unavailable": "run_rest"}
_PINNED = ["save-tokens", "balanced", "solo", "yolo"]
_NEW = ["think-it-through", "deep-debug", "quick-fix"]


# ── starters ────────────────────────────────────────────────────────────


def test_starters_use_real_catalog_ids() -> None:
    by_id = {p["id"]: p for p in role_prefs.starters()}
    assert list(by_id) == [*_PINNED, *_NEW]
    balanced = by_id["balanced"]["roles"]
    assert balanced["lead"] == {"mode": "session"}
    assert balanced["planner"] == {"mode": "fixed", "model": "claude-opus-5-5", "effort": "high"}
    assert balanced["reviewer"] == {
        "mode": "multi",
        "reviewers": [
            {"mode": "fixed", "model": "claude-sonnet-5-5", "effort": "medium"},
            _CODEX | {"effort": "medium"},
        ],
    }
    assert balanced["helper"] == {"mode": "fixed", "model": "claude-haiku-5-5", "effort": "low"}
    yolo = by_id["yolo"]
    fable = {"mode": "fixed", "model": "claude-fable-5-1", "effort": "max"}
    assert all(r == fable for role, r in yolo["roles"].items() if role != "reviewer")
    assert yolo["roles"]["reviewer"] == {
        "mode": "multi",
        "reviewers": [fable, _CODEX | {"effort": "xhigh"}],
    }
    assert yolo["rules"] == {
        "raise_or_lower": "off",
        "lead_steps_down": False,
        "auto_raises": 0,
        "helpers": "any",
    }
    assert by_id["save-tokens"]["rules"] == {
        "raise_or_lower": "tell",
        "lead_steps_down": True,
        "auto_raises": 1,
        "helpers": "any",
    }
    assert by_id["save-tokens"]["roles"]["reviewer"]["mode"] == "fixed"
    solo = by_id["solo"]
    # Solo keeps one reviewer: its Helpers rule allows no child sessions.
    assert solo["roles"] == {
        **by_id["balanced"]["roles"],
        "reviewer": {"mode": "fixed", "model": "claude-sonnet-5-5", "effort": "medium"},
    }
    assert solo["rules"] == {**by_id["balanced"]["rules"], "helpers": "own"}
    assert (solo["title"], solo["color"]) == ("Solo", "slate")
    assert all(p["when_to_use"] for p in by_id.values())
    opus = "claude-opus-5-5"
    sonnet = "claude-sonnet-5-5"
    think = by_id["think-it-through"]
    assert (think["title"], think["icon"], think["color"]) == (
        "Think it through",
        "brain",
        "violet",
    )
    assert think["roles"]["planner"] == {"mode": "fixed", "model": opus, "effort": "xhigh"}
    assert think["roles"]["builder"] == {"mode": "fixed", "model": sonnet, "effort": "medium"}
    assert [s["mode"] for s in think["roles"]["reviewer"]["reviewers"]] == ["fixed", "agent"]
    assert think["rules"]["lead_steps_down"] is True
    debug = by_id["deep-debug"]
    assert (debug["title"], debug["icon"], debug["color"]) == ("Deep debug", "bug", "amber")
    assert debug["roles"]["builder"] == {"mode": "fixed", "model": opus, "effort": "high"}
    assert debug["roles"]["helper"] == {"mode": "fixed", "model": sonnet, "effort": "low"}
    assert debug["rules"]["lead_steps_down"] is False
    quick = by_id["quick-fix"]
    assert (quick["title"], quick["icon"], quick["color"]) == ("Quick fix", "bolt", "blue")
    low = {"mode": "fixed", "model": sonnet, "effort": "low"}
    assert all(quick["roles"][r] == low for r in ("planner", "reviewer", "builder"))
    assert quick["rules"]["helpers"] == "own"
    assert all(p["roles"]["lead"] == {"mode": "session"} for p in (think, debug, quick))


def test_a_never_saved_user_gets_the_first_four_pinned_and_the_rest_not() -> None:
    state = RolePrefsStore(None).get("reid")
    assert [(p.id, p.pinned) for p in state.presets] == [
        *((i, True) for i in _PINNED),
        *((i, False) for i in _NEW),
    ]


def test_a_never_saved_user_reads_the_starters() -> None:
    state = RolePrefsStore(None).get("reid")
    assert state.revision == 0
    assert state.default_preset == "balanced"
    assert state.apply_start_on is True
    assert [p.id for p in state.presets] == [*_PINNED, *_NEW]


# ── validation ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (_body(presets=[_preset("a"), _preset("a")]), "unique"),
        (_body(default_preset="nope"), "not one of the presets"),
        (_body(presets=[], default_preset="x"), "at least 1"),
        (_body(presets=[_preset(f"p{n}") for n in range(31)]), "at most 30"),
        (_body(presets=[_preset("Bad")]), "pattern"),
        (_body(presets=[_preset("lead-picks")]), "reserved"),
        (_body(presets=[_preset("a", color="orange")]), "color"),
        (_body(presets=[_preset("a", title="")]), "title"),
        (_body(presets=[_preset("a", title="x" * 41)]), "title"),
        (_body(presets=[_preset("a", title="two\nlines")]), "title"),
        (_body(presets=[_preset("a", description="line\u2028break")]), "description"),
        (_body(presets=[_preset("a", when_to_use="x" * 401)]), "when_to_use"),
        (_body(presets=[_preset("a", icon="Bad Icon")]), "icon"),
        (_body(presets=[_preset("a", extra=1)]), "extra"),
        (_body(surprise=True), "surprise"),
    ],
)
def test_put_body_validation(body: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        RolesPut.model_validate(body)


def _with_role(role: str, setting: dict[str, Any]) -> dict[str, Any]:
    preset = _preset("a")
    preset["roles"][role] = setting
    return _body(presets=[preset])


@pytest.mark.parametrize(
    "body",
    [
        _with_role("planner", {"mode": "session"}),
        _with_role("builder", {"mode": "fixed", "model": "claude-opus-5-5"}),
        _with_role("builder", {"mode": "fixed", "model": "claude-opus-5-5", "effort": "huge"}),
        _with_role("builder", {"mode": "fixed", "model": "bad model\n", "effort": "low"}),
        _with_role("builder", {"mode": "auto", "model": "claude-opus-5-5"}),
        _with_role("builder", {"mode": "sometimes"}),
    ],
)
def test_role_settings_are_checked(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RolesPut.model_validate(body)


@pytest.mark.parametrize(
    "rules",
    [
        {"raise_or_lower": "maybe", "lead_steps_down": True, "auto_raises": 1},
        {"raise_or_lower": "tell", "lead_steps_down": "yes", "auto_raises": 1},
        {"raise_or_lower": "tell", "lead_steps_down": True, "auto_raises": 4},
        {"raise_or_lower": "tell", "lead_steps_down": True, "auto_raises": True},
        {"raise_or_lower": "tell", "lead_steps_down": True},
        {"raise_or_lower": "tell", "lead_steps_down": True, "auto_raises": 1, "helpers": "all"},
    ],
)
def test_rules_are_checked(rules: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RolesPut.model_validate(_body(presets=[_preset("a", rules=rules)]))


def test_lead_may_use_auto_or_session_and_others_auto() -> None:
    preset = _preset("a")
    preset["roles"]["lead"] = {"mode": "auto"}
    preset["roles"]["helper"] = {"mode": "auto"}
    put = RolesPut.model_validate(_body(presets=[preset]))
    assert put.presets[0].roles.helper.mode == "auto"


def test_helpers_defaults_to_any_for_presets_saved_before_it() -> None:
    preset = _preset("a")
    del preset["rules"]["helpers"]
    put = RolesPut.model_validate(_body(presets=[preset]))
    assert put.presets[0].rules.helpers == "any"


def test_a_single_reviewer_setting_is_still_valid() -> None:
    preset = _preset("a")
    preset["roles"]["reviewer"] = {"mode": "fixed", "model": "claude-sonnet-5-5", "effort": "low"}
    put = RolesPut.model_validate(_body(presets=[preset]))
    assert put.presets[0].roles.reviewer.model_dump() == preset["roles"]["reviewer"]


def test_review_rules_default_for_presets_saved_before_them() -> None:
    preset = _preset("a")
    for key in _REVIEW_DEFAULTS:
        preset["rules"].pop(key, None)
    put = RolesPut.model_validate(_body(presets=[preset]))
    assert put.presets[0].rules.review_merge == "merged"
    assert put.presets[0].rules.reviewer_unavailable == "run_rest"


@pytest.mark.parametrize("count", [1, 2, 3])
def test_one_to_three_reviewers(count: int) -> None:
    slots = [
        {"mode": "fixed", "model": "claude-sonnet-5-5", "effort": "medium"},
        _CODEX | {"model": "gpt-5.6-sol", "effort": "high"},
        {"mode": "agent", "agent": "ag_gemini", "name": "Gemini CLI"},
    ][:count]
    preset = _preset("a", rules={**_preset()["rules"], "review_merge": "separate"})
    preset["roles"]["reviewer"] = {"mode": "multi", "reviewers": slots}
    put = RolesPut.model_validate(_body(presets=[preset]))
    dumped = put.presets[0].roles.reviewer.model_dump()
    assert len(dumped["reviewers"]) == count
    assert put.presets[0].rules.review_merge == "separate"
    if count == 3:
        assert dumped["reviewers"][2] == {
            "mode": "agent",
            "agent": "ag_gemini",
            "name": "Gemini CLI",
            "agent_name": None,
            "model": None,
            "effort": None,
        }


@pytest.mark.parametrize(
    ("reviewers", "fragment"),
    [
        ([], "at least 1"),
        ([{"mode": "fixed", "model": "claude-sonnet-5-5", "effort": "low"}] * 4, "at most 3"),
        ([{"mode": "fixed", "model": "x", "effort": "low"}, {"mode": "auto"}], "reviewer 1"),
        ([{"mode": "session"}], "does not match any of the expected tags"),
        ([{"mode": "multi", "reviewers": []}], "does not match any of the expected tags"),
        ([{"mode": "agent", "name": "Codex"}], "agent"),
        ([{"mode": "agent", "agent": "x y", "name": "Codex"}], "pattern"),
        ([{"mode": "agent", "agent": "x", "name": " "}], "blank"),
        ([{"mode": "agent", "agent": "x", "name": "a\nb"}], "pattern"),
        ([{"mode": "agent", "agent": "x", "name": "C", "effort": "huge"}], "effort"),
        ([{"mode": "agent", "agent": "x", "name": "C", "extra": 1}], "extra"),
    ],
)
def test_reviewer_slots_are_checked(reviewers: list[Any], fragment: str) -> None:
    preset = _preset("a")
    preset["roles"]["reviewer"] = {"mode": "multi", "reviewers": reviewers}
    with pytest.raises(ValidationError) as caught:
        RolesPut.model_validate(_body(presets=[preset]))
    assert fragment in str(caught.value)


@pytest.mark.parametrize("role", ["planner", "builder", "helper", "lead"])
def test_only_the_reviewer_takes_several(role: str) -> None:
    preset = _preset("a")
    preset["roles"][role] = {"mode": "multi", "reviewers": [_CODEX]}
    with pytest.raises(ValidationError):
        RolesPut.model_validate(_body(presets=[preset]))


@pytest.mark.parametrize(
    "rules",
    [{"review_merge": "both"}, {"reviewer_unavailable": "skip"}, {"review_merge": None}],
)
def test_review_rules_are_checked(rules: dict[str, Any]) -> None:
    preset = _preset("a")
    preset["rules"] = {**preset["rules"], **rules}
    with pytest.raises(ValidationError):
        RolesPut.model_validate(_body(presets=[preset]))


def test_validate_custom_takes_several_reviewers() -> None:
    balanced = role_prefs.starters()[1]
    rules = {**balanced["rules"], "reviewer_unavailable": "ask"}
    checked = role_prefs.validate_custom({"roles": balanced["roles"], "rules": rules})
    assert checked["roles"]["reviewer"]["reviewers"][1]["name"] == "Codex"
    assert checked["rules"]["reviewer_unavailable"] == "ask"
    bad = {**balanced["roles"], "reviewer": {"mode": "multi", "reviewers": [_CODEX] * 4}}
    with pytest.raises(ValueError):
        role_prefs.validate_custom({"roles": bad, "rules": rules})


def test_a_user_who_saved_keeps_their_single_reviewer_balanced(tmp_path: Path) -> None:
    path = tmp_path / "roles.json"
    old = _preset("balanced", title="Balanced")
    old["roles"]["reviewer"] = {"mode": "fixed", "model": "claude-sonnet-5-5", "effort": "medium"}
    for key in _REVIEW_DEFAULTS:
        old["rules"].pop(key, None)
    entry = {
        "revision": 4,
        "default_preset": "balanced",
        "apply_start_on": True,
        "presets": [old],
        "starters_seen": [*_PINNED, *_NEW],
    }
    path.write_text(json.dumps({"version": 1, "users": {"reid": entry}}))
    state = RolePrefsStore(path).get("reid")
    assert state.revision == 4
    assert [p.id for p in state.presets] == ["balanced"]
    assert state.presets[0].roles.reviewer.model_dump()["mode"] == "fixed"


def test_a_never_saved_user_gets_two_reviewers_in_balanced() -> None:
    state = RolePrefsStore().get("someone")
    balanced = next(p for p in state.presets if p.id == "balanced")
    assert state.revision == 0
    assert [s.mode for s in balanced.roles.reviewer.reviewers] == ["fixed", "agent"]


def test_validate_custom_keeps_the_helpers_rule() -> None:
    starter = role_prefs.starters()[0]
    rules = {**starter["rules"], "helpers": "own"}
    checked = role_prefs.validate_custom({"roles": starter["roles"], "rules": rules})
    assert checked["rules"]["helpers"] == "own"


def test_validate_custom() -> None:
    starter = role_prefs.starters()[0]
    assert (
        role_prefs.validate_custom({"roles": starter["roles"], "rules": starter["rules"]})[
            "roles"
        ]["planner"]["model"]
        == "claude-sonnet-5-5"
    )
    with pytest.raises(ValueError):
        role_prefs.validate_custom({"roles": starter["roles"]})
    with pytest.raises(ValueError):
        role_prefs.validate_custom("nope")


# ── the store ───────────────────────────────────────────────────────────


def test_store_round_trips_through_the_file(tmp_path: Path) -> None:
    path = tmp_path / "state" / "roles.json"
    first = RolePrefsStore(path)
    body = RolesPut.model_validate(_body(apply_start_on=False))
    result = first.put("reid", body)
    assert result.conflict is False
    assert result.state.revision == 1
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    entry = raw["users"]["reid"]
    assert entry["revision"] == 1
    assert entry["updated_at"]
    assert entry["default_preset"] == "mine"
    assert entry["apply_start_on"] is False
    again = RolePrefsStore(path).get("reid")
    assert again.model_dump() == result.state.model_dump()
    assert RolePrefsStore(path).get("steve").revision == 0


def test_stale_revision_conflicts_with_the_current_state() -> None:
    store = RolePrefsStore(None)
    store.put("reid", RolesPut.model_validate(_body()))
    stale = store.put("reid", RolesPut.model_validate(_body(presets=[_preset("other")])))
    assert stale.conflict is True
    assert stale.state.revision == 1
    assert [p.id for p in stale.state.presets] == ["mine"]
    ok = store.put("reid", RolesPut.model_validate(_body(1, [_preset("other")])))
    assert ok.state.revision == 2


def test_a_first_put_must_be_based_on_revision_0() -> None:
    store = RolePrefsStore(None)
    assert store.put("reid", RolesPut.model_validate(_body(3))).conflict is True
    assert store.get("reid").revision == 0


def test_a_failed_save_rolls_back_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(role_prefs.tempfile, "mkstemp", refuse)
    store = RolePrefsStore(tmp_path / "roles.json")
    with pytest.raises(role_prefs.RolesSaveError):
        store.put("reid", RolesPut.model_validate(_body()))
    assert store.get("reid").revision == 0
    assert store.get("reid").presets[0].id == "save-tokens"


def test_a_corrupt_file_is_moved_aside(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "roles.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert RolePrefsStore(path).get("reid").revision == 0
    assert "moved it to" in caplog.text
    assert list(tmp_path.glob("roles.json.corrupt-*"))


def test_an_invalid_entry_serves_starters_at_its_revision(tmp_path: Path) -> None:
    path = tmp_path / "roles.json"
    bad = {"revision": 4, "default_preset": "gone", "apply_start_on": True, "presets": []}
    path.write_text(json.dumps({"version": 1, "users": {"reid": bad}}), encoding="utf-8")
    store = RolePrefsStore(path)
    state = store.get("reid")
    assert state.revision == 4
    assert state.default_preset == "balanced"
    # The bad entry stays in the file until the user saves on top of it.
    store.put("steve", RolesPut.model_validate(_body()))
    assert json.loads(path.read_text(encoding="utf-8"))["users"]["reid"] == bad
    assert store.put("reid", RolesPut.model_validate(_body(4))).state.revision == 5


def _old_entry(presets: list[dict[str, Any]], revision: int = 2) -> dict[str, Any]:
    """A user saved before ``starters_seen`` and the Solo starter existed."""
    for preset in presets:
        preset["rules"].pop("helpers", None)
    return {
        "revision": revision,
        "updated_at": "2026-10-22T00:00:00+00:00",
        "default_preset": presets[0]["id"],
        "apply_start_on": True,
        "presets": presets,
    }


def test_a_user_who_saved_before_solo_gets_it_once(tmp_path: Path) -> None:
    path = tmp_path / "roles.json"
    entry = _old_entry([_preset("mine"), _preset("balanced")])
    path.write_text(json.dumps({"version": 1, "users": {"reid": entry}}), encoding="utf-8")
    store = RolePrefsStore(path)
    state = store.get("reid")
    assert [p.id for p in state.presets] == ["mine", "balanced", "solo", *_NEW]
    assert state.revision == 2
    assert state.starters_seen == ["save-tokens", "balanced", "yolo", "solo", *_NEW]
    # No pins were saved, so what the user had is pinned and the new starters are not.
    assert [p.pinned for p in state.presets] == [True, True, True, False, False, False]
    # Reading never writes the file.
    assert "starters_seen" not in json.loads(path.read_text(encoding="utf-8"))["users"]["reid"]
    # Deleting it and saving records it as seen: it does not come back.
    seen = store.get("reid").starters_seen
    store.put("reid", RolesPut.model_validate(_body(2, [_preset("mine")], starters_seen=seen)))
    assert [p.id for p in store.get("reid").presets] == ["mine"]
    assert [p.id for p in RolePrefsStore(path).get("reid").presets] == ["mine"]
    saved = json.loads(path.read_text(encoding="utf-8"))["users"]["reid"]
    assert "solo" in saved["starters_seen"]


def test_a_saved_state_with_a_pin_keeps_its_pins_apart_from_the_append(tmp_path: Path) -> None:
    path = tmp_path / "roles.json"
    entry = _old_entry([_preset("mine", pinned=True), _preset("balanced")])
    path.write_text(json.dumps({"version": 1, "users": {"reid": entry}}), encoding="utf-8")
    state = RolePrefsStore(path).get("reid")
    assert [(p.id, p.pinned) for p in state.presets][:2] == [("mine", True), ("balanced", False)]
    assert [p.id for p in state.presets][2:] == ["solo", *_NEW]
    assert [p.pinned for p in state.presets if p.id in _NEW] == [False, False, False]


def test_a_state_that_has_seen_every_starter_is_never_repinned(tmp_path: Path) -> None:
    path = tmp_path / "roles.json"
    entry = _old_entry([_preset("mine", pinned=False), _preset("balanced", pinned=False)])
    entry["starters_seen"] = [*_PINNED, *_NEW]
    path.write_text(json.dumps({"version": 1, "users": {"reid": entry}}), encoding="utf-8")
    state = RolePrefsStore(path).get("reid")
    assert [(p.id, p.pinned) for p in state.presets] == [("mine", False), ("balanced", False)]


def test_a_stale_save_does_not_mark_solo_seen(tmp_path: Path) -> None:
    path = tmp_path / "roles.json"
    entry = _old_entry([_preset("mine"), _preset("balanced")])
    path.write_text(json.dumps({"version": 1, "users": {"reid": entry}}), encoding="utf-8")
    store = RolePrefsStore(path)
    # A client that read before Solo existed sends no starters_seen and no Solo.
    store.put("reid", RolesPut.model_validate(_body(2, [_preset("mine")])))
    saved = json.loads(path.read_text(encoding="utf-8"))["users"]["reid"]
    assert "solo" not in saved["starters_seen"]
    assert [p.id for p in store.get("reid").presets] == ["mine", "solo", *_NEW]


def test_a_user_who_already_has_a_solo_preset_gets_no_second_one() -> None:
    store = RolePrefsStore(None)
    store._loaded = True
    store._users["reid"] = _old_entry([_preset("solo", title="My solo")])
    store._states["reid"] = role_prefs.RolesState.model_validate(
        {k: v for k, v in store._users["reid"].items() if k != "updated_at"}
    )
    state = store.get("reid")
    assert [(p.id, p.title) for p in state.presets][:1] == [("solo", "My solo")]
    assert [p.id for p in state.presets].count("solo") == 1
    assert "solo" in (state.starters_seen or [])


def test_a_full_list_gets_the_new_starter_once_there_is_room() -> None:
    store = RolePrefsStore(None)
    store._loaded = True
    full = [_preset(f"p{n}") for n in range(role_prefs.MAX_PRESETS)]
    store._users["reid"] = _old_entry(full)
    store._states["reid"] = role_prefs.RolesState.model_validate(
        {k: v for k, v in store._users["reid"].items() if k != "updated_at"}
    )
    state = store.get("reid")
    assert len(state.presets) == role_prefs.MAX_PRESETS
    assert "solo" not in (state.starters_seen or [])
    # Saving the full list keeps it unoffered; freeing a slot offers it.
    body = _body(2, [p.model_dump() for p in state.presets])
    store.put("reid", RolesPut.model_validate(body))
    assert "solo" not in (store.get("reid").starters_seen or [])
    store.put("reid", RolesPut.model_validate(_body(3, [_preset("p0")])))
    # That save was based on a read with no room, so Solo is offered on the next read.
    assert [p.id for p in store.get("reid").presets] == ["p0", "solo", *_NEW]


def test_a_full_list_unpinned_by_hand_is_not_repinned_as_room_frees() -> None:
    store = RolePrefsStore(None)
    store._loaded = True
    # One slot free: only Solo fits on this read, so nothing pin-era is offered yet.
    almost = [_preset(f"p{n}") for n in range(role_prefs.MAX_PRESETS - 2)]
    store._users["reid"] = _old_entry(almost)
    store._states["reid"] = role_prefs.RolesState.model_validate(
        {k: v for k, v in store._users["reid"].items() if k != "updated_at"}
    )
    state = store.get("reid")
    assert [p.id for p in state.presets][-2:] == ["solo", "think-it-through"]
    assert all(p.pinned for p in state.presets[:-1])
    # The user unpins everything, saves, then frees a slot.
    unpinned = [{**p.model_dump(), "pinned": False} for p in state.presets[:-1]]
    body = _body(2, unpinned, starters_seen=state.starters_seen)
    store.put("reid", RolesPut.model_validate(body))
    state = store.get("reid")
    assert state.presets[-1].id == "deep-debug"
    assert not any(p.pinned for p in state.presets)


def test_a_full_list_is_not_pinned_while_no_starter_fits() -> None:
    store = RolePrefsStore(None)
    store._loaded = True
    store._users["reid"] = _old_entry([_preset(f"p{n}") for n in range(role_prefs.MAX_PRESETS)])
    store._states["reid"] = role_prefs.RolesState.model_validate(
        {k: v for k, v in store._users["reid"].items() if k != "updated_at"}
    )
    state = store.get("reid")
    assert not any(p.pinned for p in state.presets)
    assert not any(s in (state.starters_seen or []) for s in _NEW)


def test_default_path_follows_env_then_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert role_prefs._default_path() is None
    monkeypatch.setattr(role_prefs, "DATA_DIR", tmp_path)
    assert role_prefs._default_path() == tmp_path / "ae-roles.json"
    monkeypatch.setenv(role_prefs.STATE_FILE_ENV, str(tmp_path / "x.json"))
    assert role_prefs._default_path() == tmp_path / "x.json"


# ── the routes ──────────────────────────────────────────────────────────


async def test_routes_round_trip(client: httpx.AsyncClient) -> None:
    first = await client.get("/v1/ae/roles")
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["revision"] == 0
    assert data["default_preset"] == "balanced"
    assert data["apply_start_on"] is True
    assert [p["id"] for p in data["presets"]] == [*_PINNED, *_NEW]
    assert data["starters"] == role_prefs.starters()

    put = await client.put("/v1/ae/roles", json=_body())
    assert put.status_code == 200, put.text
    assert put.json()["revision"] == 1
    assert [p["id"] for p in put.json()["presets"]] == ["mine"]
    assert put.json()["starters"] == role_prefs.starters()
    assert (await client.get("/v1/ae/roles")).json()["default_preset"] == "mine"


async def test_stale_revision_is_a_409_with_the_current_state(client: httpx.AsyncClient) -> None:
    await client.put("/v1/ae/roles", json=_body())
    stale = await client.put("/v1/ae/roles", json=_body(presets=[_preset("other")]))
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "conflict"
    assert "another device" in error["message"]
    assert error["revision"] == 1
    assert [p["id"] for p in error["presets"]] == ["mine"]
    assert error["default_preset"] == "mine"


async def test_invalid_bodies_are_refused(client: httpx.AsyncClient) -> None:
    dup = await client.put("/v1/ae/roles", json=_body(presets=[_preset("a"), _preset("a")]))
    assert dup.status_code == 422
    missing = await client.put("/v1/ae/roles", json=_body(default_preset="nope"))
    assert missing.status_code == 422
    negative = await client.put("/v1/ae/roles", json=_body(-1))
    assert negative.status_code == 422
    assert (await client.get("/v1/ae/roles")).json()["revision"] == 0


async def test_a_failed_write_is_a_503(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(role_prefs.tempfile, "mkstemp", refuse)
    role_prefs.reset_store(RolePrefsStore(tmp_path / "roles.json"))
    failed = await client.put("/v1/ae/roles", json=_body())
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "roles_not_saved"
    assert (await client.get("/v1/ae/roles")).json()["revision"] == 0


async def test_put_is_json_only_and_origin_checked(client: httpx.AsyncClient) -> None:
    form = await client.put(
        "/v1/ae/roles",
        content="revision=0",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert form.status_code == 415
    foreign = await client.put(
        "/v1/ae/roles", json=_body(), headers={"Origin": "https://evil.example"}
    )
    assert foreign.status_code == 403
    assert (await client.get("/v1/ae/roles")).json()["revision"] == 0


async def test_routes_need_an_identity_off_single_user(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    assert (await client.get("/v1/ae/roles")).status_code == 401
    assert (await client.put("/v1/ae/roles", json=_body())).status_code == 401


async def test_two_users_are_kept_apart(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    reid = {"X-Forwarded-Email": "reid@example.com"}
    steve = {"X-Forwarded-Email": "steve@example.com"}
    assert (await client.put("/v1/ae/roles", json=_body(), headers=reid)).status_code == 200
    assert (await client.get("/v1/ae/roles", headers=steve)).json()["revision"] == 0
    assert (await client.get("/v1/ae/roles", headers=reid)).json()["default_preset"] == "mine"


def test_default_review_rules_are_not_written() -> None:
    preset = _preset("a")
    put = RolesPut.model_validate(_body(presets=[preset]))
    rules = put.presets[0].rules.model_dump()
    assert "review_merge" not in rules
    assert "reviewer_unavailable" not in rules
    changed = _preset("a", rules={**_preset()["rules"], "review_merge": "separate"})
    dumped = RolesPut.model_validate(_body(presets=[changed])).presets[0].rules.model_dump()
    assert dumped["review_merge"] == "separate"
    assert "reviewer_unavailable" not in dumped


def test_resaving_an_untouched_preset_writes_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "roles.json"
    store = RolePrefsStore(path)
    first = store.put("u", RolesPut.model_validate(_body(presets=[_preset("a")])))
    before = json.loads(path.read_text())["users"]["u"]["presets"]
    again = RolesPut.model_validate(
        _body(
            presets=[p.model_dump() for p in first.state.presets],
            revision=first.state.revision,
        )
    )
    store.put("u", again)
    assert json.loads(path.read_text())["users"]["u"]["presets"] == before


def test_agent_slots_carry_a_stable_name_the_readers_resolve_by() -> None:
    codex = role_prefs.starters()[1]["roles"]["reviewer"]["reviewers"][1]
    assert codex["agent_name"] == role_prefs.CODEX_AGENT_NAME == "codex-native-ui"
    preset = _preset("a")
    preset["roles"]["reviewer"] = {"mode": "multi", "reviewers": [_CODEX | {"agent_name": "x y"}]}
    with pytest.raises(ValidationError):
        RolesPut.model_validate(_body(presets=[preset]))


@pytest.mark.parametrize("role", ["planner", "builder", "helper"])
def test_workers_take_a_codex_slot_but_no_other_agent(role: str) -> None:
    preset = _preset("a")
    preset["roles"][role] = _CODEX | {"model": "gpt-5.6-sol", "effort": "high"}
    RolesPut.model_validate(_body(presets=[preset]))
    preset["roles"][role] = _CODEX | {"agent": "ag_gemini", "agent_name": None, "name": "Gemini"}
    with pytest.raises(ValidationError, match="Claude or Codex"):
        RolesPut.model_validate(_body(presets=[preset]))
    preset["roles"]["lead"] = _CODEX
    preset["roles"][role] = {"mode": "auto"}
    with pytest.raises(ValidationError):
        RolesPut.model_validate(_body(presets=[preset]))


def test_session_mode_is_not_a_reviewer_slot() -> None:
    preset = _preset("a")
    preset["roles"]["reviewer"] = {"mode": "multi", "reviewers": [{"mode": "session"}]}
    with pytest.raises(ValidationError):
        RolesPut.model_validate(_body(presets=[preset]))
    preset["roles"]["reviewer"] = {"mode": "session"}
    with pytest.raises(ValidationError):
        RolesPut.model_validate(_body(presets=[preset]))


def test_insights_is_optional_and_absent_from_the_stored_json(tmp_path: Path) -> None:
    path = tmp_path / "roles.json"
    store = RolePrefsStore(path)
    result = store.put("u", RolesPut.model_validate(_body()))
    assert result.state.insights is None
    assert "insights" not in json.loads(path.read_text())["users"]["u"]
    assert "insights" not in role_prefs.view(result.state)


def test_insights_round_trip_absent_keeps_and_null_clears(tmp_path: Path) -> None:
    path = tmp_path / "roles.json"
    store = RolePrefsStore(path)
    insights = {
        "enabled": True,
        "weeks": 4,
        "scope": "own",
        "show": "both",
        "frequency": "biweekly",
    }
    first = store.put("u", RolesPut.model_validate(_body(insights=insights)))
    assert json.loads(path.read_text())["users"]["u"]["insights"] == insights
    assert RolePrefsStore(path).get("u").insights.enabled is True
    assert role_prefs.view(first.state)["insights"] == insights
    second = store.put("u", RolesPut.model_validate(_body(revision=1)))
    assert second.state.revision == 2 and second.state.insights.enabled is True
    third = store.put("u", RolesPut.model_validate(_body(revision=2, insights=None)))
    assert third.state.revision == 3 and third.state.insights is None
    assert "insights" not in json.loads(path.read_text())["users"]["u"]


def test_insights_show_defaults_to_both_and_validates(tmp_path: Path) -> None:
    old = {"enabled": True, "weeks": 4, "scope": "own"}
    store = RolePrefsStore(tmp_path / "roles.json")
    state = store.put("u", RolesPut.model_validate(_body(insights=old))).state
    assert state.insights.show == "both"
    for show in ("cost", "tokens", "both"):
        parsed = RolesPut.model_validate(_body(insights={**old, "show": show}))
        assert parsed.insights.show == show
    with pytest.raises(ValidationError):
        RolesPut.model_validate(_body(insights={**old, "show": "dollars"}))


def test_insights_show_survives_a_put_that_omits_insights(tmp_path: Path) -> None:
    store = RolePrefsStore(tmp_path / "roles.json")
    saved = {"enabled": False, "weeks": 2, "scope": "all", "show": "tokens"}
    store.put("u", RolesPut.model_validate(_body(insights=saved)))
    again = store.put("u", RolesPut.model_validate(_body(revision=1)))
    assert again.state.insights.show == "tokens"
    assert store.get("u").insights.show == "tokens"


@pytest.mark.parametrize("bad", [None, 3, ""])
def test_insights_show_rejects_non_literals(bad: Any) -> None:
    with pytest.raises(ValidationError):
        RolesPut.model_validate(
            _body(insights={"enabled": True, "weeks": 4, "scope": "own", "show": bad})
        )


@pytest.mark.parametrize(
    "bad",
    [
        {"enabled": "yes", "weeks": 4, "scope": "own"},
        {"enabled": True, "weeks": 0, "scope": "own"},
        {"enabled": True, "weeks": 4, "scope": "team"},
        {"enabled": True, "weeks": 4, "scope": "own", "extra": 1},
    ],
)
def test_insights_rejects_bad_values(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RolesPut.model_validate(_body(insights=bad))


def test_all_states_lists_valid_saved_users_with_new_starters(tmp_path: Path) -> None:
    path = tmp_path / "roles.json"
    store = RolePrefsStore(path)
    assert store.all_states() == {}
    store.put("ann", RolesPut.model_validate(_body(0, [_preset("mine")])))
    store.put("bob", RolesPut.model_validate(_body(0, [_preset("other")])))
    # A user whose saved entry no longer validates is left out.
    raw = json.loads(path.read_text())
    raw["users"]["bad"] = {"revision": 3, "default_preset": "nope", "presets": []}
    path.write_text(json.dumps(raw))
    states = RolePrefsStore(path).all_states()
    assert sorted(states) == ["ann", "bob"]
    ids = [p.id for p in states["ann"].presets]
    assert "mine" in ids
    assert states["ann"].revision == 1
    # Copies: changing one does not change the store.
    states["ann"].presets.clear()
    assert RolePrefsStore(path).all_states()["ann"].presets


def test_insights_frequency_defaults_to_biweekly_round_trips_and_validates(
    tmp_path: Path,
) -> None:
    old = {"enabled": True, "weeks": 4, "scope": "own", "show": "both"}
    store = RolePrefsStore(tmp_path / "roles.json")
    state = store.put("u", RolesPut.model_validate(_body(insights=old))).state
    assert state.insights.frequency == "biweekly"
    for frequency in ("weekly", "biweekly", "monthly"):
        parsed = RolesPut.model_validate(_body(insights={**old, "frequency": frequency}))
        assert parsed.insights.frequency == frequency
    saved = store.put(
        "u", RolesPut.model_validate(_body(revision=1, insights={**old, "frequency": "monthly"}))
    ).state
    assert RolePrefsStore(tmp_path / "roles.json").get("u").insights.frequency == "monthly"
    assert role_prefs.view(saved)["insights"]["frequency"] == "monthly"
    with pytest.raises(ValidationError):
        RolesPut.model_validate(_body(insights={**old, "frequency": "daily"}))


def test_insights_frequency_null_reads_as_the_default_and_bad_values_are_refused() -> None:
    old = {"enabled": True, "weeks": 2, "scope": "all", "show": "both"}
    parsed = RolesPut.model_validate(_body(insights={**old, "frequency": None}))
    assert parsed.insights.frequency == "biweekly"
    for bad in ("daily", 7, ["weekly"], True):
        with pytest.raises(ValidationError, match="weekly"):
            RolesPut.model_validate(_body(insights={**old, "frequency": bad}))


def test_ultra_is_stored_for_an_agent_slot_only() -> None:
    preset = _preset("a")
    preset["roles"]["reviewer"] = {
        "mode": "multi",
        "reviewers": [_CODEX | {"model": "gpt-6-sol", "effort": "ultra"}],
    }
    put = RolesPut.model_validate(_body(presets=[preset]))
    assert put.presets[0].roles.reviewer.model_dump()["reviewers"][0]["effort"] == "ultra"
    for slot in (
        {"mode": "fixed", "model": "claude-sonnet-5-5", "effort": "ultra"},
        {"mode": "multi", "reviewers": [{"mode": "fixed", "model": "x", "effort": "ultra"}]},
    ):
        bad = _preset("a")
        bad["roles"]["reviewer"] = slot
        with pytest.raises(ValidationError):
            RolesPut.model_validate(_body(presets=[bad]))
    planner = _preset("a")
    planner["roles"]["planner"] = {"mode": "fixed", "model": "claude-opus-5-5", "effort": "ultra"}
    with pytest.raises(ValidationError):
        RolesPut.model_validate(_body(presets=[planner]))
