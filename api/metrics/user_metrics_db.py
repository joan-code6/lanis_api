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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("user_metrics")

# Default database path (relative to project root)
DEFAULT_DB_PATH = Path(__file__).parent.parent.parent / "data" / "user_metrics.db"


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
                    UNIQUE(school_id, login)
                )
            """)
            
            # Create index for faster lookups
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_users_school_login 
                ON users(school_id, login)
            """)
            
            await db.commit()
        
        self._initialized = True
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
                update_count=row["update_count"]
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
                    update_count=row["update_count"]
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
                    update_count=row["update_count"]
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
            
            return {
                "total_users": total_users,
                "unique_schools": unique_schools,
                "recent_updates_24h": recent_updates,
                "new_users_today": new_today,
                "db_path": str(self.db_path),
            }


# Global database instance
user_metrics_db = UserMetricsDB()
