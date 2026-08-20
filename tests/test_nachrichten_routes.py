import asyncio
from types import SimpleNamespace

from starlette.routing import Match

from api.api import app, search_recipients, sessions, task_queue


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


def test_recipient_search_refresh_uses_client_query_parameter(monkeypatch) -> None:
    captured = {}

    async def get_cached(*_args, **_kwargs):
        return None

    async def set_cache(*_args, **_kwargs):
        return None

    async def add_task(task):
        captured["task"] = task

    monkeypatch.setattr(sessions, "get_cached", get_cached)
    monkeypatch.setattr(sessions, "set_cache", set_cache)
    monkeypatch.setattr(task_queue, "add_task", add_task)

    client = SimpleNamespace(
        nachrichten_search_recipients=lambda query: {
            "success": True,
            "query": query,
            "results": [],
        }
    )
    auth = SimpleNamespace(user_id="user", client=client)

    result = asyncio.run(search_recipients("Bennet", auth))

    assert result["success"] is True
    assert captured["task"].args[3] == {"query": "Bennet"}
