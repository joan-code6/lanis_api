import asyncio
from datetime import datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException

from api import api as api_module
from api import auth_db
from api import outage_cache


def test_revoked_session_invalidates_its_access_token(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()

        refresh_token = await auth_db.store_refresh_token(
            "5201:student", "5201", "student", "secret"
        )
        refresh_data = await auth_db.get_refresh_token(refresh_token)
        access_token = api_module.sessions.create_access_token(
            "5201:student",
            "5201",
            "student",
            refresh_data["session_id"],
        )

        identity = await api_module.local_auth_dependency(access_token)
        assert identity.user_id == "5201:student"

        await auth_db.delete_user_tokens("5201:student")
        with pytest.raises(HTTPException) as exc_info:
            await api_module.local_auth_dependency(access_token)
        assert exc_info.value.status_code == 401

    asyncio.run(scenario())


def test_access_token_without_jti_stays_invalid_after_relogin(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()

        legacy_token = jwt.encode(
            {
                "sub": "5201:student",
                "school_id": "5201",
                "username": "student",
                "iat": datetime.utcnow(),
                "exp": datetime.utcnow() + timedelta(minutes=30),
            },
            api_module.JWT_SECRET,
            algorithm=api_module.JWT_ALGORITHM,
        )

        await auth_db.store_refresh_token(
            "5201:student", "5201", "student", "new-secret"
        )
        with pytest.raises(HTTPException) as exc_info:
            await api_module.local_auth_dependency(legacy_token)
        assert exc_info.value.status_code == 401

    asyncio.run(scenario())


def test_login_uses_account_lifecycle_lock(monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def fake_login(_payload, _school_id, _username):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return api_module.LoginResponse(
                access_token="access",
                refresh_token="refresh",
                school_id="5201",
                username="student",
                encryption_ready=False,
            )

        monkeypatch.setattr(api_module, "_login_account", fake_login)
        payload = api_module.LoginRequest(
            school_id="5201", username="student", password="secret"
        )
        first = asyncio.create_task(api_module.login_endpoint(payload))
        await entered.wait()
        second = asyncio.create_task(api_module.login_endpoint(payload))
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        await asyncio.gather(first, second)
        assert calls == 2

    asyncio.run(scenario())


def test_runtime_deletion_removes_cache_versions_without_reopening_old_writes(
    monkeypatch,
):
    async def scenario():
        manager = api_module.AuthManager()
        store = outage_cache.SnapshotStore()
        monkeypatch.setattr(api_module, "snapshots", store)
        user_id = "5201:student"

        await manager.set_cache(user_id, "/modules", {"success": True})
        cache_version = await manager.get_cache_version(user_id, "/modules")
        snapshot_version = store.version(user_id, "/modules")
        store.put(
            (user_id, "/modules", ()),
            b'{"success":true}',
            datetime.utcnow(),
            snapshot_version,
        )

        await manager.delete_user_runtime_data(user_id)

        assert not manager._cache
        assert not [key for key in manager._cache_versions if key[0] == user_id]
        assert user_id not in store.user_versions
        assert not [key for key in store.path_versions if key[0] == user_id]
        assert not [key for key in store.entries if key[0] == user_id]
        assert not await manager.set_cache_if_current_version(
            user_id,
            "/modules",
            {"success": True, "stale": True},
            "",
            cache_version,
        )

    asyncio.run(scenario())


def test_cache_versions_keep_global_monotonicity_after_account_deletion():
    async def scenario():
        manager = api_module.AuthManager()
        user_id = "5201:student"
        stale_version = await manager.get_cache_version(user_id, "/modules")
        for _ in range(5):
            await manager.invalidate_endpoint_cache(user_id, "/modules")
        newest_before_delete = await manager.get_cache_version(user_id, "/modules")

        await manager.delete_user_runtime_data(user_id)

        new_version = await manager.get_cache_version(user_id, "/modules")
        assert newest_before_delete > stale_version
        assert new_version > newest_before_delete
        assert not await manager.set_cache_if_current_version(
            user_id,
            "/modules",
            {"success": True, "stale": True},
            "",
            newest_before_delete,
        )

    asyncio.run(scenario())
