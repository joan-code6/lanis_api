from __future__ import annotations

import copy
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


def _normalise(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split()).casefold()


def _parse_entry_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None

    iso_match = re.search(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", text)
    if iso_match:
        try:
            return date(*(int(part) for part in iso_match.groups()))
        except ValueError:
            return None

    german_match = re.search(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?!\d)", text)
    if not german_match:
        return None
    day, month, year = (int(part) for part in german_match.groups())
    if year < 100:
        year += 2000
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _active_week(value: object) -> str | None:
    match = re.search(r"\b([AB])\b", str(value or "").upper())
    return match.group(1) if match else None


def _matching_course(
    lesson: dict[str, Any], courses_by_name: dict[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    candidates = courses_by_name.get(_normalise(lesson.get("name")), [])
    if not candidates:
        return None

    by_id = {str(entry.get("book_id") or ""): entry for entry in candidates}
    by_id.pop("", None)
    unique_candidates = list(by_id.values())
    if len(unique_candidates) == 1:
        return unique_candidates[0]

    teacher = _normalise(lesson.get("teacher"))
    teacher_matches = [
        entry
        for entry in unique_candidates
        if teacher and _normalise(entry.get("teacher_short")) == teacher
    ]
    return teacher_matches[0] if len(teacher_matches) == 1 else None


def _eligible_occurrences(
    plan: list[list[dict[str, Any]]],
    monday: date,
    active_week: str | None,
) -> Iterable[tuple[date, int, int, dict[str, Any]]]:
    for day_index, lessons in enumerate(plan):
        lesson_date = monday + timedelta(days=day_index)
        for lesson_index, lesson in enumerate(lessons):
            badge = str(lesson.get("badge") or "").upper()
            if active_week and badge in {"A", "B"} and badge != active_week:
                continue
            yield lesson_date, day_index, lesson_index, lesson


def _enrich_plan(
    plan: object,
    course_entries: list[dict[str, Any]],
    courses_by_name: dict[str, list[dict[str, Any]]],
    monday: date,
    active_week: str | None,
) -> object:
    if not isinstance(plan, list):
        return plan

    occurrences_by_course: dict[str, list[tuple[date, int, int]]] = defaultdict(list)

    for day_index, lessons in enumerate(plan):
        for lesson_index, lesson in enumerate(lessons):
            course = _matching_course(lesson, courses_by_name)
            if not course:
                continue
            course_id = str(course.get("book_id") or "")
            if not course_id:
                continue
            lesson["course_id"] = course_id
            lesson["course_name"] = str(course.get("name") or lesson.get("name") or "")

    for lesson_date, day_index, lesson_index, lesson in _eligible_occurrences(
        plan, monday, active_week
    ):
        course_id = str(lesson.get("course_id") or "")
        if course_id:
            occurrences_by_course[course_id].append(
                (lesson_date, day_index, lesson_index)
            )

    for entry in course_entries:
        homework_text = str(entry.get("homework") or "").strip()
        assigned_date = _parse_entry_date(entry.get("datum"))
        course_id = str(entry.get("book_id") or "")
        if (
            not homework_text
            or not assigned_date
            or not occurrences_by_course[course_id]
        ):
            continue

        next_occurrence = next(
            (
                occurrence
                for occurrence in occurrences_by_course[course_id]
                if occurrence[0] > assigned_date
            ),
            None,
        )
        if not next_occurrence:
            continue

        _, day_index, lesson_index = next_occurrence
        lesson = plan[day_index][lesson_index]
        lesson.setdefault("homework", []).append(
            {
                "entry_id": entry.get("entry_id"),
                "text": homework_text,
                "done": bool(entry.get("homework_done")),
                "assigned_date": assigned_date.isoformat(),
            }
        )

    return plan


def enrich_timetable(
    timetable: dict[str, Any],
    course_overview: dict[str, Any],
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Link timetable lessons to courses and place homework on its next lesson.

    The Schulportal overview associates homework with the lesson date on which it
    was entered. Because it does not expose a reliable assignment time, lessons
    on that same calendar date are deliberately excluded.
    """
    result = copy.deepcopy(timetable)
    if not result.get("success") or not course_overview.get("success"):
        return result

    course_entries = [
        entry
        for entry in course_overview.get("entries", [])
        if isinstance(entry, dict) and entry.get("book_id") and entry.get("name")
    ]
    courses_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in course_entries:
        courses_by_name[_normalise(entry.get("name"))].append(entry)

    current_date = today or datetime.now(BERLIN).date()
    monday = current_date - timedelta(days=current_date.weekday())
    active_week = _active_week(result.get("week_badge"))
    for key in ("plan_for_all", "plan_for_own"):
        result[key] = _enrich_plan(
            copy.deepcopy(result.get(key)),
            course_entries,
            courses_by_name,
            monday,
            active_week,
        )
    return result
