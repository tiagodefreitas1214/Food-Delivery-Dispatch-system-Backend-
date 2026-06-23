"""
app/services/maps_models.py
===========================
Typed data models for the Google Maps Distance Matrix API.

These are NOT SQLAlchemy models — they are pure Pydantic value objects
used exclusively inside the maps client and the dispatch scoring engine.
They have no DB footprint.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# =============================================================================
# Input
# =============================================================================

class LatLng(BaseModel):
    """A GPS coordinate pair passed to the Distance Matrix API."""

    lat: float = Field(..., ge=-90.0, le=90.0)
    lng: float = Field(..., ge=-180.0, le=180.0)

    def to_param(self) -> str:
        """Formats as 'lat,lng' for a Google Maps API query string."""
        return f"{self.lat},{self.lng}"


# =============================================================================
# Response — mirrors Google's Distance Matrix element structure
# =============================================================================

class DistanceMatrixStatus(str, Enum):
    """
    Top-level API response statuses.
    Reference: https://developers.google.com/maps/documentation/distance-matrix/response
    """
    OK                  = "OK"
    INVALID_REQUEST     = "INVALID_REQUEST"
    MAX_ELEMENTS_EXCEEDED = "MAX_ELEMENTS_EXCEEDED"
    OVER_DAILY_LIMIT    = "OVER_DAILY_LIMIT"
    OVER_QUERY_LIMIT    = "OVER_QUERY_LIMIT"
    REQUEST_DENIED      = "REQUEST_DENIED"
    UNKNOWN_ERROR       = "UNKNOWN_ERROR"


class ElementStatus(str, Enum):
    """
    Per-element statuses within a Distance Matrix response row.
    ZERO_RESULTS means Google found no route (e.g. origin on an island).
    """
    OK              = "OK"
    NOT_FOUND       = "NOT_FOUND"
    ZERO_RESULTS    = "ZERO_RESULTS"
    MAX_ROUTE_LENGTH_EXCEEDED = "MAX_ROUTE_LENGTH_EXCEEDED"
    UNKNOWN_ERROR   = "UNKNOWN_ERROR"


class RouteETAResult(BaseModel):
    """
    The resolved result for a single (origin → destination) pair.

    duration_seconds is the primary field consumed by the dispatch engine.
    distance_meters is recorded for analytics but never used in scoring.

    If ok=False, both duration_seconds and distance_meters are None.
    The dispatch loop must treat ok=False as a hard HOLD signal.
    """

    ok: bool
    duration_seconds: Optional[int] = None   # Drive time in seconds
    distance_meters: Optional[int] = None    # Route distance in metres

    # Machine-readable reason for failures — used in logging only
    failure_reason: Optional[str] = None

    @property
    def duration_minutes(self) -> Optional[float]:
        """Convenience accessor for the dispatch scoring engine."""
        if self.duration_seconds is None:
            return None
        return self.duration_seconds / 60.0

    @classmethod
    def success(cls, duration_seconds: int, distance_meters: int) -> "RouteETAResult":
        return cls(
            ok=True,
            duration_seconds=duration_seconds,
            distance_meters=distance_meters,
        )

    @classmethod
    def failure(cls, reason: str) -> "RouteETAResult":
        return cls(ok=False, failure_reason=reason)
