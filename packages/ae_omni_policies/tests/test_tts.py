"""Natural voices for Speak replies: SSML, the caps, the bucket and the routes (2026-11-09)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest

from ae_omni_policies import tts

MP3 = b"ID3fake-mp3-bytes"
AVA = "en-US-AvaMultilingualNeural"
BODY = {"text": "Hello there.", "voice": AVA, "rate": 1.0}


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    tts.reset()
    yield
    tts.reset()


@pytest.fixture()
def azure(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Speech configured, with Azure answering 200 and the mp3 bytes."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=MP3)

    monkeypatch.setenv("AE_SPEECH_KEY", "secret-key")
    monkeypatch.setenv("AE_SPEECH_REGION", "westus2")
    tts.reset(httpx.MockTransport(handler))
    return seen


def test_ssml_escapes_markup_and_quotes() -> None:
    out = tts.ssml("a < b & \"c\" 'd'", AVA, 1.0)
    assert "a &lt; b &amp; &quot;c&quot; &apos;d&apos;" in out
    assert f'<voice name="{AVA}">' in out


def test_prosody_only_for_the_faster_rate() -> None:
    assert "prosody" not in tts.ssml("Hi.", AVA, 1.0)
    assert '<prosody rate="+25%">Hi.</prosody>' in tts.ssml("Hi.", AVA, 1.25)


def test_the_caps() -> None:
    assert tts.MAX_TEXT == 600
    assert tts.RATES == (1.0, 1.25)
    assert [v.name for v in tts.VOICES] == ["Ava", "Andrew", "Emma", "Brian"]


def test_the_bucket_empties_refills_and_is_per_user() -> None:
    for _ in range(tts.RATE_LIMIT_PER_MINUTE):
        assert tts.take_token("a", now=100.0)
    assert not tts.take_token("a", now=100.0)
    assert tts.take_token("b", now=100.0)
    assert tts.take_token("a", now=100.0 + 1.0)


def test_configured_needs_both_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AE_SPEECH_KEY", raising=False)
    monkeypatch.setenv("AE_SPEECH_REGION", "westus2")
    assert not tts.configured()
    monkeypatch.setenv("AE_SPEECH_KEY", "k")
    assert tts.configured()


async def test_voices_are_empty_when_not_configured(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("AE_SPEECH_REGION", raising=False)
    response = await client.get("/v1/ae/tts/voices")
    assert response.status_code == 200, response.text
    assert response.json() == {"voices": []}


async def test_voices_list_when_configured(
    client: httpx.AsyncClient, azure: list[httpx.Request]
) -> None:
    response = await client.get("/v1/ae/tts/voices")
    assert response.json()["voices"][0] == {"id": AVA, "name": "Ava"}
    assert len(response.json()["voices"]) == 4


async def test_speak_returns_mp3_and_sends_the_key_and_ssml(
    client: httpx.AsyncClient, azure: list[httpx.Request]
) -> None:
    response = await client.post("/v1/ae/tts", json={**BODY, "rate": 1.25})
    assert response.status_code == 200, response.text
    assert response.content == MP3
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["cache-control"] == "no-store"
    sent = azure[0]
    assert str(sent.url) == "https://westus2.tts.speech.microsoft.com/cognitiveservices/v1"
    assert sent.headers["Ocp-Apim-Subscription-Key"] == "secret-key"
    assert sent.headers["Content-Type"] == "application/ssml+xml"
    assert b'prosody rate="+25%"' in sent.content


async def test_azure_refusal_is_a_503_without_the_key(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AE_SPEECH_KEY", "secret-key")
    monkeypatch.setenv("AE_SPEECH_REGION", "westus2")
    tts.reset(httpx.MockTransport(lambda _r: httpx.Response(401, text="bad secret-key")))
    response = await client.post("/v1/ae/tts", json=BODY)
    assert response.status_code == 503, response.text
    assert "secret-key" not in response.text


async def test_not_configured_is_a_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AE_SPEECH_KEY", raising=False)
    response = await client.post("/v1/ae/tts", json=BODY)
    assert response.status_code == 503, response.text


@pytest.mark.parametrize(
    "patch",
    [
        {"text": ""},
        {"text": "   "},
        {"text": "x" * 601},
        {"voice": "en-US-NopeNeural"},
        {"rate": 2.0},
    ],
)
async def test_bad_input_is_a_422(
    client: httpx.AsyncClient, azure: list[httpx.Request], patch: dict[str, object]
) -> None:
    response = await client.post("/v1/ae/tts", json={**BODY, **patch})
    assert response.status_code == 422, response.text
    assert azure == []


async def test_extra_fields_are_refused(
    client: httpx.AsyncClient, azure: list[httpx.Request]
) -> None:
    response = await client.post("/v1/ae/tts", json={**BODY, "extra": 1})
    assert response.status_code == 422, response.text


async def test_an_empty_bucket_is_a_429(
    client: httpx.AsyncClient,
    azure: list[httpx.Request],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A fixed clock keeps the bucket from refilling while the requests run.
    real = tts.take_token
    monkeypatch.setattr(tts, "take_token", lambda user: real(user, now=1000.0))
    for _ in range(tts.RATE_LIMIT_PER_MINUTE):
        assert (await client.post("/v1/ae/tts", json=BODY)).status_code == 200
    response = await client.post("/v1/ae/tts", json=BODY)
    assert response.status_code == 429, response.text


async def test_a_foreign_origin_is_refused(
    client: httpx.AsyncClient, azure: list[httpx.Request]
) -> None:
    response = await client.post(
        "/v1/ae/tts", json=BODY, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403, response.text


def test_control_characters_are_dropped_but_whitespace_stays() -> None:
    out = tts.ssml("a\x00b\x08c\x0bd\x1fe\tf\ng\rh", AVA, 1.0)
    assert "abcde\tf\ng\rh" in out


def test_region_must_be_a_plain_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AE_SPEECH_KEY", "k")
    monkeypatch.setenv("AE_SPEECH_REGION", "evil.example/x")
    with caplog.at_level("WARNING"):
        assert not tts.configured()
    assert "AE_SPEECH_REGION" in caplog.text
    assert "evil.example" not in caplog.text
    monkeypatch.setenv("AE_SPEECH_REGION", "westus2")
    assert tts.configured()


async def test_a_bad_url_is_unavailable_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise httpx.InvalidURL("bad")

    monkeypatch.setenv("AE_SPEECH_KEY", "k")
    monkeypatch.setenv("AE_SPEECH_REGION", "westus2")
    monkeypatch.setattr(httpx.AsyncClient, "post", refuse)
    with pytest.raises(tts.TtsUnavailable):
        await tts.synthesize("Hi.", AVA, 1.0)


def test_the_bucket_map_is_bounded_and_evicts_idle_then_oldest() -> None:
    for n in range(tts.MAX_BUCKETS):
        assert tts.take_token(f"u{n}", now=float(n))
    assert len(tts._buckets) == tts.MAX_BUCKETS
    # Nobody idle for 10 minutes yet: the oldest (u0) goes.
    assert tts.take_token("new", now=300.0)
    assert len(tts._buckets) == tts.MAX_BUCKETS
    assert "u0" not in tts._buckets
    assert "u1" in tts._buckets
    # Everyone but the newest is now idle past 10 minutes: they all go.
    assert tts.take_token("later", now=300.0 + tts.IDLE_EVICT_S + 100)
    assert set(tts._buckets) == {"later"}


def test_the_daily_budget_runs_out_and_resets_after_24_hours() -> None:
    assert tts.DAILY_CHAR_BUDGET == 200_000
    assert tts.charge_chars("a", 150_000, now=10.0)
    assert not tts.charge_chars("a", 60_000, now=20.0)
    assert tts.charge_chars("a", 50_000, now=30.0)
    assert not tts.charge_chars("a", 1, now=40.0)
    assert tts.charge_chars("b", 1, now=40.0)
    assert tts.charge_chars("a", 1, now=10.0 + tts.BUDGET_WINDOW_S)


def test_the_budget_clock_defaults_to_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(tts.time, "monotonic", lambda: now[0])
    assert tts.charge_chars("a", tts.DAILY_CHAR_BUDGET)
    assert not tts.charge_chars("a", 1)
    now[0] += tts.BUDGET_WINDOW_S + 1
    assert tts.charge_chars("a", 1)


async def test_a_spent_budget_is_a_429(
    client: httpx.AsyncClient, azure: list[httpx.Request]
) -> None:
    assert tts.charge_chars(None, tts.DAILY_CHAR_BUDGET - 5)
    response = await client.post("/v1/ae/tts", json=BODY)
    assert response.status_code == 429, response.text
    assert "daily voice budget" in response.json()["error"]
