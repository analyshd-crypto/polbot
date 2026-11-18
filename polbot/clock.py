"""Clock abstraction: real time for live runs, simulated time for backtests."""

from __future__ import annotations

import asyncio
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A wall clock plus an awaitable sleep."""

    def now(self) -> float:
        """Current unix timestamp (seconds)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Await passage of ``seconds`` (instantaneous for simulated clocks)."""
        ...


class RealClock:
    """Wall clock backed by :func:`time.time` and :func:`asyncio.sleep`."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class SimClock:
    """Simulated clock: ``sleep`` just advances the clock instantly."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self._now += float(seconds)
