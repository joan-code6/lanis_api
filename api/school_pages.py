"""Public school landing page data loaded from Schulportal Hessen."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool

from .school_locations import get_school_directory

router = APIRouter(prefix="/schools", tags=["school pages"])

_EXPORT_URL = "https://startcache.schulportal.hessen.de/exporteur.php"
_CACHE_TTL = 12 * 60 * 60
_ERROR_CACHE_TTL = 60
_MAX_CACHE_ENTRIES = 512
_fetch_slots = asyncio.Semaphore(4)
# Fixed lock stripes coalesce requests for the same ID without an unbounded
# lock registry. Network calls run off the event loop and are concurrency limited.
_refresh_locks = [asyncio.Lock() for _ in range(64)]


@dataclass
class _CacheEntry:
    expires_at: float
    profile: dict[str, Any] | None = None
    error_status: int | None = None
    error_detail: str | None = None


_profile_cache: OrderedDict[str, _CacheEntry] = OrderedDict()


def _asset_url(value: Any) -> str | None:
    """Accept HTTPS SPH assets, never executable URLs or unrelated hosts."""
    if not isinstance(value, str):
        return None
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not (host == "schulportal.hessen.de" or host.endswith(".schulportal.hessen.de"))
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return value


def _color(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?", value):
        return value
    return None


def _fetch_school_profile(school_id: str) -> dict[str, Any]:
    """Read SPH's public school exporter; no account or authentication needed."""
    try:
        with requests.get(
            _EXPORT_URL,
            params={"a": "school", "i": school_id},
            timeout=(5, 15),
        ) as response:
            if response.status_code == 404:
                raise HTTPException(status_code=404, detail="School not found")
            response.raise_for_status()
            payload = response.json()
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail="SPH school data request timed out") from exc
    except (requests.RequestException, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Could not load SPH school data") from exc

    if (
        not isinstance(payload, dict)
        or str(payload.get("Id", "")) != school_id
        or not isinstance(payload.get("Name"), str)
        or not payload["Name"].strip()
    ):
        raise HTTPException(status_code=502, detail="Invalid SPH school data")

    colors = payload.get("Farben")
    colors = colors if isinstance(colors, dict) else {}
    backgrounds = payload.get("bgimg")
    backgrounds = backgrounds if isinstance(backgrounds, dict) else {}
    campus: dict[str, str | None] = {}
    widths: dict[str, int | None] = {}
    for size in ("xs", "sm", "md", "lg"):
        image = backgrounds.get(size)
        image = image if isinstance(image, dict) else {}
        campus[size] = _asset_url(image.get("url"))
        width = image.get("px")
        widths[size] = (
            width if isinstance(width, int) and not isinstance(width, bool) and width > 0 else None
        )

    return {
        "school_id": school_id,
        "name": payload["Name"].strip(),
        "city": payload.get("Ort") if isinstance(payload.get("Ort"), str) else "",
        "short_name": payload.get("Kurzname") if isinstance(payload.get("Kurzname"), str) else "",
        "login_url": f"https://login.schulportal.hessen.de/?i={school_id}",
        "palette": {
            "primary": _color(colors.get("bg")),
            "primary_dark": _color(colors.get("border")),
            "accent": _color(colors.get("activeBG")),
            "text": _color(colors.get("text")),
            "active_text": _color(colors.get("activeText")),
            "footer": _color(colors.get("footer")),
            "heading": _color(colors.get("headtitle")),
        },
        "assets": {
            "logo": _asset_url(payload.get("Logo")),
            "stylesheet": _asset_url(payload.get("CSS")),
            "campus": campus,
            "campus_widths": widths,
        },
        "hint": payload.get("Hint"),
        "support_html": payload.get("Support") if isinstance(payload.get("Support"), str) else None,
        "last_modified": payload.get("LetzteAenderung"),
    }


def _cached_profile(school_id: str) -> dict[str, Any] | None:
    entry = _profile_cache.get(school_id)
    if entry is None:
        return None
    if entry.expires_at <= time.monotonic():
        del _profile_cache[school_id]
        return None
    _profile_cache.move_to_end(school_id)
    if entry.error_status is not None:
        raise HTTPException(status_code=entry.error_status, detail=entry.error_detail)
    return deepcopy(entry.profile)


def _remember(school_id: str, entry: _CacheEntry) -> None:
    _profile_cache[school_id] = entry
    _profile_cache.move_to_end(school_id)
    while len(_profile_cache) > _MAX_CACHE_ENTRIES:
        _profile_cache.popitem(last=False)


@router.get("/landing-pages")
async def list_school_landing_pages() -> dict[str, Any]:
    """List all schools from the shared, cached SPH school directory."""
    directory = await get_school_directory()
    if not directory:
        raise HTTPException(status_code=503, detail="SPH school directory is unavailable")
    schools = [
        {"school_id": school_id, "name": school["name"], "city": school["location"]}
        for school_id, school in directory.items()
    ]
    schools.sort(key=lambda school: (school["name"].casefold(), school["school_id"]))
    return {"success": True, "schools": schools}


@router.get("/{school_id}/landing-page")
async def get_school_landing_page(school_id: str) -> dict[str, Any]:
    """Load one school's public identity, colors, and assets directly from SPH."""
    normalized_id = school_id.strip()
    if not re.fullmatch(r"[0-9]{1,10}", normalized_id):
        raise HTTPException(status_code=422, detail="Invalid school ID")
    profile = _cached_profile(normalized_id)
    if profile is None:
        async with _refresh_locks[int(normalized_id) % len(_refresh_locks)]:
            profile = _cached_profile(normalized_id)
            if profile is None:
                try:
                    async with _fetch_slots:
                        profile = await run_in_threadpool(_fetch_school_profile, normalized_id)
                except HTTPException as exc:
                    _remember(normalized_id, _CacheEntry(
                        time.monotonic() + _ERROR_CACHE_TTL,
                        error_status=exc.status_code,
                        error_detail=exc.detail,
                    ))
                    raise
                _remember(normalized_id, _CacheEntry(time.monotonic() + _CACHE_TTL, profile))
    return {"success": True, "school": deepcopy(profile)}
