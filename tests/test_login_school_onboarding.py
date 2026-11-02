import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api import api as api_module


@pytest.mark.parametrize("first_user", [True, False, None])
def test_login_returns_school_onboarding_flag(monkeypatch, first_user):
    monkeypatch.setattr(
        api_module.sessions, "create_schulportal_session",
        AsyncMock(return_value="5201:student"),
    )
    monkeypatch.setattr(
        api_module.sessions, "_get_or_create_schulportal_client",
        AsyncMock(return_value=SimpleNamespace(client=SimpleNamespace(cryptor=None))),
    )
    monkeypatch.setattr(api_module.sessions, "create_access_token", lambda **_: "access")
    monkeypatch.setattr(api_module, "store_refresh_token", AsyncMock(return_value="refresh"))
    monkeypatch.setattr(
        api_module, "get_refresh_token", AsyncMock(return_value={"session_id": "test-session"}),
    )
    from api import account_data
    monkeypatch.setattr(account_data, "clear_account_deletion_marker", AsyncMock())
    monkeypatch.setattr(api_module.task_queue, "allow_user_tasks", AsyncMock())
    monkeypatch.setattr(api_module.task_queue, "add_task", AsyncMock())
    monkeypatch.setattr(api_module.user_metrics_db, "record_login", AsyncMock(return_value=True))
    lookup = AsyncMock(return_value=first_user)
    if first_user is None:
        lookup.side_effect = RuntimeError("Metrics unavailable")
    monkeypatch.setattr(api_module.user_metrics_db, "is_first_school_user", lookup)

    payload = api_module.LoginRequest(school_id="5201", username="student", password="test")
    response = asyncio.run(api_module.login_endpoint(payload))

    assert response.is_first_school_user is (first_user is True)
    assert response.access_token == "access"
    lookup.assert_awaited_once_with("5201", "student")
    tasks = [call.args[0].name for call in api_module.task_queue.add_task.await_args_list]
    assert tasks == ["notify_new_user:student@5201", "fetch_user_data:student@5201"]
