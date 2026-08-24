from datetime import datetime

from schulportal_hessen.external.dsb.api import (
    _parse_last_updated,
    dsb_get_substitution_plan,
)


def test_parse_last_updated_from_stand_marker() -> None:
    html = "<p>Vertretungsplan <strong>Stand: 21.08.2026 11:30</strong></p>"

    assert _parse_last_updated(html) == datetime.fromisoformat("2026-08-21T11:30:00")


def test_parse_last_updated_uses_latest_marker_and_optional_seconds() -> None:
    html = """
    <p>Stand: 20.08.2026 15:05</p>
    <p>Stand: 21.8.2026 07:04:09</p>
    """

    assert _parse_last_updated(html) == datetime.fromisoformat("2026-08-21T07:04:09")


def test_parse_last_updated_returns_none_when_marker_is_missing() -> None:
    assert _parse_last_updated("<p>Kein Zeitstempel vorhanden</p>") is None


class FakeResponse:
    text = """
    <html><body>
      <h1>Vertretungsplan</h1>
      <p>Stand: 21.08.2026 11:30</p>
      <table>
        <tr><th>Klasse</th></tr>
        <tr><td>Q2</td></tr>
      </table>
    </body></html>
    """

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def get(self, *_args, **_kwargs) -> FakeResponse:
        return FakeResponse()


class FakeClient:
    dsb_logged_in = True
    dsb_session = FakeSession()


def test_dsb_plan_response_includes_last_updated() -> None:
    result = dsb_get_substitution_plan(
        FakeClient(), plan_url="https://example.invalid/plan.htm"
    )

    assert result["success"] is True
    assert result["last_updated"] == "2026-08-21T11:30:00"
