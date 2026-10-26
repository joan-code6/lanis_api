import json
from types import SimpleNamespace

import pytest
import requests

from schulportal_hessen.applets.kalender.api import (
    kalender_get_event,
    kalender_get_events,
)


@pytest.mark.parametrize("detail", [False, True])
def test_calendar_ajax_reads_do_not_parse_portal_navigation_as_json(detail):
    """SPH redirects ordinary navigation requests instead of returning event JSON."""

    def post(url, *, data, headers=None):
        response = requests.Response()
        response.status_code = 200
        headers = headers or {}
        if (
            headers.get("X-Requested-With") != "XMLHttpRequest"
            or headers.get("Referer") != url
        ):
            response._content = b"<html>Portal navigation</html>"
        else:
            assert data["u"] == "selected-view"
            event = {"id": "event-1", "title": "School event"}
            response._content = json.dumps(event if detail else [event]).encode()
        return response

    client = SimpleNamespace(
        logged_in=True,
        BASE_START_URL="https://start.schulportal.hessen.de",
        session=SimpleNamespace(post=post),
        kalender_get_overview=lambda: {
            "success": True,
            "calendar": {"first_id": "default-view"},
        },
    )
    if detail:
        result = kalender_get_event(client, "event-1", view_id="selected-view")
        assert result["success"] is True
        assert result["event"]["id"] == "event-1"
    else:
        result = kalender_get_events(client, view_id="selected-view")
        assert result["success"] is True
        assert result["events"][0]["id"] == "event-1"
