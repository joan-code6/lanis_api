"""Privacy-preserving public data for the LANIS homepage."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any

from fastapi import APIRouter

from .metrics import user_metrics_db
from .school_locations import (
    city_coordinates,
    get_cached_school_coordinates,
    get_school_directory,
)

router = APIRouter(prefix="/homepage", tags=["homepage"])

_MINIMUM_ACCOUNTS_PER_PIN = 5


def _coordinates_for_school(
    school_id: str, location: str
) -> tuple[float, float] | None:
    """Resolve a pin from local caches without remote request-time geocoding."""
    coordinates = get_cached_school_coordinates(school_id) or city_coordinates(
        location
    )
    if coordinates is None:
        return None
    latitude, longitude = coordinates
    if not (
        isfinite(latitude)
        and isfinite(longitude)
        and 49.2 <= latitude <= 51.8
        and 7.4 <= longitude <= 10.2
    ):
        return None
    return coordinates


@router.get("/user-map")
async def homepage_user_map() -> dict[str, Any]:
    """Return global adoption totals and privacy-thresholded school pins.

    School pins contain directory metadata only and appear once at least five
    accounts exist for the school. Account and activity counts are never
    attached to an individual school in this public response.
    """
    known_users, known_schools, qualifying_schools = (
        await user_metrics_db.get_homepage_adoption(
            minimum=_MINIMUM_ACCOUNTS_PER_PIN
        )
    )
    directory = await get_school_directory() if qualifying_schools else {}

    schools: list[dict[str, Any]] = []
    for school_id in qualifying_schools:
        school = directory.get(school_id, {})
        location = str(school.get("location") or "")
        coordinates = _coordinates_for_school(school_id, location)
        if coordinates is None:
            continue
        schools.append(
            {
                "school_id": school_id,
                "name": str(school.get("name") or school_id),
                "city": location,
                "latitude": coordinates[0],
                "longitude": coordinates[1],
            }
        )

    schools.sort(key=lambda school: (school["name"].casefold(), school["school_id"]))
    return {
        "success": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "known_users": known_users,
        "known_schools": known_schools,
        "mapped_schools": len(schools),
        "schools": schools,
    }
