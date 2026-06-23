"""
scripts/mock_drivers.py
=======================
Simulates 5 delivery drivers moving around Podgorica, Montenegro.

Each tick (every UPDATE_INTERVAL_SECONDS):
  - Each driver moves by a small randomised delta in their current direction.
  - Direction bounces when a driver hits the city bounding box.
  - A 12% chance per tick causes a driver to pause (simulates waiting at a
    restaurant, a red light, or a delivery handoff).
  - All positions are written to the database in a single transaction.

Usage:
    python -m scripts.mock_drivers

Environment:
    Set DATABASE_URL in your .env or export it before running.
    The script reads it via app.core.config.settings, or falls back to
    the DEFAULT_DATABASE_URL constant below for quick local testing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.future import select

from app.models.driver import Driver, DriverStatus
from app.models.order import Order

# =============================================================================
# Configuration
# =============================================================================

DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@localhost:5432/dispatch_db"
)
DATABASE_URL: str = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)

# How often to push new positions to the DB (seconds)
UPDATE_INTERVAL_SECONDS: int = 5

# Stale-driver threshold used by the dispatch loop (for reference only here)
STALE_THRESHOLD_MINUTES: int = 5

# Podgorica bounding box — drivers bounce off these edges
LAT_MIN, LAT_MAX = 42.418, 42.472
LNG_MIN, LNG_MAX = 19.225, 19.315

# Max displacement per tick — roughly 60-120 m in real-world terms
DELTA_LAT: float = 0.0009
DELTA_LNG: float = 0.0012

# Probability that a driver is stationary on any given tick
PAUSE_PROBABILITY: float = 0.12

# =============================================================================
# Seed data — 5 real Podgorica starting positions
# =============================================================================

@dataclass(frozen=True)
class DriverSeed:
    name: str
    phone: str
    start_lat: float
    start_lng: float


DRIVER_SEEDS: list[DriverSeed] = [
    DriverSeed(
        name="Marko Petrović",
        phone="+38269100001",
        start_lat=42.4415,
        start_lng=19.2636,
    ),  # City centre / Trg Republike
    DriverSeed(
        name="Nikola Đurović",
        phone="+38269100002",
        start_lat=42.4280,
        start_lng=19.2490,
    ),  # Delta City mall area
    DriverSeed(
        name="Ana Vukčević",
        phone="+38269100003",
        start_lat=42.4432,
        start_lng=19.2720,
    ),  # Old town / Stara Varoš
    DriverSeed(
        name="Stefan Joković",
        phone="+38269100004",
        start_lat=42.4380,
        start_lng=19.2580,
    ),  # Gorica Hill residential
    DriverSeed(
        name="Milica Knežević",
        phone="+38269100005",
        start_lat=42.4310,
        start_lng=19.2670,
    ),  # Train station / Željeznička
]

# =============================================================================
# In-memory driver state
# =============================================================================

@dataclass
class DriverState:
    """Mutable position + heading for one simulated driver."""

    db_id: UUID
    name: str
    lat: float
    lng: float

    # Movement direction: +1.0 or -1.0 on each axis
    lat_dir: float = field(default_factory=lambda: random.choice([-1.0, 1.0]))
    lng_dir: float = field(default_factory=lambda: random.choice([-1.0, 1.0]))

    # True when the driver is stationary this tick
    paused: bool = False


# =============================================================================
# Logging
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mock_drivers")

# =============================================================================
# Database helpers
# =============================================================================

async def seed_drivers(session: AsyncSession) -> list[DriverState]:
    """
    Ensure all seed drivers exist in the DB.
    Inserts missing rows; leaves existing rows untouched.
    Returns a DriverState for every seed driver.
    """
    states: list[DriverState] = []

    for seed in DRIVER_SEEDS:
        result = await session.execute(
            select(Driver).where(Driver.phone == seed.phone)
        )
        driver: Driver | None = result.scalar_one_or_none()

        if driver is None:
            driver = Driver(
                id=uuid4(),
                name=seed.name,
                phone=seed.phone,
                status=DriverStatus.ONLINE_AVAILABLE,
                current_lat=seed.start_lat,
                current_lng=seed.start_lng,
                last_location_at=datetime.now(timezone.utc),
            )
            session.add(driver)
            logger.info(f"  ✚ Seeded  {driver.name:<20s}  id={driver.id}")
        else:
            logger.info(f"  ✓ Exists  {driver.name:<20s}  id={driver.id}")

        states.append(
            DriverState(
                db_id=driver.id,
                name=driver.name,
                lat=driver.current_lat or seed.start_lat,
                lng=driver.current_lng or seed.start_lng,
            )
        )

    await session.commit()
    return states


async def push_positions(
    session: AsyncSession,
    states: list[DriverState],
) -> None:
    """
    Write all driver positions to the DB inside a single transaction.
    Uses bulk UPDATE (one statement per driver, one commit).
    """
    now = datetime.now(timezone.utc)

    for state in states:
        await session.execute(
            update(Driver)
            .where(Driver.id == state.db_id)
            .values(
                current_lat=state.lat,
                current_lng=state.lng,
                last_location_at=now,
                updated_at=now,
            )
        )

    await session.commit()

# =============================================================================
# Movement simulation
# =============================================================================

def advance(state: DriverState) -> None:
    """
    Mutate state in-place with the next simulated position.

    Movement model:
      - Jittered delta (30–100% of max delta) to avoid perfectly straight lines.
      - 12% chance to pause entirely (restaurant wait / red light).
      - Boundary bounce: reverse direction axis when hitting bounding box edge.
    """
    # Pause check
    if random.random() < PAUSE_PROBABILITY:
        state.paused = True
        return  # Position unchanged this tick

    state.paused = False
    jitter_lat = random.uniform(0.30, 1.00)
    jitter_lng = random.uniform(0.30, 1.00)

    new_lat = state.lat + DELTA_LAT * state.lat_dir * jitter_lat
    new_lng = state.lng + DELTA_LNG * state.lng_dir * jitter_lng

    # Latitude boundary bounce
    if not (LAT_MIN <= new_lat <= LAT_MAX):
        state.lat_dir *= -1
        new_lat = max(LAT_MIN, min(LAT_MAX, new_lat))

    # Longitude boundary bounce
    if not (LNG_MIN <= new_lng <= LNG_MAX):
        state.lng_dir *= -1
        new_lng = max(LNG_MIN, min(LNG_MAX, new_lng))

    state.lat = new_lat
    state.lng = new_lng


def log_tick(tick: int, states: list[DriverState]) -> None:
    logger.info(f"── Tick #{tick:<4d} ─────────────────────────────────────────")
    for s in states:
        status = "⏸ paused" if s.paused else "▶ moving"
        logger.info(
            f"   {s.name:<20s}  ({s.lat:.6f}, {s.lng:.6f})  {status}"
        )

# =============================================================================
# Main simulation loop
# =============================================================================

async def run_simulation() -> None:
    engine = create_async_engine(DATABASE_URL, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    logger.info("=" * 60)
    logger.info("  Mock Driver Simulator — Podgorica")
    logger.info(f"  Drivers : {len(DRIVER_SEEDS)}")
    logger.info(f"  Interval: {UPDATE_INTERVAL_SECONDS}s")
    logger.info(f"  DB      : {DATABASE_URL.split('@')[-1]}")  # hide credentials
    logger.info("=" * 60)

    async with Session() as session:
        logger.info("Seeding drivers…")
        states = await seed_drivers(session)

    logger.info(f"\nSimulation running. Press Ctrl+C to stop.\n")
    tick = 0

    try:
        while True:
            tick += 1

            # Compute next positions (pure in-memory, no I/O)
            for state in states:
                advance(state)

            log_tick(tick, states)

            # Single DB round-trip for all drivers
            async with Session() as session:
                await push_positions(session, states)

            await asyncio.sleep(UPDATE_INTERVAL_SECONDS)

    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("\nSimulation stopped.")
    finally:
        await engine.dispose()
        logger.info("Engine disposed. Goodbye.")


if __name__ == "__main__":
    asyncio.run(run_simulation())
