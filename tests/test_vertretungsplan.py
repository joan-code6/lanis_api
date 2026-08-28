from __future__ import annotations

from typing import Any, Optional

from schulportal_hessen.applets.vertretungsplan.api import vertretungsplan_get_plan


class FakeResponse:
    def __init__(self, text: str = "", payload: Optional[Any] = None):
        self.text = text
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, html: str, ajax_payload: Optional[Any] = None):
        self.html = html
        self.ajax_payload = ajax_payload

    def get(self, *_args, **_kwargs):
        return FakeResponse(text=self.html)

    def post(self, *_args, **_kwargs):
        return FakeResponse(payload=self.ajax_payload)


class FakeClient:
    BASE_START_URL = "https://portal.example"
    logged_in = True

    def __init__(self, html: str, ajax_payload: Optional[Any] = None):
        self.session = FakeSession(html, ajax_payload)


def test_native_ajax_plan_reports_source_and_availability():
    client = FakeClient(
        '<div data-tag="28.08.2026"></div>',
        [
            {
                "Tag": "28.08.2026",
                "Tag_en": "2026-08-28",
                "Stunde": "1.-2.",
                "Fach": "Mathe",
                "Klasse": "10a",
                "Lehrer": "A",
                "Vertreter": "B",
                "Raum": "A1",
                "Art": "Vertretung",
            }
        ],
    )

    result = vertretungsplan_get_plan(client)

    assert result["success"] is True
    assert result["source"] == "schulportal"
    assert result["available"] is True
    assert result["mode"] == "ajax"
    assert result["count"] == 1
    assert result["days"][0]["substitutions"][0]["stunde"] == "1 - 2"


def test_native_non_ajax_plan_is_parsed_and_reported_available():
    html = """
    <div data-tag="28_08_2026"></div>
    <div id="vtable28_08_2026">
      <table>
        <thead>
          <tr>
            <th data-field="Stunde">Stunde</th>
            <th data-field="Klasse">Klasse</th>
            <th data-field="Fach">Fach</th>
            <th data-field="Art">Art</th>
          </tr>
        </thead>
        <tbody>
          <tr><td>3</td><td>8b</td><td>Deutsch</td><td>Entfall</td></tr>
        </tbody>
      </table>
    </div>
    """

    result = vertretungsplan_get_plan(FakeClient(html))

    assert result["success"] is True
    assert result["available"] is True
    assert result["mode"] == "non_ajax"
    assert result["count"] == 1
    assert result["days"][0]["substitutions"][0]["klasse"] == "8b"


def test_successful_page_without_native_markers_is_unavailable():
    result = vertretungsplan_get_plan(FakeClient("<html><body>Keine Daten</body></html>"))

    assert result == {
        "success": True,
        "source": "schulportal",
        "available": False,
        "mode": "non_ajax",
        "last_updated": None,
        "days": [],
        "count": 0,
        "raw_html": None,
    }
