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
