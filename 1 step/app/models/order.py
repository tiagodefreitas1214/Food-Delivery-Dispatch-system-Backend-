from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, text
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.driver import Driver


class DispatchStatus(str, enum.Enum):
    """
    State machine for an order through the dispatch lifecycle.

    Transitions (enforced by dispatch loop):
        pending  → holding   (predictedReadyAt > 10 min away)
        pending  → assigned  (predictedReadyAt ≤ 10 min; driver found)
        holding  → assigned  (window expires; driver found on next loop tick)
        assigned → picked_up (driver confirms collection at restaurant)
        picked_up→ delivered (driver confirms drop-off at customer)
        any      → cancelled (explicit cancellation event)

    The dispatch loop only evaluates orders in: [pending, holding]
    """

    PENDING = "pending"
    HOLDING = "holding"
    ASSIGNED = "assigned"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class Order(Base):
    """
    A customer order from creation through delivery.

    Design notes:
        - restaurant_lat/lng are denormalised from the restaurants table.
          This avoids joins in the dispatch hot-path and insulates dispatch
          logic from restaurant schema changes.
        - predicted_ready_at is computed once at creation (Step 3) and
          treated as immutable by the dispatch loop.
        - assigned_driver_id + assigned_at are written in the same atomic
          transaction as the driver lock (Step 5).
    """

    __tablename__ = "orders"

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )

    # ------------------------------------------------------------------
    # Restaurant (coordinates denormalised for dispatch hot-path)
    # ------------------------------------------------------------------
    restaurant_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        comment="References restaurants table (Phase 2 scope)",
    )
    restaurant_lat: Mapped[float] = mapped_column(DOUBLE_PRECISION, nullable=False)
    restaurant_lng: Mapped[float] = mapped_column(DOUBLE_PRECISION, nullable=False)

    # ------------------------------------------------------------------
    # Customer / delivery destination
    # ------------------------------------------------------------------
    customer_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        nullable=False,
        comment="References customers table (Phase 2 scope)",
    )
    delivery_lat: Mapped[float] = mapped_column(DOUBLE_PRECISION, nullable=False)
    delivery_lng: Mapped[float] = mapped_column(DOUBLE_PRECISION, nullable=False)

    # ------------------------------------------------------------------
    # Dispatch state machine
    # ------------------------------------------------------------------
    dispatch_status: Mapped[DispatchStatus] = mapped_column(
        SAEnum(
            DispatchStatus,
            name="dispatch_status",
            create_type=False,
            native_enum=False,
            values_callable=lambda enums: [e.value for e in enums],
        ),
        nullable=False,
        default=DispatchStatus.PENDING,
        index=True,
    )

    # Computed at creation: created_at + 15min prep + 3min buffer (Step 3)
    # Immutable after creation — do not update this in the dispatch loop.
    predicted_ready_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )

    # ------------------------------------------------------------------
    # Assignment — written atomically under SELECT FOR UPDATE (Step 5)
    # ------------------------------------------------------------------
    assigned_driver_id: Mapped[Optional[UUID]] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("drivers.id", name="fk_orders_assigned_driver_id_drivers", ondelete="SET NULL"),
        nullable=True,
    )
    assigned_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Lifecycle timestamps (for SLA monitoring)
    # ------------------------------------------------------------------
    picked_up_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Audit timestamps (updated_at maintained by DB trigger)
    # ------------------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------
    assigned_driver: Mapped[Optional[Driver]] = relationship(
        "Driver",
        foreign_keys=[assigned_driver_id],
        lazy="noload",
    )

    def __repr__(self) -> str:
        return (
            f"<Order id={self.id} "
            f"status={self.dispatch_status.value} "
            f"ready_at={self.predicted_ready_at.isoformat()} "
            f"driver={self.assigned_driver_id}>"
        )
