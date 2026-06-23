"""
app/services/order_service.py
==============================
Order Service — handles order creation with atomic prep time computation.

Responsibilities:
  1. Accept a validated OrderCreateRequest.
  2. Call prep_estimator.compute() to get predicted_ready_at.
  3. Insert the order row with predicted_ready_at + status='pending'
     in a single atomic transaction.
  4. Return the persisted Order ORM object.

What this service does NOT do:
  - It does not evaluate drivers. That is the dispatch loop's job.
  - It does not call Google Maps. ETA comes at dispatch time.
  - It does not set dispatch_status to 'holding'. The dispatch loop
    handles that transition on its first evaluation tick.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import DispatchStatus, Order
from app.schemas.order import OrderCreateRequest
from app.services.prep_estimator import compute


async def create_order(
    db: AsyncSession,
    payload: OrderCreateRequest,
) -> Order:
    """
    Create and persist a new order with predicted_ready_at computed atomically.

    Args:
        db:      An active async database session (from get_db dependency).
        payload: Validated order creation payload.

    Returns:
        The persisted Order ORM object, ready to be serialised into
        an OrderResponse by the route handler.
    """
    now = datetime.now(timezone.utc)

    predicted_ready_at = compute(created_at=now)

    order = Order(
        restaurant_id=payload.restaurant_id,
        restaurant_lat=payload.restaurant_lat,
        restaurant_lng=payload.restaurant_lng,
        customer_id=payload.customer_id,
        delivery_lat=payload.delivery_lat,
        delivery_lng=payload.delivery_lng,
        dispatch_status=DispatchStatus.PENDING,
        predicted_ready_at=predicted_ready_at,
    )

    db.add(order)
    await db.commit()
    await db.refresh(order)

    return order
