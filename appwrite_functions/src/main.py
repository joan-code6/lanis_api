"""HTTP Appwrite Function for the LANiS API.

The function intentionally talks to the public ``sph-client`` package and the
local Appwrite backend adapter only.  It does not import the repository's
FastAPI application, which keeps this directory deployable as an independent
Appwrite bundle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import parse_qs, unquote_plus, urlsplit

try:  # Appwrite loads ``src/main.py`` either as a package or as a module.
    from .backend import get_backend
except ImportError:  # pragma: no cover - exercised by the Appwrite runtime
    try:
        from src.backend import get_backend
    except ImportError:
        from backend import get_backend

from schulportal_hessen import SchulportalHessenAPI

try:
    import jwt
except ImportError as exc:  # pragma: no cover - deployment dependency guard
    raise RuntimeError("PyJWT is required by the Appwrite HTTP Function") from exc


logger = logging.getLogger("lanis.appwrite.http")


class HttpError(Exception):
    """An expected request error with a safe public response."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _json_response(context: Any, payload: Any, status: int = 200) -> Any:
    return context.res.json(payload, status, _cors_headers(context))


def _cors_headers(context: Any) -> dict[str, str]:
    """Return browser-safe headers for the standalone HTTP Function.

    The API intentionally has no cookie-based authentication, so the default
    wildcard origin is safe for the token-in-header contract. Deployments can
    restrict it with ``LANIS_CORS_ORIGINS`` (comma-separated exact origins).
    """

    request = getattr(context, "req", context)
    origin = _header(request, "origin")
    configured = os.getenv("LANIS_CORS_ORIGINS", "").strip()
    allowed = {item.strip() for item in configured.split(",") if item.strip()}
    if not allowed or "*" in allowed:
        allow_origin = "*"
        vary = None
    elif origin and _cors_origin_allowed(origin, allowed):
        allow_origin = origin
        vary = "Origin"
    else:
        allow_origin = ""
        vary = None

    headers = {
        "Access-Control-Allow-Headers": (
            "Content-Type, X-Session-Token, Authorization, X-Appwrite-Key"
        ),
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Max-Age": "86400",
    }
    if allow_origin:
        headers["Access-Control-Allow-Origin"] = allow_origin
    if vary:
        headers["Vary"] = vary
    return headers


def _cors_origin_allowed(origin: str, allowed: set[str]) -> bool:
    """Match exact origins and hostname suffix patterns from the allowlist."""

    try:
        parsed_origin = urlsplit(origin)
    except ValueError:
        return False
    if parsed_origin.scheme not in {"http", "https"} or not parsed_origin.hostname:
        return False

    hostname = parsed_origin.hostname.lower().rstrip(".")
    for pattern in allowed:
        candidate = pattern.strip().lower()
        if not candidate:
            continue
        if "://" in candidate:
            try:
                parsed_pattern = urlsplit(candidate)
            except ValueError:
                continue
            if parsed_pattern.scheme and parsed_pattern.scheme != parsed_origin.scheme:
                continue
            candidate = parsed_pattern.hostname or ""
        candidate = candidate.rstrip(".")
        if candidate.startswith("*."):
            suffix = candidate[1:]
            if hostname.endswith(suffix) and hostname != suffix[1:]:
                return True
        elif candidate.startswith("."):
            if hostname.endswith(candidate) and hostname != candidate[1:]:
                return True
        elif hostname == candidate and parsed_origin.port is None:
            return True
    return False


def _binary_response(context: Any, content: bytes, headers: dict[str, str]) -> Any:
    response_headers = {**_cors_headers(context), **headers}
    return context.res.binary(content, 200, response_headers)


def _header(request: Any, name: str) -> str | None:
    headers = getattr(request, "headers", {}) or {}
    wanted = name.lower()
    if hasattr(headers, "get"):
        value = headers.get(wanted)
        if value is None:
            value = headers.get(name)
        return str(value) if value is not None else None
    return None


def _query(request: Any) -> dict[str, str]:
    raw = getattr(request, "query", None)
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    query_string = getattr(request, "query_string", "") or ""
    parsed = parse_qs(str(query_string), keep_blank_values=True)
    return {key: unquote_plus(values[-1]) for key, values in parsed.items()}


def _body(request: Any) -> dict[str, Any]:
    value = getattr(request, "body_json", None)
    if isinstance(value, dict):
        return value
    raw = getattr(request, "body_text", None)
    if raw is None:
        raw_bytes = getattr(request, "body_binary", b"") or b""
        raw = raw_bytes.decode("utf-8", errors="replace")
    if not str(raw).strip():
        return {}
    content_type = (_header(request, "content-type") or "").lower()
    if "application/x-www-form-urlencoded" in content_type:
        parsed_form = parse_qs(str(raw), keep_blank_values=True)
        return {key: unquote_plus(values[-1]) for key, values in parsed_form.items()}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError) as exc:
        raise HttpError(400, "Request body must contain valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HttpError(400, "Request body must be a JSON object")
    return parsed


def _required(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or not str(value).strip():
        raise HttpError(422, f"Missing required field: {key}")
    return str(value).strip()


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _query_required(query: dict[str, str], key: str) -> str:
    value = query.get(key, "").strip()
    if not value:
        raise HttpError(422, f"Missing required query parameter: {key}")
    return value


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET") or os.getenv("LANIS_JWT_SECRET")
    if not secret or len(secret) < 32:
        raise HttpError(503, "JWT_SECRET is not configured")
    return secret


def _make_access_token(user_id: str, school_id: str, username: str) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": user_id,
        "school_id": school_id,
        "username": username,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "jti": secrets.token_hex(12),
    }
    return str(jwt.encode(claims, _jwt_secret(), algorithm="HS256"))


def _decode_access_token(token: str) -> dict[str, Any]:
    if not token:
        raise HttpError(401, "Authentication required")
    try:
        claims = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except (jwt.InvalidTokenError, TypeError, ValueError) as exc:
        raise HttpError(401, "Invalid or expired session token") from exc
    if not claims.get("sub"):
        raise HttpError(401, "Invalid session token")
    return claims


def _session_token(request: Any) -> str | None:
    token = _header(request, "X-Session-Token")
    if token:
        return token
    authorization = _header(request, "Authorization") or ""
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


async def _new_client(credentials: Any) -> SchulportalHessenAPI:
    client = SchulportalHessenAPI()
    result = await asyncio.to_thread(
        client.login,
        credentials.school_id,
        credentials.username,
        credentials.password,
    )
    if not isinstance(result, dict) or not result.get("success"):
        client.close()
        raise HttpError(401, "Schulportal session could not be restored")
    return client


async def _protected(context: Any) -> tuple[Any, Any, SchulportalHessenAPI]:
    token = _session_token(context.req)
    claims = _decode_access_token(token) if token else {}
    appwrite_user_id = _header(context.req, "x-appwrite-user-id")
    user_id = str(claims.get("sub") or appwrite_user_id or "")
    if not user_id:
        raise HttpError(401, "Authentication required")
    backend = get_backend(dynamic_key=_header(context.req, "x-appwrite-key"))
    credentials = await backend.credentials.get_credentials(user_id)
    if credentials is None:
        raise HttpError(401, "Session credentials are no longer available")
    client = await _new_client(credentials)
    return backend, credentials, client


async def _public_client() -> SchulportalHessenAPI:
    return SchulportalHessenAPI()


async def _cached_call(
    backend: Any,
    user_id: str,
    endpoint: str,
    params: str,
    func: Callable[[], Any],
    *,
    long_term: bool = False,
) -> Any:
    cached, stale = await backend.cache.get_with_revalidate(user_id, endpoint, params)
    if cached is not None:
        if stale:
            try:
                await backend.dispatcher.dispatch(
                    "refresh_cache",
                    {"user_id": user_id, "endpoint": endpoint, "params": params},
                )
            except Exception:
                logger.debug("Unable to dispatch cache refresh", exc_info=True)
        return cached
    result = await asyncio.to_thread(func)
    await backend.cache.set(user_id, endpoint, result, params, is_long_term=long_term)
    return result


async def _login(context: Any, payload: dict[str, Any]) -> dict[str, Any]:
    school_id = _required(payload, "school_id")
    username = _required(payload, "username")
    password = _required(payload, "password")
    client = await _public_client()
    try:
        result = await asyncio.to_thread(client.login, school_id, username, password)
    finally:
        # The client only needs to live long enough to validate credentials.
        client.close()
    if not isinstance(result, dict) or not result.get("success"):
        raise HttpError(401, "Login failed")

    backend = get_backend(dynamic_key=_header(context.req, "x-appwrite-key"))
    refresh_token = await backend.credentials.store_refresh_token(
        user_id=_stable_user_id(school_id, username),
        school_id=school_id,
        username=username,
        password=password,
    )
    identity = await backend.identity.ensure_user_and_create_token(school_id, username)
    access_token = _make_access_token(identity.user_id, school_id, username)
    try:
        await backend.dispatcher.dispatch(
            "fetch_user_data",
            {"user_id": identity.user_id, "school_id": school_id, "username": username},
        )
    except Exception:
        logger.warning("User-data Function dispatch failed after login", exc_info=True)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "school_id": school_id,
        "username": username,
        "encryption_ready": bool(result.get("encryption_ready")),
        "expires_in": 3600,
        "appwrite_user_id": identity.user_id,
        "appwrite_token": identity.secret,
        "appwrite_token_expire": identity.expire,
    }


def _stable_user_id(school_id: str, username: str) -> str:
    # Kept in sync with the backend IdentityService's SHA-256-derived ID.
    import hashlib

    return "d" + hashlib.sha256(f"identity:{school_id}:{username}".encode()).hexdigest()[:35]


async def _refresh(context: Any, payload: dict[str, Any]) -> dict[str, Any]:
    token = _required(payload, "refresh_token")
    backend = get_backend(dynamic_key=_header(context.req, "x-appwrite-key"))
    data = await backend.credentials.get_refresh_token(token)
    if not data:
        raise HttpError(401, "Invalid or expired refresh token")
    return {
        "access_token": _make_access_token(
            str(data["user_id"]), str(data["school_id"]), str(data["username"])
        ),
        "token_type": "bearer",
        "expires_in": 3600,
    }


async def _call_public(operation: str, args: list[Any], kwargs: dict[str, Any]) -> Any:
    allowed = {
        "school_list_get_all",
        "school_list_get_by_district",
        "school_list_search_by_name",
        "dsb_login",
        "dsb_get_plan_urls",
        "dsb_get_substitution_plan",
    }
    if operation not in allowed:
        raise HttpError(404, "Unknown public operation")
    client = await _public_client()
    try:
        return await asyncio.to_thread(getattr(client, operation), *args, **kwargs)
    finally:
        client.close()


async def _call_protected(
    context: Any,
    operation: str,
    args: list[Any],
    kwargs: dict[str, Any],
    *,
    endpoint: str | None = None,
    cache: bool = True,
    long_term: bool = False,
) -> Any:
    allowed = {
        "get_apps",
        "get_available_modules",
        "benutzer_get_data",
        "kalender_get_overview",
        "kalender_get_events",
        "kalender_get_event",
        "vertretungsplan_get_plan",
        "stundenplan_get_plan",
        "dateispeicher_get_root",
        "dateispeicher_get_node",
        "dateispeicher_search_files",
        "lerngruppen_get_overview",
        "nachrichten_get_headers",
        "nachrichten_get_conversation",
        "nachrichten_search_recipients",
        "nachrichten_send_message",
        "nachrichten_reply_message",
        "nachrichten_mark_read",
        "meinunterricht_get_overview",
        "meinunterricht_get_course",
        "meinunterricht_get_entry_details",
        "meinunterricht_get_weekly_view",
        "meinunterricht_get_submissions",
        "meinunterricht_set_homework_done",
        "meinunterricht_download_file",
        "dsb_login",
        "dsb_get_plan_urls",
        "dsb_get_substitution_plan",
    }
    if operation not in allowed:
        raise HttpError(404, "Unknown LANiS operation")
    backend, credentials, client = await _protected(context)
    try:
        if operation == "get_available_modules":
            call = lambda: {"success": True, "modules": client.get_available_modules()}
        else:
            call = lambda: getattr(client, operation)(*args, **kwargs)
        if cache and endpoint:
            return await _cached_call(
                backend,
                credentials.user_id,
                endpoint,
                json.dumps({"args": args, "kwargs": kwargs}, sort_keys=True),
                call,
                long_term=long_term,
            )
        return await asyncio.to_thread(call)
    finally:
        client.close()


async def _course(context: Any, course_id: str) -> Any:
    backend, credentials, client = await _protected(context)
    endpoint = "/meinunterricht/course"
    params = json.dumps({"course_id": course_id}, sort_keys=True)
    try:
        cached, _stale = await backend.cache.get_with_revalidate(
            credentials.user_id, endpoint, params
        )
        if cached is not None:
            return cached
        result = await asyncio.to_thread(client.meinunterricht_get_course, course_id)
        if isinstance(result, dict) and result.get("success"):
            for entry in result.get("entries", []):
                for file_info in entry.get("files", []):
                    source_url = file_info.get("download_url") or file_info.get("url")
                    if not source_url:
                        continue
                    file_hash = backend.files.hash_url(str(source_url))
                    already_cached = await backend.files.is_cached(file_hash)
                    already_pending = await backend.files.is_pending(file_hash)
                    if not already_cached and not already_pending:
                        await backend.files.mark_pending(file_hash, str(source_url))
                    base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
                    if not base_url:
                        scheme = str(getattr(context.req, "scheme", "https") or "https")
                        host = str(getattr(context.req, "host", "") or "")
                        base_url = f"{scheme}://{host}".rstrip("/")
                    file_url = f"{base_url}/meinunterricht/file/{file_hash}"
                    file_info.update(
                        {"download_url": file_url, "url": file_url, "file_hash": file_hash}
                    )
                    if not already_cached and not already_pending:
                        await backend.dispatcher.dispatch(
                            "download_course_file",
                            {"user_id": credentials.user_id, "file_hash": file_hash},
                        )
            await backend.cache.set(credentials.user_id, endpoint, result, params)
        return result
    finally:
        client.close()


async def _file(context: Any, file_hash: str) -> Any:
    backend, _credentials, client = await _protected(context)
    try:
        metadata = await backend.files.get_metadata(file_hash)
        if metadata and metadata.status == "ready":
            content = await backend.files.get_content(file_hash)
            headers = {
                "Content-Type": metadata.content_type or "application/octet-stream",
                "Content-Disposition": f'attachment; filename="{metadata.filename or "download"}"',
            }
            return _binary_response(context, content, headers)
        # A synchronous fallback makes the endpoint useful when a worker has
        # not yet been scheduled, while the metadata remains server-only.
        if metadata and metadata.source_url:
            result = await asyncio.to_thread(
                client.meinunterricht_download_file, metadata.source_url
            )
            if result.get("success"):
                await backend.files.save(
                    file_hash,
                    result["content"],
                    result.get("content_type", "application/octet-stream"),
                    result.get("filename", "download"),
                )
                content = await backend.files.get_content(file_hash)
                return _binary_response(
                    context,
                    content,
                    {
                        "Content-Type": result.get(
                            "content_type", "application/octet-stream"
                        ),
                        "Content-Disposition": (
                            f'attachment; filename="{result.get("filename", "download")}"'
                        ),
                    },
                )
        raise HttpError(404, "File not yet available, please try again shortly")
    finally:
        client.close()


async def _dispatch_route(context: Any) -> Any:
    request = context.req
    method = str(getattr(request, "method", "GET")).upper()
    path = str(getattr(request, "path", "/") or "/").rstrip("/") or "/"
    query = _query(request)
    payload = _body(request) if method in {"POST", "PUT", "PATCH"} else {}

    if method == "OPTIONS":
        return {"status": "ok"}
    if method == "GET" and path == "/health":
        return {"status": "ok"}
    if method == "POST" and path == "/login":
        return await _login(context, payload)
    if method == "POST" and path == "/auth/refresh":
        return await _refresh(context, payload)
    if method == "POST" and path == "/logout":
        token = _session_token(request)
        claims = _decode_access_token(token) if token else {}
        user_id = str(claims.get("sub") or _header(request, "x-appwrite-user-id") or "")
        if not user_id:
            raise HttpError(401, "Authentication required")
        backend = get_backend(dynamic_key=_header(request, "x-appwrite-key"))
        await backend.credentials.delete_user_tokens(user_id)
        return {"status": "logged_out"}

    if method == "GET" and path == "/school-list":
        return await _call_public("school_list_get_all", [], {})
    if method == "GET" and path.startswith("/school-list/district/"):
        return await _call_public("school_list_get_by_district", [path.rsplit("/", 1)[1]], {})
    if method == "GET" and path == "/school-list/search":
        return await _call_public("school_list_search_by_name", [_query_required(query, "q")], {})

    if method == "GET" and path == "/metrics/stats":
        backend, _credentials, client = await _protected(context)
        client.close()
        return {"success": True, "database": await backend.metrics.get_stats(), "provider": "appwrite"}

    if path == "/meinunterricht/file" or path.startswith("/meinunterricht/file/"):
        if method != "GET" or path == "/meinunterricht/file":
            raise HttpError(404, "File not found")
        return await _file(context, path.rsplit("/", 1)[1])
    if method == "GET" and path == "/meinunterricht/course":
        raise HttpError(422, "course_id is required")
    if method == "GET" and path.startswith("/meinunterricht/course/"):
        return await _course(context, path.rsplit("/", 1)[1])

    route_map: dict[tuple[str, str], tuple[str, str | None, bool, bool]] = {
        ("GET", "/apps"): ("get_apps", "/apps", True, True),
        ("GET", "/modules"): ("get_available_modules", "/modules", True, True),
        ("GET", "/benutzer"): ("benutzer_get_data", "/benutzer", True, True),
        ("GET", "/kalender"): ("kalender_get_overview", "/kalender", True, False),
        ("GET", "/kalender/events"): ("kalender_get_events", "/kalender/events", True, False),
        ("GET", "/vertretungsplan"): ("vertretungsplan_get_plan", "/vertretungsplan", True, False),
        ("GET", "/stundenplan"): ("stundenplan_get_plan", "/stundenplan", True, False),
        ("GET", "/dateispeicher"): ("dateispeicher_get_node", "/dateispeicher", True, False),
        ("GET", "/dateispeicher/search"): ("dateispeicher_search_files", "/dateispeicher/search", True, False),
        ("GET", "/lerngruppen"): ("lerngruppen_get_overview", "/lerngruppen", True, False),
        ("GET", "/nachrichten/headers"): ("nachrichten_get_headers", "/nachrichten/headers", True, False),
        ("GET", "/nachrichten/search"): ("nachrichten_search_recipients", "/nachrichten/search", True, False),
        ("GET", "/meinunterricht"): ("meinunterricht_get_overview", "/meinunterricht", True, False),
        ("GET", "/meinunterricht/entry"): ("meinunterricht_get_entry_details", "/meinunterricht/entry", True, False),
        ("GET", "/meinunterricht/weekly"): ("meinunterricht_get_weekly_view", "/meinunterricht/weekly", True, False),
        ("GET", "/meinunterricht/submissions"): ("meinunterricht_get_submissions", "/meinunterricht/submissions", True, False),
    }
    if (method, path) in route_map:
        operation, endpoint, use_cache, long_term = route_map[(method, path)]
        args: list[Any] = []
        kwargs: dict[str, Any] = {}
        if path == "/kalender/events":
            kwargs = {"year": _int(query.get("year")), "start": query.get("start", "year"), "category": query.get("category", ""), "search": query.get("search", ""), "target": query.get("target", ""), "view_id": query.get("view_id") or None}
        elif path == "/vertretungsplan":
            kwargs = {"include_raw": _bool(query.get("include_raw"))}
        elif path == "/dateispeicher":
            kwargs = {"folder_id": _int(query.get("folder_id"))}
        elif path == "/dateispeicher/search":
            kwargs = {"query": _query_required(query, "q")}
        elif path == "/nachrichten/headers":
            kwargs = {"get_type": query.get("get_type", "All"), "last": _int(query.get("last"))}
        elif path == "/nachrichten/search":
            kwargs = {"query": _query_required(query, "q")}
        elif path == "/meinunterricht/entry":
            kwargs = {"url": _query_required(query, "url")}
        return await _call_protected(context, operation, args, kwargs, endpoint=endpoint, cache=use_cache, long_term=long_term)

    if method == "GET" and path.startswith("/kalender/event/"):
        return await _call_protected(context, "kalender_get_event", [path.rsplit("/", 1)[1]], {"view_id": query.get("view_id") or None}, endpoint="/kalender/event")
    if method == "GET" and path.startswith("/nachrichten/"):
        return await _call_protected(context, "nachrichten_get_conversation", [path.rsplit("/", 1)[1]], {"last": _int(query.get("last"))}, endpoint="/nachrichten/conversation")

    if method == "POST" and path in {"/dsb/login", "/dsb/plan-urls", "/dsb/plan"}:
        operation = {"/dsb/login": "dsb_login", "/dsb/plan-urls": "dsb_get_plan_urls", "/dsb/plan": "dsb_get_substitution_plan"}[path]
        if operation == "dsb_login":
            args = [_required(payload, "username"), _required(payload, "password")]
        elif operation == "dsb_get_plan_urls":
            args = [payload.get("username"), payload.get("password")]
        else:
            args = [payload.get("username"), payload.get("password"), _int(payload.get("plan_index")), payload.get("plan_url"), _bool(payload.get("include_raw"))]
        return await _call_protected(context, operation, args, {}, cache=False)
    if method == "POST" and path == "/nachrichten/send":
        return await _call_protected(context, "nachrichten_send_message", [{"recipients": payload.get("recipients", []), "subject": payload.get("subject", ""), "body": payload.get("body", "")}], {}, cache=False)
    if method == "POST" and path == "/nachrichten/reply":
        return await _call_protected(context, "nachrichten_reply_message", [_required(payload, "conversation_id"), _required(payload, "body"), payload.get("to", "all")], {}, cache=False)
    if method == "POST" and path == "/nachrichten/mark-read":
        return await _call_protected(context, "nachrichten_mark_read", [_required(payload, "conversation_id")], {}, cache=False)
    if method == "POST" and path == "/meinunterricht/homework-done":
        result = await _call_protected(context, "meinunterricht_set_homework_done", [_required(payload, "course_id"), _required(payload, "entry_id"), _bool(payload.get("done"), True)], {}, cache=False)
        backend, _credentials, client = await _protected(context)
        client.close()
        await backend.cache.invalidate_user(_credentials.user_id)
        return result

    # A constrained RPC endpoint keeps newly released sph-client operations
    # available without exposing arbitrary object attributes.
    if method == "POST" and path.startswith("/sph/call/"):
        operation = path.rsplit("/", 1)[1]
        args = payload.get("args", [])
        kwargs = payload.get("kwargs", {})
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            raise HttpError(422, "sph/call args must be a list and kwargs an object")
        return await _call_protected(context, operation, args, kwargs, cache=False)

    raise HttpError(404, "Route not found")


async def main(context: Any) -> Any:
    """Appwrite Function entrypoint."""
    try:
        result = await _dispatch_route(context)
        if isinstance(result, (dict, list)):
            return _json_response(context, result)
        if isinstance(result, str):
            return context.res.text(result, 200, _cors_headers(context))
        return result
    except HttpError as exc:
        return _json_response(context, {"success": False, "error": exc.detail}, exc.status)
    except Exception:
        try:
            context.error("Unhandled LANiS Appwrite Function error")
        except Exception:
            logger.exception("Unhandled Appwrite Function error")
        return _json_response(context, {"success": False, "error": "Internal server error"}, 500)


__all__ = ["main"]
