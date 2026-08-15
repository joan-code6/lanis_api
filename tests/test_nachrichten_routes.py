from starlette.routing import Match

from api.api import app


def test_recipient_search_route_precedes_conversation_route() -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/nachrichten/search",
        "root_path": "",
    }

    first_match = next(
        route for route in app.routes if route.matches(scope)[0] is Match.FULL
    )

    assert first_match.endpoint.__name__ == "search_recipients"
