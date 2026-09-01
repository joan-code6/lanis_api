"""Appwrite Functions adapter for the complete LANIS FastAPI application."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

os.environ.setdefault("LANIS_APPWRITE_NATIVE", "1")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.api import _startup, app  # noqa: E402

try:  # Appwrite may load the entrypoint as either a package or a plain module.
    from .backend import get_backend  # noqa: E402
except ImportError:  # pragma: no cover - Appwrite runtime import shape
    from appwrite_functions.src.backend import get_backend  # noqa: E402


_startup_lock = asyncio.Lock()
_started = False


def _headers(request: Any) -> dict[str, str]:
    values = getattr(request, "headers", {}) or {}
    return {str(key).lower(): str(value) for key, value in dict(values).items()}


async def _ensure_started() -> None:
    global _started
    if _started:
        return
    async with _startup_lock:
        if not _started:
            await _startup()
            _started = True


async def _invoke_asgi(context: Any) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    request = context.req
    headers = _headers(request)
    body = getattr(request, "body_binary", None)
    if body is None:
        body = str(getattr(request, "body_text", "") or "").encode()
    query_string = getattr(request, "query_string", "") or ""
    if isinstance(query_string, bytes):
        query_bytes = query_string
    else:
        query_bytes = str(query_string).lstrip("?").encode()
    path = str(getattr(request, "path", "/") or "/")
    raw_headers = [(key.encode(), value.encode()) for key, value in headers.items()]
    status_code = 500
    response_headers: list[tuple[bytes, bytes]] = []
    chunks: list[bytes] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if request_sent:
            await asyncio.Event().wait()
        request_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code, response_headers
        if message["type"] == "http.response.start":
            status_code = int(message["status"])
            response_headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": str(getattr(request, "method", "GET")).upper(),
        "scheme": str(getattr(request, "scheme", "https") or "https"),
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_bytes,
        "root_path": "",
        "headers": raw_headers,
        "client": (headers.get("x-forwarded-for", "appwrite"), 0),
        "server": (str(getattr(request, "host", "appwrite")), 443),
    }
    await app(scope, receive, send)
    return status_code, response_headers, b"".join(chunks)


async def main(context: Any) -> Any:
    """Forward one Appwrite execution to FastAPI without running a web server."""
    try:
        headers = _headers(context.req)
        get_backend(dynamic_key=headers.get("x-appwrite-key"))
        await _ensure_started()
        status_code, raw_headers, body = await _invoke_asgi(context)
        response_headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in raw_headers
            if key.lower() not in {b"content-length", b"transfer-encoding"}
        }
        return context.res.binary(body, status_code, response_headers)
    except Exception as error:  # pragma: no cover - runtime safety net
        context.error(f"LANIS FastAPI adapter failed: {type(error).__name__}: {error}")
        return context.res.json(
            {"success": False, "error": "Internal server error"}, 500
        )


__all__ = ["main"]
