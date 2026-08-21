import asyncio
import sys
from datetime import datetime
from types import SimpleNamespace
from types import ModuleType
from zoneinfo import ZoneInfo

from api import auth_db
from api import message_notifications as notifications


def test_push_delivery_uses_a_finite_timeout(monkeypatch):
    calls = {}
    pywebpush = ModuleType("pywebpush")

    def webpush(**kwargs):
        calls.update(kwargs)

    pywebpush.webpush = webpush
    monkeypatch.setitem(sys.modules, "pywebpush", pywebpush)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "private")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@example.org")

    notifications._send_push_sync(
        {"endpoint": "https://push.example/subscription"}, {"title": "Test"}
    )

    assert calls["timeout"] == 10


def test_notification_storage_round_trip(tmp_path, monkeypatch):
    database_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", str(database_path))

    async def scenario():
        await auth_db.initialize()

        defaults = await auth_db.get_notification_preferences("user-a")
        assert defaults["enabled"] is False
        assert defaults["poll_interval_minutes"] == 15

        await auth_db.save_notification_preferences(
            "user-a",
            {
                "enabled": True,
                "start_time": "08:00",
                "end_time": "18:30",
                "poll_interval_minutes": 30,
                "timezone": "Europe/Berlin",
                "show_preview": False,
            },
        )
        await auth_db.save_push_subscription(
            "user-a",
            {
                "endpoint": "https://push.example/subscription",
                "keys": {"p256dh": "public-key", "auth": "auth-key"},
            },
        )
        await auth_db.save_message_notification_state(
            "user-a",
            {"checked_at": "2026-08-21T10:00:00+02:00", "conversations": {"c-1": "sig"}},
        )
        assert await auth_db.get_message_notification_state("user-a") == {
            "checked_at": "2026-08-21T10:00:00+02:00",
            "conversations": {"c-1": "sig"},
        }
        await auth_db.save_notification_preferences(
            "user-a",
            {
                "enabled": False,
                "start_time": "08:00",
                "end_time": "18:30",
                "poll_interval_minutes": 30,
                "timezone": "Europe/Berlin",
                "show_preview": False,
            },
        )
        assert await auth_db.get_message_notification_state("user-a") is not None
        await auth_db.save_notification_preferences(
            "user-a",
            {
                "enabled": True,
                "start_time": "08:00",
                "end_time": "18:30",
                "poll_interval_minutes": 30,
                "timezone": "Europe/Berlin",
                "show_preview": False,
            },
        )
        await auth_db.store_refresh_token("user-a", "5201", "user", "password")

        assert await auth_db.get_notification_preferences("user-a") == {
            "user_id": "user-a",
            "enabled": True,
            "start_time": "08:00",
            "end_time": "18:30",
            "poll_interval_minutes": 30,
            "timezone": "Europe/Berlin",
            "show_preview": False,
        }
        assert await auth_db.get_push_subscriptions("user-a") == [
            {
                "endpoint": "https://push.example/subscription",
                "keys": {"p256dh": "public-key", "auth": "auth-key"},
            }
        ]
        assert await auth_db.get_message_notification_state("user-a") is None
        assert [user["user_id"] for user in await auth_db.get_enabled_notification_users()] == [
            "user-a"
        ]

    asyncio.run(scenario())


def test_notification_window_supports_daytime_and_overnight_ranges():
    daytime = {
        "start_time": "07:00",
        "end_time": "21:00",
        "timezone": "Europe/Berlin",
    }
    overnight = {
        "start_time": "22:00",
        "end_time": "06:00",
        "timezone": "Europe/Berlin",
    }
    timezone = ZoneInfo("Europe/Berlin")

    assert notifications.is_notification_window_open(
        daytime, datetime(2026, 8, 21, 12, 0, tzinfo=timezone)
    )
    assert not notifications.is_notification_window_open(
        daytime, datetime(2026, 8, 21, 22, 0, tzinfo=timezone)
    )
    assert notifications.is_notification_window_open(
        overnight, datetime(2026, 8, 21, 23, 0, tzinfo=timezone)
    )
    assert notifications.is_notification_window_open(
        overnight, datetime(2026, 8, 21, 5, 0, tzinfo=timezone)
    )
    assert not notifications.is_notification_window_open(
        overnight, datetime(2026, 8, 21, 12, 0, tzinfo=timezone)
    )


def test_message_poll_baselines_then_notifies_on_a_changed_conversation(monkeypatch):
    state = None
    sent_payloads = []

    async def get_state(_user_id):
        return state

    async def save_state(_user_id, snapshot):
        nonlocal state
        state = snapshot

    async def get_subscriptions(_user_id):
        return [
            {
                "endpoint": "https://push.example/subscription",
                "keys": {"p256dh": "public-key", "auth": "auth-key"},
            }
        ]

    async def send_push(_user_id, _subscriptions, payload):
        sent_payloads.append(payload)
        return True

    monkeypatch.setattr(notifications, "get_message_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_message_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)
    monkeypatch.setattr(notifications, "_send_push_to_subscriptions", send_push)

    class FakeClient:
        responses = [
            {
                "success": True,
                "conversations": [
                    {
                        "id": "conversation-1",
                        "sender": "Herr Müller",
                        "subject": "Klausur",
                        "date": "2026-08-21T10:00:00+02:00",
                    }
                ],
            },
            {
                "success": True,
                "conversations": [
                    {
                        "id": "conversation-1",
                        "sender": "Herr Müller",
                        "subject": "Klausur",
                        "date": "2026-08-21T10:16:00+02:00",
                    }
                ],
            },
        ]

        def nachrichten_get_headers(self, _get_type, _last):
            return self.responses.pop(0)

    user = {
        "user_id": "user-a",
        "enabled": True,
        "start_time": "07:00",
        "end_time": "21:00",
        "poll_interval_minutes": 15,
        "timezone": "Europe/Berlin",
        "show_preview": True,
    }
    now = datetime(2026, 8, 21, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    async def get_client(_user_id):
        return SimpleNamespace(client=FakeClient())

    async def scenario():
        assert not await notifications.check_user_messages(user, get_client, now)
        assert sent_payloads == []

        changed = await notifications.check_user_messages(
            user,
            get_client,
            datetime(2026, 8, 21, 10, 16, tzinfo=ZoneInfo("Europe/Berlin")),
        )
        assert changed
        assert sent_payloads == [
            {
                "title": "Neue Nachricht in Lanis",
                "body": "Herr Müller: Klausur",
                "url": "/messages?conversation=conversation-1",
                "tag": "lanis-messages-conversation-1",
            }
        ]

    asyncio.run(scenario())


def test_message_poll_skips_portal_fetch_without_a_push_subscription(monkeypatch):
    user = {
        "user_id": "user-a",
        "enabled": True,
        "start_time": "07:00",
        "end_time": "21:00",
        "poll_interval_minutes": 15,
        "timezone": "Europe/Berlin",
        "show_preview": True,
    }
    now = datetime(2026, 8, 21, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    async def get_state(_user_id):
        return None

    async def get_subscriptions(_user_id):
        return []

    async def get_client(_user_id):
        raise AssertionError("portal client should not be created without a subscription")

    monkeypatch.setattr(notifications, "get_message_notification_state", get_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)

    assert not asyncio.run(notifications.check_user_messages(user, get_client, now))


def test_notification_cycle_isolates_a_single_user_failure(monkeypatch):
    users = [{"user_id": "bad"}, {"user_id": "good"}]
    checked = []

    async def get_users():
        return users

    async def check_user(user, _get_client):
        if user["user_id"] == "bad":
            raise ValueError("corrupt notification state")
        checked.append(user["user_id"])

    monkeypatch.setattr(notifications, "get_enabled_notification_users", get_users)
    monkeypatch.setattr(notifications, "check_user_messages", check_user)

    asyncio.run(notifications.run_message_notification_cycle(lambda _user_id: None))

    assert checked == ["good"]


def test_push_delivery_retries_when_any_device_has_a_transient_failure(monkeypatch):
    subscriptions = [
        {"endpoint": "https://push.example/one", "keys": {}},
        {"endpoint": "https://push.example/two", "keys": {}},
    ]

    async def run_in_threadpool(_func, subscription, _payload):
        if subscription["endpoint"].endswith("/two"):
            return RuntimeError("temporary provider failure")
        return None

    monkeypatch.setattr(notifications, "push_configured", lambda: True)
    monkeypatch.setattr(notifications, "run_in_threadpool", run_in_threadpool)

    async def scenario():
        return await notifications._send_push_to_subscriptions(
            "user-a", subscriptions, {"title": "Test"}
        )

    assert not asyncio.run(scenario())


def test_push_delivery_reports_failure_when_all_devices_are_gone(monkeypatch):
    subscriptions = [
        {"endpoint": "https://push.example/one", "keys": {}},
        {"endpoint": "https://push.example/two", "keys": {}},
    ]
    deleted = []

    class GoneError(Exception):
        def __init__(self):
            self.response = SimpleNamespace(status_code=410)

    async def run_in_threadpool(_func, _subscription, _payload):
        return GoneError()

    async def delete_subscription(user_id, endpoint):
        deleted.append((user_id, endpoint))

    monkeypatch.setattr(notifications, "push_configured", lambda: True)
    monkeypatch.setattr(notifications, "run_in_threadpool", run_in_threadpool)
    monkeypatch.setattr(notifications, "delete_push_subscription", delete_subscription)

    async def scenario():
        return await notifications._send_push_to_subscriptions(
            "user-a", subscriptions, {"title": "Test"}
        )

    assert not asyncio.run(scenario())
    assert deleted == [("user-a", "https://push.example/one"), ("user-a", "https://push.example/two")]
