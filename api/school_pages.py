"""Public school data and proxied images loaded from Schulportal Hessen."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import os
import re
import time
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
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


@router.get("")
async def list_schools() -> dict[str, Any]:
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


async def _get_school_profile(school_id: str) -> dict[str, Any]:
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
    return deepcopy(profile)


@router.get("/{school_id}/data")
async def get_school_data(school_id: str, request: Request) -> dict[str, Any]:
    """Return school information with image URLs served by this backend."""
    profile = await _get_school_profile(school_id)
    base_url = os.getenv("PUBLIC_BASE_URL", str(request.base_url)).rstrip("/")
    image_base = f"{base_url}/schools/{profile['school_id']}"
    assets = profile["assets"]
    if assets["logo"] is not None:
        assets["logo"] = f"{image_base}/logo"
    assets["campus"] = {
        size: f"{image_base}/campus?size={size}" if url is not None else None
        for size, url in assets["campus"].items()
    }
    return {"success": True, "school": profile}


_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_CACHE_BYTES = 64 * 1024 * 1024
_MAX_IMAGE_CACHE_ENTRIES = 128
_image_locks = [asyncio.Lock() for _ in range(64)]


@dataclass
class _ImageEntry:
    expires_at: float
    content: bytes = b""
    media_type: str | None = None
    error_status: int | None = None
    error_detail: str | None = None


_image_cache: OrderedDict[str, _ImageEntry] = OrderedDict()


def _fetch_image(url: str) -> tuple[bytes, str]:
    """Download a bounded image, validating every redirect before following it."""
    try:
        for _ in range(6):
            if _asset_url(url) is None:
                raise HTTPException(status_code=502, detail="Invalid SPH image URL")
            with requests.get(url, timeout=(5, 15), stream=True, allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise HTTPException(status_code=502, detail="Invalid SPH image redirect")
                    url = urljoin(url, location)
                    continue
                if response.status_code == 404:
                    raise HTTPException(status_code=404, detail="School image not found")
                response.raise_for_status()
                media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if media_type not in {
                    "image/png", "image/jpeg", "image/gif", "image/webp",
                    "image/avif", "image/svg+xml", "image/x-icon", "image/vnd.microsoft.icon",
                }:
                    raise HTTPException(status_code=502, detail="SPH did not return an image")
                content = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if len(content) + len(chunk) > _MAX_IMAGE_BYTES:
                        raise HTTPException(status_code=502, detail="SPH image is too large")
                    content.extend(chunk)
                if not content:
                    raise HTTPException(status_code=502, detail="SPH returned an empty image")
                return bytes(content), media_type
        raise HTTPException(status_code=502, detail="Too many SPH image redirects")
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail="SPH image request timed out") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Could not load SPH image") from exc


def _cached_image(url: str) -> _ImageEntry | None:
    entry = _image_cache.get(url)
    if entry is None:
        return None
    if entry.expires_at <= time.monotonic():
        del _image_cache[url]
        return None
    _image_cache.move_to_end(url)
    if entry.error_status is not None:
        raise HTTPException(status_code=entry.error_status, detail=entry.error_detail)
    return entry


def _remember_image(url: str, entry: _ImageEntry) -> None:
    now = time.monotonic()
    for key in list(_image_cache):
        if _image_cache[key].expires_at <= now:
            del _image_cache[key]
    _image_cache[url] = entry
    _image_cache.move_to_end(url)
    while (
        len(_image_cache) > _MAX_IMAGE_CACHE_ENTRIES
        or sum(len(item.content) for item in _image_cache.values()) > _MAX_IMAGE_CACHE_BYTES
    ):
        _image_cache.popitem(last=False)


async def _serve_image(url: str | None) -> Response:
    if url is None:
        raise HTTPException(status_code=404, detail="School image not available")
    entry = _cached_image(url)
    if entry is None:
        async with _image_locks[hash(url) % len(_image_locks)]:
            entry = _cached_image(url)
            if entry is None:
                try:
                    async with _fetch_slots:
                        content, media_type = await run_in_threadpool(_fetch_image, url)
                except HTTPException as exc:
                    _remember_image(url, _ImageEntry(
                        time.monotonic() + _ERROR_CACHE_TTL,
                        error_status=exc.status_code, error_detail=exc.detail,
                    ))
                    raise
                entry = _ImageEntry(time.monotonic() + _CACHE_TTL, content, media_type)
                _remember_image(url, entry)
    max_age = max(0, int(entry.expires_at - time.monotonic()))
    return Response(content=entry.content, media_type=entry.media_type, headers={
        "Cache-Control": f"public, max-age={max_age}",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })


@router.get("/{school_id}/logo", response_class=Response,
            responses={200: {"content": {"image/png": {}, "image/jpeg": {}, "image/svg+xml": {}}}})
async def get_school_logo(school_id: str) -> Response:
    """Serve the school's SPH logo bytes through the backend."""
    profile = await _get_school_profile(school_id)
    return await _serve_image(profile["assets"]["logo"])


@router.get("/{school_id}/campus", response_class=Response,
            responses={200: {"content": {"image/jpeg": {}, "image/png": {}}}})
async def get_school_campus(
    school_id: str, size: Literal["xs", "sm", "md", "lg"] = "lg",
) -> Response:
    """Serve a school's SPH background image; default to the large variant."""
    profile = await _get_school_profile(school_id)
    return await _serve_image(profile["assets"]["campus"][size])
