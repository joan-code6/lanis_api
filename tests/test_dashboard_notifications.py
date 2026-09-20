import asyncio
from types import SimpleNamespace

from api import api as api_module
from api import auth_db
from api.dashboard_notifications import dsb_plan_items, message_items, native_plan_items


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
        asyncio.run(auth_db.sync_dashboard_notifications("5201:Student", [item])) == []
    )

    including_read = asyncio.run(
        auth_db.sync_dashboard_notifications("5201:Student", [item], include_read=True)
    )
    assert len(including_read) == 1
    assert including_read[0]["read"] is True
    assert including_read[0]["read_at"]


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
                {"name": "DSBmobile", "url": "/dsb.php"},
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
    monkeypatch.setattr(api_module, "sync_dashboard_notifications", sync)

    result = asyncio.run(
        api_module.get_dashboard_notification_inbox(
            auth=SimpleNamespace(user_id="5201:student")
        )
    )
    assert result["success"] is True
    assert result["unread_count"] == 3
    assert result["source_counts"] == {"messages": 1, "native": 1, "dsb": 1}
    assert captured == {
        "user_id": "5201:student",
        "include_read": False,
        "limit": 10,
    }
