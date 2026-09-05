"""WhatsApp Cloud API helpers for the read-only LANIS assistant."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests
from fastapi.concurrency import run_in_threadpool

MAX_MESSAGE_LENGTH = 3900
PAIRING_PATTERN = re.compile(
    r"(?:^|\s)LANIS\s+([A-Z0-9]{4}-[A-Z0-9]{4})(?:\s|$)", re.IGNORECASE
)


@dataclass(frozen=True)
class WhatsAppConfig:
    access_token: str
    phone_number_id: str
    verify_token: str
    app_secret: str
    graph_api_version: str
    public_number: str
    ui_base_url: str

    @classmethod
    def from_env(cls) -> "WhatsAppConfig":
        return cls(
            access_token=os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip(),
            phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
            verify_token=os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip(),
            app_secret=os.getenv("WHATSAPP_APP_SECRET", "").strip(),
            graph_api_version=os.getenv("WHATSAPP_GRAPH_API_VERSION", "").strip(),
            public_number=re.sub(r"\D", "", os.getenv("WHATSAPP_PUBLIC_NUMBER", "")),
            ui_base_url=os.getenv(
                "LANIS_UI_BASE_URL", "https://lanis.arg-server.de"
            ).rstrip("/"),
        )

    @property
    def configured(self) -> bool:
        return bool(
            all(
                (
                    self.access_token,
                    self.phone_number_id,
                    self.verify_token,
                    self.app_secret,
                    self.graph_api_version,
                    self.public_number,
                )
            )
            and re.fullmatch(r"v\d+\.\d+", self.graph_api_version)
            and re.fullmatch(r"\d{7,15}", self.public_number)
        )

    def pairing_url(self, code: str) -> str:
        return f"https://wa.me/{self.public_number}?text={quote(f'LANIS {code}')}"


@dataclass(frozen=True)
class IncomingWhatsAppMessage:
    message_id: str
    sender_id: str
    text: str


class WhatsAppCloudClient:
    """Minimal Cloud API client that deliberately logs no message contents."""

    def __init__(self, config: WhatsAppConfig):
        self.config = config

    async def send_text(self, recipient: str, body: str) -> None:
        await self._send(
            recipient,
            {
                "type": "text",
                "text": {"preview_url": False, "body": truncate_message(body)},
            },
        )

    async def send_menu(self, recipient: str, body: str) -> None:
        """Send the assistant's primary navigation as a native WhatsApp list."""
        await self._send(
            recipient,
            {
                "type": "interactive",
                "interactive": {
                    "type": "list",
                    "body": {"text": body[:1024]},
                    "footer": {"text": "Nur lesender Zugriff · STOP trennt die Verbindung"},
                    "action": {
                        "button": "Funktion auswählen",
                        "sections": [
                            {
                                "title": "Schultag",
                                "rows": [
                                    {
                                        "id": "today",
                                        "title": "Heute",
                                        "description": "Dein heutiger Stundenplan",
                                    },
                                    {
                                        "id": "tomorrow",
                                        "title": "Morgen",
                                        "description": "Dein morgiger Stundenplan",
                                    },
                                    {
                                        "id": "substitutions",
                                        "title": "Vertretungen",
                                        "description": "Ausfälle und Änderungen",
                                    },
                                ],
                            },
                            {
                                "title": "Organisieren",
                                "rows": [
                                    {
                                        "id": "homework",
                                        "title": "Hausaufgaben",
                                        "description": "Noch offene Aufgaben",
                                    },
                                    {
                                        "id": "exams",
                                        "title": "Klausuren",
                                        "description": "Anstehende Prüfungen",
                                    },
                                    {
                                        "id": "calendar",
                                        "title": "Termine",
                                        "description": "Die nächsten Kalendereinträge",
                                    },
                                    {
                                        "id": "messages",
                                        "title": "Nachrichten",
                                        "description": "Ungelesene Unterhaltungen",
                                    },
                                ],
                            },
                        ],
                    },
                },
            },
        )

    async def send_quick_actions(self, recipient: str, body: str) -> None:
        await self._send(
            recipient,
            {
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body[:1024]},
                    "action": {
                        "buttons": [
                            {
                                "type": "reply",
                                "reply": {"id": "today", "title": "Heute"},
                            },
                            {
                                "type": "reply",
                                "reply": {"id": "tomorrow", "title": "Morgen"},
                            },
                            {
                                "type": "reply",
                                "reply": {"id": "help", "title": "Menü"},
                            },
                        ]
                    },
                },
            },
        )

    async def _send(self, recipient: str, message: Dict[str, Any]) -> None:
        if not self.config.configured:
            raise RuntimeError("WhatsApp Cloud API is not configured")
        if not re.fullmatch(r"\d{7,20}", recipient):
            raise ValueError("Invalid WhatsApp recipient")
        if message.get("type") == "text" and not message.get("text", {}).get("body"):
            raise ValueError("WhatsApp message body cannot be empty")
        url = (
            "https://graph.facebook.com/"
            f"{self.config.graph_api_version}/{self.config.phone_number_id}/messages"
        )

        def _send() -> None:
            response = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.config.access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "recipient_type": "individual",
                    "to": recipient,
                    **message,
                },
                timeout=15,
            )
            response.raise_for_status()

        await run_in_threadpool(_send)


def verify_webhook_signature(body: bytes, signature: str, app_secret: str) -> bool:
    if not signature.startswith("sha256=") or not app_secret:
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature[7:], expected)


def extract_incoming_messages(
    payload: Any, phone_number_id: str = ""
) -> List[IncomingWhatsAppMessage]:
    if (
        not isinstance(payload, dict)
        or payload.get("object") != "whatsapp_business_account"
    ):
        return []
    extracted: List[IncomingWhatsAppMessage] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            webhook_phone_id = str(
                (value.get("metadata") or {}).get("phone_number_id") or ""
            )
            if phone_number_id and webhook_phone_id != phone_number_id:
                continue
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                message_id = str(message.get("id") or "").strip()
                sender_id = str(message.get("from") or "").strip()
                text = _message_text(message)
                if (
                    1 <= len(message_id) <= 256
                    and sender_id.isdigit()
                    and 7 <= len(sender_id) <= 20
                    and text
                ):
                    extracted.append(
                        IncomingWhatsAppMessage(message_id, sender_id, text[:4096])
                    )
    return extracted


def _message_text(message: Dict[str, Any]) -> str:
    message_type = message.get("type")
    if message_type == "text":
        return str((message.get("text") or {}).get("body") or "").strip()
    if message_type == "button":
        button = message.get("button") or {}
        return str(button.get("payload") or button.get("text") or "").strip()
    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        for key in ("button_reply", "list_reply"):
            reply = interactive.get(key)
            if isinstance(reply, dict):
                return str(reply.get("id") or reply.get("title") or "").strip()
    return ""


def pairing_code(text: str) -> Optional[str]:
    match = PAIRING_PATTERN.search(text.strip())
    return match.group(1).upper() if match else None


def normalize_command(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def command_intent(text: str) -> str:
    command = normalize_command(text)
    words = set(command.split())
    if words & {"stop", "unlink", "trennen", "abmelden", "entkoppeln"}:
        return "unlink"
    if words & {
        "nachricht",
        "nachrichten",
        "postfach",
        "ungelesen",
        "mails",
        "messages",
    }:
        return "messages"
    if words & {
        "vertretung",
        "vertretungen",
        "vertretungsplan",
        "ausfall",
        "ausfalle",
        "entfall",
        "substitutions",
    }:
        return "substitutions"
    if words & {
        "hausaufgabe",
        "hausaufgaben",
        "aufgabe",
        "aufgaben",
        "homework",
        "todos",
    }:
        return "homework"
    if words & {"klausur", "klausuren", "prufung", "prufungen", "test", "exams"}:
        return "exams"
    if words & {"termin", "termine", "kalender", "event", "events"}:
        return "calendar"
    if words & {"morgen", "tomorrow"}:
        return "tomorrow"
    if words & {"heute", "today", "stundenplan", "unterricht", "schule"}:
        return "today"
    if words & {
        "hilfe",
        "help",
        "start",
        "menu",
        "menue",
        "ubersicht",
        "kannst",
    }:
        return "help"
    return "unknown"


def help_message(ui_base_url: str) -> str:
    return (
        "👋 *Was möchtest du wissen?*\n\n"
        "Wähle unten eine Funktion aus oder schreib einfach eine Frage wie "
        "„Was habe ich morgen?“"
    )


def unknown_message() -> str:
    return (
        "Das habe ich noch nicht verstanden. Versuch zum Beispiel "
        "„Hausaufgaben“, „Vertretungen“ oder „Was habe ich morgen?“."
    )


def not_linked_message(ui_base_url: str) -> str:
    return (
        "🔒 Diese WhatsApp-Nummer ist noch nicht mit LANIS verbunden.\n\n"
        "Öffne LANIS, gehe zu *Einstellungen → WhatsApp-Assistent* und sende "
        "anschließend den dort erzeugten Verbindungscode.\n\n"
        f"{ui_base_url}/settings/whatsapp"
    )


def format_timetable(result: Dict[str, Any], target: date) -> str:
    title = target.strftime("%d.%m.%Y")
    if not result.get("success"):
        return f"⚠️ Der Stundenplan für {title} konnte nicht geladen werden."
    days = result.get("days") or []
    day_index = target.weekday()
    if day_index >= len(days):
        return f"📅 *{title}*\nKein Unterricht im Stundenplan."
    target_week = target - timedelta(days=target.weekday())
    try:
        result_week = date.fromisoformat(str(result.get("week_start")))
    except (TypeError, ValueError):
        result_week = target_week
    outside_result_week = result_week != target_week
    own_plan = (
        result.get("template_plan_for_own")
        if outside_result_week
        else result.get("plan_for_own")
    ) or []
    all_plan = (
        result.get("template_plan_for_all")
        if outside_result_week
        else result.get("plan_for_all")
    ) or []
    plan = own_plan if any(own_plan) else all_plan
    lessons = (
        plan[day_index]
        if day_index < len(plan) and isinstance(plan[day_index], list)
        else []
    )
    if not lessons:
        return f"📅 *{days[day_index]} · {title}*\nKein Unterricht eingetragen."
    lines = [f"📅 *{days[day_index]} · {title}*", ""]
    for lesson in sorted(lessons, key=lambda item: _number(item.get("stunde"))):
        period = _period_label(lesson)
        subject = _plain(lesson.get("name") or "Unterricht")
        details = [_plain(lesson.get("room")), _plain(lesson.get("teacher"))]
        suffix = " · ".join(value for value in details if value)
        lines.append(f"{period}. {subject}" + (f" · {suffix}" if suffix else ""))
    return "\n".join(lines)


def format_substitutions(result: Dict[str, Any], own_class: str = "") -> str:
    if not result.get("success"):
        return "⚠️ Der Vertretungsplan konnte nicht geladen werden."
    selected = []
    for day in result.get("days") or []:
        entries = day.get("substitutions") or [] if isinstance(day, dict) else []
        if own_class:
            entries = [entry for entry in entries if _class_matches(entry, own_class)]
        for entry in entries:
            selected.append((day, entry))
    if not selected:
        scope = f" für {own_class}" if own_class else ""
        return f"✅ Keine aktuellen Vertretungsänderungen{scope}."
    lines = ["🔄 *Deine Vertretungen*", ""]
    for day, entry in selected[:12]:
        when = _plain(entry.get("tag") or entry.get("tag_en") or day.get("date"))
        period = _plain(entry.get("stunde"))
        subject = _plain(entry.get("fach") or entry.get("fach_alt") or "Unterricht")
        kind = _plain(entry.get("art") or entry.get("hinweis") or "Änderung")
        room = _plain(entry.get("raum"))
        lines.append(
            f"*{when} · {period}. Std.*\n{subject}: {kind}"
            + (f" · Raum {room}" if room else "")
        )
    if len(selected) > 12:
        lines.append(f"… und {len(selected) - 12} weitere")
    return "\n".join(lines)


def format_homework(result: Dict[str, Any]) -> str:
    if not result.get("success"):
        return "⚠️ Die Hausaufgaben konnten nicht geladen werden."
    entries = _list_value(result, "entries", "homework", "items")
    open_entries = [
        item
        for item in entries
        if _plain(item.get("homework") or item.get("task") or item.get("text"))
        and not bool(item.get("homework_done") or item.get("done"))
    ]
    if not open_entries:
        return "✅ Keine offenen Hausaufgaben gefunden."
    lines = [f"📝 *{len(open_entries)} offene Hausaufgaben*", ""]
    for item in open_entries[:10]:
        course = _plain(
            item.get("name") or item.get("course_name") or item.get("subject") or "Kurs"
        )
        task = _plain(item.get("homework") or item.get("task") or item.get("text"))
        due = _plain(item.get("datum") or item.get("date") or item.get("due_date"))
        lines.append(f"*{course}*" + (f" · {due}" if due else "") + f"\n{task}")
    if len(open_entries) > 10:
        lines.append(f"… und {len(open_entries) - 10} weitere")
    return "\n".join(lines)


def format_exams(result: Dict[str, Any], today: Optional[date] = None) -> str:
    if not result.get("success"):
        return "⚠️ Die Klausuren konnten nicht geladen werden."
    exams = _list_value(result, "exams")
    if not exams:
        for group in _list_value(result, "groups", "study_groups"):
            exams.extend(_list_value(group, "exams"))
    today = today or date.today()
    upcoming = [
        exam
        for exam in exams
        if (exam_date := _date_value(exam.get("date") or exam.get("datum"))) is None
        or exam_date >= today
    ]
    upcoming.sort(
        key=lambda exam: _date_value(exam.get("date") or exam.get("datum")) or date.max
    )
    if not upcoming:
        return "✅ Keine anstehenden Klausuren gefunden."
    lines = [f"🧪 *{len(upcoming)} anstehende Klausuren*", ""]
    for exam in upcoming[:10]:
        name = _plain(
            exam.get("course_name") or exam.get("name") or exam.get("type") or "Klausur"
        )
        exam_date = _plain(exam.get("date") or exam.get("datum"))
        detail = _plain(exam.get("type") if exam.get("course_name") else "")
        lines.append(
            f"{exam_date + ' · ' if exam_date else ''}*{name}*"
            + (f" ({detail})" if detail else "")
        )
    return "\n".join(lines)


def format_calendar(result: Dict[str, Any], today: date) -> str:
    if not result.get("success"):
        return "⚠️ Die Termine konnten nicht geladen werden."
    upcoming = []
    for event in _list_value(result, "events"):
        event_date = _date_value(event.get("start") or event.get("date"))
        if event_date is None or event_date >= today:
            upcoming.append((event_date or date.max, event))
    upcoming.sort(key=lambda pair: pair[0])
    if not upcoming:
        return "✅ Keine kommenden Termine gefunden."
    lines = ["🗓️ *Deine nächsten Termine*", ""]
    for event_date, event in upcoming[:10]:
        title = _plain(event.get("title") or event.get("name") or "Termin")
        date_label = (
            event_date.strftime("%d.%m.%Y") if event_date != date.max else "Ohne Datum"
        )
        lines.append(f"*{date_label}*\n{title}")
    return "\n".join(lines)


def format_messages(result: Dict[str, Any], show_preview: bool = False) -> str:
    if not result.get("success"):
        return "⚠️ Die Nachrichten konnten nicht geladen werden."
    conversations = _list_value(result, "conversations", "messages", "rows")
    unread = [item for item in conversations if _truthy(item.get("unread"))]
    if not unread:
        return "✅ Keine ungelesenen Schulportal-Nachrichten."
    lines = [f"✉️ *{len(unread)} ungelesene Unterhaltungen*"]
    if show_preview:
        for item in unread[:8]:
            sender = _plain(
                item.get("SenderName")
                or item.get("sender")
                or item.get("Sender")
                or "Unbekannt"
            )
            subject = _plain(
                item.get("Betreff") or item.get("subject") or "Neue Nachricht"
            )
            lines.append(f"• {sender}: {subject}")
    else:
        lines.append("Vorschauen sind aus Datenschutzgründen deaktiviert.")
    return "\n".join(lines)


def truncate_message(body: str) -> str:
    if len(body) <= MAX_MESSAGE_LENGTH:
        return body
    return body[: MAX_MESSAGE_LENGTH - 2].rstrip() + "…"


def _plain(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _number(value: Any) -> int:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else 999


def _period_label(lesson: Dict[str, Any]) -> str:
    start = _number(lesson.get("stunde"))
    if start == 999:
        return "?"
    duration = max(1, int(lesson.get("duration") or 1))
    return str(start) if duration == 1 else f"{start}–{start + duration - 1}"


def _class_matches(entry: Dict[str, Any], own_class: str) -> bool:
    expected = normalize_command(own_class).replace(" ", "")
    values = f"{entry.get('klasse', '')} {entry.get('klasse_alt', '')}"
    candidates = {
        normalize_command(part).replace(" ", "")
        for part in re.split(r"[,;/|\s]+", values)
        if part
    }
    return expected in candidates


def _list_value(mapping: Dict[str, Any], *keys: str) -> List[Dict[str, Any]]:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _date_value(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], pattern).date()
        except ValueError:
            continue
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() not in {"", "0", "false", "no", "nein"}
    return bool(value)
