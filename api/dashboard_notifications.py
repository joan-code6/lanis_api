"""Build stable, user-scoped items for the dashboard notification inbox."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from html import unescape
from typing import Any

from .timetable_substitutions import class_tokens


def _plain_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]*>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _first_text(*values: Any) -> str:
    for value in values:
        text = _plain_text(value)
        if text:
            return text
    return ""


def _matches_class(value: Any, target_class: str) -> bool:
    target_tokens = class_tokens(target_class)
    return bool(target_tokens and class_tokens(value) & target_tokens)


def _stable_id(source: str, values: dict[str, Any]) -> str:
    serialized = json.dumps(values, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"{source}:{digest}"


def _sortable_datetime(value: Any) -> str:
    text = _plain_text(value)
    if not text:
        return ""
    german_date = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", text)
    if german_date:
        year = int(german_date.group(3))
        if year < 100:
            year += 2000
        try:
            time_match = re.search(r"\b(\d{1,2}):(\d{2})(?::(\d{2}))?\b", text)
            return datetime(
                year,
                int(german_date.group(2)),
                int(german_date.group(1)),
                int(time_match.group(1)) if time_match else 0,
                int(time_match.group(2)) if time_match else 0,
                int(time_match.group(3)) if time_match and time_match.group(3) else 0,
                tzinfo=timezone.utc,
            ).isoformat()
        except ValueError:
            return ""
    time_only = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if time_only:
        now = datetime.now(timezone.utc)
        try:
            return datetime(
                now.year,
                now.month,
                now.day,
                int(time_only.group(1)),
                int(time_only.group(2)),
                int(time_only.group(3)) if time_only.group(3) else 0,
                tzinfo=timezone.utc,
            ).isoformat()
        except ValueError:
            return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return ""


def _is_unread(conversation: dict[str, Any]) -> bool:
    unread = conversation.get("unread")
    if isinstance(unread, str):
        return unread.strip().casefold() in {"1", "true", "yes", "unread"}
    if unread is not None:
        return bool(unread)
    read = conversation.get("read")
    if isinstance(read, str):
        return read.strip().casefold() in {"0", "false", "no", "unread"}
    return read is False


def message_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one inbox item per currently unread conversation revision."""
    items: list[dict[str, Any]] = []
    for conversation in result.get("conversations") or []:
        if not isinstance(conversation, dict) or not _is_unread(conversation):
            continue
        conversation_id = _first_text(
            conversation.get("Uniquid"),
            conversation.get("uniqid"),
            conversation.get("id"),
            conversation.get("Id"),
        )
        if not conversation_id:
            continue
        last_message_id = _first_text(
            conversation.get("last_message_id"),
            conversation.get("lastMessageId"),
            conversation.get("Id"),
        )
        identity = {"conversation_id": conversation_id}
        if last_message_id:
            identity["last_message_id"] = last_message_id
        else:
            raw_date = conversation.get("date") or conversation.get("Datum")
            identity.update(
                activity_at=_sortable_datetime(raw_date) or _plain_text(raw_date),
                sender=conversation.get("Sender") or conversation.get("sender"),
                subject=conversation.get("Betreff") or conversation.get("subject"),
            )
        items.append(
            {
                "id": _stable_id("messages", identity),
                "source": "messages",
                "title": _first_text(
                    conversation.get("Betreff"),
                    conversation.get("subject"),
                    "Neue Nachricht",
                ),
                "detail": _first_text(
                    conversation.get("SenderName"),
                    conversation.get("Sender"),
                    conversation.get("sender"),
                    "Unbekannter Absender",
                ),
                "meta": _first_text(
                    conversation.get("date"), conversation.get("Datum")
                ),
                "occurred_at": _sortable_datetime(
                    conversation.get("date") or conversation.get("Datum")
                ),
                "path": "/messages",
            }
        )
    return items


def native_plan_items(
    result: dict[str, Any], target_class: str = ""
) -> list[dict[str, Any]]:
    """Return class-relevant native Schulportal substitution entries."""
    items: list[dict[str, Any]] = []
    for day in result.get("days") or []:
        if not isinstance(day, dict):
            continue
        for entry in day.get("substitutions") or []:
            if not isinstance(entry, dict):
                continue
            class_name = _first_text(entry.get("klasse"), entry.get("klasse_alt"))
            class_scope = " ".join(
                filter(
                    None,
                    (
                        _plain_text(entry.get("klasse")),
                        _plain_text(entry.get("klasse_alt")),
                    ),
                )
            )
            if not _matches_class(class_scope, target_class):
                continue
            identity = {
                "date": entry.get("tag_en") or entry.get("tag") or day.get("date"),
                "period": entry.get("stunde"),
                "class": entry.get("klasse"),
                "previous_class": entry.get("klasse_alt"),
                "subject": entry.get("fach"),
                "previous_subject": entry.get("fach_alt"),
                "kind": entry.get("art"),
                "teacher": entry.get("lehrer"),
                "substitute": entry.get("vertreter"),
                "room": entry.get("raum"),
                "previous_room": entry.get("raum_alt"),
                "note": entry.get("hinweis"),
                "note_2": entry.get("hinweis2"),
                "group": entry.get("lerngruppe"),
            }
            subject = _first_text(entry.get("fach"), entry.get("fach_alt"))
            kind = _first_text(entry.get("art"), entry.get("hinweis"), "Änderung")
            period = _plain_text(entry.get("stunde"))
            room = _plain_text(entry.get("raum"))
            detail = " · ".join(
                filter(
                    None,
                    (
                        class_name,
                        f"{period}. Std." if period else "",
                        f"Raum {room}" if room else "",
                    ),
                )
            )
            items.append(
                {
                    "id": _stable_id("native", identity),
                    "source": "native",
                    "title": f"{kind} · {subject}" if subject else kind,
                    "detail": detail,
                    "meta": _first_text(entry.get("tag"), day.get("date")),
                    "occurred_at": _sortable_datetime(
                        entry.get("tag_en") or entry.get("tag") or day.get("date")
                    ),
                    "path": "/vertretungsplan",
                }
            )
    return items


def _table_value(headers: list[Any], row: Any, patterns: Iterable[str]) -> str:
    normalized_patterns = tuple(
        re.sub(r"[^a-z0-9]", "", _plain_text(pattern).casefold())
        for pattern in patterns
    )
    index = next(
        (
            position
            for position, header in enumerate(headers)
            if any(
                pattern
                in re.sub(r"[^a-z0-9]", "", _plain_text(header).casefold())
                for pattern in normalized_patterns
            )
        ),
        -1,
    )
    if index < 0:
        return ""
    if isinstance(row, list):
        return _plain_text(row[index] if index < len(row) else "")
    if isinstance(row, dict):
        return _plain_text(row.get(str(headers[index]), ""))
    return ""


def dsb_plan_items(
    result: dict[str, Any], target_class: str = ""
) -> list[dict[str, Any]]:
    """Return class-relevant DSBmobile rows, including caption-scoped tables."""
    items: list[dict[str, Any]] = []
    for table in result.get("tables") or []:
        if not isinstance(table, dict):
            continue
        headers = table.get("headers") or []
        if not isinstance(headers, list):
            continue
        caption = _plain_text(table.get("caption"))
        table_date = _first_text(table.get("date"), caption)
        for row in table.get("rows") or []:
            class_name = _table_value(headers, row, ("klasse", "class"))
            effective_class = class_name or caption
            if not _matches_class(effective_class, target_class):
                continue
            subject = _table_value(headers, row, ("fach", "subject"))
            kind = _first_text(
                _table_value(headers, row, ("art", "änderung", "aenderung", "type")),
                _table_value(headers, row, ("info", "hinweis")),
                "Änderung",
            )
            period = _table_value(headers, row, ("stunde", "std", "period"))
            room = _table_value(headers, row, ("raum", "room"))
            shown_class = class_name or (
                target_class if _matches_class(caption, target_class) else ""
            )
            detail = " · ".join(
                filter(
                    None,
                    (
                        shown_class,
                        f"{period}. Std." if period else "",
                        f"Raum {room}" if room else "",
                    ),
                )
            )
            identity = {
                "date": table_date,
                "caption": caption,
                "headers": headers,
                "row": row,
            }
            items.append(
                {
                    "id": _stable_id("dsb", identity),
                    "source": "dsb",
                    "title": f"{kind} · {subject}" if subject else kind,
                    "detail": detail,
                    "meta": table_date,
                    "occurred_at": _sortable_datetime(table.get("date")),
                    "path": "/dsb",
                }
            )
    return items


def source_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"messages": 0, "native": 0, "dsb": 0}
    for item in items:
        source = str(item.get("source") or "")
        if source in counts:
            counts[source] += 1
    return counts
