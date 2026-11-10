"""The Decision-1 advisor: the guard, the request, the verdict and every failure."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ae_omni_policies import decision_advisor as da
from ae_omni_policies import preset_runs

ENDPOINT = "https://decision.example.com"
KEY = "test-key-value"
GOOD = "Fix a failing test in the report builder"
FAKE_CLIENT = "Zorbex"  # a made-up name for the denylist fixture


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    deny = tmp_path / "deny.txt"
    deny.write_text(f"# private\n{FAKE_CLIENT}\nproject nightingale\n", encoding="utf-8")
    return {
        "AE_DECISION_ENABLED": "1",
        "AE_DECISION_ENDPOINT": ENDPOINT + "/",
        "AE_DECISION_API_KEY": KEY,
        "AE_DECISION_USERS": "owner@example.com",
        "AE_DECISION_DENYLIST_FILE": str(deny),
        "AE_DECISION_TERM_ALLOW": "Fabric,Power BI",
    }


PRESETS: list[dict[str, Any]] = [
    {"id": "balanced", "title": "Balanced", "when_to_use": "Most tasks."},
    {"id": "deep", "title": "Deep", "when_to_use": "Hard, high-stakes problems."},
    {"id": "quick", "title": "Quick", "when_to_use": "Small, clear changes."},
]


def request(summary: str = GOOD, **kw: Any) -> da.AdviseRequest:
    return da.AdviseRequest(summary=summary, **kw)


def mock(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    monkeypatch.setattr(da, "_shared_transport", lambda: httpx.MockTransport(wrapped))
    return seen


# The live size answer (checked 2026-11-06): a float index, a legend keyed by level number and
# a probability per level. Levels 2 and 3 are named here for the test.
LIVE_LEGEND = {"0": "Cosmetic", "1": "Minor", "2": "Moderate", "3": "Major"}
LIVE_SIZE_PROBS = {"0": 0.0014, "1": 0.0049, "2": 0.346, "3": 0.647}


@pytest.fixture(autouse=True)
def _fresh_rate_limit() -> None:
    da._calls.clear()


def answer(
    choice: str, probs: dict[str, float], size_probs: dict[str, float] | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "answers": {
                "preset": {
                    "type": "choice",
                    "choice": choice,
                    "confidence": probs.get(choice, 0),
                    "probabilities": probs,
                },
                "size": {
                    "type": "score",
                    "score": 2.639,
                    "confidence": 0.53,
                    "legend": LIVE_LEGEND,
                    "probabilities": size_probs or LIVE_SIZE_PROBS,
                },
            }
        },
    )


async def run(env: dict[str, str], summary: str = GOOD, messages: Any = (), **kw: Any):
    return await da.advise(
        request(summary, **kw), PRESETS, messages, owner="owner@example.com", environ=env
    )


# -- request shape


async def test_request_shape(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    seen = mock(monkeypatch, lambda r: answer("deep", {"deep": 0.8, "balanced": 0.1}))
    reply, _ = await run(env, files=12, repos=1, kind="code", needs_design=True)
    (req,) = seen
    assert str(req.url) == ENDPOINT + "/providers/microsoft/v1/systemone"
    assert req.headers["api-key"] == KEY
    body = json.loads(req.content)
    assert body["model"] == "decision-1"
    assert body["state"] == {
        "task": GOOD,
        "files": 12,
        "repos": 1,
        "kind": "code",
        "needs_design": True,
        "has_tests": False,
    }
    preset = body["questions"]["preset"]
    assert preset["type"] == "choice"
    assert set(preset["criteria"]) == {"balanced", "deep", "quick", "abstain"}
    assert preset["criteria"]["deep"] == "Hard, high-stakes problems."
    assert body["questions"]["size"] == {
        "type": "score",
        "instructions": "How big is this task?",
        "criteria": da.SIZE_LEVELS,
    }
    assert reply["verdict"] == "use"


# -- verdict thresholds


@pytest.mark.parametrize(
    ("choice", "probs", "verdict"),
    [
        ("deep", {"deep": 0.8, "balanced": 0.1, "abstain": 0.1}, "use"),
        ("deep", {"deep": 0.59, "balanced": 0.1}, "decide_yourself"),  # under 0.6
        ("deep", {"deep": 0.6, "balanced": 0.1}, "use"),  # at the floor
        ("deep", {"deep": 0.62, "balanced": 0.5}, "decide_yourself"),  # margin 0.12
        ("deep", {"deep": 0.65, "balanced": 0.5}, "use"),  # margin 0.15
        ("abstain", {"abstain": 0.9, "deep": 0.05}, "decide_yourself"),
        ("ghost", {"ghost": 0.9}, "decide_yourself"),  # not one of the presets
    ],
)
async def test_thresholds(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    choice: str,
    probs: dict[str, float],
    verdict: str,
) -> None:
    mock(monkeypatch, lambda r: answer(choice, probs))
    reply, entry = await run(env)
    assert reply["verdict"] == verdict and entry["verdict"] == verdict
    if verdict == "use":
        assert reply["preset"] == choice and reply["size"] == "Major"
        assert reply["confidence"] == round(probs[choice], 3)


async def test_min_confidence_is_configurable(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    mock(monkeypatch, lambda r: answer("deep", {"deep": 0.7, "balanced": 0.1}))
    assert (await run(env))[0]["verdict"] == "use"
    strict = env | {"AE_DECISION_MIN_CONFIDENCE": "0.9"}
    assert (await run(strict))[0]["verdict"] == "decide_yourself"


async def test_size_is_the_legend_label_of_the_most_likely_level(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}))
    reply, entry = await run(env)
    assert reply["size"] == "Major"  # not the float score 2.639
    assert entry["size_probabilities"]["3"] == 0.647


async def test_size_is_left_out_when_no_level_holds_half_the_weight(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    spread = {"0": 0.1, "1": 0.2, "2": 0.4, "3": 0.3}
    mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}, spread))
    reply, _ = await run(env)
    assert reply["verdict"] == "use" and "size" not in reply
    assert da.size_label({"legend": LIVE_LEGEND, "probabilities": {"2": 0.5}}) == "Moderate"
    assert da.size_label({"legend": LIVE_LEGEND, "probabilities": {"2": 0.49}}) is None
    assert da.size_label({"probabilities": {"2": 0.9}}) is None


# -- failures never raise and never block


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(500, text="boom"), "http_500"),
        (httpx.Response(401, text="no"), "http_401"),
        (httpx.Response(200, text="not json"), "bad_json"),
        (httpx.Response(200, json={"answers": 3}), "bad_shape"),
    ],
)
async def test_http_failures_give_decide_yourself(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], response: httpx.Response, error: str
) -> None:
    mock(monkeypatch, lambda r: response)
    reply, entry = await run(env)
    assert reply == {"verdict": "decide_yourself", "reason": "error"}
    assert entry["error_class"] == error
    assert KEY not in json.dumps([reply, entry])


async def test_timeout_gives_decide_yourself(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    def slow(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    mock(monkeypatch, slow)
    reply, entry = await run(env)
    assert reply["verdict"] == "decide_yourself" and entry["error_class"] == "timeout"


async def test_connect_error_names_the_class_only(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {ENDPOINT} with {KEY}")

    mock(monkeypatch, down)
    reply, entry = await run(env)
    assert entry["error_class"] == "ConnectError"
    assert KEY not in json.dumps([reply, entry]) and ENDPOINT not in json.dumps([reply, entry])


# -- off by default


async def test_off_by_default_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}))
    reply, _ = await run({})
    assert reply == {"verdict": "decide_yourself", "reason": "advisor_off"}
    assert seen == []


@pytest.mark.parametrize(
    "drop",
    ["AE_DECISION_ENABLED", "AE_DECISION_ENDPOINT", "AE_DECISION_API_KEY", "AE_DECISION_USERS"],
)
async def test_each_required_setting_matters(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], drop: str
) -> None:
    seen = mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}))
    assert (await run(env))[0]["verdict"] == "use"
    assert len(seen) == 1
    assert (await run({k: v for k, v in env.items() if k != drop}))[0]["reason"] == "advisor_off"
    assert len(seen) == 1
    assert not da.available("owner@example.com", {k: v for k, v in env.items() if k != drop})
    assert da.available("owner@example.com", env)


async def test_a_user_who_is_not_allowed_is_refused(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    seen = mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}))
    other = await da.advise(request(), PRESETS, [], owner="helper@example.com", environ=env)
    assert other[0]["reason"] == "advisor_off" and seen == []
    assert not da.available("helper@example.com", env)
    assert not da.available(None, env)


# -- the guard: every refusal returns a code, never the text, and the same input
# without the offending part goes through


def passes(env: dict[str, str], summary: str, messages: Any = ()) -> str | None:
    cfg = da.config(env)
    return da.check_summary(summary, da.load_denylist(cfg.denylist_file), cfg.term_allow, messages)


GUARD_CASES = [
    ("newline", "Fix a failing test\nin the report builder", GOOD),
    ("url", "Fix a failing test, see https://x.example.com/a", GOOD),
    ("url", "Fix a failing test on www.thing", GOOD),
    ("email", "Fix a failing test, ask pat@example.com", GOOD),
    ("path", "Fix a failing test in src/builder", GOOD),
    ("backtick", "Fix a failing `test` in the report builder", GOOD),
    ("quote", 'Fix a failing "test" in the report builder', GOOD),
    ("quote", "Fix a failing test in the builder's report", GOOD),
    ("digits", "Fix failing test 12345 in the report builder", GOOD),
    (
        "digits",
        "Fix failing test 1234 in the report builder",
        "Fix failing test 123 in the report builder",
    ),
    ("denylist", f"Fix a failing test for {FAKE_CLIENT} reports", GOOD),
    ("denylist", "Fix a failing test for PROJECT NIGHTINGALE", GOOD),
    ("proper_noun", "Fix a failing test for Marigold reports", GOOD),
    ("proper_noun", "Fix a failing test for Pat in reports", GOOD),
    # a name as the first word, a non-ASCII name, a capital after a hyphen
    ("proper_noun", "Contoso dashboard fix needed soon", "Update dashboard fix needed soon"),
    ("proper_noun", "Fix the \u00d8rsted model for reports", "Fix the model for reports"),
    ("proper_noun", "Plan the re-Contoso rollout steps", "Plan the re-do rollout steps"),
    # whitespace tricks do not hide a denylisted phrase
    ("denylist", "Fix tests for project\t nightingale reports", "Fix tests for project reports"),
    ("denylist", "Fix tests for PROJECT   Nightingale reports", "Fix tests for project reports"),
    # any domain-shaped token, but not versions
    ("url", "Fix tests for contoso.co.uk reports", "Fix tests for v1.2 reports"),
    ("url", "Fix tests for acme.nl reports", "Fix tests on python 3.11 reports"),
    # digit groups split by spaces, dashes or dots, but not small counts
    ("digits", "Fix tests for 555 12 34 reports", "Fix 4 files and 12 tests for reports"),
    ("digits", "Fix tests for 12-34-56-78 reports", "Fix tests for 12-3 reports"),
    ("digits", "Fix tests for 1.2.3.4 reports", "Fix tests for 1.2.3 reports"),
]


@pytest.mark.parametrize(("code", "bad", "good"), GUARD_CASES)
def test_each_guard_check_refuses_and_its_clean_twin_passes(
    env: dict[str, str], code: str, bad: str, good: str
) -> None:
    assert passes(env, bad) == code
    assert passes(env, good) is None


@pytest.mark.parametrize(("code", "bad", "good"), GUARD_CASES)
async def test_a_refusal_sends_nothing_and_never_echoes_the_text(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], code: str, bad: str, good: str
) -> None:
    seen = mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}))
    reply, entry = await run(env, bad)
    assert reply == {"verdict": "decide_yourself", "reason": f"refused_{code}"}
    assert seen == []
    dumped = json.dumps([reply, entry]).lower()
    for word in ("zorbex", "nightingale", "marigold", "12345", "pat@", "https", "src/"):
        assert word not in dumped
    ok, _ = await run(env, good)
    assert ok["verdict"] == "use" and len(seen) == 1


def test_a_capitalised_first_word_and_allowed_terms_pass(env: dict[str, str]) -> None:
    assert passes(env, "Update the summary of a Fabric report fix") is None
    assert passes(env, "Update the Power BI theme file notes") is None
    assert passes(env, "Review the PR and update the README") is None
    assert passes(env, "Update the Power Query notes") == "proper_noun"  # not allowed here
    assert passes(env, "Update the Power notes") == "proper_noun"  # only the whole phrase counts
    assert passes(env, "Marigold summary of a Fabric report fix") == "proper_noun"  # first word
    assert passes(env, "Fix a failing test so I can ship it") is None  # "I" is never a name
    assert passes(env, "A failing test in the builder needs work") is None
    assert passes(env, "Fix the e.g. case and the i.e. case") is None


def test_a_multi_word_allow_entry_matches_as_a_whole_phrase_only(env: dict[str, str]) -> None:
    both = env | {"AE_DECISION_TERM_ALLOW": "Acme CRM,Fabric"}
    assert passes(both, "Update the Acme CRM export notes") is None
    assert passes(both, "Update the Acme export notes") == "proper_noun"
    assert passes(both, "Update the CRM export notes") == "proper_noun"
    assert passes(both, "Update the acme crm export notes") is None
    assert passes(both, "Update the Fabric export notes") is None


def test_passthrough_is_refused_against_any_user_message(env: dict[str, str]) -> None:
    messages = ["hello there", f"please {GOOD.lower()} today", "thanks"]
    assert passes(env, GOOD, messages) == "passthrough"
    assert passes(env, GOOD, ["hello there", "thanks"]) is None
    assert passes(env, GOOD, [f"{GOOD[:20]} something else entirely"]) is None


def test_fails_closed_without_a_denylist(tmp_path: Path, env: dict[str, str]) -> None:
    assert passes(env, GOOD) is None
    assert da.check_summary(GOOD, [], da.DEFAULT_TERM_ALLOW, []) == "no_denylist"
    missing = env | {"AE_DECISION_DENYLIST_FILE": str(tmp_path / "nope.txt")}
    assert da.load_denylist(missing["AE_DECISION_DENYLIST_FILE"]) == []
    assert not da.available("owner@example.com", missing)
    empty = tmp_path / "empty.txt"
    empty.write_text("# nothing\n\n", encoding="utf-8")
    assert not da.available("owner@example.com", env | {"AE_DECISION_DENYLIST_FILE": str(empty)})
    unset = {k: v for k, v in env.items() if k != "AE_DECISION_DENYLIST_FILE"}
    assert not da.available("owner@example.com", unset)


async def test_no_denylist_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    seen = mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}))
    reply, _ = await run(env | {"AE_DECISION_DENYLIST_FILE": ""})
    assert reply == {"verdict": "decide_yourself", "reason": "refused_no_denylist"}
    assert seen == []


async def test_a_preset_text_on_the_denylist_is_dropped_from_the_criteria(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    seen = mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}))
    presets = [*PRESETS, {"id": "tainted", "title": "T", "when_to_use": f"Work for {FAKE_CLIENT}"}]
    reply, _ = await da.advise(request(), presets, [], owner="owner@example.com", environ=env)
    criteria = json.loads(seen[0].content)["questions"]["preset"]["criteria"]
    assert "tainted" not in criteria and "deep" in criteria
    assert reply["verdict"] == "use"
    clean = [*PRESETS, {"id": "tainted", "title": "T", "when_to_use": "Work for nobody"}]
    await da.advise(request(), clean, [], owner="owner@example.com", environ=env)
    assert "tainted" in json.loads(seen[1].content)["questions"]["preset"]["criteria"]


async def test_only_the_first_eight_presets_are_offered(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    seen = mock(monkeypatch, lambda r: answer("p0", {"p0": 0.9}))
    many = [{"id": f"p{n}", "title": f"P{n}", "when_to_use": "x" * 300} for n in range(12)]
    await da.advise(request(), many, [], owner="owner@example.com", environ=env)
    criteria = json.loads(seen[0].content)["questions"]["preset"]["criteria"]
    assert len(criteria) == 9 and all(len(v) <= 200 for k, v in criteria.items() if k != "abstain")


async def test_no_usable_preset_text_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    seen = mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}))
    reply, _ = await da.advise(
        request(), [{"id": "a", "title": "A"}], [], owner="owner@example.com", environ=env
    )
    assert reply["reason"] == "no_presets" and seen == []


# -- the log entry


async def test_the_entry_hashes_the_summary_unless_asked(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9, "balanced": 0.05}))
    _, entry = await run(env, files=3, kind="docs")
    assert entry["summary_sha"] == da.summary_hash(GOOD) and entry["summary_len"] == len(GOOD)
    assert "summary" not in entry and GOOD not in json.dumps(entry)
    assert entry["preset_probabilities"] == {"deep": 0.9, "balanced": 0.05}
    assert entry["kind"] == "docs" and entry["files"] == 3 and entry["latency_ms"] >= 0
    _, kept = await run(env | {"AE_DECISION_LOG_SUMMARY": "1"})
    assert kept["summary"] == GOOD


async def test_a_refusal_entry_has_the_code_and_not_the_term(env: dict[str, str]) -> None:
    _, entry = await run(env, f"Fix a failing test for {FAKE_CLIENT} reports")
    assert entry["reason"] == "refused_denylist"
    assert FAKE_CLIENT.lower() not in json.dumps(entry).lower()


def test_request_model_rejects_extras_and_bad_sizes() -> None:
    with pytest.raises(ValueError):
        da.AdviseRequest(summary=GOOD, repo="x")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        da.AdviseRequest(summary="short")
    with pytest.raises(ValueError):
        da.AdviseRequest(summary="x" * 161)
    with pytest.raises(ValueError):
        da.AdviseRequest(summary=GOOD, kind="client")  # type: ignore[arg-type]


# -- the run log (preset_runs.record_advice)


def test_record_advice_keeps_at_most_ten_and_skips_when_insights_are_off() -> None:
    state = preset_runs.StateFile(None)
    assert preset_runs.record_advice(state, "lead", {"n": 0}, insights_on=False) is False
    with state.locked() as data:
        assert data["reports"] == {}
    for n in range(12):
        kept = preset_runs.record_advice(state, "lead", {"n": n})
        assert kept is (n < preset_runs.ADVISOR_MAX)
    with state.locked() as data:
        assert [e["n"] for e in data["reports"]["lead"]["advisor"]] == list(range(10))
    block = preset_runs.Sweeper._review_block(data["reports"]["lead"], 0)
    assert block["reported"] is False  # advice alone is not a review report
    out = preset_runs.record_review(state, "lead", 2, 3, 1)
    assert out["totals"] == {
        "reviewers_run": 2,
        "findings_returned": 3,
        "findings_fixed": 1,
        "calls": 1,
    }


# -- review round 1


async def test_a_refused_summary_is_never_kept_in_the_log_entry(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    keep = env | {"AE_DECISION_LOG_SUMMARY": "1"}
    seen = mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}))
    bad = f"Fix a failing test for {FAKE_CLIENT} reports"
    reply, entry = await run(keep, bad)
    assert reply["reason"] == "refused_denylist" and seen == []
    assert "summary" not in entry and FAKE_CLIENT.lower() not in json.dumps(entry).lower()
    _, sent = await run(keep)  # the same setting keeps the text of a call that was sent
    assert sent["summary"] == GOOD and len(seen) == 1


async def test_everything_sent_for_a_preset_passes_the_name_and_denylist_checks(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    seen = mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}))
    tainted = [
        {"id": f"{FAKE_CLIENT.lower()}-flow", "title": "A", "when_to_use": "Plain work."},
        {"id": "named", "title": "B", "when_to_use": "Work for Contoso reports."},
        {"id": "described", "title": "C", "description": "Cases at project nightingale."},
        {"id": "Mixed-Case", "title": "D", "when_to_use": "Plain work."},
    ]
    clean = [
        {"id": "plain-flow", "title": "A", "when_to_use": "Plain work."},
        {"id": "named", "title": "B", "when_to_use": "Routine work for reports."},
        {"id": "described", "title": "C", "description": "Cases at the office."},
        {"id": "mixed-case", "title": "D", "when_to_use": "Plain work."},
    ]
    kw = {"owner": "owner@example.com", "environ": env}
    await da.advise(request(), [*PRESETS[:1], *tainted], [], **kw)
    sent = json.loads(seen[0].content)["questions"]["preset"]["criteria"]
    assert set(sent) == {"balanced", "abstain"}
    await da.advise(request(), clean, [], **kw)
    sent = json.loads(seen[1].content)["questions"]["preset"]["criteria"]
    assert set(sent) == {"plain-flow", "named", "described", "mixed-case", "abstain"}


async def test_calls_stop_after_ten_a_session(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    seen = mock(monkeypatch, lambda r: answer("deep", {"deep": 0.9}))
    kw = {"owner": "owner@example.com", "environ": env}
    for _ in range(da.MAX_PER_RUN):
        reply, _ = await da.advise(request(), PRESETS, [], session="conv_a", **kw)
        assert reply["verdict"] == "use"
    reply, entry = await da.advise(request(), PRESETS, [], session="conv_a", **kw)
    assert reply == {"verdict": "decide_yourself", "reason": "rate_limited"}
    assert len(seen) == da.MAX_PER_RUN and entry["verdict"] == "decide_yourself"
    other, _ = await da.advise(request(), PRESETS, [], session="conv_b", **kw)
    assert other["verdict"] == "use"
    assert da._allow_call("conv_a", now=10**9) is True  # the hour window rolls over


def test_a_busy_run_log_skips_the_advisor_entry_instead_of_waiting(tmp_path: Path) -> None:
    import threading
    import time

    path = tmp_path / "state.json"
    holder, advisor = preset_runs.StateFile(path), preset_runs.StateFile(path)
    result: list[bool] = []
    took: list[float] = []

    def call() -> None:
        started = time.monotonic()
        result.append(preset_runs.record_advice(advisor, "lead", {"n": 1}))
        took.append(time.monotonic() - started)

    with holder.locked():  # another writer holds the file lock
        worker = threading.Thread(target=call)
        worker.start()
        worker.join(timeout=5)  # without the fix the call waits here for as long as we hold it
        assert not worker.is_alive(), "record_advice blocked on the held lock"
    assert result == [False] and took[0] < 2
    assert preset_runs.record_advice(advisor, "lead", {"n": 2}) is True  # free again
    with advisor.locked() as data:
        assert [e["n"] for e in data["reports"]["lead"]["advisor"]] == [2]
