"""The label helpers: blank means unset, and the vocabulary is what the docs say."""

from __future__ import annotations

import pytest

from ae_omni_policies import labels


def test_blank_and_missing_read_as_unset() -> None:
    assert labels.label({}, labels.STATUS) is None
    assert labels.label(None, labels.STATUS) is None
    assert labels.label({labels.STATUS: ""}, labels.STATUS) is None
    assert labels.label({labels.STATUS: "   "}, labels.STATUS) is None
    assert labels.label({labels.STATUS: " working "}, labels.STATUS) == "working"


def test_is_working() -> None:
    assert labels.is_working({labels.STATUS: labels.WORKING})
    assert not labels.is_working({labels.STATUS: labels.PARKED})
    assert not labels.is_working({})


def test_vocabulary() -> None:
    assert labels.STATUS_VALUES == ("working", "blocked", "review", "parked", "reference")
    assert labels.DEFAULT_SLOT_LIMIT == 3
    assert all(
        key.startswith(labels.NAMESPACE)
        for key in (
            labels.STATUS,
            labels.STARTED,
            labels.GATE,
            labels.FORK,
            labels.NEXT,
            labels.ABSORBED,
            labels.UNFILED,
            labels.FORK_SUSPECT,
            labels.TRIAGE,
            labels.TASK,
            labels.OVERFLOW,
        )
    )


def test_clip_is_one_line_and_capped() -> None:
    assert labels.clip("a\n  b\tc") == "a b c"
    long = "word " * 100
    clipped = labels.clip(long, cap=40)
    assert len(clipped) <= 40
    assert clipped.endswith("...")


def test_note_keys_number_each_saved_line() -> None:
    assert labels.note_key(labels.ABSORBED, 3) == "ae.absorbed.3"
    assert labels.note_key(labels.UNFILED, 1) == "ae.unfiled.1"
    assert labels.is_note_key("ae.absorbed.12")
    assert labels.is_note_key("ae.unfiled.1")
    for key in ("ae.absorbed", "ae.unfiled", "ae.absorbed.x", "ae.status", "ae.next.1"):
        assert not labels.is_note_key(key)


def test_slot_limit_reads_the_environment() -> None:
    # One setting since 2026-09-24: AE_SLOT_LIMIT in config/server.env.
    assert labels.slot_limit_from_env({}) == 3
    assert labels.slot_limit_from_env({"AE_SLOT_LIMIT": ""}) == 3
    assert labels.slot_limit_from_env({"AE_SLOT_LIMIT": " 4 "}) == 4
    assert labels.slot_limit_from_env({"AE_SLOT_LIMIT": "1"}) == 1


@pytest.mark.parametrize("raw", ["0", "-2", "four", "3.5"])
def test_slot_limit_refuses_a_bad_value_by_name(raw: str) -> None:
    with pytest.raises(ValueError, match=r"AE_SLOT_LIMIT=.*config/server\.env"):
        labels.slot_limit_from_env({"AE_SLOT_LIMIT": raw})
