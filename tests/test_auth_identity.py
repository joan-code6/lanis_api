import asyncio

import aiosqlite

from api import auth_db


def test_existing_case_variant_auth_rows_are_rekeyed(tmp_path, monkeypatch):
    database_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", str(database_path))

    async def scenario():
        await auth_db.initialize()

        async with aiosqlite.connect(database_path) as db:
            await db.execute(
                """
                INSERT INTO refresh_tokens
                    (token, user_id, school_id, username, password, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "token-upper",
                    "5201:Bennet.Wegener",
                    "5201",
                    "Bennet.Wegener",
                    "test-password",
                    "2099-01-01T00:00:00",
                ),
            )
            await db.execute(
                """
                INSERT INTO notification_preferences
                    (user_id, enabled, updated_at)
                VALUES (?, ?, ?)
                """,
                ("5201:bennet.wegener", 0, "2026-08-25T11:00:00"),
            )
            await db.execute(
                """
                INSERT INTO notification_preferences
                    (user_id, enabled, updated_at)
                VALUES (?, ?, ?)
                """,
                ("5201:Bennet.wegener", 1, "2026-08-25T12:00:00"),
            )
            await db.execute(
                """
                INSERT INTO custom_lessons
                    (user_id, lesson_date, period, subject)
                VALUES (?, ?, ?, ?)
                """,
                ("5201:Bennet.wegener", "2026-08-25", "1", "Mathematik"),
            )
            await db.execute(
                """
                INSERT INTO push_subscriptions
                    (endpoint, user_id, p256dh, auth)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "https://push.example/subscription",
                    "5201:Bennet.wegener",
                    "public-key",
                    "auth-key",
                ),
            )
            await db.execute(
                """
                INSERT INTO message_notification_state (user_id, snapshot)
                VALUES (?, ?)
                """,
                ("5201:Bennet.wegener", '{"checked_at": "2026-08-25"}'),
            )
            await db.commit()

        # Startup is the migration boundary used by production.
        await auth_db.initialize()

        async with aiosqlite.connect(database_path) as db:
            for table in (
                "refresh_tokens",
                "notification_preferences",
                "custom_lessons",
                "push_subscriptions",
                "message_notification_state",
            ):
                async with db.execute(
                    f"SELECT DISTINCT user_id FROM {table}"
                ) as cursor:
                    assert [row[0] for row in await cursor.fetchall()] == [
                        "5201:bennet.wegener"
                    ]

        preferences = await auth_db.get_notification_preferences("5201:Bennet.Wegener")
        assert preferences["user_id"] == "5201:bennet.wegener"
        assert preferences["enabled"] is True
        assert (await auth_db.get_custom_lessons("5201:Bennet.Wegener"))[0][
            "subject"
        ] == "Mathematik"
        assert await auth_db.get_push_subscriptions("5201:Bennet.Wegener")
        assert await auth_db.get_message_notification_state("5201:Bennet.Wegener")

        token = await auth_db.store_refresh_token(
            "5201:Bennet.Wegener",
            "5201",
            "Bennet.Wegener",
            "test-password",
        )
        token_data = await auth_db.get_refresh_token(token)
        assert token_data["user_id"] == "5201:bennet.wegener"

    asyncio.run(scenario())
