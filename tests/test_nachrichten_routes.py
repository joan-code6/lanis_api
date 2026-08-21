import asyncio
import base64
from types import SimpleNamespace

from api import api as api_module
from starlette.routing import Match

from api.api import (
    PushSubscriptionRequest,
    app,
    get_notification_config,
    register_notification_subscription,
    reply_message,
    search_recipients,
    send_message,
    sessions,
    task_queue,
)
from fastapi import HTTPException


def valid_push_keys():
    return {
        "p256dh": base64.urlsafe_b64encode(b"\x04" + b"\x01" * 64).rstrip(b"=").decode(),
        "auth": base64.urlsafe_b64encode(b"\x02" * 16).rstrip(b"=").decode(),
    }


def test_recipient_search_route_precedes_conversation_route() -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/nachrichten/search",
        "root_path": "",
    }

    first_match = next(
        route for route in app.routes if route.matches(scope)[0] is Match.FULL
    )

    assert first_match.endpoint.__name__ == "search_recipients"


def test_recipient_search_refresh_uses_client_query_parameter(monkeypatch) -> None:
    captured = {}

    async def get_cached(*_args, **_kwargs):
        return None

    async def set_cache_if_current_version(*args, **_kwargs):
        captured["cache_write"] = args
        return True

    async def set_cache(*_args, **_kwargs):
        return None

    async def get_cache_version(*_args, **_kwargs):
        return 0

    async def add_task(task):
        captured["task"] = task

    monkeypatch.setattr(sessions, "get_cached", get_cached)
    monkeypatch.setattr(sessions, "set_cache", set_cache)
    monkeypatch.setattr(
        sessions, "set_cache_if_current_version", set_cache_if_current_version
    )
    monkeypatch.setattr(sessions, "get_cache_version", get_cache_version)
    monkeypatch.setattr(task_queue, "add_task", add_task)

    client = SimpleNamespace(
        nachrichten_search_recipients=lambda query: {
            "success": True,
            "query": query,
            "results": [],
        }
    )
    auth = SimpleNamespace(user_id="user", client=client)

    result = asyncio.run(search_recipients("Bennet", auth))

    assert result["success"] is True
    assert captured["task"].args[3] == {"query": "Bennet"}
    assert captured["cache_write"][:3] == (
        "user",
        "/nachrichten/search",
        result,
    )
    assert captured["cache_write"][4] == 0


def test_send_message_invalidates_message_caches(monkeypatch) -> None:
    invalidated = []

    async def invalidate_endpoint_cache(user_id, endpoint):
        invalidated.append((user_id, endpoint))

    monkeypatch.setattr(sessions, "invalidate_endpoint_cache", invalidate_endpoint_cache)
    client = SimpleNamespace(
        nachrichten_send_message=lambda data: {
            "success": True,
            "message_id": "message-id",
            "data": data,
        }
    )
    auth = SimpleNamespace(user_id="user", client=client)

    result = asyncio.run(send_message(["recipient"], "Subject", "Body", auth))

    assert result["success"] is True
    assert invalidated == [
        ("user", "/nachrichten/headers"),
        ("user", "/nachrichten/conversation"),
    ]


def test_reply_message_invalidates_message_caches(monkeypatch) -> None:
    invalidated = []

    async def invalidate_endpoint_cache(user_id, endpoint):
        invalidated.append((user_id, endpoint))

    monkeypatch.setattr(sessions, "invalidate_endpoint_cache", invalidate_endpoint_cache)
    client = SimpleNamespace(
        nachrichten_reply_message=lambda conversation_id, body, to: {
            "success": True,
            "details": {"id": "reply-id", "conversation_id": conversation_id},
            "body": body,
            "to": to,
        }
    )
    auth = SimpleNamespace(user_id="user", client=client)

    result = asyncio.run(reply_message("conversation-id", "Body", "all", auth))

    assert result["success"] is True
    assert invalidated == [
        ("user", "/nachrichten/headers"),
        ("user", "/nachrichten/conversation"),
    ]


def test_notification_config_only_exposes_public_key_when_push_is_configured(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "public")
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("VAPID_SUBJECT", raising=False)

    incomplete = asyncio.run(get_notification_config(SimpleNamespace()))

    assert incomplete == {"success": True, "configured": False, "public_key": ""}

    monkeypatch.setenv("VAPID_PRIVATE_KEY", "private")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.org")
    configured = asyncio.run(get_notification_config(SimpleNamespace()))

    assert configured == {"success": True, "configured": True, "public_key": "public"}


def test_notification_subscription_requires_complete_vapid_configuration(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "public")
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("VAPID_SUBJECT", raising=False)
    payload = PushSubscriptionRequest(
        endpoint="https://push.example/subscription",
        keys={"p256dh": "public-key", "auth": "auth-key"},
    )
    auth = SimpleNamespace(user_id="user")

    async def scenario():
        try:
            await register_notification_subscription(payload, auth)
        except HTTPException as error:
            assert error.status_code == 503
        else:
            raise AssertionError("incomplete VAPID configuration must be rejected")

    asyncio.run(scenario())


def test_notification_subscription_rejects_untrusted_push_endpoints(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "public")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "private")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.org")
    payload = PushSubscriptionRequest(
        endpoint="https://127.0.0.1/metadata",
        keys={"p256dh": "public-key", "auth": "auth-key"},
    )
    auth = SimpleNamespace(user_id="user")
    saved = []

    async def save_subscription(*args):
        saved.append(args)

    monkeypatch.setattr(api_module, "save_push_subscription", save_subscription)

    async def scenario():
        try:
            await register_notification_subscription(payload, auth)
        except HTTPException as error:
            assert error.status_code == 422
        else:
            raise AssertionError("untrusted push endpoint must be rejected")

    asyncio.run(scenario())
    assert saved == []

    valid_payload = PushSubscriptionRequest(
        endpoint="https://fcm.googleapis.com/fcm/send/subscription",
        keys=valid_push_keys(),
    )
    assert asyncio.run(register_notification_subscription(valid_payload, auth)) == {
        "success": True
    }
    assert saved[0][0] == "user"
    assert saved[0][1]["endpoint"] == valid_payload.endpoint


def test_notification_subscription_rejects_malformed_push_keys(monkeypatch):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "public")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "private")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.org")
    payload = PushSubscriptionRequest(
        endpoint="https://fcm.googleapis.com/fcm/send/subscription",
        keys={"p256dh": "not-a-public-key", "auth": "not-auth"},
    )
    auth = SimpleNamespace(user_id="user")
    saved = []

    async def save_subscription(*args):
        saved.append(args)

    monkeypatch.setattr(api_module, "save_push_subscription", save_subscription)

    async def scenario():
        try:
            await register_notification_subscription(payload, auth)
        except HTTPException as error:
            assert error.status_code == 422
        else:
            raise AssertionError("malformed push keys must be rejected")

    asyncio.run(scenario())
    assert saved == []
