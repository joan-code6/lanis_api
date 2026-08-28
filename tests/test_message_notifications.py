import asyncio
import sys
from datetime import datetime
from types import SimpleNamespace
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

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
        assert defaults["messages_enabled"] is True
        assert defaults["vertretungsplan_class_mode"] == "own"
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
        await auth_db.save_vertretungsplan_notification_state(
            "user-a",
            {"checked_at": "2026-08-21T10:00:00+02:00", "entries": {"v-1": "sig"}},
        )
        assert await auth_db.get_vertretungsplan_notification_state("user-a") == {
            "checked_at": "2026-08-21T10:00:00+02:00",
            "entries": {"v-1": "sig"},
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
        assert await auth_db.get_vertretungsplan_notification_state("user-a") is not None
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
            "messages_enabled": True,
            "vertretungsplan_enabled": False,
            "vertretungsplan_class_mode": "own",
            "vertretungsplan_classes": [],
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
        assert await auth_db.get_vertretungsplan_notification_state("user-a") is None
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


def test_notification_preferences_reject_timezone_offsets_in_clocks():
    preferences = {
        "start_time": "07:00+02:00",
        "end_time": "21:00",
        "timezone": "Europe/Berlin",
    }

    with pytest.raises(ValueError):
        notifications.validate_notification_preferences(preferences)
    assert not notifications.is_notification_window_open(
        preferences, datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    )


def test_enabled_notification_preferences_require_a_notification_category():
    preferences = {
        "enabled": True,
        "messages_enabled": False,
        "vertretungsplan_enabled": False,
        "vertretungsplan_class_mode": "own",
        "vertretungsplan_classes": [],
        "start_time": "07:00",
        "end_time": "21:00",
        "poll_interval_minutes": 15,
        "timezone": "Europe/Berlin",
    }

    with pytest.raises(ValueError, match="notification category"):
        notifications.validate_notification_preferences(preferences)


def test_vertretungsplan_snapshot_defaults_to_the_users_own_class():
    result = {
        "success": True,
        "days": [
            {
                "date": "28.08.2026",
                "substitutions": [
                    {"tag_en": "2026-08-28", "stunde": "1", "klasse": "10a", "fach": "Mathe"},
                    {"tag_en": "2026-08-28", "stunde": "2", "klasse": "10b", "fach": "Deutsch"},
                ],
            }
        ],
    }
    preferences = {
        "vertretungsplan_class_mode": "own",
        "vertretungsplan_classes": [],
    }

    snapshot, details = notifications.build_vertretungsplan_snapshot(
        result, preferences, "10a"
    )

    assert len(snapshot) == 1
    assert [entry["class"] for entry in details.values()] == ["10a"]


def test_vertretungsplan_scope_supports_selected_and_all_classes():
    result = {
        "days": [
            {
                "date": "28.08.2026",
                "substitutions": [
                    {"stunde": "1", "klasse": "10a", "fach": "Mathe"},
                    {"stunde": "2", "klasse": "10b", "fach": "Deutsch"},
                ],
            }
        ]
    }

    selected, _ = notifications.build_vertretungsplan_snapshot(
        result,
        {"vertretungsplan_class_mode": "selected", "vertretungsplan_classes": ["10b"]},
    )
    all_entries, _ = notifications.build_vertretungsplan_snapshot(
        result,
        {"vertretungsplan_class_mode": "all", "vertretungsplan_classes": []},
    )

    assert len(selected) == 1
    assert len(all_entries) == 2


def test_vertretungsplan_splits_whitespace_delimited_classes():
    result = {
        "success": True,
        "days": [
            {
                "date": "28.08.2026",
                "substitutions": [
                    {"stunde": "1", "klasse": "10a 10b", "fach": "Mathe"}
                ],
            }
        ],
    }

    snapshot, _ = notifications.build_vertretungsplan_snapshot(
        result,
        {"vertretungsplan_class_mode": "own", "vertretungsplan_classes": []},
        "10a",
    )
    options = notifications.vertretungsplan_notification_options(
        {"success": True, "data": {"klasse": "10a"}}, result
    )

    assert len(snapshot) == 1
    assert options["available_classes"] == ["10a", "10b"]


def test_vertretungsplan_poll_uses_refreshed_class_mode_for_profile_fetch(monkeypatch):
    state = None

    async def get_state(_user_id):
        return state

    async def save_state(_user_id, snapshot):
        nonlocal state
        state = snapshot

    async def get_subscriptions(_user_id):
        return [{"endpoint": "https://push.example/subscription", "keys": {}}]

    monkeypatch.setattr(notifications, "get_vertretungsplan_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_vertretungsplan_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)

    class FakeClient:
        profile_calls = 0

        def vertretungsplan_get_plan(self, _include_raw):
            return {"success": True, "days": []}

        def benutzer_get_data(self):
            self.profile_calls += 1
            return {"success": True, "data": {"klasse": "10a"}}

    user = {
        "user_id": "user-a",
        "enabled": True,
        "vertretungsplan_enabled": True,
        "vertretungsplan_class_mode": "all",
        "vertretungsplan_classes": [],
        "start_time": "07:00",
        "end_time": "21:00",
        "poll_interval_minutes": 15,
        "timezone": "Europe/Berlin",
    }
    client = FakeClient()

    async def get_client(_user_id):
        return SimpleNamespace(client=client)

    async def get_preferences(_user_id):
        return {"vertretungsplan_class_mode": "own"}

    async def scenario():
        now = datetime(2026, 8, 28, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        assert not await notifications.check_user_vertretungsplan(
            user, get_client, now, get_preferences=get_preferences
        )

    asyncio.run(scenario())
    assert client.profile_calls == 1
    assert state["entries"] == {}


def test_vertretungsplan_poll_baselines_then_notifies_for_a_new_own_class_entry(monkeypatch):
    state = None
    sent_payloads = []

    async def get_state(_user_id):
        return state

    async def save_state(_user_id, snapshot):
        nonlocal state
        state = snapshot

    async def get_subscriptions(_user_id):
        return [{"endpoint": "https://push.example/subscription", "keys": {}}]

    async def send_push(_user_id, deliveries):
        sent_payloads.extend(payload for _subscription, payload in deliveries.values())
        return {endpoint: "delivered" for endpoint in deliveries}

    monkeypatch.setattr(notifications, "get_vertretungsplan_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_vertretungsplan_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)
    monkeypatch.setattr(notifications, "_send_push_payloads", send_push)

    baseline = {
        "success": True,
        "days": [{"date": "28.08.2026", "substitutions": []}],
    }
    changed = {
        "success": True,
        "days": [
            {
                "date": "28.08.2026",
                "substitutions": [
                    {"stunde": "3", "klasse": "10a", "fach": "Mathe", "art": "Vertretung"},
                    {"stunde": "4", "klasse": "10b", "fach": "Deutsch", "art": "Ausfall"},
                ],
            }
        ],
    }

    class FakeClient:
        plans = [baseline, changed]

        def vertretungsplan_get_plan(self, _include_raw):
            return self.plans.pop(0)

        def benutzer_get_data(self):
            return {"success": True, "data": {"klasse": "10a"}}

    user = {
        "user_id": "user-a",
        "enabled": True,
        "vertretungsplan_enabled": True,
        "vertretungsplan_class_mode": "own",
        "vertretungsplan_classes": [],
        "start_time": "07:00",
        "end_time": "21:00",
        "poll_interval_minutes": 15,
        "timezone": "Europe/Berlin",
    }
    client = FakeClient()

    async def get_client(_user_id):
        return SimpleNamespace(client=client)

    async def scenario():
        now = datetime(2026, 8, 28, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        assert not await notifications.check_user_vertretungsplan(user, get_client, now)
        assert await notifications.check_user_vertretungsplan(
            user, get_client, now.replace(minute=15)
        )

    asyncio.run(scenario())
    assert len(sent_payloads) == 1
    assert "10a" in sent_payloads[0]["body"]
    assert "10b" not in sent_payloads[0]["body"]


def test_vertretungsplan_poll_rebaselines_when_the_users_own_class_changes(monkeypatch):
    state = None
    sent_payloads = []

    async def get_state(_user_id):
        return state

    async def save_state(_user_id, snapshot):
        nonlocal state
        state = snapshot

    async def get_subscriptions(_user_id):
        return [{"endpoint": "https://push.example/subscription", "keys": {}}]

    async def send_push(_user_id, deliveries):
        sent_payloads.extend(payload for _subscription, payload in deliveries.values())
        return {endpoint: "delivered" for endpoint in deliveries}

    monkeypatch.setattr(notifications, "get_vertretungsplan_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_vertretungsplan_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)
    monkeypatch.setattr(notifications, "_send_push_payloads", send_push)

    def plan_for(class_name):
        return {
            "success": True,
            "days": [
                {
                    "date": "28.08.2026",
                    "substitutions": [
                        {
                            "stunde": "3",
                            "klasse": class_name,
                            "fach": "Mathe",
                            "art": "Vertretung",
                        }
                    ],
                }
            ],
        }

    class FakeClient:
        plans = [plan_for("10a"), plan_for("10b")]
        own_classes = ["10a", "10b"]

        def vertretungsplan_get_plan(self, _include_raw):
            return self.plans.pop(0)

        def benutzer_get_data(self):
            return {"success": True, "data": {"klasse": self.own_classes.pop(0)}}

    user = {
        "user_id": "user-a",
        "enabled": True,
        "vertretungsplan_enabled": True,
        "vertretungsplan_class_mode": "own",
        "vertretungsplan_classes": [],
        "start_time": "07:00",
        "end_time": "21:00",
        "poll_interval_minutes": 15,
        "timezone": "Europe/Berlin",
    }
    client = FakeClient()

    async def get_client(_user_id):
        return SimpleNamespace(client=client)

    async def scenario():
        now = datetime(2026, 8, 28, 10, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        assert not await notifications.check_user_vertretungsplan(user, get_client, now)
        assert not await notifications.check_user_vertretungsplan(
            user, get_client, now.replace(minute=15)
        )

    asyncio.run(scenario())
    assert sent_payloads == []
    assert state["scope"] == {"mode": "own", "classes": ["10b"]}
    assert len(state["entries"]) == 1


def test_message_poll_baselines_then_notifies_on_a_changed_conversation(monkeypatch):
    state = None
    sent_payloads = []
    invalidated = []

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

    async def send_push(_user_id, deliveries):
        sent_payloads.extend(payload for _subscription, payload in deliveries.values())
        return {endpoint: "delivered" for endpoint in deliveries}

    async def invalidate_cache(_user_id, endpoint):
        invalidated.append(endpoint)

    monkeypatch.setattr(notifications, "get_message_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_message_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)
    monkeypatch.setattr(notifications, "_send_push_payloads", send_push)

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
                        "unread": True,
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
                        "unread": True,
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
            invalidate_cache=invalidate_cache,
        )
        assert changed
        assert invalidated == ["/nachrichten/headers", "/nachrichten/conversation"]
        assert sent_payloads == [
            {
                "title": "Neue Nachricht in Lanis",
                "body": "Herr Müller: Klausur",
                "url": "/messages?conversation=conversation-1",
                "tag": "lanis-messages-conversation-1",
            }
        ]

    asyncio.run(scenario())


def test_message_poll_rechecks_preferences_before_delivery(monkeypatch):
    state = None
    sent_bodies = []
    preference_versions = [
        {"enabled": True, "show_preview": True},
        {"enabled": True, "show_preview": False},
        {"enabled": False, "show_preview": False},
    ]

    async def get_state(_user_id):
        return state

    async def save_state(_user_id, snapshot):
        nonlocal state
        state = snapshot

    async def get_subscriptions(_user_id):
        return [{"endpoint": "https://push.example/subscription", "keys": {}}]

    async def send_push(_user_id, deliveries):
        sent_bodies.extend(payload["body"] for _subscription, payload in deliveries.values())
        return {endpoint: "delivered" for endpoint in deliveries}

    async def get_preferences(_user_id):
        return {
            **preference_versions.pop(0),
            "start_time": "07:00",
            "end_time": "21:00",
            "poll_interval_minutes": 15,
            "timezone": "Europe/Berlin",
        }

    monkeypatch.setattr(notifications, "get_message_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_message_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)
    monkeypatch.setattr(notifications, "_send_push_payloads", send_push)

    class FakeClient:
        responses = [
            {"success": True, "conversations": [{"id": "c-1", "sender": "Lehrkraft", "subject": "Erste", "unread": True}]},
            {"success": True, "conversations": [{"id": "c-1", "sender": "Lehrkraft", "subject": "Aktuell", "unread": True}]},
            {"success": True, "conversations": [{"id": "c-1", "sender": "Lehrkraft", "subject": "Neu", "unread": True}]},
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
    timezone = ZoneInfo("Europe/Berlin")

    async def get_client(_user_id):
        return SimpleNamespace(client=FakeClient())

    async def scenario():
        assert not await notifications.check_user_messages(
            user,
            get_client,
            datetime(2026, 8, 21, 10, 0, tzinfo=timezone),
            get_preferences=get_preferences,
        )
        assert await notifications.check_user_messages(
            user,
            get_client,
            datetime(2026, 8, 21, 10, 16, tzinfo=timezone),
            get_preferences=get_preferences,
        )
        assert not await notifications.check_user_messages(
            user,
            get_client,
            datetime(2026, 8, 21, 10, 32, tzinfo=timezone),
            get_preferences=get_preferences,
        )

    asyncio.run(scenario())
    assert sent_bodies == ["Du hast neue Nachrichten."]


def test_message_poll_reloads_baseline_after_preferences_recheck(monkeypatch):
    state = None
    preference_checks = 0
    sent_payloads = []

    async def get_state(_user_id):
        return state

    async def save_state(_user_id, snapshot):
        nonlocal state
        state = snapshot

    async def get_subscriptions(_user_id):
        return [{"endpoint": "https://push.example/subscription", "keys": {}}]

    async def send_push(_user_id, deliveries):
        sent_payloads.extend(deliveries.values())
        return {endpoint: "delivered" for endpoint in deliveries}

    async def get_preferences(_user_id):
        nonlocal preference_checks, state
        preference_checks += 1
        if preference_checks == 2:
            state = None
        return {
            "enabled": True,
            "start_time": "07:00",
            "end_time": "21:00",
            "poll_interval_minutes": 15,
            "timezone": "Europe/Berlin",
            "show_preview": True,
        }

    monkeypatch.setattr(notifications, "get_message_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_message_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)
    monkeypatch.setattr(notifications, "_send_push_payloads", send_push)

    class FakeClient:
        responses = [
            {"success": True, "conversations": [{"id": "c-1", "subject": "Erste", "unread": True}]},
            {"success": True, "conversations": [{"id": "c-1", "subject": "Aktuell", "unread": True}]},
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
    timezone = ZoneInfo("Europe/Berlin")

    async def get_client(_user_id):
        return SimpleNamespace(client=FakeClient())

    async def scenario():
        assert not await notifications.check_user_messages(
            user,
            get_client,
            datetime(2026, 8, 21, 10, 0, tzinfo=timezone),
            get_preferences=get_preferences,
        )
        assert not await notifications.check_user_messages(
            user,
            get_client,
            datetime(2026, 8, 21, 10, 16, tzinfo=timezone),
            get_preferences=get_preferences,
        )

    asyncio.run(scenario())
    assert sent_payloads == []


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


def test_message_poll_records_failed_attempt_before_the_next_interval(monkeypatch):
    state = None
    fetches = 0

    async def get_state(_user_id):
        return state

    async def save_state(_user_id, snapshot):
        nonlocal state
        state = snapshot

    async def get_subscriptions(_user_id):
        return [{"endpoint": "https://push.example/subscription", "keys": {}}]

    async def get_client(_user_id):
        nonlocal fetches
        fetches += 1
        raise RuntimeError("portal unavailable")

    monkeypatch.setattr(notifications, "get_message_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_message_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)

    user = {
        "user_id": "user-a",
        "enabled": True,
        "start_time": "07:00",
        "end_time": "21:00",
        "poll_interval_minutes": 15,
        "timezone": "Europe/Berlin",
        "show_preview": True,
    }
    timezone = ZoneInfo("Europe/Berlin")

    async def scenario():
        assert not await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 0, tzinfo=timezone)
        )
        assert not await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 1, tzinfo=timezone)
        )

    asyncio.run(scenario())
    assert fetches == 1
    assert state["checked_at"] == "2026-08-21T10:00:00+02:00"


def test_message_poll_advances_baseline_without_notifying_for_read_activity(monkeypatch):
    state = None
    delivery_attempts = []

    async def get_state(_user_id):
        return state

    async def save_state(_user_id, snapshot):
        nonlocal state
        state = snapshot

    async def get_subscriptions(_user_id):
        return [{"endpoint": "https://push.example/subscription", "keys": {}}]

    async def send_push(_user_id, deliveries):
        delivery_attempts.append(deliveries)
        return {endpoint: "delivered" for endpoint in deliveries}

    monkeypatch.setattr(notifications, "get_message_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_message_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)
    monkeypatch.setattr(notifications, "_send_push_payloads", send_push)

    class FakeClient:
        responses = [
            {"success": True, "conversations": [{"id": "c-1", "date": "10:00", "unread": False}]},
            {"success": True, "conversations": [{"id": "c-1", "date": "10:16", "unread": False}]},
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
    timezone = ZoneInfo("Europe/Berlin")

    async def get_client(_user_id):
        return SimpleNamespace(client=FakeClient())

    async def scenario():
        assert not await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 0, tzinfo=timezone)
        )
        assert not await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 16, tzinfo=timezone)
        )

    asyncio.run(scenario())
    assert delivery_attempts == []
    assert state["conversations"]["c-1"]


def test_message_poll_retries_only_the_device_that_failed(monkeypatch):
    state = None
    delivery_attempts = []
    delivery_payloads = []
    subscriptions = [
        {"endpoint": "https://push.example/one", "keys": {}},
        {"endpoint": "https://push.example/two", "keys": {}},
    ]

    async def get_state(_user_id):
        return state

    async def save_state(_user_id, snapshot):
        nonlocal state
        state = snapshot

    async def get_subscriptions(_user_id):
        return subscriptions

    async def send_push(_user_id, deliveries):
        delivery_attempts.append(set(deliveries))
        delivery_payloads.append(
            {endpoint: payload["body"] for endpoint, (_subscription, payload) in deliveries.items()}
        )
        if len(delivery_attempts) == 1:
            return {
                "https://push.example/one": "delivered",
                "https://push.example/two": "retry",
            }
        return {"https://push.example/two": "delivered"}

    monkeypatch.setattr(notifications, "get_message_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_message_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)
    monkeypatch.setattr(notifications, "_send_push_payloads", send_push)

    class FakeClient:
        responses = [
            {"success": True, "conversations": [{"id": "c-1", "date": "10:00", "unread": True}]},
            {"success": True, "conversations": [{"id": "c-1", "date": "10:16", "unread": True}]},
            {"success": True, "conversations": [{"id": "c-1", "date": "10:16", "unread": True}]},
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
    timezone = ZoneInfo("Europe/Berlin")

    async def get_client(_user_id):
        return SimpleNamespace(client=FakeClient())

    async def scenario():
        assert not await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 0, tzinfo=timezone)
        )
        assert await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 16, tzinfo=timezone)
        )
        user["show_preview"] = False
        assert not await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 32, tzinfo=timezone)
        )

    asyncio.run(scenario())
    assert delivery_attempts == [
        {"https://push.example/one", "https://push.example/two"},
        {"https://push.example/two"},
    ]
    assert delivery_payloads[1]["https://push.example/two"] == "Du hast neue Nachrichten."
    assert "pending_deliveries" not in state


def test_message_poll_retries_the_latest_payload_after_new_activity(monkeypatch):
    state = None
    sent_bodies = []
    endpoint = "https://push.example/subscription"

    async def get_state(_user_id):
        return state

    async def save_state(_user_id, snapshot):
        nonlocal state
        state = snapshot

    async def get_subscriptions(_user_id):
        return [{"endpoint": endpoint, "keys": {}}]

    async def send_push(_user_id, deliveries):
        sent_bodies.extend(payload["body"] for _subscription, payload in deliveries.values())
        return {endpoint: "retry" if len(sent_bodies) == 1 else "delivered"}

    monkeypatch.setattr(notifications, "get_message_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_message_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)
    monkeypatch.setattr(notifications, "_send_push_payloads", send_push)

    class FakeClient:
        responses = [
            {"success": True, "conversations": [{"id": "c-1", "sender": "Lehrkraft", "subject": "Erste", "unread": True}]},
            {"success": True, "conversations": [{"id": "c-1", "sender": "Lehrkraft", "subject": "Zwischenstand", "unread": True}]},
            {"success": True, "conversations": [{"id": "c-1", "sender": "Lehrkraft", "subject": "Aktuell", "unread": True}]},
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
    timezone = ZoneInfo("Europe/Berlin")

    async def get_client(_user_id):
        return SimpleNamespace(client=FakeClient())

    async def scenario():
        assert not await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 0, tzinfo=timezone)
        )
        assert await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 16, tzinfo=timezone)
        )
        assert await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 32, tzinfo=timezone)
        )

    asyncio.run(scenario())
    assert sent_bodies == ["Lehrkraft: Zwischenstand", "Lehrkraft: Aktuell"]
    assert "pending_deliveries" not in state


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
    monkeypatch.setattr(notifications, "is_trusted_push_endpoint", lambda _endpoint: True)
    monkeypatch.setattr(notifications, "run_in_threadpool", run_in_threadpool)

    async def scenario():
        return await notifications._send_push_to_subscriptions(
            "user-a", subscriptions, {"title": "Test"}
        )

    assert asyncio.run(scenario()) == {
        "https://push.example/one": "delivered",
        "https://push.example/two": "retry",
    }


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
    monkeypatch.setattr(notifications, "is_trusted_push_endpoint", lambda _endpoint: True)
    monkeypatch.setattr(notifications, "run_in_threadpool", run_in_threadpool)
    monkeypatch.setattr(notifications, "delete_push_subscription", delete_subscription)

    async def scenario():
        return await notifications._send_push_to_subscriptions(
            "user-a", subscriptions, {"title": "Test"}
        )

    assert asyncio.run(scenario()) == {
        "https://push.example/one": "gone",
        "https://push.example/two": "gone",
    }
    assert deleted == [("user-a", "https://push.example/one"), ("user-a", "https://push.example/two")]


def test_push_delivery_drops_untrusted_stored_endpoints(monkeypatch):
    deleted = []

    async def run_in_threadpool(*_args):
        raise AssertionError("untrusted endpoints must not reach the provider")

    async def delete_subscription(user_id, endpoint):
        deleted.append((user_id, endpoint))

    monkeypatch.setattr(notifications, "push_configured", lambda: True)
    monkeypatch.setattr(notifications, "run_in_threadpool", run_in_threadpool)
    monkeypatch.setattr(notifications, "delete_push_subscription", delete_subscription)

    statuses = asyncio.run(
        notifications._send_push_to_subscriptions(
            "user-a",
            [{"endpoint": "https://legacy.example/push", "keys": {}}],
            {"title": "Test"},
        )
    )

    assert statuses == {"https://legacy.example/push": "gone"}
    assert deleted == [("user-a", "https://legacy.example/push")]


def test_message_poll_persists_delivery_state_when_stale_cleanup_fails(monkeypatch):
    state = None
    sent_endpoints = []
    subscriptions = [
        {"endpoint": "https://push.example/one", "keys": {}},
        {"endpoint": "https://push.example/two", "keys": {}},
    ]

    class GoneError(Exception):
        def __init__(self):
            self.response = SimpleNamespace(status_code=410)

    async def get_state(_user_id):
        return state

    async def save_state(_user_id, snapshot):
        nonlocal state
        state = snapshot

    async def get_subscriptions(_user_id):
        return subscriptions

    async def run_in_threadpool(func, *args):
        if args and args[0] == "All":
            return func(*args)
        subscription, _payload = args
        sent_endpoints.append(subscription["endpoint"])
        return GoneError() if subscription["endpoint"].endswith("/two") else None

    async def delete_subscription(_user_id, _endpoint):
        raise RuntimeError("database temporarily unavailable")

    monkeypatch.setattr(notifications, "get_message_notification_state", get_state)
    monkeypatch.setattr(notifications, "save_message_notification_state", save_state)
    monkeypatch.setattr(notifications, "get_push_subscriptions", get_subscriptions)
    monkeypatch.setattr(notifications, "push_configured", lambda: True)
    monkeypatch.setattr(notifications, "is_trusted_push_endpoint", lambda _endpoint: True)
    monkeypatch.setattr(notifications, "run_in_threadpool", run_in_threadpool)
    monkeypatch.setattr(notifications, "delete_push_subscription", delete_subscription)

    class FakeClient:
        responses = [
            {"success": True, "conversations": [{"id": "c-1", "date": "10:00", "unread": True}]},
            {"success": True, "conversations": [{"id": "c-1", "date": "10:16", "unread": True}]},
            {"success": True, "conversations": [{"id": "c-1", "date": "10:16", "unread": True}]},
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
    timezone = ZoneInfo("Europe/Berlin")

    async def get_client(_user_id):
        return SimpleNamespace(client=FakeClient())

    async def scenario():
        assert not await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 0, tzinfo=timezone)
        )
        assert await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 16, tzinfo=timezone)
        )
        assert not await notifications.check_user_messages(
            user, get_client, datetime(2026, 8, 21, 10, 32, tzinfo=timezone)
        )

    asyncio.run(scenario())
    assert sent_endpoints == ["https://push.example/one", "https://push.example/two"]
    assert "pending_deliveries" not in state
