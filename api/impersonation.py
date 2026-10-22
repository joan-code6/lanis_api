"""Shared storage for the private broker and public, restricted session exchange."""

from __future__ import annotations

import hashlib
import os
import secrets
import time

import aiosqlite
from fastapi import HTTPException

from . import auth_db
from .admin import _configured_accounts
from .identity import canonicalize_user_id

LAUNCH_SECONDS = 45
SESSION_SECONDS = 15 * 60
# Explicit allowlist: new routes are unavailable until reviewed for side effects.
READ_PATHS = {
    "/apps",
    "/modules",
    "/benutzer",
    "/kalender",
    "/kalender/events",
    "/vertretungsplan",
    "/vertretungsplan/options",
    "/stundenplan",
    "/stundenplan/view",
    "/dsb/school-plan",
    "/settings/preferences",
    "/settings/timetable/lessons",
    "/settings/class-links",
    "/lerngruppen",
    "/meinunterricht",
    "/meinunterricht/attendance",
    "/meinunterricht/entry",
    "/meinunterricht/weekly",
    "/dateispeicher",
    "/dateispeicher/search",
    "/dateiverteilung",
    "/nachrichten/headers",
    "/notifications/config",
    "/notifications/preferences",
    "/notifications/vertretungsplan/options",
    "/cache/status",
}


def enabled() -> bool:
    return bool(os.getenv("LANIS_IMPERSONATION_SOCKET", "").strip())


def permitted(method: str, path: str) -> bool:
    return (method == "POST" and path == "/impersonation/end") or (
        method == "GET"
        and (
            path in READ_PATHS
            or path.startswith(("/kalender/event/", "/meinunterricht/course/"))
        )
    )


async def _open():
    db = await aiosqlite.connect(auth_db.DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("""CREATE TABLE IF NOT EXISTS impersonation_sessions (
        id TEXT PRIMARY KEY, code_hash TEXT UNIQUE, actor TEXT NOT NULL,
        target TEXT NOT NULL, parent_id TEXT NOT NULL, actor_session TEXT NOT NULL, launch_until REAL NOT NULL,
        expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)""")
    await db.commit()
    return db


async def create(actor: str, target: str, actor_session: str) -> dict:
    if not enabled():
        raise HTTPException(503, "Private impersonation service is not configured")
    target = canonicalize_user_id(target)
    if actor not in _configured_accounts():
        raise HTTPException(403, "Admin account is no longer enabled")
    if not await auth_db.admin_session_is_active(actor, actor_session):
        raise HTTPException(401, "Admin session is no longer active")
    db = await _open()
    try:
        now = time.time()
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "DELETE FROM impersonation_sessions WHERE expires_at <= ?", (now,)
        )
        # Metadata only. The broker never reads or decrypts an SPH password.
        row = await (
            await db.execute(
                "SELECT session_id FROM refresh_tokens WHERE user_id = ? AND julianday(expires_at) > julianday('now') ORDER BY created_at DESC LIMIT 1",
                (target,),
            )
        ).fetchone()
        if not row:
            raise HTTPException(409, "This account has no usable saved session")
        active = await (
            await db.execute(
                "SELECT count(*) FROM impersonation_sessions WHERE actor = ?",
                (actor,),
            )
        ).fetchone()
        if active[0] >= 5:
            raise HTTPException(429, "End an existing preview or wait for it to expire")
        code = secrets.token_urlsafe(32)
        await db.execute(
            "INSERT INTO impersonation_sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                secrets.token_urlsafe(32),
                hashlib.sha256(code.encode()).hexdigest(),
                actor,
                target,
                row["session_id"],
                actor_session,
                now + LAUNCH_SECONDS,
                now + SESSION_SECONDS,
            ),
        )
        await db.commit()
        return {
            "code": code,
            "expires_in": LAUNCH_SECONDS,
            "session_seconds": SESSION_SECONDS,
        }
    finally:
        await db.close()


async def consume(code: str) -> dict:
    db = await _open()
    try:
        await db.execute("BEGIN IMMEDIATE")
        row = await (
            await db.execute(
                "SELECT * FROM impersonation_sessions WHERE code_hash = ? AND consumed = 0 AND launch_until > ?",
                (hashlib.sha256(code.encode()).hexdigest(), time.time()),
            )
        ).fetchone()
        if not row:
            raise HTTPException(401, "Preview link has expired or was already used")
        await db.execute(
            "UPDATE impersonation_sessions SET consumed = 1, code_hash = NULL WHERE id = ?",
            (row["id"],),
        )
        await db.commit()
        return dict(row)
    finally:
        await db.close()


async def validate(session_id: str, target: str, parent_id: str) -> dict:
    if not enabled():
        raise HTTPException(401, "Impersonation is disabled")
    db = await _open()
    try:
        row = await (
            await db.execute(
                "SELECT * FROM impersonation_sessions WHERE id = ? AND target = ? AND parent_id = ? AND consumed = 1 AND expires_at > ?",
                (session_id, target, parent_id, time.time()),
            )
        ).fetchone()
        if not row or row["actor"] not in _configured_accounts():
            raise HTTPException(401, "Preview has ended or expired")
        if not await auth_db.admin_session_is_active(
            row["actor"], row["actor_session"]
        ):
            raise HTTPException(401, "Admin session is no longer active")
        return dict(row)
    finally:
        await db.close()


async def revoke(session_id: str) -> None:
    db = await _open()
    try:
        await db.execute(
            "DELETE FROM impersonation_sessions WHERE id = ?", (session_id,)
        )
        await db.commit()
    finally:
        await db.close()


async def purge_expired() -> None:
    db = await _open()
    try:
        await db.execute(
            "DELETE FROM impersonation_sessions WHERE expires_at <= ?", (time.time(),)
        )
        await db.commit()
    finally:
        await db.close()
