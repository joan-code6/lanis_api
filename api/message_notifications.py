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
from urllib.parse import quote, urlsplit
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
CLOCK_PATTERN = re.compile(r"^\d{2}:\d{2}$")
TRUSTED_PUSH_ENDPOINT_HOSTS = frozenset(
    {
        "fcm.googleapis.com",
        "android.googleapis.com",
        "notify.windows.com",
        "push.services.mozilla.com",
        "updates.push.services.mozilla.com",
        "web.push.apple.com",
    }
)
TRUSTED_PUSH_ENDPOINT_SUFFIXES = (".notify.windows.com",)


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
        "unread": _is_unread_conversation(message),
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


def _parse_notification_clock(value: Any) -> time:
    text = str(value)
    if not CLOCK_PATTERN.fullmatch(text):
        raise ValueError("expected HH:MM")
    parsed = time.fromisoformat(text)
    if parsed.tzinfo is not None:
        raise ValueError("notification clocks must not include a timezone offset")
    return parsed


def validate_notification_preferences(preferences: Dict[str, Any]) -> None:
    """Validate clock and timezone values before they reach the scheduler."""
    for key in ("start_time", "end_time"):
        try:
            _parse_notification_clock(preferences[key])
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
        start = _parse_notification_clock(preferences["start_time"])
        end = _parse_notification_clock(preferences["end_time"])
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


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "ja"}
    return bool(value)


def _is_unread_conversation(conversation: Dict[str, Any]) -> bool:
    for key in ("unread", "is_unread", "isUnread"):
        if key in conversation:
            return _as_bool(conversation[key])
    for key in ("read", "is_read", "isRead"):
        if key in conversation:
            return not _as_bool(conversation[key])
    return False


def _get_pending_deliveries(
    state: Dict[str, Any] | None, endpoints: set[str]
) -> Dict[str, List[Dict[str, Any]]]:
    raw_pending = state.get("pending_deliveries") if isinstance(state, dict) else None
    if not isinstance(raw_pending, dict):
        return {}

    pending: Dict[str, List[Dict[str, Any]]] = {}
    for endpoint in endpoints:
        queued = raw_pending.get(endpoint)
        if isinstance(queued, dict):
            queued = [queued]
        if not isinstance(queued, list):
            continue
        valid_payloads = [payload.copy() for payload in queued if isinstance(payload, dict)]
        if valid_payloads:
            pending[endpoint] = valid_payloads
    return pending


def push_configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT")
    )


def is_trusted_push_endpoint(endpoint: str) -> bool:
    """Allow only HTTPS endpoints exposed by known browser push services."""
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return False

    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        return False

    normalized_hostname = hostname.rstrip(".").lower()
    return normalized_hostname in TRUSTED_PUSH_ENDPOINT_HOSTS or any(
        normalized_hostname.endswith(suffix)
        for suffix in TRUSTED_PUSH_ENDPOINT_SUFFIXES
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
        timeout=10,
    )


async def _send_push_payloads(
    user_id: str,
    deliveries: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]],
) -> Dict[str, str]:
    """Send one queued payload per device and report each device's outcome."""
    if not deliveries:
        return {}
    if not push_configured():
        logger.warning("Push notification skipped: VAPID configuration is incomplete")
        return {endpoint: "retry" for endpoint in deliveries}

    results = await asyncio.gather(
        *(
            run_in_threadpool(_send_push_sync, subscription, payload)
            for subscription, payload in deliveries.values()
        ),
        return_exceptions=True,
    )
    statuses: Dict[str, str] = {}
    for (endpoint, (_subscription, _payload)), result in zip(deliveries.items(), results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if not isinstance(result, Exception):
            statuses[endpoint] = "delivered"
            continue

        response = getattr(result, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in (404, 410):
            try:
                await delete_push_subscription(user_id, endpoint)
            except Exception as error:
                logger.warning(
                    "Failed to remove stale push subscription %s for %s: %s",
                    endpoint,
                    user_id,
                    error,
                )
            statuses[endpoint] = "gone"
            continue

        statuses[endpoint] = "retry"
        logger.warning("Push notification failed for %s: %s", user_id, result)
    return statuses


async def _send_push_to_subscriptions(
    user_id: str,
    subscriptions: List[Dict[str, Any]],
    payload: Dict[str, Any],
) -> Dict[str, str]:
    """Send the same payload to each device and report per-device outcomes."""
    deliveries = {
        subscription["endpoint"]: (subscription, payload)
        for subscription in subscriptions
    }
    return await _send_push_payloads(user_id, deliveries)


async def send_test_push_notification(user_id: str) -> bool:
    """Send a user-triggered test notification to all registered devices."""
    subscriptions = await get_push_subscriptions(user_id)
    if not subscriptions:
        return False
    statuses = await _send_push_to_subscriptions(
        user_id,
        subscriptions,
        {
            "title": "Lanis-Benachrichtigungen funktionieren",
            "body": "Dies ist eine Testbenachrichtigung.",
            "url": "/settings",
            "tag": "lanis-notification-test",
        },
    )
    return any(status == "delivered" for status in statuses.values())


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

    subscriptions = await get_push_subscriptions(user_id)
    if not subscriptions:
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

    conversations = result.get("conversations") or []
    current_snapshot, details = build_message_snapshot(conversations)
    has_baseline = isinstance(previous_state, dict) and "conversations" in previous_state
    previous_snapshot = (previous_state or {}).get("conversations") or {}
    if not isinstance(previous_snapshot, dict):
        previous_snapshot = {}
    unread_ids = {
        conversation_id
        for conversation in conversations
        if isinstance(conversation, dict)
        and _is_unread_conversation(conversation)
        and (conversation_id := _message_id(conversation))
    }
    changed_ids = [
        conversation_id
        for conversation_id, signature in current_snapshot.items()
        if (
            has_baseline
            and conversation_id in unread_ids
            and previous_snapshot.get(conversation_id) != signature
        )
    ]

    subscriptions_by_endpoint = {
        subscription["endpoint"]: subscription
        for subscription in subscriptions
        if subscription.get("endpoint")
    }
    pending_deliveries = _get_pending_deliveries(
        previous_state, set(subscriptions_by_endpoint)
    )

    if changed_ids:
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
        for endpoint in subscriptions_by_endpoint:
            pending_deliveries.setdefault(endpoint, []).append(payload)

    deliveries = {
        endpoint: (subscriptions_by_endpoint[endpoint], queued[0])
        for endpoint, queued in pending_deliveries.items()
        if queued
    }
    statuses = await _send_push_payloads(user_id, deliveries) if deliveries else {}
    for endpoint, status in statuses.items():
        if status in {"delivered", "gone"} and endpoint in pending_deliveries:
            pending_deliveries[endpoint].pop(0)
            if not pending_deliveries[endpoint]:
                pending_deliveries.pop(endpoint)

    next_state: Dict[str, Any] = {
        "checked_at": local_now.isoformat(),
        "conversations": current_snapshot,
    }
    if pending_deliveries:
        next_state["pending_deliveries"] = pending_deliveries
    await save_message_notification_state(
        user_id,
        next_state,
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
            try:
                await check_user_messages(user, get_client)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("Message notification poll crashed: %s", error)

    await asyncio.gather(
        *(check_with_limit(user) for user in users), return_exceptions=True
    )


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
