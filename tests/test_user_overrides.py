import asyncio
from datetime import date

from api import auth_db
from api.user_overrides import apply_custom_lessons, merge_class_link_overrides


def _raw_timetable() -> dict:
    hours = [
        {
            "label": "1. Stunde",
            "start_time": {"hour": 8, "minute": 0},
            "end_time": {"hour": 8, "minute": 45},
        },
        {
            "label": "2. Stunde",
            "start_time": {"hour": 8, "minute": 50},
            "end_time": {"hour": 9, "minute": 35},
        },
        {
            "label": "3. Stunde",
            "start_time": {"hour": 9, "minute": 50},
            "end_time": {"hour": 10, "minute": 35},
        },
    ]
    lesson = {
        "id": "portal-1",
        "stunde": 1,
        "name": "Mathematik",
        "teacher": "AB",
        "room": "A101",
        "duration": 1,
        "homework": [{"text": "Aufgabe 1"}],
    }
    return {
        "success": True,
        "hours": hours,
        "plan_for_all": [[lesson], []],
        "plan_for_own": [[dict(lesson)], []],
    }


def test_custom_lesson_replaces_portal_lesson_and_preserves_enrichment() -> None:
    result = apply_custom_lessons(
        _raw_timetable(),
        [
            {
                "date": "2026-08-24",
                "period": "1",
                "subject": "Deutsch",
                "teacher": "CD",
                "room": "B202",
                "duration": 1,
                "removed": False,
            }
        ],
        today=date(2026, 8, 24),
    )

    lesson = result["plan_for_all"][0][0]
    assert lesson["name"] == "Deutsch"
    assert lesson["teacher"] == "CD"
    assert lesson["room"] == "B202"
    assert lesson["is_custom"] is True
    assert lesson["homework"] == [{"text": "Aufgabe 1"}]
    assert result["plan_for_own"][0][0]["name"] == "Deutsch"


def test_custom_lesson_recurs_on_the_same_weekday() -> None:
    result = apply_custom_lessons(
        _raw_timetable(),
        [
            {
                "date": "2026-08-24",
                "period": "1",
                "subject": "Deutsch",
                "duration": 1,
                "removed": False,
            }
        ],
        today=date(2026, 8, 31),
    )

    assert result["plan_for_all"][0][0]["name"] == "Deutsch"


def test_custom_lesson_can_be_added_to_an_empty_day_with_portal_times() -> None:
    result = apply_custom_lessons(
        _raw_timetable(),
        [
            {
                "date": "2026-08-25",
                "period": "3",
                "subject": "Biologie",
                "duration": 1,
                "removed": False,
            }
        ],
        today=date(2026, 8, 24),
    )

    lesson = result["plan_for_all"][1][0]
    assert lesson["name"] == "Biologie"
    assert lesson["stunde"] == 3
    assert lesson["start_time"] == {"hour": 9, "minute": 50}
    assert lesson["end_time"] == {"hour": 10, "minute": 35}


def test_custom_lesson_can_hide_a_portal_lesson() -> None:
    result = apply_custom_lessons(
        _raw_timetable(),
        [
            {
                "date": "2026-08-24",
                "period": "1",
                "subject": "",
                "duration": 1,
                "removed": True,
            }
        ],
        today=date(2026, 8, 24),
    )

    assert result["plan_for_all"][0] == []
    assert result["plan_for_own"][0] == []
    assert result["custom_lessons"][0]["removed"] is True


def test_custom_lesson_preserves_parallel_lessons_in_the_same_period() -> None:
    timetable = _raw_timetable()
    parallel = {
        "id": "portal-2",
        "stunde": 1,
        "name": "Englisch",
        "course_id": "course-2",
        "duration": 1,
    }
    timetable["plan_for_all"][0].append(parallel)
    timetable["plan_for_own"][0].append(dict(parallel))

    result = apply_custom_lessons(
        timetable,
        [
            {
                "date": "2026-08-24",
                "period": "1",
                "subject": "Deutsch",
                "duration": 1,
                "removed": False,
            }
        ],
        today=date(2026, 8, 24),
    )

    assert [lesson["name"] for lesson in result["plan_for_all"][0]] == [
        "Deutsch",
        "Englisch",
    ]


def test_course_id_targets_one_parallel_lesson() -> None:
    timetable = _raw_timetable()
    timetable["plan_for_all"][0].append(
        {"id": "portal-2", "stunde": 1, "name": "Englisch", "course_id": "course-2"}
    )

    result = apply_custom_lessons(
        timetable,
        [
            {
                "date": "2026-08-24",
                "period": "1",
                "subject": "Deutsch",
                "course_id": "course-2",
                "duration": 1,
                "removed": False,
            }
        ],
        today=date(2026, 8, 24),
    )

    assert [lesson["name"] for lesson in result["plan_for_all"][0]] == [
        "Mathematik",
        "Deutsch",
    ]


def test_period_zero_is_preserved() -> None:
    timetable = _raw_timetable()
    timetable["plan_for_all"][0][0]["stunde"] = 0

    result = apply_custom_lessons(
        timetable,
        [
            {
                "date": "2026-08-24",
                "period": "0",
                "subject": "Tutorium",
                "duration": 1,
                "removed": False,
            }
        ],
        today=date(2026, 8, 24),
    )

    assert result["plan_for_all"][0][0]["name"] == "Tutorium"
    assert result["plan_for_all"][0][0]["stunde"] == 0


def test_custom_lesson_without_portal_hours_does_not_crash() -> None:
    timetable = _raw_timetable()
    timetable["hours"] = []

    result = apply_custom_lessons(
        timetable,
        [
            {
                "date": "2026-08-25",
                "period": "3",
                "subject": "Biologie",
                "duration": 1,
                "removed": False,
            }
        ],
        today=date(2026, 8, 24),
    )

    assert result["plan_for_all"][1][0]["name"] == "Biologie"


def test_class_link_overrides_do_not_mutate_cached_overview() -> None:
    overview = {
        "success": True,
        "entries": [{"book_id": "course-1", "course_link": "old"}],
    }

    result = merge_class_link_overrides(
        overview, {"course-1": "https://example.test/class"}
    )

    assert result["entries"][0]["course_link"] == "https://example.test/class"
    assert result["entries"][0]["course_link_custom"] is True
    assert overview["entries"][0]["course_link"] == "old"


def test_account_overrides_are_persisted_and_scoped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))

    async def scenario() -> None:
        await auth_db.initialize()
        await auth_db.save_custom_lesson(
            "user-a",
            {
                "date": "2026-08-24",
                "period": "1",
                "subject": "Deutsch",
                "duration": 1,
                "removed": False,
            },
        )
        await auth_db.save_class_link(
            "user-a", "course-1", "https://example.test/class"
        )
        await auth_db.save_custom_lesson(
            "user-b",
            {
                "date": "2026-08-24",
                "period": "1",
                "subject": "Physik",
                "duration": 1,
                "removed": False,
            },
        )

        assert (await auth_db.get_custom_lessons("user-a"))[0]["subject"] == "Deutsch"
        assert (await auth_db.get_custom_lessons("user-b"))[0]["subject"] == "Physik"
        assert await auth_db.get_class_link_overrides("user-a") == {
            "course-1": "https://example.test/class"
        }

        await auth_db.delete_custom_lesson("user-a", "2026-08-24", "1")
        await auth_db.delete_class_link("user-a", "course-1")
        assert await auth_db.get_custom_lessons("user-a") == []
        assert await auth_db.get_class_link_overrides("user-a") == {}

    asyncio.run(scenario())
