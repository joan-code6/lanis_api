"""Minimal account-backed preferences for self-service Android beta recruitment."""

import os
import time

import aiosqlite

from . import auth_db

SCHEMA = """CREATE TABLE IF NOT EXISTS android_beta_preferences (
 user_id TEXT PRIMARY KEY, dismissed INTEGER NOT NULL DEFAULT 0,
 prompt_after INTEGER NOT NULL DEFAULT 0
);"""


async def migrate_legacy(db):
    """Preserve recruitment preferences and erase the superseded email registry."""
    legacy = await (
        await db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='android_beta'"
        )
    ).fetchone()
    if legacy:
        await db.execute(
            "INSERT OR IGNORE INTO android_beta_preferences(user_id,dismissed,prompt_after) SELECT user_id,dismissed,prompt_after FROM android_beta"
        )
        await db.execute("DROP TABLE android_beta")


def config():
    return {
        "available": os.getenv("ANDROID_BETA_ENABLED", "").lower() == "true",
        "group_url": os.getenv(
            "ANDROID_BETA_GROUP_URL", "https://groups.google.com/g/lanis-beta/about"
        ),
        "join_url": os.getenv(
            "ANDROID_BETA_JOIN_URL",
            "https://play.google.com/apps/testing/de.joancode.lanis",
        ),
    }


async def get(user_id):
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        row = await (
            await db.execute(
                "SELECT dismissed,prompt_after FROM android_beta_preferences WHERE user_id=?",
                (user_id,),
            )
        ).fetchone()
    return {
        **config(),
        "dismissed": bool(row and row[0]),
        "prompt_after": row[1] if row else 0,
        "invitation_due": not row or (not row[0] and row[1] <= int(time.time())),
    }


async def change(user_id, action):
    # Actions describe only recruitment preferences, never Google membership.
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT OR IGNORE INTO android_beta_preferences(user_id) VALUES(?)",
            (user_id,),
        )
        if action == "dismiss":
            await db.execute(
                "UPDATE android_beta_preferences SET dismissed=1 WHERE user_id=?",
                (user_id,),
            )
        else:
            await db.execute(
                "UPDATE android_beta_preferences SET prompt_after=? WHERE user_id=?",
                (int(time.time()) + 7 * 86400, user_id),
            )
        await db.commit()
    return await get(user_id)
