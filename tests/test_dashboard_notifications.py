import asyncio
import sqlite3
from types import SimpleNamespace

from api import api as api_module
from api import auth_db
from api.dashboard_notifications import (
    _sortable_datetime,
    dsb_plan_items,
    message_items,
    native_plan_items,
)


def test_portal_datetime_sorting_preserves_clock_time() -> None:
    assert _sortable_datetime("20.09.2026 10:16").endswith("10:16:00+00:00")
    assert _sortable_datetime("10:16").endswith("10:16:00+00:00")


def test_builders_return_only_unread_and_class_relevant_items() -> None:
    messages = message_items(
        {
            "conversations": [
                {
                    "Uniquid": "one",
                    "Id": "m1",
                    "Betreff": "Neu",
                    "Sender": "A",
                    "unread": 1,
                    "date": "2026-09-20",
                },
                {
                    "Uniquid": "two",
                    "Id": "m2",
                    "Betreff": "Alt",
                    "Sender": "B",
                    "unread": 0,
                    "date": "2026-09-19",
                },
            ]
        }
    )
    assert [item["title"] for item in messages] == ["Neu"]
    assert messages[0]["source"] == "messages"

    native = native_plan_items(
        {
            "days": [
                {
                    "date": "20.09.2026",
                    "substitutions": [
                        {
                            "klasse": "10 A",
                            "stunde": "2",
                            "fach": "Mathe",
                            "art": "Vertretung",
                        },
                        {
                            "klasse": "9 B",
                            "stunde": "3",
                            "fach": "Deutsch",
                            "art": "Entfall",
                        },
                    ],
                }
            ]
        },
        "10A",
    )
    assert len(native) == 1
    assert native[0]["title"] == "Vertretung · Mathe"

    dsb = dsb_plan_items(
        {
            "tables": [
                {
                    "caption": "Klasse 10 A — Vertretungsplan",
                    "date": "20.09.2026",
                    "headers": ["Stunde", "Fach", "Info"],
                    "rows": [
                        {"Stunde": "4", "Fach": "Englisch", "Info": "Raumänderung"}
                    ],
                }
            ]
        },
        "10A",
    )
    assert len(dsb) == 1
    assert dsb[0]["detail"].startswith("10A · 4. Std.")


def test_class_filter_requires_exact_tokens_and_checks_previous_class() -> None:
    result = {
        "days": [
            {
                "date": "20.09.2026",
                "substitutions": [
                    {"klasse": "11 A", "fach": "Falsch", "art": "Vertretung"},
                    {
                        "klasse": "10 B",
                        "klasse_alt": "1 A",
                        "fach": "Richtig",
                        "art": "Vertretung",
                    },
                ],
            }
        ]
    }

    assert [item["title"] for item in native_plan_items(result, "1A")] == [
        "Vertretung · Richtig"
    ]
    assert native_plan_items(result, "") == []


def test_dashboard_notification_read_state_is_persisted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    asyncio.run(auth_db.initialize())
    item = {
        "id": "messages:stable",
        "source": "messages",
        "title": "Neue Nachricht",
        "detail": "Sekretariat",
        "meta": "20.09.2026",
        "path": "/messages",
    }

    initial = asyncio.run(auth_db.sync_dashboard_notifications("5201:Student", [item]))
    assert len(initial) == 1
    assert initial[0]["read"] is False
    assert initial[0]["created_at"]

    updated = asyncio.run(
        auth_db.mark_dashboard_notifications_read("5201:STUDENT", ["messages:stable"])
    )
    assert updated == 1
    assert (
        asyncio.run(
            auth_db.mark_dashboard_notifications_read(
                "5201:STUDENT", ["messages:stable"]
            )
        )
        == 0
    )
    assert asyncio.run(auth_db.mark_dashboard_notifications_read("5201:STUDENT")) == 0
    assert (
        asyncio.run(auth_db.sync_dashboard_notifications("5201:Student", [item])) == []
    )

    including_read = asyncio.run(
        auth_db.sync_dashboard_notifications("5201:Student", [item], include_read=True)
    )
    assert len(including_read) == 1
    assert including_read[0]["read"] is True
    assert including_read[0]["read_at"]


def test_read_all_can_be_scoped_to_enabled_sources(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    asyncio.run(auth_db.initialize())
    items = [
        {"id": "messages:one", "source": "messages", "title": "One"},
        {"id": "native:one", "source": "native", "title": "Two"},
    ]
    asyncio.run(auth_db.sync_dashboard_notifications("user-a", items))

    assert (
        asyncio.run(
            auth_db.mark_dashboard_notifications_read(
                "user-a", sources=["messages"]
            )
        )
        == 1
    )
    remaining = asyncio.run(
        auth_db.sync_dashboard_notifications("user-a", items, include_read=True)
    )
    assert [item["id"] for item in remaining if not item["read"]] == ["native:one"]


def test_empty_inbox_still_removes_expired_rows(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", str(database_path))
    asyncio.run(auth_db.initialize())
    with sqlite3.connect(database_path) as db:
        db.execute(
            """
            INSERT INTO dashboard_notifications
                (user_id, notification_id, source, payload, last_seen_at)
            VALUES (?, ?, ?, ?, datetime('now', '-46 days'))
            """,
            ("user-a", "expired", "messages", "{}"),
        )
        db.commit()

    assert asyncio.run(auth_db.sync_dashboard_notifications("user-a", [])) == []
    with sqlite3.connect(database_path) as db:
        count = db.execute(
            "SELECT COUNT(*) FROM dashboard_notifications WHERE user_id = ?",
            ("user-a",),
        ).fetchone()[0]
    assert count == 0


def test_new_revision_gets_fresh_unread_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    asyncio.run(auth_db.initialize())
    first = {"id": "messages:first", "source": "messages", "title": "First"}
    second = {"id": "messages:second", "source": "messages", "title": "Second"}
    asyncio.run(auth_db.sync_dashboard_notifications("user-a", [first]))
    asyncio.run(auth_db.mark_dashboard_notifications_read("user-a", [first["id"]]))

    current = asyncio.run(
        auth_db.sync_dashboard_notifications("user-a", [first, second])
    )
    assert [item["id"] for item in current] == [second["id"]]


def test_dashboard_inbox_aggregates_enabled_backend_sources(monkeypatch) -> None:
    async def preferences(_user_id):
        return (
            {
                "dashboard": {
                    "notifications_enabled": True,
                    "notification_messages_enabled": True,
                    "notification_native_enabled": True,
                    "notification_dsb_enabled": True,
                    "notification_show_read": False,
                    "notification_limit": 10,
                },
                "vertretungsplan": {"class_override": ""},
            },
            True,
        )

    async def modules(auth):
        del auth
        return {
            "success": True,
            "modules": [
                {"name": "Nachrichten", "url": "/nachrichten.php"},
                {"name": "Vertretungsplan", "url": "/vertretungsplan.php"},
            ],
        }

    async def profile(auth):
        del auth
        return {"success": True, "data": {"klasse": "10 A"}}

    async def messages(get_type, last, auth):
        del get_type, last, auth
        return {
            "success": True,
            "conversations": [
                {"Uniquid": "one", "Id": "m1", "Betreff": "Neu", "unread": 1},
            ],
        }

    async def native(include_raw, refresh, auth):
        del include_raw, refresh, auth
        return {
            "success": True,
            "days": [
                {
                    "date": "2026-09-20",
                    "substitutions": [
                        {"klasse": "10 A", "fach": "Mathe", "art": "Vertretung"},
                    ],
                }
            ],
        }

    async def dsb(refresh, auth):
        del refresh, auth
        return {
            "success": True,
            "tables": [
                {
                    "caption": "Klasse 10 A",
                    "headers": ["Fach"],
                    "rows": [{"Fach": "Englisch"}],
                }
            ],
        }

    captured = {}

    async def sync(user_id, items, include_read, limit):
        captured.update(user_id=user_id, include_read=include_read, limit=limit)
        return [
            {**item, "created_at": "2026-09-20", "read": False, "read_at": None}
            for item in items
        ]

    monkeypatch.setattr(api_module, "get_user_preferences", preferences)
    monkeypatch.setattr(api_module, "get_modules", modules)
    monkeypatch.setattr(api_module, "get_user_data", profile)
    monkeypatch.setattr(api_module, "get_message_headers", messages)
    monkeypatch.setattr(api_module, "get_vertretungsplan", native)
    monkeypatch.setattr(api_module, "get_school_dsb_plan", dsb)
    monkeypatch.setattr(
        api_module, "_school_dsb_credentials", lambda school_id: ("user", "password")
    )
    monkeypatch.setattr(api_module, "sync_dashboard_notifications", sync)

    result = asyncio.run(
        api_module.get_dashboard_notification_inbox(
            auth=SimpleNamespace(user_id="5201:student", school_id="5201")
        )
    )
    assert result["success"] is True
    assert result["unread_count"] == 3
    assert result["source_counts"] == {"messages": 1, "native": 1, "dsb": 1}
    assert captured == {
        "user_id": "5201:student",
        "include_read": False,
        "limit": 600,
    }


def test_dashboard_inbox_counts_all_active_items_before_display_limit(
    monkeypatch,
) -> None:
    async def preferences(_user_id):
        return (
            {
                "dashboard": {
                    "notifications_enabled": True,
                    "notification_messages_enabled": True,
                    "notification_native_enabled": False,
                    "notification_dsb_enabled": False,
                    "notification_show_read": False,
                    "notification_limit": 5,
                },
                "vertretungsplan": {"class_override": ""},
            },
            True,
        )

    async def modules(auth):
        del auth
        return {
            "success": True,
            "modules": [{"name": "Nachrichten", "url": "/nachrichten.php"}],
        }

    async def messages(get_type, last, auth):
        del get_type, last, auth
        return {"success": True, "conversations": []}

    async def sync(user_id, items, include_read, limit):
        del user_id, items, include_read, limit
        return [
            {
                "id": f"messages:{index}",
                "source": "messages",
                "read": False,
            }
            for index in range(12)
        ]

    monkeypatch.setattr(api_module, "get_user_preferences", preferences)
    monkeypatch.setattr(api_module, "get_modules", modules)
    monkeypatch.setattr(api_module, "get_message_headers", messages)
    monkeypatch.setattr(api_module, "sync_dashboard_notifications", sync)

    result = asyncio.run(
        api_module.get_dashboard_notification_inbox(
            auth=SimpleNamespace(user_id="5201:student", school_id="5201")
        )
    )
    assert len(result["notifications"]) == 5
    assert result["unread_count"] == 12
    assert result["source_counts"] == {"messages": 12, "native": 0, "dsb": 0}
