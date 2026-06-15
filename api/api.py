"""
FastAPI wrapper around `functions` to expose Schulportal Hessen endpoints.

Features:
- Per-API-user session tokens so multiple users with different credentials can work concurrently.
- Thin wrappers around the existing `SchulportalHessenAPI` methods.
- Long-term refresh tokens (90 days, stored in SQLite) survive backend restarts.
- Short-term access tokens (1 hour, JWT) are validated purely in-memory.
- In-memory Schulportal client cache minimises DB reads.

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

import jwt

from schulportal_hessen.base import SchulportalHessenAPI

from .queue import task_queue, Task, TaskPriority
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

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger("api")


SESSION_TTL_SECONDS = 1 * 60 * 60  # expire inactive Schulportal sessions after 1 hour
CACHE_TTL_SECONDS = 10 * 60  # cache responses for 10 minutes
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
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
    return f"{school_id}:{username}"


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

    data: Any
    created_at: datetime
    is_long_term: bool = False

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
        self._lock = asyncio.Lock()
        self._ttl = ttl_seconds

    # -- JWT helpers -------------------------------------------------

    def create_access_token(self, user_id: str, school_id: str, username: str) -> str:
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
                data=data, created_at=datetime.utcnow(), is_long_term=is_long_term
            )

    async def invalidate_user_cache(self, user_id: str) -> None:
        user_prefix = self._make_cache_key(user_id, "")[:64]
        async with self._lock:
            expired = [
                key for key in self._cache.keys() if key.startswith(user_prefix)
            ]
            for key in expired:
                self._cache.pop(key)


sessions = AuthManager()
_dsb_scheduler_task = None


# --- Background Tasks ---


async def fetch_and_store_user_data(user_id: str, school_id: str, username: str) -> None:
    """Background task: fetch user profile and store in metrics DB."""
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
    user_id = payload["sub"]
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


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:4173",
        "http://localhost:5173",
        "https://lanis.arg-server.de",
    ],
    allow_origin_regex=r"^https://.*\.surge\.sh$|^https://.*\.appwrite\.network$|^http://192\.168\.\d{1,3}\.\d{1,3}(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(documentation_router)


@app.on_event("startup")
async def _startup() -> None:
    global _dsb_scheduler_task
    await auth_db_initialize()
    await user_metrics_db.initialize()
    await dsb_snapshot_db.initialize()
    await task_queue.start()
    _dsb_scheduler_task = await run_dsb_scheduler()
    logger.info("API started with task queue, databases, and DSB snapshot scheduler")


@app.on_event("shutdown")
async def _cleanup_sessions() -> None:
    global _dsb_scheduler_task
    if _dsb_scheduler_task:
        _dsb_scheduler_task.cancel()
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
    # 1. Log into Schulportal
    user_id = await sessions.create_schulportal_session(
        payload.school_id, payload.username, payload.password
    )

    # 2. Store long-term refresh token in DB
    refresh_token = await store_refresh_token(
        user_id=user_id,
        school_id=payload.school_id,
        username=payload.username,
        password=payload.password,
    )

    # 3. Issue short-term access token (JWT)
    access_token = sessions.create_access_token(
        user_id=user_id,
        school_id=payload.school_id,
        username=payload.username,
    )

    # 4. Read encryption state
    session_data = await sessions._get_or_create_schulportal_client(user_id)
    encryption_ready = bool(
        getattr(session_data.client, "cryptor", None)
        and session_data.client.cryptor.authenticated
    )

    # 5. Queue background metrics fetch
    user_data_task = Task(
        name=f"fetch_user_data:{payload.username}@{payload.school_id}",
        func=fetch_and_store_user_data,
        args=(user_id, payload.school_id, payload.username),
        priority=TaskPriority.LOW,
        max_retries=2,
    )
    await task_queue.add_task(user_data_task)

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        school_id=payload.school_id,
        username=payload.username,
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
    cached = await sessions.get_cached(auth.user_id, "/stundenplan")
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.stundenplan_get_plan)
    await sessions.set_cache(auth.user_id, "/stundenplan", result)
    return result


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


async def _update_message_cache_task(
    user_id: str, endpoint: str, fetch_func, params: dict, cache_params: str
):
    try:
        fresh_data = await run_in_threadpool(fetch_func, **params)
        cached_data = await sessions.get_cached(user_id, endpoint, cache_params)
        if cached_data is None or not _responses_equal(cached_data, fresh_data):
            await sessions.set_cache(user_id, endpoint, fresh_data, cache_params)
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
    await sessions.set_cache(auth.user_id, endpoint, result, cache_params)
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
    await sessions.set_cache(auth.user_id, endpoint, result, cache_params)
    return result


@app.get("/nachrichten/search")
async def search_recipients(
    q: str,
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    endpoint = "/nachrichten/search"
    params = {"q": q}
    cache_params = _make_param_key(params)
    cached = await sessions.get_cached(auth.user_id, endpoint, cache_params)
    task = Task(
        name=f"update_message_cache:{endpoint}",
        func=_update_message_cache_task,
        args=(
            auth.user_id,
            endpoint,
            auth.client.nachrichten_search_recipients,
            params,
            cache_params,
        ),
        priority=TaskPriority.LOW,
        max_retries=2,
    )
    await task_queue.add_task(task)
    if cached is not None:
        return cached
    result = await run_in_threadpool(auth.client.nachrichten_search_recipients, q)
    await sessions.set_cache(auth.user_id, endpoint, result, cache_params)
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
    return await run_in_threadpool(auth.client.nachrichten_send_message, message_data)


@app.post("/nachrichten/reply")
async def reply_message(
    conversation_id: str = Body(..., description="Conversation uniqid to reply to"),
    body: str = Body(..., description="Reply message body"),
    to: str = Body("all", description="Recipient selector: 'all' or a user id"),
    auth: AuthSession = Depends(client_dependency),
) -> Dict[str, object]:
    return await run_in_threadpool(
        auth.client.nachrichten_reply_message, conversation_id, body, to
    )


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
    cached = await sessions.get_cached(auth.user_id, "/meinunterricht")
    if cached is not None:
        return cached

    result = await run_in_threadpool(auth.client.meinunterricht_get_overview)
    await sessions.set_cache(auth.user_id, "/meinunterricht", result)
    return result


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


__all__ = ["app"]
