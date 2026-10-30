"""A small stale-while-revalidate cache for the Dashboard's slow reads.

Why (2026-09-26): the owner waited 3 to 5 seconds on every open of the Dashboard
on their iPhone. Measured on the box that day with their login (two GETs each):
``/v1/ae/prs`` 2321 ms cold and 12 ms warm (the tracked-PR scan, one loopback
per session), ``/v1/ae/lot?status=open`` about 320 ms on every call
(task-store through Cloudflare, uncached), ``/v1/ae/lot/tags?icons=true``
471 ms cold and 122 ms warm. A read past its TTL used to be awaited, so the
first open after a minute away paid the whole scan.

The rule here: a value under ``ttl_s`` old is served as is; an older one is
served at once and one background load replaces it (single-flight: at most
one load per key in flight, and concurrent callers share it); only a key
with nothing held (the first read after a server start, or after
:meth:`SwrCache.invalidate`) is awaited. ``fresh=True`` awaits a load, sharing
one already in flight. A load that fails keeps the last good value, and
:meth:`SwrCache.last_error` says why until a load succeeds. ``max_stale_s``
caps how old a value may be and still be served while the source is failing.

Background loads run as tasks this module holds a reference to (so they are
not collected mid-flight) and whose exceptions it always retrieves (so none
is ever "never retrieved"). A load started before :meth:`invalidate` never
stores its result: a Park must not be overwritten by a read that started
before it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Awaitable, Callable, Hashable

_logger = logging.getLogger(__name__)


def _class_name(exc: BaseException) -> str:
    # The class name only by default: a message could carry a URL with a credential in it.
    return type(exc).__name__


class SwrCache[K: Hashable, V]:
    """Values by key, served stale while one background load refreshes them.

    :param name: What the cache holds, for log lines.
    :param ttl_s: How long a value is served without starting a refresh.
    :param max_stale_s: Past this age a value is not served; the read is awaited.
        ``None`` serves the last good value however old.
    :param describe: How a failed load is reported by :meth:`last_error`.
    :param clock: Monotonic seconds (tests pass their own).
    """

    def __init__(
        self,
        name: str,
        ttl_s: float,
        *,
        max_stale_s: float | None = None,
        describe: Callable[[BaseException], str] = _class_name,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.ttl_s = ttl_s
        self.max_stale_s = max_stale_s
        self._describe = describe
        self._clock = clock
        self._entries: dict[K, tuple[float, V]] = {}
        self._inflight: dict[K, asyncio.Task[V]] = {}
        self._errors: dict[K, str] = {}
        self._tasks: set[asyncio.Task[V]] = set()
        self._generation = 0

    async def get(self, key: K, load: Callable[[], Awaitable[V]], *, fresh: bool = False) -> V:
        """The value for *key*: held, held while a refresh runs, or awaited.

        :raises Exception: Whatever *load* raised, when the read was awaited.
        """
        held = self._entries.get(key)
        if not fresh and held is not None:
            age = self._clock() - held[0]
            if age < self.ttl_s:
                return held[1]
            if self.max_stale_s is None or age < self.max_stale_s:
                self._start(key, load)
                return held[1]
        # Shielded: a caller that goes away (a closed tab) must not cancel the
        # load other callers share.
        return await asyncio.shield(self._start(key, load))

    def last_error(self, key: K) -> str | None:
        """Why the last load of *key* failed, or ``None`` when it succeeded."""
        return self._errors.get(key)

    def peek(self, key: K) -> V | None:
        """The held value for *key*, however old, or ``None`` (never starts a load)."""
        held = self._entries.get(key)
        return held[1] if held is not None else None

    def refreshing(self, key: K) -> bool:
        task = self._inflight.get(key)
        return task is not None and not task.done()

    def invalidate(self) -> None:
        """Forget every value; loads already running finish but store nothing."""
        self._generation += 1
        self._entries.clear()
        self._inflight.clear()
        self._errors.clear()

    def mark_stale(self) -> None:
        """Keep every value but refresh it on the next read."""
        for key, (_, value) in list(self._entries.items()):
            self._entries[key] = (-math.inf, value)

    def reset(self) -> None:
        """Forget everything and cancel running loads (tests, a changed setting)."""
        self.invalidate()
        for task in list(self._tasks):
            # RuntimeError: the task's loop is closed (a finished test); it never runs again.
            with contextlib.suppress(RuntimeError):
                task.cancel()
        self._tasks.clear()

    def _start(self, key: K, load: Callable[[], Awaitable[V]]) -> asyncio.Task[V]:
        loop = asyncio.get_running_loop()
        running = self._inflight.get(key)
        if running is not None and not running.done() and running.get_loop() is loop:
            return running
        generation = self._generation

        async def run() -> V:
            try:
                value = await load()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if generation == self._generation:
                    self._errors[key] = self._describe(exc)
                raise
            if generation == self._generation:
                self._entries[key] = (self._clock(), value)
                self._errors.pop(key, None)
            return value

        task = loop.create_task(run(), name=f"ae-swr:{self.name}")
        self._inflight[key] = task
        self._tasks.add(task)

        def done(finished: asyncio.Task[V]) -> None:
            self._tasks.discard(finished)
            if self._inflight.get(key) is finished:
                del self._inflight[key]
            if finished.cancelled():
                return
            exc = finished.exception()  # retrieved here, so never "never retrieved"
            if exc is not None:
                _logger.warning(
                    "%s: a refresh failed (%s); the last good value stays",
                    self.name,
                    type(exc).__name__,
                )

        task.add_done_callback(done)
        return task


__all__ = ["SwrCache"]
