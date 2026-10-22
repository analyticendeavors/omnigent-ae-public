"""The stale-while-revalidate cache behind the Dashboard's slow reads (2026-09-26)."""

from __future__ import annotations

import asyncio
import gc
import time

import pytest

from ae_omni_policies.swr import SwrCache


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Source:
    """A load that counts its calls and, when told to, waits for a release."""

    def __init__(self) -> None:
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.fail: Exception | None = None

    async def __call__(self) -> str:
        self.calls += 1
        call = self.calls
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return f"v{call}"


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_the_first_read_is_awaited_and_then_held() -> None:
    clock = Clock()
    cache = SwrCache[str, str]("t", 20.0, clock=clock)
    source = Source()
    assert await cache.get("k", source) == "v1"
    clock.now += 19
    assert await cache.get("k", source) == "v1"
    assert source.calls == 1


async def test_a_stale_value_returns_at_once_and_one_refresh_replaces_it() -> None:
    clock = Clock()
    cache = SwrCache[str, str]("t", 20.0, clock=clock)
    source = Source()
    await cache.get("k", source)
    clock.now += 21
    source.gate = asyncio.Event()
    started = time.monotonic()
    # Three readers while the refresh is held open: all get the old value now,
    # and they share one load.
    answers = [await cache.get("k", source) for _ in range(3)]
    assert time.monotonic() - started < 0.5
    assert answers == ["v1", "v1", "v1"]
    await settle()
    assert source.calls == 2
    assert cache.refreshing("k")
    assert await cache.get("k", source) == "v1"
    await settle()
    assert source.calls == 2
    source.gate.set()
    await settle()
    assert not cache.refreshing("k")
    assert await cache.get("k", source) == "v2"
    assert source.calls == 2


async def test_concurrent_first_reads_share_one_load() -> None:
    cache = SwrCache[str, str]("t", 20.0)
    source = Source()
    source.gate = asyncio.Event()
    readers = [asyncio.create_task(cache.get("k", source)) for _ in range(4)]
    await settle()
    source.gate.set()
    assert await asyncio.gather(*readers) == ["v1"] * 4
    assert source.calls == 1


async def test_fresh_awaits_a_new_load() -> None:
    cache = SwrCache[str, str]("t", 20.0)
    source = Source()
    await cache.get("k", source)
    assert await cache.get("k", source, fresh=True) == "v2"
    assert await cache.get("k", source) == "v2"


async def test_a_failed_refresh_keeps_the_last_value_and_says_why() -> None:
    clock = Clock()
    cache = SwrCache[str, str]("t", 20.0, clock=clock, describe=str)
    source = Source()
    await cache.get("k", source)
    clock.now += 21
    source.fail = RuntimeError("store down")
    assert await cache.get("k", source) == "v1"
    await settle()
    assert cache.last_error("k") == "store down"
    assert await cache.get("k", source) == "v1"
    source.fail = None
    await settle()
    clock.now += 21
    await cache.get("k", source)
    await settle()
    assert cache.last_error("k") is None
    assert await cache.get("k", source) == "v4"


async def test_an_awaited_read_that_fails_raises_to_its_caller() -> None:
    cache = SwrCache[str, str]("t", 20.0)
    source = Source()
    source.fail = RuntimeError("down")
    with pytest.raises(RuntimeError, match="down"):
        await cache.get("k", source)


async def test_past_max_stale_the_read_is_awaited_again() -> None:
    clock = Clock()
    cache = SwrCache[str, str]("t", 20.0, max_stale_s=300.0, clock=clock)
    source = Source()
    await cache.get("k", source)
    clock.now += 301
    source.fail = RuntimeError("down")
    with pytest.raises(RuntimeError):
        await cache.get("k", source)


async def test_invalidate_drops_values_and_a_load_already_running_stores_nothing() -> None:
    clock = Clock()
    cache = SwrCache[str, str]("t", 20.0, clock=clock)
    source = Source()
    await cache.get("k", source)
    clock.now += 21
    source.gate = asyncio.Event()
    assert await cache.get("k", source) == "v1"  # refresh v2 starts, held open
    cache.invalidate()  # a Park lands while it runs
    source.gate.set()
    await settle()
    # v2 began before the Park: it is not kept; the next read loads again.
    assert await cache.get("k", source) == "v3"


async def test_mark_stale_keeps_the_value_and_refreshes_on_the_next_read() -> None:
    cache = SwrCache[str, str]("t", 20.0)
    source = Source()
    await cache.get("k", source)
    cache.mark_stale()
    assert await cache.get("k", source) == "v1"
    await settle()
    assert await cache.get("k", source) == "v2"


async def test_background_failures_are_retrieved_not_left_unobserved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    cache = SwrCache[str, str]("t", 20.0, clock=clock)
    source = Source()
    await cache.get("k", source)
    clock.now += 21
    source.fail = RuntimeError("secret-token-in-a-url")
    unobserved: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: unobserved.append(context))
    try:
        await cache.get("k", source)
        await settle()
        gc.collect()
        await settle()
    finally:
        loop.set_exception_handler(None)
    assert unobserved == []
    assert "a refresh failed (RuntimeError)" in caplog.text
    assert "secret-token-in-a-url" not in caplog.text


async def test_reset_cancels_running_loads() -> None:
    cache = SwrCache[str, str]("t", 20.0)
    source = Source()
    source.gate = asyncio.Event()
    reader = asyncio.create_task(cache.get("k", source))
    await settle()
    cache.reset()
    with pytest.raises(asyncio.CancelledError):
        await reader
    assert not cache.refreshing("k")
