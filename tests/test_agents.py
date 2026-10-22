"""OAuth and MCP integration tests with real persistence, no school credentials/network."""

import asyncio
import base64
import hashlib
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException, Request

from api import agent_store as store
from api import agents, auth_db
from api import api as api_module
from api.agent_mcp import mcp


@pytest.fixture
def environment(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("LANIS_AGENT_ISSUER", "http://localhost:8000")
    monkeypatch.setenv("LANIS_AGENT_UI_ORIGIN", "http://localhost:5173")

    async def owner(request: Request):
        user = request.headers.get("X-Session-Token")
        if not user:
            raise HTTPException(401)
        return api_module.AuthSession(
            client=None, user_id=user, school_id="5201", username=user
        )

    api_module.app.dependency_overrides[agents.owner] = owner
    asyncio.run(auth_db.initialize())
    asyncio.run(
        auth_db.store_refresh_token("5201:alice", "5201", "alice", "fake-test-only")
    )
    asyncio.run(
        auth_db.store_refresh_token("5201:bob", "5201", "bob", "fake-test-only")
    )
    yield
    api_module.app.dependency_overrides.clear()


@asynccontextmanager
async def client():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_module.app),
        base_url="http://localhost:8000",
    ) as c:
        yield c


HEAD = {"X-Session-Token": "5201:alice"}
BOB = {"X-Session-Token": "5201:bob"}
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .decode()
    .rstrip("=")
)
CALLBACK = "https://agent.example/callback?existing=1"


async def request_grant(c, scopes="schoolwork:read timetable:read", approve=True):
    registration = await c.post(
        "/oauth/register",
        json={"client_name": "Study agent", "redirect_uris": [CALLBACK]},
    )
    assert registration.status_code == 201, registration.text
    cid = registration.json()["client_id"]
    response = await c.get(
        "/oauth/authorize",
        params={
            "client_id": cid,
            "redirect_uri": CALLBACK,
            "code_challenge": CHALLENGE,
            "state": "agent-csrf-state",
            "scope": scopes,
            "resource": "http://localhost:8000/mcp",
        },
    )
    assert response.status_code == 303, response.text
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["request"][0]
    details = await c.get("/agents/authorization/" + request_id, headers=HEAD)
    assert details.json()["verified"] is False
    response = await c.post(
        "/agents/authorization/" + request_id,
        headers=HEAD,
        json={"approve": approve, "scopes": scopes.split(), "name": "My study agent"},
    )
    assert response.status_code == 200, response.text
    query = parse_qs(urlsplit(response.json()["redirect_url"]).query)
    assert query["state"] == ["agent-csrf-state"]
    assert query["existing"] == ["1"]
    if approve:
        return cid, query["code"][0], request_id
    return cid, query["error"][0], request_id


async def exchange(c, cid, code, **extra):
    return await c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": cid,
            "redirect_uri": CALLBACK,
            "code": code,
            "code_verifier": VERIFIER,
            **extra,
        },
    )


def test_oauth_pkce_permissions_revocation_and_account_isolation(environment):
    async def scenario():
        async with client() as c:
            cid, code, request_id = await request_grant(c)
            assert (
                await c.get("/agents/authorization/" + request_id, headers=HEAD)
            ).status_code == 410
            assert (
                await exchange(c, cid, code, code_verifier="x" * 64)
            ).status_code == 400
            assert (
                await exchange(c, cid, code, redirect_uri="https://evil.example")
            ).status_code == 400
            result = await exchange(c, cid, code)
            assert result.status_code == 200, result.text
            assert result.headers["cache-control"] == "no-store"
            token = result.json()["access_token"]
            assert (await agents.authenticate(token))["name"] == "My study agent"
            overview = (await c.get("/agents", headers=HEAD)).json()
            grant = overview["connections"][0]["id"]
            assert (await c.get("/agents", headers=BOB)).json()["connections"] == []
            assert (
                await c.patch(
                    "/agents/" + grant,
                    headers=BOB,
                    json={"name": "stolen", "scopes": ["schoolwork:read"]},
                )
            ).status_code == 404
            await c.delete("/agents/" + grant, headers=BOB)
            assert await agents.authenticate(token)
            assert (
                await c.patch(
                    "/agents/" + grant,
                    headers=HEAD,
                    json={"name": "Renamed", "scopes": ["messages:read"]},
                )
            ).status_code == 400
            assert (
                await c.patch(
                    "/agents/" + grant,
                    headers=HEAD,
                    json={"name": "Renamed", "scopes": ["schoolwork:read"]},
                )
            ).status_code == 200
            assert (await agents.authenticate(token))["scopes"] == ["schoolwork:read"]
            await c.delete("/agents/" + grant, headers=HEAD)
            assert await agents.authenticate(token) is None
            async with store.database() as db:
                rows = await store.one(
                    db,
                    "SELECT COUNT(*) AS count FROM agent_tokens WHERE id=?",
                    (token,),
                )
                assert rows["count"] == 0

    asyncio.run(scenario())


def test_refresh_rotation_replay_and_independent_agents(environment):
    async def scenario():
        async with client() as c:
            cid, code, _ = await request_grant(c)
            tokens = (await exchange(c, cid, code)).json()
            cid2, code2, _ = await request_grant(c)
            other = (await exchange(c, cid2, code2)).json()
            refresh = {
                "grant_type": "refresh_token",
                "client_id": cid,
                "refresh_token": tokens["refresh_token"],
            }
            response = await c.post("/oauth/token", data=refresh)
            assert response.status_code == 200
            rotated = response.json()
            assert tokens["refresh_token"] != rotated["refresh_token"]
            assert (await c.post("/oauth/token", data=refresh)).json()[
                "error"
            ] == "invalid_grant"
            assert await agents.authenticate(rotated["access_token"]) is None
            assert await agents.authenticate(other["access_token"])

    asyncio.run(scenario())


def test_discovery_validation_and_denial(environment):
    async def scenario():
        async with client() as c:
            metadata = (await c.get("/.well-known/oauth-authorization-server")).json()
            assert metadata["code_challenge_methods_supported"] == ["S256"]
            resource = (await c.get("/.well-known/oauth-protected-resource/mcp")).json()
            assert resource["resource"].endswith("/mcp")
            assert (await c.get("/agents")).status_code == 401
            for uri in [
                "javascript:alert(1)",
                "http://remote.example/callback",
                "https://agent.example/cb#fragment",
                "https://user:password@agent.example/cb",
            ]:
                assert (
                    await c.post("/oauth/register", json={"redirect_uris": [uri]})
                ).status_code == 400
            cid, error, rid = await request_grant(c, approve=False)
            assert error == "access_denied"
            assert (await c.get("/agents", headers=HEAD)).json()["connections"] == []
            assert (
                await c.get("/agents/authorization/" + rid, headers=HEAD)
            ).status_code == 410
            params = {
                "client_id": cid,
                "redirect_uri": CALLBACK,
                "state": "state",
                "code_challenge": CHALLENGE,
            }
            for updates in [
                {"redirect_uri": "https://evil.example"},
                {"code_challenge_method": "plain"},
                {"scope": "admin"},
                {"resource": "https://evil.example/mcp"},
            ]:
                assert (
                    await c.get("/oauth/authorize", params={**params, **updates})
                ).status_code == 400

    asyncio.run(scenario())


def test_expiry_code_replay_and_account_cleanup(environment):
    async def scenario():
        async with client() as c:
            cid, code, _ = await request_grant(c)
            tokens = (await exchange(c, cid, code)).json()
            assert (await exchange(c, cid, code)).status_code == 400
            assert await agents.authenticate(tokens["access_token"]) is None
            cid, code, _ = await request_grant(c)
            async with store.database(True) as db:
                await db.execute(
                    "UPDATE agent_tokens SET expires=? WHERE id=?",
                    (time.time() - 1, store.digest(code)),
                )
            assert (await exchange(c, cid, code)).status_code == 400
            cid, code, _ = await request_grant(c)
            tokens = (await exchange(c, cid, code)).json()
            await auth_db.delete_user_data("5201:alice")
            assert await agents.authenticate(tokens["access_token"]) is None
            assert await store.connections("5201:alice") == []

    asyncio.run(scenario())


def test_real_mcp_transport_scoped_discovery_and_revocation(environment):
    async def scenario():
        async with mcp.session_manager.run(), client() as c:
            unauth = await c.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            assert unauth.status_code == 401
            assert "resource_metadata=" in unauth.headers["www-authenticate"]
            cid, code, _ = await request_grant(c, scopes="schoolwork:read")
            tokens = (await exchange(c, cid, code)).json()
            headers = {
                "Authorization": "Bearer " + tokens["access_token"],
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
            }
            init = await c.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test-agent", "version": "1"},
                    },
                },
            )
            assert init.status_code == 200, init.text
            assert "LANIS" in init.text
            response = await c.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            assert response.status_code == 200, response.text
            names = {t["name"] for t in response.json()["result"]["tools"]}
            assert names == {"lanis_connection", "lanis_courses", "lanis_course"}
            response = await c.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "lanis_connection", "arguments": {}},
                },
            )
            assert response.status_code == 200, response.text
            assert "Europe/Berlin" in response.text
            denied = await c.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "lanis_messages", "arguments": {}},
                },
            )
            assert denied.json()["result"]["isError"] is True
            assert "PERMISSION_REQUIRED" in denied.text
            grant = (await c.get("/agents", headers=HEAD)).json()["connections"][0][
                "id"
            ]
            await c.delete("/agents/" + grant, headers=HEAD)
            assert (
                await c.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
                )
            ).status_code == 401

    asyncio.run(scenario())


def test_schoolwork_projection_excludes_grades_and_attendance():
    from api.agent_mcp import course_payload

    original = {
        "success": True,
        "course_id": "42",
        "marks": ["private grade"],
        "attendance_summary": {"absent": 4},
        "entries": [
            {
                "entry_id": str(i),
                "date": "2026-10-22",
                "homework": "Read chapter 2",
                "attendance": "private",
            }
            for i in range(3)
        ],
    }
    first = course_payload(original, 0, 2)
    assert "marks" not in first and "attendance_summary" not in first
    assert first["next_offset"] == 2
    assert len(first["entries"]) == 2
    assert all("attendance" not in entry for entry in first["entries"])
    last = course_payload(original, first["next_offset"], 2)
    assert last["next_offset"] is None
    assert last["entries"][0]["entry_id"] == "2"
    assert (
        original["entries"][0]["attendance"] == "private"
    )  # Shared UI cache stays intact.


def test_rate_limit_and_authorization_request_expiry(environment):
    async def scenario():
        async with client() as c:
            registration = (
                await c.post("/oauth/register", json={"redirect_uris": [CALLBACK]})
            ).json()
            response = await c.get(
                "/oauth/authorize",
                params={
                    "client_id": registration["client_id"],
                    "redirect_uri": CALLBACK,
                    "state": "state",
                    "code_challenge": CHALLENGE,
                },
            )
            rid = parse_qs(urlsplit(response.headers["location"]).query)["request"][0]
            async with store.database(True) as db:
                await db.execute("UPDATE agent_requests SET expires=0")
            assert (
                await c.get("/agents/authorization/" + rid, headers=HEAD)
            ).status_code == 410
            assert (
                await c.post(
                    "/agents/authorization/" + rid,
                    headers=HEAD,
                    json={"approve": True, "scopes": ["schoolwork:read"]},
                )
            ).status_code == 410
            assert await store.rate_limit("test-limiter", 1, 60)
            assert not await store.rate_limit("test-limiter", 1, 60)

    asyncio.run(scenario())


def test_school_tool_holds_account_lifecycle_lease(environment, monkeypatch):
    """Account deletion cannot race a tool into recreating school caches."""
    from types import SimpleNamespace

    from api import agent_mcp
    from api.account_data import account_lifecycle_lock

    async def scenario():
        entered, release, deleting = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def identity(_scope=None):
            return {
                "id": "test-grant",
                "user_id": "5201:alice",
                "scopes": ["schoolwork:read"],
            }

        async def session(_user_id):
            return SimpleNamespace(client=None)

        async def read_school(auth):
            assert auth.session_id
            entered.set()
            await release.wait()
            return {"success": True}

        async def delete():
            lock = await account_lifecycle_lock("5201:alice")
            async with lock:
                deleting.set()

        monkeypatch.setattr(agent_mcp, "identity", identity)
        monkeypatch.setattr(
            api_module.sessions, "_get_or_create_schulportal_client", session
        )
        task = asyncio.create_task(agent_mcp.run("schoolwork:read", read_school))
        await asyncio.wait_for(entered.wait(), 3)
        deletion = asyncio.create_task(delete())
        await asyncio.sleep(0)
        assert not deleting.is_set()
        release.set()
        assert (await task)["success"] is True
        await deletion
        assert deleting.is_set()

    asyncio.run(scenario())
