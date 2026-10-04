"""Authenticated file downloads for Appwrite Functions.

The published ``sph-client`` package does not expose the file-download helper
in every release, so this module falls back to its authenticated requests
session when the convenience method is unavailable.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit


def _filename(headers: Any, url: str) -> str:
    disposition = str(getattr(headers, "get", lambda _key, _default=None: "")(
        "Content-Disposition", ""
    ) or "")
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
    if encoded:
        return unquote(encoded.group(1).strip().strip('"')) or "download"
    plain = re.search(r"filename\s*=\s*\"?([^\";]+)", disposition, re.IGNORECASE)
    if plain:
        return plain.group(1).strip() or "download"
    path_name = urlsplit(url).path.rsplit("/", 1)[-1]
    return unquote(path_name) or "download"


def download_course_file(client: Any, url: str) -> dict[str, Any]:
    """Download a course attachment using the logged-in SPH session."""

    convenience = getattr(client, "meinunterricht_download_file", None)
    if callable(convenience):
        return convenience(url)
    if not getattr(client, "logged_in", False):
        return {"success": False, "error": "Not logged in"}

    base_url = str(getattr(client, "BASE_START_URL", "")).rstrip("/") + "/"
    download_url = url if urlsplit(url).scheme in {"http", "https"} else urljoin(base_url, url)
    response = client.session.get(download_url)
    response.raise_for_status()
    return {
        "success": True,
        "filename": _filename(response.headers, download_url),
        "content_type": response.headers.get("Content-Type"),
        "content": response.content,
        "url": download_url,
    }


__all__ = ["download_course_file"]
