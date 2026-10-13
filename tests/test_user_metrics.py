import asyncio
from datetime import datetime, timedelta

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


def test_activity_heartbeats_and_admin_audit_are_persisted(tmp_path):
    database = UserMetricsDB(tmp_path / "user_metrics.db")

    async def scenario():
        assert await database.record_login("5201", "Bennet.Wegener") is True
        assert await database.record_login("5201", "Bennet.Wegener") is False
        await database.record_admin_action(
            "5201:admin", "credential_reveal", "5201:student"
        )

        record = await database.get_user("5201", "bennet.wegener")
        assert record is not None
        assert record.login_count == 2
        assert record.session_count == 2
        assert record.last_seen is not None

        audit = await database.get_admin_audit()
        assert audit[0]["action"] == "credential_reveal"
        assert audit[0]["target_user_id"] == "5201:student"
        assert len(await database.get_admin_audit(query="student")) == 1
        assert await database.get_admin_audit_count(query="student") == 1

    asyncio.run(scenario())


def test_analytics_aggregates_activity_and_module_launches(tmp_path):
    database = UserMetricsDB(tmp_path / "metrics.db")

    async def scenario():
        await database.record_login("5201", "student")
        await database.record_module_open("5201", "student", "Kalender")
        since = datetime.utcnow() - timedelta(days=1)

        growth = await database.get_growth_series(since)
        assert growth[-1]["new_users"] == 1
        assert growth[-1]["active_users"] == 1
        assert growth[-1]["logins"] == 1

        heatmap = await database.get_usage_heatmap(since)
        assert len(heatmap) == 7 * 24
        assert sum(cell["events"] for cell in heatmap) == 2

        assert await database.get_module_usage(since) == [
            {"module": "Kalender", "launches": 1, "unique_users": 1}
        ]

    asyncio.run(scenario())


def test_retention_uses_only_mature_users_in_each_cohort(tmp_path):
    database = UserMetricsDB(tmp_path / "metrics.db")

    async def scenario():
        await database.initialize()
        async with aiosqlite.connect(database.db_path) as db:
            await db.executemany(
                """
                INSERT INTO users
                    (school_id, login, data_hash, user_data, first_seen, last_updated)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "5201",
                        "early",
                        "hash",
                        "{}",
                        "2026-09-14T12:00:00",
                        "2026-09-14T12:00:00",
                    ),
                    (
                        "5201",
                        "late",
                        "hash",
                        "{}",
                        "2026-09-20T08:00:00",
                        "2026-09-20T08:00:00",
                    ),
                ],
            )
            await db.execute(
                """
                INSERT INTO activity_events (event_type, school_id, login, occurred_at)
                VALUES ('login', '5201', 'early', '2026-09-15T09:00:00')
                """
            )
            await db.commit()

        cohorts = await database.get_retention_cohorts(
            datetime.fromisoformat("2026-09-01T00:00:00"),
            now=datetime.fromisoformat("2026-09-20T12:00:00"),
        )
        assert cohorts == [
            {
                "cohort": "2026-09-14",
                "new_users": 2,
                "retention_1d": 100.0,
                "retention_7d": None,
                "retention_30d": None,
            }
        ]

    asyncio.run(scenario())


def test_retention_maturity_uses_elapsed_time(tmp_path):
    database = UserMetricsDB(tmp_path / "metrics.db")

    async def scenario():
        await database.initialize()
        async with aiosqlite.connect(database.db_path) as db:
            await db.execute(
                """
                INSERT INTO users
                    (school_id, login, data_hash, user_data, first_seen, last_updated)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "5201",
                    "student",
                    "hash",
                    "{}",
                    "2026-09-19T12:00:00",
                    "2026-09-19T12:00:00",
                ),
            )
            await db.execute(
                """
                INSERT INTO activity_events (event_type, school_id, login, occurred_at)
                VALUES ('login', '5201', 'student', '2026-09-20T00:00:00')
                """
            )
            await db.commit()

        cohorts = await database.get_retention_cohorts(
            datetime.fromisoformat("2026-09-01T00:00:00"),
            now=datetime.fromisoformat("2026-09-20T23:59:00"),
        )
        assert cohorts[0]["retention_1d"] is None

    asyncio.run(scenario())


def test_homepage_adoption_is_complete_above_five_thousand_users(tmp_path):
    database = UserMetricsDB(tmp_path / "user_metrics.db")

    async def scenario():
        await database.initialize()
        assert await database.get_homepage_adoption(minimum=5) == (0, 0, [])
        async with aiosqlite.connect(database.db_path) as db:
            rows = [
                (
                    "large-school",
                    f"student-{index}",
                    "hash",
                    "{}",
                )
                for index in range(5001)
            ]
            rows.extend(
                ("small-school", f"student-{index}", "hash", "{}")
                for index in range(4)
            )
            await db.executemany(
                """
                INSERT INTO users (school_id, login, data_hash, user_data)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )
            await db.commit()

        assert await database.get_homepage_adoption(minimum=5) == (
            5005,
            2,
            ["large-school"],
        )

    asyncio.run(scenario())


def test_first_school_user_survives_relogin_and_additional_users(tmp_path):
    database = UserMetricsDB(tmp_path / "metrics.db")

    async def scenario():
        assert not await database.is_first_school_user("5201", "first")
        await database.record_login("5201", "First")
        await database.record_login("5201", "second")
        await database.record_login("5202", "other")
        await database.record_login("5201", "FIRST")
        # Background profile updates must not change who was first.
        await database.upsert_user("5201", "first", {"name": "First"})
        assert await database.is_first_school_user("5201", "FIRST")
        assert not await database.is_first_school_user("5201", "second")
        assert await database.is_first_school_user("5202", "other")
        assert not await database.is_first_school_user("5203", "first")

    asyncio.run(scenario())


def test_concurrent_school_logins_identify_exactly_one_first_user(tmp_path):
    database = UserMetricsDB(tmp_path / "metrics.db")

    async def scenario():
        await database.initialize()
        async def login_and_check(login):
            await database.record_login("5201", login)
            return await database.is_first_school_user("5201", login)

        flags = await asyncio.gather(*(
            login_and_check(login) for login in ("alice", "bob", "charlie")
        ))
        assert sum(flags) == 1

    asyncio.run(scenario())


def test_earlier_captured_timestamp_cannot_replace_committed_first_user(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from importlib import import_module
    metrics_module = import_module("api.metrics.user_metrics_db")

    database = UserMetricsDB(tmp_path / "metrics.db")

    async def scenario():
        await database.initialize()
        # A waiting login can capture its timestamp before the login that commits first.
        timestamps = iter([datetime(2026, 1, 1, 12, 1), datetime(2026, 1, 1, 12, 0)])
        monkeypatch.setattr(metrics_module, "datetime", SimpleNamespace(utcnow=lambda: next(timestamps)))
        await database.record_login("5201", "committed-first")
        assert await database.is_first_school_user("5201", "committed-first")
        await database.record_login("5201", "captured-earlier")
        assert not await database.is_first_school_user("5201", "captured-earlier")
        assert await database.is_first_school_user("5201", "committed-first")

    asyncio.run(scenario())
