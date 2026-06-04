"""
DSB Daily Snapshot Database and Scheduler.

Every day at 15:00, fetches all DSB data (login, plan URLs, substitution plan
with raw HTML and parsed tables) and stores it as a single JSON snapshot in SQLite.

Simple and robust: one table, one blob per day, errors are caught and logged.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import aiosqlite
from fastapi.concurrency import run_in_threadpool

from schulportal_hessen.base import SchulportalHessenAPI

logger = logging.getLogger("dsb_snapshot")

DB_PATH = Path(__file__).parent.parent / "data" / "dsb_daily_snapshot.db"


class DsbSnapshotDB:
    def __init__(self) -> None:
        self.db_path = DB_PATH
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fetch_date DATE NOT NULL UNIQUE,
                    fetch_time TIMESTAMP NOT NULL,
                    data TEXT NOT NULL,
                    entry_count INTEGER DEFAULT 0
                )
            """)
            await db.commit()
        self._initialized = True
        logger.info("DSB snapshot database initialized at %s", self.db_path)

    async def store_snapshot(
        self, school_id: str, snapshot_data: Dict[str, Any], entry_count: int
    ) -> None:
        await self.initialize()
        now = datetime.utcnow()
        today = date.today()
        data_json = json.dumps(snapshot_data, ensure_ascii=False)
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO daily_snapshots
                    (fetch_date, fetch_time, data, entry_count)
                VALUES (?, ?, ?, ?)
                """,
                (today.isoformat(), now.isoformat(), data_json, entry_count),
            )
            await db.commit()
        logger.info(
            "Stored DSB snapshot for %s on %s (%d entries)",
            school_id,
            today,
            entry_count,
        )

    async def get_snapshot(self, target_date: date) -> Optional[Dict[str, Any]]:
        await self.initialize()
        async with aiosqlite.connect(str(self.db_path)) as db:
            cursor = await db.execute(
                "SELECT data FROM daily_snapshots WHERE fetch_date = ?",
                (target_date.isoformat(),),
            )
            row = await cursor.fetchone()
            if row:
                return json.loads(row[0])
        return None


dsb_snapshot_db = DsbSnapshotDB()


async def _fetch_and_store_dsb_snapshot() -> None:
    dsb_username = os.getenv("DSB_USERNAME", "")
    dsb_password = os.getenv("DSB_PASSWORD", "")
    school_id = os.getenv("DSB_SCHOOL_ID", "")

    if not all([dsb_username, dsb_password, school_id]):
        logger.warning("DSB daily snapshot: missing DSB_USERNAME, DSB_PASSWORD, or DSB_SCHOOL_ID env vars")
        return

    try:
        api = SchulportalHessenAPI()

        login_result = await run_in_threadpool(api.dsb_login, dsb_username, dsb_password)
        plan_urls_result = await run_in_threadpool(
            api.dsb_get_plan_urls, dsb_username, dsb_password
        )
        plan_result = await run_in_threadpool(
            api.dsb_get_substitution_plan,
            dsb_username,
            dsb_password,
            0,
            None,
            True,
        )

        tables = plan_result.get("tables", []) if plan_result.get("success") else []
        entry_count = sum(len(t.get("rows", [])) for t in tables)

        snapshot = {
            "login": login_result,
            "plan_urls": plan_urls_result,
            "plan": plan_result,
        }

        await dsb_snapshot_db.store_snapshot(school_id, snapshot, entry_count)
    except Exception as e:
        logger.error("DSB daily snapshot failed: %s", e)


async def run_dsb_scheduler() -> asyncio.Task:
    await dsb_snapshot_db.initialize()

    async def _loop() -> None:
        while True:
            now = datetime.now()
            next_run = now.replace(hour=15, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            wait_seconds = (next_run - now).total_seconds()
            logger.info("DSB snapshot scheduler: next run at %s (in %.0fs)", next_run, wait_seconds)
            await asyncio.sleep(wait_seconds)
            try:
                await _fetch_and_store_dsb_snapshot()
            except Exception as e:
                logger.error("DSB snapshot scheduler error: %s", e)

    return asyncio.create_task(_loop())
