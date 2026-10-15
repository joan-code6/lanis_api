from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import api as api_module
from api.api import AuthSession
from schulportal_hessen.applets.dateiverteilung.api import (
    _absolute_portal_url,
    _looks_like_login,
    dateiverteilung_download_file,
    dateiverteilung_get_overview,
    parse_dateiverteilung_html,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dateiverteilung_overview.html"
BASE_URL = "https://start.schulportal.hessen.de"


def test_parser_groups_files_and_preserves_provenance():
    distributions = parse_dateiverteilung_html(FIXTURE.read_text(), BASE_URL)

    assert [item["title"] for item in distributions] == [
        "Elternbrief zum Wandertag",
        "WLAN-Zugang für die Projektwoche",
        "Information zum Schulfest",
    ]
    assert distributions[0] == {
        "id": "elternbrief-2026",
        "title": "Elternbrief zum Wandertag",
        "description": "Bitte bis Freitag unterschrieben zurückgeben.",
        "source": "Schulleitung",
        "created_at": "12.09.2026",
        "unread": True,
        "files": [
            {
                "id": "brief-anna.pdf",
                "name": "Persönlicher Elternbrief.pdf",
                "size": "184 KB",
                "download_url": "https://start.schulportal.hessen.de/dateiverteilung.php?a=download&v=73&f=brief-anna.pdf",
            }
        ],
        "links": [],
    }
    assert distributions[1]["files"][0]["name"] == "Zugangsdaten.txt"
    assert distributions[2]["files"] == []
    assert distributions[2]["links"] == [
        {"label": "Ablauf ansehen", "url": "https://schule.example/schulfest"}
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/dateiverteilung.php?a=download&f=1",
        "//evil.example/dateiverteilung.php?a=download&f=1",
        "/dateispeicher.php?a=download&f=1",
        "/dateiverteilung.php?a=admin&f=1",
        "/dateiverteilung.php?a=download&a=admin&f=1",
        "/dateiverteilung.php?action=download&action=admin&f=1",
        "/dateiverteilung.php?a=download&action=admin&f=1",
        "/dateiverteilung.php?a=download&a[]=admin&f=1",
        "/dateiverteilung.php?a=download&action[]=admin&f=1",
        "/dateiverteilung.php?a=download&%20a=admin&f=1",
        "/dateiverteilung.php?a=download&f=1#fragment",
    ],
)
def test_download_url_validation_rejects_unsafe_urls(url):
    assert _absolute_portal_url(BASE_URL, url) == ""


def test_overview_detects_login_page_without_content_type():
    response = SimpleNamespace(
        headers={},
        url="https://login.schulportal.hessen.de/",
        text="<form><label>Passwort</label></form>",
    )

    assert _looks_like_login(response) is True


class FakeResponse:
    status_code = 200

    def __init__(self):
        self.closed = False
        self.headers = {
            "Content-Disposition": "attachment; filename*=UTF-8''Elternbrief%20Anna.pdf",
            "Content-Type": "application/pdf",
        }

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=8192):
        yield b"personal document"

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.response = FakeResponse()
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


def test_download_streams_through_authenticated_session():
    client = SimpleNamespace(
        logged_in=True, BASE_START_URL=BASE_URL, session=FakeSession()
    )
    url = "/dateiverteilung.php?a=download&v=73&f=brief-anna.pdf"

    result = dateiverteilung_download_file(client, url)

    assert result["success"] is True
    assert result["filename"] == "Elternbrief Anna.pdf"
    assert b"".join(result["stream"]) == b"personal document"
    assert client.session.response.closed is True
    assert client.session.calls[0][1]["allow_redirects"] is False


def test_routes_are_published():
    routes = {
        (route.path, method)
        for route in api_module.app.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/dateiverteilung", "GET") in routes
    assert ("/dateiverteilung/file", "GET") in routes


def test_download_route_maps_validation_errors(monkeypatch):
    auth = AuthSession(
        client=SimpleNamespace(dateiverteilung_download_file=lambda _url: None),
        user_id="user-a",
        school_id="school",
        username="user",
    )

    async def run_in_threadpool(_func, _url):
        return {"success": False, "error": "unsafe", "error_kind": "validation"}

    monkeypatch.setattr(api_module, "run_in_threadpool", run_in_threadpool)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            api_module.download_dateiverteilung_file(
                "https://evil.example/file", auth=auth
            )
        )
    assert error.value.status_code == 400


def test_refresh_bypasses_overview_cache(monkeypatch):
    client = SimpleNamespace(
        dateiverteilung_get_overview=lambda: {
            "success": True,
            "distributions": [],
            "distribution_count": 0,
            "file_count": 0,
            "unread_count": 0,
        }
    )
    auth = AuthSession(
        client=client, user_id="user-a", school_id="school", username="user"
    )

    class FakeSessions:
        def __init__(self):
            self.cached = []

        async def get_cached(self, *_args, **_kwargs):
            raise AssertionError("refresh must bypass the response cache")

        async def set_cache(self, *args):
            self.cached.append(args)

    fake_sessions = FakeSessions()
    monkeypatch.setattr(api_module, "sessions", fake_sessions)

    result = asyncio.run(api_module.get_dateiverteilung(refresh=True, auth=auth))

    assert result["success"] is True
    assert len(fake_sessions.cached) == 1


def test_overview_cache_is_scoped_to_authenticated_user(monkeypatch):
    auth = AuthSession(
        client=SimpleNamespace(dateiverteilung_get_overview=lambda: None),
        user_id="school:user-a",
        school_id="school",
        username="user-a",
    )

    class FakeSessions:
        def __init__(self):
            self.lookups = []

        async def get_cached(self, *args):
            self.lookups.append(args)
            return {"success": True, "distributions": [{"id": "only-user-a"}]}

    fake_sessions = FakeSessions()
    monkeypatch.setattr(api_module, "sessions", fake_sessions)

    result = asyncio.run(api_module.get_dateiverteilung(auth=auth))

    assert result["distributions"][0]["id"] == "only-user-a"
    assert fake_sessions.lookups == [("school:user-a", "/dateiverteilung")]


@pytest.mark.parametrize("action", ["a", "action"])
def test_download_url_accepts_single_download_action(action):
    url = f"/dateiverteilung.php?{action}=download&v=73&f=brief.pdf"
    assert _absolute_portal_url(BASE_URL, url) == BASE_URL + url


@pytest.mark.parametrize("location", ["http://127.0.0.1/private", "https://evil.example/"])
def test_overview_rejects_redirects_without_following(location):
    session = FakeSession()
    session.response.status_code = 302
    session.response.headers = {"Location": location}
    client = SimpleNamespace(logged_in=True, BASE_START_URL=BASE_URL, session=session)

    result = dateiverteilung_get_overview(client)

    assert result["success"] is False
    assert result["error_kind"] == "authentication"
    assert len(session.calls) == 1
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.response.closed is True


def test_module_registry_marks_dateiverteilung_usable(monkeypatch):
    client = api_module.SchulportalHessenAPI()
    client.logged_in = True
    monkeypatch.setattr(client, "_resolve_direct_url", lambda url: url)
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: SimpleNamespace(headers={}, close=lambda: None))

    modules = client.get_available_modules({
        "success": True,
        "data": {"entrys": [{"Name": "Dateiverteilung", "link": "dateiverteilung.php"}]},
    })

    assert modules[0]["usable"] is True
    assert modules[0]["usage"] == ["dateiverteilung_get_overview", "dateiverteilung_download_file"]


def test_current_recipient_panels_and_javascript_download_buttons():
    html = (FIXTURE.parent / "dateiverteilung_recipient.html").read_text()
    distributions = parse_dateiverteilung_html(html, BASE_URL)

    assert [item["id"] for item in distributions] == ["101", "102", "103"]
    assert [item["title"] for item in distributions] == [
        "Hinweise zur Projektwoche", "Persönliche Unterlagen", "Informationen zum Schulfest"
    ]
    assert [item["source"] for item in distributions] == ["Frau Beispiel", "Schulbüro", "Schulleitung"]
    assert [item["unread"] for item in distributions] == [True, True, False]
    assert distributions[0]["description"] == "Treffpunkt ist die Aula.\nBitte pünktlich sein."
    assert distributions[1]["description"] == ""
    assert distributions[1]["files"] == [
        {"id": "201", "name": "Persönliche Datei erstellen und herunterladen", "size": "",
         "download_url": BASE_URL + "/dateiverteilung.php?a=user&v=102&k=201"},
        {"id": "202", "name": "Elternbrief.pdf", "size": "",
         "download_url": BASE_URL + "/dateiverteilung.php?a=user&v=102&k=202"},
    ]
    assert distributions[2]["links"] == [{"label": "Ablauf ansehen", "url": "https://schule.example/ablauf"}]
    assert distributions[0]["created_at"] == "01.10.2026"


@pytest.mark.parametrize("url", [
    "/dateiverteilung.php?a=user",
    "/dateiverteilung.php?a=user&v=102",
    "/dateiverteilung.php?a=user&v=102&k=201&b=read",
    "/dateiverteilung.php?a=user&v=102&k=201&a=admin",
    "/dateiverteilung.php?a=user&v=102&v=103&k=201",
    "/dateiverteilung.php?a=user&v=102&k[]=201",
    "/dateiverteilung.php?a=user&v=102&k=201&k=202",
    "/dateiverteilung.php?a=user&v=not-an-id&k=201",
    "/dateiverteilung.php?a=user&v=102&k=-1",
    "/dateiverteilung.php?action=user&v=102&k=201",
])
def test_recipient_download_rejects_ambiguous_or_non_download_actions(url):
    assert _absolute_portal_url(BASE_URL, url) == ""


def test_recipient_download_uses_the_authenticated_stream():
    client = SimpleNamespace(logged_in=True, BASE_START_URL=BASE_URL, session=FakeSession())
    result = dateiverteilung_download_file(client, "/dateiverteilung.php?a=user&v=102&k=201")
    assert result["success"] is True
    assert b"".join(result["stream"]) == b"personal document"
    assert client.session.response.closed is True


def test_invalid_recipient_button_ids_never_produce_downloads():
    html = '<div class="verteilung" data-id="101"><h3>Hinweis</h3><p class="verteilungmulti" data-id="201&amp;b=delete"><button class="readAndDownload">Datei</button></p></div>'
    assert parse_dateiverteilung_html(html, BASE_URL)[0]["files"] == []


@pytest.mark.parametrize("response_kind, error_kind", [
    ("redirect", "authentication"),
    ("login_html", "authentication"),
    ("http_401", "authentication"),
    ("http_403", "authentication"),
    ("http_404", "upstream"),
    ("timeout", "upstream"),
    ("stream_error", "upstream"),
])
def test_failed_downloads_close_responses(response_kind, error_kind):
    import requests

    session = FakeSession()
    response = session.response
    if response_kind == "redirect":
        response.status_code = 302
    elif response_kind == "login_html":
        response.headers = {"Content-Type": "text/html"}
        response.iter_content = lambda **kwargs: iter([b'<html><form>Passwort</form></html>'])
    elif response_kind.startswith("http_"):
        response.status_code = int(response_kind.removeprefix("http_"))
        def raise_for_status():
            raise requests.HTTPError("upstream error", response=response)
        response.raise_for_status = raise_for_status
    elif response_kind == "timeout":
        def get(*args, **kwargs):
            raise requests.Timeout("upstream timeout")
        session.get = get
    elif response_kind == "stream_error":
        def iter_content(**kwargs):
            raise requests.ConnectionError("stream failed")
        response.iter_content = iter_content
    client = SimpleNamespace(logged_in=True, BASE_START_URL=BASE_URL, session=session)

    result = dateiverteilung_download_file(client, "/dateiverteilung.php?a=user&v=102&k=201")

    assert result["success"] is False
    assert result["error_kind"] == error_kind
    if response_kind != "timeout":
        assert response.closed is True


def test_stream_can_be_cancelled_after_first_chunk():
    client = SimpleNamespace(logged_in=True, BASE_START_URL=BASE_URL, session=FakeSession())
    result = dateiverteilung_download_file(client, "/dateiverteilung.php?a=user&v=102&k=201")
    assert next(result["stream"]) == b"personal document"
    result["stream"].close()
    assert client.session.response.closed is True


def test_html_attachment_is_a_file_not_a_login_page():
    client = SimpleNamespace(logged_in=True, BASE_START_URL=BASE_URL, session=FakeSession())
    client.session.response.headers = {"Content-Disposition": 'attachment; filename="lesson.html"', "Content-Type": "text/html"}
    client.session.response.iter_content = lambda **kwargs: iter([b'<html>School resource</html>'])
    result = dateiverteilung_download_file(client, "/dateiverteilung.php?a=user&v=102&k=201")
    assert result["success"] is True
    assert b"".join(result["stream"]) == b'<html>School resource</html>'


@pytest.mark.parametrize("error_kind, upstream_status, expected_status", [
    ("authentication", None, 401),
    ("validation", None, 400),
    ("upstream", 404, 404),
    ("upstream", 503, 502),
])
def test_download_http_errors_and_session_invalidation(monkeypatch, error_kind, upstream_status, expected_status):
    client = SimpleNamespace(dateiverteilung_download_file=lambda url: {
        "success": False, "error": "Download failed", "error_kind": error_kind,
        "upstream_status": upstream_status,
    })
    auth = AuthSession(client=client, user_id="test-user", school_id="test-school", username="test")
    invalidated = []
    async def invalidate(*args):
        invalidated.append(args)
    monkeypatch.setattr(api_module.sessions, "invalidate_schulportal_client", invalidate)

    with pytest.raises(HTTPException) as error:
        asyncio.run(api_module.download_dateiverteilung_file("/dateiverteilung.php?a=user&v=102&k=201", auth=auth))

    assert error.value.status_code == expected_status
    assert invalidated == ([("test-user", client)] if error_kind == "authentication" else [])


def test_download_response_preserves_filename_and_closes_before_stream_start():
    closed = []
    auth = AuthSession(
        client=SimpleNamespace(dateiverteilung_download_file=lambda url: {
            "success": True, "filename": 'Persönlich "neu".pdf', "content_type": "application/pdf",
            "stream": iter([b"%PDF-test"]), "close": lambda: closed.append(True),
        }), user_id="test-user", school_id="test-school", username="test",
    )
    response = asyncio.run(api_module.download_dateiverteilung_file("/dateiverteilung.php?a=user&v=102&k=201", auth=auth))
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "Pers%C3%B6nlich%20_neu_.pdf" in response.headers["content-disposition"]
    asyncio.run(response.background())
    assert closed == [True]


def test_top_level_download_fragment_has_a_stable_container():
    items = parse_dateiverteilung_html('<a href="dateiverteilung.php?a=download&amp;f=brief.pdf">Brief.pdf</a>', BASE_URL)
    assert len(items) == 1
    assert items[0]["files"][0]["name"] == "Brief.pdf"


def test_empty_recipient_page_never_returns_navigation_as_distributions():
    html = '<nav><a href="dateiverteilung.php">Dateiverteilung</a></nav><div id="content"><h1>Dateiverteilung</h1><div class="alert alert-error">Aktuell stehen keine Dateien zur Verfügung.</div></div>'
    assert parse_dateiverteilung_html(html, BASE_URL) == []
