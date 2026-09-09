from schulportal_hessen.applets.mein_unterricht import api as mein_unterricht_api
from schulportal_hessen.applets.mein_unterricht.api import (
    meinunterricht_get_attendance_overview,
    meinunterricht_get_course,
    meinunterricht_get_overview,
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


def test_course_summary_can_skip_entry_decryption() -> None:
    class SummaryResponse:
        text = """
        <html><body>
          <h1 data-book="42">Mathematik 10a</h1>
          <div id="attendanceTable">
            <table><tr><td>Anwesend</td><td>12 Stunden</td></tr></table>
          </div>
        </body></html>
        """

        def raise_for_status(self) -> None:
            return None

    class SummarySession:
        def get(self, *_args, **_kwargs):
            return SummaryResponse()

    class SummaryClient:
        logged_in = True
        cryptor = FakeCryptor()
        session = SummarySession()
        BASE_START_URL = "https://example.invalid"

    result = meinunterricht_get_course(
        SummaryClient(), "42", decrypt_attendance=False
    )

    assert result["success"] is True
    assert result["attendance_summary"] == {"Anwesend": "12 Stunden"}


def test_overview_includes_course_folders_without_activity_rows() -> None:
    class OverviewResponse:
        text = """
        <html><body>
          <table><tbody>
            <tr data-book="1">
              <td><a href="meinunterricht.php?a=sus_view&amp;id=1"><span class="name">Mathe</span></a></td>
            </tr>
          </tbody></table>
          <div class="course-folder">
            <a href="meinunterricht.php?a=sus_view&amp;id=2"><span class="name">Informatik</span></a>
          </div>
        </body></html>
        """

        def raise_for_status(self) -> None:
            return None

    class OverviewSession:
        def get(self, *_args, **_kwargs):
            return OverviewResponse()

    class OverviewClient:
        logged_in = True
        session = OverviewSession()
        BASE_START_URL = "https://example.invalid"

    result = meinunterricht_get_overview(OverviewClient())

    assert result["success"] is True
    assert result["course_count"] == 2
    assert {course["book_id"] for course in result["courses"]} == {"1", "2"}


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
        lambda _client, course_id, decrypt_attendance=True: details[course_id],
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


def test_attendance_overview_enumerates_course_folders_without_recent_entries(monkeypatch) -> None:
    class AttendanceClient:
        logged_in = True

    monkeypatch.setattr(
        mein_unterricht_api,
        "meinunterricht_get_overview",
        lambda _client: {
            "success": True,
            "entries": [{"book_id": "1", "name": "Mathe"}],
            "courses": [
                {"book_id": "1", "name": "Mathe"},
                {"book_id": "2", "name": "Informatik"},
            ],
        },
    )
    monkeypatch.setattr(
        mein_unterricht_api,
        "meinunterricht_get_course",
        lambda _client, course_id, decrypt_attendance=True: {
            "success": True,
            "course_name": course_id,
            "attendance_summary": {"Anwesend": "1"},
        },
    )

    result = meinunterricht_get_attendance_overview(AttendanceClient())

    assert result["course_count"] == 2
    assert {course["course_id"] for course in result["courses"]} == {"1", "2"}


def test_attendance_overview_marks_unparseable_summaries_as_failed(monkeypatch) -> None:
    class AttendanceClient:
        logged_in = True

    monkeypatch.setattr(
        mein_unterricht_api,
        "meinunterricht_get_overview",
        lambda _client: {
            "success": True,
            "courses": [{"book_id": "1", "name": "Mathe"}],
            "entries": [],
        },
    )
    monkeypatch.setattr(
        mein_unterricht_api,
        "meinunterricht_get_course",
        lambda _client, _course_id, decrypt_attendance=True: {
            "success": True,
            "attendance_summary": {"Anwesend": "nicht verfügbar"},
        },
    )

    result = meinunterricht_get_attendance_overview(AttendanceClient())

    assert result["success"] is True
    assert result["totals"] == {}
    assert result["courses"] == []
    assert result["failed_course_count"] == 1


def test_course_decrypts_summary_even_when_entry_decryption_is_disabled(monkeypatch):
    class SummaryCryptor:
        authenticated = False

        def __init__(self, session):
            pass

        def authenticate(self):
            self.authenticated = True
            return True

        def decrypt(self, value):
            assert self.authenticated
            return {
                'U2FsdGVkX1label': '<span>Anwesend</span><span class="hidden">ignored</span>',
                'U2FsdGVkX1count': '<strong>12</strong> Stunden',
            }[value]

    class SummarySession:
        def get(self, *_args, **_kwargs):
            # The key must be established before requesting encrypted cells.
            assert client.cryptor.authenticated
            response = FakeResponse()
            response.text = '''<div id="attendanceTable"><table><tr>
                <td><encoded>U2FsdGVkX1label</encoded></td>
                <td><encoded>U2FsdGVkX1count</encoded></td>
                </tr></table></div>'''
            return response

    monkeypatch.setattr(mein_unterricht_api, 'Cryptor', SummaryCryptor)
    client = FakeClient()
    client.cryptor = None
    client.session = SummarySession()
    result = meinunterricht_get_course(client, '42', decrypt_attendance=False)
    assert result['success'] is True
    assert result['attendance_summary'] == {'Anwesend': '12 Stunden'}


def test_attendance_counts_reject_ciphertext_and_preserve_zero():
    parse = mein_unterricht_api._parse_attendance_count
    assert parse('U2FsdGVkX1encrypted123') is None
    assert parse('error 2') is None
    assert parse('-1') is None
    assert parse(0) == 0
    assert parse('2,5 Stunden') == 2.5


def test_course_does_not_return_ciphertext_when_summary_decryption_fails():
    class BrokenCryptor:
        authenticated = True

        def decrypt(self, value):
            raise ValueError('Invalid encrypted data')

    class SummarySession:
        def get(self, *_args, **_kwargs):
            response = FakeResponse()
            response.text = '''<div id="attendanceTable"><table><tr>
                <td><encoded>U2FsdGVkX1label</encoded></td><td>2</td>
                </tr></table></div>'''
            return response

    client = FakeClient()
    client.cryptor = BrokenCryptor()
    client.session = SummarySession()
    result = meinunterricht_get_course(client, '42', decrypt_attendance=False)
    assert result['success'] is False
    assert 'attendance_summary' not in result


def test_attendance_overview_does_not_report_empty_course_as_failed(monkeypatch):
    monkeypatch.setattr(
        mein_unterricht_api, "meinunterricht_get_overview",
        lambda _client: {"success": True, "courses": [{"book_id": "42"}]},
    )
    monkeypatch.setattr(
        mein_unterricht_api, "meinunterricht_get_course",
        lambda *_args, **_kwargs: {"success": True, "attendance_summary": {}},
    )
    result = meinunterricht_get_attendance_overview(FakeClient())
    assert result["success"] is True
    assert result["available"] is False
    assert result["course_count"] == 1
    assert result["failed_course_count"] == 0
    assert result["totals"] == {}
