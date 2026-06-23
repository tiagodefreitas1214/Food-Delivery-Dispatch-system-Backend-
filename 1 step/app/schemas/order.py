"""
app/schemas/order.py
=====================
Pydantic schemas for the Order API surface.

These are the request/response shapes for the order endpoints.
They are deliberately separate from the SQLAlchemy model — the schema
controls what the client sees; the model controls what the DB stores.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class OrderCreateRequest(BaseModel):
    """
    Payload the client sends to create a new order.

    Coordinates are required at creation time — they are denormalised
    onto the order row so the dispatch loop never needs to join.
    """

    restaurant_id:   UUID  = Field(..., description="UUID of the restaurant")
    restaurant_lat:  float = Field(..., ge=-90.0,  le=90.0,  description="Restaurant latitude")
    restaurant_lng:  float = Field(..., ge=-180.0, le=180.0, description="Restaurant longitude")

    customer_id:     UUID  = Field(..., description="UUID of the customer")
    delivery_lat:    float = Field(..., ge=-90.0,  le=90.0,  description="Delivery latitude")
    delivery_lng:    float = Field(..., ge=-180.0, le=180.0, description="Delivery longitude")


class OrderResponse(BaseModel):
    """
    The order representation returned to the client after creation.
    """

    restaurant_id:      UUID
    customer_id:        UUID

    dispatch_status:    str
    predicted_ready_at: datetime

    assigned_driver_id: UUID | None = None
    assigned_at:        datetime | None = None

    created_at:         datetime
    updated_at:         datetime

    model_config = {"from_attributes": True}
