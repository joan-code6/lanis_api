"""
File cache for downloaded course attachments.

Files are stored in data/files/ keyed by SHA-256 hash of their download URL.
Hash-based deduplication ensures two students in the same class don't store
the same file twice.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

logger = logging.getLogger("file_cache")

FILE_CACHE_DIR = Path(__file__).parent.parent / "data" / "files"
# Shared attachment files are not user-owned. They are retained for 30 days,
# then removed by the periodic cleanup job to limit stale personal content.
FILE_CACHE_RETENTION_SECONDS = 30 * 24 * 60 * 60

_pending_downloads: set[str] = set()
_staged_cleanup_tasks: set[asyncio.Task] = set()


def _sanitize_filename(filename: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", filename)


def _ensure_cache_dir() -> None:
    FILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def get_file_hash(download_url: str) -> str:
    return hashlib.sha256(download_url.encode()).hexdigest()


def delete_files(file_hashes: list[str]) -> int:
    """Delete cached files after their final owning account is removed."""
    deleted = 0
    for file_hash in set(file_hashes):
        if not re.fullmatch(r"[a-f0-9]{64}", file_hash):
            raise ValueError("Invalid cached file hash")
        for path in (_content_path(file_hash), _meta_path(file_hash)):
            if path.exists():
                path.unlink()
                deleted += 1
        unmark_pending(file_hash)
    return deleted


def stage_files_for_deletion(file_hashes: list[str]) -> list[Path]:
    """Move unreferenced cache files out of the live namespace quickly.

    Callers must hold the database write lock while checking references and
    staging these paths. Unlinking staged files can then happen after releasing
    that lock without risking deletion of a newly shared live cache entry.
    """
    staged: list[tuple[Path, Path]] = []
    try:
        for file_hash in set(file_hashes):
            if not re.fullmatch(r"[a-f0-9]{64}", file_hash):
                raise ValueError("Invalid cached file hash")
            for path in (_content_path(file_hash), _meta_path(file_hash)):
                if not path.exists():
                    continue
                staged_path = path.with_name(
                    f".{file_hash}.{uuid.uuid4().hex}.deleting"
                )
                os.replace(path, staged_path)
                staged.append((staged_path, path))
    except Exception:
        for staged_path, original_path in reversed(staged):
            try:
                os.replace(staged_path, original_path)
            except OSError:
                logger.exception(
                    "Could not restore staged cached file %s", original_path
                )
        raise
    return [staged_path for staged_path, _ in staged]


def delete_staged_files(staged_paths: list[Path]) -> int:
    """Unlink paths already removed from the live cache namespace."""
    deleted = 0
    for path in staged_paths:
        try:
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            continue
    return deleted


def purge_staged_files() -> int:
    """Remove staged attachment files left by an interrupted deletion."""
    if not FILE_CACHE_DIR.exists():
        return 0
    deleted = 0
    staged_name = re.compile(r"\.[a-f0-9]{64}\.[a-f0-9]{32}\.deleting\Z")
    for path in FILE_CACHE_DIR.iterdir():
        if not staged_name.fullmatch(path.name):
            continue
        try:
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning(
                "Could not remove staged cached file %s", path, exc_info=True
            )
    return deleted


async def _retry_staged_file_cleanup(staged_paths: list[Path]) -> None:
    for attempt in range(3):
        await asyncio.sleep(0.5 * (attempt + 1))
        try:
            await asyncio.to_thread(delete_staged_files, staged_paths)
            return
        except OSError:
            logger.warning(
                "Retry %s failed to remove staged cached files",
                attempt + 1,
                exc_info=True,
            )


def schedule_staged_file_cleanup_retry(staged_paths: list[Path]) -> None:
    """Retry failed unlinks soon; staged names also allow startup recovery."""
    task = asyncio.create_task(_retry_staged_file_cleanup(staged_paths))
    _staged_cleanup_tasks.add(task)
    task.add_done_callback(_staged_cleanup_tasks.discard)


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
    """Keep the upstream URL without hiding an already usable offline copy."""
    _ensure_cache_dir()
    existing = get_meta(file_hash) if is_file_cached(file_hash) else None
    metadata = existing or {"content_type": None, "filename": None}
    metadata["download_url"] = download_url
    temporary = _meta_path(file_hash).with_name(f".{file_hash}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(metadata))
        os.replace(temporary, _meta_path(file_hash))
    finally:
        temporary.unlink(missing_ok=True)


def save_file(file_hash: str, content: bytes, content_type: str, filename: str) -> None:
    """Atomically replace shared bytes so in-flight readers retain a complete copy."""
    _ensure_cache_dir()
    content_temp = _content_path(file_hash).with_name(
        f".{file_hash}.{uuid.uuid4().hex}.tmp"
    )
    meta_temp = _meta_path(file_hash).with_name(f".{file_hash}.{uuid.uuid4().hex}.tmp")
    try:
        content_temp.write_bytes(content)
        meta_temp.write_text(
            json.dumps(
                {
                    "download_url": None,
                    "content_type": content_type or "application/octet-stream",
                    "filename": _sanitize_filename(filename or "download")
                    or "download",
                }
            )
        )
        os.replace(content_temp, _content_path(file_hash))
        os.replace(meta_temp, _meta_path(file_hash))
    finally:
        content_temp.unlink(missing_ok=True)
        meta_temp.unlink(missing_ok=True)
    unmark_pending(file_hash)
    logger.info("Cached file %s", file_hash[:12])


def get_meta(file_hash: str) -> dict | None:
    mp = _meta_path(file_hash)
    if mp.exists():
        return json.loads(mp.read_text())
    return None


def get_content_path(file_hash: str) -> Path:
    return _content_path(file_hash)


def purge_expired_files(protected_hashes: set[str] | None = None) -> int:
    """Delete shared cache entries older than the documented retention period."""
    if not FILE_CACHE_DIR.exists():
        return 0
    cutoff = time.time() - FILE_CACHE_RETENTION_SECONDS
    deleted = purge_staged_files()
    for path in FILE_CACHE_DIR.iterdir():
        try:
            if (
                path.name.split(".meta", 1)[0] in (protected_hashes or set())
                or path.name.split(".meta", 1)[0] in _pending_downloads
                or path.stat().st_mtime >= cutoff
            ):
                continue
            path.unlink()
            deleted += 1
        except OSError:
            logger.warning(
                "Could not remove expired cached file %s", path, exc_info=True
            )
    return deleted
