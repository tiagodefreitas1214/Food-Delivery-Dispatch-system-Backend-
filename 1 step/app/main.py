"""
app/main.py
============
FastAPI application entry point.

Lifespan (startup / shutdown):
  1. Build the async DB engine + session factory.
  2. Start the MapsClient HTTP pool.
  3. Start the APScheduler with the dispatch loop job.
  4. On shutdown: stop scheduler → close Maps pool → dispose DB engine.

The ordering matters:
  - DB engine must be up before the scheduler starts (the first tick
    could fire within seconds of startup).
  - Scheduler must stop before the DB engine is disposed (in-flight
    ticks must complete before connections are torn down).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import Depends, FastAPI, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine, get_db
from app.schemas.order import OrderCreateRequest, OrderResponse
from app.services.maps_client import MapsClient
from app.services.order_service import create_order
from app.workers.scheduler import create_scheduler

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)


# =============================================================================
# Lifespan — startup and shutdown
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage startup and shutdown of all long-lived resources."""

    logger.info("── Startup ──────────────────────────────────────────")

    # 1. Maps client
    maps_client = MapsClient()
    await maps_client.start()
    app.state.maps_client = maps_client
    logger.info("  ✓ MapsClient ready")

    # 2. Scheduler (DB engine is module-level in session.py, always available)
    scheduler = create_scheduler(
        session_factory=AsyncSessionLocal,
        maps_client=maps_client,
    )
    scheduler.start()
    app.state.scheduler = scheduler
    logger.info(
        "  ✓ Scheduler started (dispatch every %ds)",
        settings.dispatch_loop_interval_seconds,
    )

    logger.info("── Ready ────────────────────────────────────────────")

    yield  # Application runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("── Shutdown ─────────────────────────────────────────")

    scheduler.shutdown(wait=True)   # Let in-flight ticks finish
    logger.info("  ✓ Scheduler stopped")

    await maps_client.stop()
    logger.info("  ✓ MapsClient stopped")

    await engine.dispose()
    logger.info("  ✓ DB engine disposed")

    logger.info("── Done ─────────────────────────────────────────────")


# =============================================================================
# App instance
# =============================================================================

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
)


# =============================================================================
# Routes
# =============================================================================

@app.get("/health")
async def health() -> dict:
    """Simple health check. Returns 200 if the app is alive."""
    return {"status": "ok", "app": settings.app_name}


@app.post(
    "/orders",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new order",
    description=(
        "Creates an order, computes predicted_ready_at, "
        "and sets dispatch_status=pending. "
        "The dispatch loop will evaluate it on the next tick."
    ),
)
async def create_order_route(
    payload: OrderCreateRequest,
    db: AsyncSession = Depends(get_db),
) -> OrderResponse:
    try:
        order = await create_order(db=db, payload=payload)
        return OrderResponse.model_validate(order)
    except Exception as exc:
        logger.exception("Failed to create order: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Order creation failed.",
        )
