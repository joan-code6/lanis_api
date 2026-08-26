from schulportal_hessen.applets.vertretungsplan.api import _parse_non_ajax_day
from schulportal_hessen.base import SchulportalHessenAPI
from schulportal_hessen.html import HTMLParser


def test_client_preserves_unbounded_session_timeout() -> None:
    client = SchulportalHessenAPI()
    try:
        assert client.session.timeout.connect is None
        assert client.session.timeout.read is None
        assert client.session.timeout.write is None
        assert client.session.timeout.pool is None
    finally:
        client.session.close()


def test_non_ajax_substitution_plan_supports_attribute_checks() -> None:
    document = HTMLParser(
        """
        <table id="vtable26_08_2026">
          <thead><tr>
            <th data-field="Stunde">Stunde</th>
            <th data-field="Fach">Fach</th>
          </tr></thead>
          <tbody><tr><td>1 - 2</td><td>Mathematik</td></tr></tbody>
        </table>
        """
    )

    result = _parse_non_ajax_day(document, "26_08_2026")

    assert result is not None
    assert result["substitutions"] == [
        {
            "tag": "26.08.2026",
            "tag_en": "2026-08-26",
            "stunde": "1 - 2",
            "fach": "Mathematik",
            "art": None,
            "raum": None,
            "hinweis": None,
            "lehrer": None,
            "vertreter": None,
            "klasse": None,
        }
    ]
