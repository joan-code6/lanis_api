from schulportal_hessen.applets.mein_unterricht import api as mein_unterricht_api
from schulportal_hessen.applets.mein_unterricht.api import (
    meinunterricht_get_attendance_overview,
    meinunterricht_get_course,
)


class FakeResponse:
    text = """
    <html><body>
      <h1 data-book="42">
        Mathematik 10a
        <small><span class="label-info">2. Halbjahr</span></small>
      </h1>
    </body></html>
    """

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def get(self, *_args, **_kwargs) -> FakeResponse:
        return FakeResponse()


class FakeCryptor:
    authenticated = True


class FakeClient:
    logged_in = True
    cryptor = FakeCryptor()
    session = FakeSession()
    BASE_START_URL = "https://example.invalid"


def test_course_heading_uses_first_visible_text() -> None:
    result = meinunterricht_get_course(FakeClient(), "42")

    assert result["success"] is True
    assert result["course_name"] == "Mathematik 10a"
    assert result["semester"] == "2. Halbjahr"


def test_attendance_overview_combines_course_summaries(monkeypatch) -> None:
    class AttendanceClient:
        logged_in = True

    monkeypatch.setattr(
        mein_unterricht_api,
        "meinunterricht_get_overview",
        lambda _client: {
            "success": True,
            "entries": [
                {"book_id": "1", "name": "Mathe", "teacher_short": "MW"},
                {"book_id": "1", "name": "Mathe"},
                {"book_id": "2", "name": "Deutsch"},
                {"book_id": "3", "name": "Physik"},
            ],
        },
    )

    details = {
        "1": {
            "success": True,
            "course_name": "Mathematik",
            "teacher_short": "MW",
            "attendance_summary": {"Anwesend": "12 Stunden", "Entschuldigt": "1"},
        },
        "2": {
            "success": True,
            "course_name": "Deutsch",
            "attendance_summary": {"anwesend": "8", "unentschuldigt": "2,5"},
        },
        "3": {"success": False, "error": "temporary failure"},
    }
    monkeypatch.setattr(
        mein_unterricht_api,
        "meinunterricht_get_course",
        lambda _client, course_id: details[course_id],
    )

    result = meinunterricht_get_attendance_overview(AttendanceClient())

    assert result["success"] is True
    assert result["source"] == "schulportal"
    assert result["available"] is True
    assert result["totals"] == {
        "anwesend": 20,
        "entschuldigt": 1,
        "unentschuldigt": 2.5,
    }
    assert result["course_count"] == 3
    assert result["attendance_course_count"] == 2
    assert result["failed_course_count"] == 1
    assert result["courses"][0]["attendance_summary"] == {
        "anwesend": 12,
        "entschuldigt": 1,
    }
