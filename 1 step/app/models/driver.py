from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import DOUBLE_PRECISION, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base

if TYPE_CHECKING:
    from app.models.order import Order


class DriverStatus(str, enum.Enum):
    """
    Operational state of a driver.

    Transitions:
        offline → online_available  (driver opens the app)
        online_available → online_busy  (dispatch loop assigns an order)
        online_busy → online_available  (order delivered or cancelled)
        any → offline  (driver closes the app)
    """

    ONLINE_AVAILABLE = "online_available"
    ONLINE_BUSY = "online_busy"
    OFFLINE = "offline"


class Driver(Base):
    """
    A delivery driver in the field.

    Staleness rule (enforced by dispatch loop, not the DB):
        If last_location_at < NOW() - 5 minutes, this driver is excluded
        from candidate scoring regardless of their status field.

    Race-condition rule:
        The dispatch loop must acquire a SELECT FOR UPDATE lock on this row
        before flipping status to online_busy. Never update status + current_order_id
        outside a transaction with that lock held.
    """

    __tablename__ = "drivers"

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)

    # ------------------------------------------------------------------
    # Operational state
    # ------------------------------------------------------------------
    status: Mapped[DriverStatus] = mapped_column(
        SAEnum(
            DriverStatus,
            name="driver_status",
            create_type=False,
            native_enum=False,
            values_callable=lambda enums: [e.value for e in enums],
        ),
        nullable=False,
        default=DriverStatus.OFFLINE,
    )

    # GPS position — NULL when driver is offline
    current_lat: Mapped[Optional[float]] = mapped_column(
        DOUBLE_PRECISION, nullable=True
    )
    current_lng: Mapped[Optional[float]] = mapped_column(
        DOUBLE_PRECISION, nullable=True
    )

    # Last GPS heartbeat — critical for staleness filtering
    last_location_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Set atomically alongside status = online_busy (Step 5)
    current_order_id: Mapped[Optional[UUID]] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("orders.id", name="fk_drivers_current_order_id_orders", ondelete="SET NULL"),
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
    current_order: Mapped[Optional[Order]] = relationship(
        "Order",
        foreign_keys=[current_order_id],
        lazy="noload",  # Never auto-load; dispatch loop fetches explicitly
    )

    def __repr__(self) -> str:
        return (
            f"<Driver id={self.id} name={self.name!r} "
            f"status={self.status.value} "
            f"lat={self.current_lat} lng={self.current_lng}>"
        )
