"""Decision points: modes, limits, breaker, whitelist, shadow log shape, rotation, fire()."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ae_omni_policies import decision_shadow as ds
from ae_omni_policies.judge import RuleJudge, Verdict

LOG_KEYS = {
    "at", "point", "session", "judge", "judge_ver", "mode", "features", "answer", "probs",
    "threshold", "would_act", "acted", "baseline", "latency_ms", "error_class",
    "dropped_features",
}  # fmt: skip


class FakeJudge:
    name = "fake"
    version = "v1"

    def __init__(self, verdict: Verdict | None = None, *, delay: float = 0, exc: bool = False):
        self.verdict = verdict or Verdict(True, {"yes": 0.9, "no": 0.1}, 5, None, "fake")
        self.delay, self.exc, self.calls = delay, exc, 0

    async def yes_no(self, point: str, state: Any, question: str) -> Verdict:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise RuntimeError("contains user text")
        return self.verdict

    async def choice(self, point: str, state: Any, options: Any) -> Verdict:
        return self.verdict

    async def score(self, point: str, state: Any, levels: Any) -> Verdict:
        return self.verdict


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    ds.reset()
    monkeypatch.setattr(ds, "DATA_DIR", tmp_path / "nodata")
    monkeypatch.setattr(ds, "_last_prune", 0.0)
    ds.register_point("step_down", "yes_no", {"files": int, "kind": ("code", "docs")})
    return {
        "AE_DECISION_ENABLED": "1",
        "AE_DECISION_POINTS": "step_down:shadow",
        "AE_DECISION_POINTS_FILE": str(tmp_path / "points.json"),
        "AE_DECISION_SHADOW_FILE": str(tmp_path / "shadow.jsonl"),
    }


def lines(env: dict[str, str]) -> list[dict[str, Any]]:
    p = Path(env["AE_DECISION_SHADOW_FILE"])
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


async def run(env: dict[str, str], judge: Any = None, **kw: Any) -> ds.Outcome:
    return await ds.ask(
        "step_down", judge or FakeJudge(), "yes_no", {"files": 2, "kind": "code"},
        baseline=False, session="s1", environ=env, question="Q?", **kw,
    )  # fmt: skip


# --- modes -----------------------------------------------------------------


def test_mode_resolution(env: dict[str, str]) -> None:
    pf = Path(env["AE_DECISION_POINTS_FILE"])
    assert ds.mode_for("step_down", env) == "shadow"
    assert ds.mode_for("other", env) == "off"
    env["AE_DECISION_POINTS"] = "step_down:live,review_stop:off"
    assert ds.mode_for("step_down", env) == "shadow"  # live ceiling, no promotion yet
    pf.write_text('{"step_down": "live"}')
    assert ds.mode_for("step_down", env) == "live"
    env["AE_DECISION_POINTS"] = "step_down:shadow"
    assert ds.mode_for("step_down", env) == "shadow"  # promotion cannot exceed the ceiling
    env["AE_DECISION_POINTS"] = "step_down:live"
    pf.write_text('{"step_down": "off"}')
    assert ds.mode_for("step_down", env) == "off"
    env["AE_DECISION_ENABLED"] = "0"
    assert ds.mode_for("step_down", env) == "off"


@pytest.mark.parametrize("content", ["{not json", '["a"]', '{"step_down": 3}'])
def test_broken_promotions_cap_at_shadow(env: dict[str, str], content: str) -> None:
    env["AE_DECISION_POINTS"] = "step_down:live"
    Path(env["AE_DECISION_POINTS_FILE"]).write_text(content)
    assert ds.mode_for("step_down", env) == "shadow"


def test_promotion_file_mtime_cache(env: dict[str, str]) -> None:
    env["AE_DECISION_POINTS"] = "step_down:live"
    pf = Path(env["AE_DECISION_POINTS_FILE"])
    pf.write_text('{"step_down": "live"}')
    assert ds.mode_for("step_down", env) == "live"
    pf.write_text('{"step_down": "shadow"}')  # size changes, so the cache key changes
    assert ds.mode_for("step_down", env) == "shadow"
    pf.write_text('{"step_down": "live"}')
    os.utime(pf, (1, 1))
    assert ds.mode_for("step_down", env) == "live"


def test_bad_ceiling_is_off(env: dict[str, str]) -> None:
    env["AE_DECISION_POINTS"] = "step_down:turbo"
    assert ds.mode_for("step_down", env) == "off"


# --- ask -------------------------------------------------------------------


async def test_off_does_not_call_or_log(env: dict[str, str]) -> None:
    env["AE_DECISION_POINTS"] = ""
    j = FakeJudge()
    out = await run(env, j)
    assert (out.acted, out.baseline, j.calls, lines(env)) == (False, False, 0, [])


async def test_shadow_logs_but_does_not_act(env: dict[str, str]) -> None:
    out = await run(env)
    assert out.would_act and not out.acted and out.value is False
    (row,) = lines(env)
    assert row["mode"] == "shadow" and row["would_act"] is True and row["acted"] is False


async def test_live_acts_above_threshold_only(env: dict[str, str]) -> None:
    env["AE_DECISION_POINTS"] = "step_down:live"
    Path(env["AE_DECISION_POINTS_FILE"]).write_text('{"step_down": "live"}')
    out = await run(env)
    assert out.acted and out.value is True
    low = FakeJudge(Verdict(True, {"yes": 0.55, "no": 0.45}, 1, None, "fake"))
    out = await run(env, low)
    assert not out.would_act and not out.acted and out.value is False


async def test_timeout_falls_back(env: dict[str, str]) -> None:
    env["AE_DECISION_POINT_TIMEOUT_S"] = "0.05"
    out = await run(env, FakeJudge(delay=1))
    assert not out.acted and out.verdict is None and out.baseline is False
    assert lines(env)[0]["error_class"] == "timeout"


async def test_judge_exception_never_leaks(env: dict[str, str]) -> None:
    out = await run(env, FakeJudge(exc=True))
    assert out.verdict is None
    row = lines(env)[0]
    assert row["error_class"] == "RuntimeError" and "user text" not in json.dumps(row)


async def test_verdict_error_counts_as_failure(env: dict[str, str]) -> None:
    out = await run(env, FakeJudge(Verdict(None, error="http_500", judge="fake")))
    assert out.verdict is None and lines(env)[0]["error_class"] == "http_500"


async def test_wrong_method_is_baseline(env: dict[str, str]) -> None:
    j = FakeJudge()
    out = await ds.ask("step_down", j, "choice", {}, baseline="x", environ=env, options={})
    assert not out.acted and j.calls == 0


async def test_ask_never_raises(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ds, "filter_state", lambda *a: 1 / 0)
    out = await run(env)
    assert out.baseline is False and not out.acted


async def test_daily_cap(env: dict[str, str]) -> None:
    env["AE_DECISION_DAILY_CAP"] = "2"
    j = FakeJudge()
    for _ in range(3):
        await run(env, j)
    assert j.calls == 2
    assert lines(env)[-1]["error_class"] == "daily_cap"


async def test_daily_cap_resets_next_day(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env["AE_DECISION_DAILY_CAP"] = "1"
    j = FakeJudge()
    await run(env, j)
    await run(env, j)
    assert j.calls == 1
    monkeypatch.setattr(ds, "_now", lambda: ds.time.time() + 86400 * 1.1)
    await run(env, j)
    assert j.calls == 2


async def test_minute_cap_then_window_moves(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env["AE_DECISION_MINUTE_CAP"] = "2"
    clock = [1000.0]
    monkeypatch.setattr(ds, "_mono", lambda: clock[0])
    j = FakeJudge()
    for _ in range(3):
        await run(env, j)
    assert j.calls == 2 and lines(env)[-1]["error_class"] == "minute_cap"
    clock[0] += 61
    await run(env, j)
    assert j.calls == 3


async def test_breaker_opens_and_resets(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(ds, "_mono", lambda: clock[0])
    env["AE_DECISION_MINUTE_CAP"] = "1000"
    bad = FakeJudge(exc=True)
    for _ in range(5):
        await run(env, bad)
        clock[0] += 1
    assert bad.calls == 5
    await run(env, bad)
    assert bad.calls == 5 and lines(env)[-1]["error_class"] == "breaker_open"
    clock[0] += 601
    good = FakeJudge()
    out = await run(env, good)
    assert good.calls == 1 and out.would_act
    for _ in range(3):  # the error streak starts over after a reset
        await run(env, bad)
    assert lines(env)[-1]["error_class"] == "RuntimeError"


async def test_success_resets_error_streak(env: dict[str, str]) -> None:
    env["AE_DECISION_MINUTE_CAP"] = "1000"
    bad, good = FakeJudge(exc=True), FakeJudge()
    for _ in range(4):
        await run(env, bad)
    await run(env, good)
    for _ in range(4):
        await run(env, bad)
    assert bad.calls == 8  # never reached 5 in a row


async def test_semaphore_limits_concurrency(env: dict[str, str]) -> None:
    env["AE_DECISION_MINUTE_CAP"] = "1000"
    running = peak = 0

    class Slow(FakeJudge):
        async def yes_no(self, point: str, state: Any, question: str) -> Verdict:
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            return self.verdict

    j = Slow()
    await asyncio.gather(*(run(env, j) for _ in range(10)))
    assert peak == ds.CONCURRENCY


# --- whitelist and log shape -----------------------------------------------


async def test_undeclared_and_mistyped_features_are_dropped(env: dict[str, str]) -> None:
    seen: dict[str, Any] = {}

    class Spy(FakeJudge):
        async def yes_no(self, point: str, state: Any, question: str) -> Verdict:
            seen.update(state)
            return self.verdict

    await ds.ask(
        "step_down", Spy(), "yes_no",
        {"files": 2, "kind": "code", "path": "/home/x", "who": "Zorbex", "files_bad": 1},
        baseline=False,
        environ=env, question="Q?",
    )  # fmt: skip
    assert seen == {"files": 2, "kind": "code"}
    row = lines(env)[0]
    assert row["dropped_features"] == 3 and row["features"] == {"files": 2, "kind": "code"}
    assert "Zorbex" not in json.dumps(row) and "/home" not in json.dumps(row)


async def test_log_line_shape(env: dict[str, str]) -> None:
    await run(env)
    (row,) = lines(env)
    assert set(row) == LOG_KEYS
    assert row["session"] == "s1" and row["judge"] == "fake" and row["judge_ver"] == "v1"
    assert row["probs"] == {"yes": 0.9, "no": 0.1} and row["baseline"] is False
    assert row["at"].endswith("Z") and row["error_class"] is None


async def test_log_sanitizes_free_text(env: dict[str, str]) -> None:
    j = FakeJudge(Verdict("some free text with spaces", {"a b c": 0.9}, 1, None, "fake"))
    ds.register_point("step_down", "yes_no", {"files": int})
    await run(env, j)
    row = lines(env)[0]
    assert row["answer"] == "?" and "free text" not in json.dumps(row)


async def test_guarded_summary_goes_to_judge_not_log(env: dict[str, str]) -> None:
    from ae_omni_policies import decision_advisor as da

    note = da.guard_summary("Fix a failing test", ["zorbex"], da.DEFAULT_TERM_ALLOW, [])
    assert note is not None
    ds.register_point("step_down", "yes_no", {"note": ds.GuardedSummary, "files": int})
    seen: dict[str, Any] = {}

    class Spy(FakeJudge):
        async def yes_no(self, point: str, state: Any, question: str) -> Verdict:
            seen.update(state)
            return self.verdict

    await ds.ask("step_down", Spy(), "yes_no", {"note": note, "files": 1}, baseline=False,
                 environ=env, question="Q?")  # fmt: skip
    assert seen["note"] == note
    assert "failing test" not in json.dumps(lines(env))


async def test_rule_judge_through_ask(env: dict[str, str]) -> None:
    out = await ds.ask("step_down", RuleJudge(lambda s: s["files"] > 1), "yes_no",
                       {"files": 2}, baseline=False, environ=env, question="Q?")  # fmt: skip
    assert out.would_act and out.verdict is not None and out.verdict.answer is True


# --- rotation and pruning --------------------------------------------------


def row(days_ago: float) -> str:
    at = (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return json.dumps({"at": at, "point": "p"})


def test_rotation_past_5mb_prunes_old(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = Path(env["AE_DECISION_SHADOW_FILE"])
    monkeypatch.setattr(ds, "MAX_BYTES", 200)
    path.write_text("\n".join([row(40), row(1), row(40) + " " * 200]) + "\n")
    assert ds.write_line({"at": row(0), "point": "new"}, path)
    old = path.with_name(path.name + ".1")
    kept = [json.loads(x) for x in old.read_text().splitlines()]
    assert len(kept) == 1 and kept[0]["point"] == "p"  # the 40-day lines are gone
    assert len(path.read_text().splitlines()) == 1


def test_daily_prune_without_rotation(env: dict[str, str]) -> None:
    path = Path(env["AE_DECISION_SHADOW_FILE"])
    path.write_text("\n".join([row(31), row(2)]) + "\n")
    ds.write_line({"at": row(0), "point": "n1"}, path)
    assert len(path.read_text().splitlines()) == 2  # old line pruned, new one added
    path.write_text(row(31) + "\n")  # within the same day nothing prunes again
    ds.write_line({"at": row(0), "point": "n2"}, path)
    assert len(path.read_text().splitlines()) == 2


def test_no_log_without_a_data_dir(env: dict[str, str]) -> None:
    del env["AE_DECISION_SHADOW_FILE"]
    assert ds.shadow_path(env) is None
    assert ds.write_line({"a": 1}, None) is False


# --- fire ------------------------------------------------------------------


async def test_fire_returns_at_once_and_completes(env: dict[str, str]) -> None:
    j = FakeJudge(delay=0.05)
    task = ds.fire(
        "step_down", j, "yes_no", {"files": 1}, baseline=False, environ=env, question="Q?"
    )
    assert task is not None and not task.done() and j.calls == 0
    assert task in ds._tasks
    out = await task
    assert out.would_act and task not in ds._tasks and len(lines(env)) == 1


def test_fire_without_a_loop_returns_none(env: dict[str, str]) -> None:
    assert ds.fire("step_down", FakeJudge(), "yes_no", {}, baseline=False, environ=env) is None


async def test_fire_never_raises_on_bad_args(env: dict[str, str]) -> None:
    task = ds.fire("nope", object(), "zzz", {"x": object()}, baseline=1, environ=env)  # type: ignore[arg-type]
    assert task is not None
    assert (await task).acted is False


# -- review round 2 (2026-11-06)


async def test_none_answer_is_not_would_act_at_threshold_zero(env: dict[str, str]) -> None:
    ds.register_point("step_down", "yes_no", {"files": int}, threshold=0.0)
    out = await run(env, FakeJudge(Verdict(None, {"yes": 0.5}, 1, None, "fake")))
    assert not out.would_act and not out.acted


async def test_timeout_covers_the_wait_for_a_permit(env: dict[str, str]) -> None:
    env["AE_DECISION_POINT_TIMEOUT_S"] = "0.1"
    env["AE_DECISION_MINUTE_CAP"] = "1000"
    slow = FakeJudge(delay=0.5)
    started = asyncio.get_running_loop().time()
    outs = await asyncio.gather(*(run(env, slow) for _ in range(ds.CONCURRENCY + 3)))
    assert asyncio.get_running_loop().time() - started < 0.4  # not 2 x the judge delay
    assert all(o.verdict is None for o in outs)
    ds._limits.errors, ds._limits.open_until = 0, 0.0  # the timeouts tripped the breaker
    # every permit came back
    fast = FakeJudge()
    outs = await asyncio.gather(*(run(env, fast) for _ in range(ds.CONCURRENCY + 1)))
    assert all(o.would_act for o in outs)


async def test_nan_probabilities_never_reach_the_log(env: dict[str, str]) -> None:
    out = await run(env, FakeJudge(Verdict(True, {"yes": float("nan")}, 1, None, "fake")))
    assert out.verdict is None
    row = lines(env)[0]
    assert row["error_class"] == "bad_shape" and row["probs"] == {}
    assert "NaN" not in Path(env["AE_DECISION_SHADOW_FILE"]).read_text()


def test_a_live_default_point_named_live_stays_live(env: dict[str, str]) -> None:
    ds.register_point("auto", "yes_no", {"files": int}, default="live")
    assert ds.mode_for("auto", env) == "live"  # not named
    env["AE_DECISION_POINTS"] = "auto:live"
    assert ds.mode_for("auto", env) == "live"
    env["AE_DECISION_POINTS"] = "auto:shadow"
    assert ds.mode_for("auto", env) == "shadow"
    env["AE_DECISION_POINTS"] = "auto:off"
    assert ds.mode_for("auto", env) == "off"


async def test_state_is_checked_once_before_the_wait(env: dict[str, str]) -> None:
    from ae_omni_policies import decision_advisor as da

    ds.register_point("step_down", "yes_no", {"note": ds.GuardedSummary})
    note = da.guard_summary("Fix a failing test", ["zorbex"], da.DEFAULT_TERM_ALLOW, [])
    assert note is not None
    seen: dict[str, Any] = {}

    class Evicting(FakeJudge):
        async def yes_no(self, point: str, state: Any, question: str) -> Verdict:
            da._minted.clear()  # evicted while the call waited
            from ae_omni_policies.judge import state_ok

            seen["ok"] = state_ok(point, state)
            return self.verdict

    await ds.ask("step_down", Evicting(), "yes_no", {"note": note}, baseline=False,
                 environ=env, question="Q?")  # fmt: skip
    assert seen["ok"] is True and da.MINTED_MAX == 4096


async def test_empty_probs_from_model_judge_is_bad_shape(env: dict[str, str]) -> None:
    out = await run(env, FakeJudge(Verdict(True, {}, 1, None, "fake")))
    assert out.verdict is None and not out.would_act
    assert lines(env)[0]["error_class"] == "bad_shape"


async def test_rule_judge_may_answer_without_probs(env: dict[str, str]) -> None:
    out = await run(env, RuleJudge(lambda s: s["files"] > 1))
    assert out.verdict is not None and out.verdict.answer is True
