"""Shared school-directory and map-coordinate helpers."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
_coordinate_db_init_lock = threading.Lock()
_initialized_coordinate_dbs: set[Path] = set()
_SCHOOL_GEOCODE_CACHE_TTL = timedelta(days=30)
_SCHOOL_GEOCODE_CLAIM_TTL = timedelta(minutes=2)
_MAX_BACKGROUND_GEOCODES = 5
_SCHOOL_GEOCODE_DB_PATH = (
    Path(__file__).parent.parent / "data" / "school_locations.db"
)
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
    """Return fresh process-local or persisted geocoding without network I/O."""
    is_cached, coordinates = _cached_school_coordinates(school_id)
    return coordinates if is_cached else None


def _cached_school_coordinates(
    school_id: str,
) -> tuple[bool, tuple[float, float] | None]:
    """Return both cache presence and its possibly empty coordinate value."""
    school_id = normalize_school_id(school_id)
    cached = _school_geocode_cache.get(school_id)
    if not cached or not isinstance(cached.get("created_at"), datetime):
        persisted = _persisted_school_coordinates(school_id)
        if persisted is None:
            return False, None
        created_at, coordinates = persisted
        _school_geocode_cache[school_id] = {
            "created_at": created_at,
            "coordinates": coordinates,
        }
        return True, coordinates
    created_at = cached["created_at"]
    if datetime.now(timezone.utc) - created_at >= _SCHOOL_GEOCODE_CACHE_TTL:
        persisted = _persisted_school_coordinates(school_id)
        if persisted is None:
            return False, None
        created_at, coordinates = persisted
        _school_geocode_cache[school_id] = {
            "created_at": created_at,
            "coordinates": coordinates,
        }
        return True, coordinates
    coordinates = cached.get("coordinates")
    return True, coordinates if isinstance(coordinates, tuple) else None


def _connect_coordinate_db() -> sqlite3.Connection:
    """Open the process-shared coordinate store used by every API worker."""
    database_path = _SCHOOL_GEOCODE_DB_PATH
    if database_path not in _initialized_coordinate_dbs:
        with _coordinate_db_init_lock:
            if database_path not in _initialized_coordinate_dbs:
                database_path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(database_path, timeout=5) as database:
                    database.execute(
                        """
                        CREATE TABLE IF NOT EXISTS school_coordinates (
                            school_id TEXT PRIMARY KEY,
                            latitude REAL,
                            longitude REAL,
                            created_at TEXT,
                            claim_until TEXT
                        )
                        """
                    )
                    database.execute(
                        """
                        CREATE TABLE IF NOT EXISTS school_geocode_state (
                            id INTEGER PRIMARY KEY CHECK (id = 1),
                            next_request_at TEXT
                        )
                        """
                    )
                    database.execute(
                        """
                        INSERT OR IGNORE INTO school_geocode_state (id, next_request_at)
                        VALUES (1, NULL)
                        """
                    )
                    database.commit()
                _initialized_coordinate_dbs.add(database_path)
    return sqlite3.connect(database_path, timeout=5)


def _persisted_school_coordinates(
    school_id: str,
) -> tuple[datetime, tuple[float, float] | None] | None:
    """Read a fresh coordinate result shared by all processes."""
    try:
        with _connect_coordinate_db() as database:
            row = database.execute(
                """
                SELECT latitude, longitude, created_at
                FROM school_coordinates
                WHERE school_id = ?
                """,
                (school_id,),
            ).fetchone()
    except (OSError, sqlite3.Error):
        logger.warning("Could not read the persisted school-coordinate cache", exc_info=True)
        return None
    if not row or not row[2]:
        return None
    try:
        created_at = datetime.fromisoformat(str(row[2]))
    except ValueError:
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created_at >= _SCHOOL_GEOCODE_CACHE_TTL:
        return None
    coordinates = (
        (float(row[0]), float(row[1]))
        if row[0] is not None and row[1] is not None
        else None
    )
    return created_at, coordinates


def _claim_school_geocode(school_id: str) -> bool:
    """Claim an expired/missing lookup so separate workers do not duplicate it."""
    now = datetime.now(timezone.utc)
    claim_until = now + _SCHOOL_GEOCODE_CLAIM_TTL
    try:
        with _connect_coordinate_db() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT created_at, claim_until FROM school_coordinates WHERE school_id = ?",
                (school_id,),
            ).fetchone()
            if row:
                if row[0]:
                    created_at = datetime.fromisoformat(str(row[0]))
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    if now - created_at < _SCHOOL_GEOCODE_CACHE_TTL:
                        return False
                if row[1]:
                    existing_claim = datetime.fromisoformat(str(row[1]))
                    if existing_claim.tzinfo is None:
                        existing_claim = existing_claim.replace(tzinfo=timezone.utc)
                    if existing_claim > now:
                        return False
            database.execute(
                """
                INSERT INTO school_coordinates (school_id, claim_until)
                VALUES (?, ?)
                ON CONFLICT(school_id) DO UPDATE SET claim_until = excluded.claim_until
                """,
                (school_id, claim_until.isoformat()),
            )
            database.commit()
            return True
    except (OSError, sqlite3.Error, ValueError):
        logger.warning("Could not claim a persisted school-coordinate lookup", exc_info=True)
        return True


def _persist_school_coordinates(
    school_id: str, coordinates: tuple[float, float] | None
) -> None:
    """Store a positive or negative lookup result for all API workers."""
    created_at = datetime.now(timezone.utc)
    try:
        with _connect_coordinate_db() as database:
            database.execute(
                """
                INSERT INTO school_coordinates (
                    school_id, latitude, longitude, created_at, claim_until
                ) VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(school_id) DO UPDATE SET
                    latitude = excluded.latitude,
                    longitude = excluded.longitude,
                    created_at = excluded.created_at,
                    claim_until = NULL
                """,
                (
                    school_id,
                    coordinates[0] if coordinates else None,
                    coordinates[1] if coordinates else None,
                    created_at.isoformat(),
                ),
            )
            database.commit()
    except (OSError, sqlite3.Error):
        logger.warning("Could not persist school coordinates", exc_info=True)
    _school_geocode_cache[school_id] = {
        "created_at": created_at,
        "coordinates": coordinates,
    }


def _wait_for_geocode_slot() -> None:
    """Reserve a global one-request-per-second Nominatim slot across workers."""
    now = datetime.now(timezone.utc)
    try:
        with _connect_coordinate_db() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT next_request_at FROM school_geocode_state WHERE id = 1"
            ).fetchone()
            next_request_at = now
            if row and row[0]:
                stored = datetime.fromisoformat(str(row[0]))
                if stored.tzinfo is None:
                    stored = stored.replace(tzinfo=timezone.utc)
                next_request_at = max(now, stored)
            database.execute(
                "UPDATE school_geocode_state SET next_request_at = ? WHERE id = 1",
                ((next_request_at + timedelta(seconds=1)).isoformat(),),
            )
            database.commit()
        wait_seconds = (next_request_at - now).total_seconds()
    except (OSError, sqlite3.Error, ValueError):
        logger.warning("Could not reserve a persisted Nominatim rate-limit slot", exc_info=True)
        wait_seconds = 1.0
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def geocode_school(
    school_id: str,
    name: str,
    location: str,
) -> tuple[float, float] | None:
    """Resolve and cache a school coordinate for private admin endpoints."""
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
        if not _claim_school_geocode(school_id):
            persisted = _persisted_school_coordinates(school_id)
            return persisted[1] if persisted else None
        _wait_for_geocode_slot()
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

        _persist_school_coordinates(school_id, coordinates)
        return coordinates


def populate_school_coordinates(schools: list[tuple[str, str, str]]) -> None:
    """Populate a bounded number of missing coordinates after the response."""
    lookups = 0
    for school_id, name, location in schools:
        is_cached, _ = _cached_school_coordinates(school_id)
        if is_cached:
            continue
        if lookups >= _MAX_BACKGROUND_GEOCODES:
            break
        geocode_school(school_id, name, location)
        lookups += 1
