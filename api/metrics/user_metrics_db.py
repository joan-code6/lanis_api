"""
User Metrics Database Module.

Stores and manages user data collected from the /benutzerverwaltung.php endpoint.
Uses SQLite for persistent storage with async support via aiosqlite.

Features:
- Stores user profile data (login, name, email, class, etc.)
- Tracks when users were first seen and last updated
- Only updates records if data has actually changed
- Provides async methods for all database operations
"""

import aiosqlite
import json
import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..identity import normalize_school_id, normalize_username

logger = logging.getLogger("user_metrics")

# Default database path (relative to project root)
DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "user_metrics.db"


def _row_recency(row: Dict[str, Any]) -> tuple[str, int]:
    """Choose the newest profile row while keeping migrations deterministic."""
    return (str(row["last_updated"] or ""), int(row["id"]))


async def _migrate_case_insensitive_logins(db: aiosqlite.Connection) -> int:
    """Merge rows created for case variants of the same SPH account."""
    db.row_factory = aiosqlite.Row
    async with db.execute("SELECT * FROM users") as cursor:
        rows = [dict(row) for row in await cursor.fetchall()]

    groups: dict[tuple[str, str], list[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[
            (normalize_school_id(row["school_id"]), normalize_username(row["login"]))
        ].append(row)

    migrated = 0
    for group_rows in groups.values():
        canonical_school_id = normalize_school_id(group_rows[0]["school_id"])
        canonical_login = normalize_username(group_rows[0]["login"])
        needs_merge = len(group_rows) > 1 or any(
            row["school_id"] != canonical_school_id or row["login"] != canonical_login
            for row in group_rows
        )
        if not needs_merge:
            continue

        winner = max(group_rows, key=_row_recency)
        first_seen = min(str(row["first_seen"] or "") for row in group_rows)
        last_updated = max(str(row["last_updated"] or "") for row in group_rows)
        update_count = sum(int(row["update_count"] or 0) for row in group_rows)

        for row in group_rows:
            if row["id"] == winner["id"]:
                continue
            await db.execute("DELETE FROM users WHERE id = ?", (row["id"],))

        await db.execute(
            """
            UPDATE users
            SET school_id = ?, login = ?, data_hash = ?, user_data = ?,
                first_seen = ?, last_updated = ?, update_count = ?
            WHERE id = ?
            """,
            (
                canonical_school_id,
                canonical_login,
                winner["data_hash"],
                winner["user_data"],
                first_seen,
                last_updated,
                update_count,
                winner["id"],
            ),
        )
        migrated += len(group_rows)

    return migrated


@dataclass
class UserRecord:
    """Represents a user record in the database."""
    id: int
    school_id: str
    login: str
    data_hash: str
    user_data: Dict[str, Any]
    first_seen: datetime
    last_updated: datetime
    update_count: int
    last_login: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    login_count: int = 0
    total_active_seconds: int = 0
    session_count: int = 0


class UserMetricsDB:
    """
    Async SQLite database for storing user metrics.
    
    Usage:
        db = UserMetricsDB()
        await db.initialize()
        
        # Store or update user data
        is_new, was_updated = await db.upsert_user("1234", "john.doe", user_data)
        
        # Query users
        user = await db.get_user("1234", "john.doe")
        all_users = await db.get_all_users()
    """
    
    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or DEFAULT_DB_PATH
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize the database and create tables if they don't exist."""
        if self._initialized:
            return
        
        # Ensure data directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    school_id TEXT NOT NULL,
                    login TEXT NOT NULL,
                    data_hash TEXT NOT NULL,
                    user_data TEXT NOT NULL,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    update_count INTEGER DEFAULT 1,
                    last_login TIMESTAMP,
                    last_seen TIMESTAMP,
                    login_count INTEGER NOT NULL DEFAULT 0,
                    total_active_seconds INTEGER NOT NULL DEFAULT 0,
                    session_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(school_id, login)
                )
            """)
            
            # Create index for faster lookups
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_school_login 
                ON users(school_id, login)
            """)

            migrated = await _migrate_case_insensitive_logins(db)
            async with db.execute("PRAGMA table_info(users)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            for column, definition in (
                ("last_login", "TIMESTAMP"),
                ("last_seen", "TIMESTAMP"),
                ("login_count", "INTEGER NOT NULL DEFAULT 0"),
                ("total_active_seconds", "INTEGER NOT NULL DEFAULT 0"),
                ("session_count", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in columns:
                    await db.execute(
                        f"ALTER TABLE users ADD COLUMN {column} {definition}"
                    )
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_users_canonical_identity
                ON users(school_id, login COLLATE NOCASE)
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS activity_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    school_id TEXT NOT NULL,
                    login TEXT NOT NULL,
                    occurred_at TIMESTAMP NOT NULL,
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    actor_user_id TEXT,
                    target_user_id TEXT,
                    action TEXT
                )
                """
            )
            async with db.execute("PRAGMA table_info(activity_events)") as cursor:
                event_columns = {row[1] for row in await cursor.fetchall()}
            for column, definition in (
                ("actor_user_id", "TEXT"),
                ("target_user_id", "TEXT"),
                ("action", "TEXT"),
            ):
                if column not in event_columns:
                    await db.execute(
                        f"ALTER TABLE activity_events ADD COLUMN {column} {definition}"
                    )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_activity_events_type_time
                ON activity_events(event_type, occurred_at)
                """
            )
            
            await db.commit()
        
        self._initialized = True
        if migrated:
            logger.info("Canonicalized %s legacy user-metrics rows", migrated)
        logger.info(f"User metrics database initialized at {self.db_path}")
    
    def _compute_hash(self, data: Dict[str, Any]) -> str:
        """Compute a hash of the user data for change detection."""
        # Sort keys for consistent hashing
        sorted_data = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(sorted_data.encode()).hexdigest()
    
    async def upsert_user(
        self,
        school_id: str,
        login: str,
        user_data: Dict[str, Any]
    ) -> tuple[bool, bool]:
        """
        Insert or update a user record.
        
        Only updates if the data has actually changed (based on hash comparison).
        
        Args:
            school_id: School ID
            login: User login name
            user_data: Dictionary of user data from benutzerverwaltung
            
        Returns:
            Tuple of (is_new_user, was_updated)
            - (True, True): New user was created
            - (False, True): Existing user was updated with new data
            - (False, False): Existing user, no changes detected
        """
        school_id = normalize_school_id(school_id)
        login = normalize_username(login)
        await self.initialize()
        
        data_hash = self._compute_hash(user_data)
        data_json = json.dumps(user_data, ensure_ascii=False)
        now = datetime.utcnow().isoformat()
        
        async with aiosqlite.connect(self.db_path) as db:
            # Check if user exists and get current hash
            cursor = await db.execute(
                "SELECT id, data_hash FROM users WHERE school_id = ? AND login = ?",
                (school_id, login)
            )
            existing = await cursor.fetchone()
            
            if existing is None:
                # New user - insert
                await db.execute(
                    """
                    INSERT INTO users (school_id, login, data_hash, user_data, first_seen, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (school_id, login, data_hash, data_json, now, now)
                )
                await db.commit()
                logger.info(f"New user recorded: {login}@{school_id}")
                return (True, True)
            
            existing_id, existing_hash = existing
            
            if existing_hash == data_hash:
                # No changes
                logger.debug(f"User unchanged: {login}@{school_id}")
                return (False, False)
            
            # Data changed - update
            await db.execute(
                """
                UPDATE users 
                SET data_hash = ?, user_data = ?, last_updated = ?, update_count = update_count + 1
                WHERE id = ?
                """,
                (data_hash, data_json, now, existing_id)
            )
            await db.commit()
            logger.info(f"User updated: {login}@{school_id}")
            return (False, True)
    
    async def get_user(self, school_id: str, login: str) -> Optional[UserRecord]:
        """Get a user record by school ID and login."""
        school_id = normalize_school_id(school_id)
        login = normalize_username(login)
        await self.initialize()
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM users WHERE school_id = ? AND login = ?",
                (school_id, login)
            )
            row = await cursor.fetchone()
            
            if row is None:
                return None
            
            return UserRecord(
                id=row["id"],
                school_id=row["school_id"],
                login=row["login"],
                data_hash=row["data_hash"],
                user_data=json.loads(row["user_data"]),
                first_seen=datetime.fromisoformat(row["first_seen"]),
                last_updated=datetime.fromisoformat(row["last_updated"]),
                update_count=row["update_count"],
                last_login=_parse_optional_datetime(row["last_login"]),
                last_seen=_parse_optional_datetime(row["last_seen"]),
                login_count=row["login_count"] or 0,
                total_active_seconds=row["total_active_seconds"] or 0,
                session_count=row["session_count"] or 0,
            )
    
    async def get_all_users(self, limit: int = 100, offset: int = 0) -> List[UserRecord]:
        """Get all user records with pagination."""
        await self.initialize()
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM users ORDER BY last_updated DESC LIMIT ? OFFSET ?",
                (limit, offset)
            )
            rows = await cursor.fetchall()
            
            return [
                UserRecord(
                    id=row["id"],
                    school_id=row["school_id"],
                    login=row["login"],
                    data_hash=row["data_hash"],
                    user_data=json.loads(row["user_data"]),
                    first_seen=datetime.fromisoformat(row["first_seen"]),
                    last_updated=datetime.fromisoformat(row["last_updated"]),
                    update_count=row["update_count"],
                    last_login=_parse_optional_datetime(row["last_login"]),
                    last_seen=_parse_optional_datetime(row["last_seen"]),
                    login_count=row["login_count"] or 0,
                    total_active_seconds=row["total_active_seconds"] or 0,
                    session_count=row["session_count"] or 0,
                )
                for row in rows
            ]
    
    async def get_user_count(self) -> int:
        """Get total number of users in the database."""
        await self.initialize()
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM users")
            row = await cursor.fetchone()
            return row[0] if row else 0
    
    async def get_users_by_school(self, school_id: str) -> List[UserRecord]:
        """Get all users from a specific school."""
        school_id = normalize_school_id(school_id)
        await self.initialize()
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM users WHERE school_id = ? ORDER BY login",
                (school_id,)
            )
            rows = await cursor.fetchall()
            
            return [
                UserRecord(
                    id=row["id"],
                    school_id=row["school_id"],
                    login=row["login"],
                    data_hash=row["data_hash"],
                    user_data=json.loads(row["user_data"]),
                    first_seen=datetime.fromisoformat(row["first_seen"]),
                    last_updated=datetime.fromisoformat(row["last_updated"]),
                    update_count=row["update_count"],
                    last_login=_parse_optional_datetime(row["last_login"]),
                    last_seen=_parse_optional_datetime(row["last_seen"]),
                    login_count=row["login_count"] or 0,
                    total_active_seconds=row["total_active_seconds"] or 0,
                    session_count=row["session_count"] or 0,
                )
                for row in rows
            ]
    
    async def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        await self.initialize()
        
        async with aiosqlite.connect(self.db_path) as db:
            # Total users
            cursor = await db.execute("SELECT COUNT(*) FROM users")
            total_users = (await cursor.fetchone())[0]
            
            # Unique schools
            cursor = await db.execute("SELECT COUNT(DISTINCT school_id) FROM users")
            unique_schools = (await cursor.fetchone())[0]
            
            # Recent activity (last 24 hours)
            cursor = await db.execute("""
                SELECT COUNT(*) FROM users 
                WHERE datetime(last_updated) > datetime('now', '-1 day')
            """)
            recent_updates = (await cursor.fetchone())[0]
            
            # New users today
            cursor = await db.execute("""
                SELECT COUNT(*) FROM users 
                WHERE datetime(first_seen) > datetime('now', '-1 day')
            """)
            new_today = (await cursor.fetchone())[0]

            active_24h = await db.execute(
                "SELECT COUNT(*) FROM users WHERE datetime(last_seen) > datetime('now', '-1 day')"
            )
            active_users_24h = (await active_24h.fetchone())[0]
            active_7d = await db.execute(
                "SELECT COUNT(*) FROM users WHERE datetime(last_seen) > datetime('now', '-7 day')"
            )
            active_users_7d = (await active_7d.fetchone())[0]
            totals = await db.execute(
                "SELECT COALESCE(SUM(login_count), 0), COALESCE(SUM(total_active_seconds), 0), "
                "COALESCE(SUM(session_count), 0) FROM users"
            )
            login_count, active_seconds, session_count = await totals.fetchone()
            
            return {
                "total_users": total_users,
                "unique_schools": unique_schools,
                "recent_updates_24h": recent_updates,
                "new_users_today": new_today,
                "active_users_24h": active_users_24h,
                "active_users_7d": active_users_7d,
                "login_count": login_count,
                "total_active_seconds": active_seconds,
                "session_count": session_count,
                "db_path": str(self.db_path),
            }

    async def record_login(self, school_id: str, login: str) -> None:
        """Record a successful LANIS login without requiring profile fetch success."""
        school_id = normalize_school_id(school_id)
        login = normalize_username(login)
        await self.initialize()
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO users (
                    school_id, login, data_hash, user_data, first_seen,
                    last_updated, last_login, last_seen, login_count, session_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
                ON CONFLICT(school_id, login) DO UPDATE SET
                    last_login = excluded.last_login,
                    last_seen = excluded.last_seen,
                    login_count = users.login_count + 1,
                    session_count = users.session_count + 1
                """,
                (school_id, login, self._compute_hash({}), "{}", now, now, now, now),
            )
            await db.execute(
                "INSERT INTO activity_events (event_type, school_id, login, occurred_at) VALUES (?, ?, ?, ?)",
                ("login", school_id, login, now),
            )
            await db.commit()

    async def get_login_series(self, since: datetime) -> List[Dict[str, Any]]:
        """Return daily login totals and unique accounts for a time range."""
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT substr(occurred_at, 1, 10) AS day,
                       COUNT(*) AS logins,
                       COUNT(DISTINCT school_id || ':' || login) AS unique_users
                FROM activity_events
                WHERE event_type = 'login' AND occurred_at >= ?
                GROUP BY day ORDER BY day
                """,
                (since.isoformat(),),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def record_admin_action(
        self, actor_user_id: str, action: str, target_user_id: str = ""
    ) -> None:
        """Persist a minimal audit record for privileged admin operations."""
        await self.initialize()
        now = datetime.utcnow().isoformat()
        actor_school, _, actor_login = actor_user_id.partition(":")
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO activity_events (
                    event_type, school_id, login, occurred_at,
                    actor_user_id, target_user_id, action
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "admin_action",
                    actor_school,
                    actor_login,
                    now,
                    actor_user_id,
                    target_user_id,
                    action,
                ),
            )
            await db.commit()

    async def get_admin_audit(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent privileged-operation audit records."""
        await self.initialize()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT occurred_at, actor_user_id, target_user_id, action
                FROM activity_events
                WHERE event_type = 'admin_action'
                ORDER BY occurred_at DESC LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def record_activity(self, school_id: str, login: str) -> None:
        """Update an activity heartbeat, counting only bounded active intervals."""
        school_id = normalize_school_id(school_id)
        login = normalize_username(login)
        await self.initialize()
        now = datetime.utcnow()
        now_value = now.isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT last_seen FROM users WHERE school_id = ? AND login = ?",
                (school_id, login),
            )
            row = await cursor.fetchone()
            delta = 0
            if row and row[0]:
                try:
                    previous = datetime.fromisoformat(str(row[0]))
                    gap = (now - previous).total_seconds()
                    if 0 <= gap < 15:
                        return
                    if 0 <= gap <= 300:
                        delta = int(gap)
                except ValueError:
                    pass
            await db.execute(
                """
                INSERT INTO users (
                    school_id, login, data_hash, user_data, first_seen,
                    last_updated, last_seen, total_active_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(school_id, login) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    total_active_seconds = users.total_active_seconds + excluded.total_active_seconds
                """,
                (school_id, login, self._compute_hash({}), "{}", now_value, now_value, now_value, delta),
            )
            if delta:
                await db.execute(
                    "INSERT INTO activity_events (event_type, school_id, login, occurred_at, duration_seconds) VALUES (?, ?, ?, ?, ?)",
                    ("activity", school_id, login, now_value, delta),
                )
            await db.commit()


def _parse_optional_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# Global database instance
user_metrics_db = UserMetricsDB()
