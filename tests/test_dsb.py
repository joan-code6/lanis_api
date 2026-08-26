from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from schulportal_hessen.external.dsb import api as dsb_api
from schulportal_hessen.external.dsb.api import (
    _decode_dsb_response,
    _parse_last_updated,
    dsb_get_substitution_plan,
)

UTF8_PLAN_HTML = """
<html><body>
  <h1>Vertretungsplan</h1>
  <p>Stand: 21.08.2026 11:30</p>
  <table>
    <tr><th>Klasse</th><th>Vertreter</th></tr>
    <tr><td>Q2</td><td>Hr. Geweniger → Fr. Müller</td></tr>
  </table>
</body></html>
"""


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
    content = UTF8_PLAN_HTML.encode("utf-8")
    text = content.decode("latin-1")

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
    assert result["tables"][0]["rows"][0]["Vertreter"] == "Hr. Geweniger → Fr. Müller"


def test_decode_dsb_response_uses_utf8_bytes_when_charset_is_missing() -> None:
    response = FakeResponse()

    decoded = _decode_dsb_response(response)

    assert "Müller" in decoded
    assert "MÃ¼ller" not in decoded


@pytest.mark.parametrize(
    "login_html",
    [
        "",
        f"<html><body>{'x' * 120}</body></html>",
        f'<form><input name="__VIEWSTATE" value="state"></form>{"x" * 120}',
    ],
)
def test_dsb_login_error_urls_are_strings(monkeypatch, login_html: str) -> None:
    class LoginResponse:
        text = login_html
        content = login_html.encode()
        status_code = 200
        url = httpx.URL("https://www.dsbmobile.de/Login.aspx")

        def raise_for_status(self) -> None:
            return None

    class LoginSession:
        def get(self, url: str, **_kwargs):
            if url.endswith("Login.aspx"):
                return LoginResponse()
            return LoginResponse()

    monkeypatch.setattr(dsb_api, "_make_dsb_session", LoginSession)

    result = dsb_api.dsb_login(SimpleNamespace(), "user", "password")

    assert result["success"] is False
    assert result["url"] == "https://www.dsbmobile.de/Login.aspx"
    assert isinstance(result["url"], str)
