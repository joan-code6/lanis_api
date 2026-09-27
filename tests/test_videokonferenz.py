from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

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
        text = FIXTURE.read_text(encoding="utf-8")
        url = "https://start.schulportal.hessen.de/videokonferenz.php"

        def raise_for_status(self) -> None:
            return None

    class StatusResponse:
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
    assert client.session.calls == [
        (
            "https://start.schulportal.hessen.de/videokonferenz.php",
            {"timeout": (10, 30)},
        ),
        (
            "https://start.schulportal.hessen.de/videokonferenz.php",
            {"params": {"a": "sus_start", "b": "update"}, "timeout": (10, 30)},
        ),
    ]


def test_failed_live_status_never_exposes_placeholder_join_links() -> None:
    class Response:
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
