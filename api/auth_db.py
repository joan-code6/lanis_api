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
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiosqlite

from .identity import canonicalize_user_id

logger = logging.getLogger("auth_db")

DB_PATH = os.path.join("data", "auth.db")
REFRESH_TOKEN_TTL_DAYS = 90
DEFAULT_NOTIFICATION_PREFERENCES: Dict[str, Any] = {
    "enabled": False,
    "messages_enabled": True,
    "vertretungsplan_enabled": False,
    "vertretungsplan_class_mode": "own",
    "vertretungsplan_classes": [],
    "start_time": "07:00",
    "end_time": "21:00",
    "poll_interval_minutes": 15,
    "timezone": "Europe/Berlin",
    "show_preview": True,
}
DEFAULT_USER_PREFERENCES: Dict[str, Any] = {
    "appearance": {
        "theme_mode": "system",
        "theme_color": "cyan",
    },
    "dashboard": {
        "pinned_modules": [],
        "view_mode": "grid",
    },
    "timetable": {
        "view_mode": "rolling",
    },
    "onboarding": {
        "version": 0,
        "status": "not_started",
        "last_step": "welcome",
    },
}

_lock = asyncio.Lock()


def _canonical_user_id(user_id: str) -> str:
    """Return the stable key used by all user-owned auth data."""
    return canonicalize_user_id(user_id)


def _merge_nested_preferences(
    current: Dict[str, Any], updates: Dict[str, Any]
) -> Dict[str, Any]:
    """Recursively merge preference groups without mutating either input."""
    merged = json.loads(json.dumps(current))
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_nested_preferences(merged[key], value)
        else:
            merged[key] = value
    return merged


def _row_recency(row: Dict[str, Any]) -> tuple[str, int]:
    """Choose the newest row while keeping migrations deterministic."""
    timestamp = row.get("updated_at") or row.get("created_at") or ""
    return (str(timestamp), int(row["__rowid"]))


async def _canonicalize_refresh_token_user_ids(db: aiosqlite.Connection) -> int:
    """Move legacy case-sensitive refresh-token keys to canonical IDs."""
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT rowid AS __rowid, user_id FROM refresh_tokens"
    ) as cursor:
        rows = await cursor.fetchall()

    migrated = 0
    for row in rows:
        canonical_id = _canonical_user_id(row["user_id"])
        if canonical_id == row["user_id"]:
            continue
        await db.execute(
            "UPDATE refresh_tokens SET user_id = ? WHERE rowid = ?",
            (canonical_id, row["__rowid"]),
        )
        migrated += 1
    return migrated


async def _merge_user_id_table(
    db: aiosqlite.Connection,
    table: str,
    conflict_columns: tuple[str, ...],
) -> int:
    """Canonicalize a user-keyed table and merge any colliding rows.

    The table names are module constants, never user input. When legacy
    spellings collide, the newest row wins and all non-key data from that row
    is retained. This keeps the migration safe for notification preferences,
    overrides, and message state alike.
    """
    db.row_factory = aiosqlite.Row
    async with db.execute(f"SELECT rowid AS __rowid, * FROM {table}") as cursor:
        rows = [dict(row) for row in await cursor.fetchall()]

    groups: dict[tuple[Any, ...], list[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        canonical_id = _canonical_user_id(row["user_id"])
        group_key = tuple(
            canonical_id if column == "user_id" else row[column]
            for column in conflict_columns
        )
        groups[group_key].append(row)

    migrated = 0
    columns = [column for column in rows[0] if column != "__rowid"] if rows else []
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)

    for group_rows in groups.values():
        if not any(
            _canonical_user_id(row["user_id"]) != row["user_id"]
            for row in group_rows
        ):
            continue

        winner = max(group_rows, key=_row_recency)
        canonical_id = _canonical_user_id(winner["user_id"])

        for row in group_rows:
            await db.execute(
                f"DELETE FROM {table} WHERE rowid = ?", (row["__rowid"],)
            )

        values = [
            canonical_id if column == "user_id" else winner[column]
            for column in columns
        ]
        await db.execute(
            f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
            values,
        )
        migrated += len(group_rows)

    return migrated


async def _canonicalize_push_subscription_user_ids(
    db: aiosqlite.Connection,
) -> int:
    """Canonicalize push owners without changing the endpoint primary key."""
    db.row_factory = aiosqlite.Row
    async with db.execute(
        "SELECT rowid AS __rowid, user_id FROM push_subscriptions"
    ) as cursor:
        rows = await cursor.fetchall()

    migrated = 0
    for row in rows:
        canonical_id = _canonical_user_id(row["user_id"])
        if canonical_id == row["user_id"]:
            continue
        await db.execute(
            "UPDATE push_subscriptions SET user_id = ? WHERE rowid = ?",
            (canonical_id, row["__rowid"]),
        )
        migrated += 1
    return migrated


async def _migrate_user_ids(db: aiosqlite.Connection) -> None:
    """Merge case-sensitive user IDs left by older LANIS releases."""
    migrated = await _canonicalize_refresh_token_user_ids(db)
    for table, conflict_columns in (
        ("notification_preferences", ("user_id",)),
        ("custom_lessons", ("user_id", "lesson_date", "period")),
        ("class_link_overrides", ("user_id", "course_id")),
        ("message_notification_state", ("user_id",)),
        ("vertretungsplan_notification_state", ("user_id",)),
        ("user_preferences", ("user_id",)),
    ):
        migrated += await _merge_user_id_table(db, table, conflict_columns)
    migrated += await _canonicalize_push_subscription_user_ids(db)
    if migrated:
        logger.info("Canonicalized %s legacy user-identity rows in auth DB", migrated)


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
                messages_enabled INTEGER NOT NULL DEFAULT 1,
                vertretungsplan_enabled INTEGER NOT NULL DEFAULT 0,
                vertretungsplan_class_mode TEXT NOT NULL DEFAULT 'own',
                vertretungsplan_classes TEXT NOT NULL DEFAULT '[]',
                start_time TEXT NOT NULL DEFAULT '07:00',
                end_time TEXT NOT NULL DEFAULT '21:00',
                poll_interval_minutes INTEGER NOT NULL DEFAULT 15,
                timezone TEXT NOT NULL DEFAULT 'Europe/Berlin',
                show_preview INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        async with db.execute("PRAGMA table_info(notification_preferences)") as cursor:
            notification_columns = {row[1] for row in await cursor.fetchall()}
        for column, definition in (
            ("messages_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("vertretungsplan_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("vertretungsplan_class_mode", "TEXT NOT NULL DEFAULT 'own'"),
            ("vertretungsplan_classes", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            if column not in notification_columns:
                await db.execute(
                    f"ALTER TABLE notification_preferences ADD COLUMN {column} {definition}"
                )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_lessons (
                user_id TEXT NOT NULL,
                lesson_date TEXT NOT NULL,
                period TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                teacher TEXT,
                room TEXT,
                class_name TEXT,
                info TEXT,
                start_time TEXT,
                end_time TEXT,
                duration INTEGER NOT NULL DEFAULT 1,
                week_type TEXT,
                course_id TEXT,
                removed INTEGER NOT NULL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, lesson_date, period)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_custom_lessons_user_date
            ON custom_lessons(user_id, lesson_date)
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS class_link_overrides (
                user_id TEXT NOT NULL,
                course_id TEXT NOT NULL,
                url TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, course_id)
            )
            """
        )
        await db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_class_link_overrides_user_id
            ON class_link_overrides(user_id)
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
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS vertretungsplan_notification_state (
                user_id TEXT PRIMARY KEY,
                snapshot TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id TEXT PRIMARY KEY,
                preferences TEXT NOT NULL DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        await _migrate_user_ids(db)
        await db.commit()
    logger.info("Auth DB initialized at %s", DB_PATH)


async def store_refresh_token(
    user_id: str, school_id: str, username: str, password: str
) -> str:
    """
    Store a new refresh token and return the token string.

    Also cleans up expired tokens for this user.
    """
    user_id = _canonical_user_id(user_id)
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

            placeholders = ", ".join("?" for _ in user_ids)
            async with db.execute(
                f"SELECT DISTINCT user_id FROM refresh_tokens "
                f"WHERE user_id IN ({placeholders}) AND expires_at > ?",
                [*user_ids, now],
            ) as cursor:
                users_with_valid_sessions = {row[0] for row in await cursor.fetchall()}
            users_to_revoke = [
                user_id for user_id in user_ids if user_id not in users_with_valid_sessions
            ]

            await db.execute("DELETE FROM refresh_tokens WHERE expires_at <= ?", (now,))
            if users_to_revoke:
                revoke_placeholders = ", ".join("?" for _ in users_to_revoke)
                await db.execute(
                    f"DELETE FROM push_subscriptions WHERE user_id IN ({revoke_placeholders})",
                    users_to_revoke,
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
        await _revoke_expired_sessions()
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
    user_id = _canonical_user_id(user_id)
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
    user_id = _canonical_user_id(user_id)
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
    user_id = _canonical_user_id(user_id)
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM push_subscriptions WHERE user_id = ?", (user_id,)
            )
            await db.commit()


def _notification_preferences_from_row(row: Any) -> Dict[str, Any]:
    try:
        classes = json.loads(row["vertretungsplan_classes"] or "[]")
    except (json.JSONDecodeError, TypeError):
        classes = []
    if not isinstance(classes, list):
        classes = []
    return {
        "enabled": bool(row["enabled"]),
        "messages_enabled": bool(row["messages_enabled"]),
        "vertretungsplan_enabled": bool(row["vertretungsplan_enabled"]),
        "vertretungsplan_class_mode": row["vertretungsplan_class_mode"],
        "vertretungsplan_classes": [str(value) for value in classes],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "poll_interval_minutes": int(row["poll_interval_minutes"]),
        "timezone": row["timezone"],
        "show_preview": bool(row["show_preview"]),
    }


async def get_notification_preferences(user_id: str) -> Dict[str, Any]:
    """Return saved message-notification preferences or safe defaults."""
    user_id = _canonical_user_id(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT enabled, messages_enabled, vertretungsplan_enabled, "
            "vertretungsplan_class_mode, vertretungsplan_classes, start_time, "
            "end_time, poll_interval_minutes, timezone, show_preview "
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
    user_id = _canonical_user_id(user_id)
    values = {
        **DEFAULT_NOTIFICATION_PREFERENCES,
        **preferences,
    }
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT enabled, messages_enabled, vertretungsplan_enabled, "
                "vertretungsplan_class_mode, vertretungsplan_classes "
                "FROM notification_preferences WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
            was_enabled = bool(row[0]) if row else False
            previous_messages_enabled = bool(row[1]) if row else True
            previous_vertretungsplan_enabled = bool(row[2]) if row else False
            previous_vertretungsplan_scope = (
                (row[3], row[4]) if row else ("own", "[]")
            )
            classes_json = json.dumps(
                values["vertretungsplan_classes"], ensure_ascii=False, sort_keys=True
            )
            await db.execute(
                """
                INSERT INTO notification_preferences
                    (user_id, enabled, messages_enabled, vertretungsplan_enabled,
                     vertretungsplan_class_mode, vertretungsplan_classes, start_time,
                     end_time, poll_interval_minutes, timezone, show_preview, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    messages_enabled = excluded.messages_enabled,
                    vertretungsplan_enabled = excluded.vertretungsplan_enabled,
                    vertretungsplan_class_mode = excluded.vertretungsplan_class_mode,
                    vertretungsplan_classes = excluded.vertretungsplan_classes,
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
                    int(bool(values["messages_enabled"])),
                    int(bool(values["vertretungsplan_enabled"])),
                    values["vertretungsplan_class_mode"],
                    classes_json,
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
                await db.execute(
                    "DELETE FROM vertretungsplan_notification_state WHERE user_id = ?",
                    (user_id,),
                )
            elif values["messages_enabled"] and not previous_messages_enabled:
                await db.execute(
                    "DELETE FROM message_notification_state WHERE user_id = ?",
                    (user_id,),
                )
            next_vertretungsplan_scope = (
                values["vertretungsplan_class_mode"], classes_json
            )
            if (
                values["vertretungsplan_enabled"]
                and (
                    not previous_vertretungsplan_enabled
                    or next_vertretungsplan_scope != previous_vertretungsplan_scope
                )
            ):
                await db.execute(
                    "DELETE FROM vertretungsplan_notification_state WHERE user_id = ?",
                    (user_id,),
                )
            await db.commit()
    return await get_notification_preferences(user_id)


def _decode_user_preferences(serialized: object, user_id: str) -> Dict[str, Any]:
    try:
        stored = json.loads(str(serialized))
    except (TypeError, json.JSONDecodeError):
        logger.warning("Ignoring malformed preferences for user %s", user_id)
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    return _merge_nested_preferences(DEFAULT_USER_PREFERENCES, stored)


async def get_user_preferences(user_id: str) -> tuple[Dict[str, Any], bool]:
    """Return merged account preferences and whether a saved record exists."""
    user_id = _canonical_user_id(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT preferences FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return _merge_nested_preferences(DEFAULT_USER_PREFERENCES, {}), False
    return _decode_user_preferences(row[0], user_id), True


async def save_user_preferences(
    user_id: str, updates: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge and persist validated account preference groups."""
    user_id = _canonical_user_id(user_id)
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT preferences FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                row = await cursor.fetchone()
            current = _decode_user_preferences(row[0], user_id) if row else (
                _merge_nested_preferences(DEFAULT_USER_PREFERENCES, {})
            )
            merged = _merge_nested_preferences(current, updates)
            serialized = json.dumps(merged, ensure_ascii=False, sort_keys=True)
            await db.execute(
                """
                INSERT INTO user_preferences (user_id, preferences, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    preferences = excluded.preferences,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, serialized),
            )
            await db.commit()
    return merged


def _custom_lesson_from_row(row: Any) -> Dict[str, Any]:
    """Convert a persisted lesson override into the public API shape."""
    return {
        "date": row["lesson_date"],
        "period": row["period"],
        "subject": row["subject"] or "",
        "teacher": row["teacher"] or "",
        "room": row["room"] or "",
        "class_name": row["class_name"] or "",
        "info": row["info"] or "",
        "start_time": row["start_time"] or "",
        "end_time": row["end_time"] or "",
        "duration": int(row["duration"] or 1),
        "week_type": row["week_type"] or None,
        "course_id": row["course_id"] or None,
        "removed": bool(row["removed"]),
        "is_custom": True,
    }


async def get_custom_lessons(user_id: str) -> List[Dict[str, Any]]:
    """Return all lesson overrides for one account."""
    user_id = _canonical_user_id(user_id)
    if not os.path.exists(DB_PATH):
        return []
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT lesson_date, period, subject, teacher, room, class_name,
                       info, start_time, end_time, duration, week_type, course_id,
                       removed
                FROM custom_lessons
                WHERE user_id = ?
                ORDER BY lesson_date, period
                """,
                (user_id,),
            ) as cursor:
                rows = await cursor.fetchall()
    except aiosqlite.OperationalError as error:
        if "no such table" not in str(error).lower():
            raise
        # Keep old test databases and rolling deployments readable until the
        # startup migration has created the optional customization tables.
        return []
    return [_custom_lesson_from_row(row) for row in rows]


async def save_custom_lesson(
    user_id: str, lesson: Dict[str, Any]
) -> Dict[str, Any]:
    """Insert or replace one date/period lesson override."""
    user_id = _canonical_user_id(user_id)
    values = {
        "date": str(lesson.get("date") or ""),
        "period": str(lesson.get("period") or ""),
        "subject": str(lesson.get("subject") or ""),
        "teacher": str(lesson.get("teacher") or "") or None,
        "room": str(lesson.get("room") or "") or None,
        "class_name": str(lesson.get("class_name") or "") or None,
        "info": str(lesson.get("info") or "") or None,
        "start_time": str(lesson.get("start_time") or "") or None,
        "end_time": str(lesson.get("end_time") or "") or None,
        "duration": int(lesson.get("duration") or 1),
        "week_type": str(lesson.get("week_type") or "") or None,
        "course_id": str(lesson.get("course_id") or "") or None,
        "removed": int(bool(lesson.get("removed"))),
    }
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO custom_lessons
                    (user_id, lesson_date, period, subject, teacher, room,
                     class_name, info, start_time, end_time, duration,
                     week_type, course_id, removed, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, lesson_date, period) DO UPDATE SET
                    subject = excluded.subject,
                    teacher = excluded.teacher,
                    room = excluded.room,
                    class_name = excluded.class_name,
                    info = excluded.info,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    duration = excluded.duration,
                    week_type = excluded.week_type,
                    course_id = excluded.course_id,
                    removed = excluded.removed,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    user_id,
                    values["date"],
                    values["period"],
                    values["subject"],
                    values["teacher"],
                    values["room"],
                    values["class_name"],
                    values["info"],
                    values["start_time"],
                    values["end_time"],
                    values["duration"],
                    values["week_type"],
                    values["course_id"],
                    values["removed"],
                ),
            )
            await db.commit()
    return {
        "date": values["date"],
        "period": values["period"],
        "subject": values["subject"],
        "teacher": values["teacher"] or "",
        "room": values["room"] or "",
        "class_name": values["class_name"] or "",
        "info": values["info"] or "",
        "start_time": values["start_time"] or "",
        "end_time": values["end_time"] or "",
        "duration": values["duration"],
        "week_type": values["week_type"],
        "course_id": values["course_id"],
        "removed": bool(values["removed"]),
        "is_custom": True,
    }


async def delete_custom_lesson(
    user_id: str, lesson_date: str, period: str
) -> None:
    """Remove one lesson override and restore the portal lesson, if any."""
    user_id = _canonical_user_id(user_id)
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM custom_lessons WHERE user_id = ? AND lesson_date = ? AND period = ?",
                (user_id, lesson_date, period),
            )
            await db.commit()


async def get_class_link_overrides(user_id: str) -> Dict[str, str]:
    """Return saved class-link values, including intentionally blank links."""
    user_id = _canonical_user_id(user_id)
    if not os.path.exists(DB_PATH):
        return {}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT course_id, url FROM class_link_overrides WHERE user_id = ?",
                (user_id,),
            ) as cursor:
                rows = await cursor.fetchall()
    except aiosqlite.OperationalError as error:
        if "no such table" not in str(error).lower():
            raise
        return {}
    return {str(row[0]): str(row[1] or "") for row in rows}


async def save_class_link(
    user_id: str, course_id: str, url: str
) -> Dict[str, Any]:
    """Persist one class-link override for an account."""
    user_id = _canonical_user_id(user_id)
    course_id = str(course_id).strip()
    url = str(url or "").strip()
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO class_link_overrides (user_id, course_id, url, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, course_id) DO UPDATE SET
                    url = excluded.url,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, course_id, url),
            )
            await db.commit()
    return {"course_id": course_id, "url": url, "overridden": True}


async def delete_class_link(user_id: str, course_id: str) -> None:
    """Remove a class-link override and restore the portal value."""
    user_id = _canonical_user_id(user_id)
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM class_link_overrides WHERE user_id = ? AND course_id = ?",
                (user_id, str(course_id).strip()),
            )
            await db.commit()


async def get_enabled_notification_users() -> List[Dict[str, Any]]:
    """Return notification-enabled users with a valid refresh token."""
    await _revoke_expired_sessions()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT p.user_id, p.enabled, p.start_time, p.end_time,
                   p.poll_interval_minutes, p.timezone, p.show_preview,
                   p.messages_enabled, p.vertretungsplan_enabled,
                   p.vertretungsplan_class_mode, p.vertretungsplan_classes
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
    user_id = _canonical_user_id(user_id)
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
    user_id = _canonical_user_id(user_id)
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
    user_id = _canonical_user_id(user_id)
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "DELETE FROM push_subscriptions WHERE user_id = ? AND endpoint = ?",
                (user_id, endpoint),
            )
            await db.commit()


async def get_message_notification_state(user_id: str) -> Optional[Dict[str, Any]]:
    """Return the last persisted message snapshot for a user."""
    user_id = _canonical_user_id(user_id)
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
    user_id = _canonical_user_id(user_id)
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


async def get_vertretungsplan_notification_state(
    user_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the last persisted Vertretungsplan snapshot for a user."""
    user_id = _canonical_user_id(user_id)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT snapshot FROM vertretungsplan_notification_state WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return json.loads(row[0]) if row else None


async def save_vertretungsplan_notification_state(
    user_id: str, snapshot: Dict[str, Any]
) -> None:
    """Persist the Vertretungsplan snapshot used for change detection."""
    user_id = _canonical_user_id(user_id)
    async with _lock:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """
                INSERT INTO vertretungsplan_notification_state
                    (user_id, snapshot, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    snapshot = excluded.snapshot,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (user_id, json.dumps(snapshot, ensure_ascii=False, sort_keys=True)),
            )
            await db.commit()
