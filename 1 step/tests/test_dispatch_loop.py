"""
tests/test_dispatch_loop.py
============================
Unit tests for the dispatch loop logic.

All DB interaction is mocked — these tests run in milliseconds with no
infrastructure. They verify the HOLD gate decisions and state transitions
independently of the scheduler, Google Maps, and the scoring engine.

Run:
    pytest tests/test_dispatch_loop.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.order import DispatchStatus, Order
from app.workers.dispatch_loop import (
    _apply_hold,
    _fetch_actionable_orders,
    _process_order,
    run_dispatch_tick,
)

# Shared mock maps_client — injected wherever the updated signatures require it
MOCK_MAPS_CLIENT = MagicMock()
MOCK_MAPS_CLIENT.get_driving_eta = AsyncMock()


# =============================================================================
# Helpers
# =============================================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_order(
    status: DispatchStatus = DispatchStatus.PENDING,
    ready_in_minutes: float = 18.0,   # Default: freshly created, well outside window
) -> Order:
    """Build a minimal Order object for testing without touching the DB."""
    order = MagicMock(spec=Order)
    order.id = uuid4()
    order.dispatch_status = status
    order.predicted_ready_at = utc_now() + timedelta(minutes=ready_in_minutes)
    return order


def make_session_factory(session: AsyncMock | None = None) -> MagicMock:
    """Build a mock session_factory whose __aenter__ returns the given session."""
    if session is None:
        session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


# =============================================================================
# _fetch_actionable_orders
# =============================================================================

def make_session_with_orders(orders: list) -> AsyncMock:
    """
    Build an AsyncMock session whose execute() returns a sync MagicMock result.

    SQLAlchemy pattern:
        result = await session.execute(...)   <- async
        rows   = result.scalars().all()       <- sync chain on the result object

    AsyncMock auto-wraps child attributes as async, breaking the sync chain.
    We fix this by setting execute's return_value to a plain MagicMock so
    scalars() and all() behave synchronously as they do in production.
    """
    session = AsyncMock()
    sync_result = MagicMock()
    sync_result.scalars.return_value.all.return_value = orders
    session.execute.return_value = sync_result
    return session


class TestFetchActionableOrders:

    @pytest.mark.asyncio
    async def test_returns_pending_and_holding_orders(self):
        pending_order = make_order(DispatchStatus.PENDING)
        holding_order = make_order(DispatchStatus.HOLDING)

        session = make_session_with_orders([pending_order, holding_order])
        result = await _fetch_actionable_orders(session)

        assert len(result) == 2
        assert pending_order in result
        assert holding_order in result

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_orders(self):
        session = make_session_with_orders([])
        result = await _fetch_actionable_orders(session)
        assert result == []


# =============================================================================
# _apply_hold — state transitions
# =============================================================================

class TestApplyHold:

    @pytest.mark.asyncio
    async def test_pending_order_is_updated_to_holding(self):
        order = make_order(DispatchStatus.PENDING, ready_in_minutes=18)
        session = AsyncMock()
        factory = make_session_factory(session)

        await _apply_hold(order, factory)

        # DB execute (the UPDATE) should have been called once
        session.execute.assert_called_once()
        session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_holding_order_does_not_write_to_db(self):
        order = make_order(DispatchStatus.HOLDING, ready_in_minutes=18)
        factory = make_session_factory()

        await _apply_hold(order, factory)

        # No DB session should have been opened for an already-holding order
        factory.return_value.__aenter__.assert_not_called()


# =============================================================================
# _process_order — HOLD gate decisions
# =============================================================================

class TestProcessOrder:

    @pytest.mark.asyncio
    async def test_order_outside_window_is_held(self):
        """18 min remaining → outside 10-min window → HOLD."""
        order = make_order(DispatchStatus.PENDING, ready_in_minutes=18)
        factory = make_session_factory()

        with patch(
            "app.workers.dispatch_loop._apply_hold", new_callable=AsyncMock
        ) as mock_hold:
            await _process_order(order, factory, MOCK_MAPS_CLIENT)
            mock_hold.assert_called_once_with(order, factory)

    @pytest.mark.asyncio
    async def test_order_inside_window_is_not_held(self):
        """5 min remaining → inside 10-min window → should NOT call _apply_hold."""
        order = make_order(DispatchStatus.PENDING, ready_in_minutes=5)
        factory = make_session_factory()

        with patch("app.workers.dispatch_loop._apply_hold", new_callable=AsyncMock) as mock_hold, \
             patch("app.workers.dispatch_loop._run_scoring", new_callable=AsyncMock):
            await _process_order(order, factory, MOCK_MAPS_CLIENT)
            mock_hold.assert_not_called()

    @pytest.mark.asyncio
    async def test_order_exactly_at_window_boundary_is_evaluated(self):
        """Exactly 10 min remaining → at boundary → should evaluate, not hold."""
        order = make_order(DispatchStatus.PENDING, ready_in_minutes=10)
        factory = make_session_factory()

        with patch("app.workers.dispatch_loop._apply_hold", new_callable=AsyncMock) as mock_hold, \
             patch("app.workers.dispatch_loop._run_scoring", new_callable=AsyncMock):
            await _process_order(order, factory, MOCK_MAPS_CLIENT)
            mock_hold.assert_not_called()

    @pytest.mark.asyncio
    async def test_overdue_order_is_evaluated_not_held(self):
        """Overdue order (ready time passed) → dispatch immediately."""
        order = make_order(DispatchStatus.PENDING, ready_in_minutes=-5)
        factory = make_session_factory()

        with patch("app.workers.dispatch_loop._apply_hold", new_callable=AsyncMock) as mock_hold, \
             patch("app.workers.dispatch_loop._run_scoring", new_callable=AsyncMock):
            await _process_order(order, factory, MOCK_MAPS_CLIENT)
            mock_hold.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_in_order_does_not_propagate(self):
        """One broken order must not crash the loop — exception is swallowed."""
        order = MagicMock(spec=Order)
        order.id = uuid4()
        type(order).predicted_ready_at = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("DB corruption"))
        )
        factory = make_session_factory()

        # Should not raise — exception is caught inside _process_order
        await _process_order(order, factory, MOCK_MAPS_CLIENT)


# =============================================================================
# run_dispatch_tick — full tick integration
# =============================================================================

class TestRunDispatchTick:

    @pytest.mark.asyncio
    async def test_empty_tick_completes_without_error(self):
        """Tick with no actionable orders should run cleanly."""
        session = make_session_with_orders([])
        factory = make_session_factory(session)

        await run_dispatch_tick(session_factory=factory, maps_client=MOCK_MAPS_CLIENT)

    @pytest.mark.asyncio
    async def test_tick_processes_all_fetched_orders(self):
        """Every fetched order should be passed to _process_order."""
        orders = [
            make_order(DispatchStatus.PENDING, ready_in_minutes=18),
            make_order(DispatchStatus.HOLDING, ready_in_minutes=12),
            make_order(DispatchStatus.PENDING, ready_in_minutes=5),
        ]
        session = make_session_with_orders(orders)
        factory = make_session_factory(session)

        with patch(
            "app.workers.dispatch_loop._process_order", new_callable=AsyncMock
        ) as mock_process:
            await run_dispatch_tick(session_factory=factory, maps_client=MOCK_MAPS_CLIENT)
            assert mock_process.call_count == 3

    @pytest.mark.asyncio
    async def test_db_failure_does_not_crash_scheduler(self):
        """If the DB is down, the tick logs and returns — scheduler survives."""
        factory = MagicMock()
        factory.return_value.__aenter__ = AsyncMock(
            side_effect=ConnectionRefusedError("DB unreachable")
        )
        factory.return_value.__aexit__ = AsyncMock(return_value=False)

        await run_dispatch_tick(session_factory=factory, maps_client=MOCK_MAPS_CLIENT)
