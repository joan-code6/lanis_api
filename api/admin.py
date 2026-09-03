"""Admin authentication and read-only observability endpoints.

Admin identities are configured as ``school_id:username`` pairs.  A password is
still checked against Schulportal at login time, but it is never stored as an
admin secret in configuration or returned by normal user-list endpoints.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
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

logger = logging.getLogger("admin")
router = APIRouter(prefix="/admin", tags=["admin"])

ADMIN_TOKEN_EXPIRE_MINUTES = 30
STEP_UP_EXPIRE_MINUTES = 5


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


def _configured_accounts() -> set[str]:
    """Parse the comma-separated allowlist and fail closed on malformed values."""
    raw = os.getenv("LANIS_ADMIN_ACCOUNTS", "")
    accounts: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        school_id, separator, username = item.partition(":")
        if not separator or not school_id.strip() or not username.strip():
            logger.error("Ignoring malformed LANIS_ADMIN_ACCOUNTS entry")
            continue
        accounts.add(make_user_id(school_id, username))
    return accounts


def _admin_secret() -> str:
    secret = os.getenv("LANIS_ADMIN_JWT_SECRET", "").strip()
    if len(secret) < 32:
        return ""
    return secret


def _issue_token(principal: AdminPrincipal, *, step_up: bool = False) -> str:
    secret = _admin_secret()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is not configured",
        )
    minutes = STEP_UP_EXPIRE_MINUTES if step_up else ADMIN_TOKEN_EXPIRE_MINUTES
    now = _utcnow()
    return jwt.encode(
        {
            "sub": principal.user_id,
            "school_id": principal.school_id,
            "username": principal.username,
            "aud": "lanis-admin",
            "typ": "admin-step-up" if step_up else "admin",
            "iat": now,
            "exp": now + timedelta(minutes=minutes),
        },
        secret,
        algorithm="HS256",
    )


def _decode_token(token: str, expected_type: str) -> AdminPrincipal:
    secret = _admin_secret()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin authentication is not configured",
        )
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="lanis-admin",
            options={"require": ["exp", "sub", "aud", "typ"]},
        )
    except jwt.InvalidTokenError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired admin session",
        ) from error
    if payload.get("typ") != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin session type",
        )
    user_id = canonicalize_user_id(str(payload["sub"]))
    if user_id not in _configured_accounts():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin account is no longer enabled",
        )
    school_id, _, canonical_username = user_id.partition(":")
    username = normalize_username(str(payload.get("username") or canonical_username))
    return AdminPrincipal(user_id=user_id, school_id=school_id, username=username)


async def admin_dependency(
    x_admin_token: str = Header(..., alias="X-Admin-Token"),
) -> AdminPrincipal:
    return _decode_token(x_admin_token, "admin")


async def step_up_dependency(
    x_admin_step_up: str = Header(..., alias="X-Admin-Step-Up"),
) -> AdminPrincipal:
    return _decode_token(x_admin_step_up, "admin-step-up")


async def _verify_sph_credentials(school_id: str, username: str, password: str) -> bool:
    client = SchulportalHessenAPI()
    try:
        result = await run_in_threadpool(client.login, school_id, username, password)
        return bool(result.get("success"))
    finally:
        client.close()


async def _record_login(school_id: str, username: str) -> None:
    try:
        await user_metrics_db.record_login(school_id, username)
    except Exception:
        logger.warning("Could not record admin login metric", exc_info=True)


async def _record_admin_action(
    actor_user_id: str, action: str, target_user_id: str = ""
) -> None:
    try:
        await user_metrics_db.record_admin_action(actor_user_id, action, target_user_id)
    except Exception:
        logger.warning("Could not persist admin audit event", exc_info=True)


@router.post("/auth/login", response_model=AdminTokenResponse)
async def admin_login(payload: AdminLoginRequest) -> AdminTokenResponse:
    school_id = normalize_school_id(payload.school_id)
    username = str(payload.username).strip()
    user_id = make_user_id(school_id, username)
    if user_id not in _configured_accounts():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
        )
    if not await _verify_sph_credentials(school_id, payload.username, payload.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
        )
    principal = AdminPrincipal(
        user_id=user_id, school_id=school_id, username=normalize_username(username)
    )
    await _record_login(school_id, username)
    await _record_admin_action(principal.user_id, "admin_login")
    return AdminTokenResponse(
        access_token=_issue_token(principal),
        expires_in=ADMIN_TOKEN_EXPIRE_MINUTES * 60,
        school_id=school_id,
        username=normalize_username(username),
    )


@router.post("/auth/step-up", response_model=AdminStepUpResponse)
async def admin_step_up(
    payload: AdminStepUpRequest,
    principal: AdminPrincipal = Depends(admin_dependency),
) -> AdminStepUpResponse:
    if not await _verify_sph_credentials(
        principal.school_id, principal.username, payload.password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin re-authentication failed",
        )
    await _record_admin_action(principal.user_id, "admin_step_up")
    return AdminStepUpResponse(
        step_up_token=_issue_token(principal, step_up=True),
        expires_in=STEP_UP_EXPIRE_MINUTES * 60,
    )


@router.get("/me")
async def admin_me(
    principal: AdminPrincipal = Depends(admin_dependency),
) -> dict[str, Any]:
    return {
        "success": True,
        "school_id": principal.school_id,
        "username": principal.username,
    }


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


# The public SPH school directory currently provides a school ID, name, town,
# and district, but not coordinates. Keep the small set of coordinates that we
# can verify locally here and label them as town centroids in the response. A
# future directory import can replace these with exact school coordinates
# without changing the admin API contract.
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


@router.get("/users", response_model=AdminUsersResponse)
async def admin_users(
    search: str = Query("", max_length=100),
    school_id: str | None = Query(None, max_length=64),
    state: str | None = Query(None, pattern="^(active|inactive|dormant)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: AdminPrincipal = Depends(admin_dependency),
) -> AdminUsersResponse:
    rows = await _metric_rows()
    summaries = [_summary_from_row(row) for row in rows]
    search_value = search.strip().casefold()
    if search_value:
        summaries = [
            row
            for row in summaries
            if search_value
            in (
                f"{row.school_id}:{row.username} {row.display_name or ''} "
                f"{row.email or ''}"
            ).casefold()
        ]
    if school_id:
        summaries = [
            row for row in summaries if row.school_id == normalize_school_id(school_id)
        ]
    if state:
        summaries = [row for row in summaries if row.activity_state == state]
    summaries.sort(key=lambda row: row.last_seen or row.first_seen or "", reverse=True)
    total = len(summaries)
    return AdminUsersResponse(
        total=total,
        limit=limit,
        offset=offset,
        users=summaries[offset : offset + limit],
    )


@router.get("/users/{user_id:path}")
async def admin_user_detail(
    user_id: str,
    _: AdminPrincipal = Depends(admin_dependency),
) -> dict[str, Any]:
    school_id, separator, username = user_id.partition(":")
    if not separator or not school_id or not username:
        raise HTTPException(status_code=404, detail="User not found")
    row = await _metric_row(school_id, username)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    summary = _summary_from_row(row)
    storage_user_id = make_user_id(school_id, username)
    credentials = await get_refresh_token_by_user_id(storage_user_id)
    preferences, _ = await get_user_preferences(storage_user_id)
    return {
        "success": True,
        "user": summary.model_dump()
        if hasattr(summary, "model_dump")
        else summary.dict(),
        "profile": _profile_from_row(row),
        "credentials": {
            "available": credentials is not None,
            "created_at": credentials.get("created_at") if credentials else None,
            "expires_at": credentials.get("expires_at") if credentials else None,
        },
        "state": {
            "preferences": preferences,
            "notification_preferences": await get_notification_preferences(
                storage_user_id
            ),
            "custom_lessons": await get_custom_lessons(storage_user_id),
            "class_links": await get_class_link_overrides(storage_user_id),
            "push_subscription_count": len(
                await get_push_subscriptions(storage_user_id)
            ),
        },
    }


@router.post("/users/{user_id:path}/credentials/reveal")
async def reveal_user_password(
    user_id: str,
    _: AdminPrincipal = Depends(step_up_dependency),
) -> dict[str, Any]:
    school_id, separator, username = user_id.partition(":")
    if not separator or not school_id or not username:
        raise HTTPException(status_code=404, detail="User not found")
    storage_user_id = make_user_id(school_id, username)
    credentials = await get_refresh_token_by_user_id(storage_user_id)
    if not credentials:
        raise HTTPException(
            status_code=404, detail="No active stored credential for this user"
        )
    target_user_id = make_user_id(school_id, username)
    await _record_admin_action(_.user_id, "credential_reveal", target_user_id)
    logger.warning("Admin credential reveal for %s", target_user_id)
    return {
        "success": True,
        "school_id": school_id,
        "username": username,
        "password": credentials["password"],
        "expires_at": credentials.get("expires_at"),
    }


@router.get("/audit")
async def admin_audit(
    limit: int = Query(100, ge=1, le=200),
    _: AdminPrincipal = Depends(admin_dependency),
) -> dict[str, Any]:
    rows = await get_admin_audit(limit)
    return {"success": True, "events": rows}


@router.get("/metrics/overview")
async def admin_metrics_overview(
    days: int = Query(30, ge=1, le=90),
    _: AdminPrincipal = Depends(admin_dependency),
) -> dict[str, Any]:
    from .api import task_queue

    stats = await user_metrics_db.get_stats()
    series = await user_metrics_db.get_login_series(_utcnow() - timedelta(days=days))
    runtime = {"provider": "sqlite", "queue": task_queue.get_queue_stats()}
    stats.pop("db_path", None)
    rows = await _metric_rows()
    average_seconds = (
        stats.get("total_active_seconds", 0) / stats.get("total_users", 1)
        if stats.get("total_users")
        else 0
    )
    return {
        "success": True,
        "generated_at": _utcnow().isoformat() + "Z",
        "range_days": days,
        "definitions": {
            "active_time": (
                "Approximate authenticated activity time; idle gaps over five minutes "
                "are excluded."
            ),
            "active_state": (
                "Active means seen within 24 hours; inactive within 30 days; dormant "
                "after that."
            ),
        },
        "summary": {
            **stats,
            "average_active_seconds_per_user": round(average_seconds),
            "dormant_users": sum(
                _summary_from_row(row).activity_state == "dormant" for row in rows
            ),
            "inactive_users": sum(
                _summary_from_row(row).activity_state == "inactive" for row in rows
            ),
        },
        "login_series": series,
        "runtime": runtime,
    }


@router.get("/metrics/schools/map")
async def admin_metrics_schools_map(
    days: int = Query(30, ge=1, le=90),
    _: AdminPrincipal = Depends(admin_dependency),
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
        "coordinate_note": "Coordinates are verified town centroids until exact school coordinates are imported.",
        "summary": {
            "schools": len(schools),
            "mapped_schools": mapped,
            "known_users": sum(entry["known_users"] for entry in schools.values()),
            "active_users_24h": sum(entry["active_users_24h"] for entry in schools.values()),
            "active_users_7d": sum(entry["active_users_7d"] for entry in schools.values()),
        },
        "schools": sorted(schools.values(), key=lambda entry: entry["known_users"], reverse=True),
    }
