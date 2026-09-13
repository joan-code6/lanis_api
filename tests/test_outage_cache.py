import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import requests
from fastapi import Depends, FastAPI

from api import api as api_module
from api import outage_cache
from schulportal_hessen.tools.transport import ObservedSession, observation


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AsgiResponse:
    def __init__(self, messages):
        start = next(
            message for message in messages if message["type"] == "http.response.start"
        )
        self.status_code = start["status"]
        self.headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in start.get("headers", [])
        }
        self.body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )

    def json(self):
        import json

        return json.loads(self.body)


async def asgi_request(app, method, target, headers=None):
    path, _, query = target.partition("?")
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [
                (key.lower().encode("latin-1"), value.encode("latin-1"))
                for key, value in (headers or {}).items()
            ],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        },
        receive,
        send,
    )
    return AsgiResponse(messages)


@pytest.fixture
def setup(monkeypatch):
    manager = api_module.AuthManager()
    store = outage_cache.SnapshotStore()
    monkeypatch.setattr(api_module, "sessions", manager)
    monkeypatch.setattr(api_module, "snapshots", store)
    monkeypatch.setattr(outage_cache, "snapshots", store)
    state = SimpleNamespace(mode="ok", revoked=False, value=1)

    async def stored(user):
        if state.revoked:
            return None
        return {"school_id": "5201", "username": user, "password": "unused"}

    monkeypatch.setattr(api_module, "get_refresh_token_by_user_id", stored)

    def request(self, method, url, **kwargs):
        assert kwargs["timeout"] == (5, 15)
        if state.mode == "timeout":
            raise requests.ReadTimeout("timeout")
        if state.mode == "tls":
            raise requests.exceptions.SSLError("certificate invalid")
        response = requests.Response()
        response.status_code = {"down": 503, "forbidden": 403}.get(state.mode, 200)
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    app = FastAPI()
    app.router.route_class = outage_cache.OutageCacheRoute

    @app.get("/kalender")
    async def read(auth=Depends(api_module.local_auth_dependency)):  # noqa: B008
        cached = await manager.get_cached(auth.user_id, "/kalender")
        if cached is not None:
            return cached

        def fetch():
            try:
                response = ObservedSession().get("https://example.invalid")
                if response.status_code != 200:
                    return {"success": False, "error": "upstream failure"}
            except requests.RequestException as exc:
                return {"success": False, "error": str(exc)}
            return {"success": True, "value": state.value}

        result = await api_module.run_in_threadpool(fetch)
        await manager.set_cache(auth.user_id, "/kalender", result)
        return result

    token = manager.create_access_token("5201:student", "5201", "student")
    return app, manager, store, state, {"X-Session-Token": token}


def test_sdk_caught_timeout_falls_back_with_original_timestamp_and_recovers(setup):
    app, manager, _store, state, headers = setup

    async def scenario():
        fresh = await asgi_request(app, "GET", "/kalender", headers)
        assert fresh.headers["x-lanis-cache"] == "fresh"
        original = fresh.headers["x-lanis-fetched-at"]
        hit = await asgi_request(app, "GET", "/kalender", headers)
        assert hit.headers["x-lanis-cache"] == "hit"
        manager._cache.clear()
        state.mode = "timeout"
        stale = await asgi_request(app, "GET", "/kalender?refresh=true", headers)
        assert stale.json() == fresh.json()
        assert stale.headers["x-lanis-cache"] == "stale"
        assert stale.headers["x-lanis-fetched-at"] == original
        assert not manager._cache  # SDK-shaped errors never enter ordinary cache.
        state.mode, state.value = "ok", 2
        recovered = await asgi_request(app, "GET", "/kalender", headers)
        assert recovered.json()["value"] == 2
        assert recovered.headers["x-lanis-cache"] == "fresh"

    asyncio.run(scenario())


def test_failed_session_restore_can_serve_an_authenticated_snapshot(setup, monkeypatch):
    _app, _manager, store, state, headers = setup

    class FailingPortal:
        def __init__(self):
            self.session = ObservedSession()

        def login(self, *_credentials):
            try:
                self.session.get("https://example.invalid")
            except requests.RequestException:
                return {"success": False, "error": "upstream unavailable"}
            return {"success": True}

        def close(self):
            self.session.close()

    monkeypatch.setattr(api_module, "SchulportalHessenAPI", FailingPortal)
    app = FastAPI()
    app.router.route_class = outage_cache.OutageCacheRoute

    @app.get("/benutzer")
    async def profile(auth=Depends(api_module.client_dependency)):  # noqa: B008
        return {"success": True, "username": auth.username}

    key = ("5201:student", "/benutzer", ())
    fetched_at = utcnow() - timedelta(minutes=20)
    store.put(
        key,
        b'{"success":true,"name":"Saved"}',
        fetched_at,
        store.version("5201:student", "/benutzer"),
    )
    state.mode = "timeout"

    response = asyncio.run(asgi_request(app, "GET", "/benutzer", headers))

    assert response.status_code == 200
    assert response.json()["name"] == "Saved"
    assert response.headers["x-lanis-cache"] == "stale"
    assert response.headers["x-lanis-fetched-at"] == outage_cache.utc_timestamp(
        fetched_at
    )


@pytest.mark.parametrize("mode", ["tls", "forbidden"])
def test_security_failures_are_not_masked(setup, mode):
    app, manager, _store, state, headers = setup

    async def scenario():
        await asgi_request(app, "GET", "/kalender", headers)
        manager._cache.clear()
        state.mode = mode
        result = await asgi_request(app, "GET", "/kalender", headers)
        assert result.json()["success"] is False
        assert "x-lanis-cache" not in result.headers

    asyncio.run(scenario())


def test_auth_parameters_user_isolation_revocation_and_write_invalidation(setup):
    app, manager, store, state, headers = setup

    async def scenario():
        await asgi_request(app, "GET", "/kalender", headers)
        manager._cache.clear()
        state.mode = "down"
        assert (await asgi_request(app, "GET", "/kalender", headers)).headers[
            "x-lanis-cache"
        ] == "stale"
        assert (
            "x-lanis-cache"
            not in (
                await asgi_request(app, "GET", "/kalender?category=other", headers)
            ).headers
        )
        other = {
            "X-Session-Token": manager.create_access_token(
                "5201:other", "5201", "other"
            )
        }
        assert (
            "x-lanis-cache"
            not in (await asgi_request(app, "GET", "/kalender", other)).headers
        )
        assert (
            await asgi_request(app, "GET", "/kalender", {"X-Session-Token": "invalid"})
        ).status_code == 401
        state.revoked = True
        assert (await asgi_request(app, "GET", "/kalender", headers)).status_code == 401
        state.revoked = False
        await manager.invalidate_endpoint_cache("5201:student", "/kalender")
        assert not store.status("5201:student")["available"]
        assert (
            "x-lanis-cache"
            not in (await asgi_request(app, "GET", "/kalender", headers)).headers
        )

    asyncio.run(scenario())


def test_snapshot_bounds_expiration_and_invalidation_races(monkeypatch):
    store = outage_cache.SnapshotStore()
    now = utcnow()
    key = ("a", "/kalender", ())
    version = store.version("a", "/kalender")
    store.put(key, b"{}", now - timedelta(hours=25), version)
    assert store.get(key, version) is None
    store.put(key, b"{}", now, version)
    store.invalidate("a")
    store.put(key, b"{}", now, version)
    assert store.get(key, store.version("a", "/kalender")) is None
    monkeypatch.setattr(outage_cache, "MAX_USER_ENTRIES", 2)
    monkeypatch.setattr(outage_cache, "MAX_TOTAL_BYTES", 6)
    monkeypatch.setattr(outage_cache, "MAX_BODY_BYTES", 3)
    version = store.version("a", "/kalender")
    for index in range(4):
        candidate = ("a", f"/{index}", ())
        store.put(candidate, b"{}", now, store.version("a", candidate[1]))
    assert len(store.entries) == 2
    store.put(("b", "/x", ()), b"{}", now, store.version("b", "/x"))
    store.put(("c", "/x", ()), b"{}", now, store.version("c", "/x"))
    assert store.total_bytes == 6
    store.put(key, b"large", now, version)
    assert store.get(key, version) is None


def test_endpoint_invalidation_preserves_unrelated_outage_snapshots():
    store = outage_cache.SnapshotStore()
    now = utcnow()
    version = store.version("a", "/kalender")
    calendar = ("a", "/kalender", ())
    timetable = ("a", "/stundenplan", ())
    timetable_view = ("a", "/stundenplan/view", ())
    for key in (calendar, timetable, timetable_view):
        store.put(key, b"{}", now, version)

    store.invalidate_endpoint("a", "/stundenplan")

    current = store.version("a", "/kalender")
    assert store.get(calendar, current) is not None
    assert store.get(timetable, store.version("a", "/stundenplan")) is None


def test_failed_homework_write_keeps_existing_fallback_data(monkeypatch):
    class Client:
        def meinunterricht_set_homework_done(self, *_args):
            return {"success": False, "error": "upstream unavailable"}

    class Sessions:
        async def invalidate_user_cache(self, _user_id):
            raise AssertionError("A failed write must not discard saved reads")

    monkeypatch.setattr(api_module, "sessions", Sessions())
    auth = api_module.AuthSession(
        client=Client(),
        user_id="5201:student",
        school_id="5201",
        username="student",
    )

    result = asyncio.run(
        api_module.meinunterricht_homework_done(
            auth=auth,
            course_id="course",
            entry_id="entry",
            done=True,
        )
    )

    assert result["success"] is False
    assert store.get(timetable_view, store.version("a", "/stundenplan/view")) is None
    # A response that started before invalidation cannot restore stale data.
    store.put(timetable, b"{}", now, version)
    assert store.get(timetable, store.version("a", "/stundenplan")) is None


def test_old_metadata_does_not_age_timetable_snapshot(setup):
    _app, manager, _store, _state, _headers = setup

    async def scenario():
        await manager.set_cache(
            "5201:student", "/modules", {"success": True}, is_long_term=True
        )
        await manager.set_cache("5201:student", "/stundenplan", {"success": True})
        manager._cache[
            manager._make_cache_key("5201:student", "/modules")
        ].created_at -= timedelta(days=10)
        from schulportal_hessen.tools.transport import TransportObservation

        context = TransportObservation(cache_path="/stundenplan/view")
        token = observation.set(context)
        try:
            await manager.get_cached("5201:student", "/modules")
            await manager.get_cached("5201:student", "/stundenplan")
            assert len(context.cached_timestamps) == 1
            assert utcnow() - context.cached_timestamps[0] < timedelta(seconds=5)
        finally:
            observation.reset(token)

    asyncio.run(scenario())
