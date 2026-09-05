import asyncio
import hashlib
import hmac
import json
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from api import api as api_module
from api import auth_db
from api.whatsapp import (
    IncomingWhatsAppMessage,
    command_intent,
    extract_incoming_messages,
    format_exams,
    format_messages,
    format_substitutions,
    format_timetable,
    pairing_code,
    verify_webhook_signature,
)


def _request(body: bytes = b"", *, query: str = "", headers=None) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    raw_headers = [
        (name.lower().encode(), value.encode())
        for name, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST" if body else "GET",
            "scheme": "https",
            "path": "/whatsapp/webhook",
            "raw_path": b"/whatsapp/webhook",
            "query_string": query.encode(),
            "headers": raw_headers,
            "client": ("127.0.0.1", 1234),
            "server": ("test", 443),
        },
        receive,
    )


def test_signature_validation_uses_raw_webhook_body() -> None:
    body = b'{"object":"whatsapp_business_account"}'
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    assert verify_webhook_signature(body, signature, "secret") is True
    assert verify_webhook_signature(body + b" ", signature, "secret") is False
    assert verify_webhook_signature(body, "missing-prefix", "secret") is False


def test_webhook_verification_uses_constant_time_token_check(monkeypatch) -> None:
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-secret")
    request = _request(
        query="hub.mode=subscribe&hub.verify_token=verify-secret&hub.challenge=12345"
    )

    response = asyncio.run(api_module.verify_whatsapp_webhook(request))

    assert response.status_code == 200
    assert response.body == b"12345"


def test_webhook_verification_accepts_non_ascii_tokens(monkeypatch) -> None:
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "prüf-token")
    request = _request(
        query="hub.mode=subscribe&hub.verify_token=pr%C3%BCf-token&hub.challenge=ok"
    )

    response = asyncio.run(api_module.verify_whatsapp_webhook(request))

    assert response.status_code == 200
    assert response.body == b"ok"


def test_signed_webhook_is_queued_and_invalid_signature_is_rejected(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "app-secret")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "phone-1")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "access-token")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-token")
    monkeypatch.setenv("WHATSAPP_GRAPH_API_VERSION", "v99.0")
    monkeypatch.setenv("WHATSAPP_PUBLIC_NUMBER", "49123456789")
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-1"},
                            "messages": [
                                {
                                    "id": "wamid.route",
                                    "from": "4912345",
                                    "type": "text",
                                    "text": {"body": "Heute"},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    queued = []

    async def add_task(task):
        queued.append(task)

    monkeypatch.setattr(api_module.task_queue, "add_task", add_task)

    accepted = asyncio.run(
        api_module.receive_whatsapp_webhook(
            _request(body, headers={"X-Hub-Signature-256": signature})
        )
    )
    assert accepted == {"status": "accepted"}
    assert len(queued) == 1
    assert queued[0].args[0].text == "Heute"

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(
            api_module.receive_whatsapp_webhook(
                _request(body, headers={"X-Hub-Signature-256": "sha256=bad"})
            )
        )
    assert rejected.value.status_code == 403


def test_extract_messages_accepts_supported_interactions_for_own_number() -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-1"},
                            "messages": [
                                {
                                    "id": "wamid.text",
                                    "from": "4912345",
                                    "type": "text",
                                    "text": {"body": "Morgen"},
                                },
                                {
                                    "id": "wamid.button",
                                    "from": "4912345",
                                    "type": "interactive",
                                    "interactive": {
                                        "button_reply": {"id": "vertretung"}
                                    },
                                },
                            ],
                        }
                    }
                ]
            }
        ],
    }

    messages = extract_incoming_messages(payload, "phone-1")

    assert [(message.message_id, message.text) for message in messages] == [
        ("wamid.text", "Morgen"),
        ("wamid.button", "vertretung"),
    ]
    assert extract_incoming_messages(payload, "another-phone") == []


def test_command_router_is_deterministic_and_pairing_is_explicit() -> None:
    assert command_intent("Was habe ich morgen?") == "tomorrow"
    assert command_intent("Gibt es heute einen Ausfall?") == "substitutions"
    assert command_intent("Zeig meine ungelesenen Nachrichten") == "messages"
    assert command_intent("Bitte abmelden") == "unlink"
    assert command_intent("Was kannst du?") == "help"
    assert pairing_code("LANIS ABCD-2345") == "ABCD-2345"
    assert pairing_code("mein Code ist ABCD-2345") is None


def test_pairing_is_single_use_and_replaces_previous_phone(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", str(db_path))
    asyncio.run(auth_db.initialize())

    code, _ = asyncio.run(auth_db.create_whatsapp_pairing_code("5201:Student"))
    assert (
        asyncio.run(auth_db.consume_whatsapp_pairing_code(code, "491111111111"))
        == "5201:student"
    )
    assert (
        asyncio.run(auth_db.consume_whatsapp_pairing_code(code, "492222222222")) is None
    )

    sender_link = asyncio.run(auth_db.get_whatsapp_link_for_sender("491111111111"))
    assert sender_link is not None
    assert sender_link["user_id"] == "5201:student"
    assert sender_link["phone_suffix"] == "1111"
    assert sender_link["show_message_previews"] is False

    with sqlite3.connect(db_path) as db:
        stored_hash, suffix = db.execute(
            "SELECT whatsapp_id_hash, phone_suffix FROM whatsapp_links"
        ).fetchone()
    assert stored_hash != "491111111111"
    assert len(stored_hash) == 64
    assert suffix == "1111"

    replacement, _ = asyncio.run(auth_db.create_whatsapp_pairing_code("5201:STUDENT"))
    asyncio.run(auth_db.consume_whatsapp_pairing_code(replacement, "492222222222"))
    assert asyncio.run(auth_db.get_whatsapp_link_for_sender("491111111111")) is None
    assert asyncio.run(auth_db.get_whatsapp_link_for_sender("492222222222")) is not None


def test_whatsapp_preferences_and_message_deduplication(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    asyncio.run(auth_db.initialize())
    code, _ = asyncio.run(auth_db.create_whatsapp_pairing_code("5201:student"))
    asyncio.run(auth_db.consume_whatsapp_pairing_code(code, "49123456789"))

    asyncio.run(
        auth_db.save_whatsapp_preferences("5201:STUDENT", show_message_previews=True)
    )
    link = asyncio.run(auth_db.get_whatsapp_link_for_user("5201:student"))
    assert link is not None
    assert link["show_message_previews"] is True

    assert asyncio.run(auth_db.reserve_whatsapp_message("wamid.1")) is True
    assert asyncio.run(auth_db.reserve_whatsapp_message("wamid.1")) is False
    assert asyncio.run(auth_db.allow_whatsapp_message("49123456789", limit=2)) is True
    assert asyncio.run(auth_db.allow_whatsapp_message("49123456789", limit=2)) is True
    assert asyncio.run(auth_db.allow_whatsapp_message("49123456789", limit=2)) is False
    assert asyncio.run(auth_db.allow_whatsapp_message("49987654321", limit=2)) is True

    asyncio.run(auth_db.delete_whatsapp_link("5201:student"))
    assert asyncio.run(auth_db.get_whatsapp_link_for_user("5201:student")) is None


def test_timetable_summary_prefers_personal_plan() -> None:
    result = {
        "success": True,
        "days": ["Montag", "Dienstag"],
        "plan_for_all": [[{"stunde": 1, "name": "Allgemein"}], []],
        "plan_for_own": [
            [
                {
                    "stunde": 1,
                    "duration": 2,
                    "name": "Mathematik",
                    "room": "A 12",
                    "teacher": "MW",
                }
            ],
            [],
        ],
    }

    summary = format_timetable(result, date(2026, 9, 7))

    assert "Montag · 07.09.2026" in summary
    assert "1–2. Mathematik · A 12 · MW" in summary
    assert "Allgemein" not in summary


def test_timetable_summary_uses_recurring_template_for_another_week() -> None:
    result = {
        "success": True,
        "week_start": "2026-09-07",
        "days": ["Montag"],
        "plan_for_own": [[{"stunde": 1, "name": "Einmalige Änderung"}]],
        "template_plan_for_own": [[{"stunde": 1, "name": "Mathematik"}]],
        "plan_for_all": [],
        "template_plan_for_all": [],
    }

    summary = format_timetable(result, date(2026, 9, 14))

    assert "Mathematik" in summary
    assert "Einmalige Änderung" not in summary


def test_stop_bypasses_rate_limit(monkeypatch) -> None:
    sent = []
    deleted = []

    async def reserve(_message_id):
        return True

    async def rate_limit(_sender_id):
        raise AssertionError("STOP must not consult the ordinary rate limit")

    async def delete(sender_id):
        deleted.append(sender_id)

    class Client:
        def __init__(self, _config):
            pass

        async def send_text(self, sender_id, body):
            sent.append((sender_id, body))

    monkeypatch.setattr(api_module, "reserve_whatsapp_message", reserve)
    monkeypatch.setattr(api_module, "allow_whatsapp_message", rate_limit)
    monkeypatch.setattr(api_module, "delete_whatsapp_link_for_sender", delete)
    monkeypatch.setattr(api_module, "WhatsAppCloudClient", Client)
    monkeypatch.setattr(api_module, "_whatsapp_config", lambda: SimpleNamespace())

    asyncio.run(
        api_module._process_whatsapp_message(
            IncomingWhatsAppMessage("wamid.stop", "49123456789", "STOP")
        )
    )

    assert deleted == ["49123456789"]
    assert sent and "getrennt" in sent[0][1]


def test_personal_response_is_suppressed_after_unlink(monkeypatch) -> None:
    sent = []
    links = [
        {
            "user_id": "5201:student",
            "linked_at": "2026-09-05 12:00:00",
            "show_message_previews": False,
        },
        None,
    ]

    async def true_result(*_args, **_kwargs):
        return True

    async def get_link(_sender_id):
        return links.pop(0)

    async def get_client(_user_id):
        return SimpleNamespace(client=object(), school_id="5201", username="Student")

    async def response(*_args, **_kwargs):
        return "persönliche Schuldaten"

    class Client:
        def __init__(self, _config):
            pass

        async def send_text(self, sender_id, body):
            sent.append((sender_id, body))

    monkeypatch.setattr(api_module, "reserve_whatsapp_message", true_result)
    monkeypatch.setattr(api_module, "allow_whatsapp_message", true_result)
    monkeypatch.setattr(api_module, "get_whatsapp_link_for_sender", get_link)
    monkeypatch.setattr(api_module.sessions, "_get_or_create_schulportal_client", get_client)
    monkeypatch.setattr(api_module, "_whatsapp_command_response", response)
    monkeypatch.setattr(api_module, "WhatsAppCloudClient", Client)
    monkeypatch.setattr(api_module, "_whatsapp_config", lambda: SimpleNamespace())

    asyncio.run(
        api_module._process_whatsapp_message(
            IncomingWhatsAppMessage("wamid.today", "49123456789", "Heute")
        )
    )

    assert sent == []


def test_sensitive_previews_default_to_counts_only() -> None:
    messages = {
        "success": True,
        "conversations": [
            {"unread": 1, "SenderName": "Teacher", "Betreff": "Private"},
            {"unread": 0, "SenderName": "Other", "Betreff": "Read"},
        ],
    }

    private = format_messages(messages)
    preview = format_messages(messages, show_preview=True)

    assert "1 ungelesene" in private
    assert "Teacher" not in private
    assert "Private" not in private
    assert "Teacher: Private" in preview


def test_exam_summary_filters_past_dates_and_sorts_upcoming() -> None:
    result = {
        "success": True,
        "exams": [
            {"course_name": "Alt", "date": "2026-09-01"},
            {"course_name": "Später", "date": "2026-09-20"},
            {"course_name": "Zuerst", "date": "2026-09-10"},
        ],
    }

    summary = format_exams(result, date(2026, 9, 5))

    assert "Alt" not in summary
    assert summary.index("Zuerst") < summary.index("Später")


def test_substitutions_are_scoped_to_the_profile_class() -> None:
    result = {
        "success": True,
        "days": [
            {
                "date": "2026-09-07",
                "substitutions": [
                    {"klasse": "10a", "stunde": "1", "fach": "Mathe", "art": "Entfall"},
                    {
                        "klasse": "9b",
                        "stunde": "2",
                        "fach": "Deutsch",
                        "art": "Vertretung",
                    },
                ],
            }
        ],
    }

    summary = format_substitutions(result, "10a")

    assert "Mathe: Entfall" in summary
    assert "Deutsch" not in summary
