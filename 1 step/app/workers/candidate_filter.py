"""
app/workers/candidate_filter.py
================================
Driver Candidate Filter — Step 5a.

Responsible for producing the list of drivers that are even eligible
to be scored for a given order. This runs BEFORE any Google Maps calls
so we never waste API quota on stale or busy drivers.

Exclusion rules (all enforced in a single DB query):
  1. status != 'online_available'   → busy or offline
  2. last_location_at is NULL       → never sent a location (just went online)
  3. last_location_at < NOW()-5min  → location is stale; driver may be offline
                                       without the app knowing

What we deliberately do NOT filter here:
  - Distance / ETA — that's the scorer's job via Google Maps.
  - Current order count — Phase 1 is single-order-per-driver only, so
    status = 'online_busy' already covers this.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.driver import Driver, DriverStatus

logger = logging.getLogger(__name__)


async def get_candidates(session: AsyncSession) -> list[Driver]:
    """
    Return all drivers eligible to be scored for dispatch.

    Args:
        session: Active async DB session.

    Returns:
        List of Driver ORM objects. May be empty — the caller handles
        the no-candidates case by holding the order until next tick.
    """
    stale_cutoff = datetime.now(timezone.utc) - timedelta(
        minutes=settings.driver_stale_threshold_minutes
    )

    result = await session.execute(
        select(Driver).where(
            Driver.status == DriverStatus.ONLINE_AVAILABLE,
            Driver.last_location_at.isnot(None),
            Driver.last_location_at >= stale_cutoff,
            Driver.current_lat.isnot(None),
            Driver.current_lng.isnot(None),
        )
    )
    candidates = list(result.scalars().all())

    logger.info(
        "    Candidate filter: %d eligible driver(s) "
        "(stale cutoff: %s UTC)",
        len(candidates),
        stale_cutoff.strftime("%H:%M:%S"),
    )

    return candidates
