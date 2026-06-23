"""
tests/test_maps_client.py
=========================
Integration test for the MapsClient.

**NOTE:** This test requires a real Google Maps API key and network access.
It is NOT run as part of the normal pytest suite. Instead, run it manually
as a standalone script:

Usage:
    export GOOGLE_MAPS_API_KEY="AIzaSyBKKzkH0rvuMvXBbMmBHX1vGRXojHwLKK8"
    python tests/test_maps_client.py

For normal unit test runs (pytest), use:
    pytest tests/test_prep_estimator.py tests/test_dispatch_loop.py

This file is kept here for reference and manual testing but is skipped by
pytest since it requires external infrastructure.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

if __name__ != "__main__":
    pytest.skip(
        "Integration-only MapsClient tests; run manually with GOOGLE_MAPS_API_KEY",
        allow_module_level=True,
    )

from app.services.maps_client import MapsClient
from app.services.maps_models import LatLng, RouteETAResult

# =============================================================================
# Podgorica test fixtures
# =============================================================================

# Restaurant: Promenade restaurant on the Morača river bank
RESTAURANT = LatLng(lat=42.4415, lng=19.2636)

# Driver: Delta City shopping centre car park
DRIVER_NEAR = LatLng(lat=42.4280, lng=19.2490)

# Driver: ~4 km out, near the stadium
DRIVER_FAR = LatLng(lat=42.4200, lng=19.2700)

# Mid-ocean point — guaranteed ZERO_RESULTS for driving
OCEAN = LatLng(lat=0.0, lng=0.0)

SEPARATOR = "─" * 56


def report(label: str, result: RouteETAResult) -> None:
    """Format and print test result with icon."""
    icon = "PASS" if result.ok else "FAIL"
    if result.ok:
        print(
            f"  {icon}: {label}\n"
            f"      duration : {result.duration_seconds}s "
            f"({result.duration_minutes:.1f} min)\n"
            f"      distance : {result.distance_meters} m"
        )
    else:
        print(f"  {icon}: {label}\n      reason   : {result.failure_reason}")


# =============================================================================
# Test cases
# =============================================================================

async def test_happy_path(client: MapsClient) -> None:
    """Test basic ETA retrieval between two close points."""
    print(f"\n{SEPARATOR}")
    print("TEST 1 - Happy path: driver near -> restaurant")
    print(SEPARATOR)
    result = await client.get_driving_eta(origin=DRIVER_NEAR, destination=RESTAURANT)
    assert result.ok, f"Expected ok=True, got: {result.failure_reason}"
    assert result.duration_seconds and result.duration_seconds > 0
    assert result.distance_meters and result.distance_meters > 0
    report("Near driver -> Restaurant", result)
    print("  -> PASS")


async def test_farther_driver(client: MapsClient) -> None:
    """Test that farther driver returns longer ETA than near driver."""
    print(f"\n{SEPARATOR}")
    print("TEST 2 - Far driver: should return longer ETA than near driver")
    print(SEPARATOR)
    result_far = await client.get_driving_eta(origin=DRIVER_FAR, destination=RESTAURANT)
    result_near = await client.get_driving_eta(origin=DRIVER_NEAR, destination=RESTAURANT)
    assert result_far.ok and result_near.ok
    report("Far driver  -> Restaurant", result_far)
    report("Near driver -> Restaurant", result_near)
    assert result_far.duration_seconds >= result_near.duration_seconds, (
        "Expected far driver to take at least as long as near driver"
    )
    print("  -> PASS (far >= near)")


async def test_zero_results(client: MapsClient) -> None:
    """Test that ocean coordinates (no drivable route) return failure in real mode."""
    print(f"\n{SEPARATOR}")
    print("TEST 3 - ZERO_RESULTS: ocean coordinate (no drivable route)")
    print(SEPARATOR)
    result = await client.get_driving_eta(origin=DRIVER_NEAR, destination=OCEAN)
    assert not result.ok, "Expected ok=False for ocean coordinate"
    report("Driver -> Ocean", result)
    print("  -> PASS (correctly returned failure)")


async def test_bad_api_key() -> None:
    """Test that invalid API key fails gracefully."""
    print(f"\n{SEPARATOR}")
    print("TEST 4 - Bad API key: should fail gracefully, not raise")
    print(SEPARATOR)
    async with MapsClient(api_key="INVALID_KEY_FOR_TESTING") as bad_client:
        result = await bad_client.get_driving_eta(
            origin=DRIVER_NEAR, destination=RESTAURANT
        )
    assert not result.ok, "Expected ok=False for invalid key"
    report("Bad API key", result)
    print("  -> PASS (correctly returned failure without raising)")


async def test_timeout_simulation(client: MapsClient) -> None:
    """
    Simulate a timeout by temporarily pointing the client at an unreachable host.
    We monkey-patch the URL constant in the module, make the call, then restore it.
    """
    print(f"\n{SEPARATOR}")
    print("TEST 5 - Timeout: unreachable host")
    print(SEPARATOR)

    import app.services.maps_client as maps_module
    original_url = maps_module.DISTANCE_MATRIX_URL
    maps_module.DISTANCE_MATRIX_URL = "https://192.0.2.1/timeout-test"  # RFC 5737 blackhole

    try:
        result = await client.get_driving_eta(origin=DRIVER_NEAR, destination=RESTAURANT)
    finally:
        maps_module.DISTANCE_MATRIX_URL = original_url

    assert not result.ok, "Expected ok=False for unreachable host"
    report("Unreachable host", result)
    print("  -> PASS (correctly returned failure on timeout/network error)")


# =============================================================================
# Runner
# =============================================================================

async def main() -> None:
    """Main test runner."""
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "")

    if not api_key:
        print("ERROR: GOOGLE_MAPS_API_KEY is not set.")
        print("       export GOOGLE_MAPS_API_KEY='AIzaSyBKKzkH0rvuMvXBbMmBHX1vGRXojHwLKK8'")
        sys.exit(1)

    print("\n" + "=" * 56)
    print("MapsClient Integration Test - Podgorica")
    print("=" * 56)

    try:
        async with MapsClient(api_key=api_key) as client:
            await test_happy_path(client)
            await test_farther_driver(client)
            await test_zero_results(client)
            await test_bad_api_key()
            await test_timeout_simulation(client)

        print(f"\n{SEPARATOR}")
        print("All tests passed. MapsClient is production-ready.")
        print(f"{SEPARATOR}")
    except AssertionError as e:
        print(f"\n{SEPARATOR}")
        print(f"TEST FAILED: {e}")
        print(f"{SEPARATOR}")
        sys.exit(1)
    except Exception as e:
        print(f"\n{SEPARATOR}")
        print(f"ERROR: {e}")
        print(f"{SEPARATOR}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
