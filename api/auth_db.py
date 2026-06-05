"""
SQLite database for persistent refresh token storage.

Survives backend restarts so users don't lose their sessions
when the server process recycles.
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite

logger = logging.getLogger("auth_db")

DB_PATH = os.path.join("data", "auth.db")
REFRESH_TOKEN_TTL_DAYS = 90

_lock = asyncio.Lock()


async def initialize() -> None:
    """Create the database and tables if they don't exist."""
    os.makedirs("data", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                school_id TEXT NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id
            ON refresh_tokens(user_id)
            """
        )
        await db.commit()
    logger.info("Auth DB initialized at %s", DB_PATH)


async def store_refresh_token(
    user_id: str, school_id: str, username: str, password: str
) -> str:
    """
    Store a new refresh token and return the token string.

    Also cleans up expired tokens for this user.
    """
    token = uuid.uuid4().hex
    expires_at = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_TTL_DAYS)

    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM refresh_tokens WHERE user_id = ? AND expires_at < ?",
                (user_id, datetime.utcnow()),
            )
            await db.execute(
                "INSERT INTO refresh_tokens (token, user_id, school_id, username, password, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token, user_id, school_id, username, password, expires_at),
            )
            await db.commit()

    return token


async def get_refresh_token(token: str) -> Optional[dict]:
    """
    Look up a refresh token and return its data if valid.
    Returns None if not found or expired.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM refresh_tokens WHERE token = ?", (token,)
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return None

    expires_at = datetime.fromisoformat(row["expires_at"])
    if datetime.utcnow() > expires_at:
        await delete_refresh_token(token)
        return None

    return {
        "token": row["token"],
        "user_id": row["user_id"],
        "school_id": row["school_id"],
        "username": row["username"],
        "password": row["password"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


async def get_refresh_token_by_user_id(user_id: str) -> Optional[dict]:
    """
    Look up the most recent valid refresh token for a user.
    Used to re-establish Schulportal session after backend restart.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM refresh_tokens WHERE user_id = ? AND expires_at > ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, datetime.utcnow()),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return None

    return {
        "token": row["token"],
        "user_id": row["user_id"],
        "school_id": row["school_id"],
        "username": row["username"],
        "password": row["password"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
    }


async def delete_refresh_token(token: str) -> None:
    """Delete a specific refresh token (used on logout)."""
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM refresh_tokens WHERE token = ?", (token,))
            await db.commit()


async def delete_user_tokens(user_id: str) -> None:
    """Delete all refresh tokens for a user (used on full logout)."""
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,)
            )
            await db.commit()
