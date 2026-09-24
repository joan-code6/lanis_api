import asyncio
import json

from api import account_data, auth_db
from api.admin import router as admin_router
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
        assert isinstance(exported["preferences"], dict)
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


def test_account_deletion_uses_authenticated_identity_after_logout_race(
    tmp_path, monkeypatch
):
    async def scenario():
        monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()
        metrics = UserMetricsDB(tmp_path / "metrics.db")
        await metrics.initialize()
        monkeypatch.setattr(account_data, "user_metrics_db", metrics)

        await auth_db.store_refresh_token(
            "5201:student", "5201", "student", "secret-password"
        )
        await auth_db.save_notification_preferences(
            "5201:student", {"enabled": True}
        )
        await metrics.upsert_user("5201", "student", {"name": "Student"})
        await auth_db.delete_user_tokens("5201:student")

        report = await account_data.delete_account_data(
            "5201:student", school_id="5201", username="student"
        )

        assert report.success
        assert (
            await auth_db.get_notification_preferences("5201:student")
        )["enabled"] is False
        assert await metrics.get_user("5201", "student") is None

    asyncio.run(scenario())


def test_deletion_marker_survives_auth_database_reinitialization(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()
        await auth_db.delete_user_data("5201:student")

        assert await auth_db.account_deletion_marker_is_active("5201:student")
        await auth_db.initialize()
        assert await auth_db.account_deletion_marker_is_active("5201:student")

        await auth_db.clear_account_deletion_marker("5201:student")
        assert not await auth_db.account_deletion_marker_is_active("5201:student")

    asyncio.run(scenario())


def test_admin_cannot_reveal_credentials_or_request_step_up_tokens():
    paths = {route.path for route in admin_router.routes}
    assert "/admin/users/{user_id:path}/credentials/reveal" not in paths
    assert "/admin/auth/step-up" not in paths
