import asyncio
import hashlib
import hmac
import json
import sqlite3
from collections import deque
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
    confirmation_code,
    extract_incoming_messages,
    format_exams,
    format_messages,
    format_substitutions,
    format_timetable,
    pairing_code,
    split_message,
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

    async def enqueue(incoming):
        queued.append(incoming)

    monkeypatch.setattr(api_module, "_enqueue_whatsapp_message", enqueue)

    accepted = asyncio.run(
        api_module.receive_whatsapp_webhook(
            _request(body, headers={"X-Hub-Signature-256": signature})
        )
    )
    assert accepted == {"status": "accepted"}
    assert len(queued) == 1
    assert queued[0].text == "Heute"

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
    assert confirmation_code("BESTÄTIGEN ABC234") == "ABC234"
    assert confirmation_code("yes ABC234") is None


def test_ai_history_is_encrypted_bounded_and_deleted_on_unlink(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", str(db_path))
    asyncio.run(auth_db.initialize())
    code, _ = asyncio.run(auth_db.create_whatsapp_pairing_code("5201:student"))
    asyncio.run(auth_db.consume_whatsapp_pairing_code(code, "491111111111"))
    history = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"message {index}"}
        for index in range(20)
    ]

    asyncio.run(auth_db.save_whatsapp_ai_history("5201:student", history))
    loaded = asyncio.run(auth_db.get_whatsapp_ai_history("5201:student"))

    assert len(loaded) == auth_db.WHATSAPP_AI_HISTORY_MESSAGES
    with sqlite3.connect(db_path) as db:
        encrypted = db.execute(
            "SELECT encrypted_history FROM whatsapp_ai_conversations"
        ).fetchone()[0]
    assert encrypted.startswith("v1:")
    assert "message 19" not in encrypted

    asyncio.run(auth_db.delete_whatsapp_link_for_sender("491111111111"))
    assert asyncio.run(auth_db.get_whatsapp_ai_history("5201:student")) == []


def test_rapid_relink_uses_a_new_generation_and_cannot_restore_old_history(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    asyncio.run(auth_db.initialize())
    first_code, _ = asyncio.run(auth_db.create_whatsapp_pairing_code("5201:student"))
    asyncio.run(auth_db.consume_whatsapp_pairing_code(first_code, "491111111111"))
    first_link = asyncio.run(auth_db.get_whatsapp_link_for_sender("491111111111"))

    second_code, _ = asyncio.run(auth_db.create_whatsapp_pairing_code("5201:student"))
    asyncio.run(auth_db.consume_whatsapp_pairing_code(second_code, "491111111111"))
    second_link = asyncio.run(auth_db.get_whatsapp_link_for_sender("491111111111"))

    assert first_link is not None and second_link is not None
    assert first_link["linked_at"] != second_link["linked_at"]
    asyncio.run(
        auth_db.save_whatsapp_ai_history(
            "5201:student",
            [{"role": "assistant", "content": "old private response"}],
            expected_link_generation=first_link["linked_at"],
        )
    )
    with sqlite3.connect(auth_db.DB_PATH) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM whatsapp_ai_conversations"
        ).fetchone()[0] == 0


def test_pending_action_is_single_use_and_bound_to_sender(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    asyncio.run(auth_db.initialize())
    action_code = asyncio.run(
        auth_db.create_whatsapp_pending_action(
            "5201:student",
            "491111111111",
            "mark_message_read",
            {"conversation_id": "conversation-1"},
        )
    )

    assert asyncio.run(
        auth_db.consume_whatsapp_pending_action("492222222222", action_code)
    ) is None
    pending = asyncio.run(
        auth_db.consume_whatsapp_pending_action("491111111111", action_code)
    )
    assert pending == {
        "user_id": "5201:student",
        "action": "mark_message_read",
        "payload": {"conversation_id": "conversation-1"},
    }
    assert asyncio.run(
        auth_db.consume_whatsapp_pending_action("491111111111", action_code)
    ) is None


def test_pending_action_requires_the_same_active_link_generation(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    asyncio.run(auth_db.initialize())
    pairing_code, _ = asyncio.run(auth_db.create_whatsapp_pairing_code("5201:student"))
    asyncio.run(auth_db.consume_whatsapp_pairing_code(pairing_code, "491111111111"))
    link = asyncio.run(auth_db.get_whatsapp_link_for_sender("491111111111"))
    assert link is not None
    asyncio.run(auth_db.delete_whatsapp_link_for_sender("491111111111"))

    with pytest.raises(LookupError, match="no longer active"):
        asyncio.run(
            auth_db.create_whatsapp_pending_action(
                "5201:student",
                "491111111111",
                "mark_message_read",
                {"conversation_id": "conversation-1"},
                expected_link_generation=link["linked_at"],
            )
        )

    with sqlite3.connect(auth_db.DB_PATH) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM whatsapp_pending_actions"
        ).fetchone()[0] == 0


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

    asyncio.run(
        auth_db.save_whatsapp_ai_history(
            "5201:student",
            [{"role": "assistant", "content": "Teacher: Private subject"}],
        )
    )
    asyncio.run(
        auth_db.save_whatsapp_preferences("5201:student", show_message_previews=False)
    )
    assert asyncio.run(auth_db.get_whatsapp_ai_history("5201:student")) == []
    asyncio.run(
        auth_db.save_whatsapp_ai_history(
            "5201:student",
            [{"role": "assistant", "content": "stale private preview"}],
            require_message_previews=True,
        )
    )
    assert asyncio.run(auth_db.get_whatsapp_ai_history("5201:student")) == []

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
    monkeypatch.setattr(
        api_module, "_ai_config", lambda: SimpleNamespace(configured=False)
    )

    asyncio.run(
        api_module._process_whatsapp_message(
            IncomingWhatsAppMessage("wamid.stop", "49123456789", "STOP")
        )
    )

    assert deleted == ["49123456789"]
    assert sent and "getrennt" in sent[0][1]


def test_stop_clears_pending_sender_turns_before_ai_processing(monkeypatch) -> None:
    deleted = []
    sender_id = "49123456789"
    sender_key = hashlib.sha256(sender_id.encode()).hexdigest()
    api_module._whatsapp_sender_queues.clear()
    api_module._whatsapp_pending_messages = 0
    api_module._whatsapp_sender_queues[sender_key] = api_module._WhatsAppSenderQueue(
        deque(
            [IncomingWhatsAppMessage("wamid.queued", sender_id, "private question")]
        )
    )
    api_module._whatsapp_pending_messages = 1

    async def reserve(_message_id):
        return True

    async def delete(sender):
        deleted.append(sender)

    class Client:
        def __init__(self, _config):
            pass

        async def send_text(self, *_args):
            pass

    monkeypatch.setattr(api_module, "reserve_whatsapp_message", reserve)
    monkeypatch.setattr(api_module, "delete_whatsapp_link_for_sender", delete)
    monkeypatch.setattr(api_module, "WhatsAppCloudClient", Client)
    monkeypatch.setattr(api_module, "_whatsapp_config", lambda: SimpleNamespace())

    asyncio.run(
        api_module._process_whatsapp_stop_immediately(
            IncomingWhatsAppMessage("wamid.stop-immediate", sender_id, "STOP")
        )
    )

    assert deleted == [sender_id]
    assert not api_module._whatsapp_sender_queues[sender_key].messages
    assert api_module._whatsapp_pending_messages == 0
    api_module._whatsapp_sender_queues.clear()


def test_stop_retains_confirmation_task_until_send_finishes(monkeypatch) -> None:
    release = None

    async def reserve(_message_id):
        return True

    async def delete(_sender_id):
        return None

    class Client:
        def __init__(self, _config):
            pass

        async def send_text(self, *_args):
            await release.wait()

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        api_module._whatsapp_unlink_confirmation_tasks.clear()
        await api_module._process_whatsapp_stop_immediately(
            IncomingWhatsAppMessage("wamid.stop-retained", "49123456789", "STOP")
        )
        await asyncio.sleep(0)
        assert len(api_module._whatsapp_unlink_confirmation_tasks) == 1
        release.set()
        await asyncio.gather(*api_module._whatsapp_unlink_confirmation_tasks)
        await asyncio.sleep(0)
        assert not api_module._whatsapp_unlink_confirmation_tasks

    monkeypatch.setattr(api_module, "reserve_whatsapp_message", reserve)
    monkeypatch.setattr(api_module, "delete_whatsapp_link_for_sender", delete)
    monkeypatch.setattr(api_module, "WhatsAppCloudClient", Client)
    monkeypatch.setattr(api_module, "_whatsapp_config", lambda: SimpleNamespace())
    asyncio.run(scenario())


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
    monkeypatch.setattr(
        api_module, "_ai_config", lambda: SimpleNamespace(configured=False)
    )

    asyncio.run(
        api_module._process_whatsapp_message(
            IncomingWhatsAppMessage("wamid.today", "49123456789", "Heute")
        )
    )

    assert sent == []


def test_ai_path_marks_incoming_message_as_read_before_answering(monkeypatch) -> None:
    events = []
    sent = []
    link = {
        "user_id": "5201:student",
        "linked_at": "2026-09-05 12:00:00",
        "show_message_previews": False,
    }

    async def true_result(*_args, **_kwargs):
        return True

    async def get_link(_sender_id):
        return link

    async def get_client(_user_id):
        return SimpleNamespace(client=object(), school_id="5201", username="Student")

    async def ai_response(incoming, _auth, _link):
        events.append(("ai", incoming.message_id))
        return "Antwort"

    class Client:
        def __init__(self, _config):
            pass

        async def mark_read(self, message_id):
            events.append(("read", message_id))

        async def send_text(self, sender_id, body, **_kwargs):
            sent.append((sender_id, body))

    monkeypatch.setattr(api_module, "reserve_whatsapp_message", true_result)
    monkeypatch.setattr(api_module, "allow_whatsapp_message", true_result)
    monkeypatch.setattr(api_module, "get_whatsapp_link_for_sender", get_link)
    monkeypatch.setattr(api_module.sessions, "_get_or_create_schulportal_client", get_client)
    monkeypatch.setattr(api_module, "_whatsapp_ai_response", ai_response)
    monkeypatch.setattr(api_module, "WhatsAppCloudClient", Client)
    monkeypatch.setattr(api_module, "_whatsapp_config", lambda: SimpleNamespace())
    monkeypatch.setattr(
        api_module, "_ai_config", lambda: SimpleNamespace(configured=True)
    )

    asyncio.run(
        api_module._process_whatsapp_message(
            IncomingWhatsAppMessage("wamid.ai", "49123456789", "Was habe ich morgen?")
        )
    )

    assert events == [("read", "wamid.ai"), ("ai", "wamid.ai")]
    assert sent == [("49123456789", "Antwort")]


def test_whatsapp_client_sends_read_receipt_payload() -> None:
    client = api_module.WhatsAppCloudClient(SimpleNamespace(configured=True))
    payloads = []

    async def post(payload):
        payloads.append(payload)

    client._post = post
    asyncio.run(client.mark_read("wamid.123"))

    assert payloads == [
        {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": "wamid.123",
        }
    ]


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


def test_write_confirmation_preserves_the_complete_message_body() -> None:
    body = "Anfang " + ("x" * 5000) + " Ende"
    preview = api_module._whatsapp_action_preview(
        "send_message",
        {"recipients": ["teacher-1"], "subject": "Betreff", "body": body},
    )
    confirmation = (
        "🔐 *Verbindliche Bestätigung*\n"
        f"{preview}\n\nBESTÄTIGEN ABC123"
    )
    chunks = split_message(confirmation)

    assert body in preview
    assert " Ende" in preview
    assert "".join(chunks) == confirmation
    assert all(len(chunk) <= 3900 for chunk in chunks)
    assert chunks[-1].endswith("BESTÄTIGEN ABC123")


def test_reply_confirmation_includes_recipient() -> None:
    preview = api_module._whatsapp_action_preview(
        "reply_message",
        {"to": "conversation-member-1", "conversation_id": "conversation-1", "body": "Hallo"},
    )
    assert "conversation-member-1" in preview


def test_whatsapp_client_sends_every_confirmation_chunk() -> None:
    body = "payload:" + ("x" * 5000) + "\nBESTÄTIGEN ABC123"
    client = api_module.WhatsAppCloudClient(SimpleNamespace())
    sent = []

    async def send(_recipient, payload):
        sent.append(payload["text"]["body"])

    client._send = send
    asyncio.run(client.send_text("49123456789", body))

    assert "".join(sent) == body
    assert sent[-1].endswith("BESTÄTIGEN ABC123")


def test_whatsapp_client_rechecks_link_before_each_chunk() -> None:
    body = "payload:" + ("x" * 5000)
    client = api_module.WhatsAppCloudClient(SimpleNamespace())
    sent = []

    async def send(_recipient, payload):
        sent.append(payload["text"]["body"])

    checks = iter([True, False])

    async def still_linked():
        return next(checks)

    client._send = send
    asyncio.run(client.send_text("49123456789", body, continuation_check=still_linked))

    assert len(sent) == 1
    assert "".join(sent) != body


def test_sender_queue_uses_one_worker_and_preserves_turn_order(monkeypatch) -> None:
    sender = "49123456789"
    sender_key = hashlib.sha256(sender.encode()).hexdigest()
    scheduled = []
    started = []

    async def add_task(task):
        scheduled.append(task)

    async def process(incoming):
        started.append(incoming.message_id)

    async def scenario():
        api_module._whatsapp_sender_queues.clear()
        api_module._whatsapp_pending_messages = 0
        await api_module._enqueue_whatsapp_message(
            IncomingWhatsAppMessage("first", sender, "one")
        )
        await api_module._enqueue_whatsapp_message(
            IncomingWhatsAppMessage("second", sender, "two")
        )
        assert len(scheduled) == 1
        assert api_module._whatsapp_queue_stats()["max_sender_queue_depth"] == 2

        await scheduled[0].func(*scheduled[0].args)
        assert started == ["first"]
        assert len(scheduled) == 2
        assert sender_key in api_module._whatsapp_sender_queues

        await scheduled[1].func(*scheduled[1].args)
        assert started == ["first", "second"]
        assert sender_key not in api_module._whatsapp_sender_queues
        assert api_module._whatsapp_pending_messages == 0

    monkeypatch.setattr(api_module.whatsapp_task_queue, "add_task", add_task)
    monkeypatch.setattr(api_module, "_process_whatsapp_message", process)
    asyncio.run(scenario())


def test_parallel_prepare_action_keeps_one_valid_confirmation(monkeypatch) -> None:
    created = []
    confirmations = []

    async def create(*args, **kwargs):
        created.append(args)
        await asyncio.sleep(0)
        return "ABC123"

    async def active_link(_sender_id):
        return {"user_id": "5201:student", "linked_at": None}

    async def scenario():
        tools = api_module._whatsapp_agent_tools(
            SimpleNamespace(user_id="5201:student"),
            {"show_message_previews": False},
            "49123456789",
            confirmations,
        )
        prepare = next(tool for tool in tools if tool.name == "prepare_action")
        arguments = {
            "action": "mark_message_read",
            "payload": {"conversation_id": "conversation-1"},
        }
        return await asyncio.gather(
            prepare.handler(arguments), prepare.handler(arguments)
        )

    monkeypatch.setattr(api_module, "create_whatsapp_pending_action", create)
    monkeypatch.setattr(api_module, "get_whatsapp_link_for_sender", active_link)
    results = asyncio.run(scenario())

    assert len(created) == 1
    assert len(confirmations) == 1
    assert sum(result["success"] is True for result in results) == 1
    assert any("Only one change" in result.get("error", "") for result in results)


def test_confirmation_preview_excludes_internal_link_generation(monkeypatch) -> None:
    confirmations = []

    async def create(*_args, **_kwargs):
        return "ABC123"

    async def active_link(_sender_id):
        return {"user_id": "5201:student", "linked_at": "generation-1"}

    monkeypatch.setattr(api_module, "create_whatsapp_pending_action", create)
    monkeypatch.setattr(api_module, "get_whatsapp_link_for_sender", active_link)
    tools = api_module._whatsapp_agent_tools(
        SimpleNamespace(user_id="5201:student"),
        {"show_message_previews": False, "linked_at": "generation-1"},
        "49123456789",
        confirmations,
    )
    prepare = next(tool for tool in tools if tool.name == "prepare_action")
    result = asyncio.run(
        prepare.handler(
            {
                "action": "update_preferences",
                "payload": {"appearance": {"theme_mode": "dark"}},
            }
        )
    )

    assert result["success"] is True
    assert "_link_generation" not in confirmations[0]["preview"]


def test_election_execution_preserves_link_generation_before_normalizing(
    monkeypatch,
) -> None:
    checks = []

    async def get_form(_election_id, _auth):
        return {"success": True, "personal_fields": [], "blocks": []}

    class Client:
        def wahlen_submit(
            self, _election_id, _submission, _confirmed, pre_submit_check
        ):
            checks.append(pre_submit_check())
            return {"success": True}

    def link_matches(sender_id, user_id, generation):
        assert sender_id == "49123456789"
        assert user_id == "5201:student"
        return generation == "generation-1"

    monkeypatch.setattr(api_module, "get_wahlen_form", get_form)
    monkeypatch.setattr(api_module, "whatsapp_link_matches_sync", link_matches)
    result = asyncio.run(
        api_module._execute_whatsapp_pending_action(
            {
                "action": "submit_election",
                "payload": {
                    "election_id": "18",
                    "fields": {},
                    "selections": {},
                    "_link_generation": "generation-1",
                },
            },
            SimpleNamespace(client=Client(), user_id="5201:student"),
            sender_id="49123456789",
        )
    )

    assert result["success"] is True
    assert checks == [True]


def test_detailed_course_homework_is_bound_to_parent_course(monkeypatch) -> None:
    confirmations = []

    async def get_course(_course_id, _auth):
        return {
            "success": True,
            "course_id": "course-a",
            "course_name": "Mathematik",
            "entries": [{"entry_id": "entry-1", "homework": "Aufgabe 1"}],
        }

    async def create(*_args, **_kwargs):
        return "ABC123"

    async def active_link(_sender_id):
        return {"user_id": "5201:student", "linked_at": "generation-1"}

    async def scenario():
        tools = api_module._whatsapp_agent_tools(
            SimpleNamespace(user_id="5201:student", client=object()),
            {"show_message_previews": False, "linked_at": "generation-1"},
            "49123456789",
            confirmations,
        )
        by_name = {tool.name: tool for tool in tools}
        await by_name["get_course"].handler({"course_id": "course-a"})
        return await by_name["prepare_action"].handler(
            {
                "action": "mark_homework_done",
                "payload": {
                    "course_id": "course-b",
                    "entry_id": "entry-1",
                    "done": True,
                },
            }
        )

    monkeypatch.setattr(api_module, "meinunterricht_course", get_course)
    monkeypatch.setattr(api_module, "create_whatsapp_pending_action", create)
    monkeypatch.setattr(api_module, "get_whatsapp_link_for_sender", active_link)
    rejected = asyncio.run(scenario())

    assert rejected["success"] is False
    assert "Load the course homework" in rejected["error"]
    assert confirmations == []


def test_course_entry_only_accepts_urls_returned_by_course_tools(monkeypatch) -> None:
    fetched = []

    async def overview(*_args, **_kwargs):
        return {
            "success": True,
            "entries": [
                {
                    "book_id": "course-1",
                    "course_link": "/meinunterricht.php?a=sus_view&id=course-1",
                }
            ],
        }

    async def entry(url, _auth):
        fetched.append(url)
        return {"success": True}

    async def active_link(_sender_id):
        return {"user_id": "5201:student", "linked_at": None}

    async def scenario():
        tools = api_module._whatsapp_agent_tools(
            SimpleNamespace(user_id="5201:student", client=object()),
            {"show_message_previews": False},
            "49123456789",
            [],
        )
        by_name = {tool.name: tool for tool in tools}
        rejected = await by_name["get_course_entry"].handler(
            {"url": "/index.php?logout=all"}
        )
        await by_name["get_courses"].handler({})
        allowed = await by_name["get_course_entry"].handler(
            {"url": "https://start.schulportal.hessen.de/meinunterricht.php?a=sus_view&id=course-1"}
        )
        return rejected, allowed

    monkeypatch.setattr(api_module, "meinunterricht_overview", overview)
    monkeypatch.setattr(api_module, "meinunterricht_entry", entry)
    monkeypatch.setattr(api_module, "get_whatsapp_link_for_sender", active_link)
    rejected, allowed = asyncio.run(scenario())

    assert rejected["success"] is False
    assert allowed["success"] is True
    assert fetched == ["/meinunterricht.php?a=sus_view&id=course-1"]


def test_message_actions_require_previews_for_target_verification(monkeypatch) -> None:
    async def active_link(_sender_id):
        return {"user_id": "5201:student", "linked_at": "generation-1"}

    monkeypatch.setattr(api_module, "get_whatsapp_link_for_sender", active_link)
    tools = api_module._whatsapp_agent_tools(
        SimpleNamespace(user_id="5201:student"),
        {
            "show_message_previews": False,
            "linked_at": "generation-1",
        },
        "49123456789",
        [],
        {},
    )
    prepare = next(tool for tool in tools if tool.name == "prepare_action")
    result = asyncio.run(
        prepare.handler(
            {
                "action": "mark_message_read",
                "payload": {"conversation_id": "conversation-1"},
            }
        )
    )

    assert result["success"] is False
    assert "Load the conversation first" in result["error"]


def test_tool_guards_fail_closed_when_link_table_is_missing(monkeypatch) -> None:
    async def missing_table(_sender_id):
        raise sqlite3.OperationalError("no such table: whatsapp_links")

    monkeypatch.setattr(api_module, "get_whatsapp_link_for_sender", missing_table)
    tools = api_module._whatsapp_agent_tools(
        SimpleNamespace(user_id="5201:student"),
        {"linked_at": "generation-1"},
        "49123456789",
        [],
    )
    profile = next(tool for tool in tools if tool.name == "get_profile")
    result = asyncio.run(profile.handler({}))

    assert result == {
        "success": False,
        "error": "WhatsApp connection is no longer active",
    }


def test_confirmation_attempts_are_rate_limited(monkeypatch) -> None:
    confirmed = []

    async def reserve(_message_id):
        return True

    async def rate_limit(_sender_id):
        return False

    async def confirm(*args):
        confirmed.append(args)

    monkeypatch.setattr(api_module, "reserve_whatsapp_message", reserve)
    monkeypatch.setattr(api_module, "allow_whatsapp_message", rate_limit)
    monkeypatch.setattr(api_module, "_confirm_whatsapp_action", confirm)
    monkeypatch.setattr(api_module, "_whatsapp_config", lambda: SimpleNamespace())

    asyncio.run(
        api_module._process_whatsapp_message(
            IncomingWhatsAppMessage(
                "wamid.confirm", "49123456789", "BESTÄTIGEN ABC123"
            )
        )
    )

    assert confirmed == []


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
