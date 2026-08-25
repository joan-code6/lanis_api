import asyncio

import aiosqlite

from api.metrics.user_metrics_db import UserMetricsDB


def test_existing_case_variant_metrics_rows_are_merged(tmp_path):
    database_path = tmp_path / "user_metrics.db"

    async def scenario():
        # Build the pre-fix schema so the migration sees the same kind of
        # case-sensitive duplicates that existed in production.
        async with aiosqlite.connect(database_path) as db:
            await db.execute(
                """
                CREATE TABLE users (
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
                """
            )
            await db.executemany(
                """
                INSERT INTO users
                    (school_id, login, data_hash, user_data,
                     first_seen, last_updated, update_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "5201",
                        "Bennet.Wegener",
                        "old-hash",
                        '{"name": "old"}',
                        "2026-08-13T11:00:00",
                        "2026-08-13T11:00:00",
                        1,
                    ),
                    (
                        "5201",
                        "bennet.wegener",
                        "new-hash",
                        '{"name": "new"}',
                        "2026-08-13T12:00:00",
                        "2026-08-13T12:00:00",
                        2,
                    ),
                ],
            )
            await db.commit()

        database = UserMetricsDB(database_path)
        await database.initialize()

        assert await database.get_user_count() == 1
        record = await database.get_user("5201", "BENNET.WEGENER")
        assert record is not None
        assert record.login == "bennet.wegener"
        assert record.user_data == {"name": "new"}
        assert record.first_seen.isoformat() == "2026-08-13T11:00:00"
        assert record.last_updated.isoformat() == "2026-08-13T12:00:00"
        assert record.update_count == 3

    asyncio.run(scenario())


def test_case_variants_upsert_one_metrics_record(tmp_path):
    database = UserMetricsDB(tmp_path / "user_metrics.db")

    async def scenario():
        assert await database.upsert_user(
            "5201", "Bennet.Wegener", {"name": "Bennet"}
        ) == (True, True)
        assert await database.upsert_user(
            "5201", "bennet.wegener", {"name": "Bennet"}
        ) == (False, False)
        assert await database.get_user_count() == 1

    asyncio.run(scenario())
