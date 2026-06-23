"""
app/workers/scoring_engine.py
==============================
Scoring Engine & Atomic Assignment — Step 5.

This module is called by the dispatch loop for every order that passes
the HOLD gate. It:

  1. Fetches all eligible driver candidates (via candidate_filter).
  2. Calls Google Maps Distance Matrix for each candidate to get a real
     driving ETA from the driver to the restaurant.
  3. Scores each candidate. Lower score = better candidate.
  4. Sorts candidates by score and attempts to atomically assign the
     winner using a PostgreSQL SELECT FOR UPDATE row lock.
  5. If the winner was grabbed by a race (another process assigned them
     between scoring and locking), tries the next candidate.
  6. If no candidate can be locked, holds the order until the next tick.

═══════════════════════════════════════════════════════════════════════════════
SCORING FORMULA
═══════════════════════════════════════════════════════════════════════════════

    score = eta_minutes
            + early_penalty_weight  * max(0,  minutes_early)
            + late_penalty_weight   * max(0,  minutes_late)

Where:
    eta_minutes    = Google Maps drive time (driver → restaurant)
    arrival_time   = now + eta
    minutes_early  = max(0, predicted_ready_at - arrival_time)  in minutes
                     Driver arrives before food is ready. They wait.
    minutes_late   = max(0, arrival_time - predicted_ready_at)  in minutes
                     Food is ready and sitting before driver arrives.

Penalty weights (from config):
    EARLY_PENALTY_WEIGHT = 0.5   → arriving 4 min early adds 2 pts
    LATE_PENALTY_WEIGHT  = 2.0   → arriving 4 min late  adds 8 pts

Late is penalised 4× harder than early. Cold food is never acceptable.

Example:
    Order ready in 8 min. Driver A: 6 min away. Driver B: 10 min away.
    Driver A: score = 6 + (0.5 × 2) + 0      = 7.0   ← winner
    Driver B: score = 10 + 0        + (2×2)   = 14.0

═══════════════════════════════════════════════════════════════════════
RACE CONDITION SAFETY
═══════════════════════════════════════════════════════════════════════

    All state changes (driver + order) happen inside a single DB
    transaction with a SELECT FOR UPDATE lock on the driver row.

    Sequence:
        BEGIN
        SELECT * FROM drivers WHERE id=? AND status='online_available'
        FOR UPDATE;           ← blocks any concurrent lock on this row
        -- If NULL returned: driver was taken. ROLLBACK. Try next.
        UPDATE drivers SET status='online_busy', current_order_id=?
        UPDATE orders  SET dispatch_status='assigned', assigned_driver_id=?
        COMMIT

    This guarantees that even if two dispatch loop instances run
    simultaneously (shouldn't happen with max_instances=1, but covered
    defensively), only one can assign a given driver to an order.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.driver import Driver, DriverStatus
from app.models.order import DispatchStatus, Order
from app.services.maps_client import MapsClient
from app.services.maps_models import LatLng
from app.workers.candidate_filter import get_candidates

logger = logging.getLogger(__name__)

# =============================================================================
# Scoring weights — sourced from config so they're tunable via .env
# =============================================================================

EARLY_PENALTY_WEIGHT: float = 0.5   # Per minute the driver arrives early
LATE_PENALTY_WEIGHT:  float = 2.0   # Per minute the food sits waiting


# =============================================================================
# Internal data structures
# =============================================================================

@dataclass
class ScoredCandidate:
    """A driver with their computed score and the ETA that produced it."""

    driver:         Driver
    eta_minutes:    float
    score:          float

    def __lt__(self, other: "ScoredCandidate") -> bool:
        return self.score < other.score


# =============================================================================
# Public entry point — called by the dispatch loop
# =============================================================================

async def assign_best_driver(
    order: Order,
    session_factory: async_sessionmaker,
    maps_client: MapsClient,
) -> bool:
    """
    Score all eligible drivers and atomically assign the best one.

    Args:
        order:           The order to assign. Must be within dispatch window.
        session_factory: Used to open DB sessions for queries and the lock.
        maps_client:     Injected Google Maps client for ETA calls.

    Returns:
        True  if a driver was successfully assigned.
        False if no driver was available (order stays in current state;
              dispatch loop will retry on the next tick).
    """
    logger.info(
        "  SCORING  order=%s  ready_at=%s",
        str(order.id)[:8],
        order.predicted_ready_at.strftime("%H:%M:%S"),
    )

    async with session_factory() as session:
        candidates = await get_candidates(session)

    if not candidates:
        logger.info("    No eligible candidates. Order held until next tick.")
        return False

    scored = await _score_candidates(candidates, order, maps_client)

    if not scored:
        logger.info(
            "    All %d candidate(s) returned Maps failures. "
            "Order held until next tick.",
            len(candidates),
        )
        return False

    scored.sort()

    logger.info("    Scored %d candidate(s):", len(scored))
    for rank, sc in enumerate(scored, 1):
        logger.info(
            "      #%d  driver=%-20s  eta=%.1fmin  score=%.2f",
            rank,
            sc.driver.name,
            sc.eta_minutes,
            sc.score,
        )

    for candidate in scored:
        assigned = await _attempt_atomic_assignment(
            order=order,
            candidate=candidate,
            session_factory=session_factory,
        )
        if assigned:
            logger.info(
                "  ✓ ASSIGNED  order=%s → driver=%s (score=%.2f)",
                str(order.id)[:8],
                candidate.driver.name,
                candidate.score,
            )
            return True

        logger.info(
            "    Driver %s was taken (race condition). Trying next candidate.",
            candidate.driver.name,
        )

    logger.info(
        "    All %d candidate(s) were taken. Order held until next tick.",
        len(scored),
    )
    return False


# =============================================================================
# Scoring
# =============================================================================


async def _score_candidates(
    candidates: list[Driver],
    order: Order,
    maps_client: MapsClient,
) -> list[ScoredCandidate]:
    """
    Call Google Maps for each candidate and compute their score.

    Candidates whose Maps call fails are silently excluded — they are
    not penalised, they simply cannot be scored. If ALL candidates fail,
    the caller holds the order.
    """
    now = datetime.now(timezone.utc)
    destination = LatLng(lat=order.restaurant_lat, lng=order.restaurant_lng)
    scored: list[ScoredCandidate] = []

    for driver in candidates:
        origin = LatLng(lat=driver.current_lat, lng=driver.current_lng)
        eta_result = await maps_client.get_driving_eta(origin, destination)

        if not eta_result.ok:
            logger.warning(
                "    Maps failure for driver %s: %s — excluded from scoring.",
                driver.name,
                eta_result.failure_reason,
            )
            continue

        eta_minutes = eta_result.duration_minutes
        score = _compute_score(
            eta_minutes=eta_minutes,
            now=now,
            predicted_ready_at=order.predicted_ready_at,
        )
        scored.append(ScoredCandidate(driver=driver, eta_minutes=eta_minutes, score=score))

    return scored


def _compute_score(
    eta_minutes: float,
    now: datetime,
    predicted_ready_at: datetime,
) -> float:
    """
    Compute the dispatch score for a single (driver, order) pair.

    Lower is better.

    Args:
        eta_minutes:       Google Maps drive time from driver to restaurant.
        now:               Current time (passed explicitly for testability).
        predicted_ready_at: When the order is expected to be ready.

    Returns:
        A float score. The candidate with the lowest score is assigned.
    """
    minutes_until_ready = (predicted_ready_at - now).total_seconds() / 60.0
    arrival_delta = minutes_until_ready - eta_minutes

    minutes_early = max(0.0, arrival_delta)
    minutes_late  = max(0.0, -arrival_delta)

    score = (
        eta_minutes
        + EARLY_PENALTY_WEIGHT * minutes_early
        + LATE_PENALTY_WEIGHT  * minutes_late
    )

    return round(score, 4)


# =============================================================================
# Atomic assignment — the race-condition-safe core
# =============================================================================


async def _attempt_atomic_assignment(
    order: Order,
    candidate: ScoredCandidate,
    session_factory: async_sessionmaker,
) -> bool:
    """
    Try to atomically assign a driver to an order using SELECT FOR UPDATE.

    The lock guarantees that even under concurrent dispatch processes,
    a driver can only be assigned to one order at a time.

    Returns:
        True  if the lock was acquired and both rows were updated.
        False if the driver was already taken (lock returned no row).
    """
    async with session_factory() as session:
        async with session.begin():
            locked_driver = await session.scalar(
                select(Driver)
                .where(
                    Driver.id == candidate.driver.id,
                    Driver.status == DriverStatus.ONLINE_AVAILABLE,
                )
                .with_for_update()
            )

            if locked_driver is None:
                return False

            now = datetime.now(timezone.utc)

            await session.execute(
                update(Driver)
                .where(Driver.id == locked_driver.id)
                .values(
                    status=DriverStatus.ONLINE_BUSY,
                    current_order_id=order.id,
                )
            )

            await session.execute(
                update(Order)
                .where(Order.id == order.id)
                .values(
                    dispatch_status=DispatchStatus.ASSIGNED,
                    assigned_driver_id=locked_driver.id,
                    assigned_at=now,
                )
            )

    return True
