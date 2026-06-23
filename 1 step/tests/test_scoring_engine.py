"""
tests/test_scoring_engine.py
=============================
Unit tests for the scoring engine and candidate filter.

All DB and Maps calls are mocked — tests run with no infrastructure.

Run:
    pytest tests/test_scoring_engine.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.models.driver import Driver, DriverStatus
from app.models.order import DispatchStatus, Order
from app.services.maps_models import RouteETAResult
from app.workers.scoring_engine import (
    ScoredCandidate,
    _attempt_atomic_assignment,
    _compute_score,
    _score_candidates,
    assign_best_driver,
)


# =============================================================================
# Helpers
# =============================================================================


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_driver(
    status: DriverStatus = DriverStatus.ONLINE_AVAILABLE,
    lat: float = 42.4280,
    lng: float = 19.2490,
    name: str = "Test Driver",
) -> MagicMock:
    driver = MagicMock(spec=Driver)
    driver.id = uuid4()
    driver.name = name
    driver.status = status
    driver.current_lat = lat
    driver.current_lng = lng
    driver.last_location_at = utc_now()
    return driver


def make_order(ready_in_minutes: float = 8.0) -> MagicMock:
    order = MagicMock(spec=Order)
    order.id = uuid4()
    order.dispatch_status = DispatchStatus.PENDING
    order.predicted_ready_at = utc_now() + timedelta(minutes=ready_in_minutes)
    order.restaurant_lat = 42.4415
    order.restaurant_lng = 19.2636
    return order


def make_maps_client(duration_seconds: int = 360) -> MagicMock:
    """Returns a MapsClient mock whose get_driving_eta always succeeds."""
    client = MagicMock()
    client.get_driving_eta = AsyncMock(
        return_value=RouteETAResult.success(
            duration_seconds=duration_seconds,
            distance_meters=2500,
        )
    )
    return client


def make_session_factory(scalar_return=None) -> MagicMock:
    """
    Factory mock whose session.scalar() returns scalar_return.
    session.execute() and session.commit() are silent AsyncMocks.
    """
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=scalar_return)
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    # Support `async with session.begin():`
    begin_ctx = MagicMock()
    begin_ctx.__aenter__ = AsyncMock(return_value=None)
    begin_ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_ctx)

    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return factory


# =============================================================================
# _compute_score — pure function, no mocks needed
# =============================================================================


class TestComputeScore:

    def test_driver_arrives_exactly_on_time(self):
        """ETA = minutes until ready → no early or late penalty."""
        now   = utc_now()
        ready = now + timedelta(minutes=10)
        score = _compute_score(eta_minutes=10.0, now=now, predicted_ready_at=ready)
        assert score == pytest.approx(10.0)

    def test_driver_arrives_early_gets_small_penalty(self):
        """Driver arrives 4 min early → 0.5 × 4 = 2 pt early penalty."""
        now   = utc_now()
        ready = now + timedelta(minutes=10)
        score = _compute_score(eta_minutes=6.0, now=now, predicted_ready_at=ready)
        assert score == pytest.approx(8.0)

    def test_driver_arrives_late_gets_heavy_penalty(self):
        """Driver arrives 4 min late → 2.0 × 4 = 8 pt late penalty."""
        now   = utc_now()
        ready = now + timedelta(minutes=10)
        score = _compute_score(eta_minutes=14.0, now=now, predicted_ready_at=ready)
        assert score == pytest.approx(22.0)

    def test_late_penalty_exceeds_early_penalty_for_same_delta(self):
        """4 min late must score worse than 4 min early — core business rule."""
        now   = utc_now()
        ready = now + timedelta(minutes=10)
        score_early = _compute_score(eta_minutes=6.0,  now=now, predicted_ready_at=ready)
        score_late  = _compute_score(eta_minutes=14.0, now=now, predicted_ready_at=ready)
        assert score_late > score_early

    def test_closer_driver_wins_when_timing_is_equal(self):
        """All else equal, a lower ETA produces a lower score."""
        now   = utc_now()
        ready = now + timedelta(minutes=10)
        score_near = _compute_score(eta_minutes=8.0,  now=now, predicted_ready_at=ready)
        score_far  = _compute_score(eta_minutes=12.0, now=now, predicted_ready_at=ready)
        assert score_near < score_far

    def test_overdue_order_penalises_all_late_drivers(self):
        """If the order is already overdue, every driver is 'late'."""
        now   = utc_now()
        ready = now - timedelta(minutes=5)
        score = _compute_score(eta_minutes=3.0, now=now, predicted_ready_at=ready)
        assert score == pytest.approx(19.0)

    def test_scored_candidate_sorts_by_score(self):
        """ScoredCandidate.__lt__ must sort ascending on score."""
        d1 = ScoredCandidate(driver=make_driver(), eta_minutes=5.0, score=5.0)
        d2 = ScoredCandidate(driver=make_driver(), eta_minutes=9.0, score=9.0)
        ranked = sorted([d2, d1])
        assert ranked[0].score == pytest.approx(5.0)
        assert ranked[1].score == pytest.approx(9.0)


# =============================================================================
# _score_candidates — async, mocked Maps calls
# =============================================================================


class TestScoreCandidates:

    @pytest.mark.asyncio
    async def test_successful_candidate_is_included(self):
        driver = make_driver(name="Ana")
        order  = make_order(ready_in_minutes=8)
        client = make_maps_client(duration_seconds=300)

        result = await _score_candidates([driver], order, client)

        assert len(result) == 1
        assert result[0].driver.name == "Ana"
        assert result[0].eta_minutes == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_maps_failure_excludes_candidate(self):
        driver = make_driver()
        order  = make_order()
        client = MagicMock()
        client.get_driving_eta = AsyncMock(
            return_value=RouteETAResult.failure("timeout")
        )

        result = await _score_candidates([driver], order, client)

        assert result == []

    @pytest.mark.asyncio
    async def test_mixed_candidates_partial_failure(self):
        good_driver = make_driver(name="Good")
        bad_driver  = make_driver(name="Bad")
        order = make_order(ready_in_minutes=8)

        client = MagicMock()
        client.get_driving_eta = AsyncMock(side_effect=[
            RouteETAResult.success(duration_seconds=300, distance_meters=2000),
            RouteETAResult.failure("network error"),
        ])

        result = await _score_candidates([good_driver, bad_driver], order, client)

        assert len(result) == 1
        assert result[0].driver.name == "Good"

    @pytest.mark.asyncio
    async def test_nearer_driver_scores_lower_than_farther(self):
        near_driver = make_driver(name="Near")
        far_driver  = make_driver(name="Far")
        order = make_order(ready_in_minutes=10)

        client = MagicMock()
        client.get_driving_eta = AsyncMock(side_effect=[
            RouteETAResult.success(duration_seconds=240, distance_meters=1500),
            RouteETAResult.success(duration_seconds=600, distance_meters=4000),
        ])

        result = await _score_candidates([near_driver, far_driver], order, client)
        result.sort()

        assert result[0].driver.name == "Near"
        assert result[1].driver.name == "Far"


# =============================================================================
# _attempt_atomic_assignment — lock acquisition
# =============================================================================


class TestAttemptAtomicAssignment:

    @pytest.mark.asyncio
    async def test_returns_true_when_driver_locked_successfully(self):
        driver = make_driver()
        order  = make_order()
        candidate = ScoredCandidate(driver=driver, eta_minutes=5.0, score=5.0)
        factory = make_session_factory(scalar_return=driver)

        result = await _attempt_atomic_assignment(order, candidate, factory)

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_driver_already_taken(self):
        driver = make_driver()
        order  = make_order()
        candidate = ScoredCandidate(driver=driver, eta_minutes=5.0, score=5.0)
        factory = make_session_factory(scalar_return=None)

        result = await _attempt_atomic_assignment(order, candidate, factory)

        assert result is False

    @pytest.mark.asyncio
    async def test_no_db_writes_when_lock_fails(self):
        driver = make_driver()
        order  = make_order()
        candidate = ScoredCandidate(driver=driver, eta_minutes=5.0, score=5.0)
        factory = make_session_factory(scalar_return=None)

        await _attempt_atomic_assignment(order, candidate, factory)

        session = factory.return_value.__aenter__.return_value
        session.execute.assert_not_called()


# =============================================================================
# assign_best_driver — full pipeline integration
# =============================================================================


class TestAssignBestDriver:

    @pytest.mark.asyncio
    async def test_returns_false_when_no_candidates(self):
        order   = make_order()
        factory = make_session_factory()
        client  = make_maps_client()

        with patch(
            "app.workers.scoring_engine.get_candidates",
            new=AsyncMock(return_value=[]),
        ):
            result = await assign_best_driver(order, factory, client)

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_all_maps_calls_fail(self):
        order   = make_order()
        factory = make_session_factory()
        client  = MagicMock()
        client.get_driving_eta = AsyncMock(
            return_value=RouteETAResult.failure("quota exceeded")
        )

        with patch(
            "app.workers.scoring_engine.get_candidates",
            new=AsyncMock(return_value=[make_driver()]),
        ):
            result = await assign_best_driver(order, factory, client)

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_successful_assignment(self):
        order  = make_order(ready_in_minutes=8)
        client = make_maps_client(duration_seconds=300)

        factory = make_session_factory(scalar_return=make_driver())

        with patch(
            "app.workers.scoring_engine.get_candidates",
            new=AsyncMock(return_value=[make_driver()]),
        ):
            result = await assign_best_driver(order, factory, client)

        assert result is True

    @pytest.mark.asyncio
    async def test_tries_next_candidate_on_race_condition(self):
        winner    = make_driver(name="Winner")
        runner_up = make_driver(name="RunnerUp")
        order  = make_order(ready_in_minutes=8)

        client = MagicMock()
        client.get_driving_eta = AsyncMock(side_effect=[
            RouteETAResult.success(duration_seconds=240, distance_meters=1500),
            RouteETAResult.success(duration_seconds=420, distance_meters=3000),
        ])

        factory = make_session_factory()
        session = factory.return_value.__aenter__.return_value
        session.scalar = AsyncMock(side_effect=[None, runner_up])

        with patch(
            "app.workers.scoring_engine.get_candidates",
            new=AsyncMock(return_value=[winner, runner_up]),
        ):
            result = await assign_best_driver(order, factory, client)

        assert result is True
        assert session.scalar.call_count == 2

    @pytest.mark.asyncio
    async def test_returns_false_when_all_locks_fail(self):
        driver1 = make_driver(name="D1")
        driver2 = make_driver(name="D2")
        order   = make_order(ready_in_minutes=8)

        client = MagicMock()
        client.get_driving_eta = AsyncMock(side_effect=[
            RouteETAResult.success(duration_seconds=240, distance_meters=1000),
            RouteETAResult.success(duration_seconds=360, distance_meters=2000),
        ])

        factory = make_session_factory()
        session = factory.return_value.__aenter__.return_value
        session.scalar = AsyncMock(return_value=None)

        with patch(
            "app.workers.scoring_engine.get_candidates",
            new=AsyncMock(return_value=[driver1, driver2]),
        ):
            result = await assign_best_driver(order, factory, client)

        assert result is False
