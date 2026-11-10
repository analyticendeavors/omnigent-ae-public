"""The judge adapters: Decision-1 request mapping, state rejection and the rule judge."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from ae_omni_policies import decision_advisor as da
from ae_omni_policies import decision_shadow as ds
from ae_omni_policies.judge import Decision1Judge, RuleJudge

CFG = da.config(
    {
        "AE_DECISION_ENABLED": "1",
        "AE_DECISION_ENDPOINT": "https://decision.example.com",
        "AE_DECISION_API_KEY": "test-key-value",
    }
)


@pytest.fixture(autouse=True)
def _points() -> None:
    ds.reset()
    ds.register_point("p_yes", "yes_no", {"files": int, "kind": ("code", "docs")})
    ds.register_point("p_choice", "choice", {"files": int})
    ds.register_point("p_score", "score", {"size": float, "note": ds.GuardedSummary})


def mock(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, content=json.dumps({"answers": {"q": answer}}))

    monkeypatch.setattr(da, "_shared_transport", lambda: httpx.MockTransport(handler))
    return bodies


async def test_yes_no_is_a_two_option_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = mock(monkeypatch, {"choice": "yes", "probabilities": {"yes": 0.9, "no": 0.1}})
    v = await Decision1Judge(CFG).yes_no("p_yes", {"files": 3, "kind": "code"}, "Is it big?")
    assert v.answer is True and v.probs == {"yes": 0.9, "no": 0.1} and v.error is None
    q = bodies[0]["questions"]["q"]
    assert q["type"] == "choice" and set(q["criteria"]) == {"yes", "no"}
    assert q["instructions"] == "Is it big?"


async def test_yes_no_no(monkeypatch: pytest.MonkeyPatch) -> None:
    mock(monkeypatch, {"choice": "no", "probabilities": {"yes": 0.2, "no": 0.8}})
    assert (await Decision1Judge(CFG).yes_no("p_yes", {"files": 1}, "Q?")).answer is False


async def test_choice_unknown_label_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    mock(monkeypatch, {"choice": "zzz", "probabilities": {"a": 0.5}})
    v = await Decision1Judge(CFG).choice("p_choice", {"files": 1}, {"a": "A", "b": "B"})
    assert v.answer is None and v.probs == {"a": 0.5}


async def test_score_maps_levels(monkeypatch: pytest.MonkeyPatch) -> None:
    mock(
        monkeypatch,
        {"legend": {"0": "low", "1": "high"}, "probabilities": {"0": 0.2, "1": 0.8}},
    )
    v = await Decision1Judge(CFG).score("p_score", {"size": 1.5}, ["low", "high"])
    assert v.answer == "high" and v.probs == {"low": 0.2, "high": 0.8}


@pytest.mark.parametrize(
    "state",
    [
        {"files": "text"},  # a string for an int key
        {"files": True},  # a bool is not an int
        {"kind": "secret"},  # not in the enum
        {"undeclared": 1},  # not declared
        {"files": float("nan")},
    ],
)
async def test_bad_state_is_rejected_and_nothing_is_sent(
    monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]
) -> None:
    bodies = mock(monkeypatch, {"choice": "yes"})
    v = await Decision1Judge(CFG).yes_no("p_yes", state, "Q?")
    assert v.error == "bad_state" and v.answer is None and bodies == []


async def test_unguarded_string_is_rejected_and_guarded_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = mock(monkeypatch, {"legend": {"0": "a"}, "probabilities": {"0": 1.0}})
    j = Decision1Judge(CFG)
    assert (await j.score("p_score", {"note": "Fix a test somewhere"}, ["a"])).error == "bad_state"
    note = da.guard_summary("Fix a failing test", ["zorbex"], da.DEFAULT_TERM_ALLOW, [])
    assert note is not None
    assert (await j.score("p_score", {"note": note}, ["a"])).error is None
    assert len(bodies) == 1


def test_guard_summary_refuses() -> None:
    assert (
        da.guard_summary("see https://example.com now", ["x"], da.DEFAULT_TERM_ALLOW, []) is None
    )
    assert da.guard_summary("Fix a failing test", [], da.DEFAULT_TERM_ALLOW, []) is None


async def test_unregistered_point_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    mock(monkeypatch, {"choice": "yes"})
    assert (await Decision1Judge(CFG).yes_no("nope", {}, "Q?")).error == "bad_state"


async def test_http_error_is_a_class_not_a_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        da,
        "_shared_transport",
        lambda: httpx.MockTransport(lambda r: httpx.Response(500, text="secret body")),
    )
    v = await Decision1Judge(CFG).yes_no("p_yes", {"files": 1}, "Q?")
    assert v.error == "http_500" and "secret" not in repr(v)


async def test_not_configured() -> None:
    v = await Decision1Judge(da.config({})).yes_no("p_yes", {"files": 1}, "Q?")
    assert v.error == "not_configured"


async def test_rule_judge() -> None:
    j = RuleJudge(lambda s: s["files"] > 2, name="base")
    assert (await j.yes_no("p", {"files": 3}, "Q")).answer is True
    bad = RuleJudge(lambda s: 1 / 0)
    assert (await bad.choice("p", {}, {})).error == "ZeroDivisionError"
    assert (await RuleJudge(lambda s: 5).score("p", {}, [])).error == "bad_answer"


@pytest.mark.parametrize(
    "probs",
    [{}, {"yes": float("nan"), "no": 0.5}, {"yes": float("inf")}, {"yes": 1.2}, {"yes": -0.1}],
)
async def test_model_probabilities_must_be_finite_in_range_and_present(
    monkeypatch: pytest.MonkeyPatch, probs: dict[str, float]
) -> None:
    mock(monkeypatch, {"choice": "yes", "probabilities": probs})
    j = Decision1Judge(CFG)
    assert (await j.yes_no("p_yes", {"files": 1}, "Q?")).error == "bad_shape"
    assert (await j.choice("p_choice", {"files": 1}, {"yes": "Y"})).error == "bad_shape"
    mock(monkeypatch, {"legend": {"0": "a"}, "probabilities": probs})
    assert (await j.score("p_score", {"size": 1.0}, ["a"])).error == "bad_shape"
