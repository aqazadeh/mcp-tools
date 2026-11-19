"""Time source. Injected everywhere so tests can control time."""

import time
from typing import Protocol

import anyio
import anyio.lowlevel


class Clock(Protocol):
    def now_ms(self) -> int:
        """Wall-clock time in epoch milliseconds (what is persisted)."""
        ...

    def monotonic(self) -> float:
        """Monotonic seconds for measuring intervals inside one process."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await anyio.sleep(seconds)


class FakeClock:
    """A manually advanced clock. `sleep` advances time instead of waiting."""

    def __init__(self, start_ms: int = 1_800_000_000_000) -> None:
        self._now_ms = start_ms

    def now_ms(self) -> int:
        return self._now_ms

    def monotonic(self) -> float:
        return self._now_ms / 1000

    def advance(self, seconds: float) -> None:
        self._now_ms += int(seconds * 1000)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await anyio.lowlevel.checkpoint()  # still yield to the event loop


def day_of(now_ms: int) -> str:
    """UTC calendar day (YYYY-MM-DD) of a timestamp; the unit of the daily spend cap."""
    return time.strftime("%Y-%m-%d", time.gmtime(now_ms / 1000))
