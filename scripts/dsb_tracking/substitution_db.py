"""
Substitution Plan Database Module.

Stores and manages substitution plan data from DSBmobile.
Uses SQLite for persistent storage with async support via aiosqlite.

Handles the complexity of overlapping/mutable substitution plans:
- Plans can cover different date ranges (today to tomorrow, or multiple days)
- Data can change across fetches for the same day
- Each entry is versioned to track changes over time

Key features:
- Version tracking for each plan entry
- Date-based storage with the actual date each entry applies to
- Conflict detection when data changes for the same day
- Source tracking (which fetch this came from)
"""

import aiosqlite
import json
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum

logger = logging.getLogger("substitution_db")

DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "substitution_plan.db"


class EntryStatus(Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    MODIFIED = "modified"


@dataclass
class SubstitutionEntry:
    """Represents a single substitution plan entry."""

    id: Optional[int] = None
    school_id: str = ""
    plan_date: date = field(default_factory=date.today)
    period: str = ""
    klasse: str = ""
    original_lesson: str = ""
    substitution_lesson: str = ""
    teacher: str = ""
    room: str = ""
    info: str = ""
    status: str = "active"
    version: int = 1
    source_fetch_date: date = field(default_factory=date.today)
    data_hash: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "school_id": self.school_id,
            "plan_date": self.plan_date.isoformat(),
            "period": self.period,
            "klasse": self.klasse,
            "original_lesson": self.original_lesson,
            "substitution_lesson": self.substitution_lesson,
            "teacher": self.teacher,
            "room": self.room,
            "info": self.info,
            "status": self.status,
            "version": self.version,
            "source_fetch_date": self.source_fetch_date.isoformat(),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class SubstitutionPlanDB:
    """
    Async SQLite database for storing substitution plans.

    Usage:
        db = SubstitutionPlanDB()
        await db.initialize()

        # Store new plan data
        entries = [...]  # List of SubstitutionEntry objects
        await db.upsert_entries("school123", entries)

        # Get entries for a specific date
        day_entries = await db.get_entries_for_date("school123", date(2026, 4, 30))
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the database and create tables if they don't exist."""
        if self._initialized:
            return

        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS substitution_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    school_id TEXT NOT NULL,
                    plan_date DATE NOT NULL,
                    period TEXT NOT NULL,
                    klasse TEXT NOT NULL,
                    original_lesson TEXT,
                    substitution_lesson TEXT,
                    teacher TEXT,
                    room TEXT,
                    info TEXT,
                    status TEXT DEFAULT 'active',
                    version INTEGER DEFAULT 1,
                    source_fetch_date DATE NOT NULL,
                    data_hash TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(school_id, plan_date, period, klasse, source_fetch_date)
                )
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_substitution_school_date
                ON substitution_entries(school_id, plan_date)
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_substitution_date
                ON substitution_entries(plan_date)
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS fetch_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    school_id TEXT NOT NULL,
                    fetch_date DATE NOT NULL,
                    fetch_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    entry_count INTEGER,
                    source_url TEXT,
                    UNIQUE(school_id, fetch_date)
                )
            """)

            await db.commit()

        self._initialized = True
        logger.info(f"Substitution plan database initialized at {self.db_path}")

    def _compute_hash(self, entry_data: Dict[str, Any]) -> str:
        """Compute a hash of entry data for change detection."""
        relevant_fields = {
            "period": entry_data.get("period", ""),
            "klasse": entry_data.get("klasse", ""),
            "original_lesson": entry_data.get("original_lesson", ""),
            "substitution_lesson": entry_data.get("substitution_lesson", ""),
            "teacher": entry_data.get("teacher", ""),
            "room": entry_data.get("room", ""),
            "info": entry_data.get("info", ""),
            "status": entry_data.get("status", "active"),
        }
        sorted_data = json.dumps(relevant_fields, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(sorted_data.encode()).hexdigest()

    def _parse_date(self, date_str: str) -> date:
        """Parse date string from various formats."""
        formats = ["%d.%m.%Y", "%Y-%m-%d", "%d-%m-%Y"]
        for fmt in formats:
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        return date.today()

    async def upsert_entries(
        self,
        school_id: str,
        entries: List[Dict[str, Any]],
        source_url: Optional[str] = None,
    ) -> Tuple[int, int, int]:
        """
        Insert or update substitution entries for a fetch.

        Args:
            school_id: The school identifier
            entries: List of entry dictionaries from DSB parsing
            source_url: URL where this data came from

        Returns:
            Tuple of (new_count, updated_count, unchanged_count)
        """
        await self.initialize()

        fetch_date = date.today()
        new_count = 0
        updated_count = 0
        unchanged_count = 0

        async with aiosqlite.connect(self.db_path) as db:
            for entry_data in entries:
                plan_date = self._parse_date(entry_data.get("plan_date", ""))
                period = entry_data.get("period", "")
                klasse = entry_data.get("klasse", "")

                data_hash = self._compute_hash(entry_data)
                now = datetime.utcnow()

                existing = await db.execute(
                    """
                    SELECT id, version, data_hash FROM substitution_entries
                    WHERE school_id = ? AND plan_date = ? AND period = ? AND klasse = ?
                    ORDER BY version DESC LIMIT 1
                    """,
                    (school_id, plan_date, period, klasse),
                )
                existing_row = await existing.fetchone()

                if existing_row is None:
                    await db.execute(
                        """
                        INSERT INTO substitution_entries (
                            school_id, plan_date, period, klasse, original_lesson,
                            substitution_lesson, teacher, room, info, status, version,
                            source_fetch_date, data_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            school_id,
                            plan_date,
                            period,
                            klasse,
                            entry_data.get("original_lesson", ""),
                            entry_data.get("substitution_lesson", ""),
                            entry_data.get("teacher", ""),
                            entry_data.get("room", ""),
                            entry_data.get("info", ""),
                            entry_data.get("status", "active"),
                            1,
                            fetch_date,
                            data_hash,
                            now,
                            now,
                        ),
                    )
                    new_count += 1

                else:
                    existing_id, existing_version, existing_hash = existing_row

                    if existing_hash == data_hash:
                        unchanged_count += 1
                        continue

                    await db.execute(
                        """
                        UPDATE substitution_entries
                        SET version = version + 1,
                            original_lesson = ?,
                            substitution_lesson = ?,
                            teacher = ?,
                            room = ?,
                            info = ?,
                            status = ?,
                            source_fetch_date = ?,
                            data_hash = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            entry_data.get("original_lesson", ""),
                            entry_data.get("substitution_lesson", ""),
                            entry_data.get("teacher", ""),
                            entry_data.get("room", ""),
                            entry_data.get("info", ""),
                            entry_data.get("status", "active"),
                            fetch_date,
                            data_hash,
                            now,
                            existing_id,
                        ),
                    )
                    updated_count += 1

            await db.execute(
                """
                INSERT OR REPLACE INTO fetch_history (school_id, fetch_date, entry_count, source_url)
                VALUES (?, ?, ?, ?)
                """,
                (school_id, fetch_date, len(entries), source_url),
            )

            await db.commit()

        logger.info(
            f"Upsert complete: {new_count} new, {updated_count} updated, {unchanged_count} unchanged"
        )
        return (new_count, updated_count, unchanged_count)

    async def get_entries_for_date(
        self,
        school_id: str,
        target_date: date,
        include_all_versions: bool = False,
    ) -> List[SubstitutionEntry]:
        """Get entries for a specific date, optionally including historical versions."""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if include_all_versions:
                query = """
                    SELECT * FROM substitution_entries
                    WHERE school_id = ? AND plan_date = ?
                    ORDER BY period, version DESC
                """
            else:
                query = """
                    SELECT * FROM substitution_entries
                    WHERE school_id = ? AND plan_date = ?
                    ORDER BY period, version DESC
                """

            cursor = await db.execute(query, (school_id, target_date))
            rows = await cursor.fetchall()

            entries = []
            seen_keys = set()
            for row in rows:
                key = (row["period"], row["klasse"])
                if not include_all_versions and key in seen_keys:
                    continue
                seen_keys.add(key)

                entries.append(
                    SubstitutionEntry(
                        id=row["id"],
                        school_id=row["school_id"],
                        plan_date=datetime.strptime(
                            row["plan_date"], "%Y-%m-%d"
                        ).date(),
                        period=row["period"],
                        klasse=row["klasse"],
                        original_lesson=row["original_lesson"],
                        substitution_lesson=row["substitution_lesson"],
                        teacher=row["teacher"],
                        room=row["room"],
                        info=row["info"],
                        status=row["status"],
                        version=row["version"],
                        source_fetch_date=datetime.strptime(
                            row["source_fetch_date"], "%Y-%m-%d"
                        ).date(),
                        data_hash=row["data_hash"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                        updated_at=datetime.fromisoformat(row["updated_at"]),
                    )
                )

            return entries

    async def get_entries_for_date_range(
        self,
        school_id: str,
        start_date: date,
        end_date: date,
    ) -> List[SubstitutionEntry]:
        """Get all entries within a date range."""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                """
                SELECT * FROM substitution_entries
                WHERE school_id = ? AND plan_date BETWEEN ? AND ?
                ORDER BY plan_date, period
                """,
                (school_id, start_date, end_date),
            )
            rows = await cursor.fetchall()

            entries_dict: Dict[Tuple[str, str, str], SubstitutionEntry] = {}

            for row in rows:
                key = (row["period"], row["klasse"], str(row["plan_date"]))
                if key not in entries_dict:
                    entries_dict[key] = SubstitutionEntry(
                        id=row["id"],
                        school_id=row["school_id"],
                        plan_date=datetime.strptime(
                            row["plan_date"], "%Y-%m-%d"
                        ).date(),
                        period=row["period"],
                        klasse=row["klasse"],
                        original_lesson=row["original_lesson"],
                        substitution_lesson=row["substitution_lesson"],
                        teacher=row["teacher"],
                        room=row["room"],
                        info=row["info"],
                        status=row["status"],
                        version=row["version"],
                        source_fetch_date=datetime.strptime(
                            row["source_fetch_date"], "%Y-%m-%d"
                        ).date(),
                        data_hash=row["data_hash"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                        updated_at=datetime.fromisoformat(row["updated_at"]),
                    )

            return list(entries_dict.values())

    async def get_conflicts(
        self,
        school_id: str,
        target_date: date,
    ) -> List[Dict[str, Any]]:
        """Get entries where multiple versions exist for the same period/klasse on a date."""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                """
                SELECT period, klasse, COUNT(*) as version_count
                FROM substitution_entries
                WHERE school_id = ? AND plan_date = ?
                GROUP BY period, klasse
                HAVING COUNT(*) > 1
                """,
                (school_id, target_date),
            )
            conflicts = await cursor.fetchall()

            result = []
            for conflict in conflicts:
                period = conflict["period"]
                klasse = conflict["klasse"]

                version_cursor = await db.execute(
                    """
                    SELECT * FROM substitution_entries
                    WHERE school_id = ? AND plan_date = ? AND period = ? AND klasse = ?
                    ORDER BY version DESC
                    """,
                    (school_id, target_date, period, klasse),
                )
                versions = await version_cursor.fetchall()

                result.append(
                    {
                        "period": period,
                        "klasse": klasse,
                        "version_count": conflict["version_count"],
                        "versions": [
                            {
                                "version": v["version"],
                                "original_lesson": v["original_lesson"],
                                "substitution_lesson": v["substitution_lesson"],
                                "teacher": v["teacher"],
                                "room": v["room"],
                                "info": v["info"],
                                "source_fetch_date": v["source_fetch_date"],
                                "updated_at": v["updated_at"],
                            }
                            for v in versions
                        ],
                    }
                )

            return result

    async def get_fetch_history(
        self,
        school_id: str,
        limit: int = 30,
    ) -> List[Dict[str, Any]]:
        """Get recent fetch history for a school."""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                """
                SELECT * FROM fetch_history
                WHERE school_id = ?
                ORDER BY fetch_date DESC
                LIMIT ?
                """,
                (school_id, limit),
            )
            rows = await cursor.fetchall()

            return [
                {
                    "id": row["id"],
                    "school_id": row["school_id"],
                    "fetch_date": row["fetch_date"],
                    "fetch_time": row["fetch_time"],
                    "entry_count": row["entry_count"],
                    "source_url": row["source_url"],
                }
                for row in rows
            ]

    async def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        await self.initialize()

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM substitution_entries")
            total_entries = (await cursor.fetchone())[0]

            cursor = await db.execute(
                "SELECT COUNT(DISTINCT school_id) FROM substitution_entries"
            )
            unique_schools = (await cursor.fetchone())[0]

            cursor = await db.execute(
                "SELECT COUNT(DISTINCT plan_date) FROM substitution_entries"
            )
            covered_dates = (await cursor.fetchone())[0]

            cursor = await db.execute(
                """SELECT COUNT(*) FROM substitution_entries 
                   WHERE version > 1"""
            )
            modified_entries = (await cursor.fetchone())[0]

            cursor = await db.execute("SELECT COUNT(*) FROM fetch_history")
            total_fetches = (await cursor.fetchone())[0]

            return {
                "total_entries": total_entries,
                "unique_schools": unique_schools,
                "covered_dates": covered_dates,
                "modified_entries": modified_entries,
                "total_fetches": total_fetches,
                "db_path": str(self.db_path),
            }


substitution_plan_db = SubstitutionPlanDB()
