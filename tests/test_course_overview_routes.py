"""Invalid portal pages must not replace the course overview caches."""

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI

from api import api as api_module
from api import outage_cache
from schulportal_hessen.applets.mein_unterricht.api import meinunterricht_get_overview
from tests.test_outage_cache import asgi_request


def test_unrecognized_overview_preserves_snapshot_and_recovers(monkeypatch):
    async def scenario():
        manager = api_module.AuthManager()
        store = outage_cache.SnapshotStore()
        monkeypatch.setattr(api_module, "sessions", manager)
        monkeypatch.setattr(api_module, "snapshots", store)
        monkeypatch.setattr(outage_cache, "snapshots", store)
        state = SimpleNamespace(html='<tr data-book="42"><td><span class="name">Mathe</span></td></tr>')
        portal = SimpleNamespace(
            logged_in=True,
            BASE_START_URL="https://start.schulportal.hessen.de",
            session=SimpleNamespace(get=lambda *_args: SimpleNamespace(
                text=state.html, raise_for_status=lambda: None,
                url="https://start.schulportal.hessen.de/meinunterricht.php",
            )),
        )
        portal.meinunterricht_get_overview = lambda: meinunterricht_get_overview(portal)
        auth = SimpleNamespace(user_id="overview-test", client=portal)

        async def authenticate(_token):
            return auth

        async def overrides(_user):
            return {}

        monkeypatch.setattr(api_module, "local_auth_dependency", authenticate)
        monkeypatch.setattr(api_module, "get_class_link_overrides", overrides)
        app = FastAPI()
        app.router.route_class = outage_cache.OutageCacheRoute

        @app.get("/meinunterricht")
        async def overview():
            return await api_module.meinunterricht_overview(auth)

        headers = {"X-Session-Token": "test"}
        response = await asgi_request(app, "GET", "/meinunterricht", headers)
        assert response.json()["course_count"] == 1
        key = (auth.user_id, "/meinunterricht", ())
        previous = store.entries[key]

        # Simulate expiry of the ten-minute response cache, leaving the last
        # successful outage snapshot available as in production.
        manager._cache.clear()
        state.html = "<html><h1>Temporarily unavailable</h1></html>"
        response = await asgi_request(app, "GET", "/meinunterricht", headers)
        assert response.json()["success"] is False
        assert await manager.get_cached(auth.user_id, "/meinunterricht") is None
        assert store.entries[key] is previous
        assert "x-lanis-cache" not in response.headers

        state.html = '<a href="meinunterricht.php?a=sus_view&amp;id=42">Mathe</a>'
        response = await asgi_request(app, "GET", "/meinunterricht", headers)
        assert response.json()["course_count"] == 1
        assert response.headers["x-lanis-cache"] == "fresh"
        assert (await manager.get_cached(auth.user_id, "/meinunterricht"))["course_count"] == 1

    asyncio.run(scenario())
