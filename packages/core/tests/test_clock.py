from datetime import UTC, datetime, timedelta

import pytest
from opsteam_core.clock import FakeClock, SystemClock

START = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)


def test_system_clock_is_utc() -> None:
    assert SystemClock().now().utcoffset() == timedelta(0)


def test_fake_clock_advances() -> None:
    clock = FakeClock(START)
    clock.advance(timedelta(seconds=90))
    assert clock.now() == START + timedelta(seconds=90)


def test_fake_clock_requires_utc() -> None:
    with pytest.raises(ValueError, match="UTC"):
        FakeClock(datetime(2026, 10, 22, 6, 0))
