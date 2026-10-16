import asyncio
from types import SimpleNamespace

import pytest
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

    async def get_cache_version(*_args, **_kwargs):
        return 4

    async def set_cache_if_current_version(*args, **_kwargs):
        captured["cache"] = args

    monkeypatch.setattr(sessions, "get_cached", get_cached)
    monkeypatch.setattr(sessions, "get_cache_version", get_cache_version)
    monkeypatch.setattr(
        sessions, "set_cache_if_current_version", set_cache_if_current_version
    )

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
    assert captured["cache"][4] == 4


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


@pytest.mark.parametrize("kind", ["list", "detail", "course"])
def test_submission_mutation_rejects_inflight_cache_writes(monkeypatch, kind) -> None:
    async def scenario():
        manager = api_module.AuthManager()
        monkeypatch.setattr(api_module, "sessions", manager)
        started, release = asyncio.Event(), asyncio.Event()
        stale = {"success": True, "entries": [], "uploaded_count": 0}
        fresh = {"success": True, "uploaded_count": 1}

        def read(*_args):
            return stale

        async def run(func, *args):
            if func is read:
                started.set()
                await release.wait()
            return func(*args)

        monkeypatch.setattr(api_module, "run_in_threadpool", run)
        auth = SimpleNamespace(
            user_id="submission-cache-race", session_id="session-id",
            client=SimpleNamespace(
                meinunterricht_get_submissions=read,
                meinunterricht_get_submission=read,
                meinunterricht_get_course=read,
                meinunterricht_delete_uploaded_file=lambda *args: {"success": True},
            ),
        )
        if kind == "list":
            endpoint, params = "/meinunterricht/submissions", ""
            request = api_module.meinunterricht_submissions(auth)
        elif kind == "detail":
            endpoint = "/meinunterricht/submissions/detail"
            params = api_module._make_param_key({"detail_ref": "opaque-ref"})
            request = api_module.meinunterricht_submission("opaque-ref", auth)
        else:
            endpoint = "/meinunterricht/course"
            params = api_module._make_param_key({"course_id": "42"})
            request = api_module.meinunterricht_course("42", auth)
        pending = asyncio.create_task(request)
        try:
            await asyncio.wait_for(started.wait(), 1)
            await api_module.meinunterricht_submission_delete_file(
                "42", "7", "9", "123", "password", auth
            )
            await manager.set_cache(auth.user_id, endpoint, fresh, params)
        finally:
            release.set()
            await pending
        assert await manager.get_cached(auth.user_id, endpoint, params) == fresh

    asyncio.run(scenario())
