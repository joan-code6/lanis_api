"""
FastAPI wrapper around `functions` to expose Schulportal Hessen endpoints.

Features:
- Per-API-user session tokens so multiple users with different credentials can work concurrently.
- Thin wrappers around the existing `SchulportalHessenAPI` methods.
- Uses a small in-memory session manager with an inactivity TTL to prevent leaked sessions.

Run locally:
        uvicorn api.api:app --reload
"""

import asyncio
import hashlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, Form, Header, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from schulportal_hessen.base import SchulportalHessenAPI

from .queue import task_queue, Task, TaskPriority
from .metrics import user_metrics_db
from .documentation import router as documentation_router
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

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("api")


SESSION_TTL_SECONDS = 1 * 60 * 60  # expire inactive sessions after 1 hour
CACHE_TTL_SECONDS = 10 * 60  # cache responses for 10 minutes
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
LONG_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60  # cache for 30 days (1 month)
LONG_CACHE_ENDPOINTS = {
    "/modules",
    "/apps",
    "/benutzer",
}  # endpoints with long-term cache


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
    token: str
    school_id: str
    username: str
    encryption_ready: bool


@dataclass
class SessionData:
    client: SchulportalHessenAPI
    created_at: datetime
    last_used: datetime
    username: str
    school_id: str


@dataclass
class CacheEntry:
    """Represents a cached response with timestamp."""

    data: Any
    created_at: datetime
    is_long_term: bool = False  # whether this uses long-term caching

    def is_expired(self, ttl_seconds: int) -> bool:
        """Check if cache entry has expired."""
        return datetime.utcnow() - self.created_at > timedelta(seconds=ttl_seconds)

    def is_stale(self, ttl_seconds: int) -> bool:
        """Check if cache entry is stale (for revalidation)."""
        return datetime.utcnow() - self.created_at > timedelta(seconds=ttl_seconds // 2)


class SessionManager:
    """Thread-safe in-memory token store for multiple API users."""

    def __init__(self, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self._sessions: Dict[str, SessionData] = {}
        self._cache: Dict[str, CacheEntry] = {}  # per-user per-endpoint cache
        self._lock = asyncio.Lock()
        self._ttl = ttl_seconds

    async def _purge_expired(self) -> None:
        now = datetime.utcnow()
        async with self._lock:
            expired = [
                token
                for token, data in self._sessions.items()
                if now - data.last_used > timedelta(seconds=self._ttl)
            ]
            for token in expired:
                data = self._sessions.pop(token)
                data.client.close()

    async def _purge_expired_cache(self) -> None:
        """Remove expired cache entries."""
        async with self._lock:
            expired = [
                key
                for key, entry in self._cache.items()
                if entry.is_expired(CACHE_TTL_SECONDS)
            ]
            for key in expired:
                self._cache.pop(key)

    def _make_cache_key(self, token: str, endpoint: str, params: str = "") -> str:
        """Generate a cache key from token, endpoint, and parameters."""
        key_str = f"{token}:{endpoint}:{params}"
        return hashlib.sha256(key_str.encode()).hexdigest()

    async def get_cached(
        self, token: str, endpoint: str, params: str = ""
    ) -> Optional[Any]:
        """
        Retrieve a cached response if available and not expired.

        Args:
                token: Session token
                endpoint: API endpoint path
                params: Query parameters as string for cache key differentiation

        Returns:
                Cached data if available and not expired, None otherwise
        """
        await self._purge_expired_cache()
        cache_key = self._make_cache_key(token, endpoint, params)

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
        self, token: str, endpoint: str, params: str = ""
    ) -> tuple[Optional[Any], bool]:
        """
        Retrieve a cached response and indicate if revalidation is needed.

        For long-term cached endpoints, returns stale data immediately while marking for background revalidation.

        Args:
                token: Session token
                endpoint: API endpoint path
                params: Query parameters as string for cache key differentiation

        Returns:
                Tuple of (cached_data, needs_revalidation)
        """
        await self._purge_expired_cache()
        cache_key = self._make_cache_key(token, endpoint, params)

        async with self._lock:
            entry = self._cache.get(cache_key)
            if entry:
                ttl = (
                    LONG_CACHE_TTL_SECONDS if entry.is_long_term else CACHE_TTL_SECONDS
                )
                if not entry.is_expired(ttl):
                    # Check if needs revalidation (for long-term cache)
                    if entry.is_long_term and entry.is_stale(ttl):
                        return entry.data, True
                    return entry.data, False
                else:
                    self._cache.pop(cache_key)

        return None, False

    async def set_cache(
        self,
        token: str,
        endpoint: str,
        data: Any,
        params: str = "",
        is_long_term: bool = False,
    ) -> None:
        """
        Cache a response.

        Args:
                token: Session token
                endpoint: API endpoint path
                data: Response data to cache
                params: Query parameters as string for cache key differentiation
                is_long_term: Whether to use long-term caching (30 days)
        """
        cache_key = self._make_cache_key(token, endpoint, params)
        async with self._lock:
            self._cache[cache_key] = CacheEntry(
                data=data, created_at=datetime.utcnow(), is_long_term=is_long_term
            )

    async def invalidate_user_cache(self, token: str) -> None:
        """Invalidate all cache entries for a specific user."""
        async with self._lock:
            expired = [key for key in self._cache.keys() if key.startswith(token)]
            for key in expired:
                self._cache.pop(key)

    async def create_session(self, school_id: str, username: str, password: str) -> str:
        token = uuid.uuid4().hex
        client = SchulportalHessenAPI()

        login_result = await run_in_threadpool(
            client.login, school_id, username, password
        )
        if not login_result.get("success"):
            client.close()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=login_result
            )

        async with self._lock:
            self._sessions[token] = SessionData(
                client=client,
                created_at=datetime.utcnow(),
                last_used=datetime.utcnow(),
                username=username,
                school_id=school_id,
            )
        return token

    async def get_client(self, token: str) -> SchulportalHessenAPI:
        await self._purge_expired()
        async with self._lock:
            data = self._sessions.get(token)
            if not data:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired session token",
                )
            data.last_used = datetime.utcnow()
            return data.client

    async def drop_session(self, token: str) -> None:
        async with self._lock:
            data = self._sessions.pop(token, None)
        if data:
            await run_in_threadpool(data.client.logout)
            data.client.close()
        # Invalidate all cache entries for this user
        await self.invalidate_user_cache(token)

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.items())
            self._sessions.clear()
        for _, data in sessions:
            await run_in_threadpool(data.client.logout)
            data.client.close()


sessions = SessionManager()


# --- Background Task: Fetch and Store User Data ---
async def fetch_and_store_user_data(token: str, school_id: str, username: str) -> None:
    """
    Background task that fetches user data from /benutzerverwaltung.php
    and stores it in the user metrics database.

    Only updates the database if the user doesn't exist or if their data has changed.
    """
    try:
        # Get the client for this session
        client = await sessions.get_client(token)

        # Fetch user data from the Schulportal
        result = await run_in_threadpool(client.benutzer_get_data)

        if not result.get("success"):
            logger.warning(
                f"Failed to fetch user data for {username}@{school_id}: {result.get('error')}"
            )
            return

        user_data = result.get("data", {})

        # Store in database (only updates if data changed)
        is_new, was_updated = await user_metrics_db.upsert_user(
            school_id=school_id, login=username, user_data=user_data
        )

        if is_new:
            logger.info(f"New user recorded in metrics: {username}@{school_id}")
        elif was_updated:
            logger.info(f"User data updated in metrics: {username}@{school_id}")
        else:
            logger.debug(f"User data unchanged: {username}@{school_id}")

    except Exception as e:
        logger.error(f"Error storing user metrics for {username}@{school_id}: {e}")


app = FastAPI(title="Schulportal Hessen API", version="0.1.0")


async def client_dependency(
    x_session_token: str = Header(..., alias="X-Session-Token"),
) -> SchulportalHessenAPI:
    return await sessions.get_client(x_session_token)


def _should_cache(endpoint: str) -> bool:
    """Determine if an endpoint should be cached. Messages are never cached."""
    return not endpoint.startswith("/nachrichten")


def _make_param_key(params: Dict[str, Any]) -> str:
    """Convert query parameters to a string for cache key generation."""
    if not params:
        return ""
    sorted_params = sorted(params.items())
    return json.dumps(sorted_params)


def _responses_equal(old_data: Any, new_data: Any) -> bool:
    """
    Compare two responses for equality.
    Uses JSON serialization for reliable comparison.
    """
    try:
        return json.dumps(old_data, sort_keys=True) == json.dumps(
            new_data, sort_keys=True
        )
    except (TypeError, ValueError):
        return old_data == new_data


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:4173",
        "http://localhost:5173",
        "https://lanis.arg-server.de",
    ],
    allow_origin_regex=r"https://.*\.appwrite\.network",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],  # includes X-Session-Token
)


app.include_router(documentation_router)


@app.on_event("startup")
async def _startup() -> None:
    """Initialize task queue and database on startup."""
    await user_metrics_db.initialize()
    await task_queue.start()
    logger.info("API started with task queue and user metrics database")


@app.on_event("shutdown")
async def _cleanup_sessions() -> None:
    await task_queue.stop(wait=True, timeout=10.0)
    await sessions.shutdown()


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics/stats")
async def get_metrics_stats() -> Dict[str, Any]:
    # add auth later for admin view
    """
    Get user metrics statistics and task queue status.

    Returns aggregate stats about collected user data and current queue state.
    No authentication required - only returns aggregate/anonymous data.
    """
    db_stats = await user_metrics_db.get_stats()
    queue_stats = task_queue.get_queue_stats()

    return {
        "success": True,
        "database": db_stats,
        "task_queue": queue_stats,
    }


@app.post("/login", response_model=LoginResponse)
async def login_endpoint(payload: LoginRequest) -> LoginResponse:
    token = await sessions.create_session(
        payload.school_id, payload.username, payload.password
    )
    # Fetch a fresh client to read encryption state for the response
    client = await sessions.get_client(token)
    encryption_ready = bool(
        getattr(client, "cryptor", None) and client.cryptor.authenticated
    )

    # Queue background task to fetch and store user data in metrics DB
    user_data_task = Task(
        name=f"fetch_user_data:{payload.username}@{payload.school_id}",
        func=fetch_and_store_user_data,
        args=(token, payload.school_id, payload.username),
        priority=TaskPriority.LOW,  # Low priority to not impact API performance
        max_retries=2,
    )
    await task_queue.add_task(user_data_task)

    return LoginResponse(
        token=token,
        school_id=payload.school_id,
        username=payload.username,
        encryption_ready=encryption_ready,
    )


@app.post("/logout")
async def logout_endpoint(
    x_session_token: str = Header(..., alias="X-Session-Token"),
) -> Dict[str, str]:
    await sessions.drop_session(x_session_token)
    return {"status": "logged_out"}


@app.post("/dsb/login")
async def dsb_login_endpoint(
    payload: DsbLoginRequest,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    return await run_in_threadpool(client.dsb_login, payload.username, payload.password)


@app.post("/dsb/plan-urls")
async def dsb_plan_urls_endpoint(
    payload: DsbLoginRequest,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    return await run_in_threadpool(
        client.dsb_get_plan_urls, payload.username, payload.password
    )


@app.post("/dsb/plan")
async def dsb_plan_endpoint(
    payload: DsbPlanRequest,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    return await run_in_threadpool(
        client.dsb_get_substitution_plan,
        payload.username,
        payload.password,
        payload.plan_index,
        payload.plan_url,
        payload.include_raw,
    )


@app.get("/apps")
async def get_apps(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    # Try to get cached response (with revalidation check)
    cached_data, needs_revalidation = await sessions.get_cached_with_revalidate(
        x_session_token, "/apps"
    )

    # Start background revalidation task if needed
    if needs_revalidation:
        asyncio.create_task(
            _revalidate_endpoint(x_session_token, "/apps", client.get_apps)
        )

    # Return cached data if available
    if cached_data is not None:
        return cached_data

    # Fetch fresh data
    result = await run_in_threadpool(client.get_apps)

    # Cache the result with long-term TTL
    await sessions.set_cache(x_session_token, "/apps", result, is_long_term=True)
    return result


async def _revalidate_endpoint(token: str, endpoint: str, fetch_func) -> None:
    """Background task to revalidate stale cache entries."""
    try:
        # Fetch fresh data
        fresh_data = await run_in_threadpool(fetch_func)

        # Get current cached data
        cached_data = await sessions.get_cached(token, endpoint)

        # Only update cache if data has changed
        if cached_data is not None and not _responses_equal(cached_data, fresh_data):
            await sessions.set_cache(token, endpoint, fresh_data, is_long_term=True)
    except Exception:
        # Silently fail background revalidation - let next request handle it
        pass


@app.get("/modules")
async def get_modules(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    # Try to get cached response (with revalidation check)
    cached_data, needs_revalidation = await sessions.get_cached_with_revalidate(
        x_session_token, "/modules"
    )

    # Start background revalidation task if needed
    if needs_revalidation:
        asyncio.create_task(
            _revalidate_modules(x_session_token, client.get_available_modules)
        )

    # Return cached data if available
    if cached_data is not None:
        return cached_data

    # Fetch fresh data
    modules = await run_in_threadpool(client.get_available_modules)
    result = {"success": True, "modules": modules}

    # Cache the result with long-term TTL
    await sessions.set_cache(x_session_token, "/modules", result, is_long_term=True)
    return result


async def _revalidate_modules(token: str, fetch_func) -> None:
    """Background task to revalidate stale modules cache."""
    try:
        fresh_modules = await run_in_threadpool(fetch_func)
        fresh_data = {"success": True, "modules": fresh_modules}

        cached_data = await sessions.get_cached(token, "/modules")

        if cached_data is not None and not _responses_equal(cached_data, fresh_data):
            await sessions.set_cache(token, "/modules", fresh_data, is_long_term=True)
    except Exception:
        pass


@app.get("/benutzer")
async def get_user_data(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    # Try to get cached response (with revalidation check)
    cached_data, needs_revalidation = await sessions.get_cached_with_revalidate(
        x_session_token, "/benutzer"
    )

    # Start background revalidation task if needed
    if needs_revalidation:
        asyncio.create_task(
            _revalidate_endpoint(x_session_token, "/benutzer", client.benutzer_get_data)
        )

    # Return cached data if available
    if cached_data is not None:
        return cached_data

    # Fetch fresh data
    result = await run_in_threadpool(client.benutzer_get_data)

    # Cache the result with long-term TTL
    await sessions.set_cache(x_session_token, "/benutzer", result, is_long_term=True)
    return result


@app.get("/kalender")
async def get_calendar_overview(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    cached = await sessions.get_cached(x_session_token, "/kalender")
    if cached is not None:
        return cached

    result = await run_in_threadpool(client.kalender_get_overview)
    await sessions.set_cache(x_session_token, "/kalender", result)
    return result


@app.get("/kalender/events")
async def get_calendar_events(
    year: int = 0,
    start: str = "year",
    category: str = "",
    search: str = "",
    target: str = "",
    view_id: Optional[str] = None,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
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
    cached = await sessions.get_cached(x_session_token, "/kalender/events", params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(
        client.kalender_get_events, year, start, category, search, target, view_id
    )
    await sessions.set_cache(x_session_token, "/kalender/events", result, params)
    return result


@app.get("/kalender/event/{event_id}")
async def get_calendar_event(
    event_id: str,
    view_id: Optional[str] = None,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"event_id": event_id, "view_id": view_id or ""})
    cached = await sessions.get_cached(x_session_token, "/kalender/event", params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(client.kalender_get_event, event_id, view_id)
    await sessions.set_cache(x_session_token, "/kalender/event", result, params)
    return result


@app.get("/vertretungsplan")
async def get_vertretungsplan(
    include_raw: bool = False,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"include_raw": include_raw})
    cached = await sessions.get_cached(x_session_token, "/vertretungsplan", params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(client.vertretungsplan_get_plan, include_raw)
    await sessions.set_cache(x_session_token, "/vertretungsplan", result, params)
    return result


@app.get("/stundenplan")
async def get_stundenplan(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    cached = await sessions.get_cached(x_session_token, "/stundenplan")
    if cached is not None:
        return cached

    result = await run_in_threadpool(client.stundenplan_get_plan)
    await sessions.set_cache(x_session_token, "/stundenplan", result)
    return result


@app.get("/dateispeicher")
async def get_dateispeicher(
    folder_id: int = 0,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"folder_id": folder_id})
    cached = await sessions.get_cached(x_session_token, "/dateispeicher", params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(client.dateispeicher_get_node, folder_id)
    await sessions.set_cache(x_session_token, "/dateispeicher", result, params)
    return result


@app.get("/dateispeicher/search")
async def search_dateispeicher(
    q: str,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"q": q})
    cached = await sessions.get_cached(x_session_token, "/dateispeicher/search", params)
    if cached is not None:
        return cached

    result = await run_in_threadpool(client.dateispeicher_search_files, q)
    await sessions.set_cache(x_session_token, "/dateispeicher/search", result, params)
    return result


@app.get("/lerngruppen")
async def get_lerngruppen(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    cached = await sessions.get_cached(x_session_token, "/lerngruppen")
    if cached is not None:
        return cached

    result = await run_in_threadpool(client.lerngruppen_get_overview)
    await sessions.set_cache(x_session_token, "/lerngruppen", result)
    return result


# --- Message Cache Update Task ---
async def _update_message_cache_task(
    token: str, endpoint: str, fetch_func, params: dict, cache_params: str
):
    """
    Background task to fetch fresh message data and update cache if changed.
    Args:
            token: Session token
            endpoint: API endpoint path (e.g. /nachrichten/headers)
            fetch_func: Callable to fetch fresh data
            params: Dict of parameters for fetch_func
            cache_params: Stringified params for cache key
    """
    try:
        # Fetch fresh data
        fresh_data = await run_in_threadpool(fetch_func, **params)
        # Get current cached data
        cached_data = await sessions.get_cached(token, endpoint, cache_params)
        # Only update cache if data has changed
        if cached_data is None or not _responses_equal(cached_data, fresh_data):
            await sessions.set_cache(token, endpoint, fresh_data, cache_params)
    except Exception as e:
        logger.error(f"Error updating message cache for {endpoint}: {e}")


# --- Background File Download Task ---
async def _download_course_file(token: str, download_url: str, file_hash: str) -> None:
    if is_file_cached(file_hash):
        unmark_pending(file_hash)
        return

    write_pending_meta(file_hash, download_url)
    client = await sessions.get_client(token)
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


# --- Caching for /nachrichten endpoints ---
@app.get("/nachrichten/headers")
async def get_message_headers(
    get_type: str = "All",
    last: int = 0,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    endpoint = "/nachrichten/headers"
    params = {"get_type": get_type, "last": last}
    cache_params = _make_param_key(params)
    cached = await sessions.get_cached(x_session_token, endpoint, cache_params)
    # Always start background update task
    task = Task(
        name=f"update_message_cache:{endpoint}",
        func=_update_message_cache_task,
        args=(
            x_session_token,
            endpoint,
            client.nachrichten_get_headers,
            params,
            cache_params,
        ),
        priority=TaskPriority.LOW,
        max_retries=2,
    )
    await task_queue.add_task(task)
    if cached is not None:
        return cached
    # Fetch fresh if not cached
    result = await run_in_threadpool(client.nachrichten_get_headers, get_type, last)
    await sessions.set_cache(x_session_token, endpoint, result, cache_params)
    return result


@app.get("/nachrichten/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    last: int = 0,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    endpoint = "/nachrichten/conversation"
    params = {"conversation_id": conversation_id, "last": last}
    cache_params = _make_param_key(params)
    cached = await sessions.get_cached(x_session_token, endpoint, cache_params)
    # Always start background update task
    task = Task(
        name=f"update_message_cache:{endpoint}",
        func=_update_message_cache_task,
        args=(
            x_session_token,
            endpoint,
            client.nachrichten_get_conversation,
            params,
            cache_params,
        ),
        priority=TaskPriority.LOW,
        max_retries=2,
    )
    await task_queue.add_task(task)
    if cached is not None:
        return cached
    # Fetch fresh if not cached
    result = await run_in_threadpool(
        client.nachrichten_get_conversation, conversation_id, last
    )
    await sessions.set_cache(x_session_token, endpoint, result, cache_params)
    return result


@app.get("/nachrichten/search")
async def search_recipients(
    q: str,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    endpoint = "/nachrichten/search"
    params = {"q": q}
    cache_params = _make_param_key(params)
    cached = await sessions.get_cached(x_session_token, endpoint, cache_params)
    # Always start background update task
    task = Task(
        name=f"update_message_cache:{endpoint}",
        func=_update_message_cache_task,
        args=(
            x_session_token,
            endpoint,
            client.nachrichten_search_recipients,
            params,
            cache_params,
        ),
        priority=TaskPriority.LOW,
        max_retries=2,
    )
    await task_queue.add_task(task)
    if cached is not None:
        return cached
    # Fetch fresh if not cached
    result = await run_in_threadpool(client.nachrichten_search_recipients, q)
    await sessions.set_cache(x_session_token, endpoint, result, cache_params)
    return result


@app.post("/nachrichten/send")
async def send_message(
    recipients: List[str] = Body(..., description="List of recipient IDs"),
    subject: str = Body(..., description="Message subject"),
    body: str = Body(..., description="Message body"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    """
    Send a new message to one or more recipients.

    Args:
        recipients: List of recipient user IDs (e.g., ["l-14480"])
        subject: Message subject line
        body: Message content/text
    """
    message_data = {
        "recipients": recipients,
        "subject": subject,
        "body": body,
    }
    return await run_in_threadpool(client.nachrichten_send_message, message_data)


@app.post("/nachrichten/reply")
async def reply_message(
    conversation_id: str = Body(..., description="Conversation uniqid to reply to"),
    body: str = Body(..., description="Reply message body"),
    to: str = Body("all", description="Recipient selector: 'all' or a user id"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    """
    Send a reply to an existing conversation.
    """
    return await run_in_threadpool(
        client.nachrichten_reply_message,
        conversation_id,
        body,
        to,
    )


@app.post("/nachrichten/mark-read")
async def mark_read(
    conversation_id: str = Body(..., description="Conversation uniqid to mark read"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    return await run_in_threadpool(client.nachrichten_mark_read, conversation_id)


@app.get("/meinunterricht")
async def meinunterricht_overview(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    # Try to get cached response
    cached = await sessions.get_cached(x_session_token, "/meinunterricht")
    if cached is not None:
        return cached

    # Fetch fresh data
    result = await run_in_threadpool(client.meinunterricht_get_overview)

    # Cache the result
    await sessions.set_cache(x_session_token, "/meinunterricht", result)
    return result


@app.get("/meinunterricht/course/{course_id}")
async def meinunterricht_course(
    course_id: str,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"course_id": course_id})

    cached = await sessions.get_cached(
        x_session_token, "/meinunterricht/course", params
    )
    if cached is not None:
        return cached

    result = await run_in_threadpool(client.meinunterricht_get_course, course_id)

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
                        args=(x_session_token, original_url, file_hash),
                        priority=TaskPriority.LOW,
                        max_retries=2,
                    )
                    await task_queue.add_task(download_task)

    await sessions.set_cache(x_session_token, "/meinunterricht/course", result, params)
    return result


@app.get("/meinunterricht/course/{course_id}/details")
async def meinunterricht_course_details(
    course_id: str,
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    params = _make_param_key({"course_id": course_id})

    cached = await sessions.get_cached(
        x_session_token, "/meinunterricht/course/details", params
    )
    if cached is not None:
        return cached

    result = await run_in_threadpool(client.meinunterricht_get_course_details, course_id)
    await sessions.set_cache(
        x_session_token, "/meinunterricht/course/details", result, params
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
        client = await sessions.get_client(x_session_token)
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
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    # Create cache key with url parameter
    params = _make_param_key({"url": url})

    # Try to get cached response
    cached = await sessions.get_cached(x_session_token, "/meinunterricht/entry", params)
    if cached is not None:
        return cached

    # Fetch fresh data
    result = await run_in_threadpool(client.meinunterricht_get_entry_details, url)

    # Cache the result
    await sessions.set_cache(x_session_token, "/meinunterricht/entry", result, params)
    return result


@app.get("/meinunterricht/weekly")
async def meinunterricht_weekly(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    # Try to get cached response
    cached = await sessions.get_cached(x_session_token, "/meinunterricht/weekly")
    if cached is not None:
        return cached

    # Fetch fresh data
    result = await run_in_threadpool(client.meinunterricht_get_weekly_view)

    # Cache the result
    await sessions.set_cache(x_session_token, "/meinunterricht/weekly", result)
    return result


@app.get("/meinunterricht/submissions")
async def meinunterricht_submissions(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
) -> Dict[str, object]:
    # Try to get cached response
    cached = await sessions.get_cached(x_session_token, "/meinunterricht/submissions")
    if cached is not None:
        return cached

    # Fetch fresh data
    result = await run_in_threadpool(client.meinunterricht_get_submissions)

    # Cache the result
    await sessions.set_cache(x_session_token, "/meinunterricht/submissions", result)
    return result


@app.post("/meinunterricht/homework-done")
async def meinunterricht_homework_done(
    x_session_token: str = Header(..., alias="X-Session-Token"),
    client: SchulportalHessenAPI = Depends(client_dependency),
    course_id: str = Form(...),
    entry_id: str = Form(...),
    done: bool = Form(True),
) -> Dict[str, object]:
    """
    Mark or unmark homework as done for a specific entry
    """
    result = await run_in_threadpool(
        client.meinunterricht_set_homework_done, course_id, entry_id, done
    )

    # Invalidate cache for meinunterricht views
    await sessions.invalidate_user_cache(x_session_token)
    return result


# --- School List Caching ---
SCHOOL_LIST_CACHE_KEY = "school_list_all"
SCHOOL_LIST_CACHE_TTL = 2 * 24 * 60 * 60  # 2 days in seconds
SCHOOL_LIST_CACHE_AUTO_REFRESH = 3 * 24 * 60 * 60  # 3 days in seconds
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
    """
    Fetch all schools organized by district/region, with caching for 2 days and auto-refresh after 3 days.
    """
    now = datetime.utcnow()
    created_at = school_list_cache["created_at"]
    data = school_list_cache["data"]
    # If no cache or cache expired (older than 3 days), refresh synchronously
    if (
        not data
        or not created_at
        or (now - created_at).total_seconds() > SCHOOL_LIST_CACHE_AUTO_REFRESH
    ):
        return await _refresh_school_list_cache()
    # If cache is stale (older than 2 days but less than 3), serve stale and refresh in background
    if (now - created_at).total_seconds() > SCHOOL_LIST_CACHE_TTL:
        asyncio.create_task(_refresh_school_list_cache())
    return data


@app.get("/school-list/district/{district_id}")
async def school_list_by_district(district_id: str) -> Dict[str, object]:
    """
    Fetch schools for a specific district by ID

    This endpoint does not require authentication as school list data is public.

    Args:
            district_id: The district ID (e.g., '7' for Bergstraße/Odenwaldkreis)

    Returns:
            Dict with district data and its schools
            Example: {
                    'success': True,
                    'district': {
                            'id': '7',
                            'name': 'Bergstraße/Odenwaldkreis',
                            'schools': [...]
                    }
            }
    """
    client = SchulportalHessenAPI()
    result = await run_in_threadpool(client.school_list_get_by_district, district_id)
    return result


@app.get("/school-list/search")
async def school_list_search(q: str) -> Dict[str, object]:
    """
    Search for schools by name across all districts

    This endpoint does not require authentication as school list data is public.

    Args:
            q: The school name or partial name to search for (case-insensitive)

    Returns:
            Dict with search results
            Example: {
                    'success': True,
                    'query': 'Goethe',
                    'count': 3,
                    'results': [
                            {
                                    'district_id': '7',
                                    'district_name': 'Bergstraße/Odenwaldkreis',
                                    'school': {'id': '3351', 'name': 'Goetheschule', 'location': 'Viernheim'}
                            },
                            ...
                    ]
            }
    """
    client = SchulportalHessenAPI()
    result = await run_in_threadpool(client.school_list_search_by_name, q)
    return result


# Convenience alias for uvicorn CLI discovery
__all__ = ["app"]
