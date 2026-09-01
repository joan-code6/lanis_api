"""Asynchronous Appwrite Function jobs for LANiS.

Only the allowlisted jobs below can be executed.  Every job reconstructs its
Schulportal session from encrypted TablesDB credentials, making retries and
cold starts safe.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

os.environ.setdefault("LANIS_APPWRITE_NATIVE", "1")

try:
    from .backend import get_backend
except ImportError:  # pragma: no cover
    try:
        from src.backend import get_backend
    except ImportError:
        from backend import get_backend

from schulportal_hessen import SchulportalHessenAPI

try:
    from .file_download import download_course_file
except ImportError:  # pragma: no cover - Appwrite may load this as a module
    from file_download import download_course_file

logger = logging.getLogger("lanis.appwrite.worker")


def _body(context: Any) -> dict[str, Any]:
    request = context.req
    value = getattr(request, "body_json", None)
    if isinstance(value, dict):
        return value
    raw = getattr(request, "body_text", None)
    if raw is None:
        raw = (getattr(request, "body_binary", b"") or b"").decode(
            "utf-8", errors="replace"
        )
    if not str(raw).strip():
        return {}
    value = json.loads(str(raw))
    if not isinstance(value, dict):
        raise TypeError("job body must be an object")
    return value


def _header(context: Any, name: str) -> str | None:
    headers = getattr(context.req, "headers", {}) or {}
    value = headers.get(name.lower()) if hasattr(headers, "get") else None
    return str(value) if value else None


async def _with_client(credentials: Any) -> SchulportalHessenAPI:
    client = SchulportalHessenAPI()
    result = await asyncio.to_thread(
        client.login,
        credentials.school_id,
        credentials.username,
        credentials.password,
    )
    if not isinstance(result, dict) or not result.get("success"):
        client.close()
        raise RuntimeError("Unable to restore the Schulportal session")
    return client


async def _fetch_user_data(backend: Any, payload: dict[str, Any]) -> dict[str, Any]:
    user_id = str(payload["user_id"])
    credentials = await backend.credentials.get_credentials(user_id)
    if credentials is None:
        raise RuntimeError("No credentials found for user-data job")
    client = await _with_client(credentials)
    try:
        user_data = await asyncio.to_thread(client.benutzer_get_data)
    finally:
        client.close()
    is_new, updated = await backend.metrics.upsert_user(
        credentials.school_id, credentials.username, user_data
    )
    return {"user_id": user_id, "new": is_new, "updated": updated}


async def _download_course_file(
    backend: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    file_hash = str(payload["file_hash"])
    metadata = await backend.files.get_metadata(file_hash)
    if metadata is None or not metadata.source_url:
        raise RuntimeError("No source URL found for file job")
    if await backend.files.is_cached(file_hash):
        await backend.files.unmark_pending(file_hash)
        return {"file_hash": file_hash, "status": "already_ready"}
    credentials = await backend.credentials.get_credentials(str(payload["user_id"]))
    if credentials is None:
        raise RuntimeError("No credentials found for file job")
    client = await _with_client(credentials)
    try:
        result = await asyncio.to_thread(
            download_course_file, client, metadata.source_url
        )
    finally:
        client.close()
    if not isinstance(result, dict) or not result.get("success"):
        await backend.files.unmark_pending(file_hash)
        raise RuntimeError("Schulportal file download failed")
    await backend.files.save(
        file_hash,
        result["content"],
        result.get("content_type", "application/octet-stream"),
        result.get("filename", "download"),
    )
    return {"file_hash": file_hash, "status": "ready"}


async def _refresh_cache(backend: Any, payload: dict[str, Any]) -> dict[str, Any]:
    user_id = str(payload["user_id"])
    endpoint = str(payload["endpoint"])
    params = str(payload.get("params", ""))
    credentials = await backend.credentials.get_credentials(user_id)
    if credentials is None:
        raise RuntimeError("No credentials found for cache refresh")
    client = await _with_client(credentials)
    operations = {
        "/apps": ("get_apps", True),
        "/modules": ("get_available_modules", True),
        "/benutzer": ("benutzer_get_data", True),
        "/kalender": ("kalender_get_overview", False),
        "/meinunterricht": ("meinunterricht_get_overview", False),
        "/meinunterricht/weekly": ("meinunterricht_get_weekly_view", False),
        "/meinunterricht/submissions": ("meinunterricht_get_submissions", False),
        "/lerngruppen": ("lerngruppen_get_overview", False),
        "/stundenplan": ("stundenplan_get_plan", False),
    }
    operation = operations.get(endpoint)
    if operation is None:
        client.close()
        return {"status": "ignored", "endpoint": endpoint}
    try:
        result = await asyncio.to_thread(getattr(client, operation[0]))
    finally:
        client.close()
    if operation[0] == "get_available_modules":
        result = {"success": True, "modules": result}
    await backend.cache.set(
        user_id, endpoint, result, params, is_long_term=operation[1]
    )
    return {"status": "refreshed", "endpoint": endpoint}


async def _run_job(backend: Any, job: str, payload: dict[str, Any]) -> dict[str, Any]:
    jobs = {
        "fetch_user_data": _fetch_user_data,
        "download_course_file": _download_course_file,
        "refresh_cache": _refresh_cache,
    }
    handler = jobs.get(job)
    if handler is None:
        raise ValueError("Unknown worker job")
    return await handler(backend, payload)


async def _notification_cycle(backend: Any) -> dict[str, Any]:
    from api.message_notifications import run_message_notification_cycle
    from api.persistence import get_notification_preferences

    clients: dict[str, SchulportalHessenAPI] = {}

    async def get_client(user_id: str) -> SchulportalHessenAPI:
        if user_id not in clients:
            credentials = await backend.credentials.get_credentials(user_id)
            if credentials is None:
                raise RuntimeError("No credentials found for notification poll")
            clients[user_id] = await _with_client(credentials)
        return clients[user_id]

    try:
        await run_message_notification_cycle(
            get_client,
            backend.cache.invalidate_endpoint,
            get_notification_preferences,
        )
    finally:
        for client in clients.values():
            client.close()
    return {"status": "completed", "clients": len(clients)}


async def main(context: Any) -> Any:
    """Appwrite Function entrypoint for asynchronous worker executions."""
    try:
        body = _body(context)
        backend = get_backend(dynamic_key=_header(context, "x-appwrite-key"))
        if not body:
            result = await _notification_cycle(backend)
            return context.res.json(
                {"success": True, "job": "notification_cycle", "result": result}
            )
        if body.get("version") != 1:
            raise ValueError("Unsupported job payload version")
        job = str(body.get("task", ""))
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise TypeError("Job payload must be an object")
        result = await _run_job(backend, job, payload)
        return context.res.json({"success": True, "job": job, "result": result})
    except Exception as exc:  # noqa: BLE001 - Appwrite must receive a safe 500
        try:
            context.error(f"LANiS worker failed: {type(exc).__name__}")
        except Exception:
            logger.exception("LANiS worker failed")
        return context.res.json(
            {"success": False, "error": "Worker execution failed"}, 500
        )


__all__ = ["main"]
