from datetime import datetime

import pytest

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

    def dsb_get_plan_urls(self, **_):
        return {"success": True, "plan_urls": ["https://example.invalid/plan.htm"]}


def test_dsb_plan_response_includes_last_updated(monkeypatch) -> None:
    monkeypatch.setattr(
        "schulportal_hessen.external.dsb.api.fetch_document", lambda *_: FakeResponse()
    )
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


def test_explicit_and_indexed_plan_selection_use_second_discovered_url(monkeypatch):
    from types import SimpleNamespace

    urls = ["https://plans.example/first.htm", "https://plans.example/second.htm"]
    fetched = []
    client = SimpleNamespace(
        dsb_session=object(),
        dsb_get_plan_urls=lambda **_: {"success": True, "plan_urls": urls},
    )
    monkeypatch.setattr(
        "schulportal_hessen.external.dsb.api.fetch_document",
        lambda _session, url: fetched.append(url) or FakeResponse(),
    )
    assert dsb_get_substitution_plan(client, plan_url=urls[1])["success"]
    assert dsb_get_substitution_plan(client, plan_index=1)["success"]
    assert fetched == [urls[1], urls[1]]


@pytest.mark.parametrize(
    "detail,expected_html",
    [
        ("relative.htm", None),
        ("https://plans.example:8443/plan.htm", None),
        ("https://[broken/plan.htm", None),
        ("https://plans.example:bad/plan.htm", None),
        ("https://user:password@plans.example/plan.htm", None),
        ("https://plans.example/#fragment.htm", None),
        ("https://plans.example/chosen.htm", "https://plans.example/chosen.htm"),
        (
            "https://plans.example:443/chosen.htm",
            "https://plans.example:443/chosen.htm",
        ),
    ],
)
def test_html_detail_discovery_skips_invalid_links_and_preserves_valid_plans(
    monkeypatch, detail, expected_html
):
    import base64
    import gzip
    import json
    from types import SimpleNamespace

    from schulportal_hessen.external.dsb import api as dsb

    menu = {
        "ResultMenuItems": [
            {
                "Detail": "https://plans.example/valid.htm",
                "Childs": [
                    {
                        "Title": "Plan",
                        "Root": {"Childs": [{"Childs": [{"Detail": detail}]}]},
                    }
                ],
            }
        ]
    }
    encoded = base64.b64encode(gzip.compress(json.dumps(menu).encode())).decode()
    session = SimpleNamespace(
        post=lambda *_args, **_kwargs: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"d": encoded}
        )
    )
    client = SimpleNamespace(dsb_logged_in=True, dsb_session=session)
    client.dsb_get_plan_urls = lambda **kwargs: dsb.dsb_get_plan_urls(client, **kwargs)
    monkeypatch.setattr(
        dsb, "_find_getdata_endpoint", lambda _: "https://www.dsbmobile.de/GetData"
    )
    fetched = []
    monkeypatch.setattr(
        dsb,
        "fetch_document",
        lambda _session, url: fetched.append(url) or FakeResponse(),
    )
    discovery = client.dsb_get_plan_urls()
    assert discovery["html_plan_url"] == expected_html
    assert dsb_get_substitution_plan(client)["success"]
    assert fetched == [expected_html or "https://plans.example/valid.htm"]
