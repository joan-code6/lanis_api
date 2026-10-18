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


@pytest.mark.parametrize("credentials", [
    {"user_id": "student", "password": "stored-test-password"},
    None,
    {"user_id": "another-student", "password": "other-test-password"},
    {"user_id": "student", "password": ""},
])
def test_submission_delete_without_password_uses_authenticated_account_credentials(
    monkeypatch, credentials
) -> None:
    import httpx

    async def scenario():
        calls, lookups, invalidated = [], [], []

        def delete(*args):
            calls.append(args)
            return {"success": True, "code": "1"}

        auth = SimpleNamespace(
            user_id="student", session_id="test-session",
            client=SimpleNamespace(meinunterricht_delete_uploaded_file=delete),
        )

        async def authenticate(_token):
            return auth

        async def stored(user_id):
            lookups.append(user_id)
            return credentials

        async def invalidate(user_id, endpoint):
            invalidated.append((user_id, endpoint))

        monkeypatch.setattr(api_module, "local_auth_dependency", authenticate)
        monkeypatch.setattr(api_module, "get_refresh_token_by_user_id", stored)
        monkeypatch.setattr(sessions, "invalidate_endpoint_cache", invalidate)
        app.dependency_overrides[api_module.client_dependency] = lambda: auth
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"X-Session-Token": "test-token"},
            ) as client:
                response = await client.request("DELETE", "/meinunterricht/submissions/file", data={
                    "course_id": "42", "entry_id": "7", "upload_id": "9", "file_index": "123",
                })
            assert lookups == ["student"]
            if credentials and credentials.get("password") and credentials["user_id"] == "student":
                assert response.status_code == 200
                assert response.json() == {"success": True, "code": "1"}
                assert calls == [("42", "7", "9", "123", "stored-test-password")]
                assert len(invalidated) == 4
                assert "stored-test-password" not in response.text
            else:
                assert response.status_code == 401
                assert calls == []
                assert invalidated == []
        finally:
            app.dependency_overrides.pop(api_module.client_dependency, None)

    asyncio.run(scenario())


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


@pytest.mark.parametrize("kind", ["list", "detail"])
def test_explicit_refresh_bypasses_cached_submissions(monkeypatch, kind) -> None:
    async def scenario():
        manager = api_module.AuthManager()
        monkeypatch.setattr(api_module, "sessions", manager)
        auth = SimpleNamespace(
            user_id="refresh-student",
            client=SimpleNamespace(
                meinunterricht_get_submissions=lambda: {"success": True, "submissions": ["fresh"]},
                meinunterricht_get_submission=lambda ref: {"success": True, "title": "Fresh"},
            ),
        )
        if kind == "list":
            endpoint, params = "/meinunterricht/submissions", ""
            async def get(refresh=False):
                return await api_module.meinunterricht_submissions(auth, refresh=refresh)
            expected = {"success": True, "submissions": ["fresh"]}
        else:
            endpoint = "/meinunterricht/submissions/detail"
            params = api_module._make_param_key({"detail_ref": "opaque-ref"})
            async def get(refresh=False):
                return await api_module.meinunterricht_submission("opaque-ref", auth, refresh=refresh)
            expected = {"success": True, "submission": {"title": "Fresh"}}
        stale = {"success": True, "cached": True}
        await manager.set_cache(auth.user_id, endpoint, stale, params)
        assert await get() == stale
        assert await get(refresh=True) == expected
        assert await get() == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["list", "detail"])
@pytest.mark.parametrize("older_refresh", [False, True])
def test_refresh_supersedes_inflight_live_cache_and_outage_snapshot(
    monkeypatch, kind, older_refresh
) -> None:
    import json

    import httpx

    from api import outage_cache
    from schulportal_hessen.tools.transport import observation

    async def scenario():
        manager = api_module.AuthManager()
        store = outage_cache.SnapshotStore()
        monkeypatch.setattr(api_module, "sessions", manager)
        monkeypatch.setattr(api_module, "snapshots", store)
        monkeypatch.setattr(outage_cache, "snapshots", store)
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0
        unavailable = False
        user_id = "refresh-race-student"
        auth = SimpleNamespace(user_id=user_id, client=SimpleNamespace(
            meinunterricht_get_submissions=lambda: None,
            meinunterricht_get_submission=lambda ref: None,
        ))

        async def authenticate(_token):
            return auth

        async def run(_func, *_args):
            nonlocal calls
            calls += 1
            if unavailable:
                observation.get().unavailable = True
                return {"success": False, "error": "Upstream unavailable"}
            value = "fresh"
            if calls == 1:
                started.set()
                await release.wait()
                value = "old"
            return {"success": True, "title": value}

        monkeypatch.setattr(api_module, "local_auth_dependency", authenticate)
        monkeypatch.setattr(api_module, "run_in_threadpool", run)
        path = "/meinunterricht/submissions"
        endpoint, params = path, ""
        if kind == "detail":
            path += "/opaque-ref"
            endpoint += "/detail"
            params = api_module._make_param_key({"detail_ref": "opaque-ref"})
        app.dependency_overrides[api_module.client_dependency] = lambda: auth
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"X-Session-Token": "test-token"},
            ) as client:
                pending = asyncio.create_task(client.get(
                    path, params={"refresh": str(older_refresh).lower()}
                ))
                try:
                    await asyncio.wait_for(started.wait(), 1)
                    refreshed = await client.get(path, params={"refresh": "true"})
                finally:
                    release.set()
                    await pending
                assert refreshed.status_code == 200
                expected = refreshed.json()
                payload = expected["submission"] if kind == "detail" else expected
                assert payload["title"] == "fresh"
                assert await manager.get_cached(user_id, endpoint, params) == expected
                key = (user_id, path, ())
                snapshot = store.get(key, store.version(user_id, path))
                assert json.loads(snapshot.body) == expected
                # A failed refresh still has the last successful outage fallback.
                unavailable = True
                fallback = await client.get(path, params={"refresh": "true"})
                assert fallback.json() == expected
                assert fallback.headers["X-LANIS-Cache"] == "stale"
        finally:
            app.dependency_overrides.pop(api_module.client_dependency, None)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [400, 401, 404, 502, None])
def test_submission_download_keeps_failure_status_and_evicts_expired_session(monkeypatch, status):
    from fastapi import HTTPException

    evicted = []
    async def invalidate(user_id, client):
        evicted.append((user_id, client))
    monkeypatch.setattr(sessions, "invalidate_schulportal_client", invalidate)
    auth = SimpleNamespace(user_id="student", client=SimpleNamespace(
        meinunterricht_download_submission_file=lambda ref: {
            "success": False, "status_code": status, "error": "Upstream failure",
        },
    ))
    with pytest.raises(HTTPException) as error:
        asyncio.run(api_module.meinunterricht_submission_file("ref", auth))
    assert error.value.status_code == (status or 502)
    assert evicted == ([("student", auth.client)] if status == 401 else [])


def test_submission_download_preserves_unicode_filename_without_double_encoding():
    from schulportal_hessen.applets.mein_unterricht.api import (
        meinunterricht_download_file,
    )

    response = SimpleNamespace(
        headers={"Content-Disposition": "attachment; filename*=UTF-8''Pr%C3%BCfung.pdf"},
        content=b"pdf", raise_for_status=lambda: None,
    )
    client = SimpleNamespace(
        logged_in=True, BASE_START_URL="https://example.invalid",
        session=SimpleNamespace(get=lambda url: response),
    )
    client.meinunterricht_download_submission_file = lambda ref: meinunterricht_download_file(
        client, "dateiverteilung.php?a=download&f=123"
    )
    result = asyncio.run(api_module.meinunterricht_submission_file(
        "ref", SimpleNamespace(user_id="student", client=client)
    ))
    assert result.body == b"pdf"
    assert result.headers["Content-Disposition"] == "attachment; filename*=UTF-8''Pr%C3%BCfung.pdf"


@pytest.mark.parametrize("kind", ["list", "detail", "file"])
def test_redirected_portal_login_is_not_cached_or_served_as_a_file(monkeypatch, kind):
    import json

    import httpx
    import requests

    from api import outage_cache
    from schulportal_hessen.applets.mein_unterricht import api as course_api
    from schulportal_hessen.applets.mein_unterricht import submissions

    async def scenario():
        manager = api_module.AuthManager()
        store = outage_cache.SnapshotStore()
        monkeypatch.setattr(api_module, "sessions", manager)
        monkeypatch.setattr(api_module, "snapshots", store)
        monkeypatch.setattr(outage_cache, "snapshots", store)
        expired = False
        evicted = []
        base_url = "https://example.invalid"

        def get(url, **_kwargs):
            response = requests.Response()
            response.status_code = 200
            response.encoding = "utf-8"
            response.url = "https://login.schulportal.hessen.de/" if expired else url
            if expired:
                response.headers["Content-Type"] = "text/html"
                response._content = b'<html><form><input name="user2"><input type="password"></form></html>'
            elif kind == "file":
                response.headers["Content-Type"] = "application/pdf"
                response._content = b"%PDF-1.7"
            else:
                response._content = b'<div id="content"><h1>Current assignment</h1></div>'
            return response

        portal = SimpleNamespace(logged_in=True, BASE_START_URL=base_url, session=SimpleNamespace(get=get))
        portal.meinunterricht_get_submissions = lambda: submissions.meinunterricht_get_submissions(portal)
        portal.meinunterricht_get_submission = lambda ref: submissions.meinunterricht_get_submission(portal, ref)
        portal.meinunterricht_download_file = lambda url: course_api.meinunterricht_download_file(portal, url)
        portal.meinunterricht_download_submission_file = lambda ref: submissions.meinunterricht_download_submission_file(portal, ref)
        auth = SimpleNamespace(user_id="login-redirect-student", session_id="test-session", client=portal)

        async def authenticate(_token):
            return auth

        async def evict(user_id, client):
            evicted.append((user_id, client))

        monkeypatch.setattr(api_module, "local_auth_dependency", authenticate)
        monkeypatch.setattr(manager, "invalidate_schulportal_client", evict)
        path = "/meinunterricht/submissions"
        endpoint, params = path, ""
        if kind == "detail":
            ref = submissions._encode_ref("meinunterricht.php?a=sus_abgabe&b=42&e=7&id=9", base_url)
            path += f"/{ref}"
            endpoint += "/detail"
            params = api_module._make_param_key({"detail_ref": ref})
        elif kind == "file":
            ref = submissions._encode_ref("dateiverteilung.php?a=download&f=123", base_url)
            path += f"/file/{ref}"
        app.dependency_overrides[api_module.client_dependency] = lambda: auth
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"X-Session-Token": "test-token"},
            ) as client:
                good = await client.get(path)
                assert good.status_code == 200
                expired = True
                rejected = await client.get(path, params={"refresh": "true"})
            assert rejected.status_code == 401
            assert "Content-Disposition" not in rejected.headers
            assert rejected.headers.get("X-LANIS-Cache") != "stale"
            assert evicted == [(auth.user_id, portal)]
            if kind != "file":
                assert await manager.get_cached(auth.user_id, endpoint, params) == good.json()
                key = (auth.user_id, path, ())
                saved = store.get(key, store.version(auth.user_id, path))
                assert json.loads(saved.body) == good.json()
        finally:
            app.dependency_overrides.pop(api_module.client_dependency, None)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["upload", "delete"])
@pytest.mark.parametrize("failure", ["redirect", "login-form", "http-401"])
def test_submission_mutations_evict_expired_clients_without_retrying(monkeypatch, kind, failure):
    import httpx
    import requests

    from schulportal_hessen.applets.mein_unterricht import submissions

    async def scenario():
        calls, evicted, invalidated, streams = [], [], [], []

        def post(url, **kwargs):
            calls.append(kwargs)
            streams.extend(item[1][1] for item in kwargs.get("files", []))
            response = requests.Response()
            response.status_code = 401 if failure == "http-401" else 200
            response.url = "https://login.schulportal.hessen.de/" if failure == "redirect" else url
            response.encoding = "utf-8"
            response._content = b'<html><form><input name="user2"><input type="password"></form></html>'
            return response

        portal = SimpleNamespace(
            logged_in=True, BASE_START_URL="https://example.invalid",
            session=SimpleNamespace(post=post),
            cryptor=SimpleNamespace(authenticated=True, encrypt=lambda _password: "encrypted-password"),
        )
        portal.meinunterricht_upload_files = lambda *args: submissions.meinunterricht_upload_files(portal, *args)
        portal.meinunterricht_delete_uploaded_file = lambda *args: submissions.meinunterricht_delete_uploaded_file(portal, *args)
        auth = SimpleNamespace(user_id="mutation-student", session_id="test-session", client=portal)

        async def authenticate(_token):
            return auth

        async def evict(user_id, client):
            evicted.append((user_id, client))

        async def invalidate(user_id, endpoint):
            invalidated.append((user_id, endpoint))

        async def stored(user_id):
            return {"user_id": user_id, "password": "stored-test-password"}

        monkeypatch.setattr(api_module, "local_auth_dependency", authenticate)
        monkeypatch.setattr(api_module, "get_refresh_token_by_user_id", stored)
        monkeypatch.setattr(sessions, "invalidate_schulportal_client", evict)
        monkeypatch.setattr(sessions, "invalidate_endpoint_cache", invalidate)
        app.dependency_overrides[api_module.client_dependency] = lambda: auth
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"X-Session-Token": "test-token"},
            ) as client:
                data = {"course_id": "42", "entry_id": "7", "upload_id": "9"}
                if kind == "upload":
                    response = await client.post("/meinunterricht/submissions/upload", data=data,
                                                 files={"files": ("work.pdf", b"pdf", "application/pdf")})
                else:
                    response = await client.request("DELETE", "/meinunterricht/submissions/file",
                                                    data={**data, "file_index": "123"})
            assert response.status_code == 401
            assert evicted == [(auth.user_id, portal)]
            assert invalidated == []
            assert len(calls) == 1  # Never automatically repeat a mutation.
            assert all(stream.closed for stream in streams)
            if kind == "upload":
                assert len(streams) == 1
            else:
                assert calls[0]["data"]["pw"] == "encrypted-password"
            assert "stored-test-password" not in response.text
        finally:
            app.dependency_overrides.pop(api_module.client_dependency, None)

    asyncio.run(scenario())
