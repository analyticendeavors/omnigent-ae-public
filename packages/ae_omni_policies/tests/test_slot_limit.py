"""The slot limit every count reads: the Settings page first, then AE_SLOT_LIMIT (2026-10-22)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from ae_omni_policies import labels, slot_limit


def _policy(limit: Any = None, *, enabled: bool = True, handler: str = slot_limit.HANDLER) -> Any:
    params = {"mode": "warn"} if limit is None else {"mode": "warn", "limit": limit}
    return SimpleNamespace(handler=handler, enabled=enabled, factory_params=params)


class _Store:
    """A policy store whose global policies are *rows*; counts each read."""

    def __init__(self, *rows: Any, error: Exception | None = None) -> None:
        self.rows = list(rows)
        self.error = error
        self.reads = 0

    def list_defaults(self) -> list[Any]:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return self.rows


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(labels, "SLOT_LIMIT", 4)
    slot_limit.remember_store(None)
    yield
    slot_limit.remember_store(None)


def test_without_a_store_the_environment_answers() -> None:
    # `just dev` and the tests' app: no container entrypoint hands a store over.
    assert slot_limit.effective_limit() == 4


def test_a_limit_set_on_the_page_wins() -> None:
    slot_limit.remember_store(_Store(_policy(6)))
    assert slot_limit.effective_limit() == 6


def test_a_slot_cap_without_a_limit_leaves_the_environment() -> None:
    # The seed's slot cap carries only mode and lot_url, as on Owner's box.
    slot_limit.remember_store(_Store(_policy()))
    assert slot_limit.effective_limit() == 4


def test_a_switched_off_slot_cap_does_not_count() -> None:
    slot_limit.remember_store(_Store(_policy(2, enabled=False)))
    assert slot_limit.effective_limit() == 4


def test_the_lowest_limit_of_several_slot_caps_wins() -> None:
    # The lowest is the one that holds prompts first.
    slot_limit.remember_store(_Store(_policy(6), _policy(5), _policy(2, enabled=False)))
    assert slot_limit.effective_limit() == 5


def test_a_cap_without_a_limit_holds_at_the_environment_value() -> None:
    # The seeded cap sets no limit and so holds at AE_SLOT_LIMIT (4 here); a second
    # cap at 6 cannot raise the count past the number that holds prompts first.
    slot_limit.remember_store(_Store(_policy(), _policy(6)))
    assert slot_limit.effective_limit() == 4
    slot_limit.remember_store(_Store(_policy(), _policy(2)))
    assert slot_limit.effective_limit() == 2


def test_a_whole_number_written_as_a_float_counts() -> None:
    slot_limit.remember_store(_Store(_policy(5.0)))
    assert slot_limit.effective_limit() == 5


@pytest.mark.parametrize("value", [0, -1, True, "5", 2.5])
def test_a_limit_that_is_not_a_whole_number_of_one_or_more_holds_at_the_environment(
    value: Any,
) -> None:
    slot_limit.remember_store(_Store(_policy(value)))
    assert slot_limit.effective_limit() == 4


def test_other_policies_with_a_limit_are_ignored() -> None:
    other = _policy(9, handler="somewhere.else.limit_policy")
    slot_limit.remember_store(_Store(other))
    assert slot_limit.effective_limit() == 4


def test_a_store_that_fails_falls_back_and_says_so(caplog: pytest.LogCaptureFixture) -> None:
    slot_limit.remember_store(_Store(error=RuntimeError("database is locked")))
    with caplog.at_level(logging.WARNING, logger=slot_limit.__name__):
        assert slot_limit.effective_limit() == 4
    assert "using the fallback" in caplog.text


def test_a_change_on_the_page_shows_on_the_next_read() -> None:
    # No cache: the store scopes each read to the caller's workspace, and an edit on
    # Settings reaches the next capacity answer.
    store = _Store(_policy(6))
    slot_limit.remember_store(store)
    assert slot_limit.effective_limit() == 6
    store.rows = [_policy(7)]
    assert slot_limit.effective_limit() == 7
    assert store.reads == 2


def test_forgetting_the_store_returns_to_the_environment() -> None:
    slot_limit.remember_store(_Store(_policy(6)))
    assert slot_limit.effective_limit() == 6
    slot_limit.remember_store(None)
    assert slot_limit.effective_limit() == 4


def test_family_children_reads_the_lowest_enabled_cap() -> None:
    def cap(params: dict[str, object], enabled: bool = True) -> SimpleNamespace:
        return SimpleNamespace(handler=slot_limit.HANDLER, enabled=enabled, factory_params=params)

    pick = slot_limit.family_children_from_policies
    assert pick([], 3) is None
    assert pick([cap({})], 3) == 3
    assert pick([cap({"family_children": 2}), cap({"family_children": 5})], 3) == 2
    assert pick([cap({"family_children": 0})], 3) == 0
    assert pick([cap({"family_children": 0}), cap({"family_children": 4})], 3) == 4
    assert pick([cap({"family_children": True}), cap({}, enabled=False)], 3) == 3


def test_resolve_takes_family_children_from_every_cap_and_the_limit_from_the_lowest() -> None:
    def cap(**params: object) -> SimpleNamespace:
        return SimpleNamespace(handler=slot_limit.HANDLER, enabled=True, factory_params=params)

    # Cap A (limit 3, family_children 0) with cap B (limit 5, family_children 2): the
    # limit is A's, but B still sets the family cap, as before 2026-10-26.
    rules = slot_limit.resolve_from_policies(
        [cap(limit=3, family_children=0), cap(limit=5, family_children=2, helpers_count=True)], 4
    )
    assert rules == slot_limit.SlotConfig(3, False, 2)


def test_resolve_falls_back_to_the_callers_rules_when_the_store_fails() -> None:
    class _Broken:
        def list_defaults(self) -> list[object]:
            raise RuntimeError("down")

    slot_limit.remember_store(_Broken())  # type: ignore[arg-type]
    try:
        mine = slot_limit.SlotConfig(7, True, 9)
        assert slot_limit.resolve(mine) == mine
    finally:
        slot_limit.remember_store(None)
