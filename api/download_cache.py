"""Authenticated binary responses stored in the existing shared file cache."""

import hashlib
import json
import os
import tempfile
from email.message import Message
from pathlib import Path

import aiosqlite
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from . import auth_db, file_cache

DOWNLOAD_ROUTES = {
    "/dateispeicher/file/{file_id}",
    "/dateiverteilung/file",
    "/meinunterricht/submissions/file/{file_ref}",
}
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024


def response_filename(response):
    """Read a safe display filename from the original attachment response."""
    message = Message()
    message["Content-Disposition"] = response.headers.get("content-disposition", "")
    return message.get_filename() or "download"


def install_file(path, file_hash, media_type, filename):
    """Atomically install content-addressed bytes and metadata under the auth lock."""
    destination = file_cache.get_content_path(file_hash)
    if destination.exists():
        path.unlink()
    else:
        os.replace(path, destination)
    meta = destination.with_name(file_hash + ".meta")
    temporary = meta.with_name(meta.name + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "download_url": None,
                "content_type": media_type,
                "filename": file_cache._sanitize_filename(filename) or "download",
            }
        )
    )
    os.replace(temporary, meta)


async def lookup(user_id, request_key):
    """Resolve only files authorized for this user, regardless of blob deduplication."""
    async with auth_db._lock, aiosqlite.connect(auth_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute(
                "SELECT d.* FROM cached_downloads d JOIN cached_file_references r "
                "ON d.user_id = r.user_id AND d.file_hash = r.file_hash "
                "WHERE d.user_id = ? AND d.request_key = ?",
                (user_id, request_key),
            )
        ).fetchone()
        return dict(row) if row else None


def wrap_download(original, fields):
    """Serve previously authorized files locally and cache successful downloads."""

    async def handler(request):
        from .api import local_auth_dependency
        from .outage_cache import normalized_query_params

        token = request.headers.get("X-Session-Token")
        if not token:
            return await original(request)
        identity = await local_auth_dependency(token)
        request.state.lanis_auth = identity
        request_key = json.dumps(
            [request.url.path, normalized_query_params(fields, request.query_params)]
        )
        from .daily_cache import warming

        cached = await lookup(identity.user_id, request_key)
        refresh = request.query_params.get("refresh", "").lower() in {
            "true",
            "1",
            "yes",
            "on",
        }
        if cached and not warming.get() and not refresh:
            path = file_cache.get_content_path(cached["file_hash"])
            if path.exists():
                return FileResponse(
                    path,
                    filename=cached["filename"],
                    media_type=cached["content_type"],
                    headers={
                        "Cache-Control": "private, no-store",
                        "X-LANIS-Cache": "hit",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
        response = await original(request)
        if response.status_code != 200:
            return response
        digest = hashlib.sha256(b"lanis-content-v1\0")
        size = 0
        await run_in_threadpool(file_cache._ensure_cache_dir)
        with tempfile.NamedTemporaryFile(
            delete=False, dir=file_cache.FILE_CACHE_DIR, prefix=".sync-"
        ) as temporary:
            path = Path(temporary.name)
        try:
            destination = await run_in_threadpool(path.open, "wb")
            try:

                async def write(chunk):
                    nonlocal size
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise HTTPException(
                            413, "File exceeds the 256 MiB download limit"
                        )
                    digest.update(chunk)
                    await run_in_threadpool(destination.write, chunk)

                if hasattr(response, "body_iterator"):
                    async for chunk in response.body_iterator:
                        await write(chunk.encode() if isinstance(chunk, str) else chunk)
                else:
                    await write(response.body)
            finally:
                await run_in_threadpool(destination.close)
            # Revocation during a download must prevent durable authorization.
            await local_auth_dependency(token)
            file_hash = digest.hexdigest()
            filename = response_filename(response)
            media_type = response.headers.get(
                "content-type", "application/octet-stream"
            )
            async with auth_db._lock, aiosqlite.connect(auth_db.DB_PATH) as db:
                await db.execute("BEGIN IMMEDIATE")
                await run_in_threadpool(
                    install_file, path, file_hash, media_type, filename
                )
                await db.execute(
                    "INSERT OR IGNORE INTO cached_file_references (user_id, file_hash) VALUES (?, ?)",
                    (identity.user_id, file_hash),
                )
                previous = await (
                    await db.execute(
                        "SELECT file_hash FROM cached_downloads WHERE user_id = ? AND request_key = ?",
                        (identity.user_id, request_key),
                    )
                ).fetchone()
                await db.execute(
                    "INSERT OR REPLACE INTO cached_downloads VALUES (?, ?, ?, ?, ?)",
                    (identity.user_id, request_key, file_hash, filename, media_type),
                )
                if previous and previous[0] != file_hash:
                    await db.execute(
                        "DELETE FROM cached_file_references WHERE user_id = ? AND file_hash = ? "
                        "AND NOT EXISTS (SELECT 1 FROM cached_downloads WHERE user_id = ? AND file_hash = ?)",
                        (identity.user_id, previous[0], identity.user_id, previous[0]),
                    )
                await db.commit()
            return FileResponse(
                file_cache.get_content_path(file_hash),
                filename=filename,
                media_type=media_type,
                headers={
                    "Cache-Control": "private, no-store",
                    "X-LANIS-Cache": "fresh",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        finally:
            await run_in_threadpool(path.unlink, missing_ok=True)
            if response.background:
                await response.background()

    return handler
