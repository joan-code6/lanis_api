"""Normalize substitution sources and resolve dated timetable lessons without mutation."""

from __future__ import annotations

import copy
import re
import unicodedata
from datetime import date, timedelta
from typing import Any

from .user_overrides import apply_custom_lessons


def text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def norm(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", text(value)).lower().split())


def periods(value: Any) -> list[int]:
    value = re.sub(
        r"\b(?:Std|Stunden?)\.?", "", text(value), flags=re.IGNORECASE
    ).strip()
    if not re.fullmatch(r"\d+[\d\s.,–—-]*", value):
        return []
    result = set()
    for part in value.split(","):
        match = re.fullmatch(r"(\d+)\.?\s*(?:[–—-]\s*(\d+)\.?)?", part.strip())
        if not match:
            return []
        first, last = int(match[1]), int(match[2] or match[1])
        if first < 1 or last < first or last > 30:
            return []
        result.update(range(first, last + 1))
    return sorted(result)


def lesson_periods(lesson: dict) -> list[int]:
    parsed = periods(lesson.get("period"))
    if len(parsed) == 1:
        return list(
            range(parsed[0], parsed[0] + max(1, int(lesson.get("duration") or 1)))
        )
    return parsed


def class_tokens(value: Any) -> set[str]:
    value = re.sub(r"(?<=\d)\s+(?=[a-z]\b)", "", norm(value))
    value = re.sub(r"\b([eq])\s+(?=\d)", r"\1", value)
    return {
        re.sub(r"^0+(?=\d)", "", part) for part in re.split(r"[,;/\s]+", value) if part
    }


def same_class(a: Any, b: Any) -> bool:
    return bool(class_tokens(a) & class_tokens(b))


def meaningful(value: str) -> bool:
    return not re.fullmatch(r"(?:[-–—]+|\?)?", value)


def subject_key(value: Any) -> str:
    aliases = {
        "d": "deutsch",
        "m": "mathematik",
        "mathe": "mathematik",
        "e": "englisch",
        "f": "französisch",
        "l": "latein",
        "bio": "biologie",
        "b": "biologie",
        "ch": "chemie",
        "ph": "physik",
        "ku": "kunst",
        "mu": "musik",
        "spo": "sport",
        "sp": "sport",
        "g": "geschichte",
        "pw": "politik und wirtschaft",
        "powi": "politik und wirtschaft",
        "ek": "erdkunde",
    }
    base = re.sub(
        r"\s+(?:[12]\.?\s*(?:fs|fremdsprache)|grundkurs|leistungskurs|gk|lk).*",
        "",
        norm(value),
    )
    return aliases.get(base, base)


def teacher_key(value: Any) -> str:
    return re.sub(r"^(?:hr\.?|fr\.?|herr|frau)\s+", "", norm(value))


def change(value: Any) -> tuple[str, str]:
    parts = re.split(r"\s*(?:→|->)\s*", text(value))
    return parts[0], parts[-1]


def make(**fields: Any) -> dict:
    fields["cancelled"] = bool(
        re.search(
            r"\b(?:entfall|ausfall|entfällt|fällt aus)\b", fields["kind"], re.IGNORECASE
        )
    )
    return fields


def dsb_substitutions(tables: list[dict]) -> list[dict]:
    result = []
    for table in tables:
        try:
            day = date.fromisoformat(text(table.get("date"))).isoformat()
        except ValueError:
            continue
        for row in table.get("rows") or []:
            cells = {
                norm(header): text(
                    row.get(header)
                    if isinstance(row, dict)
                    else row[i]
                    if i < len(row)
                    else ""
                )
                for i, header in enumerate(table.get("headers") or [])
            }

            def get(*names, cells=cells):
                return next((cells[name] for name in names if cells.get(name)), "")

            classes = get("klasse(n)", "klasse", "klassen")
            if not classes:
                token = r"(?:[0-9]{1,2}(?:\s*[a-z]\b)?|[eq]\s*\d)"
                caption = re.search(
                    r"\bklasse(?:n|\(n\))?\s*:?\s*("
                    + token
                    + r"(?:\s*[,;/]\s*"
                    + token
                    + r")*)",
                    norm(table.get("caption")),
                )
                classes = caption[1] if caption else ""
            if not classes:
                continue
            old_subject, subject = change(get("fach"))
            old_teacher, teacher = change(
                get("vertreter", "vertretung", "lehrer", "lehrkraft")
            )
            if get("lehrkraft", "lehrer") and get("vertreter", "vertretung"):
                old_teacher = get("lehrkraft", "lehrer")
            result.append(
                make(
                    source="DSB",
                    date=day,
                    periods=periods(get("stunde", "stunden", "std.")),
                    classes=classes,
                    oldSubject=old_subject,
                    subject=subject,
                    oldTeacher=old_teacher,
                    teacher=teacher,
                    room=change(get("raum"))[1],
                    kind=get("art")
                    or (
                        "Entfall"
                        if re.search(
                            r"\b(?:entfall|ausfall|entfällt)\b",
                            get("info"),
                            re.IGNORECASE,
                        )
                        else "Vertretung"
                    ),
                    info=get(
                        "bemerkungen / hinweise", "hinweis", "text", "bemerkung", "info"
                    ),
                    group=get("lerngruppe", "kurs"),
                )
            )
    return result


def native_substitutions(plan: dict) -> list[dict]:
    return [
        make(
            source="Schulportal",
            date=text(entry.get("tag_en") or day.get("date")),
            periods=periods(entry.get("stunde")),
            classes=text(entry.get("klasse_alt") or entry.get("klasse")),
            subject=text(entry.get("fach")),
            oldSubject=text(entry.get("fach_alt") or entry.get("fach")),
            teacher=text(entry.get("vertreterkuerzel") or entry.get("vertreter")),
            oldTeacher=text(entry.get("lehrerkuerzel") or entry.get("lehrer")),
            room=text(entry.get("raum")),
            kind=text(entry.get("art")),
            info=" · ".join(
                text(entry.get(key))
                for key in ("hinweis", "hinweis2")
                if entry.get(key)
            ),
            group=text(entry.get("lerngruppe")),
        )
        for day in plan.get("days") or []
        for entry in day.get("substitutions") or []
    ]


def apply_substitutions(
    days: list[dict], changes: list[dict], own_class: str, slots: list[dict]
) -> list[dict]:
    unique = []
    for item in changes:
        if not any(
            {k: v for k, v in item.items() if k != "source"}
            == {k: v for k, v in other.items() if k != "source"}
            for other in unique
        ):
            unique.append(item)
    result = copy.deepcopy(days)
    for day in result:
        lessons = day["lessons"]
        relevant = [
            item
            for item in unique
            if item["date"] == day["date"]
            and (
                same_class(item["classes"], own_class)
                or any(
                    same_class(item["classes"], lesson.get("class_name"))
                    for lesson in lessons
                )
            )
        ]
        assigned: dict[int, dict[int, list[dict]]] = {}
        notices = []

        def notice(item, notices=notices):
            if item not in notices:
                notices.append(item)

        for item in relevant:
            scored = []
            for index, lesson in enumerate(lessons):
                if not same_class(
                    item["classes"], lesson.get("class_name") or own_class
                ):
                    continue
                subject = meaningful(item["oldSubject"]) and subject_key(
                    item["oldSubject"]
                ) == subject_key(lesson["subject"])
                teacher = meaningful(item["oldTeacher"]) and teacher_key(
                    item["oldTeacher"]
                ) == teacher_key(lesson.get("teacher"))
                group = bool(item["group"]) and norm(item["group"]) == norm(
                    lesson.get("course_name")
                )
                scored.append((2 * subject + 3 * teacher + 5 * group, index))
            scored.sort(reverse=True)
            concrete = all(
                re.fullmatch(r"\d+[a-z]", value)
                for value in class_tokens(item["classes"])
            )
            special = bool(
                re.search(
                    r"sondereins|klassen(?:leitungs)?stunde|zusatz",
                    item["kind"] + " " + item["subject"],
                    re.IGNORECASE,
                )
            )
            for period in item["periods"]:
                options = [
                    (score, index)
                    for score, index in scored
                    if period in lesson_periods(lessons[index])
                ]
                if (
                    not options
                    or not (
                        (options[0][0] > 0 or special)
                        if concrete
                        else options[0][0] >= 3
                    )
                    or (len(options) > 1 and options[0][0] == options[1][0])
                ):
                    notice(item)
                    continue
                assigned.setdefault(options[0][1], {}).setdefault(period, []).append(
                    item
                )
            if not item["periods"]:
                notice(item)
        resolved = []
        for index, lesson in enumerate(lessons):
            if index not in assigned:
                resolved.append(lesson)
                continue
            runs: list[tuple[list[int], dict | None]] = []
            original_periods = lesson_periods(lesson)
            for period in original_periods:
                applicable = assigned[index].get(period, [])
                if len(applicable) > 1:
                    for item in applicable:
                        notice(item)
                item = applicable[0] if len(applicable) == 1 else None
                if runs and runs[-1][1] == item and runs[-1][0][-1] + 1 == period:
                    runs[-1][0].append(period)
                else:
                    runs.append(([period], item))
            for run, item in runs:
                split = copy.deepcopy(lesson)
                split.update(
                    id=f"{lesson.get('id', index)}-{day['date']}-{run[0]}",
                    period=run[0] if len(run) == 1 else f"{run[0]}–{run[-1]}",
                    duration=len(run),
                )
                for key, period, boundary in (
                    ("start_time", run[0], original_periods[0]),
                    ("end_time", run[-1], original_periods[-1]),
                ):
                    split[key] = next(
                        (
                            slot[key]
                            for slot in slots
                            if slot["period"] == period and slot.get(key)
                        ),
                        lesson.get(key) if period == boundary else None,
                    )
                if item:
                    for key in ("subject", "teacher", "room"):
                        if not item["cancelled"] and meaningful(item[key]):
                            split[key] = item[key]
                    split.update(
                        cancelled=item["cancelled"],
                        substitution=copy.deepcopy(item),
                        original_lesson={
                            key: lesson.get(key)
                            for key in ("subject", "teacher", "room")
                        },
                        info=" · ".join(
                            text(value)
                            for value in (lesson.get("info"), item["info"])
                            if value
                        ),
                    )
                resolved.append(split)
        day.update(lessons=resolved, substitutionNotices=copy.deepcopy(notices))
    return result


def clock(value: Any) -> str:
    if isinstance(value, dict) and "hour" in value and "minute" in value:
        return f"{value['hour']:02d}:{value['minute']:02d}"
    return value if isinstance(value, str) and value else ""


def week_for(day: date, monday: date, reference: str | None) -> str | None:
    if not reference:
        return None
    return (
        reference
        if ((day - monday).days // 7) % 2 == 0
        else "B"
        if reference == "A"
        else "A"
    )


def resolve_timetable(
    timetable: dict,
    changes: list[dict],
    own_class: str,
    today: date,
    view_mode: str = "rolling",
    plan_mode: str = "personal",
    week_type: str | None = None,
) -> dict:
    """Project recurring templates and account overrides before applying dated changes."""
    if not timetable.get("success"):
        return {
            "success": False,
            "days": [],
            "message": timetable.get("error", "Stundenplan nicht verfügbar"),
        }
    monday = today - timedelta(days=today.weekday())
    reference_monday = date.fromisoformat(
        timetable.get("week_start") or monday.isoformat()
    )
    badge = re.search(r"\b([AB])\b", text(timetable.get("week_badge")).upper())
    reference = badge[1] if badge else None
    all_plan = (
        timetable.get("template_plan_for_all", timetable.get("plan_for_all")) or []
    )
    own_plan = (
        timetable.get("template_plan_for_own", timetable.get("plan_for_own")) or []
    )
    plan = all_plan if plan_mode == "all" or not any(own_plan) else own_plan
    overrides = timetable.get("custom_lessons") or []
    week_types = {lesson.get("badge") for day in plan for lesson in day} | {
        item.get("week_type") for item in overrides
    }
    alternating = bool({"A", "B"} & week_types)
    forced = (
        (week_type or ("A" if not reference else None))
        if view_mode == "week" and alternating
        else None
    )
    start = (
        today
        if view_mode == "rolling"
        else monday + timedelta(days=7 if today.weekday() >= 5 else 0)
    )
    dates = [
        start + timedelta(days=i) for i in range(7 if view_mode == "rolling" else 5)
    ]
    slots = [
        {
            "period": (periods(slot.get("label")) or [i + 1])[0],
            "start_time": clock(slot.get("start_time")),
            "end_time": clock(slot.get("end_time")),
        }
        for i, slot in enumerate(timetable.get("hours") or [])
    ]
    days = []
    for day in dates:
        index = day.weekday()
        if index >= 5 or index >= len(timetable.get("days") or []):
            continue
        active = forced or week_for(day, reference_monday, reference)
        filtered = [
            [
                copy.deepcopy(lesson)
                for lesson in entries
                if lesson.get("badge") not in ("A", "B")
                or not active
                or lesson["badge"] == active
            ]
            for entries in plan
        ]
        while len(filtered) < 5:
            filtered.append([])
        applicable = [
            item
            for item in overrides
            if not item.get("week_type") or not active or item["week_type"] == active
        ]
        custom = apply_custom_lessons(
            {
                "success": True,
                "hours": timetable.get("hours", []),
                "plan_for_own": filtered,
            },
            applicable,
        )["plan_for_own"][index]
        lessons = []
        for raw in custom:
            lesson = copy.deepcopy(raw)
            duration = max(1, int(raw.get("duration") or 1))
            period = raw.get("stunde")
            lesson.update(
                subject=raw.get("name") or "Unterricht",
                period=f"{period}–{period + duration - 1}"
                if isinstance(period, int) and duration > 1
                else period,
                duration=duration,
                start_time=clock(raw.get("start_time")),
                end_time=clock(raw.get("end_time")),
                class_name=(
                    raw.get("class_name")
                    or (
                        raw.get("badge")
                        if plan_mode == "all"
                        and raw.get("badge") not in (None, "A", "B")
                        else None
                    )
                ),
                week_type=raw.get("badge") if raw.get("badge") in ("A", "B") else None,
            )
            lessons.append(lesson)
        projected = {
            "date": day.isoformat(),
            "name": timetable["days"][index],
            "lessons": lessons,
        }
        dated_changes = (
            changes
            if not forced
            or (
                reference is not None
                and active == week_for(day, reference_monday, reference)
            )
            else []
        )
        days.extend(apply_substitutions([projected], dated_changes, own_class, slots))
    first_displayed = date.fromisoformat(days[0]["date"]) if days else start
    displayed_monday = first_displayed - timedelta(days=first_displayed.weekday())
    return {
        "success": True,
        "days": days,
        "week_start": displayed_monday.isoformat(),
        "active_week": forced or week_for(first_displayed, reference_monday, reference),
        "has_alternating_weeks": alternating,
        "time_slots": slots,
        "exams": copy.deepcopy(timetable.get("exams") or []),
        "exams_error": timetable.get("exams_error"),
    }
