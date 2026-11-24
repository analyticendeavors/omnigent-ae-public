"""Natural voices for Speak replies: SSML, the caps, the bucket and the routes (2026-11-09)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import httpx
import pytest

from ae_omni_policies import rocky_tts_bridge, tts

MP3 = b"ID3fake-mp3-bytes"
AVA = "en-US-AvaMultilingualNeural"
BODY = {"text": "Hello there.", "voice": AVA, "rate": 1.0}


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv(tts.ROCKY_SOCKET_ENV, raising=False)
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
    assert '<prosody rate="+50%">Hi.</prosody>' in tts.ssml("Hi.", AVA, 1.5)


def test_the_caps() -> None:
    assert tts.MAX_TEXT == 600
    assert tts.RATES == (1.0, 1.25, 1.5)
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
    assert response.json()["voices"][0] == {"id": AVA, "name": "Ava", "provider": "azure"}
    assert len(response.json()["voices"]) == 4


async def test_rocky_voice_appears_and_returns_wav_without_azure(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"voice": "rocky"})
        return httpx.Response(200, content=b"RIFFrocky", headers={"Content-Type": "audio/wav"})

    monkeypatch.setenv(tts.ROCKY_SOCKET_ENV, "/run/ae-rocky-tts/rocky.sock")
    monkeypatch.delenv(tts.KEY_ENV, raising=False)
    tts.reset(rocky_transport=httpx.MockTransport(handler))
    voices = await client.get("/v1/ae/tts/voices")
    assert voices.json() == {
        "voices": [{"id": tts.ROCKY_VOICE.id, "name": "Rocky", "provider": "rocky"}]
    }
    result = await client.post(
        "/v1/ae/tts", json={**BODY, "voice": tts.ROCKY_VOICE.id, "rate": 1.25}
    )
    assert result.status_code == 200, result.text
    assert result.content == b"RIFFrocky"
    assert result.headers["content-type"] == "audio/wav"
    assert result.headers["cache-control"] == "no-store"
    assert seen[-1].url.path == "/synthesize"
    assert seen[-1].read() == b'{"text":"Hello there.","rate":1.25}'


async def test_rocky_service_failure_does_not_expose_upstream_body(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(tts.ROCKY_SOCKET_ENV, "/run/ae-rocky-tts/rocky.sock")
    tts.reset(
        rocky_transport=httpx.MockTransport(
            lambda request: (
                httpx.Response(200, json={"voice": "rocky"})
                if request.url.path == "/health"
                else httpx.Response(503, text="private engine detail")
            )
        )
    )
    result = await client.post("/v1/ae/tts", json={**BODY, "voice": tts.ROCKY_VOICE.id})
    assert result.status_code == 503
    assert "private engine detail" not in result.text


def test_rocky_bridge_transforms_and_uses_repo_speed(tmp_path: Path) -> None:
    seen: list[object] = []

    class FakeModel:
        def tts_to_file(
            self, *, text: str, speaker_wav: str, language: str, file_path: str
        ) -> None:
            seen.extend((text, speaker_wav, language))
            Path(file_path).write_bytes(b"RIFFaudio")

    rocky = SimpleNamespace(
        rocky_transform=lambda text: f"Rocky: {text}",
        apply_speed=lambda wav, speed: seen.append(speed) or wav,
    )
    socket = tmp_path / "rocky.sock"
    with rocky_tts_bridge.RockyServer(
        socket, rocky, FakeModel(), tmp_path / "reference.wav"
    ) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket))) as bridge:
                assert bridge.get("http://rocky/health").json() == {"voice": "rocky"}
                response = bridge.post(
                    "http://rocky/synthesize", json={"text": "Hello there.", "rate": 1.25}
                )
                assert response.status_code == 200
                assert response.content == b"RIFFaudio"
                # Fastest (1.5) is relative to Rocky's own base speed of 1.5: factor 2.25.
                fastest = bridge.post(
                    "http://rocky/synthesize", json={"text": "Hello there.", "rate": 1.5}
                )
                assert fastest.status_code == 200
                assert (
                    bridge.post(
                        "http://rocky/synthesize", json={"text": "", "rate": 1.0}
                    ).status_code
                    == 400
                )
        finally:
            server.shutdown()
            worker.join(timeout=3)
    assert seen == [
        "Rocky: Hello there.",
        str(tmp_path / "reference.wav"),
        "en",
        1.875,
        "Rocky: Hello there.",
        str(tmp_path / "reference.wav"),
        "en",
        2.25,
    ]


def test_rocky_socket_dir_rejects_untrusted_parent(tmp_path: Path) -> None:
    parent = tmp_path / "socket"
    parent.mkdir(mode=0o777)
    parent.chmod(0o777)
    with pytest.raises(ValueError, match="private and owned"):
        rocky_tts_bridge.prepare_socket_dir(parent / "rocky.sock")


def test_rocky_health_during_synthesis(tmp_path: Path) -> None:
    synthesizing = Event()
    release = Event()

    class SlowModel:
        def tts_to_file(self, *, file_path: str, **_kwargs: object) -> None:
            synthesizing.set()
            assert release.wait(timeout=5)
            Path(file_path).write_bytes(b"RIFFaudio")

    rocky = SimpleNamespace(rocky_transform=lambda text: text, apply_speed=lambda wav, _speed: wav)
    socket = tmp_path / "rocky.sock"
    with rocky_tts_bridge.RockyServer(
        socket, rocky, SlowModel(), tmp_path / "reference.wav"
    ) as server:
        worker = Thread(target=server.serve_forever)
        worker.start()
        try:
            with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket))) as bridge:
                speaking = Thread(
                    target=lambda: bridge.post(
                        "http://rocky/synthesize", json={"text": "Hello.", "rate": 1.0}
                    )
                )
                speaking.start()
                assert synthesizing.wait(timeout=3)
                assert bridge.get("http://rocky/health", timeout=1).json() == {"voice": "rocky"}
                release.set()
                speaking.join(timeout=3)
                assert not speaking.is_alive()
        finally:
            release.set()
            server.shutdown()
            worker.join(timeout=3)


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


def test_synthesize_reuses_one_client_and_reset_closes_it(azure: list[httpx.Request]) -> None:
    import asyncio

    async def run() -> None:
        await tts.synthesize("One.", AVA, 1.0)
        first = tts._client
        await tts.synthesize("Two.", AVA, 1.0)
        assert first is not None and tts._client is first
        assert len(azure) == 2
        tts.reset()
        assert tts._client is None
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert first.is_closed

    asyncio.run(run())


def test_a_new_event_loop_gets_a_new_client_without_reset(azure: list[httpx.Request]) -> None:
    import asyncio

    clients: list[object] = []

    async def once() -> None:
        await tts.synthesize("One.", AVA, 1.0)
        clients.append(tts._client)

    asyncio.run(once())
    asyncio.run(once())
    assert clients[0] is not clients[1]
    assert len(azure) == 2


def test_a_transport_error_retries_once_with_a_fresh_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadError("stale keepalive", request=request)
        return httpx.Response(200, content=MP3)

    monkeypatch.setenv("AE_SPEECH_KEY", "secret-key")
    monkeypatch.setenv("AE_SPEECH_REGION", "westus2")
    tts.reset(httpx.MockTransport(handler))
    assert asyncio.run(tts.synthesize("Hi.", AVA, 1.0)) == MP3
    assert len(calls) == 2


def test_a_second_transport_error_is_unavailable_and_a_refusal_never_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    calls: list[int] = []

    def broken(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setenv("AE_SPEECH_KEY", "secret-key")
    monkeypatch.setenv("AE_SPEECH_REGION", "westus2")
    tts.reset(httpx.MockTransport(broken))
    with pytest.raises(tts.TtsUnavailable):
        asyncio.run(tts.synthesize("Hi.", AVA, 1.0))
    assert len(calls) == 2

    refused: list[int] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        refused.append(1)
        return httpx.Response(401)

    tts.reset(httpx.MockTransport(refuse))
    with pytest.raises(tts.TtsUnavailable):
        asyncio.run(tts.synthesize("Hi.", AVA, 1.0))
    assert len(refused) == 1


def test_a_runtime_error_from_the_client_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    def closed(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("Event loop is closed")

    monkeypatch.setenv("AE_SPEECH_KEY", "secret-key")
    monkeypatch.setenv("AE_SPEECH_REGION", "westus2")
    tts.reset(httpx.MockTransport(closed))
    with pytest.raises(tts.TtsUnavailable):
        asyncio.run(tts.synthesize("Hi.", AVA, 1.0))


def test_reset_keeps_the_close_task_until_it_finishes(azure: list[httpx.Request]) -> None:
    import asyncio

    async def run() -> None:
        await tts.synthesize("One.", AVA, 1.0)
        tts.reset()
        assert len(tts._closing) == 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not tts._closing

    asyncio.run(run())


def test_concurrent_stale_failures_do_not_close_each_others_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    seen: dict[str, int] = {}
    arrived = [0]
    gate: list[asyncio.Event] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        text = request.content.decode()
        seen[text] = seen.get(text, 0) + 1
        if seen[text] == 1:
            # Both first attempts are in flight before either fails.
            arrived[0] += 1
            if arrived[0] == 2:
                gate[0].set()
            await gate[0].wait()
            raise httpx.ReadError("stale keepalive", request=request)
        await asyncio.sleep(0.01)
        return httpx.Response(200, content=MP3)

    monkeypatch.setenv("AE_SPEECH_KEY", "secret-key")
    monkeypatch.setenv("AE_SPEECH_REGION", "westus2")
    tts.reset(httpx.MockTransport(handler))

    async def run() -> list[bytes]:
        gate.append(asyncio.Event())
        return list(
            await asyncio.gather(
                tts.synthesize("A sentence.", AVA, 1.0), tts.synthesize("B sentence.", AVA, 1.0)
            )
        )

    assert asyncio.run(run()) == [MP3, MP3]


def test_a_timeout_does_not_drop_the_shared_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    monkeypatch.setenv("AE_SPEECH_KEY", "secret-key")
    monkeypatch.setenv("AE_SPEECH_REGION", "westus2")
    tts.reset(httpx.MockTransport(slow))

    async def run() -> None:
        with pytest.raises(tts.TtsUnavailable):
            await tts.synthesize("Hi.", AVA, 1.0)
        assert tts._client is not None and not tts._client.is_closed

    asyncio.run(run())
