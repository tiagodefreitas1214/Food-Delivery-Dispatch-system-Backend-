"""
app/services/prep_estimator.py
===============================
Prep Time Estimator — Step 3.

Formula (Phase 1, static):
    predicted_ready_at = order_created_at + prep_time + safety_buffer
                       = order_created_at + 15 min   + 3 min
                       = order_created_at + 18 min

Design rules:
  - Pure function. No DB access, no I/O, no side effects.
  - Takes an explicit `created_at` argument — never calls datetime.now()
    internally. This makes it fully testable and deterministic.
  - Both tunable constants come from settings so they can be changed in
    .env without touching code.
  - Returns a timezone-aware datetime. The caller (order service) writes
    this directly into orders.predicted_ready_at.

Phase 2 note:
    When per-restaurant prep times are available (from a restaurants table
    or a real-time ML estimator), replace the body of `compute()` without
    changing its signature. The dispatch loop and order service are
    completely insulated from this change.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import settings


def compute(created_at: datetime) -> datetime:
    """
    Compute predicted_ready_at for an order.

    Args:
        created_at: The moment the order was placed. Must be timezone-aware.
                    Naive datetimes are rejected — they produce silent bugs
                    in the dispatch loop's time comparisons.

    Returns:
        A timezone-aware datetime representing when the order is
        expected to be ready for collection by the driver.

    Raises:
        ValueError: If created_at is a naive datetime (no tzinfo).
    """
    if created_at.tzinfo is None:
        raise ValueError(
            f"created_at must be timezone-aware, got naive datetime: {created_at!r}. "
            "Use datetime.now(timezone.utc) when creating orders."
        )

    total_minutes = settings.prep_time_minutes + settings.safety_buffer_minutes
    return created_at + timedelta(minutes=total_minutes)


def minutes_until_ready(predicted_ready_at: datetime, now: datetime | None = None) -> float:
    """
    How many minutes remain until this order is ready.

    Used by the dispatch loop's HOLD gate:
        if minutes_until_ready(order.predicted_ready_at) > settings.dispatch_window_minutes:
            → HOLD, skip this order

    Args:
        predicted_ready_at: The order's predicted_ready_at timestamp.
        now: Reference point in time. Defaults to datetime.now(timezone.utc).
             Pass explicitly in tests.

    Returns:
        Minutes remaining as a float. Negative means the order is overdue.

    Raises:
        ValueError: If either argument is a naive datetime.
    """
    reference = now or datetime.now(timezone.utc)

    if predicted_ready_at.tzinfo is None:
        raise ValueError("predicted_ready_at must be timezone-aware.")
    if reference.tzinfo is None:
        raise ValueError("now must be timezone-aware.")

    delta = predicted_ready_at - reference
    return delta.total_seconds() / 60.0


def is_within_dispatch_window(
    predicted_ready_at: datetime,
    now: datetime | None = None,
) -> bool:
    """
    Returns True if the order is within the dispatch window and should
    be actively evaluated for driver assignment.

    This is the exact gate the dispatch loop uses.

    Rule: dispatch if minutes_until_ready <= dispatch_window_minutes
    (i.e. the order will be ready within the next 10 minutes)
    """
    return minutes_until_ready(predicted_ready_at, now) <= settings.dispatch_window_minutes
