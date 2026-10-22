"""LANIS unread state preserved when background synchronization opens portal messages."""

import copy
import hashlib
import json

import aiosqlite

from . import auth_db


def conversation_id(row):
    """Accept identifiers emitted by supported Schulportal header formats."""
    return str(
        next(
            (
                row[key]
                for key in ("id", "Uniquid", "uniqid", "Id")
                if row.get(key) is not None
            ),
            "",
        )
    )


def signature(row):
    """Identify message activity independently of upstream read-state changes."""
    data = {key: value for key, value in row.items() if key not in {"read", "unread"}}
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()


def is_unread(row):
    """Normalize boolean and numeric portal unread values."""
    value = row.get("unread", not row.get("read", True))
    return str(value).lower() in {"true", "1", "yes"}


async def overlay_headers(user_id, result, *, preserve=False, fresh=True):
    """Preserve unread messages before sync and merge state into all header reads."""
    if not isinstance(result, dict) or not result.get("success"):
        return result
    result = copy.deepcopy(result)
    async with auth_db._lock, aiosqlite.connect(auth_db.DB_PATH) as db:
        rows = await (
            await db.execute(
                "SELECT conversation_id, signature, unread FROM message_read_overrides WHERE user_id = ?",
                (user_id,),
            )
        ).fetchall()
        saved = {row[0]: (row[1], bool(row[2])) for row in rows}
        for row in result.get("conversations", []):
            identifier = conversation_id(row)
            if not identifier:
                continue
            activity = signature(row)
            unread = is_unread(row)
            previous = saved.get(identifier)
            if previous and (not fresh or previous[1] or previous[0] == activity):
                unread = previous[1]
            row["unread"] = int(unread)
            row["read"] = not unread
            # Keep the override tied to the latest activity shown to the user.
            # Otherwise opening a newer body clears only the old signature and
            # cached headers immediately make that conversation unread again.
            if fresh and (previous or (preserve and unread)):
                await db.execute(
                    "INSERT OR REPLACE INTO message_read_overrides VALUES (?, ?, ?, ?)",
                    (user_id, identifier, activity, int(unread)),
                )
        await db.commit()
    return result


async def current_activity(user_id, identifier):
    """Capture the activity eligible for acknowledgement before a live body read."""
    async with auth_db._lock, aiosqlite.connect(auth_db.DB_PATH) as db:
        row = await (
            await db.execute(
                "SELECT signature FROM message_read_overrides WHERE user_id = ? AND conversation_id = ?",
                (user_id, identifier),
            )
        ).fetchone()
    return row[0] if row else None


async def mark_opened(user_id, identifier, *, expected_activity=...):
    """Clear a preserved unread flag only for an interactive LANIS read."""
    async with auth_db._lock, aiosqlite.connect(auth_db.DB_PATH) as db:
        sql = "UPDATE message_read_overrides SET unread = 0 WHERE user_id = ? AND conversation_id = ?"
        params = (user_id, identifier)
        if expected_activity is not ...:
            sql += " AND signature = ?"
            params += (expected_activity,)
        await db.execute(sql, params)
        await db.commit()
