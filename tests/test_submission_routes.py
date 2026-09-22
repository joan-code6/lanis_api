import asyncio
from types import SimpleNamespace

from starlette.routing import Match

from api import api as api_module
from api.api import app, sessions


def _full_route(path: str, method: str = "GET"):
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    return next(route for route in app.routes if route.matches(scope)[0] is Match.FULL)


def test_submission_file_route_precedes_detail_route() -> None:
    assert _full_route("/meinunterricht/submissions/file/example").endpoint.__name__ == (
        "meinunterricht_submission_file"
    )
    assert _full_route("/meinunterricht/submissions/example").endpoint.__name__ == (
        "meinunterricht_submission"
    )


def test_submission_detail_route_returns_nested_contract_and_caches_success(monkeypatch) -> None:
    captured = {}

    async def get_cached(*_args, **_kwargs):
        return None

    async def set_cache(*args, **_kwargs):
        captured["cache"] = args

    monkeypatch.setattr(sessions, "get_cached", get_cached)
    monkeypatch.setattr(sessions, "set_cache", set_cache)

    auth = SimpleNamespace(
        user_id="student",
        client=SimpleNamespace(
            meinunterricht_get_submission=lambda detail_ref: {
                "success": True,
                "detail_ref": detail_ref,
                "title": "Mathe",
            }
        ),
    )

    result = asyncio.run(api_module.meinunterricht_submission("opaque-ref", auth))

    assert result == {
        "success": True,
        "submission": {"detail_ref": "opaque-ref", "title": "Mathe"},
    }
    assert captured["cache"][0:2] == ("student", "/meinunterricht/submissions/detail")


def test_submission_delete_invalidates_submission_caches(monkeypatch) -> None:
    invalidated = []

    async def invalidate_endpoint_cache(user_id, endpoint):
        invalidated.append((user_id, endpoint))

    monkeypatch.setattr(sessions, "invalidate_endpoint_cache", invalidate_endpoint_cache)

    auth = SimpleNamespace(
        user_id="student",
        client=SimpleNamespace(
            meinunterricht_delete_uploaded_file=lambda *args: {
                "success": True,
                "code": "1",
            }
        ),
    )

    result = asyncio.run(
        api_module.meinunterricht_submission_delete_file(
            "42", "7", "9", "123", "password", auth
        )
    )

    assert result["success"] is True
    assert invalidated == [
        ("student", "/meinunterricht/submissions"),
        ("student", "/meinunterricht/submissions/detail"),
        ("student", "/meinunterricht/course"),
        ("student", "/meinunterricht/course/*"),
    ]
