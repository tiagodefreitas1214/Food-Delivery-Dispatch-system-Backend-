"""
app/services/maps_client.py
============================
Async client for the Google Maps Distance Matrix API.

Design rules (non-negotiable):
  1. Air-distance is strictly prohibited. Every ETA comes from Google.
  2. On ANY failure (network, quota, bad status, missing element),
     return RouteETAResult.failure(...). The dispatch loop then HOLDs.
  3. This client is stateless and dependency-injectable. It owns one
     shared httpx.AsyncClient for connection pooling across all calls.
  4. One call = one (origin, destination) pair. The dispatch loop scores
     candidates one-by-one; batch matrix calls are a Phase 2 optimisation.
Failure surface handled here:
  - Network errors / timeouts          → RouteETAResult.failure
  - HTTP 4xx / 5xx                     → RouteETAResult.failure
  - API-level non-OK status            → RouteETAResult.failure
  - Element-level non-OK status        → RouteETAResult.failure
  - Missing or malformed response keys → RouteETAResult.failure
  - Zero-result routes                 → RouteETAResult.failure

The caller (dispatch loop) never needs a try/except around this client.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.core.config import settings
from app.services.maps_models import (
    DistanceMatrixStatus,
    ElementStatus,
    LatLng,
    RouteETAResult,
)

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

DISTANCE_MATRIX_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"

# Driving mode is the only valid mode for food delivery.
TRAVEL_MODE = "driving"

# Traffic model: best_guess is the balanced default.
# Requires a departure_time — we use "now" (handled by Google when omitted
# with traffic_model; we pass departure_time=now explicitly for accuracy).
TRAFFIC_MODEL = "best_guess"

# Timeout for the Google API call. If Google doesn't respond within this
# window, we return a failure and hold the order.
REQUEST_TIMEOUT_SECONDS: float = 5.0


# =============================================================================
# Client
# =============================================================================

class MapsClient:
    """
    Wraps the Google Distance Matrix API with async HTTP and strict typing.

    Instantiate once at app startup (lifespan) and inject via FastAPI
    dependency or pass directly into the dispatch loop. Do not instantiate
    per-request — the underlying httpx.AsyncClient maintains a connection pool.

    Usage:
        client = MapsClient()
        await client.start()
        result = await client.get_driving_eta(origin, destination)
        await client.stop()

    Or use as an async context manager:
        async with MapsClient() as client:
            result = await client.get_driving_eta(origin, destination)
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        """
        Initialize the MapsClient.

        Args:
            api_key: Google Maps API key. If None, uses settings.google_maps_api_key.
                     If still empty, Maps calls will fail gracefully (order held).
        """
        self._api_key = api_key or settings.google_maps_api_key
        self._http: Optional[httpx.AsyncClient] = None

        if not self._api_key:
            logger.warning(
                "Google Maps API key is not configured. "
                "Maps calls will fail gracefully; orders will be held."
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the shared HTTP connection pool. Call once at app startup."""
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            # Google uses keep-alive; pooling reduces per-call overhead.
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        logger.info("MapsClient: HTTP pool opened.")

    async def stop(self) -> None:
        """Close the HTTP pool. Call at app shutdown."""
        if self._http:
            await self._http.aclose()
            self._http = None
            logger.info("MapsClient: HTTP pool closed.")

    async def __aenter__(self) -> "MapsClient":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Core method — the only entry point the dispatch loop should call
    # ------------------------------------------------------------------

    async def get_driving_eta(
        self,
        origin: LatLng,
        destination: LatLng,
    ) -> RouteETAResult:
        """
        Returns the real-world driving ETA from origin to destination.

        This method NEVER raises. All failure paths return a
        RouteETAResult with ok=False, which the dispatch loop
        treats as a HOLD signal.

        Args:
            origin:      GPS position of the driver (current_lat, current_lng).
            destination: GPS position of the restaurant (restaurant_lat, restaurant_lng).

        Returns:
            RouteETAResult with duration_seconds set on success,
            or ok=False with a failure_reason on any error.
        """
        if not self._api_key:
            logger.warning(
                "Google Maps API key is not configured. "
                "Returning failure to hold order."
            )
            return RouteETAResult.failure("API key not configured")

        if self._http is None:
            logger.error(
                "MapsClient.get_driving_eta called before start(). "
                "Returning failure to hold order."
            )
            return RouteETAResult.failure("Client not initialised")

        params = self._build_params(origin, destination)

        try:
            response = await self._http.get(DISTANCE_MATRIX_URL, params=params)
        except httpx.TimeoutException:
            logger.warning(
                "Google Maps API timed out after %.1fs. "
                "Order will be held until next tick.",
                REQUEST_TIMEOUT_SECONDS,
            )
            return RouteETAResult.failure("Request timed out")
        except httpx.RequestError as exc:
            logger.warning(
                "Google Maps API network error: %s. "
                "Order will be held until next tick.",
                exc,
            )
            return RouteETAResult.failure(f"Network error: {exc}")

        # HTTP-level failure (5xx, 4xx)
        if response.status_code != 200:
            logger.warning(
                "Google Maps API returned HTTP %d. "
                "Order will be held until next tick.",
                response.status_code,
            )
            return RouteETAResult.failure(f"HTTP {response.status_code}")

        return self._parse_response(response)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_params(self, origin: LatLng, destination: LatLng) -> dict[str, str]:
        return {
            "origins":          origin.to_param(),
            "destinations":     destination.to_param(),
            "mode":             TRAVEL_MODE,
            "traffic_model":    TRAFFIC_MODEL,
            "departure_time":   "now",
            "units":            "metric",
            "key":              self._api_key,
        }

    def _parse_response(self, response: httpx.Response) -> RouteETAResult:
        try:
            body: dict = response.json()
        except Exception as exc:
            logger.warning("Failed to parse Google Maps JSON response: %s", exc)
            return RouteETAResult.failure("Invalid JSON response")

        # --- Top-level status ---
        api_status = body.get("status")
        if api_status != DistanceMatrixStatus.OK:
            logger.warning(
                "Google Maps API status: %s. Order will be held.",
                api_status,
            )
            return RouteETAResult.failure(f"API status: {api_status}")

        # --- Drill into rows[0].elements[0] ---
        # We always send exactly one origin + one destination,
        # so the matrix is guaranteed to be 1×1 on success.
        try:
            element: dict = body["rows"][0]["elements"][0]
        except (KeyError, IndexError) as exc:
            logger.warning(
                "Unexpected Distance Matrix response shape: %s. Body: %s",
                exc,
                body,
            )
            return RouteETAResult.failure("Unexpected response structure")

        # --- Element-level status ---
        element_status = element.get("status")
        if element_status != ElementStatus.OK:
            logger.warning(
                "Distance Matrix element status: %s "
                "(origin=%s, destination=%s). Order will be held.",
                element_status,
                body.get("origin_addresses", ["?"])[0],
                body.get("destination_addresses", ["?"])[0],
            )
            return RouteETAResult.failure(f"Element status: {element_status}")

        # --- Extract duration and distance ---
        # Prefer duration_in_traffic when available (requires departure_time=now).
        # Fall back to duration if traffic data isn't returned for this route.
        try:
            duration_seconds = int(
                element["duration_in_traffic"]["value"]
                if "duration_in_traffic" in element
                else element["duration"]["value"]
            )
            distance_meters = int(element["distance"]["value"])
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "Invalid distance matrix element payload: %s. Body: %s",
                exc,
                body,
            )
            return RouteETAResult.failure("Invalid route payload")

        return RouteETAResult.success(
            duration_seconds=duration_seconds,
            distance_meters=distance_meters,
        )
