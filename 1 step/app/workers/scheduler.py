"""
app/workers/scheduler.py
=========================
APScheduler configuration for the dispatch loop.

Why APScheduler (not Celery):
  - Phase 1 has a single app process. No broker, no worker processes,
    no infrastructure overhead. The scheduler runs inside the same
    asyncio event loop as FastAPI.
  - The job function signature is identical to what a Celery task would
    look like — migrating in Phase 2 requires no changes to the loop logic.

Scheduler lifecycle:
  - start() is called inside the FastAPI lifespan (on startup).
  - shutdown() is called on app teardown.
  - The scheduler is stored as app.state.scheduler so it can be
    inspected or paused via admin endpoints in Phase 2.

Job configuration:
  - trigger: 'interval' — fires every N seconds.
  - max_instances=1 — if a tick takes longer than the interval (e.g. a
    slow Google Maps call), the next tick is skipped rather than stacked.
    This prevents runaway parallelism in the dispatch loop.
  - misfire_grace_time=15 — if the scheduler wakes up late (e.g. after
    a GC pause), it still fires the job if it's within 15s of due time.
    Beyond that, the tick is skipped and the next one picks up.
  - coalesce=True — if multiple ticks were missed, fire just one catch-up
    tick rather than firing them all at once.
"""

from __future__ import annotations

import logging
from functools import partial

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import settings
from app.services.maps_client import MapsClient
from app.workers.dispatch_loop import run_dispatch_tick

logger = logging.getLogger(__name__)


def create_scheduler(
    session_factory: async_sessionmaker,
    maps_client: MapsClient,
) -> AsyncIOScheduler:
    """
    Build and configure the AsyncIOScheduler with the dispatch loop job.

    Args:
        session_factory: The async_sessionmaker to inject into the loop.
        maps_client:     Shared MapsClient instance for Google ETA calls.

    Returns:
        A configured (but not started) AsyncIOScheduler.
    """
    scheduler = AsyncIOScheduler(
        logger=logging.getLogger("apscheduler"),
    )

    job_fn = partial(
        run_dispatch_tick,
        session_factory=session_factory,
        maps_client=maps_client,
    )

    scheduler.add_job(
        func=job_fn,
        trigger=IntervalTrigger(seconds=settings.dispatch_loop_interval_seconds),
        id="dispatch_loop",
        name="Dispatch Loop",
        max_instances=1,
        misfire_grace_time=15,
        coalesce=True,
        replace_existing=True,
    )

    logger.info(
        "Scheduler configured: dispatch loop every %ds.",
        settings.dispatch_loop_interval_seconds,
    )

    return scheduler
