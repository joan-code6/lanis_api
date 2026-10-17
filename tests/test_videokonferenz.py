from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from fastapi import Response

from api import api as api_module
from schulportal_hessen.applets.videokonferenz.api import (
    parse_videokonferenz_overview,
    videokonferenz_get_rooms,
)

FIXTURE = Path(__file__).parent / "fixtures" / "videokonferenz_overview.html"


def test_parse_room_overview_and_join_states() -> None:
    result = parse_videokonferenz_overview(
        FIXTURE.read_text(encoding="utf-8"),
        "https://start.schulportal.hessen.de/videokonferenz.php",
    )

    assert result["success"] is True
    assert result["available"] is True
    assert result["count"] == 3
    assert result["open_count"] == 1
    assert result["updated_label"] == "08:14 Uhr"

    open_room, waiting_room, closed_room = result["rooms"]
    assert open_room == {
        "id": "mathe-10a",
        "name": "Mathematik 10a",
        "teachers": ["FRA", "BEI"],
        "status": "open",
        "status_label": "Raum betreten",
        "join_url": "https://start.schulportal.hessen.de/videokonferenz.php?a=join&room=42",
        "can_join": True,
        "links": [{"label": "Raumregeln", "url": "https://example.edu/regeln"}],
    }
    assert waiting_room["id"] == "91"
    assert waiting_room["teachers"] == ["WIN", "SOM"]
    assert waiting_room["status"] == "waiting"
    assert waiting_room["links"] == []
    assert closed_room["status"] == "closed"
    assert closed_room["can_join"] is False


def test_recognized_empty_overview_is_available() -> None:
    result = parse_videokonferenz_overview(
        "<h1>Videokonferenz</h1><p>Ansicht aktuell</p>"
        "<table><thead><tr><th>Lerngruppe</th><th>Aktion</th></tr></thead><tbody></tbody></table>"
    )

    assert result["success"] is True
    assert result["available"] is True
    assert result["rooms"] == []


def test_login_page_is_reported_as_authentication_failure() -> None:
    result = parse_videokonferenz_overview(
        '<form action="https://login.schulportal.hessen.de/login"><input name="user"></form>'
    )

    assert result["success"] is False
    assert result["error_kind"] == "authentication"


def test_client_requires_login_and_uses_authenticated_page() -> None:
    assert videokonferenz_get_rooms(SimpleNamespace(logged_in=False))["error_kind"] == "authentication"

    class Response:
        status_code = 200
        text = FIXTURE.read_text(encoding="utf-8")
        url = "https://start.schulportal.hessen.de/videokonferenz.php"

        def raise_for_status(self) -> None:
            return None

    class StatusResponse:
        status_code = 200
        text = '["mathe-10a"]'

        def raise_for_status(self) -> None:
            return None

    class Session:
        def __init__(self) -> None:
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if kwargs.get("params"):
                return StatusResponse()
            return Response()

    client = SimpleNamespace(
        logged_in=True,
        BASE_START_URL="https://start.schulportal.hessen.de",
        session=Session(),
    )
    result = videokonferenz_get_rooms(client)

    assert result["success"] is True
    assert result["status_live"] is True
    assert result["open_count"] == 1
    assert [room["status"] for room in result["rooms"]] == ["open", "waiting", "closed"]
    assert result["rooms"][2]["can_join"] is False
    assert client.session.calls == [
        (
            "https://start.schulportal.hessen.de/videokonferenz.php",
            {"timeout": (10, 30), "allow_redirects": False},
        ),
        (
            "https://start.schulportal.hessen.de/videokonferenz.php",
            {"params": {"a": "sus_start", "b": "update"}, "timeout": (10, 30), "allow_redirects": False},
        ),
    ]


def test_failed_live_status_never_exposes_placeholder_join_links() -> None:
    class Response:
        status_code = 200
        text = FIXTURE.read_text(encoding="utf-8")
        url = "https://start.schulportal.hessen.de/videokonferenz.php"

        def raise_for_status(self) -> None:
            return None

    class Session:
        def get(self, _url, **kwargs):
            if kwargs.get("params"):
                raise requests.Timeout("status unavailable")
            return Response()

    client = SimpleNamespace(
        logged_in=True,
        BASE_START_URL="https://start.schulportal.hessen.de",
        session=Session(),
    )

    result = videokonferenz_get_rooms(client)

    assert result["success"] is True
    assert result["status_live"] is False
    assert result["open_count"] == 0
    assert {room["status"] for room in result["rooms"]} == {"unknown"}
    assert all(room["can_join"] is False for room in result["rooms"])
    assert all(room["join_url"] is None for room in result["rooms"])


def test_route_never_caches_user_bound_join_urls(monkeypatch) -> None:
    auth = SimpleNamespace(
        user_id="5201:test",
        client=SimpleNamespace(
            videokonferenz_get_rooms=lambda: {
                "success": True,
                "available": True,
                "rooms": [],
                "count": 0,
                "open_count": 0,
            }
        ),
    )
    async def run_in_threadpool(function, *args):
        return function(*args)

    monkeypatch.setattr(api_module, "run_in_threadpool", run_in_threadpool)

    first_response = Response()
    fresh_response = Response()
    first = asyncio.run(api_module.get_videokonferenz(response=first_response, refresh=False, auth=auth))
    fresh = asyncio.run(api_module.get_videokonferenz(response=fresh_response, refresh=True, auth=auth))

    assert first["rooms"] == []
    assert fresh["rooms"] == []
    assert first_response.headers["cache-control"] == "private, no-store"
    assert fresh_response.headers["cache-control"] == "private, no-store"


def test_insecure_absolute_urls_are_not_exposed() -> None:
    result = parse_videokonferenz_overview(
        """
        <h1>Videokonferenz</h1><p>Ansicht aktuell</p>
        <table><thead><tr><th>Lerngruppe</th><th>Aktion</th><th>Links</th></tr></thead>
        <tbody><tr><td>Biologie</td><td><a href="http://rooms.example/join">Raum betreten</a></td>
        <td><a href="data:text/plain,test">Material</a></td></tr></tbody></table>
        """
    )

    assert result["rooms"][0]["join_url"] is None
    assert result["rooms"][0]["can_join"] is False
    assert result["rooms"][0]["links"] == []


def test_production_style_blank_header_uses_visible_room_state() -> None:
    result = parse_videokonferenz_overview(
        """
        <h1>Videokonferenz</h1><p>Ansicht aktuell</p>
        <table><thead><tr><th></th><th>Lehrkräfte</th><th>Aktion</th><th>Links</th></tr></thead>
        <tbody><tr><td>Mathematik</td><td>FRA</td><td>
          <a class="btn btn-success hidden joinroom" href="videokonferenz.php?a=goto&amp;room=42">Raum betreten</a>
          <a class="btn btn-danger closedroom" href="videokonferenz.php?a=goto&amp;room=42">Raum nicht offen</a>
        </td><td></td></tr></tbody></table>
        """
    )

    assert result["count"] == 1
    assert result["rooms"][0]["name"] == "Mathematik"
    assert result["rooms"][0]["status"] == "waiting"
    assert result["rooms"][0]["can_join"] is False


def test_module_registry_marks_videokonferenz_usable(monkeypatch) -> None:
    client = api_module.SchulportalHessenAPI()
    client.logged_in = True
    monkeypatch.setattr(client, "_resolve_direct_url", lambda url: url)
    monkeypatch.setattr(client.session, "get", lambda *args, **kwargs: SimpleNamespace(headers={}, close=lambda: None))

    modules = client.get_available_modules(
        {
            "success": True,
            "data": {"entrys": [{"Name": "Videokonferenz", "link": "videokonferenz.php"}]},
        }
    )

    assert modules[0]["usable"] is True
    assert modules[0]["usage"] == ["videokonferenz_get_rooms"]


class VideoResponse:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status
        self.url = "https://start.schulportal.hessen.de/videokonferenz.php"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("upstream failure", response=self)


class VideoSession:
    def __init__(self, overview=None, status=None):
        self.overview = overview or VideoResponse(FIXTURE.read_text())
        self.status = status or VideoResponse('["mathe-10a"]')
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.status if kwargs.get("params") else self.overview
        if isinstance(response, Exception):
            raise response
        return response


def video_client(session):
    return SimpleNamespace(logged_in=True, BASE_START_URL="https://start.schulportal.hessen.de", session=session)


@pytest.mark.parametrize("payload", ['{}', 'null', 'true', '[true]', '[{}]', '[null]', 'not JSON'])
def test_malformed_live_status_is_unknown_and_removes_join_urls(payload):
    session = VideoSession(status=VideoResponse(payload))
    result = videokonferenz_get_rooms(video_client(session))
    assert result["success"] is True
    assert result["status_live"] is False
    assert result["open_count"] == 0
    assert all(room["status"] == "unknown" and not room["can_join"] and room["join_url"] is None for room in result["rooms"])


@pytest.mark.parametrize("which", ["overview", "status"])
@pytest.mark.parametrize("status", [302, 401, 403])
def test_expired_sessions_are_authentication_failures_without_following_redirects(which, status):
    session = VideoSession()
    setattr(session, which, VideoResponse('Login required', status))
    result = videokonferenz_get_rooms(video_client(session))
    assert result["success"] is False
    assert result["error_kind"] == "authentication"
    assert all(kwargs["allow_redirects"] is False for _, kwargs in session.calls)
    assert len(session.calls) == (1 if which == "overview" else 2)


@pytest.mark.parametrize("error", [requests.Timeout("timeout"), requests.ConnectionError("offline")])
def test_overview_transport_failure_is_recoverable_upstream_error(error):
    result = videokonferenz_get_rooms(video_client(VideoSession(overview=error)))
    assert result["success"] is False
    assert result["error_kind"] == "upstream"


@pytest.mark.parametrize("payload", ['[]', '["mathe-10a"]'])
def test_live_status_is_authoritative_even_when_page_buttons_disagree(payload):
    result = videokonferenz_get_rooms(video_client(VideoSession(status=VideoResponse(payload))))
    assert result["status_live"] is True
    assert result["open_count"] == (0 if payload == '[]' else 1)
    assert result["rooms"][0]["can_join"] == (payload != '[]')
    assert result["rooms"][1]["can_join"] is False


@pytest.mark.parametrize("url", [
    'https://user:secret@example.invalid/join', 'https://[broken/join',
    'https://example.invalid:99999/join', 'https://example.invalid:bad/join',
    'javascript:alert(1)', 'data:text/html,test', 'http://example.invalid/join', '   ',
])
def test_invalid_or_credential_bearing_room_links_cannot_be_joined(url):
    html = f'<table><thead><tr><th>Lerngruppe</th><th>Aktion</th></tr></thead><tbody><tr><td>Testkurs</td><td><a class="btn btn-success" href="{url}">Raum betreten</a></td></tr></tbody></table>'
    result = parse_videokonferenz_overview(html)
    assert result["rooms"][0]["join_url"] is None
    assert result["rooms"][0]["can_join"] is False


@pytest.mark.parametrize("kind, expected_evictions", [("authentication", 1), ("upstream", 0)])
def test_route_evicts_only_expired_clients_and_always_disables_caching(monkeypatch, kind, expected_evictions):
    client = SimpleNamespace(videokonferenz_get_rooms=lambda: {"success": False, "error_kind": kind})
    auth = SimpleNamespace(client=client, user_id="test:account")
    evicted = []
    async def invalidate(*args):
        evicted.append(args)
    monkeypatch.setattr(api_module.sessions, "invalidate_schulportal_client", invalidate)
    response = Response()
    result = asyncio.run(api_module.get_videokonferenz(response=response, auth=auth))
    assert result["success"] is False
    assert len(evicted) == expected_evictions
    if evicted:
        assert evicted[0] == (auth.user_id, client)
    assert response.headers["cache-control"] == "private, no-store"


def test_live_recipient_structure_keeps_polling_ids_and_ignores_hidden_placeholder_state():
    result = parse_videokonferenz_overview((FIXTURE.parent / "videokonferenz_recipient.html").read_text())
    assert result["available"] is True
    assert result["count"] == 2
    assert [room["id"] for room in result["rooms"]] == ["101", "102"]
    assert [room["status"] for room in result["rooms"]] == ["waiting", "waiting"]
    assert result["rooms"][0]["teachers"] == ["BEI", "MUS"]
    assert result["rooms"][0]["links"] == [{"label": "Material", "url": "https://school.example/material"}]
    assert result["updated_label"] == "15:30 Uhr"


def test_closed_cards_remain_visible_and_live_status_is_checked():
    html = ('<div class="conference-room" data-room-id="closed-1"><h2>Room one</h2></div>'
            '<div class="conference-room" data-room-id="closed-2"><h2>Room two</h2>'
            '<button disabled>Raum geschlossen</button></div>')
    parsed = parse_videokonferenz_overview(html)
    assert parsed["count"] == 2
    assert all(room["status"] == "closed" and not room["can_join"] for room in parsed["rooms"])
    calls = []
    def get(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, text='[]' if kwargs.get("params") else html,
                               url=url, raise_for_status=lambda: None)
    result = videokonferenz_get_rooms(SimpleNamespace(logged_in=True,
        BASE_START_URL="https://start.schulportal.hessen.de", session=SimpleNamespace(get=get)))
    assert len(calls) == 2
    assert result["status_live"] is True
    assert result["count"] == 2
    assert all(room["status"] == "closed" and not room["can_join"] for room in result["rooms"])


def test_ordinary_login_link_does_not_expire_a_valid_overview():
    result = parse_videokonferenz_overview(
        FIXTURE.read_text(encoding="utf-8")
        + '<a href="https://login.schulportal.hessen.de/">Portal login</a>')
    assert result["success"] is True
    assert result["count"] == 3


def test_login_response_host_is_still_rejected():
    result = parse_videokonferenz_overview('<h1>Login</h1>',
        'https://login.schulportal.hessen.de/')
    assert result["success"] is False
    assert result["error_kind"] == 'authentication'
