"""Exercise the real ASGI middleware boundary: demo requests must never escape."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from api import api as api_module
from api.demo import ACCESS_TOKEN, DISTRICT, REFRESH_TOKEN, SCHOOL, sample_data


def run_request(method, path, **kwargs):
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app), base_url="http://test"
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(run())


@pytest.fixture(autouse=True)
def no_real_work(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Demo escaped into a real service")

    monkeypatch.setattr(api_module, "SchulportalHessenAPI", forbidden)
    for name in (
        "store_refresh_token",
        "get_refresh_token",
        "get_refresh_token_by_session_id",
        "get_user_preferences",
        "delete_refresh_token_by_session_id",
    ):
        monkeypatch.setattr(api_module, name, forbidden)
    monkeypatch.setattr(api_module.task_queue, "add_task", forbidden)
    monkeypatch.setattr(api_module.user_metrics_db, "record_login", forbidden)
    monkeypatch.setattr(api_module.user_metrics_db, "record_activity", forbidden)
    monkeypatch.setattr(api_module.sessions, "decode_access_token", forbidden)
    from api import analytics_store

    for name in ("record_request", "save_diagnostic", "account_reference"):
        monkeypatch.setattr(analytics_store, name, forbidden)


@pytest.mark.parametrize("password", ["", "anything", "reviewer-password"])
def test_login_and_refresh_without_upstream_or_persistence(password, caplog):
    response = run_request(
        "POST",
        "/login",
        json={
            "school_id": "9999",
            "username": "Vornahme.Nachname",
            "password": password,
        },
    )
    assert response.status_code == 200
    assert response.json()["access_token"] == ACCESS_TOKEN
    assert response.json()["refresh_token"] == REFRESH_TOKEN
    assert "X-LANIS-Request-ID" not in response.headers
    refreshed = run_request(
        "POST", "/auth/refresh", json={"refresh_token": REFRESH_TOKEN}
    )
    assert refreshed.json()["access_token"] == ACCESS_TOKEN
    assert not [r for r in caplog.records if r.name.startswith("lanis")]


def test_reserved_school_never_contacts_sph_for_other_names():
    assert (
        run_request(
            "POST",
            "/login",
            json={"school_id": "9999", "username": "someone", "password": "x"},
        ).status_code
        == 401
    )


@pytest.mark.parametrize("path", [p for p in sample_data() if "?" not in p])
def test_all_sample_reads_are_isolated(path):
    response = run_request(
        "GET",
        path,
        headers={
            "X-Session-Token": ACCESS_TOKEN,
            "X-LANIS-Analytics-Consent": "account-v2",
        },
    )
    assert response.status_code == 200, (path, response.text)
    assert "X-LANIS-Request-ID" not in response.headers


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/nachrichten/send"),
        ("POST", "/nachrichten/reply"),
        ("POST", "/meinunterricht/homework-done"),
        ("POST", "/meinunterricht/submissions/upload"),
        ("DELETE", "/meinunterricht/submissions/file"),
        ("POST", "/wahlen/demo-election/submit"),
        ("PATCH", "/settings/preferences"),
        ("PUT", "/notifications/preferences"),
        ("POST", "/notifications/subscription"),
        ("POST", "/notifications/test"),
        ("POST", "/feedback"),
        ("POST", "/analytics/diagnostics"),
        ("DELETE", "/account"),
        ("POST", "/logout"),
    ],
)
def test_actions_have_no_side_effects(method, path):
    response = run_request(
        method, path, headers={"X-Session-Token": ACCESS_TOKEN}, json={}
    )
    assert response.status_code == 200
    # Actions do not erase or mutate the shared public sample data.
    assert run_request(
        "GET", "/meinunterricht", headers={"X-Session-Token": ACCESS_TOKEN}
    ).json()["entries"]


@pytest.mark.parametrize(
    "path", ["/admin", "/admin/users", "/mcp", "/oauth/token", "/agents", "/unknown"]
)
def test_demo_never_falls_through_to_real_routes(path):
    assert run_request(
        "GET", path, headers={"X-Session-Token": ACCESS_TOKEN}
    ).status_code in {403, 404}


def test_download_and_cors():
    response = run_request(
        "GET",
        "/meinunterricht/file/example?token=" + ACCESS_TOKEN,
        headers={"Origin": "https://lanis.arg-server.de"},
    )
    assert response.status_code == 200
    assert "Beispieldatei" in response.text
    assert (
        response.headers["access-control-allow-origin"] == "https://lanis.arg-server.de"
    )


def test_school_directory_and_search(monkeypatch):
    from datetime import datetime, timezone

    from api.demo import school_directory

    original = {
        "success": True,
        "districts": [{"id": "1", "name": "Real", "schools": []}],
    }
    directory = school_directory(original)
    assert directory["districts"][-1] == DISTRICT
    assert original["districts"][0]["schools"] == []
    assert school_directory(directory) == directory
    monkeypatch.setitem(api_module.school_list_cache, "data", directory)
    monkeypatch.setitem(
        api_module.school_list_cache,
        "created_at",
        datetime.now(timezone.utc).replace(tzinfo=None),
    )
    assert run_request("GET", "/school-list").json()["districts"][-1]["schools"] == [
        SCHOOL
    ]
    assert (
        run_request("GET", "/school-list/district/lanis-demo").json()["district"]
        == DISTRICT
    )
    client = type(
        "Client",
        (),
        {
            "school_list_search_by_name": lambda self, q: {
                "success": True,
                "results": [],
            }
        },
    )()
    monkeypatch.setattr(api_module, "SchulportalHessenAPI", lambda: client)
    assert (
        run_request("GET", "/school-list/search?q=Lanis%20Test").json()["results"][0][
            "school"
        ]
        == SCHOOL
    )


def test_normal_login_still_uses_real_handler(monkeypatch):
    normal = AsyncMock(
        return_value={
            "access_token": "real",
            "refresh_token": "real",
            "school_id": "1234",
            "username": "Vornahme.Nachname",
            "encryption_ready": False,
        }
    )
    monkeypatch.setattr(api_module, "_login_account", normal)
    assert (
        run_request(
            "POST",
            "/login",
            json={
                "school_id": "1234",
                "username": "Vornahme.Nachname",
                "password": "x",
            },
        ).status_code
        == 200
    )
    normal.assert_awaited_once()


def test_write_contracts_echo_without_persisting():
    headers = {"X-Session-Token": ACCESS_TOKEN}
    response = run_request(
        "PATCH",
        "/settings/preferences",
        headers=headers,
        json={"appearance": {"theme_mode": "dark"}},
    )
    assert response.json()["preferences"]["appearance"]["theme_mode"] == "dark"
    lesson = {"date": "2026-10-27", "period": "1", "subject": "Test"}
    response = run_request(
        "PUT", "/settings/timetable/lessons", headers=headers, json=lesson
    )
    assert response.json()["lesson"] == {**lesson, "is_custom": True}
    assert (
        run_request("GET", "/settings/timetable/lessons", headers=headers).json()[
            "lessons"
        ]
        == []
    )
    response = run_request("GET", "/search/semantic?q=Deutsch", headers=headers)
    assert response.json()["results"][0]["title"] == "Deutsch 9c"


@pytest.mark.parametrize(
    "current_date,expected_monday",
    [
        ("2026-10-26", "2026-10-26"),
        ("2026-11-01", "2026-10-26"),
        ("2026-11-02", "2026-11-02"),
        ("2026-11-03", "2026-11-02"),
        ("2026-11-08", "2026-11-02"),
        ("2026-11-09", "2026-11-09"),
    ],
)
def test_fixture_dates_follow_calendar_week(monkeypatch, current_date, expected_monday):
    from datetime import date, datetime, timedelta

    from api import demo

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(current_date + "T12:00:00").replace(tzinfo=tz)

    monkeypatch.setattr(demo, "datetime", FrozenDateTime)
    # A Tuesday export must advance to the next week on Monday, not Tuesday.
    monkeypatch.setattr(
        demo,
        "_FIXTURES",
        {
            **demo._FIXTURES,
            "generated_on": "2026-10-27",
        },
    )
    data = demo.sample_data()
    timetable = data["/stundenplan"]
    assert timetable["week_start"] == expected_monday
    assert (
        timetable["week_end"]
        == (date.fromisoformat(expected_monday) + timedelta(days=4)).isoformat()
    )
    assert data["/meinunterricht/weekly"]["week"]["start_date"] == expected_monday
    assert [
        date.fromisoformat(day["date"]).weekday() for day in timetable["days"]
    ] == list(range(5))


@pytest.mark.parametrize(
    "current_date,mode,expected_dates",
    [
        (
            "2026-11-02",
            "rolling",
            ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"],
        ),
        (
            "2026-11-04",
            "rolling",
            ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"],
        ),
        (
            "2026-11-04",
            "week",
            ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"],
        ),
        (
            "2026-11-07",
            "rolling",
            ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"],
        ),
        (
            "2026-11-08",
            "week",
            ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"],
        ),
    ],
)
def test_resolved_demo_timetable_projects_requested_dates(
    monkeypatch, current_date, mode, expected_dates
):
    from datetime import date, datetime, timedelta

    from api import demo

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(current_date + "T12:00:00").replace(tzinfo=tz)

    monkeypatch.setattr(demo, "datetime", FrozenDateTime)
    before = demo.sample_data()["/stundenplan"]
    headers = {"X-Session-Token": ACCESS_TOKEN}
    response = run_request(
        "GET",
        "/stundenplan/view",
        headers=headers,
        params={"view_mode": mode, "show_substitutions": "false"},
    )
    assert response.status_code == 200
    result = response.json()
    assert [day["date"] for day in result["days"]] == expected_dates
    first = date.fromisoformat(expected_dates[0])
    assert result["week_start"] == (first - timedelta(days=first.weekday())).isoformat()
    assert result["substitution_sources"] == []
    for day in result["days"]:
        weekday = date.fromisoformat(day["date"]).weekday()
        assert [lesson["subject"] for lesson in day["lessons"]] == [
            lesson["subject"] for lesson in before["days"][weekday]["lessons"]
        ]
        assert (
            day["lessons"][0]["start_time"]
            == before["days"][weekday]["lessons"][0]["start_time"]
        )
    assert demo.sample_data()["/stundenplan"] == before


@pytest.mark.parametrize(
    "query", ["view_mode=invalid", "plan_mode=invalid", "week_type=C"]
)
def test_demo_timetable_rejects_invalid_view_parameters(query):
    assert (
        run_request(
            "GET",
            "/stundenplan/view?" + query,
            headers={"X-Session-Token": ACCESS_TOKEN},
        ).status_code
        == 422
    )
