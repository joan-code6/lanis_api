from datetime import date

import pytest

from api.timetable_enrichment import _course_subject_aliases, enrich_timetable


def _timetable(*lessons_by_day, week_badge="Woche A"):
    return {
        "success": True,
        "days": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"],
        "plan_for_all": [list(day) for day in lessons_by_day],
        "plan_for_own": [list(day) for day in lessons_by_day],
        "week_badge": week_badge,
    }


def _lesson(name, teacher="AB", badge=None):
    return {"name": name, "teacher": teacher, "badge": badge, "stunde": 1}


def _entry(**overrides):
    entry = {
        "entry_id": "entry-1",
        "book_id": "course-1",
        "name": "Mathematik",
        "teacher_short": "AB",
        "datum": "11.08.2026",
        "homework": "Seite 42, Aufgaben 1–4",
        "homework_done": False,
    }
    entry.update(overrides)
    return entry


def test_links_every_matching_lesson_but_places_homework_only_on_next_lesson():
    timetable = _timetable(
        [_lesson(" Mathematik ")],
        [],
        [_lesson("MATHEMATIK")],
        [],
        [_lesson("Mathematik")],
    )

    result = enrich_timetable(
        timetable, {"success": True, "entries": [_entry()]}, today=date(2026, 8, 12)
    )

    monday, wednesday, friday = (
        result["plan_for_all"][0][0],
        result["plan_for_all"][2][0],
        result["plan_for_all"][4][0],
    )
    assert monday["course_id"] == "course-1"
    assert wednesday["homework"] == [
        {
            "entry_id": "entry-1",
            "text": "Seite 42, Aufgaben 1–4",
            "done": False,
            "assigned_date": "2026-08-11",
        }
    ]
    assert "homework" not in monday
    assert "homework" not in friday


def test_preserves_completed_state_and_supports_iso_dates():
    timetable = _timetable([], [], [_lesson("Mathematik")], [], [])
    result = enrich_timetable(
        timetable,
        {
            "success": True,
            "entries": [_entry(datum="2026-08-11T09:00:00", homework_done=True)],
        },
        today=date(2026, 8, 12),
    )
    assert result["plan_for_own"][2][0]["homework"][0]["done"] is True


def test_uses_teacher_to_disambiguate_courses_with_the_same_name():
    timetable = _timetable([], [_lesson("Sport", teacher="XY")], [], [], [])
    entries = [
        _entry(book_id="course-a", name="Sport", teacher_short="AB", homework=""),
        _entry(book_id="course-b", name="Sport", teacher_short="XY", homework=""),
    ]
    result = enrich_timetable(
        timetable, {"success": True, "entries": entries}, today=date(2026, 8, 12)
    )
    assert result["plan_for_all"][1][0]["course_id"] == "course-b"


@pytest.mark.parametrize(
    ("timetable_name", "course_name"),
    [
        ("CH", "Chemie 10b"),
        ("E 1.FS", "Englisch 10b"),
        ("ETH", "Ethik 10"),
        ("KU", "Kunst 10b (1.HJ)"),
        ("PW", "Politik und Wirtschaft 10b"),
        ("WU", "WU Informatik (10a, 10b)"),
    ],
)
def test_derives_subject_abbreviations_without_a_subject_map(
    timetable_name, course_name
):
    timetable = _timetable([_lesson(timetable_name, teacher="XY")], [], [], [], [])
    result = enrich_timetable(
        timetable,
        {
            "success": True,
            "entries": [
                _entry(
                    book_id="derived-course",
                    name=course_name,
                    teacher_short="XY",
                    homework="",
                )
            ],
        },
        today=date(2026, 8, 12),
    )
    assert result["plan_for_all"][0][0]["course_id"] == "derived-course"


def test_uses_teacher_code_from_trailing_parentheses():
    timetable = _timetable([_lesson("M", teacher="ST")], [], [], [], [])
    entries = [
        _entry(
            book_id="chemistry",
            name="Chemie 10b",
            teacher_short="",
            teacher_full_name="Stohr, Corinna (ST)",
            homework="",
        ),
        _entry(
            book_id="mathematics",
            name="Mathematik 10b",
            teacher_short="",
            teacher_full_name="Stohr, Corinna (ST)",
            homework="",
        ),
    ]
    result = enrich_timetable(
        timetable, {"success": True, "entries": entries}, today=date(2026, 8, 12)
    )
    assert result["plan_for_all"][0][0]["course_id"] == "mathematics"


def test_unique_teacher_match_supports_unfamiliar_subject_codes():
    timetable = _timetable([_lesson("XYZ", teacher="AB")], [], [], [], [])
    result = enrich_timetable(
        timetable,
        {
            "success": True,
            "entries": [
                _entry(name="Unbekanntes Wahlfach 10b", teacher_short="AB", homework="")
            ],
        },
        today=date(2026, 8, 12),
    )
    assert result["plan_for_all"][0][0]["course_id"] == "course-1"


def test_course_aliases_are_derived_from_words_and_connectors():
    aliases = _course_subject_aliases("Politik und Wirtschaft 10b")
    assert {"pw", "powi", "politikundwirtschaft"} <= aliases


def test_does_not_guess_when_duplicate_courses_remain_ambiguous():
    timetable = _timetable([_lesson("Sport", teacher="")], [], [], [], [])
    entries = [
        _entry(book_id="course-a", name="Sport", teacher_short="AB"),
        _entry(book_id="course-b", name="Sport", teacher_short="XY"),
    ]
    result = enrich_timetable(
        timetable, {"success": True, "entries": entries}, today=date(2026, 8, 12)
    )
    assert "course_id" not in result["plan_for_all"][0][0]


def test_homework_ignores_inactive_ab_week_lesson():
    timetable = _timetable(
        [],
        [],
        [_lesson("Mathematik", badge="B")],
        [_lesson("Mathematik", badge="A")],
        [],
        week_badge="Woche A",
    )
    result = enrich_timetable(
        timetable,
        {"success": True, "entries": [_entry(datum="10.08.2026")]},
        today=date(2026, 8, 12),
    )
    assert "homework" not in result["plan_for_all"][2][0]
    assert result["plan_for_all"][3][0]["homework"][0]["entry_id"] == "entry-1"


def test_homework_is_not_attached_to_a_lesson_on_the_assignment_date():
    timetable = _timetable(
        [],
        [_lesson("Mathematik")],
        [],
        [_lesson("Mathematik")],
        [],
    )
    result = enrich_timetable(
        timetable,
        {"success": True, "entries": [_entry(datum="11.08.2026")]},
        today=date(2026, 8, 12),
    )
    assert "homework" not in result["plan_for_all"][1][0]
    assert result["plan_for_all"][3][0]["homework"][0]["entry_id"] == "entry-1"


def test_assignment_from_previous_week_maps_to_its_immediate_next_weekday_slot():
    timetable = _timetable(
        [_lesson("Mathematik")],
        [],
        [_lesson("Mathematik")],
        [],
        [],
    )
    result = enrich_timetable(
        timetable,
        {"success": True, "entries": [_entry(datum="04.08.2026")]},
        today=date(2026, 8, 12),
    )
    assert "homework" not in result["plan_for_all"][0][0]
    assert result["plan_for_all"][2][0]["homework"][0]["entry_id"] == "entry-1"


def test_friday_assignment_maps_to_monday_in_the_next_week():
    timetable = _timetable([_lesson("Mathematik")], [], [], [], [])
    result = enrich_timetable(
        timetable,
        {"success": True, "entries": [_entry(datum="14.08.2026")]},
        today=date(2026, 8, 12),
    )
    assert result["plan_for_all"][0][0]["homework"][0]["entry_id"] == "entry-1"


def test_next_week_transition_respects_alternating_week_type():
    timetable = _timetable(
        [
            _lesson("Mathematik", badge="A"),
            _lesson("Mathematik", badge="B"),
        ],
        [],
        [],
        [],
        [],
        week_badge="Woche A",
    )
    result = enrich_timetable(
        timetable,
        {"success": True, "entries": [_entry(datum="14.08.2026")]},
        today=date(2026, 8, 12),
    )
    monday_a, monday_b = result["plan_for_all"][0]
    assert "homework" not in monday_a
    assert monday_b["homework"][0]["entry_id"] == "entry-1"


def test_missing_or_unparseable_homework_data_keeps_course_link_only():
    timetable = _timetable([], [], [_lesson("Mathematik")], [], [])
    result = enrich_timetable(
        timetable,
        {
            "success": True,
            "entries": [
                _entry(datum="unbekannt"),
                _entry(entry_id="entry-2", homework=""),
            ],
        },
        today=date(2026, 8, 12),
    )
    lesson = result["plan_for_all"][2][0]
    assert lesson["course_id"] == "course-1"
    assert "homework" not in lesson


def test_failed_course_overview_returns_an_unchanged_copy():
    timetable = _timetable([_lesson("Mathematik")], [], [], [], [])
    result = enrich_timetable(timetable, {"success": False}, today=date(2026, 8, 12))
    assert result == timetable
    assert result is not timetable
