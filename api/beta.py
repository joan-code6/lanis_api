"""Account-owned Android closed beta registry. Approval records allowlisting only."""

import csv
import io
import os
import re
import sqlite3
import time
import uuid

import aiosqlite
from fastapi import HTTPException

from . import auth_db

SCHEMA = """CREATE TABLE IF NOT EXISTS android_beta (
 user_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
 email TEXT UNIQUE, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
 approved_at INTEGER, revision TEXT NOT NULL, acknowledged_revision TEXT,
 dismissed INTEGER NOT NULL DEFAULT 0, prompt_after INTEGER NOT NULL DEFAULT 0
);"""


def config():
    return {
        "available": os.getenv("ANDROID_BETA_ENABLED", "").lower() == "true",
        "join_url": os.getenv(
            "ANDROID_BETA_JOIN_URL",
            "https://play.google.com/apps/testing/de.joancode.lanis",
        ),
    }


def normalize_email(value):
    value = value.strip().lower()
    if (
        len(value) > 254
        or len(value.split("@")[0]) > 64
        or not re.fullmatch(
            r"[a-z0-9][a-z0-9.!#$%&'*+/=?^_`{|}~-]*@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+",
            value,
        )
        or ".." in value
        or value.split("@")[0].endswith(".")
    ):
        raise HTTPException(422, "Bitte gib eine gültige E-Mail-Adresse ein.")
    return value


def public(row):
    return {
        **config(),
        "email": row["email"] if row else None,
        "status": ("approved" if row["approved_at"] else "pending")
        if row and row["email"]
        else "none",
        "revision": row["revision"] if row else None,
        "approval_unseen": bool(
            row
            and row["approved_at"]
            and row["acknowledged_revision"] != row["revision"]
        ),
        "invitation_due": not row
        or (
            not row["email"]
            and not row["dismissed"]
            and row["prompt_after"] <= int(time.time())
        ),
    }


async def get(user_id):
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (
            await db.execute("SELECT * FROM android_beta WHERE user_id=?", (user_id,))
        ).fetchone()
        return public(row)


async def change(user_id, action, email=None, revision=None):
    now = int(time.time())
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT OR IGNORE INTO android_beta(user_id,request_id,created_at,updated_at,revision) VALUES(?,?,?,?,?)",
            (user_id, uuid.uuid4().hex, now, now, uuid.uuid4().hex),
        )
        row = await (
            await db.execute("SELECT * FROM android_beta WHERE user_id=?", (user_id,))
        ).fetchone()
        if action == "signup":
            if not config()["available"]:
                raise HTTPException(409, "Die Beta-Anmeldung ist derzeit geschlossen.")
            email = normalize_email(email or "")
            if row["email"] != email:
                try:
                    await db.execute(
                        "UPDATE android_beta SET email=?,approved_at=NULL,acknowledged_revision=NULL,revision=?,updated_at=? WHERE user_id=?",
                        (email, uuid.uuid4().hex, now, user_id),
                    )
                except sqlite3.IntegrityError:
                    raise HTTPException(
                        409,
                        "Diese Adresse kann nicht verwendet werden. Prüfe die Adresse oder verwende eine andere.",
                    )
        elif action == "withdraw":
            await db.execute(
                "UPDATE android_beta SET email=NULL,approved_at=NULL,acknowledged_revision=NULL,revision=?,updated_at=?,dismissed=1 WHERE user_id=?",
                (uuid.uuid4().hex, now, user_id),
            )
        elif action == "later":
            await db.execute(
                "UPDATE android_beta SET prompt_after=? WHERE user_id=?",
                (now + 7 * 86400, user_id),
            )
        elif action == "dismiss":
            await db.execute(
                "UPDATE android_beta SET dismissed=1 WHERE user_id=?", (user_id,)
            )
        elif action == "acknowledge":
            await db.execute(
                "UPDATE android_beta SET acknowledged_revision=? WHERE user_id=? AND revision=? AND approved_at IS NOT NULL",
                (revision, user_id, revision),
            )
        await db.commit()
    return await get(user_id)


async def listing():
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        return [
            dict(row)
            for row in await (
                await db.execute(
                    "SELECT request_id,email,created_at,updated_at,approved_at,revision FROM android_beta WHERE email IS NOT NULL ORDER BY created_at"
                )
            ).fetchall()
        ]


async def approve(items):
    owners = []
    async with aiosqlite.connect(auth_db.DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        for item in items:
            owner = await (
                await db.execute(
                    "SELECT user_id FROM android_beta WHERE request_id=?",
                    (item.request_id,),
                )
            ).fetchone()
            if owner:
                owners.append(owner[0])
            cursor = await db.execute(
                "UPDATE android_beta SET approved_at=?,updated_at=? WHERE request_id=? AND revision=? AND email IS NOT NULL AND approved_at IS NULL",
                (int(time.time()), int(time.time()), item.request_id, item.revision),
            )
            if cursor.rowcount != 1:
                raise HTTPException(
                    409, "Eine Anmeldung wurde geändert. Bitte lade die Liste neu."
                )
        await db.commit()
    return owners


def export_csv(rows, existing):
    # Google Play expects addresses, one per line, without a header or BOM.
    addresses = {normalize_email(value) for value in existing}
    addresses.update(row["email"] for row in rows)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    for address in sorted(addresses):
        writer.writerow([address])
    return stream.getvalue()
