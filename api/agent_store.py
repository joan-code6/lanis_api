"""Persistent, independently revocable OAuth connections. No plaintext tokens at rest."""

import hashlib
import json
import time
from contextlib import asynccontextmanager

import aiosqlite

from . import auth_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_clients (id TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_requests (id TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL, user_id TEXT);
CREATE TABLE IF NOT EXISTS agent_grants (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL, last_used REAL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS agent_tokens (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, grant_id TEXT NOT NULL, client_id TEXT NOT NULL, kind TEXT NOT NULL, expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agent_rate_limits (id TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
CREATE INDEX IF NOT EXISTS agent_grants_user ON agent_grants(user_id);
CREATE INDEX IF NOT EXISTS agent_tokens_grant ON agent_tokens(grant_id);
"""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@asynccontextmanager
async def database(write=False):
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if write:
            await db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            if write:
                await db.commit()
        except BaseException:
            if write:
                await db.rollback()
            raise


async def one(db, sql, params=()):
    async with db.execute(sql, params) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def connections(user_id):
    async with (
        database() as db,
        db.execute(
            "SELECT * FROM agent_grants WHERE user_id = ? ORDER BY created DESC",
            (user_id,),
        ) as cursor,
    ):
        rows = await cursor.fetchall()
    return [
        dict(
            id=r["id"],
            **json.loads(r["data"]),
            created_at=r["created"],
            last_used_at=r["last_used"],
            revoked=bool(r["revoked"]),
        )
        for r in rows
    ]


async def prune(db):
    now = time.time()
    await db.execute("DELETE FROM agent_requests WHERE expires < ?", (now,))
    await db.execute("DELETE FROM agent_rate_limits WHERE expires <= ?", (now,))
    await db.execute(
        "DELETE FROM agent_clients WHERE expires < ? AND id NOT IN (SELECT client_id FROM agent_tokens WHERE expires > ?)",
        (now, now),
    )
    # Retain used refresh tokens until family expiry to detect replay.
    await db.execute("DELETE FROM agent_tokens WHERE expires < ?", (now,))


async def rate_limit(key, limit, seconds):
    """Shared across workers/restarts; callers use a hashed network identity or grant."""
    now = time.time()
    key = digest(key)
    async with database(True) as db:
        await db.execute("DELETE FROM agent_rate_limits WHERE expires <= ?", (now,))
        row = await one(db, "SELECT count FROM agent_rate_limits WHERE id=?", (key,))
        if row and row["count"] >= limit:
            return False
        await db.execute(
            "INSERT INTO agent_rate_limits VALUES (?,1,?) ON CONFLICT(id) DO UPDATE SET count=count+1",
            (key, now + seconds),
        )
    return True
