"""Preset suggestions: the path allowlist, validation, the state machine and the file."""

from __future__ import annotations

import copy
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ae_omni_policies import preset_suggestions as ps
from ae_omni_policies import role_prefs
from ae_omni_policies.role_prefs import RolePrefsStore, RolesPut

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
SONNET_HIGH = {"mode": "fixed", "model": role_prefs.SONNET, "effort": "high"}


def _preset(id: str = "mine") -> dict[str, Any]:
    raw = copy.deepcopy(role_prefs.starters()[1])
    raw.update(id=id, title=id.title(), pinned=False)
    return raw


@pytest.fixture
def roles(tmp_path: Path) -> RolePrefsStore:
    store = RolePrefsStore(tmp_path / "roles.json")
    body = {
        "revision": 0,
        "default_preset": "mine",
        "apply_start_on": True,
        "presets": [_preset("mine"), _preset("other")],
    }
    store.put("ann", RolesPut.model_validate(body))
    store.put(
        "bob",
        RolesPut.model_validate(body | {"presets": [_preset("bobs")], "default_preset": "bobs"}),
    )
    return store


@pytest.fixture
def sugs(tmp_path: Path) -> ps.SuggestionStore:
    return ps.SuggestionStore(tmp_path / "sugs.json")


def _own(**kw: Any) -> dict[str, Any]:
    item = {
        "kind": "own",
        "preset_id": "mine",
        "title": "Plan on Sonnet",
        "why": "12 runs, median $9 against $14.",
        "saving": "about $5 per run",
        "changes": [{"path": "roles.planner", "value": SONNET_HIGH}],
    }
    return item | kw


def _body(*items: dict[str, Any], summary: str = "Two presets checked.") -> ps.SubmitBody:
    return ps.SubmitBody.model_validate({"summary": summary, "suggestions": list(items)})


def _submit(
    sugs: ps.SuggestionStore,
    roles: RolePrefsStore,
    *items: dict[str, Any],
    session: str = "conv_rev",
    owner: str = "ann",
    admin: bool = False,
) -> dict[str, Any]:
    return sugs.submit(
        session=session,
        owner=owner,
        admin=admin,
        body=_body(*items),
        roles_for=roles.get,
        now=NOW,
    )


# -- paths and apply_changes


@pytest.mark.parametrize(
    "path",
    [
        "roles.lead",
        "roles.planner",
        "roles.reviewer",
        "roles.reviewer.reviewers.0",
        "roles.reviewer.reviewers.2",
        "rules.raise_or_lower",
        "rules.reviewer_unavailable",
    ],
)
def test_allowed_paths(path: str) -> None:
    assert ps.PATH_PATTERN.match(path)


@pytest.mark.parametrize(
    "path",
    ["title", "id", "roles", "roles.other", "roles.reviewer.reviewers.3", "rules.nope", "rules."],
)
def test_other_paths_are_refused(path: str) -> None:
    with pytest.raises(ValidationError):
        ps.Change(path=path, value=1)


def test_apply_changes_sets_validates_and_leaves_the_input_alone() -> None:
    before = _preset("mine")
    keep = copy.deepcopy(before)
    after = ps.apply_changes(
        before,
        [
            {"path": "roles.planner", "value": SONNET_HIGH},
            {"path": "rules.auto_raises", "value": 2},
        ],
    )
    assert after.roles.planner.model == role_prefs.SONNET and after.rules.auto_raises == 2
    assert before == keep
    with pytest.raises(ValueError):
        ps.apply_changes(before, [{"path": "rules.auto_raises", "value": 9}])
    with pytest.raises(ValueError):
        ps.apply_changes(before, [{"path": "roles.planner", "value": {"mode": "bogus"}}])
    with pytest.raises(ValueError):
        ps.apply_changes(before, [{"path": "title", "value": "x"}])


def test_a_reviewer_slot_path_needs_that_slot() -> None:
    before = _preset("mine")  # balanced: two reviewers
    after = ps.apply_changes(
        before,
        [
            {
                "path": "roles.reviewer.reviewers.0",
                "value": {"mode": "fixed", "model": role_prefs.OPUS, "effort": "low"},
            }
        ],
    )
    assert after.roles.reviewer.reviewers[0].model == role_prefs.OPUS
    with pytest.raises(ValueError, match="no reviewer 3"):
        ps.apply_changes(before, [{"path": "roles.reviewer.reviewers.2", "value": SONNET_HIGH}])
    single = _preset("one")
    single["roles"]["reviewer"] = SONNET_HIGH
    with pytest.raises(ValueError):
        ps.apply_changes(single, [{"path": "roles.reviewer.reviewers.0", "value": SONNET_HIGH}])


# -- item validation


@pytest.mark.parametrize(
    "item",
    [
        _own(preset_id=None),
        _own(saving=None),
        _own(changes=[]),
        {"kind": "no_change", "title": "t", "why": "w"},
        {
            "kind": "no_change",
            "preset_id": "mine",
            "title": "t",
            "why": "w",
            "changes": [{"path": "rules.helpers", "value": "own"}],
        },
        {"kind": "starter", "title": "t", "why": "w"},
        _own(extra=1),
        _own(title=""),
        _own(changes=[{"path": "rules.helpers", "value": "own"}] * 7),
    ],
)
def test_bad_items_fail_validation(item: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _body(item)


def test_at_most_five_suggestions_and_a_summary() -> None:
    nc = {"kind": "no_change", "preset_id": "mine", "title": "t", "why": "w"}
    assert len(_body(*[nc] * 5).suggestions) == 5
    with pytest.raises(ValidationError):
        _body(*[nc] * 6)
    with pytest.raises(ValidationError):
        _body(nc, summary="")


# -- submit


def test_submit_stores_a_review_and_records(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    nc = {"kind": "no_change", "preset_id": "other", "title": "Fine", "why": "On par."}
    starter = {"kind": "starter", "title": "Raise timeout", "why": "w", "saving": "20 min per run"}
    review = _submit(sugs, roles, _own(), nc, starter)
    assert review["count"] == 3 and review["owner"] == "ann" and review["admin"] is False
    listed, reviews = sugs.list_for("ann")
    assert [r["id"] for r in reviews] == [review["id"]]
    by_kind = {r["kind"]: r for r in listed}
    own = by_kind["own"]
    assert own["status"] == "open" and own["review_session"] == "conv_rev"
    assert own["preset_title"] == "Mine" and own["before"]["id"] == "mine" and own["after"] is None
    assert by_kind["no_change"]["before"] is None
    assert by_kind["starter"]["preset_title"] is None
    assert sugs.last_review("ann")["id"] == review["id"]
    assert sugs.last_review("bob") is None
    assert sugs.open_count("ann") == 2  # no_change does not count


def test_a_second_submit_from_the_same_session_is_refused(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    _submit(sugs, roles, _own())
    with pytest.raises(ps.SuggestionError) as err:
        _submit(sugs, roles, _own())
    assert (err.value.status, err.value.code) == (409, "already_submitted")
    assert _submit(sugs, roles, _own(), session="conv_other")["count"] == 1


def test_owner_rule_and_missing_presets_name_the_item(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    with pytest.raises(ps.SuggestionError, match=r"suggestions\[1\].*owner") as err:
        _submit(sugs, roles, _own(), _own(owner="bob", preset_id="bobs"))
    assert err.value.status == 422
    with pytest.raises(ps.SuggestionError, match=r"suggestions\[0\].*no preset"):
        _submit(sugs, roles, _own(preset_id="nope"))
    with pytest.raises(ps.SuggestionError, match=r"suggestions\[0\].*valid preset"):
        _submit(sugs, roles, _own(changes=[{"path": "rules.auto_raises", "value": 9}]))
    with pytest.raises(ps.SuggestionError, match="leave the preset as it is"):
        same = _preset("mine")["roles"]["planner"]
        _submit(sugs, roles, _own(changes=[{"path": "roles.planner", "value": same}]))
    # Nothing from a refused batch was stored.
    assert sugs.list_for(None) == ([], [])


def test_an_admin_review_may_name_another_user(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    _submit(sugs, roles, _own(owner="bob", preset_id="bobs"), admin=True)
    (record,), _ = sugs.list_for("bob")
    assert record["owner"] == "bob" and record["before"]["id"] == "bobs"


# -- state machine


def _applied(sugs: ps.SuggestionStore, roles: RolePrefsStore) -> dict[str, Any]:
    _submit(sugs, roles, _own())
    (record,), _ = sugs.list_for("ann")
    return record


def test_apply_writes_the_preset_and_undo_restores_it(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    record = _applied(sugs, roles)
    done, revision = sugs.apply("ann", record["id"], roles=roles, now=NOW)
    assert revision == 2 and done["status"] == "applied" and done["applied_revision"] == 2
    state = roles.get("ann")
    mine = next(p for p in state.presets if p.id == "mine")
    assert mine.roles.planner.model == role_prefs.SONNET
    assert next(p.id for p in state.presets if p.id != "mine") == "other"
    assert ps.public(done, "ann")["can_undo"] is True
    assert "before" not in ps.public(done, "ann")
    undone, revision = sugs.undo("ann", record["id"], roles=roles, now=NOW)
    assert undone["status"] == "undone" and revision == 3
    back = next(p for p in roles.get("ann").presets if p.id == "mine")
    assert back.model_dump(mode="json") == record["before"]
    # An undone suggestion can be applied again.
    again, _ = sugs.apply("ann", record["id"], roles=roles, now=NOW)
    assert again["status"] == "applied"


def test_apply_refuses_a_preset_that_changed_or_went(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    record = _applied(sugs, roles)
    state = roles.get("ann")
    edited = [
        p.model_copy(update={"description": "edited"}) if p.id == "mine" else p
        for p in state.presets
    ]
    roles.put("ann", RolesPut(**{**state.model_dump(), "presets": edited}))
    with pytest.raises(ps.SuggestionError, match="changed since the review on 2026-10-24") as err:
        sugs.apply("ann", record["id"], roles=roles)
    assert (err.value.status, err.value.code) == (409, "preset_changed")
    state = roles.get("ann")
    gone = [p for p in state.presets if p.id != "mine"]
    roles.put(
        "ann",
        RolesPut(**{**state.model_dump(), "presets": gone, "default_preset": gone[0].id}),
    )
    with pytest.raises(ps.SuggestionError) as err:
        sugs.apply("ann", record["id"], roles=roles)
    assert err.value.code == "preset_gone"


def test_undo_refuses_when_the_preset_is_no_longer_what_apply_wrote(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    record = _applied(sugs, roles)
    sugs.apply("ann", record["id"], roles=roles)
    state = roles.get("ann")
    edited = [
        p.model_copy(update={"description": "later"}) if p.id == "mine" else p
        for p in state.presets
    ]
    roles.put("ann", RolesPut(**{**state.model_dump(), "presets": edited}))
    with pytest.raises(ps.SuggestionError) as err:
        sugs.undo("ann", record["id"], roles=roles)
    assert (err.value.status, err.value.code) == (409, "preset_changed")


def test_apply_keeps_the_insights_setting(sugs: ps.SuggestionStore, roles: RolePrefsStore) -> None:
    state = roles.get("ann")
    roles.put(
        "ann",
        RolesPut(
            **{
                **state.model_dump(),
                "insights": {"enabled": True, "weeks": 3, "scope": "own"},
            }
        ),
    )
    record = _applied(sugs, roles)
    sugs.apply("ann", record["id"], roles=roles)
    insights = roles.get("ann").insights
    assert insights is not None and (insights.enabled, insights.weeks) == (True, 3)


def test_who_may_act(sugs: ps.SuggestionStore, roles: RolePrefsStore) -> None:
    record = _applied(sugs, roles)
    sid = record["id"]
    with pytest.raises(ps.SuggestionError) as err:
        sugs.apply("bob", sid, roles=roles)
    assert (err.value.status, err.value.code) == (403, "not_owner")
    with pytest.raises(ps.SuggestionError) as err:
        sugs.dismiss("bob", sid, False)
    assert err.value.code == "not_owner"
    with pytest.raises(ps.SuggestionError) as err:
        sugs.apply("ann", "sg_missing", roles=roles)
    assert err.value.status == 404
    with pytest.raises(ps.SuggestionError) as err:
        sugs.undo("ann", sid, roles=roles)  # still open
    assert err.value.code == "not_applicable"
    assert sugs.dismiss("bob", sid, True)["status"] == "dismissed"  # an admin may
    with pytest.raises(ps.SuggestionError):
        sugs.dismiss("ann", sid, False)  # no longer open
    with pytest.raises(ps.SuggestionError):
        sugs.apply("ann", sid, roles=roles)


def test_mark_issue_only_for_an_open_starter(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    starter = {"kind": "starter", "title": "T", "why": "w", "saving": "s"}
    _submit(sugs, roles, starter, _own())
    listed, _ = sugs.list_for("ann")
    s = next(r for r in listed if r["kind"] == "starter")
    o = next(r for r in listed if r["kind"] == "own")
    with pytest.raises(ps.SuggestionError):
        sugs.mark_issue(o["id"], "https://github.com/a/b/issues/1")
    done = sugs.mark_issue(s["id"], "https://github.com/a/b/issues/1")
    assert done["status"] == "issue_opened" and done["issue_url"].endswith("/1")
    with pytest.raises(ps.SuggestionError):
        sugs.mark_issue(s["id"], "https://github.com/a/b/issues/2")
    pub = ps.public(done, "ann")
    assert pub["can_apply"] is False and pub["can_undo"] is False
    assert ps.public(o, "bob")["can_apply"] is False
    assert ps.public(o, "ann")["can_apply"] is True


# -- the run-now flag


def test_the_forced_flag_is_consumed_and_expires(sugs: ps.SuggestionStore) -> None:
    sugs.force("ann", 1000.0)
    assert sugs.peek_forced("ann", 1100.0) is True
    assert sugs.take_forced("ann", 1100.0) is True
    assert sugs.take_forced("ann", 1100.0) is False
    sugs.force("ann", 1000.0)
    assert sugs.take_forced("ann", 1000.0 + ps.FORCED_MAX_AGE_S + 1) is False
    assert sugs.peek_forced("ann", 1000.0) is False  # consumed even when stale


# -- caps and the file


def test_caps_drop_the_oldest_non_open_first(
    sugs: ps.SuggestionStore, roles: RolePrefsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ps, "MAX_SUGGESTIONS", 4)
    monkeypatch.setattr(ps, "MAX_REVIEWS", 2)
    nc = {"kind": "no_change", "preset_id": "mine", "title": "t", "why": "w"}
    _submit(sugs, roles, nc, nc, session="s1")
    first, _ = sugs.list_for("ann")
    sugs.dismiss("ann", first[1]["id"], False)  # the older of the two, now non-open
    keep_open = first[0]["id"]
    _submit(sugs, roles, nc, nc, session="s2")
    _submit(sugs, roles, nc, session="s3")
    listed, reviews = sugs.list_for("ann")
    assert len(listed) == 4 and len(reviews) == 2
    ids = {r["id"] for r in listed}
    assert first[1]["id"] not in ids and keep_open in ids  # open ones outlive dismissed


def test_the_file_round_trips_and_a_corrupt_one_is_moved_aside(
    tmp_path: Path, roles: RolePrefsStore, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state" / "sugs.json"
    store = ps.SuggestionStore(path)
    _submit(store, roles, _own())
    store.force("ann", 5.0)
    again = ps.SuggestionStore(path)
    assert again.list_for("ann")[0][0]["title"] == "Plan on Sonnet"
    assert again.peek_forced("ann", 6.0)
    path.write_text("{not json")
    caplog.set_level(logging.WARNING)
    fresh = ps.SuggestionStore(path)
    assert fresh.list_for(None) == ([], [])
    assert list(path.parent.glob("sugs.json.corrupt-*"))
    path.write_text(json.dumps({"reviews": "x"}))
    assert ps.SuggestionStore(path).list_for(None) == ([], [])


def test_a_failed_write_changes_nothing_and_says_so(
    tmp_path: Path, roles: RolePrefsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ps.SuggestionStore(tmp_path / "sugs.json")
    monkeypatch.setattr(store, "_save", lambda: False)
    with pytest.raises(ps.SuggestionError) as err:
        _submit(store, roles, _own())
    assert err.value.code == "suggestions_not_saved"
    assert store.list_for(None) == ([], [])


def test_the_default_store_follows_the_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ps, "DATA_DIR", tmp_path / "nodata")
    monkeypatch.delenv(ps.STATE_FILE_ENV, raising=False)
    ps.reset_store()
    assert ps._default_path() is None
    monkeypatch.setenv(ps.STATE_FILE_ENV, str(tmp_path / "x.json"))
    assert ps._default_path() == tmp_path / "x.json"
    ps.reset_store()


# -- review round 1 fixes (2026-10-24)


def _fail_nth_save(store: ps.SuggestionStore, monkeypatch: pytest.MonkeyPatch, nth: int) -> None:
    real, calls = store._save, {"n": 0}

    def save() -> bool:
        calls["n"] += 1
        return False if calls["n"] == nth else real()

    monkeypatch.setattr(store, "_save", save)


def test_apply_that_loses_its_final_write_is_settled_by_the_next_try(
    sugs: ps.SuggestionStore, roles: RolePrefsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _applied(sugs, roles)
    _fail_nth_save(sugs, monkeypatch, 2)  # the first write (applying) lands, the second fails
    with pytest.raises(ps.SuggestionError) as err:
        sugs.apply("ann", record["id"], roles=roles, now=NOW)
    assert err.value.code == "suggestions_not_saved"
    assert sugs.get(record["id"])["status"] == "applying"
    mine = next(p for p in roles.get("ann").presets if p.id == "mine")
    assert mine.roles.planner.model == role_prefs.SONNET  # the preset did change
    settled, _ = sugs.apply("ann", record["id"], roles=roles, now=NOW)
    assert settled["status"] == "applied"
    assert roles.get("ann").revision == 2  # nothing was applied twice


def test_undo_that_loses_its_final_write_is_settled_by_the_next_try(
    sugs: ps.SuggestionStore, roles: RolePrefsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _applied(sugs, roles)
    sugs.apply("ann", record["id"], roles=roles, now=NOW)
    _fail_nth_save(sugs, monkeypatch, 2)
    with pytest.raises(ps.SuggestionError):
        sugs.undo("ann", record["id"], roles=roles, now=NOW)
    assert sugs.get(record["id"])["status"] == "undoing"
    settled, _ = sugs.undo("ann", record["id"], roles=roles, now=NOW)
    assert settled["status"] == "undone"
    back = next(p for p in roles.get("ann").presets if p.id == "mine")
    assert back.model_dump(mode="json") == record["before"]


def test_a_failed_swap_puts_the_status_back(
    sugs: ps.SuggestionStore, roles: RolePrefsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _applied(sugs, roles)

    def broken(*_a: Any, **_k: Any) -> Any:
        raise role_prefs.RolesSaveError("disk full")

    monkeypatch.setattr(roles, "put", broken)
    with pytest.raises(ps.SuggestionError) as err:
        sugs.apply("ann", record["id"], roles=roles, now=NOW)
    assert err.value.status == 503
    assert sugs.get(record["id"])["status"] == "open"
    monkeypatch.undo()
    sugs.apply("ann", record["id"], roles=roles, now=NOW)
    monkeypatch.setattr(roles, "put", broken)
    with pytest.raises(ps.SuggestionError):
        sugs.undo("ann", record["id"], roles=roles, now=NOW)
    assert sugs.get(record["id"])["status"] == "applied"


def test_a_stuck_apply_whose_preset_never_changed_rolls_back(
    sugs: ps.SuggestionStore, roles: RolePrefsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _applied(sugs, roles)
    stored = sugs._data["suggestions"][0]
    stored.update(status="applying", prev_status="open", after={"x": 1})
    done, _ = sugs.apply("ann", record["id"], roles=roles, now=NOW)
    assert done["status"] == "applied"  # rolled back to open, then applied for real


def test_reserve_issue_blocks_a_second_click_and_release_reopens(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    _submit(sugs, roles, {"kind": "starter", "title": "T", "why": "w", "saving": "s"})
    (record,), _ = sugs.list_for("ann")
    sugs.reserve_issue(record["id"], now=NOW)
    with pytest.raises(ps.SuggestionError) as err:
        sugs.reserve_issue(record["id"], now=NOW)
    assert (err.value.status, err.value.code) == (409, "issue_in_progress")
    sugs.release_issue(record["id"], now=NOW)
    assert sugs.get(record["id"])["status"] == "open"
    sugs.reserve_issue(record["id"], now=NOW)
    assert sugs.mark_issue(record["id"], "https://x/1", now=NOW)["status"] == "issue_opened"


def test_dismiss_takes_open_and_undone(sugs: ps.SuggestionStore, roles: RolePrefsStore) -> None:
    record = _applied(sugs, roles)
    sugs.apply("ann", record["id"], roles=roles)
    sugs.undo("ann", record["id"], roles=roles)
    assert sugs.dismiss("ann", record["id"], False)["status"] == "dismissed"


def test_a_value_over_2000_bytes_is_refused_by_name() -> None:
    with pytest.raises(ValidationError, match=r"roles\.lead"):
        ps.Change(path="roles.lead", value={"x": "y" * 2100})


def test_changes_text_reads_a_role_row_and_other_paths() -> None:
    record = {
        "kind": "own",
        "before": _preset("mine"),
        "after": None,
        "changes": [
            {"path": "roles.planner", "value": SONNET_HIGH},
            {"path": "rules.auto_raises", "value": 1},
        ],
    }
    text = ps.changes_text(record)
    first, second = text.splitlines()
    assert first.startswith("Planner: ") and f"{role_prefs.SONNET} high" in first
    assert " to " in first and second.startswith("rules.auto_raises: ")


def test_an_own_suggestion_on_an_unchanged_starter_copy_is_refused(
    sugs: ps.SuggestionStore, tmp_path: Path
) -> None:
    store = RolePrefsStore(tmp_path / "r2.json")
    starter = role_prefs.starters()[1]
    body = {
        "revision": 0,
        "default_preset": starter["id"],
        "apply_start_on": True,
        "presets": [starter],
    }
    store.put("ann", RolesPut.model_validate(body))
    with pytest.raises(ps.SuggestionError, match="not in the read's presets"):
        _submit(sugs, store, _own(preset_id=starter["id"]))


def test_trim_never_drops_the_newest_review_per_owner(sugs: ps.SuggestionStore) -> None:
    sugs._data["reviews"] = [{"id": "rv_ann", "owner": "ann"}] + [
        {"id": f"rv_{i}", "owner": "bob"} for i in range(ps.MAX_REVIEWS + 3)
    ]
    sugs._trim()
    ids = [r["id"] for r in sugs._data["reviews"]]
    assert "rv_ann" in ids and f"rv_{ps.MAX_REVIEWS + 2}" in ids
    assert len(ids) == ps.MAX_REVIEWS


# -- round 2 fixes


def _starter_item() -> dict[str, Any]:
    return {"kind": "starter", "title": "Raise it", "why": "w", "saving": "20 min"}


def test_a_stale_issue_reservation_counts_as_free(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    _submit(sugs, roles, _starter_item())
    (record,), _ = sugs.list_for("ann")
    sid = record["id"]
    sugs.reserve_issue(sid, now=NOW)
    with pytest.raises(ps.SuggestionError) as fresh:
        sugs.reserve_issue(sid, now=NOW + timedelta(seconds=60))
    assert fresh.value.code == "issue_in_progress"
    again = sugs.reserve_issue(sid, now=NOW + timedelta(seconds=ps.RESERVATION_TTL_S + 1))
    assert again["status"] == "issue_opening"
    assert again["reserved_at"].startswith("2026-10-24T12:02")
    # A hold with no usable stamp (an older file) is free too.
    sugs.get(sid)
    sugs._find(sid).pop("reserved_at")
    assert sugs.reserve_issue(sid, now=NOW)["status"] == "issue_opening"


def _record(sid: str, status: str, kind: str = "own") -> dict[str, Any]:
    return {"id": sid, "kind": kind, "status": status}


def test_trim_drops_only_finished_records_oldest_first(sugs: ps.SuggestionStore) -> None:
    keep = [
        _record("a_applied", "applied"),
        _record("a_applying", "applying"),
        _record("a_undoing", "undoing"),
        _record("a_opening", "issue_opening", "starter"),
        _record("a_open", "open"),
        _record("a_open_starter", "open", "starter"),
    ]
    gone = [
        _record("g_dismissed", "dismissed"),
        _record("g_issue", "issue_opened", "starter"),
        _record("g_undone", "undone"),
        _record("g_nochange", "open", "no_change"),
    ]
    sugs._data["suggestions"] = [gone[0], *keep[:3], gone[1], *keep[3:], gone[2], gone[3]]
    ps.MAX_SUGGESTIONS, saved = 7, ps.MAX_SUGGESTIONS
    try:
        sugs._trim()
        ids = [r["id"] for r in sugs._data["suggestions"]]
        # Three over the cap: the three oldest droppable go, in order.
        assert "g_dismissed" not in ids and "g_issue" not in ids and "g_undone" not in ids
        assert "g_nochange" in ids and len(ids) == 7
        ps.MAX_SUGGESTIONS = 2
        sugs._trim()
        left = {r["id"] for r in sugs._data["suggestions"]}
        # Nothing finished remains to drop, so the live ones stay over the cap.
        assert left == {r["id"] for r in keep}
    finally:
        ps.MAX_SUGGESTIONS = saved


def test_a_due_read_is_remembered_across_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "sugs.json"
    store = ps.SuggestionStore(path)
    assert not store.has_due_read("conv_a")
    store.note_due_read("conv_a", 5.0)
    assert ps.SuggestionStore(path).has_due_read("conv_a")
    assert not ps.SuggestionStore(path).has_due_read("conv_b")


def test_bad_stored_records_are_dropped_with_a_warning_not_a_crash(
    tmp_path: Path, roles: RolePrefsStore, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "sugs.json"
    store = ps.SuggestionStore(path)
    _submit(store, roles, _own())
    data = json.loads(path.read_text())
    good_review, good_sug = data["reviews"][0], data["suggestions"][0]
    data["reviews"] += [{"id": "rv_bad"}, {"id": 5, "session": "s", "owner": "o"}, "x"]
    data["suggestions"] += [
        {"id": "sg_bad", "status": "open"},
        good_sug | {"id": "sg_status", "status": "weird"},
        good_sug | {"id": "sg_nobefore", "before": None},
        good_sug | {"id": "sg_changes", "changes": "nope"},
    ]
    path.write_text(json.dumps(data))
    caplog.set_level(logging.WARNING)
    fresh = ps.SuggestionStore(path)
    sugs_listed, reviews = fresh.list_for(None)
    assert [r["id"] for r in reviews] == [good_review["id"]]
    assert [s["id"] for s in sugs_listed] == [good_sug["id"]]
    assert caplog.text.count("dropped an invalid") == 7
    # The top level still decides quarantine.
    path.write_text(json.dumps({"reviews": "x"}))
    assert ps.SuggestionStore(path).list_for(None) == ([], [])


@pytest.mark.parametrize(
    ("title", "preset_title", "expected"),
    [
        ("Balanced: too few runs to judge", "Balanced", "too few runs to judge"),
        ("balanced:  too few runs", "Balanced", "too few runs"),
        ("Builder Opus to Sonnet", "Balanced", "Builder Opus to Sonnet"),
        ("Balanced:", "Balanced", ""),
        ("Balanced : x", "Balanced", "x"),
        ("STRASSE: reduce reviewers", "Straße", "reduce reviewers"),
        ("C++ fix: x", "C++ fix", "x"),
        ("Balanced: x", None, "Balanced: x"),
        ("Builder: Sonnet", "Balanced", "Builder: Sonnet"),
    ],
)
def test_bare_title_drops_a_leading_preset_name(
    title: str, preset_title: str | None, expected: str
) -> None:
    assert ps.bare_title(title, preset_title) == expected


def test_public_serves_a_stored_title_without_its_preset_name() -> None:
    record = {
        "id": "sg_1",
        "owner": "a",
        "kind": "no_change",
        "status": "open",
        "preset_title": "Balanced",
        "title": "Balanced: too few runs to judge",
    }
    assert ps.public(record, "a")["title"] == "too few runs to judge"


def test_submit_stores_the_title_without_its_preset_name(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    _submit(sugs, roles, _own(title="Mine: Builder to Sonnet"))
    listed, _ = sugs.list_for("ann")
    assert listed[0]["title"] == "Builder to Sonnet" and listed[0]["preset_title"] == "Mine"


def test_submit_refuses_a_title_that_is_only_the_preset_name(
    sugs: ps.SuggestionStore, roles: RolePrefsStore
) -> None:
    with pytest.raises(ps.SuggestionError) as err:
        _submit(sugs, roles, _own(title="Mine:"))
    assert err.value.status == 422 and "only the preset name" in str(err.value)
