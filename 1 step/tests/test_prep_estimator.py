"""
tests/test_prep_estimator.py
=============================
Unit tests for the prep time estimator.

These tests are fully deterministic — no DB, no I/O, no randomness.
All time values are injected explicitly so the suite is stable regardless
of when it runs.

Run:
    pytest tests/test_prep_estimator.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services.prep_estimator import (
    compute,
    is_within_dispatch_window,
    minutes_until_ready,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

TOTAL_PREP = settings.prep_time_minutes + settings.safety_buffer_minutes  # 18 min


def utc(year=2024, month=1, day=1, hour=12, minute=0, second=0) -> datetime:
    """Helper: build a UTC-aware datetime with named args."""
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# compute()
# ---------------------------------------------------------------------------

class TestCompute:

    def test_returns_timezone_aware_datetime(self):
        result = compute(utc())
        assert result.tzinfo is not None

    def test_adds_correct_total_minutes(self):
        created = utc(hour=12, minute=0)
        result = compute(created)
        expected = created + timedelta(minutes=TOTAL_PREP)
        assert result == expected

    def test_total_is_18_minutes_with_default_settings(self):
        """15 min prep + 3 min buffer = 18 min."""
        created = utc(hour=10, minute=0)
        result = compute(created)
        diff = (result - created).total_seconds() / 60
        assert diff == pytest.approx(18.0)

    def test_works_at_midnight_boundary(self):
        """Order placed at 23:55 should be ready next day at 00:13."""
        created = utc(hour=23, minute=55)
        result = compute(created)
        assert result.day == created.day + 1
        assert result.hour == 0
        assert result.minute == 13

    def test_rejects_naive_datetime(self):
        naive = datetime(2024, 1, 1, 12, 0, 0)  # no tzinfo
        with pytest.raises(ValueError, match="timezone-aware"):
            compute(naive)

    def test_output_preserves_utc_timezone(self):
        result = compute(utc())
        assert result.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# minutes_until_ready()
# ---------------------------------------------------------------------------

class TestMinutesUntilReady:

    def test_positive_when_ready_in_future(self):
        now = utc(hour=12, minute=0)
        ready = utc(hour=12, minute=10)
        assert minutes_until_ready(ready, now) == pytest.approx(10.0)

    def test_zero_when_exactly_at_ready_time(self):
        t = utc(hour=12, minute=0)
        assert minutes_until_ready(t, t) == pytest.approx(0.0)

    def test_negative_when_overdue(self):
        now = utc(hour=12, minute=20)
        ready = utc(hour=12, minute=10)
        assert minutes_until_ready(ready, now) == pytest.approx(-10.0)

    def test_fractional_minutes(self):
        now = utc(hour=12, minute=0, second=0)
        ready = utc(hour=12, minute=0, second=30)  # 30 seconds = 0.5 min
        assert minutes_until_ready(ready, now) == pytest.approx(0.5)

    def test_rejects_naive_predicted_ready_at(self):
        naive = datetime(2024, 1, 1, 12, 0)
        with pytest.raises(ValueError):
            minutes_until_ready(naive, utc())

    def test_rejects_naive_now(self):
        naive_now = datetime(2024, 1, 1, 12, 0)
        with pytest.raises(ValueError):
            minutes_until_ready(utc(), naive_now)


# ---------------------------------------------------------------------------
# is_within_dispatch_window()
# ---------------------------------------------------------------------------

class TestIsWithinDispatchWindow:

    def test_true_when_exactly_at_window_boundary(self):
        """10 minutes away = exactly at boundary = should dispatch."""
        now = utc(hour=12, minute=0)
        ready = utc(hour=12, minute=10)   # dispatch_window_minutes = 10
        assert is_within_dispatch_window(ready, now) is True

    def test_true_when_inside_window(self):
        now = utc(hour=12, minute=0)
        ready = utc(hour=12, minute=5)    # 5 min away — well inside
        assert is_within_dispatch_window(ready, now) is True

    def test_true_when_overdue(self):
        """Overdue order is still within window (dispatch immediately)."""
        now = utc(hour=12, minute=20)
        ready = utc(hour=12, minute=10)
        assert is_within_dispatch_window(ready, now) is True

    def test_false_when_outside_window(self):
        now = utc(hour=12, minute=0)
        ready = utc(hour=12, minute=11)   # 11 min away — HOLD
        assert is_within_dispatch_window(ready, now) is False

    def test_false_when_far_in_future(self):
        now = utc(hour=12, minute=0)
        ready = utc(hour=12, minute=30)   # 30 min away
        assert is_within_dispatch_window(ready, now) is False

    def test_full_pipeline_new_order_is_held(self):
        """
        Integration: a freshly created order has predicted_ready_at = now + 18 min.
        18 > 10, so is_within_dispatch_window must return False.
        The dispatch loop should HOLD this order.
        """
        now = utc(hour=12, minute=0)
        predicted_ready_at = compute(created_at=now)
        assert is_within_dispatch_window(predicted_ready_at, now) is False

    def test_full_pipeline_order_enters_window(self):
        """
        Integration: same order, 9 minutes later.
        18 - 9 = 9 min remaining < 10 min window.
        Dispatch loop should now EVALUATE this order.
        """
        created_at = utc(hour=12, minute=0)
        predicted_ready_at = compute(created_at=created_at)

        nine_minutes_later = utc(hour=12, minute=9)
        assert is_within_dispatch_window(predicted_ready_at, nine_minutes_later) is True
