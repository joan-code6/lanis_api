import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import httpx
import pytest
from fastapi import HTTPException

from api import api as api_module
from api import auth_db, impersonation
from api.admin import AdminPrincipal, _issue_token
from api.impersonation_broker import app as broker


async def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
    monkeypatch.setenv("LANIS_IMPERSONATION_SOCKET", "/private/broker.sock")
    monkeypatch.setenv("LANIS_ADMIN_ACCOUNTS", "123:admin")
    monkeypatch.setenv("LANIS_ADMIN_JWT_SECRET", "a" * 64)
    await auth_db.initialize()
    refresh = await auth_db.store_refresh_token(
        "123:student", "123", "student", "test-only"
    )
    parent = await auth_db.get_refresh_token(refresh)
    actor_session = await auth_db.create_admin_session(
        "123:admin",
        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30),
    )
    return parent, actor_session


def test_launch_replay_revocation_and_parent_isolation(tmp_path, monkeypatch):
    async def scenario():
        parent, actor = await setup(tmp_path, monkeypatch)
        launch = await impersonation.create("123:admin", "123:student", actor)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app), base_url="http://test"
        ) as client:
            first, replay = await asyncio.gather(
                *(
                    client.post(
                        "/impersonation/exchange", json={"code": launch["code"]}
                    )
                    for _ in range(2)
                )
            )
            assert sorted([first.status_code, replay.status_code]) == [200, 401]
            response = first if first.status_code == 200 else replay
            assert response.headers["cache-control"] == "no-store"
            data = response.json()
            assert "refresh_token" not in data
            token = data["access_token"]
            identity = await api_module.local_auth_dependency(token)
            assert identity.impersonation_id
            headers = {"X-Session-Token": token}
            # Writes are blocked even for routes that normally bypass lifecycle middleware.
            for method, path in [
                ("DELETE", "/account"),
                ("POST", "/logout"),
                ("PATCH", "/settings/preferences"),
                ("GET", "/nachrichten/42"),
                ("GET", "/account/export"),
                ("GET", "/meinunterricht/file/secret"),
                ("POST", "/notifications/subscription"),
                ("POST", "/mcp"),
                ("GET", "/agents"),
                ("POST", "/agents/authorization/test"),
                ("GET", "/dashboard/notifications"),
                ("GET", "/wahlen/42"),
            ]:
                result = await client.request(method, path, headers=headers, json={})
                assert result.status_code == 403, (method, path, result.text)
            assert (
                await client.get("/settings/preferences", headers=headers)
            ).status_code == 200
            assert (
                await client.post("/impersonation/end", headers=headers)
            ).status_code == 200
            assert (
                await client.get("/settings/preferences", headers=headers)
            ).status_code == 401
            # Ending the preview never revokes the student's real session.
            assert await auth_db.get_refresh_token_by_session_id(parent["session_id"])
            normal = api_module.sessions.create_access_token(
                "123:student", "123", "student", parent["session_id"]
            )
            assert (
                await api_module.local_auth_dependency(normal)
            ).impersonation_id == ""

    asyncio.run(scenario())


def test_admin_and_student_revocation_and_expiry(tmp_path, monkeypatch):
    async def scenario():
        _parent, actor = await setup(tmp_path, monkeypatch)
        launch = await impersonation.create("123:admin", "123:student", actor)
        grant = await impersonation.consume(launch["code"])
        await impersonation.validate(grant["id"], grant["target"], grant["parent_id"])
        async with aiosqlite.connect(auth_db.DB_PATH) as db:
            await db.execute(
                "DELETE FROM admin_sessions WHERE session_id = ?", (actor,)
            )
            await db.commit()
        with pytest.raises(HTTPException) as error:
            await impersonation.validate(
                grant["id"], grant["target"], grant["parent_id"]
            )
        assert error.value.status_code == 401
        # A removed allowlist entry revokes previews immediately too.
        monkeypatch.setenv("LANIS_ADMIN_ACCOUNTS", "")
        with pytest.raises(HTTPException):
            await impersonation.validate(
                grant["id"], grant["target"], grant["parent_id"]
            )
        monkeypatch.setenv("LANIS_ADMIN_ACCOUNTS", "123:admin")
        actor = await auth_db.create_admin_session(
            "123:admin",
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30),
        )
        launch = await impersonation.create("123:admin", "123:student", actor)
        await auth_db.delete_user_tokens("123:student")
        with pytest.raises(HTTPException):
            await api_module.exchange_impersonation(
                api_module.ImpersonationExchangeRequest(code=launch["code"]),
                api_module.Response(),
            )
        # Expired links cannot be exchanged.
        await auth_db.store_refresh_token("123:student", "123", "student", "test-only")
        launch = await impersonation.create("123:admin", "123:student", actor)
        monkeypatch.setattr(
            impersonation.time, "time", lambda: grant["expires_at"] + 3600
        )
        with pytest.raises(HTTPException):
            await impersonation.consume(launch["code"])

    asyncio.run(scenario())


def test_broker_requires_live_admin_and_audit(tmp_path, monkeypatch):
    async def scenario():
        _, actor = await setup(tmp_path, monkeypatch)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=broker), base_url="http://broker"
        ) as client:
            assert (
                await client.post("/launch", json={"user_id": "123:student"})
            ).status_code == 422
            assert (
                await client.post(
                    "/launch",
                    json={"user_id": "123:student"},
                    headers={"X-Admin-Token": "invalid"},
                )
            ).status_code == 401
            from api.impersonation_broker import user_metrics_db

            actions = []

            async def audit(*args):
                actions.append(args)

            monkeypatch.setattr(user_metrics_db, "record_admin_action", audit)
            token = _issue_token(AdminPrincipal("123:admin", "123", "admin", actor))
            response = await client.post(
                "/launch",
                json={"user_id": "123:student"},
                headers={"X-Admin-Token": token},
            )
            assert response.status_code == 200
            assert actions == [("123:admin", "impersonation_start", "123:student")]
            monkeypatch.delenv("LANIS_IMPERSONATION_SOCKET")
            assert (
                await client.post(
                    "/launch",
                    json={"user_id": "123:student"},
                    headers={"X-Admin-Token": token},
                )
            ).status_code == 503

    asyncio.run(scenario())


def test_route_policy_fails_closed():
    assert impersonation.permitted("GET", "/meinunterricht/course/1")
    for path in [
        "/future-route",
        "/videokonferenz",
        "/nachrichten/1",
        "/dateispeicher/file/1",
    ]:
        assert not impersonation.permitted("GET", path)
    assert not impersonation.permitted("POST", "/stundenplan")


@pytest.mark.parametrize(
    "path",
    [
        "/meinunterricht/entry?url=index.php%3Flogout%3Dall",
        "/meinunterricht/entry?url=https%3A%2F%2Fexample.invalid%2Fcallback",
        "/apps",
        "/app/External",
        "/app/External/anything",
    ],
)
def test_preview_blocks_arbitrary_fetches_and_raw_launch_links(
    tmp_path, monkeypatch, path
):
    async def scenario():
        _, actor = await setup(tmp_path, monkeypatch)
        launch = await impersonation.create("123:admin", "123:student", actor)

        async def unexpected_client(_user_id):
            raise AssertionError("Blocked preview must never obtain an upstream client")

        monkeypatch.setattr(
            api_module.sessions, "_get_or_create_schulportal_client", unexpected_client
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app), base_url="http://test"
        ) as client:
            exchanged = await client.post(
                "/impersonation/exchange", json={"code": launch["code"]}
            )
            response = await client.get(
                path, headers={"X-Session-Token": exchanged.json()["access_token"]}
            )
            assert response.status_code == 403

    asyncio.run(scenario())


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize(
    "native_link",
    [
        "meinunterricht.php",
        "/meinunterricht.php",
        "https://start.schulportal.hessen.de/meinunterricht.php",
    ],
)
def test_preview_modules_never_resolve_links_or_read_normal_caches(
    monkeypatch, cached, native_link
):
    from schulportal_hessen.base import SchulportalHessenAPI

    marker = "unit-test-callback"
    app_list = {
        "success": True,
        "data": {
            "entrys": [
                {
                    "Name": "Mein Unterricht",
                    "link": native_link + "?sid=" + marker,
                    "Farbe": "123456",
                    "Logo": "fa-book",
                    "Ordner": ["Schule"],
                },
                {
                    "Name": "External",
                    "link": "https://example.invalid/callback?code=" + marker,
                },
            ]
        },
    }
    client = SchulportalHessenAPI.__new__(SchulportalHessenAPI)
    client.get_apps = lambda: app_list

    def unexpected_resolution(*_args, **_kwargs):
        raise AssertionError("Preview must never resolve or probe upstream login links")

    client._resolve_direct_url = unexpected_resolution
    from types import SimpleNamespace

    client.session = SimpleNamespace(get=unexpected_resolution)
    original_cache = {
        "success": True,
        "modules": [{"direct_url": "https://example.invalid/?code=" + marker}],
    }

    class Cache:
        async def get_cached_with_revalidate(self, *_args):
            if cached:
                return original_cache, True
            return None, False

        async def set_cache(self, *_args, **_kwargs):
            raise AssertionError("Preview must not overwrite normal-user cache")

    monkeypatch.setattr(api_module, "sessions", Cache())

    async def unexpected_refresh(*_args):
        raise AssertionError("Preview must not schedule normal module refresh")

    monkeypatch.setattr(
        api_module, "_schedule_account_revalidation", unexpected_refresh
    )
    auth = api_module.AuthSession(
        client, "123:student", "123", "student", "parent", "preview"
    )
    result = asyncio.run(api_module.get_modules(auth=auth))
    assert result["success"]
    assert marker not in str(result)
    native, external = result["modules"]
    assert native["name"] == "Mein Unterricht"
    assert native["url"].endswith("/meinunterricht.php")
    assert native["folders"] == ["Schule"]
    assert external["name"] == "External"
    assert external["url"] == external["direct_url"] == ""
    assert not external["proxy_app"]
    assert marker in str(original_cache), "Normal account data must remain unchanged"


def test_preview_bypasses_outage_snapshots_without_replacing_normal_data(monkeypatch):
    import json
    from types import SimpleNamespace

    from fastapi import FastAPI

    from api import outage_cache
    from schulportal_hessen.tools.transport import observation

    store = outage_cache.SnapshotStore()
    monkeypatch.setattr(outage_cache, "snapshots", store)
    state = SimpleNamespace(preview=True, down=True)

    async def identity(_token):
        return api_module.AuthSession(
            None,
            "123:student",
            "123",
            "student",
            "parent",
            "preview" if state.preview else "",
        )

    monkeypatch.setattr(api_module, "local_auth_dependency", identity)
    app = FastAPI()
    app.router.route_class = outage_cache.OutageCacheRoute

    @app.get("/modules")
    async def modules():
        current = observation.get()
        if current is not None:
            current.unavailable = state.down
        return {"success": not state.down, "modules": []}

    key = ("123:student", "/modules", ())
    version = store.version("123:student", "/modules")
    cached = json.dumps(
        {
            "success": True,
            "modules": [
                {"direct_url": "https://example.invalid/?code=unit-test-callback"}
            ],
        }
    ).encode()
    store.put(key, cached, datetime.now(timezone.utc).replace(tzinfo=None), version)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/modules", headers={"X-Session-Token": "preview"}
            )
            assert response.json() == {"success": False, "modules": []}
            assert response.headers["cache-control"] == "private, no-store"
            state.down = False
            response = await client.get(
                "/modules", headers={"X-Session-Token": "preview"}
            )
            assert response.json()["success"]
            assert store.get(key, version).body == cached
            state.preview = False
            state.down = True
            normal = await client.get("/modules", headers={"X-Session-Token": "normal"})
            assert normal.json() == json.loads(cached)
            assert normal.headers["x-lanis-cache"] == "stale"

    asyncio.run(scenario())


def test_normal_module_discovery_still_resolves_external_login_links():
    from types import SimpleNamespace

    from schulportal_hessen.base import SchulportalHessenAPI

    client = SchulportalHessenAPI.__new__(SchulportalHessenAPI)
    resolved = []

    def resolve(url):
        resolved.append(url)
        return "https://example.invalid/callback?code=normal-unit-test"

    client._resolve_direct_url = resolve
    client.session = SimpleNamespace(
        get=lambda *_args, **_kwargs: SimpleNamespace(headers={}, close=lambda: None)
    )
    result = client.get_available_modules(
        {
            "success": True,
            "data": {
                "entrys": [
                    {"Name": "External", "link": "https://example.invalid/start"}
                ]
            },
        }
    )
    assert resolved == ["https://example.invalid/start"]
    assert result[0]["direct_url"].endswith("code=normal-unit-test")


def test_normal_entry_reads_keep_the_existing_linked_page_contract(monkeypatch):
    from types import SimpleNamespace

    calls = []

    class Cache:
        async def get_cached(self, *_args):
            return None

        async def set_cache(self, *_args):
            pass

    monkeypatch.setattr(api_module, "sessions", Cache())

    def fetch(url):
        calls.append(url)
        return {"success": True, "content_type": "html", "content": "Linked page"}

    auth = api_module.AuthSession(
        SimpleNamespace(meinunterricht_get_entry_details=fetch),
        "123:student",
        "123",
        "student",
    )
    result = asyncio.run(
        api_module.meinunterricht_entry(
            url="https://example.invalid/lesson?view=details", auth=auth
        )
    )
    assert result["content"] == "Linked page"
    assert calls == ["https://example.invalid/lesson?view=details"]
