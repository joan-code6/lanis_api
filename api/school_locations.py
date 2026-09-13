"""Shared school-directory and map-coordinate helpers."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests
from fastapi.concurrency import run_in_threadpool

from schulportal_hessen.base import SchulportalHessenAPI

from .identity import normalize_school_id

logger = logging.getLogger("school_locations")

# The SPH directory has no coordinates. Town centroids provide immediate,
# deterministic public pins while the private admin map may cache more precise
# school coordinates from Nominatim.
_HESSEN_CITY_COORDINATES: dict[str, tuple[float, float]] = {
    "bad hersfeld": (50.87, 9.71),
    "bad homburg": (50.23, 8.62),
    "bad nauheim": (50.36, 8.74),
    "bad vilbel": (50.18, 8.74),
    "bensheim": (49.68, 8.62),
    "darmstadt": (49.87, 8.65),
    "eschborn": (50.14, 8.57),
    "frankfurt": (50.11, 8.68),
    "friedberg": (50.34, 8.76),
    "fulda": (50.55, 9.68),
    "gießen": (50.59, 8.67),
    "giessen": (50.59, 8.67),
    "hanau": (50.13, 8.92),
    "heppenheim": (49.64, 8.64),
    "kassel": (51.31, 9.50),
    "kelkheim": (50.14, 8.45),
    "limburg": (50.38, 8.06),
    "maintal": (50.15, 8.83),
    "marburg": (50.81, 8.77),
    "melsungen": (51.13, 9.55),
    "michelstadt": (49.68, 9.00),
    "mörfelden-walldorf": (49.99, 8.58),
    "moerfelden-walldorf": (49.99, 8.58),
    "neu-isenburg": (50.05, 8.69),
    "neu isenburg": (50.05, 8.69),
    "offenbach": (50.10, 8.77),
    "petersberg": (50.56, 9.72),
    "rüsselsheim": (49.99, 8.42),
    "ruesselsheim": (49.99, 8.42),
    "schwalmstadt": (50.91, 9.22),
    "wetzlar": (50.56, 8.50),
    "wiesbaden": (50.08, 8.24),
    "willingen": (51.29, 8.61),
    "zierenberg": (51.37, 9.30),
}
_school_directory_cache: dict[str, Any] = {"data": None, "created_at": None}
_school_directory_lock = asyncio.Lock()
_school_geocode_cache: dict[str, dict[str, Any]] = {}
_school_geocode_lock = threading.Lock()
_school_geocode_last_request = 0.0
_SCHOOL_GEOCODE_CACHE_TTL = timedelta(days=30)
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_USER_AGENT = "LANIS Admin Portal map/1.0 (private admin tool)"


def _directory_cache_is_fresh(now: datetime) -> bool:
    """Return whether the shared school directory cache can be served."""
    created_at = _school_directory_cache["created_at"]
    return bool(
        isinstance(created_at, datetime)
        and _school_directory_cache["data"]
        and now - created_at < timedelta(hours=12)
    )


async def get_school_directory() -> dict[str, dict[str, Any]]:
    """Return the cached SPH school directory, with one refresh at a time."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if _directory_cache_is_fresh(now):
        return _school_directory_cache["data"]

    async with _school_directory_lock:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if _directory_cache_is_fresh(now):
            return _school_directory_cache["data"]
        client = SchulportalHessenAPI()
        try:
            payload = await run_in_threadpool(client.school_list_get_all)
        except Exception:
            logger.warning("Could not load school directory for map", exc_info=True)
            return _school_directory_cache["data"] or {}
        finally:
            client.close()

        directory: dict[str, dict[str, Any]] = {}
        districts = payload.get("districts", []) if isinstance(payload, dict) else []
        for district in districts:
            schools = district.get("schools", []) if isinstance(district, dict) else []
            for school in schools:
                school_id = normalize_school_id(str(school.get("id", "")))
                if school_id:
                    directory[school_id] = {
                        "name": school.get("name") or school_id,
                        "location": school.get("location") or "",
                        "district": district.get("name") or "",
                    }
        _school_directory_cache["data"] = directory
        _school_directory_cache["created_at"] = now
        return directory


def city_coordinates(location: str) -> tuple[float, float] | None:
    """Return a configured town centroid for a directory location."""
    normalized = str(location or "").casefold().strip()
    for city, coordinates in _HESSEN_CITY_COORDINATES.items():
        if city in normalized:
            return coordinates
    return None


def get_cached_school_coordinates(school_id: str) -> tuple[float, float] | None:
    """Return fresh cached geocoding without performing network I/O."""
    is_cached, coordinates = _cached_school_coordinates(school_id)
    return coordinates if is_cached else None


def _cached_school_coordinates(
    school_id: str,
) -> tuple[bool, tuple[float, float] | None]:
    """Return both cache presence and its possibly empty coordinate value."""
    cached = _school_geocode_cache.get(normalize_school_id(school_id))
    if not cached:
        return False, None
    created_at = cached.get("created_at")
    if not isinstance(created_at, datetime):
        return False, None
    if datetime.now(timezone.utc) - created_at >= _SCHOOL_GEOCODE_CACHE_TTL:
        return False, None
    coordinates = cached.get("coordinates")
    return True, coordinates if isinstance(coordinates, tuple) else None


def geocode_school(
    school_id: str,
    name: str,
    location: str,
) -> tuple[float, float] | None:
    """Resolve and cache a school coordinate for private admin endpoints."""
    global _school_geocode_last_request
    school_id = normalize_school_id(school_id)
    now = datetime.now(timezone.utc)
    is_cached, cached = _cached_school_coordinates(school_id)
    if is_cached:
        return cached

    query = ", ".join(
        part
        for part in (str(name).strip(), str(location).strip(), "Hessen", "Deutschland")
        if part
    )
    if not query:
        _school_geocode_cache[school_id] = {"created_at": now, "coordinates": None}
        return None

    with _school_geocode_lock:
        is_cached, cached = _cached_school_coordinates(school_id)
        if is_cached:
            return cached
        wait_seconds = 1.0 - (time.monotonic() - _school_geocode_last_request)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        try:
            query_parameters = urlencode(
                {
                    "q": query,
                    "format": "jsonv2",
                    "limit": 1,
                    "countrycodes": "de",
                }
            )
            response = requests.get(
                f"{_NOMINATIM_URL}?{query_parameters}",
                headers={"User-Agent": _NOMINATIM_USER_AGENT},
                timeout=8,
            )
            _school_geocode_last_request = time.monotonic()
            response.raise_for_status()
            results = response.json()
            result = results[0] if isinstance(results, list) and results else None
            latitude = float(result["lat"]) if result else None
            longitude = float(result["lon"]) if result else None
            if (
                latitude is None
                or longitude is None
                or not 49.2 <= latitude <= 51.8
                or not 7.4 <= longitude <= 10.2
            ):
                coordinates = None
            else:
                coordinates = (latitude, longitude)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            logger.warning("Could not geocode school %s with OpenStreetMap", school_id)
            coordinates = None

        _school_geocode_cache[school_id] = {
            "created_at": datetime.now(timezone.utc),
            "coordinates": coordinates,
        }
        return coordinates
