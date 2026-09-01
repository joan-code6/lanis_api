"""Appwrite TablesDB implementation of LANIS-owned persistent user state."""

from __future__ import annotations

import copy
import hashlib
from typing import Any, Dict, List, Optional

from appwrite_functions.src.backend import get_backend

from .identity import canonicalize_user_id


DEFAULT_NOTIFICATION_PREFERENCES: Dict[str, Any] = {
    "enabled": False,
    "messages_enabled": True,
    "vertretungsplan_enabled": False,
    "vertretungsplan_class_mode": "own",
    "vertretungsplan_classes": [],
    "start_time": "07:00",
    "end_time": "21:00",
    "poll_interval_minutes": 15,
    "timezone": "Europe/Berlin",
    "show_preview": True,
}
DEFAULT_USER_PREFERENCES: Dict[str, Any] = {
    "appearance": {"theme_mode": "system", "theme_color": "cyan"},
    "dashboard": {"pinned_modules": [], "view_mode": "grid"},
    "timetable": {"view_mode": "rolling"},
    "vertretungsplan": {"class_override": ""},
    "onboarding": {
        "version": 0,
        "status": "not_started",
        "last_step": "welcome",
    },
}


def _user(user_id: str) -> str:
    return canonicalize_user_id(user_id)


def _merge(current: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(current)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


async def initialize() -> None:
    """Schemas are managed declaratively and provisioned before deployment."""


async def store_refresh_token(
    user_id: str, school_id: str, username: str, password: str
) -> str:
    return await get_backend().credentials.store_refresh_token(
        _user(user_id), school_id, username, password
    )


async def get_refresh_token(token: str) -> Optional[dict]:
    return await get_backend().credentials.get_refresh_token(token)


async def get_refresh_token_by_user_id(user_id: str) -> Optional[dict]:
    return await get_backend().credentials.get_refresh_token_by_user_id(_user(user_id))


async def delete_refresh_token(token: str) -> None:
    await get_backend().credentials.delete_refresh_token(token)


async def delete_user_tokens(user_id: str) -> None:
    await get_backend().credentials.delete_user_tokens(_user(user_id))


async def get_notification_preferences(user_id: str) -> Dict[str, Any]:
    user_id = _user(user_id)
    stored = await get_backend().state.get(user_id, "notification_preferences")
    return {"user_id": user_id, **DEFAULT_NOTIFICATION_PREFERENCES, **(stored or {})}


async def save_notification_preferences(
    user_id: str, preferences: Dict[str, Any]
) -> Dict[str, Any]:
    user_id = _user(user_id)
    current = await get_notification_preferences(user_id)
    values = {**DEFAULT_NOTIFICATION_PREFERENCES, **preferences}
    state = get_backend().state
    if values["enabled"] and not current["enabled"]:
        await state.delete(user_id, "message_notification_state")
        await state.delete(user_id, "vertretungsplan_notification_state")
    elif values["messages_enabled"] and not current["messages_enabled"]:
        await state.delete(user_id, "message_notification_state")
    if values["vertretungsplan_enabled"] and (
        not current["vertretungsplan_enabled"]
        or current["vertretungsplan_class_mode"] != values["vertretungsplan_class_mode"]
        or current["vertretungsplan_classes"] != values["vertretungsplan_classes"]
    ):
        await state.delete(user_id, "vertretungsplan_notification_state")
    await state.set(
        user_id,
        "notification_preferences",
        values,
        enabled=bool(values["enabled"]),
    )
    return {"user_id": user_id, **values}


async def get_user_preferences(user_id: str) -> tuple[Dict[str, Any], bool]:
    stored = await get_backend().state.get(_user(user_id), "user_preferences")
    return _merge(DEFAULT_USER_PREFERENCES, stored or {}), stored is not None


async def save_user_preferences(
    user_id: str, updates: Dict[str, Any]
) -> Dict[str, Any]:
    user_id = _user(user_id)
    current, _ = await get_user_preferences(user_id)
    merged = _merge(current, updates)
    await get_backend().state.set(user_id, "user_preferences", merged)
    return merged


async def get_custom_lessons(user_id: str) -> List[Dict[str, Any]]:
    rows = await get_backend().state.list(_user(user_id), "custom_lesson")
    return [value for _, value in rows]


async def save_custom_lesson(user_id: str, lesson: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        **lesson,
        "teacher": str(lesson.get("teacher") or ""),
        "room": str(lesson.get("room") or ""),
        "class_name": str(lesson.get("class_name") or ""),
        "info": str(lesson.get("info") or ""),
        "start_time": str(lesson.get("start_time") or ""),
        "end_time": str(lesson.get("end_time") or ""),
        "duration": int(lesson.get("duration") or 1),
        "week_type": lesson.get("week_type") or None,
        "course_id": lesson.get("course_id") or None,
        "removed": bool(lesson.get("removed")),
        "is_custom": True,
    }
    key = f"{result.get('date', '')}:{result.get('period', '')}"
    await get_backend().state.set(_user(user_id), "custom_lesson", result, key)
    return result


async def delete_custom_lesson(user_id: str, lesson_date: str, period: str) -> None:
    await get_backend().state.delete(
        _user(user_id), "custom_lesson", f"{lesson_date}:{period}"
    )


async def get_class_link_overrides(user_id: str) -> Dict[str, str]:
    rows = await get_backend().state.list(_user(user_id), "class_link")
    return {key: str(value.get("url") or "") for key, value in rows}


async def save_class_link(user_id: str, course_id: str, url: str) -> Dict[str, Any]:
    course_id = str(course_id).strip()
    value = {"course_id": course_id, "url": str(url or "").strip(), "overridden": True}
    await get_backend().state.set(_user(user_id), "class_link", value, course_id)
    return value


async def delete_class_link(user_id: str, course_id: str) -> None:
    await get_backend().state.delete(
        _user(user_id), "class_link", str(course_id).strip()
    )


async def get_enabled_notification_users() -> List[Dict[str, Any]]:
    result = []
    for user_id, preferences in await get_backend().state.list_enabled(
        "notification_preferences"
    ):
        if await get_refresh_token_by_user_id(user_id):
            result.append({"user_id": user_id, **preferences})
    return result


def _subscription_key(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()


async def save_push_subscription(user_id: str, subscription: Dict[str, Any]) -> None:
    await get_backend().state.set(
        _user(user_id),
        "push_subscription",
        subscription,
        _subscription_key(subscription["endpoint"]),
    )


async def get_push_subscriptions(user_id: str) -> List[Dict[str, Any]]:
    return [
        value
        for _, value in await get_backend().state.list(
            _user(user_id), "push_subscription"
        )
    ]


async def delete_push_subscription(user_id: str, endpoint: str) -> None:
    await get_backend().state.delete(
        _user(user_id), "push_subscription", _subscription_key(endpoint)
    )


async def delete_user_push_subscriptions(user_id: str) -> None:
    state = get_backend().state
    for key, _ in await state.list(_user(user_id), "push_subscription"):
        await state.delete(_user(user_id), "push_subscription", key)


async def get_message_notification_state(user_id: str) -> Optional[Dict[str, Any]]:
    return await get_backend().state.get(_user(user_id), "message_notification_state")


async def save_message_notification_state(
    user_id: str, snapshot: Dict[str, Any]
) -> None:
    await get_backend().state.set(
        _user(user_id), "message_notification_state", snapshot
    )


async def get_vertretungsplan_notification_state(
    user_id: str,
) -> Optional[Dict[str, Any]]:
    return await get_backend().state.get(
        _user(user_id), "vertretungsplan_notification_state"
    )


async def save_vertretungsplan_notification_state(
    user_id: str, snapshot: Dict[str, Any]
) -> None:
    await get_backend().state.set(
        _user(user_id), "vertretungsplan_notification_state", snapshot
    )
