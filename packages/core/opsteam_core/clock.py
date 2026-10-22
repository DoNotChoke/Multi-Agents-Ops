"""Injectable time source so code under test never reads the wall clock directly."""

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a UTC-aware datetime."""
        ...


class SystemClock:
    """Wall clock in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Clock that only moves when the test advances it."""

    def __init__(self, start: datetime) -> None:
        if start.utcoffset() != timedelta(0):
            raise ValueError("FakeClock start must be UTC-aware")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        """Move the clock forward by `delta`."""
        self._now += delta
