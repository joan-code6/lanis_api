"""
SQLite database for persistent refresh token storage.

Survives backend restarts so users don't lose their sessions
when the server process recycles.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiosqlite

logger = logging.getLogger("auth_db")

DB_PATH = os.path.join("data", "auth.db")
REFRESH_TOKEN_TTL_DAYS = 90
DEFAULT_NOTIFICATION_PREFERENCES: Dict[str, Any] = {
    "enabled": False,
    "start_time": "07:00",
    "end_time": "21:00",
    "poll_interval_minutes": 15,
    "timezone": "Europe/Berlin",
    "show_preview": True,
}

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
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_preferences (
                user_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                start_time TEXT NOT NULL DEFAULT '07:00',
                end_time TEXT NOT NULL DEFAULT '21:00',
                poll_interval_minutes INTEGER NOT NULL DEFAULT 15,
                timezone TEXT NOT NULL DEFAULT 'Europe/Berlin',
                show_preview INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user_id
            ON push_subscriptions(user_id)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS message_notification_state (
                user_id TEXT PRIMARY KEY,
                snapshot TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
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


async def _revoke_expired_sessions() -> None:
    """Remove expired sessions and their browser subscriptions together."""
    now = datetime.utcnow()
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT DISTINCT user_id FROM refresh_tokens WHERE expires_at <= ?",
                (now,),
            ) as cursor:
                user_ids = [row[0] for row in await cursor.fetchall()]
            if not user_ids:
                return

            await db.execute("DELETE FROM refresh_tokens WHERE expires_at <= ?", (now,))
            placeholders = ", ".join("?" for _ in user_ids)
            await db.execute(
                f"DELETE FROM push_subscriptions WHERE user_id IN ({placeholders})",
                user_ids,
            )
            await db.commit()


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
        await delete_user_push_subscriptions(row["user_id"])
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
    await _revoke_expired_sessions()
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
            await db.execute(
                "DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,)
            )
            await db.commit()


async def delete_user_push_subscriptions(user_id: str) -> None:
    """Remove all browser push subscriptions for a user on full logout."""
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,)
            )
            await db.commit()


def _notification_preferences_from_row(row: Any) -> Dict[str, Any]:
    return {
        "enabled": bool(row["enabled"]),
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "poll_interval_minutes": int(row["poll_interval_minutes"]),
        "timezone": row["timezone"],
        "show_preview": bool(row["show_preview"]),
    }


async def get_notification_preferences(user_id: str) -> Dict[str, Any]:
    """Return saved message-notification preferences or safe defaults."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT enabled, start_time, end_time, poll_interval_minutes, timezone, show_preview "
            "FROM notification_preferences WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return {"user_id": user_id, **DEFAULT_NOTIFICATION_PREFERENCES}
    return {"user_id": user_id, **_notification_preferences_from_row(row)}


async def save_notification_preferences(
    user_id: str, preferences: Dict[str, Any]
) -> Dict[str, Any]:
    """Persist and return one user's message-notification preferences."""
    values = {
        **DEFAULT_NOTIFICATION_PREFERENCES,
        **preferences,
    }
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT enabled FROM notification_preferences WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
            was_enabled = bool(row[0]) if row else False
            await db.execute(
                """
                INSERT INTO notification_preferences
                    (user_id, enabled, start_time, end_time, poll_interval_minutes, timezone, show_preview, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    poll_interval_minutes = excluded.poll_interval_minutes,
                    timezone = excluded.timezone,
                    show_preview = excluded.show_preview,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    int(bool(values["enabled"])),
                    values["start_time"],
                    values["end_time"],
                    int(values["poll_interval_minutes"]),
                    values["timezone"],
                    int(bool(values["show_preview"])),
                ),
            )
            if values["enabled"] and not was_enabled:
                await db.execute(
                    "DELETE FROM message_notification_state WHERE user_id = ?",
                    (user_id,),
                )
            await db.commit()
    return await get_notification_preferences(user_id)


async def get_enabled_notification_users() -> List[Dict[str, Any]]:
    """Return notification-enabled users with a valid refresh token."""
    await _revoke_expired_sessions()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT p.user_id, p.enabled, p.start_time, p.end_time,
                   p.poll_interval_minutes, p.timezone, p.show_preview
            FROM notification_preferences p
            WHERE p.enabled = 1
              AND EXISTS (
                  SELECT 1 FROM refresh_tokens r
                  WHERE r.user_id = p.user_id AND r.expires_at > ?
              )
            """,
            (datetime.utcnow(),),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {"user_id": row["user_id"], **_notification_preferences_from_row(row)}
        for row in rows
    ]


async def save_push_subscription(user_id: str, subscription: Dict[str, Any]) -> None:
    """Associate a browser push subscription with a user."""
    keys = subscription.get("keys") or {}
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO push_subscriptions
                    (endpoint, user_id, p256dh, auth, created_at, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(endpoint) DO UPDATE SET
                    user_id = excluded.user_id,
                    p256dh = excluded.p256dh,
                    auth = excluded.auth,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    subscription["endpoint"],
                    user_id,
                    keys["p256dh"],
                    keys["auth"],
                ),
            )
            await db.commit()


async def get_push_subscriptions(user_id: str) -> List[Dict[str, Any]]:
    """Return all browser push subscriptions belonging to a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            rows = await cursor.fetchall()
    return [
        {
            "endpoint": row["endpoint"],
            "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
        }
        for row in rows
    ]


async def delete_push_subscription(user_id: str, endpoint: str) -> None:
    """Remove one browser push subscription."""
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
                (user_id, endpoint),
            )
            await db.commit()


async def get_message_notification_state(user_id: str) -> Optional[Dict[str, Any]]:
    """Return the last persisted message snapshot for a user."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT snapshot FROM message_notification_state WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return json.loads(row[0]) if row else None


async def save_message_notification_state(
    user_id: str, snapshot: Dict[str, Any]
) -> None:
    """Persist the last message snapshot used for change detection."""
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO message_notification_state (user_id, snapshot, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    snapshot = excluded.snapshot,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True)),
            )
            await db.commit()
