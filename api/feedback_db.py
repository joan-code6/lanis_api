"""Dedicated SQLite storage for user-submitted feedback."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import json
import aiosqlite

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "feedback.db"
FeedbackStatus = Literal["open", "done"]
MAX_SUBMISSIONS_PER_HOUR = 5


class FeedbackRateLimitError(Exception):
    """Raised when an account submits feedback too frequently."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def initialize() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL CHECK (category IN ('feature', 'bug', 'general')),
                title TEXT NOT NULL,
                details TEXT NOT NULL,
                page TEXT,
                submitter_user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
                done_at TEXT,
                done_by TEXT
            )
            """
        )
        async with db.execute('PRAGMA table_info(feedback_reports)') as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if 'support_context' not in columns:
            await db.execute("ALTER TABLE feedback_reports ADD COLUMN support_context TEXT")
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_status_created "
            "ON feedback_reports(status, created_at DESC)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_submitter_created "
            "ON feedback_reports(submitter_user_id, created_at DESC)"
        )
        await db.commit()


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    result = dict(row)
    result['support_context'] = json.loads(result['support_context']) if result.get('support_context') else None
    return result


async def create_feedback(
    *,
    category: str,
    title: str,
    details: str,
    page: str | None,
    submitter_user_id: str,
    support_context: dict | None = None,
) -> dict[str, Any]:
    await initialize()
    created_at = _now()
    cutoff = (created_at - timedelta(hours=1)).isoformat()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT COUNT(*) FROM feedback_reports "
            "WHERE submitter_user_id = ? AND created_at >= ?",
            (submitter_user_id, cutoff),
        ) as cursor:
            count = int((await cursor.fetchone())[0])
        if count >= MAX_SUBMISSIONS_PER_HOUR:
            await db.rollback()
            raise FeedbackRateLimitError
        cursor = await db.execute(
            """
            INSERT INTO feedback_reports (
                category, title, details, page, submitter_user_id, created_at, support_context
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (category, title, details, page, submitter_user_id, created_at.isoformat(), json.dumps(support_context) if support_context else None),
        )
        report_id = cursor.lastrowid
        await db.commit()
        async with db.execute(
            "SELECT * FROM feedback_reports WHERE id = ?", (report_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_dict(row)


async def list_feedback(
    *, status: FeedbackStatus | None, limit: int, offset: int
) -> tuple[list[dict[str, Any]], int]:
    await initialize()
    where = "WHERE status = ?" if status else ""
    parameters: tuple[Any, ...] = (status,) if status else ()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT COUNT(*) FROM feedback_reports {where}", parameters
        ) as cursor:
            total = int((await cursor.fetchone())[0])
        async with db.execute(
            f"SELECT * FROM feedback_reports {where} "
            "ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, created_at DESC "
            "LIMIT ? OFFSET ?",
            (*parameters, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows], total


async def update_feedback_status(
    report_id: int, status: FeedbackStatus, done_by: str
) -> dict[str, Any] | None:
    await initialize()
    done_at = _now().isoformat() if status == "done" else None
    completed_by = done_by if status == "done" else None
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "UPDATE feedback_reports SET status = ?, done_at = ?, done_by = ? "
            "WHERE id = ?",
            (status, done_at, completed_by, report_id),
        )
        if cursor.rowcount == 0:
            await db.commit()
            return None
        await db.commit()
        async with db.execute(
            "SELECT * FROM feedback_reports WHERE id = ?", (report_id,)
        ) as cursor:
            row = await cursor.fetchone()
    return _row_to_dict(row)


async def get_account_data(user_id: str) -> list[dict[str, Any]]:
    """Export submitted reports without exposing other administrators' identities."""
    if not DB_PATH.exists():
        return []
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, category, title, details, page, created_at, status, done_at "
            "FROM feedback_reports WHERE submitter_user_id = ? ORDER BY id",
            (user_id,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]


async def delete_user_data(user_id: str) -> dict[str, int]:
    """Remove submitted content and anonymize actions on other users' reports."""
    if not DB_PATH.exists():
        return {"reports": 0, "actions_anonymized": 0}
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("BEGIN IMMEDIATE")
        deleted = await db.execute(
            "DELETE FROM feedback_reports WHERE submitter_user_id = ?", (user_id,),
        )
        anonymized = await db.execute(
            "UPDATE feedback_reports SET done_by = NULL WHERE done_by = ?", (user_id,),
        )
        await db.commit()
        return {"reports": max(deleted.rowcount, 0), "actions_anonymized": max(anonymized.rowcount, 0)}
