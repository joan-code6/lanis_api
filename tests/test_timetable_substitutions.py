import asyncio
import copy
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

from api import api as api_module
from api.api import AuthManager, AuthSession
from api.timetable_substitutions import (
    apply_substitutions,
    class_tokens,
    dsb_substitutions,
    native_substitutions,
    periods,
    resolve_timetable,
)

DAY = "2026-09-09"
LESSON = {
    "id": "lesson",
    "subject": "D",
    "teacher": "AB",
    "room": "A1",
    "period": "3–4",
    "duration": 2,
    "course_id": "book",
    "homework": [{"text": "Read"}],
}
SLOTS = [
    {"period": 3, "start_time": "09:40", "end_time": "10:25"},
    {"period": 4, "start_time": "10:25", "end_time": "11:10"},
]


def native(**fields):
    return native_substitutions(
        {
            "days": [
                {
                    "date": DAY,
                    "substitutions": [
                        {
                            "klasse": "10B",
                            "stunde": "3",
                            "fach": "Deutsch",
                            "art": "Entfall",
                            **fields,
                        }
                    ],
                }
            ]
        }
    )


def apply(changes, lessons=None, day=DAY, own_class="10B"):
    return apply_substitutions(
        [{"date": day, "lessons": lessons or [LESSON]}], changes, own_class, SLOTS
    )[0]


def test_partial_double_period_and_metadata_preservation():
    before = copy.deepcopy(LESSON)
    lessons = apply(native())["lessons"]
    assert [item["period"] for item in lessons] == [3, 4]
    assert lessons[0]["cancelled"] is True
    assert not lessons[1].get("cancelled")
    assert lessons[0]["end_time"] == lessons[1]["start_time"] == "10:25"
    assert lessons[0]["course_id"] == "book"
    assert lessons[0]["homework"] == LESSON["homework"]
    assert lessons[0]["id"] != lessons[1]["id"]
    assert LESSON == before


def test_date_class_and_year_group_guards():
    assert class_tokens("05A, Q1/Q2") == {"5a", "q1", "q2"}
    assert apply(native(klasse="10BB"))["lessons"] == [LESSON]
    assert apply(native(), day="2026-09-16")["lessons"] == [LESSON]
    result = apply(native(klasse="Q1/Q2"), own_class="Q1")
    assert result["lessons"] == [LESSON]
    assert len(result["substitutionNotices"]) == 1
    result = apply(native(klasse="Q1/Q2", lehrerkuerzel="AB"), own_class="Q1")
    assert result["lessons"][0]["cancelled"]


def test_native_replacement_uses_original_identifiers():
    lesson = apply(
        native(
            fach_alt="Deutsch",
            fach="Mathe",
            vertreterkuerzel="CD",
            raum="B2",
            art="Vertretung",
        )
    )["lessons"][0]
    assert (lesson["subject"], lesson["teacher"], lesson["room"]) == (
        "Mathe",
        "CD",
        "B2",
    )
    assert lesson["original_lesson"]["subject"] == "D"


def test_ambiguity_conflicts_and_duplicate_print_pages():
    result = apply(native(), [LESSON, {**LESSON, "id": "parallel"}])
    assert len(result["substitutionNotices"]) == 1
    assert all(not lesson.get("cancelled") for lesson in result["lessons"])
    result = apply(native() + native(art="Raumwechsel", raum="B2"))
    assert len(result["substitutionNotices"]) == 2
    assert all(not lesson.get("cancelled") for lesson in result["lessons"])
    assert apply(native() + native())["lessons"][0]["cancelled"]
    result = apply(
        native(stunde="3 - 4"),
        [LESSON, {**LESSON, "id": "parallel", "period": 4, "duration": 1}],
    )
    assert result["lessons"][0]["cancelled"]
    assert all(
        not item.get("cancelled") for item in result["lessons"] if item["period"] == 4
    )


def test_separate_lessons_and_unparseable_periods():
    result = apply(
        native(stunde="3 - 4"), [{**LESSON, "period": n, "duration": 1} for n in (3, 4)]
    )
    assert all(item["cancelled"] for item in result["lessons"])
    result = apply(native(stunde="nach Vereinbarung"))
    assert result["lessons"] == [LESSON]
    assert len(result["substitutionNotices"]) == 1
    for value in ("4–3", "23:59", "0", "3 bis irgendwann"):
        assert periods(value) == []


def test_dsb_arrays_objects_arrow_changes_and_missing_dates():
    headers = ["Klasse(n)", "Stunde", "Art", "Vertreter", "Fach", "Raum"]
    row = ["05A", "3 - 4", "Vertretung", "Fr. Alt →Hr. Neu", "Deutsch →Kunst", "A1 →B2"]
    table = {"date": DAY, "headers": headers, "rows": [row]}
    parsed = dsb_substitutions([table])
    assert parsed == dsb_substitutions([{**table, "rows": [dict(zip(headers, row))]}])
    assert (
        parsed[0]["oldSubject"],
        parsed[0]["subject"],
        parsed[0]["teacher"],
        parsed[0]["room"],
    ) == ("Deutsch", "Kunst", "Hr. Neu", "B2")
    assert dsb_substitutions([{**table, "date": None}]) == []


def timetable():
    return {
        "success": True,
        "week_start": "2026-09-07",
        "week_badge": "A",
        "days": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"],
        "template_plan_for_own": [
            [],
            [],
            [
                {
                    "id": "raw",
                    "name": "D",
                    "teacher": "AB",
                    "stunde": 3,
                    "duration": 2,
                    "badge": "A",
                },
                {"name": "M", "stunde": 3, "duration": 2, "badge": "B"},
            ],
            [],
            [],
        ],
        "template_plan_for_all": [[], [], [], [], []],
    }


def test_projection_week_rollover_overrides_and_alternate_preview():
    raw = timetable()
    before = copy.deepcopy(raw)
    result = resolve_timetable(raw, native(), "10B", date(2026, 9, 9))
    assert result["days"][0]["lessons"][0]["cancelled"]
    assert result["has_alternating_weeks"]
    assert [day["date"] for day in result["days"]] == [
        "2026-09-09",
        "2026-09-10",
        "2026-09-11",
        "2026-09-14",
        "2026-09-15",
    ]
    preview = resolve_timetable(
        raw, native(), "10B", date(2026, 9, 9), "week", week_type="B"
    )
    assert preview["days"][2]["lessons"][0]["subject"] == "M"
    assert not preview["days"][2]["substitutionNotices"]
    next_week = resolve_timetable(raw, native(), "10B", date(2026, 9, 12), "week")
    assert next_week["days"][0]["date"] == "2026-09-14"
    assert next_week["days"][2]["lessons"][0]["subject"] == "M"
    assert raw == before
    raw["custom_lessons"] = [
        {"date": DAY, "period": "3", "subject": "Kunst", "week_type": "B"}
    ]
    assert (
        resolve_timetable(raw, [], "10B", date(2026, 9, 9))["days"][0]["lessons"][0][
            "subject"
        ]
        == "D"
    )
    assert (
        resolve_timetable(raw, [], "10B", date(2026, 9, 16))["days"][0]["lessons"][0][
            "subject"
        ]
        == "Kunst"
    )


def test_school_cache_reuse_isolation_refresh_expiry_and_failure(monkeypatch):
    async def scenario():
        manager = AuthManager()
        monkeypatch.setattr(api_module, "sessions", manager)
        monkeypatch.setattr(api_module, "_school_dsb_lock", asyncio.Lock())
        monkeypatch.setenv("DSB_SCHOOL_ID", "5201")
        monkeypatch.setenv("DSB_USERNAME", "configured-account")
        monkeypatch.setenv("DSB_PASSWORD", "configured-password")

        class Client:
            calls = 0
            fail = False

            closed = 0

            def dsb_get_substitution_plan(self, *credentials):
                assert credentials == ("configured-account", "configured-password")
                self.calls += 1
                return {"success": not self.fail, "tables": [], "revision": self.calls}

            def close(self):
                self.closed += 1

        client = Client()
        monkeypatch.setattr(api_module, "SchulportalHessenAPI", lambda: client)

        class UserClient:
            dsb_logged_in = True

            def dsb_get_substitution_plan(self, *args):
                raise AssertionError(
                    "Never use an arbitrary user's DSB session for a shared plan"
                )

        a = AuthSession(UserClient(), "a", "5201", "a")
        b = AuthSession(client, "b", "5201", "b")
        other = AuthSession(client, "other", "9999", "other")
        first, second = await asyncio.gather(
            api_module.get_school_dsb_plan(auth=a),
            api_module.get_school_dsb_plan(auth=b),
        )
        assert first == second
        assert client.calls == 1
        assert not (await api_module.get_school_dsb_plan(auth=other))["success"]
        assert client.calls == 1
        assert (await api_module.get_school_dsb_plan(refresh=True, auth=a))[
            "revision"
        ] == 2
        for entry in manager._cache.values():
            entry.created_at -= timedelta(seconds=api_module.CACHE_TTL_SECONDS + 1)
        assert (await api_module.get_school_dsb_plan(auth=b))["revision"] == 3
        await manager.invalidate_user_cache("dsb-school:5201")
        client.fail = True
        assert not (await api_module.get_school_dsb_plan(auth=a))["success"]
        client.fail = False
        assert (await api_module.get_school_dsb_plan(auth=a))["revision"] == 5
        assert client.closed == client.calls == 5

    asyncio.run(scenario())


def test_resolved_endpoint_reuses_native_cache_and_handles_partial_failure(monkeypatch):
    async def scenario():
        manager = AuthManager()
        monkeypatch.setattr(api_module, "sessions", manager)

        class Client:
            native_calls = 0

            def vertretungsplan_get_plan(self, _raw):
                self.native_calls += 1
                return {"success": True, "days": []}

            def dsb_get_substitution_plan(self, *_args):
                raise RuntimeError("offline")

        class FailingDsbClient:
            def dsb_get_substitution_plan(self, *_args):
                raise RuntimeError("offline")

            def close(self):
                pass

        monkeypatch.setattr(api_module, "SchulportalHessenAPI", FailingDsbClient)
        client = Client()
        auth = AuthSession(client, "user", "5201", "user")

        async def base(**kwargs):
            return timetable()

        async def modules(**kwargs):
            return {
                "success": True,
                "modules": [{"name": "DSBmobile"}, {"name": "Vertretungsplan"}],
            }

        async def profile(**kwargs):
            return {"success": True, "data": {"klasse": "10B"}}

        async def preferences(user_id):
            return {}, False

        monkeypatch.setattr(api_module, "get_stundenplan", base)
        monkeypatch.setattr(api_module, "get_modules", modules)
        monkeypatch.setattr(api_module, "get_user_data", profile)
        monkeypatch.setattr(api_module, "get_user_preferences", preferences)
        monkeypatch.setattr(api_module, "_school_dsb_lock", asyncio.Lock())
        monkeypatch.setattr(
            api_module, "_school_dsb_credentials", lambda _: ("test", "test")
        )
        for _ in range(2):
            result = await api_module.get_timetable_view(auth=auth)
            assert result["success"]
            assert result["substitution_sources"] == [
                {"name": "Schulportal", "error": False, "updated": None},
                {"name": "DSB", "error": True},
            ]
        assert client.native_calls == 1
        await api_module.get_timetable_view(refresh=True, auth=auth)
        assert client.native_calls == 2

        async def failed_profile(**kwargs):
            raise RuntimeError("profile unavailable")

        monkeypatch.setattr(api_module, "get_user_data", failed_profile)
        result = await api_module.get_timetable_view(auth=auth)
        assert result["success"]
        assert result["substitution_sources"][0] == {
            "name": "Klassenzuordnung",
            "error": True,
        }
        assert result["substitution_sources"][1] == {
            "name": "Schulportal",
            "error": False,
            "updated": None,
        }

    asyncio.run(scenario())


@pytest.mark.skipif(
    not os.getenv("LANIS_LIVE_DSB"), reason="Private live captures are opt-in"
)
def test_real_dsb_and_timetable():
    dsb = json.loads(Path(os.environ["LANIS_LIVE_DSB"]).read_text())
    raw = json.loads(Path(os.environ["LANIS_LIVE_TIMETABLE"]).read_text())
    changes = dsb_substitutions(dsb["tables"])
    assert len(changes) == sum(
        len(table["rows"])
        for table in dsb["tables"]
        if table.get("date") and table.get("headers")
    )
    special = next(
        item
        for item in changes
        if item["classes"] == "10B" and item["date"] == DAY and 3 in item["periods"]
    )
    result = resolve_timetable(raw, changes, "10B", date.fromisoformat(DAY))
    today = result["days"][0]
    third = next(item for item in today["lessons"] if item["period"] == 3)
    assert third["subject"] == "Klassenstunde"
    assert third["teacher"] == special["teacher"]
    assert any(
        item["period"] == 4 and not item.get("substitution")
        for item in today["lessons"]
    )


def test_caption_only_dsb_classes_and_separate_teacher_columns():
    table = {
        "date": DAY,
        "caption": "Klasse 10 B — Vertretungsplan",
        "headers": ["Stunde", "Fach", "Lehrkraft", "Vertretung", "Raum", "Info"],
        "rows": [
            {
                "Stunde": "3",
                "Fach": "Deutsch",
                "Lehrkraft": "AB",
                "Vertretung": "CD",
                "Raum": "B2",
                "Info": "Vertretung",
            }
        ],
    }
    changes = dsb_substitutions([table])
    assert len(changes) == 1
    assert changes[0]["oldTeacher"] == "AB"
    assert changes[0]["teacher"] == "CD"
    assert apply(changes)["lessons"][0]["teacher"] == "CD"
    table["rows"][0]["Info"] = "Entfall"
    assert dsb_substitutions([table])[0]["cancelled"]


def test_spaced_class_identifiers_match_without_merging_separate_classes():
    assert class_tokens("10 a, 05 B / Q 1; Q2") == {"10a", "5b", "q1", "q2"}
    assert class_tokens("10A 10B") == {"10a", "10b"}
    assert apply(native(klasse="10 b"))["lessons"][0]["cancelled"]
    assert apply(native(), own_class="10 b")["lessons"][0]["cancelled"]


def test_resolved_endpoint_honors_saved_class_override(monkeypatch):
    async def scenario():
        async def base(**kwargs):
            return timetable()

        async def modules(**kwargs):
            return {"success": True, "modules": [{"name": "Vertretungsplan"}]}

        async def profile(**kwargs):
            raise AssertionError(
                "A saved class override must not depend on profile availability"
            )

        async def preferences(user_id):
            assert user_id == "user"
            return {"vertretungsplan": {"class_override": "10 B"}}, True

        async def plan(**kwargs):
            return {"success": True, "days": []}

        captured = {}

        def resolve(raw, changes, own_class, *args):
            captured["class"] = own_class
            return {"success": True, "days": []}

        monkeypatch.setattr(api_module, "get_stundenplan", base)
        monkeypatch.setattr(api_module, "get_modules", modules)
        monkeypatch.setattr(api_module, "get_user_data", profile)
        monkeypatch.setattr(api_module, "get_user_preferences", preferences)
        monkeypatch.setattr(api_module, "get_vertretungsplan", plan)
        monkeypatch.setattr(api_module, "resolve_timetable", resolve)
        await api_module.get_timetable_view(
            auth=AuthSession(None, "user", "school", "user")
        )
        assert captured["class"] == "10 B"

    asyncio.run(scenario())


def test_one_sided_weeks_are_previewable_and_metadata_matches_display():
    raw = timetable()
    raw["template_plan_for_own"][2] = [raw["template_plan_for_own"][2][0]]
    opposite = resolve_timetable(
        raw, [], "10B", date(2026, 9, 9), "week", week_type="B"
    )
    assert opposite["has_alternating_weeks"]
    assert opposite["days"][2]["lessons"] == []
    assert opposite["active_week"] == "B"
    raw["template_plan_for_own"][2] = []
    raw["custom_lessons"] = [
        {"date": DAY, "period": "3", "subject": "Kunst", "week_type": "B"}
    ]
    custom = resolve_timetable(raw, [], "10B", date(2026, 9, 9), "week", week_type="B")
    assert custom["days"][2]["lessons"][0]["subject"] == "Kunst"
    assert custom["active_week"] == "B"
    next_week = resolve_timetable(timetable(), [], "10B", date(2026, 9, 12), "week")
    assert next_week["active_week"] == "B"
    assert next_week["week_start"] == "2026-09-14"
    rolling = resolve_timetable(timetable(), [], "10B", date(2026, 9, 13))
    assert rolling["active_week"] == "B"
    assert rolling["week_start"] == "2026-09-14"


def test_unknown_reference_preview_never_applies_dated_changes():
    raw = timetable()
    raw.pop("week_badge")
    preview = resolve_timetable(
        raw, native(fach="Mathematik"), "10B", date(2026, 9, 9), "week", week_type="B"
    )
    assert preview["active_week"] == "B"
    assert preview["days"][2]["lessons"][0]["subject"] == "M"
    assert not preview["days"][2]["lessons"][0].get("cancelled")
    assert not preview["days"][2]["substitutionNotices"]


def test_school_credentials_require_complete_explicit_configuration(monkeypatch):
    for key in ("DSB_SCHOOL_ID", "DSB_USERNAME", "DSB_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    assert api_module._school_dsb_credentials("5201") is None
    monkeypatch.setenv("DSB_USERNAME", "test")
    monkeypatch.setenv("DSB_PASSWORD", " leading and trailing ")
    assert api_module._school_dsb_credentials("") is None
    monkeypatch.setenv("DSB_SCHOOL_ID", "5201")
    assert api_module._school_dsb_credentials("5201") == (
        "test",
        " leading and trailing ",
    )
    assert api_module._school_dsb_credentials("other") is None


def test_caption_only_tables_receive_their_preceding_day_heading():
    from schulportal_hessen.external.dsb.api import _parse_plan_tables

    table = "<table><caption>Klasse 10 B</caption><tr><th>Stunde</th><th>Fach</th><th>Art</th></tr><tr><td>3</td><td>Deutsch</td><td>Entfall</td></tr></table>"
    html = (
        '<div class="mon_title">9.9.2026</div>'
        + table
        + table
        + '<div class="mon_title">10.9.2026</div>'
        + table
    )
    parsed = _parse_plan_tables(html)["tables"]
    assert [item["date"] for item in parsed] == [DAY, DAY, "2026-09-10"]
    result = apply(dsb_substitutions(parsed))
    assert result["lessons"][0]["cancelled"]
    assert result["lessons"][1]["period"] == 4
    assert not result["lessons"][1].get("cancelled")
