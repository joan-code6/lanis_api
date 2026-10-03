"""Dedicated SQLite storage for user-submitted feedback."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

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
    return dict(row)


async def create_feedback(
    *,
    category: str,
    title: str,
    details: str,
    page: str | None,
    submitter_user_id: str,
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
                category, title, details, page, submitter_user_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (category, title, details, page, submitter_user_id, created_at.isoformat()),
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
