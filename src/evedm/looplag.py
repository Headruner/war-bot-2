"""Event-loop lag monitor: one task that sleeps a second and measures how late it wakes up.

Everything in the app shares one event loop, so a long synchronous section anywhere (a big JSON parse, a long
database query) delays every notification read, Discord post and web request by that long. The overshoot of the
sleep is exactly that delay. The monitor keeps the last ten minutes, logs a WARNING when a single overshoot is
over ``warn_s`` (at most once a minute) and is shown in ``/api/health``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("evedm.looplag")

INTERVAL_S = 1.0
WARN_S = 2.0
WINDOW_S = 600.0
WARN_EVERY_S = 60.0


class LoopLagMonitor:
    def __init__(
        self,
        *,
        interval_s: float = INTERVAL_S,
        warn_s: float = WARN_S,
        window_s: float = WINDOW_S,
        warn_every_s: float = WARN_EVERY_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.interval_s = interval_s
        self.warn_s = warn_s
        self.window_s = window_s
        self.warn_every_s = warn_every_s
        self._clock = clock
        self._sleep = sleep
        self._samples: deque[tuple[float, float]] = deque()  # (when, overshoot in seconds)
        self._last_warn = -1e18
        self.warnings = 0
        self._task: asyncio.Task[None] | None = None

    def record(self, overshoot_s: float, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        overshoot_s = max(0.0, overshoot_s)
        self._samples.append((now, overshoot_s))
        while self._samples and self._samples[0][0] < now - self.window_s:
            self._samples.popleft()
        if overshoot_s > self.warn_s and now - self._last_warn >= self.warn_every_s:
            self._last_warn = now
            self.warnings += 1
            log.warning(
                "event loop blocked for %.1f s (max %.1f s in the last %.0f min): reads, posts and requests waited",
                overshoot_s, self.max_s(), self.window_s / 60,
            )

    def max_s(self) -> float:
        return max((v for _, v in self._samples), default=0.0)

    def p99_s(self) -> float:
        values = sorted(v for _, v in self._samples)
        if not values:
            return 0.0
        return values[min(len(values) - 1, int(0.99 * (len(values) - 1) + 0.5))]

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_s": round(self.max_s(), 3),
            "p99_s": round(self.p99_s(), 3),
            "samples": len(self._samples),
            "window_s": self.window_s,
            "warnings": self.warnings,
        }

    async def run(self) -> None:
        while True:
            start = self._clock()
            await self._sleep(self.interval_s)
            self.record(self._clock() - start - self.interval_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
