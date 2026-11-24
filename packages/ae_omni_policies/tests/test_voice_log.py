"""The voice log upload: auth, caps, reasons, the rate limit, the file and its rotation."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from ae_omni_policies import auth, voice_log

OWNER = {"X-Forwarded-Email": "owner@example.com"}
LINES = [
    {"t": 1700000000000, "event": "mic start"},
    {"t": 1700000001000, "event": "final", "detail": "hi"},
]
BODY = {"reason": "manual", "chat_id": "chat-1", "lines": LINES}


@pytest.fixture(autouse=True)
def _dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    target = tmp_path / "logs"
    monkeypatch.setenv(voice_log.DIR_ENV, str(target))
    voice_log.reset()
    yield target
    voice_log.reset()


def _files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*")) if directory.is_dir() else []


async def test_identity_is_required(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, _dir: Path
) -> None:
    monkeypatch.delenv("OMNIGENT_LOCAL_SINGLE_USER")
    auth.reset_provider_cache()
    refused = await client.post("/v1/ae/voice-log", json=BODY)
    assert refused.status_code == 401
    assert _files(_dir) == []
    ok = await client.post("/v1/ae/voice-log", json=BODY, headers=OWNER)
    assert ok.status_code == 200, ok.text


async def test_a_foreign_origin_is_refused(client: httpx.AsyncClient, _dir: Path) -> None:
    response = await client.post(
        "/v1/ae/voice-log", json=BODY, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403, response.text
    assert _files(_dir) == []


async def test_an_upload_is_appended_as_one_json_line(
    client: httpx.AsyncClient, _dir: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    for _ in range(2):
        response = await client.post("/v1/ae/voice-log", json=BODY, headers=OWNER)
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "lines": 2}
    files = _files(_dir)
    assert len(files) == 1 and files[0].name.endswith(".jsonl")
    rows = [json.loads(row) for row in files[0].read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["user"] == "owner@example.com"
    assert rows[0]["reason"] == "manual"
    assert rows[0]["chat_id"] == "chat-1"
    assert rows[0]["lines"] == LINES
    assert "received_at" in rows[0]
    assert "mic start" not in capfd.readouterr().out
    assert oct(files[0].stat().st_mode & 0o777) == "0o600"


async def test_users_get_separate_files(client: httpx.AsyncClient, _dir: Path) -> None:
    await client.post("/v1/ae/voice-log", json=BODY, headers=OWNER)
    await client.post(
        "/v1/ae/voice-log", json=BODY, headers={"X-Forwarded-Email": "../member@example.com"}
    )
    names = [f.name for f in _files(_dir)]
    assert len(names) == 2
    assert all("/" not in n and not n.startswith(".") for n in names)


@pytest.mark.parametrize("reason", sorted(voice_log.REASONS))
async def test_every_allowed_reason_is_kept(client: httpx.AsyncClient, reason: str) -> None:
    response = await client.post("/v1/ae/voice-log", json={**BODY, "reason": reason})
    assert response.status_code == 200, response.text


def test_page_hidden_is_an_allowed_reason() -> None:
    assert "page_hidden" in voice_log.REASONS


@pytest.mark.parametrize(
    "patch",
    [
        {"reason": "because"},
        {"reason": ""},
        {"lines": [{"t": 1, "event": "x" * (voice_log.MAX_EVENT + 1)}]},
        {"lines": [{"t": 1, "event": "x", "detail": "y" * (voice_log.MAX_DETAIL + 1)}]},
        {"lines": [{"t": "soon", "event": "x"}]},
        {"lines": [{"t": 1, "event": "x", "extra": 1}]},
        {"lines": [{"t": 1, "event": "x"}] * (voice_log.MAX_LINES + 1)},
        {"chat_id": "c" * (voice_log.MAX_CHAT_ID + 1)},
        {"extra": 1},
    ],
)
async def test_bad_input_is_a_422_and_writes_nothing(
    client: httpx.AsyncClient, _dir: Path, patch: dict[str, object]
) -> None:
    response = await client.post("/v1/ae/voice-log", json={**BODY, **patch})
    assert response.status_code == 422, response.text
    assert _files(_dir) == []


async def test_exactly_the_line_cap_is_kept(client: httpx.AsyncClient, _dir: Path) -> None:
    lines = [{"t": i, "event": "e"} for i in range(voice_log.MAX_LINES)]
    response = await client.post("/v1/ae/voice-log", json={**BODY, "lines": lines})
    assert response.status_code == 200, response.text
    assert response.json()["lines"] == voice_log.MAX_LINES


async def test_not_json_is_a_422(client: httpx.AsyncClient, _dir: Path) -> None:
    response = await client.post(
        "/v1/ae/voice-log", content=b"{nope", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    assert _files(_dir) == []


async def test_a_body_over_64_kb_is_a_413(client: httpx.AsyncClient, _dir: Path) -> None:
    big = {
        **BODY,
        "lines": [{"t": i, "event": "e", "detail": "d" * 290} for i in range(300)],
    }
    assert len(json.dumps(big)) > voice_log.MAX_BODY_BYTES
    response = await client.post("/v1/ae/voice-log", json=big)
    assert response.status_code == 413, response.text
    assert _files(_dir) == []


async def test_the_twelfth_upload_passes_and_the_next_is_a_429(
    client: httpx.AsyncClient, _dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = voice_log.allow
    monkeypatch.setattr(voice_log, "allow", lambda user: real(user, now=1000.0))
    for _ in range(voice_log.RATE_LIMIT):
        assert (await client.post("/v1/ae/voice-log", json=BODY)).status_code == 200
    assert (await client.post("/v1/ae/voice-log", json=BODY)).status_code == 429


def test_the_window_refills_and_is_per_user() -> None:
    for _ in range(voice_log.RATE_LIMIT):
        assert voice_log.allow("a", now=100.0)
    assert not voice_log.allow("a", now=100.0)
    assert voice_log.allow("b", now=100.0)
    assert voice_log.allow("a", now=100.0 + voice_log.RATE_WINDOW_S + 1)


def test_the_user_table_stays_bounded() -> None:
    for i in range(voice_log.MAX_USERS + 20):
        voice_log.allow(f"u{i}", now=float(i))
    assert len(voice_log._hits) <= voice_log.MAX_USERS


async def test_no_directory_is_a_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(voice_log.DIR_ENV, "")
    response = await client.post("/v1/ae/voice-log", json=BODY)
    assert response.status_code == 503, response.text


def test_rotation_keeps_the_active_file_and_four_older(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(voice_log, "ROTATE_BYTES", 400)
    upload = voice_log.VoiceLogUpload(
        reason="manual", lines=[voice_log.VoiceLine(t=1, event="e", detail="d" * 200)]
    )
    for _ in range(40):
        assert voice_log.append("owner", upload, tmp_path)
    names = sorted(f.name for f in tmp_path.iterdir())
    assert len(names) == voice_log.KEEP_FILES
    stem = voice_log._file_stem("owner")
    assert names == sorted(
        [f"{stem}.jsonl"] + [f"{stem}.jsonl.{n}" for n in range(1, voice_log.KEEP_FILES)]
    )
    # The newest rows sit in the active file, the oldest kept ones in the highest number.
    assert (tmp_path / f"{stem}.jsonl").stat().st_size < 700


def test_append_without_a_directory_stores_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(voice_log.DIR_ENV, "")
    upload = voice_log.VoiceLogUpload(reason="manual", lines=[])
    assert voice_log.append("owner", upload) is False


async def test_a_refused_upload_does_not_spend_quota(
    client: httpx.AsyncClient, _dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = voice_log.allow
    monkeypatch.setattr(voice_log, "allow", lambda user: real(user, now=1000.0))
    for _ in range(voice_log.RATE_LIMIT * 2):
        bad = await client.post("/v1/ae/voice-log", json={**BODY, "reason": "nope"})
        assert bad.status_code == 422
    big = {**BODY, "lines": [{"t": i, "event": "e", "detail": "d" * 290} for i in range(300)]}
    assert (await client.post("/v1/ae/voice-log", json=big)).status_code == 413
    for _ in range(voice_log.RATE_LIMIT):
        assert (await client.post("/v1/ae/voice-log", json=BODY)).status_code == 200
    assert (await client.post("/v1/ae/voice-log", json=BODY)).status_code == 429


@pytest.mark.parametrize(
    "raw_t",
    ['"1700000000000"', "NaN", "Infinity", "-Infinity", "true", "null"],
)
async def test_t_must_be_a_finite_number(
    client: httpx.AsyncClient, _dir: Path, raw_t: str
) -> None:
    raw = '{"reason":"manual","lines":[{"t":' + raw_t + ',"event":"x"}]}'
    response = await client.post(
        "/v1/ae/voice-log", content=raw.encode(), headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    assert _files(_dir) == []


async def test_integer_and_float_t_are_kept_as_strict_json(
    client: httpx.AsyncClient, _dir: Path
) -> None:
    lines = [{"t": 1700000000000, "event": "a"}, {"t": 1700000000.5, "event": "b"}]
    response = await client.post("/v1/ae/voice-log", json={**BODY, "lines": lines})
    assert response.status_code == 200, response.text
    (active,) = _files(_dir)
    json.loads(active.read_text(), parse_constant=lambda c: pytest.fail(c))


def test_a_record_never_holds_nan(tmp_path: Path) -> None:
    upload = voice_log.VoiceLogUpload.model_construct(
        reason="manual",
        chat_id=None,
        lines=[voice_log.VoiceLine.model_construct(t=float("nan"), event="x", detail=None)],
    )
    with pytest.raises(ValueError, match="Out of range"):
        voice_log.append("u", upload, tmp_path)
    assert list(tmp_path.glob("*")) == []


def test_the_owner_is_applied_to_the_directory_and_each_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(
        voice_log.os, "fchown", lambda fd, u, g: calls.append((os.fstat(fd).st_ino, u, g))
    )
    monkeypatch.setenv(voice_log.OWNER_ENV, "1000:1001")
    base = tmp_path / "out"
    upload = voice_log.VoiceLogUpload.model_validate(BODY)
    assert voice_log.append("u", upload, base)
    active = next(base.glob("*.jsonl"))
    assert calls == [(base.stat().st_ino, 1000, 1001), (active.stat().st_ino, 1000, 1001)]
    assert base.stat().st_mode & 0o777 == 0o700
    assert active.stat().st_mode & 0o777 == 0o600


def test_a_chown_that_is_not_permitted_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise PermissionError

    monkeypatch.setattr(voice_log.os, "fchown", refuse)
    monkeypatch.setenv(voice_log.OWNER_ENV, "0:0")
    assert voice_log.append("u", voice_log.VoiceLogUpload.model_validate(BODY), tmp_path / "d")
    assert len(list((tmp_path / "d").glob("*.jsonl"))) == 1


def test_an_unset_or_bad_owner_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(voice_log.os, "fchown", lambda *a: calls.append(a))
    for value in (
        None,
        "",
        "abc",
        "1:x",
        "1000",
        "-1:0",
        "0:4294967295",
        "99999999999999999999:0",
    ):
        if value is None:
            monkeypatch.delenv(voice_log.OWNER_ENV, raising=False)
        else:
            monkeypatch.setenv(voice_log.OWNER_ENV, value)
        assert voice_log.append("u", voice_log.VoiceLogUpload.model_validate(BODY), tmp_path)
    assert calls == []


def test_the_real_chown_to_ones_own_ids_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(voice_log.OWNER_ENV, f"{os.getuid()}:{os.getgid()}")
    assert voice_log.append("u", voice_log.VoiceLogUpload.model_validate(BODY), tmp_path / "x")
    (active,) = (tmp_path / "x").glob("*.jsonl")
    assert active.stat().st_uid == os.getuid()


def test_an_owner_too_large_for_the_platform_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert voice_log._MAX_ID == 2**32 - 2
    monkeypatch.setenv(voice_log.OWNER_ENV, "4294967294:0")
    assert voice_log._owner() == (2**32 - 2, 0)
    monkeypatch.setenv(voice_log.OWNER_ENV, "0:4294967295")
    assert voice_log._owner() is None

    def overflow(*_: object, **__: object) -> None:
        raise OverflowError

    # Even if a value slipped past the range check, the upload still succeeds.
    monkeypatch.setattr(voice_log.os, "fchown", overflow)
    monkeypatch.setenv(voice_log.OWNER_ENV, "0:0")
    assert voice_log.append("u", voice_log.VoiceLogUpload.model_validate(BODY), tmp_path / "d")


def test_a_symlink_is_neither_chowned_nor_chmodded_nor_written_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "victim"
    target.write_text("keep")
    target.chmod(0o644)
    link = tmp_path / "link"
    link.symlink_to(target)
    calls: list[object] = []
    monkeypatch.setattr(voice_log.os, "fchown", lambda *a, **k: calls.append(a))
    monkeypatch.setenv(voice_log.OWNER_ENV, "1000:1001")
    voice_log._own_dir(link, 0o600, created=True)
    assert calls == []
    assert target.stat().st_mode & 0o777 == 0o644
    # A log file that is a symlink is not opened through.
    base = tmp_path / "d"
    base.mkdir()
    (base / f"{voice_log._file_stem('u')}.jsonl").symlink_to(target)
    with pytest.raises(OSError):
        voice_log.append("u", voice_log.VoiceLogUpload.model_validate(BODY), base)
    assert target.read_text() == "keep"


def test_ownership_never_goes_through_a_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A name swapped for a symlink between the open and the chmod must not redirect it,
    # so the path-based calls are never used.
    def path_call(*_: object, **__: object) -> None:
        raise AssertionError("path-based chown or chmod used")

    monkeypatch.setenv(voice_log.OWNER_ENV, f"{os.getuid()}:{os.getgid()}")
    base = tmp_path / "d"
    base.mkdir(mode=0o755)
    base.chmod(0o755)
    active = base / f"{voice_log._file_stem('u')}.jsonl"
    active.write_text("")
    active.chmod(0o644)
    monkeypatch.setattr(voice_log.os, "chown", path_call)
    monkeypatch.setattr(voice_log.os, "chmod", path_call)
    assert voice_log.append("u", voice_log.VoiceLogUpload.model_validate(BODY), base)
    assert active.stat().st_mode & 0o777 == 0o600
    # The directory already belongs to the owner, so fchmod narrows it.
    assert base.stat().st_mode & 0o777 == 0o700


def test_an_existing_directory_is_never_chowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bind-mounted log directory whose host source was swapped for a symlink looks like
    # a real directory here, so only a directory this call created is chowned.
    calls: list[int] = []
    monkeypatch.setattr(voice_log.os, "fchown", lambda fd, u, g: calls.append(os.fstat(fd).st_ino))
    monkeypatch.setenv(voice_log.OWNER_ENV, f"{os.getuid() + 1}:{os.getgid()}")
    base = tmp_path / "d"
    base.mkdir(mode=0o755)
    base.chmod(0o755)
    assert voice_log.append("u", voice_log.VoiceLogUpload.model_validate(BODY), base)
    (active,) = base.glob("*.jsonl")
    assert calls == [active.stat().st_ino]
    # Not the owner's directory, so its mode is left alone too.
    assert base.stat().st_mode & 0o777 == 0o755


def test_a_fifo_at_the_log_name_is_refused_without_blocking(tmp_path: Path) -> None:
    base = tmp_path / "d"
    base.mkdir()
    os.mkfifo(base / f"{voice_log._file_stem('u')}.jsonl")
    with pytest.raises(OSError):
        voice_log.append("u", voice_log.VoiceLogUpload.model_validate(BODY), base)


def test_a_hard_linked_log_file_is_not_chmodded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "victim"
    target.write_text("")
    target.chmod(0o644)
    base = tmp_path / "d"
    base.mkdir()
    os.link(target, base / f"{voice_log._file_stem('u')}.jsonl")
    monkeypatch.setenv(voice_log.OWNER_ENV, f"{os.getuid()}:{os.getgid()}")
    assert voice_log.append("u", voice_log.VoiceLogUpload.model_validate(BODY), base)
    assert target.stat().st_mode & 0o777 == 0o644
