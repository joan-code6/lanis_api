"""Encrypted channel state and durable work for backend-hosted agents."""

import json
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter

from . import agent_store as store
from . import auth_db

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosted_pairings (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, channel TEXT NOT NULL, data TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS hosted_links (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, channel TEXT NOT NULL, sender_hash TEXT NOT NULL, recipient TEXT NOT NULL, history TEXT, history_at REAL, last_inbound_at REAL NOT NULL DEFAULT 0, UNIQUE(channel,sender_hash));
CREATE TABLE IF NOT EXISTS hosted_schedules (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, grant_id TEXT NOT NULL, data TEXT NOT NULL, next_run REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, lease REAL NOT NULL DEFAULT 0, last_status TEXT);
CREATE TABLE IF NOT EXISTS hosted_messages (id TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS hosted_actions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, grant_id TEXT NOT NULL, data TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS hosted_runs (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, grant_id TEXT NOT NULL, started REAL NOT NULL, finished REAL, status TEXT NOT NULL);
"""


def seal(value):
    return auth_db._encrypt_password(json.dumps(value, ensure_ascii=False))


def unseal(value):
    return json.loads(auth_db._decrypt_password(value))


def sender_hash(channel, sender):
    # Keyed hashes prevent enumeration of phone numbers from a database dump.
    return auth_db._whatsapp_id_hash(channel + ":" + sender)


async def grant(grant_id):
    async with store.database() as db:
        row = await store.one(
            db, "SELECT * FROM agent_grants WHERE id=? AND revoked=0", (grant_id,)
        )
    if not row or not await auth_db.get_refresh_token_by_user_id(row["user_id"]):
        return None
    return dict(id=row["id"], user_id=row["user_id"], **json.loads(row["data"]))


async def link(channel, sender):
    async with store.database() as db:
        return await store.one(
            db,
            "SELECT * FROM hosted_links WHERE channel=? AND sender_hash=?",
            (channel, sender_hash(channel, sender)),
        )


async def pair(code, channel, sender):
    from .agents import active_account

    now = time.time()
    async with store.database(True) as db:
        await db.execute("DELETE FROM hosted_pairings WHERE expires<=?", (now,))
        row = await store.one(
            db,
            "SELECT * FROM hosted_pairings WHERE id=? AND channel=?",
            (store.digest(code.upper()), channel),
        )
        if not row or not await active_account(db, row["user_id"]):
            return False
        existing = await store.one(
            db,
            "SELECT * FROM hosted_links WHERE channel=? AND sender_hash=?",
            (channel, sender_hash(channel, sender)),
        )
        if existing and existing["user_id"] != row["user_id"]:
            return False
        # Replacing a connection revokes its schedules and permissions immediately.
        async with db.execute(
            "SELECT id FROM hosted_links WHERE user_id=? AND channel=?",
            (row["user_id"], channel),
        ) as cursor:
            old = await cursor.fetchall()
        for item in old:
            await db.execute(
                "UPDATE agent_grants SET revoked=1 WHERE id=?", (item["id"],)
            )
            await db.execute(
                "DELETE FROM hosted_schedules WHERE grant_id=?", (item["id"],)
            )
        await db.execute(
            "DELETE FROM hosted_links WHERE user_id=? AND channel=?",
            (row["user_id"], channel),
        )
        gid = secrets.token_urlsafe(24)
        data = unseal(row["data"])
        data.update(
            channel=channel,
            client_name=channel.title(),
            client_id="hosted:" + channel,
            redirect_origin="LANIS Backend",
        )
        await db.execute(
            "DELETE FROM hosted_actions WHERE user_id=? AND grant_id NOT IN (SELECT id FROM hosted_links)",
            (row["user_id"],),
        )
        await db.execute(
            "INSERT INTO agent_grants(id,user_id,data,created) VALUES(?,?,?,?)",
            (gid, row["user_id"], json.dumps(data), now),
        )
        await db.execute(
            "INSERT INTO hosted_links(id,user_id,channel,sender_hash,recipient,last_inbound_at) VALUES(?,?,?,?,?,?)",
            (
                gid,
                row["user_id"],
                channel,
                sender_hash(channel, sender),
                seal(sender),
                now,
            ),
        )
        await db.execute("DELETE FROM hosted_pairings WHERE id=?", (row["id"],))
    if channel == "whatsapp":
        await auth_db.delete_whatsapp_link(row["user_id"])
    return True


def next_occurrence(data, after=None):
    now = time.time() if after is None else after
    if data.get("cron"):
        expression = data["cron"]
        if len(expression.split()) != 5 or len(expression) > 100:
            raise ValueError("Use a five-field cron expression")
        tz = ZoneInfo(data.get("timezone", "Europe/Berlin"))
        base = datetime.fromtimestamp(now, tz)
        upcoming = (
            croniter(expression, base, max_years_between_matches=5)
            .get_next(datetime)
            .timestamp()
        )
        iterator = croniter(
            expression,
            datetime.fromtimestamp(upcoming, tz),
            max_years_between_matches=5,
        )
        previous = upcoming
        for _ in range(64):
            following = iterator.get_next(datetime).timestamp()
            if following - previous < 300:
                raise ValueError("Schedules must be at least five minutes apart")
            previous = following
        return upcoming
    at = datetime.fromisoformat(data.get("at", ""))
    if at.tzinfo is None or at.timestamp() <= now:
        raise ValueError("Provide a future ISO date with timezone")
    return at.timestamp()


async def add_schedule(gid, data):
    current = await grant(gid)
    if not current or current.get("channel") not in ("signal", "whatsapp"):
        raise ValueError("Connection revoked")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000:
        raise ValueError("A task of 1–4000 characters is required")
    if bool(data.get("cron")) == bool(data.get("at")):
        raise ValueError("Choose either cron or at")
    upcoming = next_occurrence(data)
    sid = secrets.token_urlsafe(18)
    async with store.database(True) as db:
        live = await store.one(
            db, "SELECT revoked FROM agent_grants WHERE id=?", (gid,)
        )
        if not live or live["revoked"]:
            raise ValueError("Connection revoked")
        async with db.execute(
            "SELECT id,data,next_run,enabled FROM hosted_schedules WHERE grant_id=?",
            (gid,),
        ) as cursor:
            existing = await cursor.fetchall()
        for row in existing:
            if row["enabled"] and unseal(row["data"]) == data:
                return {"id": row["id"], "next_run": row["next_run"], **data}
        count = await store.one(
            db, "SELECT count(*) AS n FROM hosted_schedules WHERE grant_id=?", (gid,)
        )
        if count["n"] >= 20:
            raise ValueError("Limit of 20 schedules per connection reached")
        await db.execute(
            "INSERT INTO hosted_schedules(id,user_id,grant_id,data,next_run) VALUES(?,?,?,?,?)",
            (sid, current["user_id"], gid, seal(data), upcoming),
        )
    return {"id": sid, "next_run": upcoming, **data}


async def schedules(gid):
    async with (
        store.database() as db,
        db.execute(
            "SELECT * FROM hosted_schedules WHERE grant_id=? ORDER BY next_run", (gid,)
        ) as cursor,
    ):
        rows = await cursor.fetchall()
    return [
        dict(
            id=r["id"],
            enabled=bool(r["enabled"]),
            next_run=r["next_run"],
            last_status=r["last_status"],
            **unseal(r["data"]),
        )
        for r in rows
    ]


async def claim_due():
    now = time.time()
    async with store.database(True) as db:
        # Leases prevent concurrent execution across workers; expired leases are
        # recovered after crashes. Scheduled tools never perform school writes.
        row = await store.one(
            db,
            "SELECT s.* FROM hosted_schedules s JOIN agent_grants g ON g.id=s.grant_id WHERE s.enabled=1 AND s.next_run<=? AND s.lease<? AND g.revoked=0 ORDER BY s.next_run LIMIT 1",
            (now, now),
        )
        if row:
            await db.execute(
                "UPDATE hosted_schedules SET lease=? WHERE id=?", (now + 300, row["id"])
            )
        return row
