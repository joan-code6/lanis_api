import asyncio
import threading

import pytest
from fastapi import HTTPException, status

from api import api as api_module
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
    }


def test_refresh_does_not_require_a_live_schulportal_session(monkeypatch):
    class FakeSessions:
        def create_access_token(self, user_id, school_id, username):
            return f"access:{user_id}:{school_id}:{username}"

        async def _get_or_create_schulportal_client(self, _user_id):
            raise AssertionError("refresh must not contact Schulportal")

    async def get_refresh_token(_token):
        return _refresh_token_data()

    monkeypatch.setattr(api_module, "sessions", FakeSessions())
    monkeypatch.setattr(api_module, "get_refresh_token", get_refresh_token)

    response = asyncio.run(
        refresh_endpoint(TokenRefreshRequest(refresh_token="refresh-token"))
    )

    assert response.access_token == "access:5201:student:5201:Student"


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
