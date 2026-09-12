"""All Logic connected to the Homepage such being metrics.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import jwt
import requests
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from schulportal_hessen.base import SchulportalHessenAPI

from .auth_db import (
    get_admin_audit,
    get_class_link_overrides,
    get_custom_lessons,
    get_notification_preferences,
    get_push_subscriptions,
    get_refresh_token_by_user_id,
    get_user_preferences,
)
from .identity import (
    canonicalize_user_id,
    make_user_id,
    normalize_school_id,
    normalize_username,
)
from .metrics import user_metrics_db
from .uptime import get_uptime_status, run_uptime_check

logger = logging.getLogger("homepage")
router = APIRouter(prefix="/homepage", tags=["homepage"])

def _utcnow() -> datetime:
    """Return a naive UTC timestamp for compatibility with existing stores."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AdminLoginRequest(BaseModel):
    school_id: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class AdminStepUpRequest(BaseModel):
    password: str = Field(..., min_length=1)


class AdminTokenResponse(BaseModel):
    access_token: str
    expires_in: int
    school_id: str
    username: str


class AdminStepUpResponse(BaseModel):
    step_up_token: str
    expires_in: int


class AdminUserSummary(BaseModel):
    user_id: str
    school_id: str
    username: str
    display_name: str | None = None
    email: str | None = None
    class_name: str | None = None
    first_seen: str | None = None
    last_login: str | None = None
    last_seen: str | None = None
    login_count: int = 0
    session_count: int = 0
    total_active_seconds: int = 0
    activity_state: str


class AdminUsersResponse(BaseModel):
    success: bool = True
    total: int
    limit: int
    offset: int
    users: list[AdminUserSummary]


@dataclass(frozen=True)
class AdminPrincipal:
    user_id: str
    school_id: str
    username: str

def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except ValueError:
        return None


def _profile_value(profile: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = profile.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _profile_from_row(row: Any) -> dict[str, Any]:
    if hasattr(row, "user_data"):
        return row.user_data or {}
    try:
        return json.loads(row.get("user_data_json") or "{}")
    except (TypeError, ValueError):
        return {}


def _summary_from_row(row: Any) -> AdminUserSummary:
    if hasattr(row, "school_id"):
        values = {
            "user_id": make_user_id(row.school_id, row.login),
            "school_id": row.school_id,
            "username": row.login,
            "first_seen": row.first_seen.isoformat() if row.first_seen else None,
            "last_login": row.last_login.isoformat() if row.last_login else None,
            "last_seen": row.last_seen.isoformat() if row.last_seen else None,
            "login_count": row.login_count,
            "session_count": row.session_count,
            "total_active_seconds": row.total_active_seconds,
        }
    else:
        values = {
            "user_id": make_user_id(row.get("school_id", ""), row.get("login", "")),
            "school_id": row.get("school_id", ""),
            "username": row.get("login", ""),
            "first_seen": row.get("first_seen"),
            "last_login": row.get("last_login"),
            "last_seen": row.get("last_seen"),
            "login_count": int(row.get("login_count", 0) or 0),
            "session_count": int(row.get("session_count", 0) or 0),
            "total_active_seconds": int(row.get("total_active_seconds", 0) or 0),
        }
    profile = _profile_from_row(row)
    values.update(
        display_name=_profile_value(profile, "name", "full_name", "displayName"),
        email=_profile_value(profile, "email", "mail", "email_address"),
        class_name=_profile_value(profile, "class", "class_name", "klasse"),
    )
    last_seen = _parse_iso(values["last_seen"])
    now = _utcnow()
    if not last_seen or now - last_seen > timedelta(days=30):
        values["activity_state"] = "dormant"
    elif now - last_seen > timedelta(days=1):
        values["activity_state"] = "inactive"
    else:
        values["activity_state"] = "active"
    return AdminUserSummary(**values)


async def _metric_rows(limit: int = 5000, offset: int = 0) -> list[Any]:
    return await user_metrics_db.get_all_users(limit=limit, offset=offset)


async def _metric_row(school_id: str, username: str) -> Any:
    return await user_metrics_db.get_user(school_id, username)


# The public SPH school directory currently provides a school ID, full name,
# town, and district, but not coordinates. OpenStreetMap/Nominatim is used to
# resolve the full school name plus town. These town centroids are only a
# fallback for schools that OSM cannot resolve.
_HESSEN_CITY_COORDINATES: dict[str, tuple[float, float]] = {
    "bad hersfeld": (50.87, 9.71),
    "bad homburg": (50.23, 8.62),
    "bad nauheim": (50.36, 8.74),
    "bad vilbel": (50.18, 8.74),
    "bensheim": (49.68, 8.62),
    "darmstadt": (49.87, 8.65),
    "eschborn": (50.14, 8.57),
    "fulda": (50.55, 9.68),
    "gießen": (50.59, 8.67),
    "giessen": (50.59, 8.67),
    "frankfurt": (50.11, 8.68),
    "friedberg": (50.34, 8.76),
    "hanau": (50.13, 8.92),
    "heppenheim": (49.64, 8.64),
    "kassel": (51.31, 9.50),
    "kelkheim": (50.14, 8.45),
    "limburg": (50.38, 8.06),
    "marburg": (50.81, 8.77),
    "melsungen": (51.13, 9.55),
    "maintal": (50.15, 8.83),
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
_school_geocode_cache: dict[str, dict[str, Any]] = {}
_school_geocode_lock = threading.Lock()
_school_geocode_last_request = 0.0
_SCHOOL_GEOCODE_CACHE_TTL = timedelta(days=30)
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_USER_AGENT = "LANIS Admin Portal map/1.0 (private admin tool)"


async def _get_school_directory() -> dict[str, dict[str, Any]]:
    now = _utcnow()
    created_at = _school_directory_cache["created_at"]
    if created_at and _school_directory_cache["data"] and now - created_at < timedelta(hours=12):
        return _school_directory_cache["data"]
    client = SchulportalHessenAPI()
    try:
        payload = await run_in_threadpool(client.school_list_get_all)
    except Exception:
        logger.warning("Could not load school directory for admin map", exc_info=True)
        return _school_directory_cache["data"] or {}
    finally:
        client.close()
    directory: dict[str, dict[str, Any]] = {}
    for district in payload.get("districts", []) if isinstance(payload, dict) else []:
        for school in district.get("schools", []) if isinstance(district, dict) else []:
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


def _city_coordinates(location: str) -> tuple[float, float] | None:
    normalized = str(location or "").casefold().strip()
    for city, coordinates in _HESSEN_CITY_COORDINATES.items():
        if city in normalized:
            return coordinates
    return None


def _geocode_school(
    school_id: str,
    name: str,
    location: str,
) -> tuple[float, float] | None:
    """Resolve a full school name to an OSM coordinate with a small cache.

    Nominatim asks clients to identify themselves and keep requests to roughly
    one per second. The process-local cache avoids repeating lookups on every
    dashboard refresh, while the lock keeps concurrent admin requests within
    that limit. A failed lookup is cached too so an unknown school does not
    repeatedly hit the geocoder.
    """
    global _school_geocode_last_request
    now = datetime.now(timezone.utc)
    cached = _school_geocode_cache.get(school_id)
    if cached and now - cached["created_at"] < _SCHOOL_GEOCODE_CACHE_TTL:
        return cached["coordinates"]

    query = ", ".join(
        part
        for part in (str(name).strip(), str(location).strip(), "Hessen", "Deutschland")
        if part
    )
    if not query:
        _school_geocode_cache[school_id] = {"created_at": now, "coordinates": None}
        return None

    with _school_geocode_lock:
        # Another request may have filled the cache while this one waited.
        cached = _school_geocode_cache.get(school_id)
        if cached and now - cached["created_at"] < _SCHOOL_GEOCODE_CACHE_TTL:
            return cached["coordinates"]

        wait_seconds = 1.0 - (time.monotonic() - _school_geocode_last_request)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        try:
            response = requests.get(
                f"{_NOMINATIM_URL}?{urlencode({'q': query, 'format': 'jsonv2', 'limit': 1, 'countrycodes': 'de'})}",
                headers={"User-Agent": _NOMINATIM_USER_AGENT},
                timeout=8,
            )
            _school_geocode_last_request = time.monotonic()
            response.raise_for_status()
            results = response.json()
            result = results[0] if isinstance(results, list) and results else None
            latitude = float(result["lat"]) if result else None
            longitude = float(result["lon"]) if result else None
            # Keep an accidental fuzzy match outside Hessen from becoming a
            # misleading school pin.
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




@router.get("/user-map")
async def homepage_user_map(
    days: int = Query(30, ge=1, le=90),
    ) -> dict[str, Any]:
    """Return school-level usage aggregates for the private Hessen map.

    A school is included only after an account from that school has completed a
    successful login. Coordinates are deliberately school/town-level and are
    never inferred from user activity or device data.
    """

    now = _utcnow()
    directory = await _get_school_directory()
    rows = await _metric_rows(limit=5000)
    schools: dict[str, dict[str, Any]] = {}
    since = now - timedelta(days=days)
    for row in rows:
        summary = _summary_from_row(row)
        school_id = normalize_school_id(summary.school_id)
        entry = schools.setdefault(
            school_id,
            {
                "school_id": school_id,
                "name": directory.get(school_id, {}).get("name") or school_id,
                "location": directory.get(school_id, {}).get("location") or "",
                "district": directory.get(school_id, {}).get("district") or "",
                "known_users": 0,
                "active_users_24h": 0,
                "active_users_7d": 0,
                "active_users_range": 0,
                "logins": 0,
            },
        )
        entry["known_users"] += 1
        entry["logins"] += summary.login_count
        last_seen = _parse_iso(summary.last_seen)
        if last_seen and now - last_seen <= timedelta(days=1):
            entry["active_users_24h"] += 1
        if last_seen and now - last_seen <= timedelta(days=7):
            entry["active_users_7d"] += 1
        if last_seen and last_seen >= since:
            entry["active_users_range"] += 1

    for entry in schools.values():
        coordinates = await run_in_threadpool(
            _geocode_school,
            entry["school_id"],
            entry["name"],
            entry["location"],
        )
        if coordinates:
            entry["latitude"], entry["longitude"] = coordinates
            entry["coordinate_source"] = "openstreetmap"
            continue
        coordinates = _city_coordinates(entry["location"])
        if coordinates:
            entry["latitude"], entry["longitude"] = coordinates
            entry["coordinate_source"] = "town-centroid"
        else:
            entry["latitude"] = None
            entry["longitude"] = None
            entry["coordinate_source"] = None

    mapped = sum(1 for entry in schools.values() if entry["latitude"] is not None)
    return {
        "success": True,
        "generated_at": now.isoformat() + "Z",
        "range_days": days,
        "coordinate_note": "School names are geocoded with OpenStreetMap. Town centroids are used only when a school cannot be found.",
        "summary": {
            "schools": len(schools),
            "mapped_schools": mapped,
            "known_users": sum(entry["known_users"] for entry in schools.values()),
        },
        "schools": sorted(schools.values(), key=lambda entry: entry["known_users"], reverse=True),
    }
