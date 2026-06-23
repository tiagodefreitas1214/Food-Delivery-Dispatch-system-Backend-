"""
app/workers/dispatch_loop.py
=============================
The Dispatch Loop — Step 4.

This is the heartbeat of the dispatch system. It runs every
DISPATCH_LOOP_INTERVAL_SECONDS (default: 30s) and processes every
unassigned order in the system.

Decision logic per order (strict, in order):

    1. HOLD gate:
       if minutes_until_ready(order.predicted_ready_at) > 10 min:
           → Set status to 'holding' (if still 'pending')
           → Skip. No driver evaluation.

    2. EVALUATE gate (Step 5):
       Order is within the 10-min window. Hand off to the
       scoring engine, which will find and lock the best driver.

Fault-tolerance rules:
    - One failing order must NEVER crash the loop. Errors are caught
      per-order, logged, and the loop continues.
    - One failing loop tick must NEVER crash the scheduler. The
      exception is caught at the tick level.
    - If the DB is unreachable for a full tick, the loop logs and
      returns. The scheduler will retry on the next interval.

All database access inside the loop uses explicit transactions.
No ORM lazy-loading — every query is intentional.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.order import DispatchStatus, Order
from app.services.maps_client import MapsClient
from app.services.prep_estimator import is_within_dispatch_window, minutes_until_ready

logger = logging.getLogger(__name__)

# ── Separator width for log readability ───────────────────────────────────────
_SEP = "─" * 60


# =============================================================================
# Public entry point — called by the scheduler every N seconds
# =============================================================================

async def run_dispatch_tick(
    session_factory: async_sessionmaker,
    maps_client: MapsClient,
) -> None:
    """
    Execute one full dispatch evaluation cycle.

    This function is registered with APScheduler and fires on every tick.
    It must not raise — all exceptions are caught internally so the
    scheduler keeps running even if a tick fails completely.

    Args:
        session_factory: The async_sessionmaker bound to the app's engine.
        maps_client:     Shared MapsClient instance for Google ETA calls.
    """
    tick_start = datetime.now(timezone.utc)
    logger.info(_SEP)
    logger.info("DISPATCH TICK  %s", tick_start.strftime("%H:%M:%S UTC"))
    logger.info(_SEP)

    try:
        async with session_factory() as session:
            orders = await _fetch_actionable_orders(session)

        if not orders:
            logger.info("  No actionable orders. Sleeping until next tick.")
            return

        logger.info("  Found %d actionable order(s).", len(orders))

        for order in orders:
            await _process_order(order, session_factory, maps_client)

    except Exception as exc:
        logger.exception(
            "DISPATCH TICK FAILED (will retry next interval): %s", exc
        )

    finally:
        elapsed = (datetime.now(timezone.utc) - tick_start).total_seconds()
        logger.info("  Tick complete in %.2fs.", elapsed)


# =============================================================================
# Step 1 of the tick — fetch orders that need evaluation
# =============================================================================

async def _fetch_actionable_orders(session: AsyncSession) -> list[Order]:
    """
    Fetch all orders that are in a state the dispatch loop can act on.

    Actionable states: pending, holding.
    Everything else (assigned, picked_up, delivered, cancelled) is ignored.

    The query deliberately avoids filtering on predicted_ready_at here —
    that check happens in Python so the HOLD gate logic stays in one place
    (prep_estimator.is_within_dispatch_window) and is fully unit-testable.
    """
    result = await session.execute(
        select(Order).where(
            Order.dispatch_status.in_([
                DispatchStatus.PENDING,
                DispatchStatus.HOLDING,
            ])
        )
    )
    return list(result.scalars().all())


# =============================================================================
# Step 2 of the tick — process one order
# =============================================================================

async def _process_order(
    order: Order,
    session_factory: async_sessionmaker,
    maps_client: MapsClient,
) -> None:
    """
    Apply the HOLD gate to one order, then hand off to scoring if appropriate.
    """
    try:
        now = datetime.now(timezone.utc)
        mins_remaining = minutes_until_ready(order.predicted_ready_at, now)

        logger.info(
            "  ORDER %s  status=%-10s  ready_in=%.1f min",
            str(order.id)[:8],
            order.dispatch_status.value,
            mins_remaining,
        )

        # ── HOLD gate ────────────────────────────────────────────────────────
        if not is_within_dispatch_window(order.predicted_ready_at, now):
            await _apply_hold(order, session_factory)
            return

        # ── Evaluate gate ─────────────────────────────────────────────────────
        logger.info(
            "    → EVALUATE  (%.1f min until ready — within %d-min window)",
            mins_remaining,
            settings.dispatch_window_minutes,
        )
        await _run_scoring(order, session_factory, maps_client)

    except Exception as exc:
        logger.exception(
            "  Failed to process order %s: %s", order.id, exc
        )


async def _run_scoring(
    order: Order,
    session_factory: async_sessionmaker,
    maps_client: MapsClient,
) -> None:
    """Delegate to the scoring engine. Imported here to avoid circular imports."""
    from app.workers.scoring_engine import assign_best_driver  # noqa: PLC0415
    await assign_best_driver(
        order=order,
        session_factory=session_factory,
        maps_client=maps_client,
    )


# =============================================================================
# State transition — PENDING → HOLDING
# =============================================================================

async def _apply_hold(order: Order, session_factory: async_sessionmaker) -> None:
    """
    Transition an order from PENDING to HOLDING, or confirm it stays HOLDING.

    HOLDING means: the order exists, the formula has run, but the 10-min
    dispatch window hasn't opened yet. The loop will re-evaluate on the
    next tick automatically because HOLDING remains an actionable state.

    Only PENDING→HOLDING is a real DB write. If the order is already
    HOLDING, we log and return — no unnecessary UPDATE.
    """
    if order.dispatch_status == DispatchStatus.HOLDING:
        logger.info(
            "    → HOLD (already holding — next ready in %.1f min)",
            minutes_until_ready(order.predicted_ready_at),
        )
        return

    # PENDING → HOLDING
    async with session_factory() as session:
        await session.execute(
            update(Order)
            .where(Order.id == order.id)
            # Guard: only update if status is still pending.
            # Prevents a race where another process already changed it.
            .where(Order.dispatch_status == DispatchStatus.PENDING)
            .values(dispatch_status=DispatchStatus.HOLDING)
        )
        await session.commit()

    logger.info(
        "    → HOLD  (pending → holding — ready in %.1f min)",
        minutes_until_ready(order.predicted_ready_at),
    )
