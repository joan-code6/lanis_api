"""Daytime message polling and Web Push notifications."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, time, timedelta
from html import unescape
from typing import Any, Awaitable, Callable, Dict, List, Tuple
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.concurrency import run_in_threadpool

from .auth_db import (
    delete_push_subscription,
    get_enabled_notification_users,
    get_message_notification_state,
    get_push_subscriptions,
    save_message_notification_state,
)

logger = logging.getLogger("message_notifications")

SCHEDULER_TICK_SECONDS = 60
MAX_CONCURRENT_USER_POLLS = 4


def _plain_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", text))).strip()


def _message_id(message: Dict[str, Any]) -> str:
    for key in ("id", "Uniquid", "uniqid", "Id"):
        value = message.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _message_signature(message: Dict[str, Any]) -> str:
    activity = {
        "date": message.get("date") or message.get("Datum"),
        "last_message_id": message.get("last_message_id") or message.get("lastMessageId"),
        "message_id": message.get("Id") or message.get("message_id"),
        "sender": message.get("sender") or message.get("Sender"),
        "subject": message.get("subject") or message.get("Betreff"),
    }
    return json.dumps(activity, ensure_ascii=False, sort_keys=True, default=str)


def build_message_snapshot(
    conversations: List[Dict[str, Any]],
) -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    """Build stable change markers and display details from header data."""
    snapshot: Dict[str, str] = {}
    details: Dict[str, Dict[str, str]] = {}
    for conversation in conversations:
        conversation_id = _message_id(conversation)
        if not conversation_id:
            continue
        snapshot[conversation_id] = _message_signature(conversation)
        details[conversation_id] = {
            "subject": _plain_text(
                conversation.get("subject") or conversation.get("Betreff") or "Neue Nachricht"
            ),
            "sender": _plain_text(
                conversation.get("sender")
                or conversation.get("SenderName")
                or conversation.get("Sender")
                or "Unbekannter Absender"
            ),
        }
    return snapshot, details


def validate_notification_preferences(preferences: Dict[str, Any]) -> None:
    """Validate clock and timezone values before they reach the scheduler."""
    for key in ("start_time", "end_time"):
        try:
            time.fromisoformat(str(preferences[key]))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"Invalid {key}; expected HH:MM") from error

    try:
        ZoneInfo(str(preferences["timezone"]))
    except (KeyError, ZoneInfoNotFoundError) as error:
        raise ValueError("Invalid timezone") from error

    try:
        interval = int(preferences.get("poll_interval_minutes", 15))
    except (TypeError, ValueError) as error:
        raise ValueError("poll_interval_minutes must be between 5 and 60") from error
    if interval < 5 or interval > 60:
        raise ValueError("poll_interval_minutes must be between 5 and 60")


def is_notification_window_open(
    preferences: Dict[str, Any], now: datetime | None = None
) -> bool:
    """Return whether a user's configured local daytime window is open."""
    try:
        timezone = ZoneInfo(str(preferences["timezone"]))
        start = time.fromisoformat(str(preferences["start_time"]))
        end = time.fromisoformat(str(preferences["end_time"]))
    except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
        return False

    local_now = datetime.now(timezone) if now is None else now.astimezone(timezone)
    current = local_now.time().replace(tzinfo=None)
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _is_due(state: Dict[str, Any] | None, interval_minutes: int, now: datetime) -> bool:
    if not state or not state.get("checked_at"):
        return True
    try:
        checked_at = datetime.fromisoformat(str(state["checked_at"]))
    except ValueError:
        return True
    if checked_at.tzinfo is None:
        checked_at = checked_at.astimezone()
    return now - checked_at.astimezone(now.tzinfo) >= timedelta(minutes=interval_minutes)


def _push_configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT")
    )


def _send_push_sync(subscription: Dict[str, Any], payload: Dict[str, Any]) -> None:
    try:
        from pywebpush import webpush
    except ImportError as error:
        raise RuntimeError(
            "pywebpush is not installed; install the API requirements to enable push notifications"
        ) from error

    webpush(
        subscription_info=subscription,
        data=json.dumps(payload, ensure_ascii=False),
        vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
        vapid_claims={"sub": os.environ["VAPID_SUBJECT"]},
    )


async def _send_push_to_subscriptions(
    user_id: str,
    subscriptions: List[Dict[str, Any]],
    payload: Dict[str, Any],
) -> bool:
    """Send a payload and return whether the batch can be considered delivered."""
    if not subscriptions:
        return True
    if not _push_configured():
        logger.warning("Push notification skipped: VAPID configuration is incomplete")
        return False

    results = await asyncio.gather(
        *(
            run_in_threadpool(_send_push_sync, subscription, payload)
            for subscription in subscriptions
        ),
        return_exceptions=True,
    )
    delivered = False
    retryable_failure = False
    for subscription, result in zip(subscriptions, results):
        if not isinstance(result, Exception):
            delivered = True
            continue

        response = getattr(result, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in (404, 410):
            await delete_push_subscription(user_id, subscription["endpoint"])
            continue

        retryable_failure = True
        logger.warning(
            "Push notification failed for %s: %s", user_id, result
        )

    return delivered or not retryable_failure


async def send_test_push_notification(user_id: str) -> bool:
    """Send a user-triggered test notification to all registered devices."""
    subscriptions = await get_push_subscriptions(user_id)
    if not subscriptions:
        return False
    return await _send_push_to_subscriptions(
        user_id,
        subscriptions,
        {
            "title": "Lanis-Benachrichtigungen funktionieren",
            "body": "Dies ist eine Testbenachrichtigung.",
            "url": "/settings",
            "tag": "lanis-notification-test",
        },
    )


async def check_user_messages(
    user: Dict[str, Any],
    get_client: Callable[[str], Awaitable[Any]],
    now: datetime | None = None,
) -> bool:
    """Poll one configured user and notify on new or changed conversations."""
    user_id = str(user["user_id"])
    if not user.get("enabled") or not is_notification_window_open(user, now):
        return False

    try:
        timezone = ZoneInfo(str(user["timezone"]))
    except (KeyError, ZoneInfoNotFoundError):
        logger.warning("Message notification poll skipped for %s: invalid timezone", user_id)
        return False
    local_now = datetime.now(timezone) if now is None else now.astimezone(timezone)
    previous_state = await get_message_notification_state(user_id)
    try:
        interval = int(user.get("poll_interval_minutes", 15))
    except (TypeError, ValueError):
        logger.warning("Message notification poll skipped for %s: invalid interval", user_id)
        return False
    if not _is_due(previous_state, interval, local_now):
        return False

    try:
        session_data = await get_client(user_id)
        client = getattr(session_data, "client", session_data)
        result = await run_in_threadpool(client.nachrichten_get_headers, "All", 0)
    except Exception as error:
        logger.warning("Message notification poll failed for %s: %s", user_id, error)
        return False

    if not result.get("success"):
        logger.warning(
            "Message notification poll returned an error for %s: %s",
            user_id,
            result.get("error"),
        )
        return False

    current_snapshot, details = build_message_snapshot(
        result.get("conversations") or []
    )
    has_baseline = isinstance(previous_state, dict) and "conversations" in previous_state
    previous_snapshot = (previous_state or {}).get("conversations") or {}
    changed_ids = [
        conversation_id
        for conversation_id, signature in current_snapshot.items()
        if has_baseline and previous_snapshot.get(conversation_id) != signature
    ]

    subscriptions = await get_push_subscriptions(user_id)
    if changed_ids and subscriptions:
        first = details[changed_ids[0]]
        if user.get("show_preview", True):
            body = f"{first['sender']}: {first['subject']}"
        else:
            body = "Du hast neue Nachrichten."
        if len(changed_ids) > 1:
            body = f"{len(changed_ids)} neue Nachrichten"
        payload = {
            "title": "Neue Nachricht in Lanis" if len(changed_ids) == 1 else "Neue Nachrichten in Lanis",
            "body": body,
            "url": f"/messages?conversation={quote(changed_ids[0])}",
            "tag": f"lanis-messages-{changed_ids[0]}",
        }
        if not await _send_push_to_subscriptions(user_id, subscriptions, payload):
            return False

    await save_message_notification_state(
        user_id,
        {"checked_at": local_now.isoformat(), "conversations": current_snapshot},
    )
    return bool(changed_ids)


async def run_message_notification_cycle(
    get_client: Callable[[str], Awaitable[Any]],
) -> None:
    """Run one bounded polling cycle for all enabled users."""
    users = await get_enabled_notification_users()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_USER_POLLS)

    async def check_with_limit(user: Dict[str, Any]) -> None:
        async with semaphore:
            await check_user_messages(user, get_client)

    await asyncio.gather(*(check_with_limit(user) for user in users))


async def run_message_notification_scheduler(
    get_client: Callable[[str], Awaitable[Any]],
) -> asyncio.Task:
    """Start the long-running daytime message polling task."""
    async def _loop() -> None:
        logger.info("Message notification scheduler started")
        while True:
            try:
                await run_message_notification_cycle(get_client)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Message notification scheduler error: %s", error)
            await asyncio.sleep(SCHEDULER_TICK_SECONDS)

    return asyncio.create_task(_loop())
