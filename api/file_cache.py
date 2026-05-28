"""
File cache for downloaded course attachments.

Files are stored in data/files/ keyed by SHA-256 hash of their download URL.
Hash-based deduplication ensures two students in the same class don't store
the same file twice.
"""

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger("file_cache")

FILE_CACHE_DIR = Path(__file__).parent.parent / "data" / "files"

_pending_downloads: set[str] = set()


def _sanitize_filename(filename: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", filename)


def _ensure_cache_dir() -> None:
    FILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_file_hash(download_url: str) -> str:
    return hashlib.sha256(download_url.encode()).hexdigest()


def _content_path(file_hash: str) -> Path:
    _ensure_cache_dir()
    return FILE_CACHE_DIR / file_hash


def _meta_path(file_hash: str) -> Path:
    _ensure_cache_dir()
    return FILE_CACHE_DIR / f"{file_hash}.meta"


def is_file_cached(file_hash: str) -> bool:
    return _content_path(file_hash).exists()


def is_file_pending(file_hash: str) -> bool:
    return file_hash in _pending_downloads


def mark_pending(file_hash: str) -> None:
    _pending_downloads.add(file_hash)


def unmark_pending(file_hash: str) -> None:
    _pending_downloads.discard(file_hash)


def write_pending_meta(file_hash: str, download_url: str) -> None:
    """Write placeholder metadata before the download starts, so the file
    endpoint can attempt a synchronous download later."""
    _ensure_cache_dir()
    _meta_path(file_hash).write_text(
        json.dumps(
            {
                "download_url": download_url,
                "content_type": None,
                "filename": None,
            }
        )
    )


def save_file(file_hash: str, content: bytes, content_type: str, filename: str) -> None:
    _ensure_cache_dir()
    _content_path(file_hash).write_bytes(content)
    _meta_path(file_hash).write_text(
        json.dumps(
            {
                "download_url": None,
                "content_type": content_type or "application/octet-stream",
                "filename": _sanitize_filename(filename) or "download",
            }
        )
    )
    unmark_pending(file_hash)
    logger.info("Cached file  %s  %s", file_hash[:12], filename)


def get_meta(file_hash: str) -> Optional[dict]:
    mp = _meta_path(file_hash)
    if mp.exists():
        return json.loads(mp.read_text())
    return None


def get_content_path(file_hash: str) -> Path:
    return _content_path(file_hash)
