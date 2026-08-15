from __future__ import annotations

import copy
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
CONNECTOR_WORDS = {"and", "e", "et", "oder", "or", "und", "y"}


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


def _tokens(value: object) -> list[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.findall(r"[^\W_]+", text, flags=re.UNICODE)


def _lesson_subject_code(value: object) -> str:
    parts: list[str] = []
    for token in _tokens(value):
        if any(character.isdigit() for character in token):
            break
        parts.append(token)
    return "".join(parts)


def _course_subject_words(value: object) -> list[str]:
    # Parenthesized and digit-bearing suffixes describe classes/semesters, not
    # the subject itself (for example "10b" or "(1.HJ)").
    without_parentheses = re.sub(r"\([^)]*\)", " ", str(value or ""))
    words: list[str] = []
    for token in _tokens(without_parentheses):
        if any(character.isdigit() for character in token):
            break
        words.append(token)
    return words


def _course_subject_aliases(value: object) -> set[str]:
    words = _course_subject_words(value)
    if not words:
        return set()

    significant = [word for word in words if word not in CONNECTOR_WORDS] or words
    aliases = {"".join(words), "".join(significant)}
    aliases.update(
        word[:length]
        for word in significant
        for length in range(1, min(len(word), 3) + 1)
    )
    aliases.add("".join(word[0] for word in words))
    aliases.add("".join(word[0] for word in significant))

    # Multi-word subjects are commonly abbreviated with one or two leading
    # letters per word (for example a two-word subject can become XY or XxYy).
    if len(significant) > 1:
        for prefix_length in (1, 2):
            aliases.add("".join(word[:prefix_length] for word in significant))
    return {alias for alias in aliases if alias}


def _teacher_code(value: object) -> str:
    return "".join(_tokens(value))


def _course_teacher_codes(course: dict[str, Any]) -> set[str]:
    codes = {_teacher_code(course.get("teacher_short"))}
    full_name = str(course.get("teacher_full_name") or "").strip()
    trailing_code = re.search(r"\(([^()]+)\)\s*$", full_name)
    if trailing_code:
        codes.add(_teacher_code(trailing_code.group(1)))
    return {code for code in codes if code}


def _subject_matches(lesson: dict[str, Any], course: dict[str, Any]) -> bool:
    if _normalise(lesson.get("name")) == _normalise(course.get("name")):
        return True
    lesson_code = _lesson_subject_code(lesson.get("name"))
    return bool(
        lesson_code and lesson_code in _course_subject_aliases(course.get("name"))
    )


def _matching_course(
    lesson: dict[str, Any], courses: list[dict[str, Any]]
) -> dict[str, Any] | None:
    lesson_teacher = _teacher_code(lesson.get("teacher"))
    teacher_matches = [
        course
        for course in courses
        if lesson_teacher and lesson_teacher in _course_teacher_codes(course)
    ]
    if teacher_matches:
        combined_matches = [
            course for course in teacher_matches if _subject_matches(lesson, course)
        ]
        if len(combined_matches) == 1:
            return combined_matches[0]
        if len(teacher_matches) == 1:
            return teacher_matches[0]
        return None

    subject_matches = [course for course in courses if _subject_matches(lesson, course)]
    return subject_matches[0] if len(subject_matches) == 1 else None


def _week_type_for_date(
    value: date, current_monday: date, active_week: str | None
) -> str | None:
    if active_week not in {"A", "B"}:
        return None
    value_monday = value - timedelta(days=value.weekday())
    week_offset = (value_monday - current_monday).days // 7
    if week_offset % 2 == 0:
        return active_week
    return "B" if active_week == "A" else "A"


def _next_course_slot(
    slots: list[tuple[int, int, str]],
    assigned_date: date,
    current_monday: date,
    active_week: str | None,
) -> tuple[int, int] | None:
    """Return the recurring timetable slot immediately after an assignment.

    The timetable is a weekly template rather than a dated event list. Search
    forward from the assignment itself, then map the found occurrence back to
    its weekday/lesson slot in that template. Fourteen days covers a complete
    A/B-week cycle.
    """
    for days_after in range(1, 15):
        candidate_date = assigned_date + timedelta(days=days_after)
        candidate_week = _week_type_for_date(
            candidate_date, current_monday, active_week
        )
        candidates = [
            (day_index, lesson_index)
            for day_index, lesson_index, badge in slots
            if day_index == candidate_date.weekday()
            and (
                badge not in {"A", "B"}
                or candidate_week is None
                or badge == candidate_week
            )
        ]
        if candidates:
            return min(candidates, key=lambda item: item[1])
    return None


def _enrich_plan(
    plan: object,
    course_entries: list[dict[str, Any]],
    courses: list[dict[str, Any]],
    monday: date,
    active_week: str | None,
) -> object:
    if not isinstance(plan, list):
        return plan

    slots_by_course: dict[str, list[tuple[int, int, str]]] = defaultdict(list)

    for day_index, lessons in enumerate(plan):
        for lesson_index, lesson in enumerate(lessons):
            course = _matching_course(lesson, courses)
            if not course:
                continue
            course_id = str(course.get("book_id") or "")
            if not course_id:
                continue
            lesson["course_id"] = course_id
            lesson["course_name"] = str(course.get("name") or lesson.get("name") or "")
            slots_by_course[course_id].append(
                (
                    day_index,
                    lesson_index,
                    str(lesson.get("badge") or "").upper(),
                )
            )

    for entry in course_entries:
        homework_text = str(entry.get("homework") or "").strip()
        assigned_date = _parse_entry_date(entry.get("datum"))
        course_id = str(entry.get("book_id") or "")
        if not homework_text or not assigned_date or not slots_by_course[course_id]:
            continue

        next_occurrence = _next_course_slot(
            slots_by_course[course_id], assigned_date, monday, active_week
        )
        if not next_occurrence:
            continue

        day_index, lesson_index = next_occurrence
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
    courses_by_id: dict[str, dict[str, Any]] = {}
    for entry in course_entries:
        courses_by_id[str(entry.get("book_id"))] = entry
    courses = list(courses_by_id.values())

    current_date = today or datetime.now(BERLIN).date()
    monday = current_date - timedelta(days=current_date.weekday())
    active_week = _active_week(result.get("week_badge"))
    for key in ("plan_for_all", "plan_for_own"):
        result[key] = _enrich_plan(
            copy.deepcopy(result.get(key)),
            course_entries,
            courses,
            monday,
            active_week,
        )
    return result
