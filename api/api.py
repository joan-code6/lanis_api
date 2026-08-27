"""
FastAPI wrapper around `functions` to expose Schulportal Hessen endpoints.

Features:
- Per-API-user session tokens so multiple users with different credentials can work concurrently.
- Thin wrappers around the existing `SchulportalHessenAPI` methods.
- Long-term refresh tokens (90 days, stored in SQLite) survive backend restarts.
- Short-term access tokens (1 hour, JWT) are validated purely in-memory.
- In-memory Schulportal client cache minimises DB reads.

Run locally:
        python -m api
"""

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests as http_requests
from fastapi import Body, Depends, FastAPI, Form, Header, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

import jwt

from schulportal_hessen.base import SchulportalHessenAPI

from .queue import task_queue, Task, TaskPriority
from .identity import (
    canonicalize_user_id,
    make_user_id,
    normalize_school_id,
    normalize_username,
)
from .metrics import user_metrics_db
from .dsb_snapshot import dsb_snapshot_db, run_dsb_scheduler
from .documentation import router as documentation_router
from .auth_db import (
    initialize as auth_db_initialize,
    store_refresh_token,
    get_refresh_token,
    get_refresh_token_by_user_id,
    delete_refresh_token,
    delete_user_tokens,
    delete_push_subscription,
    delete_user_push_subscriptions,
    get_notification_preferences,
    save_notification_preferences,
    save_push_subscription,
    get_class_link_overrides,
    save_class_link,
    delete_class_link,
    get_custom_lessons,
    save_custom_lesson,
    delete_custom_lesson,
)
from .file_cache import (
    get_file_hash,
    is_file_cached,
    is_file_pending,
    mark_pending,
    unmark_pending,
    write_pending_meta,
    save_file,
    get_meta,
    get_content_path,
)
from .timetable_enrichment import enrich_timetable
from .user_overrides import (
    apply_custom_lessons,
    current_timetable_monday,
    merge_class_link_overrides,
)
from .message_notifications import (
    push_configured,
    run_message_notification_scheduler,
    send_test_push_notification,
    is_valid_push_subscription,
    is_trusted_push_endpoint,
    validate_notification_preferences,
)
from .server_config import load_server_config

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("api")


SESSION_TTL_SECONDS = 1 * 60 * 60  # expire inactive Schulportal sessions after 1 hour
CACHE_TTL_SECONDS = 10 * 60  # cache responses for 10 minutes
SERVER_CONFIG = load_server_config()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", SERVER_CONFIG.public_url).rstrip("/")
LONG_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60  # cache for 30 days (1 month)
LONG_CACHE_ENDPOINTS = {
    "/modules",
    "/apps",
    "/benutzer",
}  # endpoints with long-term cache

ACCESS_TOKEN_EXPIRE_MINUTES = 60
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    JWT_SECRET = uuid.uuid4().hex
    logger.warning(
        "JWT_SECRET not set in environment — using random value %s. "
        "Access tokens will be invalidated on restart. "
        "Set JWT_SECRET in .env for persistent tokens.",
        JWT_SECRET,
    )
JWT_ALGORITHM = "HS256"


def _make_user_id(school_id: str, username: str) -> str:
    return make_user_id(school_id, username)


# --- Pydantic Models ---

class LoginRequest(BaseModel):
    school_id: str = Field(..., description="Schul-ID (e.g. 1234)")
    username: str = Field(..., description="Username without school prefix")
    password: str = Field(..., description="User password")


class DsbLoginRequest(BaseModel):
    username: str = Field(..., description="DSBmobile username or school identifier")
    password: str = Field(..., description="DSBmobile password")


class DsbPlanRequest(BaseModel):
    username: Optional[str] = Field(
        None, description="DSBmobile username or school identifier"
    )
    password: Optional[str] = Field(None, description="DSBmobile password")
    plan_index: int = Field(0, description="Which plan iframe index to fetch")
    plan_url: Optional[str] = Field(
        None, description="Explicit plan URL (overrides plan_index)"
    )
    include_raw: bool = Field(
        False, description="Include raw HTML of the plan page in the response"
    )


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    school_id: str
    username: str
    encryption_ready: bool
    expires_in: int = ACCESS_TOKEN_EXPIRE_MINUTES * 60


class TokenRefreshRequest(BaseModel):
    refresh_token: str = Field(..., description="Long-term refresh token")


class TokenRefreshResponse(BaseModel):
    access_token: str
    expires_in: int = ACCESS_TOKEN_EXPIRE_MINUTES * 60


class NotificationPreferencesRequest(BaseModel):
    enabled: bool = Field(False, description="Poll for new messages and send push notifications")
    start_time: str = Field("07:00", description="Local time at which polling may start")
    end_time: str = Field("21:00", description="Local time at which polling may stop")
    poll_interval_minutes: int = Field(15, ge=5, le=60)
    timezone: str = Field("Europe/Berlin", description="IANA timezone used for the polling window")
    show_preview: bool = Field(True, description="Include sender and subject in push notifications")


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionRequest(BaseModel):
    endpoint: str = Field(..., min_length=1)
    keys: PushSubscriptionKeys


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


class CustomLessonRequest(BaseModel):
    date: str = Field(..., description="Lesson date in YYYY-MM-DD format")
    period: str = Field(..., min_length=1, max_length=30)
    subject: str = Field("", max_length=200)
    teacher: str = Field("", max_length=200)
    room: str = Field("", max_length=100)
    class_name: str = Field("", max_length=100)
    info: str = Field("", max_length=500)
    start_time: str = Field("", max_length=5)
    end_time: str = Field("", max_length=5)
    duration: int = Field(1, ge=1, le=12)
    week_type: Optional[str] = Field(None, description="A or B")
    course_id: Optional[str] = Field(None, max_length=200)
    removed: bool = False


class ClassLinkRequest(BaseModel):
    course_id: str = Field(..., min_length=1, max_length=200)
    url: str = Field("", max_length=2000)


# --- Internal Data Structures ---

@dataclass
class SchulportalSessionData:
    """A live Schulportal HTTP session cached in memory."""

    client: SchulportalHessenAPI
    created_at: datetime
    last_used: datetime
    username: str
    school_id: str


@dataclass
class AuthSession:
    """Result of authenticating a request — the client and user identity."""

    client: SchulportalHessenAPI
    user_id: str
    school_id: str
    username: str


@dataclass
class CacheEntry:
    """Represents a cached response with timestamp."""

    user_id: str
    data: Any
    created_at: datetime
    is_long_term: bool = False
    endpoint: str = ""
    params: str = ""

    def is_expired(self, ttl_seconds: int) -> bool:
        return datetime.utcnow() - self.created_at > timedelta(seconds=ttl_seconds)

    def is_stale(self, ttl_seconds: int) -> bool:
        return datetime.utcnow() - self.created_at > timedelta(seconds=ttl_seconds // 2)


# --- Auth / Session Manager ---


class AuthManager:
    """
    Manages authentication, sessions, and caching.

    Refresh tokens  → persisted in SQLite (survive restart).
    Access tokens   → signed JWTs (validated in-memory, no DB hit).
    Schulportal sessions → in-memory cache keyed by user_id (lazy re-login on miss).
    API response cache   → in-memory keyed by user_id + endpoint + params.
    """

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self._schulportal_clients: Dict[str, SchulportalSessionData] = {}
        self._cache: Dict[str, CacheEntry] = {}
        self._cache_versions: Dict[tuple[str, str], int] = {}
        self._lock = asyncio.Lock()
        self._ttl = ttl_seconds

    # -- JWT helpers -------------------------------------------------

    def create_access_token(self, user_id: str, school_id: str, username: str) -> str:
        user_id = canonicalize_user_id(user_id)
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        payload = {
            "sub": user_id,
            "school_id": school_id,
            "username": username,
            "iat": datetime.utcnow(),
            "exp": expire,
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

    def decode_access_token(self, token: str) -> dict:
        try:
            payload = jwt.decode(
                token, JWT_SECRET, algorithms=[JWT_ALGORITHM], options={"require": ["exp", "sub"]}
            )
            return payload
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Access token expired",
            )
        except jwt.InvalidTokenError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid access token",
            )

    # -- Schulportal client cache ------------------------------------

    async def _purge_expired_schulportal(self) -> None:
        now = datetime.utcnow()
        async with self._lock:
            expired = [
                uid
                for uid, data in self._schulportal_clients.items()
                if now - data.last_used > timedelta(seconds=self._ttl)
            ]
            for uid in expired:
                data = self._schulportal_clients.pop(uid)
                data.client.close()

    async def _get_or_create_schulportal_client(
        self, user_id: str
    ) -> SchulportalSessionData:
        user_id = canonicalize_user_id(user_id)
        await self._purge_expired_schulportal()

        async with self._lock:
            data = self._schulportal_clients.get(user_id)
            if data:
                data.last_used = datetime.utcnow()
                return data

        # Cache miss — re-establish Schulportal session from DB credentials
        rt_data = await get_refresh_token_by_user_id(user_id)
        if not rt_data:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No valid session found — please log in again",
            )

        client = SchulportalHessenAPI()
        login_result = await run_in_threadpool(
            client.login, rt_data["school_id"], rt_data["username"], rt_data["password"]
        )
        if not login_result.get("success"):
            client.close()
            await delete_user_tokens(user_id)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session re-establishment failed — please log in again",
            )

        session_data = SchulportalSessionData(
            client=client,
            created_at=datetime.utcnow(),
            last_used=datetime.utcnow(),
            username=rt_data["username"],
            school_id=rt_data["school_id"],
        )

        async with self._lock:
            self._schulportal_clients[user_id] = session_data

        return session_data

    async def create_schulportal_session(
        self, school_id: str, username: str, password: str
    ) -> str:
        """Log into Schulportal, cache the client, return user_id."""
        school_id = normalize_school_id(school_id)
        username = str(username).strip()
        user_id = _make_user_id(school_id, username)
        client = SchulportalHessenAPI()

        login_result = await run_in_threadpool(
            client.login, school_id, username, password
        )
        if not login_result.get("success"):
            client.close()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=login_result
            )

        session_data = SchulportalSessionData(
            client=client,
            created_at=datetime.utcnow(),
            last_used=datetime.utcnow(),
            username=username,
            school_id=school_id,
        )
        async with self._lock:
            self._schulportal_clients[user_id] = session_data
        return user_id

    async def drop_schulportal_session(self, user_id: str) -> None:
        """Close and remove a Schulportal session."""
        user_id = canonicalize_user_id(user_id)
        async with self._lock:
            data = self._schulportal_clients.pop(user_id, None)
        if data:
            await run_in_threadpool(data.client.logout)
            data.client.close()
        await self.invalidate_user_cache(user_id)

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = list(self._schulportal_clients.items())
            self._schulportal_clients.clear()
        for _, data in sessions:
            await run_in_threadpool(data.client.logout)
            data.client.close()

    # -- Response cache ----------------------------------------------

    async def _purge_expired_cache(self) -> None:
        async with self._lock:
            expired = [
                key
                for key, entry in self._cache.items()
                if entry.is_expired(CACHE_TTL_SECONDS)
            ]
            for key in expired:
                self._cache.pop(key)

    def _make_cache_key(self, user_id: str, endpoint: str, params: str = "") -> str:
        key_str = f"{user_id}:{endpoint}:{params}"
        return hashlib.sha256(key_str.encode()).hexdigest()

    async def get_cached(
        self, user_id: str, endpoint: str, params: str = ""
    ) -> Optional[Any]:
        await self._purge_expired_cache()
        cache_key = self._make_cache_key(user_id, endpoint, params)

        async with self._lock:
            entry = self._cache.get(cache_key)
            if entry:
                ttl = (
                    LONG_CACHE_TTL_SECONDS if entry.is_long_term else CACHE_TTL_SECONDS
                )
                if not entry.is_expired(ttl):
                    return entry.data
                else:
                    self._cache.pop(cache_key)

        return None

    async def get_cached_with_revalidate(
        self, user_id: str, endpoint: str, params: str = ""
    ) -> tuple:
        await self._purge_expired_cache()
        cache_key = self._make_cache_key(user_id, endpoint, params)

        async with self._lock:
            entry = self._cache.get(cache_key)
            if entry:
                ttl = (
                    LONG_CACHE_TTL_SECONDS if entry.is_long_term else CACHE_TTL_SECONDS
                )
                if not entry.is_expired(ttl):
                    if entry.is_long_term and entry.is_stale(ttl):
                        return entry.data, True
                    return entry.data, False
                else:
                    self._cache.pop(cache_key)

        return None, False

    async def set_cache(
        self,
        user_id: str,
        endpoint: str,
        data: Any,
        params: str = "",
        is_long_term: bool = False,
    ) -> None:
        cache_key = self._make_cache_key(user_id, endpoint, params)
        async with self._lock:
            self._cache[cache_key] = CacheEntry(
                user_id=user_id,
                data=data,
                created_at=datetime.utcnow(),
                is_long_term=is_long_term,
                endpoint=endpoint,
                params=params,
            )

    async def get_cache_version(self, user_id: str, endpoint: str) -> int:
        """Return the current version for an endpoint's cache entries."""
        async with self._lock:
            return self._cache_versions.setdefault((user_id, endpoint), 0)

    async def set_cache_if_current_version(
        self,
        user_id: str,
        endpoint: str,
        data: Any,
        params: str,
        version: int,
        is_long_term: bool = False,
    ) -> bool:
        """Store a response unless the endpoint was invalidated while fetching it."""
        async with self._lock:
            version_key = (user_id, endpoint)
            if self._cache_versions.get(version_key, 0) != version:
                return False

            cache_key = self._make_cache_key(user_id, endpoint, params)
            self._cache[cache_key] = CacheEntry(
                user_id=user_id,
                data=data,
                created_at=datetime.utcnow(),
                is_long_term=is_long_term,
                endpoint=endpoint,
                params=params,
            )
            return True

    async def invalidate_endpoint_cache(self, user_id: str, endpoint: str) -> None:
        """Invalidate all cached parameter variants for one endpoint."""
        async with self._lock:
            version_key = (user_id, endpoint)
            self._cache_versions[version_key] = (
                self._cache_versions.get(version_key, 0) + 1
            )
            expired = [
                key
                for key, entry in self._cache.items()
                if entry.user_id == user_id and entry.endpoint == endpoint
            ]
            for key in expired:
                self._cache.pop(key)

    async def invalidate_user_cache(self, user_id: str) -> None:
        async with self._lock:
            version_keys = [
                key for key in self._cache_versions if key[0] == user_id
            ]
            for version_key in version_keys:
                self._cache_versions[version_key] += 1
            expired = [
                key for key, entry in self._cache.items() if entry.user_id == user_id
            ]
            for key in expired:
                self._cache.pop(key)


sessions = AuthManager()
_dsb_scheduler_task = None
_message_notification_task = None


# --- Background Tasks ---


async def fetch_and_store_user_data(user_id: str, school_id: str, username: str) -> None:
    """Background task: fetch user profile and store in metrics DB."""
    user_id = canonicalize_user_id(user_id)
    school_id = normalize_school_id(school_id)
    username = normalize_username(username)
    try:
        session_data = await sessions._get_or_create_schulportal_client(user_id)
        client = session_data.client

        result = await run_in_threadpool(client.benutzer_get_data)

        if not result.get("success"):
            logger.warning(
                f"Failed to fetch user data for {username}@{school_id}: {result.get('error')}"
            )
            return

        user_data = result.get("data", {})

        is_new, was_updated = await user_metrics_db.upsert_user(
            school_id=school_id, login=username, user_data=user_data
        )

        if is_new:
            logger.info(f"New user recorded in metrics: {username}@{school_id}")
        elif was_updated:
            logger.info(f"User data updated in metrics: {username}@{school_id}")
        else:
            logger.debug(f"User data unchanged: {username}@{school_id}")

    except HTTPException:
        logger.warning(f"Session gone for {username}@{school_id}, skipping metrics")
    except Exception as e:
        logger.error(f"Error storing user metrics for {username}@{school_id}: {e}")


# --- FastAPI App ---

app = FastAPI(title="Schulportal Hessen API", version="0.2.0")


async def client_dependency(
    x_session_token: str = Header(..., alias="X-Session-Token"),
) -> AuthSession:
    """Validate access token (JWT) and return the AuthSession with a live Schulportal client."""
    payload = sessions.decode_access_token(x_session_token)
    user_id = canonicalize_user_id(payload["sub"])
    session_data = await sessions._get_or_create_schulportal_client(user_id)
    return AuthSession(
        client=session_data.client,
        user_id=user_id,
        school_id=session_data.school_id,
        username=session_data.username,
    )


def _should_cache(endpoint: str) -> bool:
    return not endpoint.startswith("/nachrichten")


def _make_param_key(params: Dict[str, Any]) -> str:
    if not params:
        return ""
    sorted_params = sorted(params.items())
    return json.dumps(sorted_params)


def _responses_equal(old_data: Any, new_data: Any) -> bool:
    try:
        return json.dumps(old_data, sort_keys=True) == json.dumps(
            new_data, sort_keys=True
        )
    except (TypeError, ValueError):
        return old_data == new_data


def _validate_custom_lesson(payload: CustomLessonRequest) -> Dict[str, Any]:
    """Validate user-entered lesson values before persisting them."""
    try:
        date.fromisoformat(payload.date)
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail="Lesson date must use the YYYY-MM-DD format",
        ) from error

    period = payload.period.strip()
    period_match = re.fullmatch(r"(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?", period)
    if not period_match:
        raise HTTPException(
            status_code=422,
            detail="Lesson period must be a number or a range such as 1-2",
        )
    period_start = int(period_match.group(1))
    period_end = int(period_match.group(2) or period_start)
    if period_end < period_start:
        raise HTTPException(status_code=422, detail="Lesson period range is reversed")
    period = (
        str(period_start)
        if period_end == period_start
        else f"{period_start}–{period_end}"
    )
    if not payload.removed and not payload.subject.strip():
        raise HTTPException(
            status_code=422,
            detail="A subject is required unless the lesson is hidden",
        )
    if payload.week_type not in (None, "", "A", "B"):
        raise HTTPException(status_code=422, detail="Week type must be A or B")
    for label, value in (("start", payload.start_time), ("end", payload.end_time)):
        if value and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise HTTPException(
                status_code=422,
                detail=f"{label.capitalize()} time must use HH:MM format",
            )

    values = (
        payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    )
    return {
        **values,
        "date": payload.date,
        "period": period,
        "duration": max(payload.duration, period_end - period_start + 1),
        "week_type": payload.week_type or None,
        "course_id": payload.course_id.strip() if payload.course_id else None,
    }


def _validate_class_link_url(url: str) -> str:
    """Allow portal-relative and HTTP(S) links, but reject executable schemes."""
    clean_url = (url or "").strip()
    if not clean_url:
        return ""
    parsed = urlparse(clean_url)
    if clean_url.startswith("//") or (
        parsed.scheme and parsed.scheme.lower() not in {"http", "https"}
    ):
        raise HTTPException(
            status_code=422,
            detail="Class links must be relative paths or HTTP(S) URLs",
        )
    if parsed.scheme in {"http", "https"} and not parsed.netloc:
        raise HTTPException(status_code=422, detail="Class link URL is incomplete")
    return clean_url


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:4173",
        "http://localhost:5173",
        "https://lanis.arg-server.de",
    ],
    allow_origin_regex=r"^https://.*\.surge\.sh$|^https://.*\.appwrite\.network$|^http://(?:localhost|127\.0\.0\.1):\d+$|^http://192\.168\.\d{1,3}\.\d{1,3}(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documentation_router)


@app.on_event("startup")
async def _startup() -> None:
    global _dsb_scheduler_task, _message_notification_task
    await auth_db_initialize()
    await user_metrics_db.initialize()
    await dsb_snapshot_db.initialize()
    await task_queue.start()
    _dsb_scheduler_task = await run_dsb_scheduler()
    _message_notification_task = await run_message_notification_scheduler(
        sessions._get_or_create_schulportal_client,
        sessions.invalidate_endpoint_cache,
        get_notification_preferences,
    )
    logger.info(
        "API started with task queue, databases, DSB snapshot scheduler, "
        "and message notification scheduler"
    )


@app.on_event("shutdown")
async def _cleanup_sessions() -> None:
    global _dsb_scheduler_task, _message_notification_task
    if _dsb_scheduler_task:
        _dsb_scheduler_task.cancel()
    if _message_notification_task:
        _message_notification_task.cancel()
    await task_queue.stop(wait=True, timeout=10.0)
    await sessions.shutdown()


# --- Public Endpoints ---


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics/stats")
async def get_metrics_stats() -> Dict[str, Any]:
    db_stats = await user_metrics_db.get_stats()
    queue_stats = task_queue.get_queue_stats()

    return {
        "success": True,
        "database": db_stats,
        "task_queue": queue_stats,
    }


# --- Auth Endpoints ---


@app.post("/login", response_model=LoginResponse)
async def login_endpoint(payload: LoginRequest) -> LoginResponse:
    school_id = normalize_school_id(payload.school_id)
    username = str(payload.username).strip()

    # 1. Log into Schulportal
    user_id = await sessions.create_schulportal_session(
        school_id, username, payload.password
    )

    # 2. Store long-term refresh token in DB
    refresh_token = await store_refresh_token(
        user_id=user_id,
        school_id=school_id,
        username=username,
        password=payload.password,
    )

    # 3. Issue short-term access token (JWT)
    access_token = sessions.create_access_token(
        user_id=user_id,
        school_id=school_id,
        username=username,
    )

    # 4. Read encryption state
    session_data = await sessions._get_or_create_schulportal_client(user_id)
    encryption_ready = bool(
        getattr(session_data.client, "cryptor", None)
        and session_data.client.cryptor.authenticated
    )

    # 5. Queue background metrics fetch
    user_data_task = Task(
        name=f"fetch_user_data:{username}@{school_id}",
        func=fetch_and_store_user_data,
        args=(user_id, school_id, normalize_username(username)),
        priority=TaskPriority.LOW,
        max_retries=2,
    )
    await task_queue.add_task(user_data_task)

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        school_id=school_id,
        username=username,
        encryption_ready=encryption_ready,
    )


@app.post("/auth/refresh", response_model=TokenRefreshResponse)
async def refresh_endpoint(payload: TokenRefreshRequest) -> TokenRefreshResponse:
    rt_data = await get_refresh_token(payload.refresh_token)
    if not rt_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    # Re-establish Schulportal session (will use in-memory cache if still valid)
    await sessions._get_or_create_schulportal_client(rt_data["user_id"])

    # Issue new access token
    access_token = sessions.create_access_token(
        user_id=rt_data["user_id"],
        school_id=rt_data["school_id"],
        username=rt_data["username"],
    )

    return TokenRefreshResponse(access_token=access_token)


@app.post("/logout")
async def logout_endpoint(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, str]:
    await delete_user_tokens(auth.user_id)
    await delete_user_push_subscriptions(auth.user_id)
    await sessions.drop_schulportal_session(auth.user_id)
    return {"status": "logged_out"}


# --- DSB Endpoints ---


@app.post("/dsb/login")
async def dsb_login_endpoint(
    payload: DsbLoginRequest,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    return await run_in_threadpool(
        auth.client.dsb_login, payload.username, payload.password
    )


@app.post("/dsb/plan-urls")
async def dsb_plan_urls_endpoint(
    payload: DsbLoginRequest,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    return await run_in_threadpool(
        auth.client.dsb_get_plan_urls, payload.username, payload.password
    )


@app.post("/dsb/plan")
async def dsb_plan_endpoint(
    payload: DsbPlanRequest,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    return await run_in_threadpool(
        auth.client.dsb_get_substitution_plan,
        payload.username,
        payload.password,
        payload.plan_index,
        payload.plan_url,
        payload.include_raw,
    )


# --- Apps / Modules ---


async def _revalidate_endpoint(
    user_id: str, endpoint: str, fetch_func
) -> None:
    try:
        fresh_data = await run_in_threadpool(fetch_func)
        cached_data = await sessions.get_cached(user_id, endpoint)
        if cached_data is not None and not _responses_equal(cached_data, fresh_data):
            await sessions.set_cache(user_id, endpoint, fresh_data, is_long_term=True)
    except Exception:
        pass


async def _revalidate_modules(user_id: str, fetch_func) -> None:
    try:
        fresh_modules = await run_in_threadpool(fetch_func)
        fresh_data = {"success": True, "modules": fresh_modules}
        cached_data = await sessions.get_cached(user_id, "/modules")
        if cached_data is not None and not _responses_equal(cached_data, fresh_data):
            await sessions.set_cache(
                user_id, "/modules", fresh_data, is_long_term=True
            )
    except Exception:
        pass


@app.get("/apps")
async def get_apps(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    cached_data, needs_revalidation = await sessions.get_cached_with_revalidate(
        auth.user_id, "/apps"
    )

    if needs_revalidation:
        asyncio.create_task(
            _revalidate_endpoint(auth.user_id, "/apps", auth.client.get_apps)
        )

    if cached_data is not None:
        return cached_data

    result = await run_in_threadpool(auth.client.get_apps)
    await sessions.set_cache(auth.user_id, "/apps", result, is_long_term=True)
    return result


@app.get("/modules")
async def get_modules(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    cached_data, needs_revalidation = await sessions.get_cached_with_revalidate(
        auth.user_id, "/modules"
    )

    if needs_revalidation:
        asyncio.create_task(
            _revalidate_modules(auth.user_id, auth.client.get_available_modules)
        )

    if cached_data is not None:
        return cached_data

    modules = await run_in_threadpool(auth.client.get_available_modules)
    result = {"success": True, "modules": modules}
    await sessions.set_cache(auth.user_id, "/modules", result, is_long_term=True)
    return result


@app.get("/benutzer")
async def get_user_data(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    cached_data, needs_revalidation = await sessions.get_cached_with_revalidate(
        auth.user_id, "/benutzer"
    )

    if needs_revalidation:
        asyncio.create_task(
            _revalidate_endpoint(
                auth.user_id, "/benutzer", auth.client.benutzer_get_data
            )
        )

    if cached_data is not None:
        return cached_data

    result = await run_in_threadpool(auth.client.benutzer_get_data)
    await sessions.set_cache(auth.user_id, "/benutzer", result, is_long_term=True)
    return result


# --- Calendar ---


@app.get("/kalender")
async def get_calendar_overview(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    cached = await sessions.get_cached(auth.user_id, "/kalender")
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.kalender_get_overview)
    await sessions.set_cache(auth.user_id, "/kalender", result)
    return result


@app.get("/kalender/events")
async def get_calendar_events(
    year: int = 0,
    start: str = "year",
    category: str = "",
    search: str = "",
    target: str = "",
    view_id: Optional[str] = None,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key(
        {
            "year": year,
            "start": start,
            "category": category,
            "search": search,
            "target": target,
            "view_id": view_id or "",
        }
    )
    cached = await sessions.get_cached(auth.user_id, "/kalender/events", params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(
        auth.client.kalender_get_events,
        year, start, category, search, target, view_id,
    )
    await sessions.set_cache(auth.user_id, "/kalender/events", result, params)
    return result


@app.get("/kalender/event/{event_id}")
async def get_calendar_event(
    event_id: str,
    view_id: Optional[str] = None,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"event_id": event_id, "view_id": view_id or ""})
    cached = await sessions.get_cached(auth.user_id, "/kalender/event", params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(
        auth.client.kalender_get_event, event_id, view_id
    )
    await sessions.set_cache(auth.user_id, "/kalender/event", result, params)
    return result


# --- Vertretungsplan / Stundenplan ---


@app.get("/vertretungsplan")
async def get_vertretungsplan(
    include_raw: bool = False,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"include_raw": include_raw})
    cached = await sessions.get_cached(auth.user_id, "/vertretungsplan", params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.vertretungsplan_get_plan, include_raw)
    await sessions.set_cache(auth.user_id, "/vertretungsplan", result, params)
    return result


@app.get("/stundenplan")
async def get_stundenplan(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    week_start = current_timetable_monday()
    timetable_params = _make_param_key({"week_start": week_start.isoformat()})
    cached = await sessions.get_cached(auth.user_id, "/stundenplan", timetable_params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.stundenplan_get_plan)
    if result.get("success"):
        course_overview = await sessions.get_cached(auth.user_id, "/meinunterricht")
        if course_overview is None:
            course_overview = await run_in_threadpool(
                auth.client.meinunterricht_get_overview
            )
            if course_overview.get("success"):
                await sessions.set_cache(
                    auth.user_id, "/meinunterricht", course_overview
                )
        result = enrich_timetable(result, course_overview)
        # Keep an unmodified recurring template for clients that project the
        # timetable onto dates outside the current Monday-Friday window.
        # Date-specific overrides are still applied to the legacy plan fields
        # below for backwards compatibility.
        result["week_start"] = week_start.isoformat()
        result["template_plan_for_all"] = copy.deepcopy(
            result.get("plan_for_all")
        )
        result["template_plan_for_own"] = copy.deepcopy(
            result.get("plan_for_own")
        )
    result = apply_custom_lessons(result, await get_custom_lessons(auth.user_id))
    await sessions.set_cache(auth.user_id, "/stundenplan", result, timetable_params)
    return result


@app.get("/settings/timetable/lessons")
async def get_custom_timetable_lessons(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    return {
        "success": True,
        "lessons": await get_custom_lessons(auth.user_id),
    }


@app.put("/settings/timetable/lessons")
async def update_custom_timetable_lesson(
    payload: CustomLessonRequest,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    lesson = _validate_custom_lesson(payload)
    saved = await save_custom_lesson(auth.user_id, lesson)
    await sessions.invalidate_endpoint_cache(auth.user_id, "/stundenplan")
    return {"success": True, "lesson": saved}


@app.delete("/settings/timetable/lessons")
async def reset_custom_timetable_lesson(
    lesson_date: str = Query(
        ..., alias="date", description="Lesson date in YYYY-MM-DD format"
    ),
    period: str = Query(..., min_length=1, max_length=30),
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    try:
        date.fromisoformat(lesson_date)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid lesson date") from error
    period_match = re.fullmatch(
        r"(\d{1,2})(?:\s*[-–—]\s*(\d{1,2}))?", period.strip()
    )
    if not period_match:
        raise HTTPException(status_code=422, detail="Invalid lesson period")
    period_start = int(period_match.group(1))
    period_end = int(period_match.group(2) or period_start)
    if period_end < period_start:
        raise HTTPException(status_code=422, detail="Invalid lesson period")
    normalised_period = (
        str(period_start)
        if period_end == period_start
        else f"{period_start}–{period_end}"
    )
    await delete_custom_lesson(auth.user_id, lesson_date, normalised_period)
    await sessions.invalidate_endpoint_cache(auth.user_id, "/stundenplan")
    return {"success": True}


# --- Dateispeicher ---


@app.get("/dateispeicher")
async def get_dateispeicher(
    folder_id: int = 0,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"folder_id": folder_id})
    cached = await sessions.get_cached(auth.user_id, "/dateispeicher", params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.dateispeicher_get_node, folder_id)
    await sessions.set_cache(auth.user_id, "/dateispeicher", result, params)
    return result


@app.get("/dateispeicher/search")
async def search_dateispeicher(
    q: str,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"q": q})
    cached = await sessions.get_cached(auth.user_id, "/dateispeicher/search", params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.dateispeicher_search_files, q)
    await sessions.set_cache(auth.user_id, "/dateispeicher/search", result, params)
    return result


# --- Lerngruppen ---


@app.get("/lerngruppen")
async def get_lerngruppen(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    cached = await sessions.get_cached(auth.user_id, "/lerngruppen")
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.lerngruppen_get_overview)
    await sessions.set_cache(auth.user_id, "/lerngruppen", result)
    return result


# --- Messages ---


@app.get("/notifications/config")
async def get_notification_config(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    """Return the public Web Push configuration for the signed-in user."""
    del auth
    configured = push_configured()
    return {
        "success": True,
        "configured": configured,
        "public_key": os.getenv("VAPID_PUBLIC_KEY", "") if configured else "",
    }


@app.get("/notifications/preferences")
async def get_user_notification_preferences(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    preferences = await get_notification_preferences(auth.user_id)
    preferences.pop("user_id", None)
    return {"success": True, "preferences": preferences}


@app.put("/notifications/preferences")
async def update_user_notification_preferences(
    payload: NotificationPreferencesRequest,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    preferences = (
        payload.model_dump()
        if hasattr(payload, "model_dump")
        else payload.dict()
    )
    try:
        validate_notification_preferences(preferences)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    saved = await save_notification_preferences(auth.user_id, preferences)
    saved.pop("user_id", None)
    return {"success": True, "preferences": saved}


@app.post("/notifications/subscription")
async def register_notification_subscription(
    payload: PushSubscriptionRequest,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    if not push_configured():
        raise HTTPException(
            status_code=503,
            detail="Push notifications are not configured on this server",
        )
    if not is_trusted_push_endpoint(payload.endpoint):
        raise HTTPException(
            status_code=422,
            detail="Push endpoint must belong to a trusted Web Push service",
        )

    subscription = (
        payload.model_dump()
        if hasattr(payload, "model_dump")
        else payload.dict()
    )
    if not is_valid_push_subscription(subscription):
        raise HTTPException(
            status_code=422,
            detail="Push subscription keys are invalid",
        )
    await save_push_subscription(auth.user_id, subscription)
    return {"success": True}


@app.post("/notifications/unsubscribe")
async def unregister_notification_subscription(
    payload: PushUnsubscribeRequest,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    await delete_push_subscription(auth.user_id, payload.endpoint)
    return {"success": True}


@app.post("/notifications/test")
async def send_notification_test(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    if not await send_test_push_notification(auth.user_id):
        raise HTTPException(
            status_code=503,
            detail="No active push subscription or push delivery failed",
        )
    return {"success": True}


MESSAGE_CACHE_ENDPOINTS = (
    "/nachrichten/headers",
    "/nachrichten/conversation",
)


async def _invalidate_message_caches(user_id: str) -> None:
    await asyncio.gather(
        *(
            sessions.invalidate_endpoint_cache(user_id, endpoint)
            for endpoint in MESSAGE_CACHE_ENDPOINTS
        )
    )


async def _update_message_cache_task(
    user_id: str,
    endpoint: str,
    fetch_func,
    params: dict,
    cache_params: str,
    cache_version: int,
):
    try:
        fresh_data = await run_in_threadpool(fetch_func, **params)
        cached_data = await sessions.get_cached(user_id, endpoint, cache_params)
        if cached_data is not None and not _responses_equal(cached_data, fresh_data):
            await sessions.set_cache_if_current_version(
                user_id,
                endpoint,
                fresh_data,
                cache_params,
                cache_version,
            )
    except Exception as e:
        logger.error(f"Error updating message cache for {endpoint}: {e}")


@app.get("/nachrichten/headers")
async def get_message_headers(
    get_type: str = "All",
    last: int = 0,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    endpoint = "/nachrichten/headers"
    params = {"get_type": get_type, "last": last}
    cache_params = _make_param_key(params)
    cache_version = await sessions.get_cache_version(auth.user_id, endpoint)
    cached = await sessions.get_cached(auth.user_id, endpoint, cache_params)
    task = Task(
        name=f"update_message_cache:{endpoint}",
        func=_update_message_cache_task,
        args=(
            auth.user_id,
            endpoint,
            auth.client.nachrichten_get_headers,
            params,
            cache_params,
            cache_version,
        ),
        priority=TaskPriority.LOW,
        max_retries=2,
    )
    await task_queue.add_task(task)
    if cached is not None:
        return cached
    result = await run_in_threadpool(
        auth.client.nachrichten_get_headers, get_type, last
    )
    await sessions.set_cache_if_current_version(
        auth.user_id, endpoint, result, cache_params, cache_version
    )
    return result


@app.get("/nachrichten/search")
async def search_recipients(
    q: str,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    endpoint = "/nachrichten/search"
    params = {"q": q}
    fetch_params = {"query": q}
    cache_params = _make_param_key(params)
    cache_version = await sessions.get_cache_version(auth.user_id, endpoint)
    cached = await sessions.get_cached(auth.user_id, endpoint, cache_params)
    task = Task(
        name=f"update_message_cache:{endpoint}",
        func=_update_message_cache_task,
        args=(
            auth.user_id,
            endpoint,
            auth.client.nachrichten_search_recipients,
            fetch_params,
            cache_params,
            cache_version,
        ),
        priority=TaskPriority.LOW,
        max_retries=2,
    )
    await task_queue.add_task(task)
    if cached is not None:
        return cached
    result = await run_in_threadpool(auth.client.nachrichten_search_recipients, q)
    await sessions.set_cache_if_current_version(
        auth.user_id, endpoint, result, cache_params, cache_version
    )
    return result


@app.get("/nachrichten/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    last: int = 0,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    endpoint = "/nachrichten/conversation"
    params = {"conversation_id": conversation_id, "last": last}
    cache_params = _make_param_key(params)
    cache_version = await sessions.get_cache_version(auth.user_id, endpoint)
    cached = await sessions.get_cached(auth.user_id, endpoint, cache_params)
    task = Task(
        name=f"update_message_cache:{endpoint}",
        func=_update_message_cache_task,
        args=(
            auth.user_id,
            endpoint,
            auth.client.nachrichten_get_conversation,
            params,
            cache_params,
            cache_version,
        ),
        priority=TaskPriority.LOW,
        max_retries=2,
    )
    await task_queue.add_task(task)
    if cached is not None:
        return cached
    result = await run_in_threadpool(
        auth.client.nachrichten_get_conversation, conversation_id, last
    )
    await sessions.set_cache_if_current_version(
        auth.user_id, endpoint, result, cache_params, cache_version
    )
    return result


@app.post("/nachrichten/send")
async def send_message(
    recipients: List[str] = Body(..., description="List of recipient IDs"),
    subject: str = Body(..., description="Message subject"),
    body: str = Body(..., description="Message body"),
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    message_data = {
        "recipients": recipients,
        "subject": subject,
        "body": body,
    }
    result = await run_in_threadpool(auth.client.nachrichten_send_message, message_data)
    if result.get("success"):
        await _invalidate_message_caches(auth.user_id)
    return result


@app.post("/nachrichten/reply")
async def reply_message(
    conversation_id: str = Body(..., description="Conversation uniqid to reply to"),
    body: str = Body(..., description="Reply message body"),
    to: str = Body("all", description="Recipient selector: 'all' or a user id"),
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    result = await run_in_threadpool(
        auth.client.nachrichten_reply_message, conversation_id, body, to
    )
    if result.get("success"):
        await _invalidate_message_caches(auth.user_id)
    return result


@app.post("/nachrichten/mark-read")
async def mark_read(
    conversation_id: str = Body(..., description="Conversation uniqid to mark read"),
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    return await run_in_threadpool(
        auth.client.nachrichten_mark_read, conversation_id
    )


# --- Mein Unterricht ---


async def _download_course_file(
    user_id: str, download_url: str, file_hash: str
) -> None:
    if is_file_cached(file_hash):
        unmark_pending(file_hash)
        return

    write_pending_meta(file_hash, download_url)
    session_data = await sessions._get_or_create_schulportal_client(user_id)
    client = session_data.client
    result = await run_in_threadpool(client.meinunterricht_download_file, download_url)

    if result.get("success"):
        save_file(
            file_hash,
            result["content"],
            result.get("content_type", "application/octet-stream"),
            result.get("filename", "download"),
        )
    else:
        unmark_pending(file_hash)
        logger.warning(
            "File download failed for %s: %s", file_hash[:12], result.get("error")
        )


@app.get("/meinunterricht")
async def meinunterricht_overview(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    class_link_overrides = await get_class_link_overrides(auth.user_id)
    cached = await sessions.get_cached(auth.user_id, "/meinunterricht")
    if cached is not None:
        return merge_class_link_overrides(cached, class_link_overrides)

    result = await run_in_threadpool(auth.client.meinunterricht_get_overview)
    if result.get("success"):
        result = merge_class_link_overrides(result, class_link_overrides)
        await sessions.set_cache(auth.user_id, "/meinunterricht", result)
    return result


@app.get("/settings/class-links")
async def get_class_link_settings(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    """Return portal classes together with their account-specific links."""
    overview = await meinunterricht_overview(auth)
    overrides = await get_class_link_overrides(auth.user_id)
    links: List[Dict[str, object]] = []
    seen: set[str] = set()

    for entry in overview.get("entries", []):
        if not isinstance(entry, dict):
            continue
        course_id = str(entry.get("book_id") or "").strip()
        if not course_id or course_id in seen:
            continue
        seen.add(course_id)
        links.append(
            {
                "course_id": course_id,
                "name": str(entry.get("name") or "").strip(),
                "teacher": str(entry.get("teacher_full_name") or "").strip(),
                "url": str(entry.get("course_link") or ""),
                "overridden": course_id in overrides,
            }
        )

    for course_id, url in overrides.items():
        if course_id in seen:
            continue
        links.append(
            {
                "course_id": course_id,
                "name": "Unbekannter Kurs",
                "teacher": "",
                "url": url,
                "overridden": True,
            }
        )

    return {
        "success": bool(overview.get("success")) or bool(links),
        "links": links,
    }


@app.put("/settings/class-links")
async def update_class_link(
    payload: ClassLinkRequest,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    course_id = payload.course_id.strip()
    if not course_id:
        raise HTTPException(status_code=422, detail="Course ID cannot be blank")
    url = _validate_class_link_url(payload.url)
    await save_class_link(auth.user_id, course_id, url)
    await asyncio.gather(
        sessions.invalidate_endpoint_cache(auth.user_id, "/meinunterricht"),
        sessions.invalidate_endpoint_cache(auth.user_id, "/stundenplan"),
    )
    return {
        "success": True,
        "link": {
            "course_id": course_id,
            "url": url,
            "overridden": True,
        },
    }


@app.delete("/settings/class-links")
async def reset_class_link(
    course_id: str = Query(..., min_length=1, max_length=200),
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    course_id = course_id.strip()
    if not course_id:
        raise HTTPException(status_code=422, detail="Course ID cannot be blank")
    await delete_class_link(auth.user_id, course_id)
    await asyncio.gather(
        sessions.invalidate_endpoint_cache(auth.user_id, "/meinunterricht"),
        sessions.invalidate_endpoint_cache(auth.user_id, "/stundenplan"),
    )
    return {"success": True}


@app.get("/meinunterricht/course/{course_id}")
async def meinunterricht_course(
    course_id: str,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"course_id": course_id})

    cached = await sessions.get_cached(
        auth.user_id, "/meinunterricht/course", params
    )
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.meinunterricht_get_course, course_id)

    if result.get("success") and "entries" in result:
        for entry in result["entries"]:
            for file_info in entry.get("files", []):
                original_url = file_info.get("download_url", "")
                if not original_url:
                    continue

                file_hash = get_file_hash(original_url)
                local_url = f"{PUBLIC_BASE_URL}/meinunterricht/file/{file_hash}"
                file_info["download_url"] = local_url
                file_info["url"] = local_url
                file_info["file_hash"] = file_hash

                if not is_file_cached(file_hash) and not is_file_pending(file_hash):
                    mark_pending(file_hash)
                    download_task = Task(
                        name=f"download_file:{file_hash[:12]}",
                        func=_download_course_file,
                        args=(auth.user_id, original_url, file_hash),
                        priority=TaskPriority.LOW,
                        max_retries=2,
                    )
                    await task_queue.add_task(download_task)

    await sessions.set_cache(
        auth.user_id, "/meinunterricht/course", result, params
    )
    return result


@app.get("/meinunterricht/file/{file_hash}")
async def meinunterricht_file(
    file_hash: str,
    x_session_token: str = Header(None, alias="X-Session-Token"),
):
    from fastapi.responses import FileResponse

    meta = get_meta(file_hash)
    content_path = get_content_path(file_hash)

    if content_path.exists() and meta and meta.get("content_type"):
        return FileResponse(
            content_path,
            media_type=meta.get("content_type", "application/octet-stream"),
            filename=meta.get("filename", "download"),
        )

    if not x_session_token:
        raise HTTPException(status_code=404, detail="File not found")

    try:
        payload = sessions.decode_access_token(x_session_token)
        user_id = payload["sub"]
        session_data = await sessions._get_or_create_schulportal_client(user_id)
        client = session_data.client
    except HTTPException:
        raise HTTPException(status_code=404, detail="File not found")

    if meta and meta.get("download_url"):
        result = await run_in_threadpool(
            client.meinunterricht_download_file, meta["download_url"]
        )
        if result.get("success"):
            save_file(
                file_hash,
                result["content"],
                result.get("content_type", "application/octet-stream"),
                result.get("filename", "download"),
            )
            return FileResponse(
                content_path,
                media_type=result.get("content_type", "application/octet-stream"),
                filename=result.get("filename", "download"),
            )

    raise HTTPException(
        status_code=404,
        detail="File not yet available, please try again shortly",
    )


@app.get("/meinunterricht/entry")
async def meinunterricht_entry(
    url: str,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"url": url})
    cached = await sessions.get_cached(auth.user_id, "/meinunterricht/entry", params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.meinunterricht_get_entry_details, url)
    await sessions.set_cache(auth.user_id, "/meinunterricht/entry", result, params)
    return result


@app.get("/meinunterricht/weekly")
async def meinunterricht_weekly(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    cached = await sessions.get_cached(auth.user_id, "/meinunterricht/weekly")
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.meinunterricht_get_weekly_view)
    await sessions.set_cache(auth.user_id, "/meinunterricht/weekly", result)
    return result


@app.get("/meinunterricht/submissions")
async def meinunterricht_submissions(
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    cached = await sessions.get_cached(auth.user_id, "/meinunterricht/submissions")
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.meinunterricht_get_submissions)
    await sessions.set_cache(auth.user_id, "/meinunterricht/submissions", result)
    return result


@app.post("/meinunterricht/homework-done")
async def meinunterricht_homework_done(
    auth: AuthSession = Depends(client_dependency),
    course_id: str = Form(...),
    entry_id: str = Form(...),
    done: bool = Form(True),
) -> Dict[str, object]:
    result = await run_in_threadpool(
        auth.client.meinunterricht_set_homework_done, course_id, entry_id, done
    )
    await sessions.invalidate_user_cache(auth.user_id)
    return result


# --- School List ---

SCHOOL_LIST_CACHE_KEY = "school_list_all"
SCHOOL_LIST_CACHE_TTL = 2 * 24 * 60 * 60  # 2 days
SCHOOL_LIST_CACHE_AUTO_REFRESH = 3 * 24 * 60 * 60  # 3 days
school_list_cache = {
    "data": None,
    "created_at": None,
}


async def _refresh_school_list_cache():
    client = SchulportalHessenAPI()
    data = await run_in_threadpool(client.school_list_get_all)
    school_list_cache["data"] = data
    school_list_cache["created_at"] = datetime.utcnow()
    return data


@app.get("/school-list")
async def school_list_all_cached() -> Dict[str, object]:
    now = datetime.utcnow()
    created_at = school_list_cache["created_at"]
    data = school_list_cache["data"]
    if (
        not data
        or not created_at
        or (now - created_at).total_seconds() > SCHOOL_LIST_CACHE_AUTO_REFRESH
    ):
        return await _refresh_school_list_cache()
    if (now - created_at).total_seconds() > SCHOOL_LIST_CACHE_TTL:
        asyncio.create_task(_refresh_school_list_cache())
    return data


@app.get("/school-list/district/{district_id}")
async def school_list_by_district(district_id: str) -> Dict[str, object]:
    client = SchulportalHessenAPI()
    result = await run_in_threadpool(client.school_list_get_by_district, district_id)
    return result


@app.get("/school-list/search")
async def school_list_search(q: str) -> Dict[str, object]:
    client = SchulportalHessenAPI()
    result = await run_in_threadpool(client.school_list_search_by_name, q)
    return result



# --- App Proxy (for OAuth/SSO apps like bettermarks) ---

_app_auth_cache: Dict[str, Dict[str, Any]] = {}
"""Cache: (user_id, app_name) -> {cookies: dict, base_url: str, target_host: str}"""


def _follow_oauth_flow(client: SchulportalHessenAPI, portal_url: str) -> Optional[Dict[str, Any]]:
    """Follow the full OAuth redirect chain for an app and extract auth cookies.

    Uses allow_redirects=True to follow the complete chain including all
    OAuth/OIDC handshakes. Returns the final cookies from the target app
    domain (non-Schulportal, non-vidis).

    Returns a dict with:
        cookies: dict of cookie_name -> cookie_value for the target domain
        target_host: the hostname of the final app (e.g. apps.bettermarks.com)
        base_url: the final URL (e.g. https://apps.bettermarks.com/one/)
    """
    try:
        resp = client.session.get(
            portal_url, allow_redirects=True, timeout=30, stream=True
        )
        final_url = resp.url
        resp.close()

        target_host = urlparse(final_url).hostname or ""

        cookies: Dict[str, str] = {}
        for cookie in client.session.cookies:
            domain = (cookie.domain or "").lstrip(".")
            if domain and "schulportal" not in domain and "vidis" not in domain:
                cookies[cookie.name] = cookie.value

        if not cookies:
            return None

        return {
            "cookies": cookies,
            "target_host": target_host,
            "base_url": final_url,
        }
    except Exception:
        return None


def _rewrite_html(content: bytes, target_host: str, proxy_prefix: str) -> bytes:
    """Rewrite URLs in HTML to route through the proxy.

    Handles multiple subdomains by encoding the subdomain in the proxy path:
    https://sub.example.com/path -> {proxy_prefix}/sub:path
    """
    text = content.decode("utf-8", errors="replace")
    base_domain = target_host.split(".", 1)[-1] if "." in target_host else target_host

    # Rewrite root-relative URLs to include proxy prefix
    def _prefix_root_relative(m: re.Match) -> str:
        attr = m.group(1)
        quote = m.group(2) or ""
        url = m.group(3)
        if url.startswith("/") and not url.startswith("//"):
            return f'{attr}={quote}{proxy_prefix}{url}{quote}'
        return m.group(0)

    text = re.sub(
        r'(src|href|action)=("|\'?)(/[^"\' >]+)("|\'?)',
        _prefix_root_relative,
        text,
    )

    # Rewrite ANY *.base_domain URL to use proxy with subdomain prefix
    def _rewrite_external(m: re.Match) -> str:
        subdomain = m.group(1) or ""
        path = m.group(2) or ""
        if subdomain:
            return f"{proxy_prefix}/{subdomain}:{path}"
        return f"{proxy_prefix}{path}"

    escaped_domain = re.escape(base_domain)
    # Match https?://(subdomain.)base_domain/path and //(subdomain.)base_domain/path
    text = re.sub(
        rf'https?://(?:([a-zA-Z0-9.-]+)\.)?{escaped_domain}(/[\w.,@?^=%&:/~+#!-]*)?',
        _rewrite_external,
        text,
    )
    text = re.sub(
        rf'//(?:([a-zA-Z0-9.-]+)\.)?{escaped_domain}(/[\w.,@?^=%&:/~+#!-]*)?',
        _rewrite_external,
        text,
    )

    return text.encode("utf-8")


def _rewrite_location(location: str, target_host: str, proxy_prefix: str) -> str:
    """Rewrite a Location header to route through the proxy."""
    if location.startswith("/"):
        return f"{proxy_prefix}{location}"

    parsed = urlparse(location)
    hostname = parsed.hostname or ""
    base_domain = target_host.split(".", 1)[-1] if "." in target_host else target_host

    if hostname.endswith("." + base_domain) or hostname == base_domain:
        subdomain = hostname.split(".")[0] if hostname != base_domain else ""
        path_and_query = parsed.path
        if parsed.query:
            path_and_query += "?" + parsed.query
        if subdomain and subdomain != target_host.split(".")[0]:
            return f"{proxy_prefix}/{subdomain}:{path_and_query}"
        return f"{proxy_prefix}{path_and_query}"

    return location


@app.api_route(
    "/app/{app_name}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
@app.api_route(
    "/app/{app_name}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def app_launch(
    app_name: str,
    request: Request,
    path: str = "",
    token: str = "",
    x_session_token: Optional[str] = Header(None, alias="X-Session-Token"),
):
    """Launch OAuth/SSO apps by resolving the OAuth flow to a callback URL.

    Returns an HTML page that sets the initial auth cookie via a redirect
    through the app's domain, then navigates to the OAuth callback URL to
    complete authentication in the user's browser.
    """
    session_token = x_session_token or token
    if not session_token:
        raise HTTPException(status_code=401, detail="X-Session-Token header or token query parameter required")

    try:
        payload = sessions.decode_access_token(session_token)
        user_id = payload["sub"]
        session_data = await sessions._get_or_create_schulportal_client(user_id)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    client = session_data.client

    # Look up the app
    try:
        modules = await run_in_threadpool(client.get_available_modules)
    except Exception:
        modules = []

    module = next((m for m in modules if m.get("name", "").lower() == app_name.lower()), None)
    if not module:
        module = next((m for m in modules if app_name.lower() in m.get("name", "").lower()), None)
    if not module:
        raise HTTPException(status_code=404, detail=f"App '{app_name}' not found")

    portal_url = module.get("url", "")
    if not portal_url:
        raise HTTPException(status_code=404, detail="No portal URL")

    # Follow flow to get OAuth URLs
    launch_data = await run_in_threadpool(_build_launch_urls, client, portal_url)
    if not launch_data:
        raise HTTPException(status_code=502, detail="Failed to build launch URLs")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{app_name}</title></head>
<body style="margin:0;display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;background:#f5f5f5">
<div style="text-align:center"><p>Redirecting to {app_name}&hellip;</p></div>
<script>
(function(){{
    var cookieUrl = {json.dumps(launch_data["cookie_url"])};
    var callbackUrl = {json.dumps(launch_data["callback_url"])};

    // Step 1: set initial auth cookie by loading the cookie-set URL
    var img = new Image();
    img.onload = img.onerror = function() {{
        // Step 2: cookie is set, now visit the callback to complete auth
        setTimeout(function(){{ window.location.replace(callbackUrl); }}, 200);
    }};
    img.src = cookieUrl;
}})();
</script>
</body></html>"""

    return Response(content=html, media_type="text/html")


def _build_launch_urls(client: SchulportalHessenAPI, portal_url: str) -> Optional[Dict[str, str]]:
    """Follow OAuth flow to extract cookie-set and callback URLs.

    The cookie-set URL (first page that redirects to the target app's
    domain) sets the initial auth cookie via its redirect response.
    The callback URL then exchanges the OAuth code for the final session.

    Returns dict with cookie_url and callback_url, or None on failure.
    """
    oauth_indicators = ("vidis", "oauth", "openid", "oidc")
    portal_hosts = ("schulportal.hessen.de", "login.schulportal.hessen.de")
    current_url = portal_url
    cookie_url = None
    callback_url = None
    seen_oauth = False

    for _ in range(12):
        try:
            resp = client.session.get(current_url, allow_redirects=False, timeout=10, stream=True)
            resp.close()
        except Exception:
            break

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location", "")
            if not location:
                break
            next_url = urljoin(current_url, location)
            next_host = urlparse(next_url).hostname or ""

            if not seen_oauth:
                if any(ind in next_url.lower() for ind in oauth_indicators):
                    seen_oauth = True

            # Capture the first redirect that lands on the target app's domain
            # (this response sets the initial auth cookie)
            if cookie_url is None and next_host:
                if not any(next_host.endswith(h) for h in portal_hosts) and "vidis" not in next_host:
                    cookie_url = next_url

            if seen_oauth and "code=" in next_url:
                if "vidis" not in next_host and not any(next_host.endswith(h) for h in portal_hosts):
                    callback_url = next_url
                    break

            current_url = next_url
        else:
            break

    if not cookie_url:
        cookie_url = portal_url

    if not callback_url:
        callback_url = current_url

    return {"cookie_url": cookie_url, "callback_url": callback_url}


__all__ = ["app", "app_proxy"]

# --- Semantic Search ---


from .semantic_search import semantic_engine


@app.get("/search/semantic")
async def semantic_search(
    q: str,
    top_k: int = Query(default=20, ge=1, le=100),
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    results = await semantic_engine.search(
        user_id=auth.user_id,
        query=q,
        auth_client=auth.client,
        top_k=top_k,
    )
    return {
        "success": True,
        "query": q,
        "results": results,
        "count": len(results),
    }
