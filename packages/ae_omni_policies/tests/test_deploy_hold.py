"""The deploy hold's store, its file and the CLI ``deploy/auto-update.sh`` reads.

The routes are in ``test_router.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from ae_omni_policies import deploy_hold
from ae_omni_policies.deploy_hold import DeployHoldSaveError, DeployHoldStore

NOW = 1_792_464_000  # 2026-10-20T02:40:00Z


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # No hold file: the store stays in memory, so nothing writes to /data.
    monkeypatch.delenv(deploy_hold.STATE_FILE_ENV, raising=False)
    monkeypatch.setattr(deploy_hold, "DATA_DIR", Path("/nonexistent-ae-data"))
    deploy_hold.reset_store()
    yield
    deploy_hold.reset_store()


def _write(path: Path, body: object) -> None:
    path.write_text(json.dumps(body) if not isinstance(body, str) else body, encoding="utf-8")


# ── the store ───────────────────────────────────────────────────────────


def test_set_writes_the_file_and_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "state" / "hold.json"
    store = DeployHoldStore(path)
    assert store.read(now=NOW).hold is None
    hold = store.set(2, "reid", now=NOW)
    assert (hold.until, hold.set_at, hold.set_by) == (NOW + 7200, NOW, "reid")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "until": NOW + 7200,
        "set_at": NOW,
        "set_by": "reid",
    }
    again = DeployHoldStore(path).read(now=NOW + 60)
    assert again.hold == hold and again.error is None


def test_set_replaces_the_hold_in_force(tmp_path: Path) -> None:
    store = DeployHoldStore(tmp_path / "hold.json")
    store.set(3, "reid", now=NOW)
    store.set(1, "steve", now=NOW + 600)
    hold = store.read(now=NOW + 700).hold
    assert hold is not None
    assert (hold.until, hold.set_by) == (NOW + 600 + 3600, "steve")


@pytest.mark.parametrize("hours", [0, 4, -1])
def test_set_refuses_hours_outside_the_choices(hours: int) -> None:
    with pytest.raises(ValueError, match="one of"):
        DeployHoldStore(None).set(hours, "reid", now=NOW)


def test_clear_removes_the_file_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "hold.json"
    store = DeployHoldStore(path)
    store.set(1, "reid", now=NOW)
    before = store.clear(now=NOW + 5)
    assert before.hold is not None and before.hold.until == NOW + 3600
    assert not path.exists()
    assert store.read(now=NOW + 5).hold is None
    assert store.clear(now=NOW + 6).hold is None


def test_an_expired_hold_reads_as_none(tmp_path: Path) -> None:
    store = DeployHoldStore(tmp_path / "hold.json")
    store.set(1, "reid", now=NOW)
    assert store.read(now=NOW + 3599).hold is not None
    expired = store.read(now=NOW + 3600)
    assert expired.hold is None and expired.error is None


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("{not json", "not valid JSON"),
        ([1, 2], "expected a JSON object"),
        ({"version": 2, "until": NOW + 60, "set_at": NOW, "set_by": "reid"}, "version"),
        ({"version": 1, "until": "soon", "set_at": NOW, "set_by": "reid"}, "until"),
        ({"version": 1, "until": NOW + 60, "set_at": True, "set_by": "reid"}, "set_at"),
        ({"version": 1, "until": NOW + 60, "set_at": NOW, "set_by": ""}, "set_by"),
        ({"version": 1, "until": NOW + 60, "set_at": NOW}, "set_by"),
    ],
)
def test_a_corrupt_file_reads_as_none_with_the_reason(
    tmp_path: Path, body: object, reason: str
) -> None:
    path = tmp_path / "hold.json"
    _write(path, body)
    reading = DeployHoldStore(path).read(now=NOW)
    assert reading.hold is None
    assert reading.error is not None and reason in reading.error
    assert str(path) in reading.error
    assert path.exists(), "a read never removes or moves the file"


def test_a_hold_longer_than_the_cap_is_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "hold.json"
    cap = deploy_hold.MAX_HOURS * 3600 + 60
    _write(path, {"version": 1, "until": NOW + cap, "set_at": NOW, "set_by": "reid"})
    assert DeployHoldStore(path).read(now=NOW).hold is not None, "the minute of slack is kept"
    _write(path, {"version": 1, "until": NOW + cap + 1, "set_at": NOW, "set_by": "reid"})
    reading = DeployHoldStore(path).read(now=NOW)
    assert reading.hold is None
    assert reading.error is not None and "longer than 3 hours" in reading.error


def test_a_hold_ending_too_far_from_now_is_corrupt(tmp_path: Path) -> None:
    # 2026-10-19: the box clock jumped back after the set, or the file was edited by hand.
    path = tmp_path / "hold.json"
    far = 3 * 3600 + 60
    body = {"version": 1, "until": NOW + 3 * 3600, "set_at": NOW, "set_by": "reid"}
    _write(path, body)
    assert DeployHoldStore(path).read(now=NOW).hold is not None, "exactly 3 hours is held"
    _write(path, {**body, "until": NOW + far + 60, "set_at": NOW + 3600})
    reading = DeployHoldStore(path).read(now=NOW)
    assert reading.hold is None
    assert reading.error is not None and "more than 3 hours away" in reading.error
    assert str(far + 60) in reading.error


def test_clear_reads_and_removes_under_one_lock(tmp_path: Path) -> None:
    store = DeployHoldStore(tmp_path / "hold.json")
    store.set(1, "reid", now=NOW)
    with store._lock:  # an RLock: the read inside clear re-enters it
        before = store.clear(now=NOW + 1)
    assert before.hold is not None and before.hold.set_by == "reid"
    assert store.read(now=NOW + 1).hold is None


@pytest.mark.parametrize(
    ("until", "set_at", "reason"),
    [(10**15, 10**15 - 60, "until"), (10**11, 10**11 - 60, "until"), (NOW + 60, 10**15, "set_at")],
)
def test_an_out_of_range_time_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    until: int,
    set_at: int,
    reason: str,
) -> None:
    path = tmp_path / "hold.json"
    _write(path, {"version": 1, "until": until, "set_at": set_at, "set_by": "reid"})
    reading = DeployHoldStore(path).read(now=NOW)
    assert reading.hold is None
    assert reading.error is not None and reason in reading.error
    monkeypatch.setenv(deploy_hold.STATE_FILE_ENV, str(path))
    deploy_hold.reset_store()
    assert deploy_hold.main(now=NOW) == 2
    assert reason in capsys.readouterr().err


def test_a_blank_user_is_stored_as_local(tmp_path: Path) -> None:
    store = DeployHoldStore(tmp_path / "hold.json")
    assert store.set(1, "  ", now=NOW).set_by == "local"
    reading = store.read(now=NOW)
    assert reading.error is None and reading.hold is not None
    assert reading.hold.set_by == "local"
    assert store.set(1, " reid ", now=NOW).set_by == "reid"


def test_setting_or_clearing_replaces_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "hold.json"
    _write(path, "{not json")
    store = DeployHoldStore(path)
    store.set(1, "reid", now=NOW)
    assert store.read(now=NOW).error is None
    _write(path, "{not json")
    assert store.clear(now=NOW).error is not None
    assert not path.exists()


def test_an_unreadable_path_is_an_error_not_a_crash(tmp_path: Path) -> None:
    # A directory where the file should be: reading it is an OSError.
    path = tmp_path / "hold.json"
    path.mkdir()
    reading = DeployHoldStore(path).read(now=NOW)
    assert reading.hold is None
    assert reading.error is not None and "could not read" in reading.error


def test_without_a_path_the_hold_stays_in_memory() -> None:
    store = DeployHoldStore(None)
    store.set(2, "reid", now=NOW)
    assert store.read(now=NOW).hold is not None
    assert DeployHoldStore(None).read(now=NOW).hold is None
    store.clear()
    assert store.read(now=NOW).hold is None


def test_default_path_follows_env_then_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert deploy_hold._default_path() is None
    assert deploy_hold.store().path is None
    monkeypatch.setattr(deploy_hold, "DATA_DIR", tmp_path)
    assert deploy_hold._default_path() == tmp_path / "ae-deploy-hold.json"
    monkeypatch.setenv(deploy_hold.STATE_FILE_ENV, str(tmp_path / "x.json"))
    assert deploy_hold._default_path() == tmp_path / "x.json"


def test_the_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "hold.json"
    store = DeployHoldStore(path)
    store.set(1, "reid", now=NOW)
    store.set(3, "reid", now=NOW + 1)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hold.json"]


def test_a_failed_write_raises_and_keeps_what_was_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "hold.json"
    store = DeployHoldStore(path)
    store.set(1, "reid", now=NOW)

    def refuse(_src: str, _dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(deploy_hold.os, "replace", refuse)
    with pytest.raises(DeployHoldSaveError, match="could not write"):
        store.set(3, "steve", now=NOW + 1)
    hold = store.read(now=NOW + 1).hold
    assert hold is not None and hold.set_by == "reid"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hold.json"], "the temp file went"


def test_view_shapes_the_answer() -> None:
    held = deploy_hold.view(
        deploy_hold.Reading(deploy_hold.Hold(NOW + 7200, NOW, "reid")), now=NOW + 60
    )
    assert held.model_dump() == {
        "held": True,
        "until": "2026-10-20T04:40:00Z",
        "set_at": "2026-10-20T02:40:00Z",
        "set_by": "reid",
        "remaining_s": 7140,
        "choices": [1, 2, 3],
        "error": None,
    }
    broken = deploy_hold.view(deploy_hold.Reading(None, error="bad"), now=NOW)
    assert broken.model_dump() == {
        "held": False,
        "until": None,
        "set_at": None,
        "set_by": None,
        "remaining_s": 0,
        "choices": [1, 2, 3],
        "error": "bad",
    }
    assert deploy_hold.clock(NOW + 7200) == "04:40Z"


# ── the CLI ─────────────────────────────────────────────────────────────


def test_cli_prints_none_without_a_hold(capsys: pytest.CaptureFixture[str]) -> None:
    assert deploy_hold.main(now=NOW) == 0
    out = capsys.readouterr()
    assert out.out == "none\n" and out.err == ""


def test_cli_prints_the_hold_with_spaces_in_the_name_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "hold.json"
    monkeypatch.setenv(deploy_hold.STATE_FILE_ENV, str(path))
    deploy_hold.reset_store()
    deploy_hold.store().set(2, " Reid  Havens\t", now=NOW)
    assert deploy_hold.main(now=NOW + 1) == 0
    assert capsys.readouterr().out == f"held {NOW + 7200} Reid_Havens\n"
    assert deploy_hold.main(now=NOW + 7200) == 0
    assert capsys.readouterr().out == "none\n", "expired is none"


def test_cli_exits_2_with_the_reason_on_a_corrupt_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "hold.json"
    _write(path, "{not json")
    monkeypatch.setenv(deploy_hold.STATE_FILE_ENV, str(path))
    deploy_hold.reset_store()
    assert deploy_hold.main(now=NOW) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "not valid JSON" in out.err


def test_cli_runs_as_a_module(tmp_path: Path) -> None:
    # The way auto-update.sh runs it, through compose exec: the last stdout line counts.
    path = tmp_path / "hold.json"
    env = {**os.environ, deploy_hold.STATE_FILE_ENV: str(path)}
    run = [sys.executable, "-m", "ae_omni_policies.deploy_hold"]
    nothing = subprocess.run(run, env=env, capture_output=True, text=True, timeout=120)
    assert (nothing.returncode, nothing.stdout.splitlines()[-1]) == (0, "none")
    DeployHoldStore(path).set(1, "reid")
    held = subprocess.run(run, env=env, capture_output=True, text=True, timeout=120)
    assert held.returncode == 0, held.stderr
    word, until, user = held.stdout.splitlines()[-1].split(" ")
    assert (word, user) == ("held", "reid") and until.isdigit()
    _write(path, "[]")
    broken = subprocess.run(run, env=env, capture_output=True, text=True, timeout=120)
    assert broken.returncode == 2 and broken.stdout == ""
    assert "expected a JSON object" in broken.stderr


def test_cli_exits_2_for_a_hold_ending_too_far_from_now(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "hold.json"
    monkeypatch.setenv(deploy_hold.STATE_FILE_ENV, str(path))
    deploy_hold.reset_store()
    body = {"version": 1, "until": NOW + 3 * 3600, "set_at": NOW, "set_by": "reid"}
    _write(path, body)
    assert deploy_hold.main(now=NOW) == 0
    assert capsys.readouterr().out == f"held {NOW + 3 * 3600} reid\n"
    _write(path, {**body, "until": NOW + 3 * 3600 + 120, "set_at": NOW + 3600})
    assert deploy_hold.main(now=NOW) == 2
    out = capsys.readouterr()
    assert out.out == "" and "more than 3 hours away" in out.err
