"""Pure helpers for applying account-specific timetable and class overrides."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


def current_timetable_monday(today: date | None = None) -> date:
    """Return the Monday used by the current portal timetable response."""
    current = today or datetime.now(BERLIN).date()
    return current - timedelta(days=current.weekday())


def _period_start(value: object) -> int | None:
    match = re.search(r"\d+", str(value if value is not None else ""))
    return int(match.group()) if match else None


def _period_text(value: object) -> str:
    return (
        str(value if value is not None else "")
        .strip()
        .replace("–", "-")
        .replace("—", "-")
    )


def _same_period(lesson: dict[str, Any], period: str) -> bool:
    lesson_start = _period_start(lesson.get("stunde"))
    override_start = _period_start(period)
    return lesson_start is not None and lesson_start == override_start


def _parse_clock(value: object) -> dict[str, int] | None:
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if not match:
        return None
    hour, minute = (int(part) for part in match.groups())
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return {"hour": hour, "minute": minute}


def _slot_times(
    timetable: dict[str, Any], period: str, duration: int
) -> tuple[dict[str, int] | None, dict[str, int] | None]:
    """Find portal slot times for a custom lesson when no times were entered."""
    hours = timetable.get("hours")
    if not isinstance(hours, list) or not hours:
        return None, None

    start_period = _period_start(period)
    if start_period is None:
        return None, None

    start_index = next(
        (
            index
            for index, slot in enumerate(hours)
            if isinstance(slot, dict)
            and _period_start(slot.get("label")) == start_period
        ),
        None,
    )
    if start_index is None:
        # Older responses do not include a period label in every slot.
        start_index = max(start_period - 1, 0)
    end_index = min(start_index + max(duration, 1) - 1, len(hours) - 1)

    start_slot = hours[start_index] if start_index < len(hours) else None
    end_slot = hours[end_index] if end_index < len(hours) else None
    if not isinstance(start_slot, dict) or not isinstance(end_slot, dict):
        return None, None

    start_time = start_slot.get("start_time")
    end_time = end_slot.get("end_time")
    return (
        copy.deepcopy(start_time) if isinstance(start_time, dict) else None,
        copy.deepcopy(end_time) if isinstance(end_time, dict) else None,
    )


def _custom_raw_lesson(
    timetable: dict[str, Any],
    override: dict[str, Any],
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    period = _period_text(override.get("period"))
    parsed_period = _period_start(period)
    start_period = parsed_period if parsed_period is not None else 1
    duration = max(int(override.get("duration") or 1), 1)
    fallback_start, fallback_end = _slot_times(timetable, period, duration)

    # Keep enriched fields such as homework when a user only corrects the
    # visible portal values for an existing lesson.
    lesson: dict[str, Any] = {
        **(copy.deepcopy(existing) if existing else {}),
        "id": f"custom-{override.get('date')}-{period}",
        "name": str(override.get("subject") or "Unterricht"),
        "teacher": str(override.get("teacher") or "") or None,
        "room": str(override.get("room") or "") or None,
        "class_name": str(override.get("class_name") or "") or None,
        "info": str(override.get("info") or "") or None,
        "stunde": start_period,
        "duration": duration,
        "badge": str(override.get("week_type") or "") or None,
        "is_custom": True,
    }
    start_time = _parse_clock(override.get("start_time")) or fallback_start
    end_time = _parse_clock(override.get("end_time")) or fallback_end
    if start_time:
        lesson["start_time"] = start_time
    if end_time:
        lesson["end_time"] = end_time

    if override.get("course_id"):
        lesson["course_id"] = str(override["course_id"])
    elif existing is None:
        lesson.pop("course_id", None)
        lesson.pop("course_name", None)

    return lesson


def _matching_lesson_index(
    lessons: list[object], period: str, override: dict[str, Any]
) -> int | None:
    """Pick one lesson for a period, using the course when available."""
    period_indexes = [
        index
        for index, lesson in enumerate(lessons)
        if isinstance(lesson, dict) and _same_period(lesson, period)
    ]
    if not period_indexes:
        return None

    course_id = str(override.get("course_id") or "").strip()
    if course_id:
        for index in period_indexes:
            lesson = lessons[index]
            if (
                isinstance(lesson, dict)
                and str(lesson.get("course_id") or "").strip() == course_id
            ):
                return index
        return None
    return period_indexes[0]


def _apply_to_plan(
    timetable: dict[str, Any],
    plan: object,
    day_index: int,
    override: dict[str, Any],
) -> None:
    if not isinstance(plan, list) or day_index >= len(plan):
        return
    lessons = plan[day_index]
    if not isinstance(lessons, list):
        return

    period = _period_text(override.get("period"))
    matching_index = _matching_lesson_index(lessons, period, override)
    if override.get("removed"):
        if matching_index is not None:
            plan[day_index] = [
                lesson
                for index, lesson in enumerate(lessons)
                if index != matching_index
            ]
        return

    existing = lessons[matching_index] if matching_index is not None else None
    custom = _custom_raw_lesson(timetable, override, existing)
    if matching_index is not None:
        plan[day_index][matching_index] = custom
        return

    lessons.append(custom)
    lessons.sort(
        key=lambda lesson: (
            _period_start(lesson.get("stunde"))
            if isinstance(lesson, dict)
            and _period_start(lesson.get("stunde")) is not None
            else 999,
            str(lesson.get("name") or "") if isinstance(lesson, dict) else "",
        )
    )


def apply_custom_lessons(
    timetable: dict[str, Any],
    custom_lessons: Iterable[dict[str, Any]],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Apply saved date/period overrides to a raw portal timetable response."""
    result = copy.deepcopy(timetable)
    overrides = [
        copy.deepcopy(lesson)
        for lesson in custom_lessons
        if isinstance(lesson, dict) and lesson.get("date") and lesson.get("period")
    ]
    result["custom_lessons"] = overrides
    if not result.get("success"):
        return result

    monday = current_timetable_monday(today)
    for override in overrides:
        try:
            lesson_date = date.fromisoformat(str(override["date"]))
        except (TypeError, ValueError):
            continue
        day_index = (lesson_date - monday).days
        if not 0 <= day_index < 7:
            continue
        for plan_key in ("plan_for_all", "plan_for_own"):
            _apply_to_plan(result, result.get(plan_key), day_index, override)
    return result


def merge_class_link_overrides(
    overview: dict[str, Any], overrides: dict[str, str]
) -> dict[str, Any]:
    """Merge saved class links into a portal course overview without mutation."""
    result = copy.deepcopy(overview)
    entries = result.get("entries")
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry.pop("course_link_custom", None)
        course_id = str(entry.get("book_id") or "")
        if course_id in overrides:
            entry["course_link"] = overrides[course_id]
            entry["course_link_custom"] = True
    return result


__all__ = [
    "apply_custom_lessons",
    "current_timetable_monday",
    "merge_class_link_overrides",
]
