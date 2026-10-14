import asyncio
import threading
from datetime import UTC, datetime, timedelta

import aiosqlite
import jwt
import pytest
from fastapi import HTTPException, status

from api import api as api_module
from api import auth_db
from api.api import (
    AuthManager,
    SchulportalSessionData,
    TokenRefreshRequest,
    refresh_endpoint,
)


def _refresh_token_data():
    return {
        "token": "refresh-token",
        "user_id": "5201:student",
        "school_id": "5201",
        "username": "Student",
        "password": "password",
        "session_id": "session-id",
    }


def test_existing_browser_logins_survive_session_id_migration(tmp_path, monkeypatch):
    database_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", str(database_path))
    monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())

    async def scenario():
        expires_at = (
            datetime.now(UTC).replace(tzinfo=None) + timedelta(days=30)
        ).isoformat()
        async with aiosqlite.connect(database_path) as db:
            await db.execute(
                """
                CREATE TABLE refresh_tokens (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    school_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL
                )
                """
            )
            for token in ("browser-a-refresh", "browser-b-refresh"):
                await db.execute(
                    """
                    INSERT INTO refresh_tokens
                        (token, user_id, school_id, username, password, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        token,
                        "5201:student",
                        "5201",
                        "student",
                        "password",
                        expires_at,
                    ),
                )
            await db.commit()

        await auth_db.initialize()
        browser_a = await auth_db.get_refresh_token("browser-a-refresh")
        browser_b = await auth_db.get_refresh_token("browser-b-refresh")
        assert browser_a["session_id"]
        assert browser_b["session_id"]
        assert browser_a["session_id"] != browser_b["session_id"]

        legacy_access_token = jwt.encode(
            {
                "sub": "5201:student",
                "school_id": "5201",
                "username": "student",
                "iat": datetime.now(UTC),
                "exp": datetime.now(UTC) + timedelta(minutes=30),
            },
            api_module.JWT_SECRET,
            algorithm=api_module.JWT_ALGORITHM,
        )
        with pytest.raises(HTTPException) as legacy_error:
            await api_module.local_auth_dependency(legacy_access_token)
        assert legacy_error.value.status_code == status.HTTP_401_UNAUTHORIZED

        refreshed_a = await api_module.refresh_endpoint(
            api_module.TokenRefreshRequest(refresh_token="browser-a-refresh")
        )
        authenticated_a = await api_module.local_auth_dependency(
            refreshed_a.access_token
        )
        assert authenticated_a.session_id == browser_a["session_id"]

        # A process restart re-runs initialization but must retain the migrated
        # session ID and keep the saved refresh token usable.
        await auth_db.initialize()
        monkeypatch.setattr(api_module, "sessions", AuthManager())
        restarted_a = await api_module.refresh_endpoint(
            api_module.TokenRefreshRequest(refresh_token="browser-a-refresh")
        )
        restarted_payload = api_module.sessions.decode_access_token(
            restarted_a.access_token
        )
        assert restarted_payload["jti"] == browser_a["session_id"]
        authenticated_a = await api_module.local_auth_dependency(
            restarted_a.access_token
        )

        await api_module.logout_endpoint(authenticated_a)
        assert await auth_db.get_refresh_token("browser-a-refresh") is None
        with pytest.raises(HTTPException) as logged_out_error:
            await api_module.local_auth_dependency(restarted_a.access_token)
        assert logged_out_error.value.status_code == status.HTTP_401_UNAUTHORIZED

        refreshed_b = await api_module.refresh_endpoint(
            api_module.TokenRefreshRequest(refresh_token="browser-b-refresh")
        )
        authenticated_b = await api_module.local_auth_dependency(
            refreshed_b.access_token
        )
        assert authenticated_b.session_id == browser_b["session_id"]

    asyncio.run(scenario())


def test_refresh_does_not_require_a_live_schulportal_session(monkeypatch):
    class FakeSessions:
        def create_access_token(self, user_id, school_id, username, session_id):
            return f"access:{user_id}:{school_id}:{username}:{session_id}"

        async def _get_or_create_schulportal_client(self, _user_id):
            raise AssertionError("refresh must not contact Schulportal")

    async def get_refresh_token(_token):
        return _refresh_token_data()

    monkeypatch.setattr(api_module, "sessions", FakeSessions())
    monkeypatch.setattr(api_module, "get_refresh_token", get_refresh_token)

    response = asyncio.run(
        refresh_endpoint(TokenRefreshRequest(refresh_token="refresh-token"))
    )

    assert response.access_token == "access:5201:student:5201:Student:session-id"


def test_failed_restore_is_retryable_and_keeps_the_refresh_token(monkeypatch):
    attempts = 0
    token_lookups = 0

    class FakeClient:
        def __init__(self):
            self.closed = False

        def login(self, _school_id, _username, _password):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return {"success": False, "message": "temporary upstream failure"}
            return {"success": True}

        def close(self):
            self.closed = True

    async def get_refresh_token_by_user_id(_user_id):
        nonlocal token_lookups
        token_lookups += 1
        return _refresh_token_data()

    monkeypatch.setattr(api_module, "SchulportalHessenAPI", FakeClient)
    monkeypatch.setattr(
        api_module, "get_refresh_token_by_user_id", get_refresh_token_by_user_id
    )

    async def scenario():
        manager = AuthManager()

        with pytest.raises(HTTPException) as exc_info:
            await manager._get_or_create_schulportal_client("5201:student")

        assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert exc_info.value.headers == {"Retry-After": "5"}

        restored = await manager._get_or_create_schulportal_client("5201:student")
        assert isinstance(restored.client, FakeClient)

    asyncio.run(scenario())

    assert attempts == 2
    assert token_lookups == 2


def test_concurrent_requests_share_one_session_restore(monkeypatch):
    login_started = threading.Event()
    allow_login_to_finish = threading.Event()
    login_calls = 0

    class FakeClient:
        def login(self, _school_id, _username, _password):
            nonlocal login_calls
            login_calls += 1
            login_started.set()
            allow_login_to_finish.wait(timeout=5)
            return {"success": True}

        def close(self):
            pass

    async def get_refresh_token_by_user_id(_user_id):
        return _refresh_token_data()

    monkeypatch.setattr(api_module, "SchulportalHessenAPI", FakeClient)
    monkeypatch.setattr(
        api_module, "get_refresh_token_by_user_id", get_refresh_token_by_user_id
    )

    async def scenario():
        manager = AuthManager()
        first = asyncio.create_task(
            manager._get_or_create_schulportal_client("5201:student")
        )
        assert await asyncio.to_thread(login_started.wait, 5)
        second = asyncio.create_task(
            manager._get_or_create_schulportal_client("5201:student")
        )
        allow_login_to_finish.set()

        first_result, second_result = await asyncio.gather(first, second)
        assert first_result is second_result

    asyncio.run(scenario())

    assert login_calls == 1


def test_shutdown_closes_clients_without_remote_logout():
    class FakeClient:
        def __init__(self):
            self.close_calls = 0
            self.logout_calls = 0

        def close(self):
            self.close_calls += 1

        def logout(self):
            self.logout_calls += 1

    async def scenario():
        manager = AuthManager()
        client = FakeClient()
        manager._schulportal_clients["5201:student"] = SchulportalSessionData(
            client=client,
            created_at=api_module.datetime.utcnow(),
            last_used=api_module.datetime.utcnow(),
            username="Student",
            school_id="5201",
        )

        await manager.shutdown()

        assert client.close_calls == 1
        assert client.logout_calls == 0

    asyncio.run(scenario())
