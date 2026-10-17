"""Scheduled DSBmobile snapshot Function.

Appwrite's UTC cron replaces the perpetual in-process scheduler used by the
local service.  Credentials are supplied as Function environment variables;
the successful plan payload is persisted in the Appwrite snapshots table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

try:
    from .backend import get_backend
except ImportError:  # pragma: no cover
    try:
        from src.backend import get_backend
    except ImportError:
        from backend import get_backend

from schulportal_hessen import SchulportalHessenAPI

logger = logging.getLogger("lanis.appwrite.dsb")


def _header(context: Any, name: str) -> str | None:
    headers = getattr(context.req, "headers", {}) or {}
    value = headers.get(name.lower()) if hasattr(headers, "get") else None
    return str(value) if value else None


def _schools() -> list[dict[str, str]]:
    configured = os.getenv("LANIS_DSB_SCHOOLS", "").strip()
    if configured:
        try:
            values = json.loads(configured)
        except ValueError as exc:
            raise ValueError("LANIS_DSB_SCHOOLS must be valid JSON") from exc
        if not isinstance(values, list):
            raise ValueError("LANIS_DSB_SCHOOLS must be a JSON list")
        return [
            {
                "school_id": str(item["school_id"]),
                "username": str(item["username"]),
                "password": str(item["password"]),
            }
            for item in values
            if isinstance(item, dict)
        ]
    school_id = os.getenv("DSB_SCHOOL_ID", "").strip()
    username = os.getenv("DSB_USERNAME", "").strip()
    password = os.getenv("DSB_PASSWORD", "").strip()
    return ([{"school_id": school_id, "username": username, "password": password}]
            if school_id and username and password else [])


async def _snapshot(backend: Any, item: dict[str, str]) -> dict[str, Any]:
    client = SchulportalHessenAPI()
    try:
        login = await asyncio.to_thread(client.dsb_login, item["username"], item["password"])
        if not isinstance(login, dict) or not login.get("success"):
            raise RuntimeError("DSBmobile login failed")
        urls = await asyncio.to_thread(client.dsb_get_plan_urls)
        if not isinstance(urls, dict) or not urls.get("success"):
            raise RuntimeError("DSBmobile plan URLs could not be fetched")
        plan = await asyncio.to_thread(client.dsb_get_substitution_plan)
    finally:
        client.close()
    if not isinstance(plan, dict) or not plan.get("success"):
        raise RuntimeError("DSBmobile substitution plan could not be fetched")
    # Keep the raw plan as returned by sph-client, but never persist DSB
    # credentials or the client session.
    entry_count = _entry_count(plan)
    await backend.snapshots.store(item["school_id"], plan, entry_count)
    return {"school_id": item["school_id"], "entry_count": entry_count}


def _entry_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for key in ("entries", "rows", "days", "tables", "data"):
            if key in value and isinstance(value[key], (list, dict)):
                return _entry_count(value[key])
        return sum(_entry_count(item) for item in value.values())
    return 0


async def main(context: Any) -> Any:
    """Appwrite scheduled Function entrypoint."""
    try:
        items = _schools()
        if not items:
            return context.res.json({"success": True, "snapshots": [], "skipped": True})
        backend = get_backend(dynamic_key=_header(context, "x-appwrite-key"))
        snapshots = []
        for item in items:
            snapshots.append(await _snapshot(backend, item))
        return context.res.json(
            {
                "success": True,
                "snapshots": snapshots,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    except Exception as exc:  # noqa: BLE001 - Appwrite must receive a safe 500
        try:
            context.error(f"LANiS DSB snapshot failed: {type(exc).__name__}")
        except Exception:
            logger.exception("LANiS DSB snapshot failed")
        return context.res.json(
            {"success": False, "error": "DSB snapshot failed"}, 500
        )


__all__ = ["main"]
