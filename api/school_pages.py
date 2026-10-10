"""Public, non-personalized content for school landing pages."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/schools", tags=["school pages"])

# Public branding content belongs here. Asset paths are served by the UI's
# static host; adding another school is a data-only change.
SCHOOL_PAGES: dict[str, dict[str, Any]] = {
    "5201": {
        "school_id": "5201",
        "name": "Adolf-Reichwein-Gymnasium",
        "city": "Heusenstamm",
        "short_name": "ARG",
        "login_url": "https://login.schulportal.hessen.de/?i=5201",
        "theme_color": "cyan",
        "palette": {
            "primary": "#00bcd5",
            "primary_dark": "#0099ae",
            "accent": "#69ddea",
        },
        "assets": {
            "logo": "/schools/5201/logo.png",
            "campus": {
                "xs": "/schools/5201/background-xs.jpg",
                "sm": "/schools/5201/background-sm.jpg",
                "md": "/schools/5201/background-md.jpg",
                "lg": "/schools/5201/background-lg.jpg",
            },
        },
    },
}


@router.get("/landing-pages")
async def list_school_landing_pages() -> dict[str, Any]:
    """List school IDs with configured landing page content."""
    schools = [
        {"school_id": profile["school_id"], "name": profile["name"], "city": profile["city"]}
        for profile in sorted(SCHOOL_PAGES.values(), key=lambda item: item["name"].casefold())
    ]
    return {"success": True, "schools": schools}


@router.get("/{school_id}/landing-page")
async def get_school_landing_page(school_id: str) -> dict[str, Any]:
    """Return the public branding and content for one configured school."""
    normalized_id = school_id.strip()
    if not re.fullmatch(r"\d{1,10}", normalized_id):
        raise HTTPException(status_code=422, detail="Invalid school ID")
    profile = SCHOOL_PAGES.get(normalized_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="School landing page not found")
    return {"success": True, "school": deepcopy(profile)}
