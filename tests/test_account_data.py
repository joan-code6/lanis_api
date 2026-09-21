import asyncio
import json

from api import account_data, auth_db
from api.metrics.user_metrics_db import UserMetricsDB


def test_account_export_excludes_authentication_secrets(tmp_path, monkeypatch):
    async def scenario():
        auth_path = tmp_path / "auth.db"
        metrics_path = tmp_path / "metrics.db"
        monkeypatch.setattr(auth_db, "DB_PATH", str(auth_path))
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()
        metrics = UserMetricsDB(metrics_path)
        await metrics.initialize()
        monkeypatch.setattr(account_data, "user_metrics_db", metrics)

        token = await auth_db.store_refresh_token(
            "5201:student", "5201", "student", "secret-password"
        )
        await auth_db.save_notification_preferences("5201:student", {"enabled": True})
        await metrics.upsert_user("5201", "student", {"name": "Student"})

        exported = await account_data.build_account_export("5201:student")
        serialized = json.dumps(exported)

        assert exported["profile"] == {"name": "Student"}
        assert exported["notification_preferences"]["enabled"] is True
        assert "secret-password" not in serialized
        assert token not in serialized
        assert "refresh tokens" in exported["excluded_secrets"]

    asyncio.run(scenario())


def test_account_deletion_removes_auth_and_metrics_data(tmp_path, monkeypatch):
    async def scenario():
        auth_path = tmp_path / "auth.db"
        metrics_path = tmp_path / "metrics.db"
        monkeypatch.setattr(auth_db, "DB_PATH", str(auth_path))
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()
        metrics = UserMetricsDB(metrics_path)
        await metrics.initialize()
        monkeypatch.setattr(account_data, "user_metrics_db", metrics)

        token = await auth_db.store_refresh_token(
            "5201:student", "5201", "student", "secret-password"
        )
        await metrics.upsert_user("5201", "student", {"name": "Student"})

        report = await account_data.delete_account_data("5201:student")

        assert report.success is True
        assert await auth_db.get_refresh_token(token) is None
        assert await metrics.get_user("5201", "student") is None

    asyncio.run(scenario())
