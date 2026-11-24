"""Aggregation behind GET /v1/ae/decision/report: counts and ratios, no ids or text."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ae_omni_policies import decision_report as dr
from ae_omni_policies import decision_shadow as ds

NOW = 1_800_000_000.0


def _at(offset_s: float) -> str:
    return ds._iso(NOW + offset_s)


def ask(offset: float, session: str = "conv_secret1", **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "at": _at(offset),
        "point": "agent_ask",
        "session": session,
        "features": {
            "kind": "choice",
            "n_options": 2,
            "n_facts": 1,
            "has_context": True,
            "batch": 2,
            "role": "lead",
        },
        "would_act": True,
        "latency_ms": 400,
        "error_class": None,
    }
    row.update(over)
    return row


def nudge(offset: float, session: str = "conv_secret1", moment: str = "start") -> dict[str, Any]:
    return {
        "at": _at(offset),
        "point": "nudge",
        "session": session,
        "mode": "live",
        "features": {"moment": moment},
        "would_act": True,
        "acted": True,
    }


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "shadow.jsonl"
    monkeypatch.setenv("AE_DECISION_SHADOW_FILE", str(path))
    monkeypatch.setenv("AE_DECISION_CALLS_FILE", str(tmp_path / "calls.json"))
    monkeypatch.setattr(ds, "_now", lambda: NOW)
    ds.reset()
    return path


def write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_aggregates(log: Path) -> None:
    write(
        log,
        [
            ask(-100, latency_ms=100),
            ask(
                -90,
                latency_ms=300,
                would_act=False,
                features={
                    **ask(0)["features"],
                    "role": "helper",
                    "batch": 1,
                    "has_context": False,
                    "kind": "yes_no",
                },
            ),
            ask(-80, latency_ms=0, error_class="timeout", would_act=False),
            ask(-70, error_class="refused_key", features={"role": "lead"}),
            ask(-60, error_class="rate_limited", features={"role": "lead"}),
            nudge(-200, moment="start"),
            nudge(-150, moment="tests", session="conv_other"),
        ],
    )
    out = dr.report(7)
    assert out["asks"]["total"] == 5
    assert out["asks"]["by_role"] == {"lead": 4, "helper": 1}
    assert out["asks"]["by_kind"] == {"choice": 2, "yes_no": 1}
    assert out["asks"]["batch_sizes"] == {"2": 2, "1": 1}
    assert out["asks"]["with_context"] == 0.667
    assert (out["asks"]["answered"], out["asks"]["low_confidence"]) == (1, 1)
    assert out["asks"]["errors"] == {"timeout": 1} and out["asks"]["errors_total"] == 1
    assert out["asks"]["refused"] == {"refused_key": 1, "rate_limited": 1}
    assert out["asks"]["refused_total"] == 2
    assert out["asks"]["latency_ms"] == {"p50": 100, "p95": 300}
    assert out["nudges"]["by_moment"] == {"start": 1, "tests": 1}
    assert (
        out["nudges"]["followed_by_ask"] == {"start": 1} and out["nudges"]["followed_total"] == 1
    )
    assert out["calls_today"] == 0 and out["daily_cap"] == ds.DEFAULT_DAILY_CAP


def test_calls_today_is_the_saved_counter_after_a_restart(log: Path) -> None:
    """The report reads the saved counter the Decision card reads, not the cap counter."""
    write(log, [])
    for _ in range(3):
        ds.note_call()
    ds.reset()  # a deploy: memory gone, the saved file stays
    assert ds.usage()["calls_today"] == 0
    assert dr.report(7)["calls_today"] == 3


def test_a_nudge_followed_after_two_minutes_does_not_count(log: Path) -> None:
    write(log, [nudge(-500), ask(-500 + 121)])
    assert dr.report(7)["nudges"]["followed_total"] == 0


def test_window_and_rotated_file(log: Path) -> None:
    write(log, [ask(-3600), ask(-3 * 86400)])
    write(log.with_name(log.name + ".1"), [ask(-2 * 86400), ask(-10 * 86400)])
    assert dr.report(1)["asks"]["total"] == 1
    assert dr.report(7)["asks"]["total"] == 3
    assert dr.report(30)["asks"]["total"] == 4


def test_output_holds_no_ids_or_text(log: Path) -> None:
    write(log, [ask(-10, session="conv_secret1"), nudge(-20, session="conv_secret1")])
    text = json.dumps(dr.report(7))
    assert "conv_secret1" not in text and "session" not in text


def test_empty_or_missing_log(log: Path) -> None:
    out = dr.report(7)
    assert out["asks"]["total"] == 0 and out["asks"]["latency_ms"] == {"p50": None, "p95": None}
    assert out["asks"]["with_context"] is None


def test_low_confidence_comes_from_the_logged_flag_not_would_act(log: Path) -> None:
    feats = ask(0)["features"]
    write(
        log,
        [
            ask(-90, features={**feats, "low_confidence": False}, would_act=False),
            ask(-80, features={**feats, "low_confidence": True}, would_act=True),
            ask(-70),  # an older line without the flag falls back to would_act
        ],
    )
    out = dr.report(7)["asks"]
    assert (out["answered"], out["low_confidence"]) == (2, 1)


def test_refused_lines_count_per_question_and_still_follow_a_nudge(log: Path) -> None:
    refused = {"features": {"role": "lead"}, "error_class": "refused_denylist"}
    write(
        log,
        [
            nudge(-100),
            ask(-60, **refused),
            ask(-60, **{**refused, "error_class": "batch_refused"}),
            ask(-59, **{**refused, "error_class": "http_429"}),
        ],
    )
    out = dr.report(7)
    assert out["asks"]["total"] == 3 and out["asks"]["refused_total"] == 3
    assert out["nudges"]["followed_total"] == 1
    assert "conv_secret1" not in json.dumps(out)


def test_the_log_is_streamed_not_read_whole(log: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(log, [ask(-60)])

    real = Path.read_text

    def boom(self: Path, *a: Any, **k: Any) -> str:
        # Only the shadow log is banned; the saved call counter is a small JSON read.
        if self.name.startswith(log.name):
            raise AssertionError("read_text on the log")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", boom)
    assert dr.report(7)["asks"]["total"] == 1
