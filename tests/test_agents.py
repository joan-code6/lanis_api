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
    for name in ("DSB_SCHOOL_ID", "DSB_USERNAME", "DSB_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANIS_AGENT_ISSUER", "http://localhost:8000")
    monkeypatch.setenv("LANIS_AGENT_UI_ORIGIN", "http://localhost:5173")

    async def owner(request: Request):
        user = request.headers.get("X-Session-Token")
        if not user:
            raise HTTPException(401)
        return api_module.AuthSession(
            client=None, user_id=user, school_id="5201", username=user
        )

    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import agent_mcp
    from api.agent_mcp import TOOL_MODULES

    monkeypatch.setattr(
        api_module.sessions,
        "_get_or_create_schulportal_client",
        AsyncMock(return_value=SimpleNamespace(client=None)),
    )
    monkeypatch.setattr(
        agent_mcp,
        "fetch_module_access",
        lambda _: {
            "success": True,
            "modules": [{"usage": sorted({m for m in TOOL_MODULES.values() if m})}],
        },
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
                    (store.digest(token),),
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
            descriptors = response.json()["result"]["tools"]
            assert all(t["outputSchema"]["type"] == "object" for t in descriptors)
            names = {t["name"] for t in descriptors}
            assert names == {
                "lanis_connection",
                "lanis_courses",
                "lanis_course",
                "lanis_study_groups",
                "lanis_exams",
            }
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
            assert isinstance(response.json()["result"]["structuredContent"], dict)
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


@pytest.mark.parametrize(
    "failure, expected_error",
    [
        ("validation", "INVALID_REQUEST"),
        (400, "INVALID_REQUEST"),
        (404, "INVALID_REQUEST"),
        (409, "INVALID_REQUEST"),
        (422, "INVALID_REQUEST"),
        (429, "INVALID_REQUEST"),
        (499, "INVALID_REQUEST"),
        (401, "SCHOOL_SESSION_EXPIRED"),
        (403, "SCHOOL_SESSION_EXPIRED"),
        (500, "SCHOOL_UNAVAILABLE"),
        (503, "SCHOOL_UNAVAILABLE"),
        ("unexpected", "SCHOOL_UNAVAILABLE"),
    ],
)
@pytest.mark.parametrize("write", [False, True])
def test_school_tool_error_recovery(
    environment, monkeypatch, failure, expected_error, write
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(return_value={"id": "test-grant", "user_id": "5201:alice"}),
        )
        monkeypatch.setattr(
            api_module.sessions,
            "_get_or_create_schulportal_client",
            AsyncMock(return_value=SimpleNamespace(client=None)),
        )
        details = {"field": "period", "message": "Invalid lesson period"}

        async def fail(_auth):
            if failure == "validation":
                api_module.CustomLessonRequest(
                    date="2026-10-22", period="1", duration=0
                )
            elif isinstance(failure, int):
                raise HTTPException(failure, detail=details)
            else:
                raise RuntimeError("private session internals")

        result = await agent_mcp.run("timetable:write", fail, write=write)
        assert result["success"] is False
        if write and expected_error == "SCHOOL_UNAVAILABLE":
            assert result["error"] == "OUTCOME_UNKNOWN"
            assert "Verify the existing result in LANIS first" in result["next_step"]
            assert "A further write requires a confirmed need" in result["next_step"]
            assert "details" not in result
            return
        assert result["error"] == expected_error
        if expected_error == "INVALID_REQUEST":
            assert result["next_step"] == (
                "Correct the request using these details before submitting it again."
            )
            if failure == "validation":
                assert result["details"] == [
                    {
                        "type": "greater_than_equal",
                        "loc": ("duration",),
                        "msg": "Input should be greater than or equal to 1",
                    }
                ]
            else:
                assert result["details"] == details
        else:
            assert "details" not in result
            if expected_error == "SCHOOL_SESSION_EXPIRED":
                assert result["next_step"] == (
                    "Ask the user to sign in to LANIS again, then retry."
                )
            elif failure == "unexpected":
                assert (
                    result["next_step"]
                    == "Explain that school data is temporarily unavailable and try reading it later."
                )
            else:
                assert result["next_step"] == (
                    "Explain that school data is temporarily unavailable and try reading it later."
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("approve", [True, False])
def test_real_browser_consent_does_not_upgrade_middleware_lock(environment, approve):
    async def scenario():
        api_module.app.dependency_overrides.pop(agents.owner)
        credentials = await auth_db.get_refresh_token_by_user_id("5201:alice")
        jwt = api_module.sessions.create_access_token(
            "5201:alice", "5201", "alice", credentials["session_id"]
        )
        async with client() as c:
            cid = (
                await c.post("/oauth/register", json={"redirect_uris": [CALLBACK]})
            ).json()["client_id"]
            response = await c.get(
                "/oauth/authorize",
                params={
                    "client_id": cid,
                    "redirect_uri": CALLBACK,
                    "state": "signed-browser-state",
                    "code_challenge": CHALLENGE,
                },
            )
            rid = parse_qs(urlsplit(response.headers["location"]).query)["request"][0]
            response = await asyncio.wait_for(
                c.post(
                    "/agents/authorization/" + rid,
                    headers={"X-Session-Token": jwt},
                    json={"approve": approve, "mode": "restricted"},
                ),
                2,
            )
            assert response.status_code == 200, response.text
            query = parse_qs(urlsplit(response.json()["redirect_url"]).query)
            assert ("code" if approve else "error") in query

    asyncio.run(scenario())


def test_token_exchange_and_rotation_with_unrelated_expired_browser_session(
    environment,
):
    async def scenario():
        async with client() as c:
            cid, code, _ = await request_grant(c)
            async with store.database(True) as db:
                await db.execute(
                    "UPDATE refresh_tokens SET expires_at='2000-01-01 00:00:00' WHERE user_id='5201:bob'"
                )
            result = await asyncio.wait_for(exchange(c, cid, code), 2)
            assert result.status_code == 200, result.text
            rotated = await asyncio.wait_for(
                c.post(
                    "/oauth/token",
                    data={
                        "client_id": cid,
                        "grant_type": "refresh_token",
                        "refresh_token": result.json()["refresh_token"],
                    },
                ),
                2,
            )
            assert rotated.status_code == 200, rotated.text

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["restricted", "full", "custom"])
def test_presets_are_enforced_on_server_and_custom_is_exact(environment, mode):
    async def scenario():
        async with client() as c:
            cid = (
                await c.post("/oauth/register", json={"redirect_uris": [CALLBACK]})
            ).json()["client_id"]
            response = await c.get(
                "/oauth/authorize",
                params={
                    "client_id": cid,
                    "redirect_uri": CALLBACK,
                    "state": "state",
                    "code_challenge": CHALLENGE,
                },
            )
            rid = parse_qs(urlsplit(response.headers["location"]).query)["request"][0]
            details = (await c.get("/agents/authorization/" + rid, headers=HEAD)).json()
            assert set(details["presets"]["full"]) == set(agents.SCOPES)
            assert not any(
                s in details["presets"]["restricted"]
                for s in [
                    "grades:read",
                    "profile:personal:read",
                    "meetings:read",
                    "attendance:read",
                    "messages:mark-read",
                    "messages:write",
                    "schoolwork:write",
                ]
            )
            selected = ["grades:read", "schoolwork:write"]
            result = await c.post(
                "/agents/authorization/" + rid,
                headers=HEAD,
                json={"approve": True, "mode": mode, "scopes": selected},
            )
            assert result.status_code == 200, result.text
            grant = (await c.get("/agents", headers=HEAD)).json()["connections"][0]
            expected = (
                agents.RESTRICTED_SCOPES
                if mode == "restricted"
                else list(agents.SCOPES)
                if mode == "full"
                else selected
            )
            assert set(grant["scopes"]) == set(expected)
            assert set(grant["requested_scopes"]) == set(agents.SCOPES)
            # Changes stay inside the original client's requested capability ceiling.
            change = await c.patch(
                "/agents/" + grant["id"],
                headers=HEAD,
                json={"name": "Updated", "mode": "custom", "scopes": ["calendar:read"]},
            )
            assert change.status_code == 200

    asyncio.run(scenario())


def test_full_preset_cannot_expand_client_requested_scope(environment):
    async def scenario():
        async with client() as c:
            cid = (
                await c.post("/oauth/register", json={"redirect_uris": [CALLBACK]})
            ).json()["client_id"]
            response = await c.get(
                "/oauth/authorize",
                params={
                    "client_id": cid,
                    "redirect_uri": CALLBACK,
                    "state": "state",
                    "code_challenge": CHALLENGE,
                    "scope": "calendar:read",
                },
            )
            rid = parse_qs(urlsplit(response.headers["location"]).query)["request"][0]
            response = await c.post(
                "/agents/authorization/" + rid,
                headers=HEAD,
                json={"approve": True, "mode": "full"},
            )
            assert response.status_code == 400

    asyncio.run(scenario())


def test_restricted_message_read_never_fetches_or_marks_conversation(
    environment, monkeypatch
):
    from api import agent_mcp

    async def scenario():
        async def identity(scope=None):
            assert scope == "messages:read"
            return {"id": "test", "user_id": "5201:alice", "scopes": ["messages:read"]}

        async def cache(*_args):
            return None

        async def forbidden_fetch(*_args, **_kwargs):
            raise AssertionError("Read-only tool fetched a conversation")

        monkeypatch.setattr(agent_mcp, "identity", identity)
        monkeypatch.setattr(api_module.sessions, "get_cached", cache)
        monkeypatch.setattr(api_module, "get_conversation", forbidden_fetch)
        result = await agent_mcp.lanis_message("conversation-1")
        assert result["error"] == "MESSAGE_NOT_CACHED"

    asyncio.run(scenario())


def test_personal_profile_requires_fresh_sensitive_permission(environment, monkeypatch):
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        # Existing profile:read grants permit module discovery, not personal data.
        allowed = set(agents.RESTRICTED_SCOPES) | {"profile:read"}

        async def identity(scope=None):
            if scope and scope not in allowed:
                raise ValueError("PERMISSION_REQUIRED")
            return {"id": "test", "user_id": "5201:alice", "scopes": list(allowed)}

        monkeypatch.setattr(agent_mcp, "identity", identity)
        profile = AsyncMock(return_value={"success": True, "name": "Alice"})
        monkeypatch.setattr(api_module, "get_user_data", profile)
        names = {t.name for t in await agent_mcp.permitted_tools()}
        assert "lanis_modules" in names
        assert "lanis_profile" not in names
        tool = mcp._tool_manager.get_tool("lanis_profile")
        with pytest.raises(ValueError, match="PERMISSION_REQUIRED"):
            await tool.fn()
        profile.assert_not_called()

        allowed.add("profile:personal:read")
        assert "lanis_profile" in {t.name for t in await agent_mcp.permitted_tools()}
        assert (await tool.fn())["name"] == "Alice"
        profile.assert_awaited_once()

    assert agents.SCOPES["profile:personal:read"]["sensitive"]
    assert "profile:personal:read" not in agents.RESTRICTED_SCOPES
    asyncio.run(scenario())


def test_write_and_sensitive_tools_enforce_separate_permissions(
    environment, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        allowed = set(agents.RESTRICTED_SCOPES)

        async def identity(scope=None):
            if scope and scope not in allowed:
                raise ValueError("PERMISSION_REQUIRED")
            return {
                "id": "test-grant",
                "user_id": "5201:alice",
                "scopes": list(allowed),
            }

        async def session(_user):
            return SimpleNamespace(client=None)

        monkeypatch.setattr(agent_mcp, "identity", identity)
        monkeypatch.setattr(
            api_module.sessions, "_get_or_create_schulportal_client", session
        )
        writer = AsyncMock(return_value={"success": True})
        monkeypatch.setattr(api_module, "meinunterricht_homework_done", writer)
        tool = mcp._tool_manager.get_tool("lanis_set_homework")
        with pytest.raises(ValueError, match="PERMISSION_REQUIRED"):
            await tool.fn(course_id="42", entry_id="1", done=True)
        writer.assert_not_called()
        allowed.add("schoolwork:write")
        assert (await tool.fn(course_id="42", entry_id="1", done=True))[
            "success"
        ] is True
        writer.assert_awaited_once()
        assert writer.call_args.kwargs["auth"].user_id == "5201:alice"
        assert not tool.annotations.readOnlyHint
        assert (
            mcp._tool_manager.get_tool("lanis_open_message").annotations.readOnlyHint
            is False
        )
        assert (
            mcp._tool_manager.get_tool(
                "lanis_delete_submission_file"
            ).annotations.destructiveHint
            is True
        )
        with pytest.raises(ValueError, match="PERMISSION_REQUIRED"):
            await mcp._tool_manager.get_tool("lanis_grades").fn(course_id="42")
        allowed.add("grades:read")
        monkeypatch.setattr(
            api_module,
            "meinunterricht_course",
            AsyncMock(
                return_value={
                    "success": True,
                    "marks": ["A"],
                    "attendance_summary": {"absent": 2},
                    "entries": [],
                }
            ),
        )
        result = await mcp._tool_manager.get_tool("lanis_grades").fn(course_id="42")
        assert result == {"success": True, "course_id": "42", "marks": ["A"]}

    asyncio.run(scenario())


def test_file_download_is_bounded_and_closes_stream():
    from types import SimpleNamespace

    from api.agent_tools import download_file

    closed = []

    def oversized():
        try:
            yield b"x" * (2 * 1024 * 1024)
            yield b"x"
        finally:
            closed.append(True)

    client = SimpleNamespace(
        dateispeicher_download_file=lambda _id: {"success": True, "stream": oversized()}
    )
    assert download_file(client, 1)["error"] == "FILE_TOO_LARGE"
    assert closed == [True]
    client.dateispeicher_download_file = lambda _id: {
        "success": True,
        "content": b"homework",
        "content_type": "text/plain",
        "filename": "task.txt",
    }
    assert download_file(client, 1) == {
        "success": True,
        "filename": "task.txt",
        "mime_type": "text/plain",
        "text": "homework",
    }


def test_custom_backend_metadata_consent_and_refresh_are_issuer_bound(
    environment, monkeypatch
):
    monkeypatch.setenv("LANIS_AGENT_ISSUER", "https://school-api.example")
    monkeypatch.setenv("LANIS_AGENT_UI_ORIGIN", "https://school-ui.example")

    async def scenario():
        async with client() as c:
            metadata = (await c.get("/.well-known/oauth-authorization-server")).json()
            assert metadata["issuer"] == "https://school-api.example"
            assert (
                metadata["token_endpoint"] == "https://school-api.example/oauth/token"
            )
            resource = (await c.get("/.well-known/oauth-protected-resource/mcp")).json()
            assert resource["resource"] == "https://school-api.example/mcp"
            cid = (
                await c.post("/oauth/register", json={"redirect_uris": [CALLBACK]})
            ).json()["client_id"]
            response = await c.get(
                "/oauth/authorize",
                params={
                    "client_id": cid,
                    "redirect_uri": CALLBACK,
                    "state": "custom-state",
                    "code_challenge": CHALLENGE,
                    "resource": "https://school-api.example/mcp",
                },
            )
            target = urlsplit(response.headers["location"])
            assert target.scheme == "https" and target.netloc == "school-ui.example"
            query = parse_qs(target.query)
            assert query["issuer"] == ["https://school-api.example"]
            rid = query["request"][0]
            approval = await c.post(
                "/agents/authorization/" + rid,
                headers=HEAD,
                json={"approve": True, "mode": "restricted"},
            )
            code = parse_qs(urlsplit(approval.json()["redirect_url"]).query)["code"][0]
            wrong = await exchange(
                c, cid, code, resource="https://different-api.example/mcp"
            )
            assert (
                wrong.status_code == 400 and wrong.json()["error"] == "invalid_target"
            )
            tokens = await exchange(
                c, cid, code, resource="https://school-api.example/mcp"
            )
            assert tokens.status_code == 200, tokens.text
            overview = (await c.get("/agents", headers=HEAD)).json()
            assert overview["mcp_url"] == "https://school-api.example/mcp"
            assert len(overview["connections"]) == 1

    asyncio.run(scenario())


def test_custom_frontend_cors_and_mcp_discovery_use_operator_origins():
    import os
    import subprocess
    import sys

    code = """
from fastapi.testclient import TestClient
from api.api import app
from api.agent_mcp import mcp
client=TestClient(app,base_url="https://school-api.example")
response=client.options("/agents/authorization/request",headers={"Origin":"https://school-ui.example","Access-Control-Request-Method":"POST","Access-Control-Request-Headers":"X-Session-Token,Content-Type"})
assert response.status_code==200,response.text
assert response.headers["access-control-allow-origin"]=="https://school-ui.example"
assert str(mcp.settings.auth.issuer_url).rstrip("/")=="https://school-api.example"
assert str(mcp.settings.auth.resource_server_url)=="https://school-api.example/mcp"
response=client.post("/mcp",json={"jsonrpc":"2.0","id":1,"method":"tools/list"})
assert response.status_code==401,response.text
assert "https://school-api.example/.well-known/oauth-protected-resource/mcp" in response.headers["www-authenticate"]
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={
            **os.environ,
            "LANIS_AGENT_ISSUER": "https://school-api.example",
            "LANIS_AGENT_UI_ORIGIN": "https://school-ui.example",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_agent_lesson_patch_preserves_details_and_timetable_reads_overrides(
    environment, monkeypatch
):
    from types import SimpleNamespace

    from api import agent_mcp

    async def scenario():
        async def identity(scope=None):
            return {
                "id": "lesson-test",
                "user_id": "5201:alice",
                "scopes": ["timetable:read", "timetable:write"],
            }

        async def session(_user):
            return SimpleNamespace(
                client=SimpleNamespace(stundenplan_get_plan=lambda: {"success": True})
            )

        monkeypatch.setattr(agent_mcp, "identity", identity)
        monkeypatch.setattr(
            api_module.sessions, "_get_or_create_schulportal_client", session
        )
        await auth_db.save_custom_lesson(
            "5201:alice",
            {
                "date": "2026-10-19",
                "period": "1–2",
                "subject": "Math",
                "teacher": "Teacher",
                "room": "Old",
                "info": "Keep notes",
                "class_name": "Class",
                "start_time": "08:00",
                "end_time": "09:30",
                "duration": 2,
                "week_type": "A",
                "course_id": "42",
                "removed": False,
            },
        )
        edit = mcp._tool_manager.get_tool("lanis_update_lesson")
        result = await edit.fn(date="2026-10-26", period="1", room="New")
        lesson = result["lesson"]
        assert lesson["room"] == "New"
        assert lesson["period"] == "1–2"
        assert lesson["date"] == "2026-10-19"
        assert len(await auth_db.get_custom_lessons("5201:alice")) == 1
        for key, expected in {
            "subject": "Math",
            "info": "Keep notes",
            "duration": 2,
            "course_id": "42",
            "start_time": "08:00",
            "end_time": "09:30",
        }.items():
            assert lesson[key] == expected
        # Force overlapping edits; neither may restore the other's old field.
        await asyncio.gather(
            edit.fn(date="2026-10-19", period="1", room="Parallel room"),
            edit.fn(date="2026-10-19", period="1", teacher="Parallel teacher"),
        )
        saved = (await auth_db.get_custom_lessons("5201:alice"))[0]
        assert saved["room"] == "Parallel room"
        assert saved["teacher"] == "Parallel teacher"
        assert saved["info"] == "Keep notes"
        read = await agent_mcp.lanis_timetable()
        assert read["custom_lessons"][0]["room"] == "Parallel room"
        await edit.fn(date="2026-10-19", period="1", removed=True)
        assert (await agent_mcp.lanis_timetable())["custom_lessons"][0][
            "removed"
        ] is True

    asyncio.run(scenario())


def test_token_scope_ceiling_resubmission_never_expands_user_grant(environment):
    async def scenario():
        async with client() as c:
            cid, code, _ = await request_grant(c, agents.DEFAULT_SCOPES)
            grant = (await c.get("/agents", headers=HEAD)).json()["connections"][0]
            restricted = list(agents.RESTRICTED_SCOPES)
            changed = await c.patch(
                "/agents/" + grant["id"],
                headers=HEAD,
                json={"name": "Restricted", "scopes": restricted},
            )
            assert changed.status_code == 200
            response = await exchange(c, cid, code, scope=agents.DEFAULT_SCOPES)
            assert response.status_code == 200
            tokens = response.json()
            for requested in ["schoolwork:read", agents.DEFAULT_SCOPES]:
                assert set(tokens["scope"].split()) == set(restricted)
                assert set(
                    (await agents.authenticate(tokens["access_token"]))["scopes"]
                ) == set(restricted)
                response = await c.post(
                    "/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": cid,
                        "refresh_token": tokens["refresh_token"],
                        "scope": requested,
                    },
                )
                assert response.status_code == 200, response.text
                tokens = response.json()
            assert set(tokens["scope"].split()) == set(restricted)

    asyncio.run(scenario())


def test_token_rate_limits_isolate_clients_and_guard_missing_peer(environment):
    async def scenario():
        # No ASGI peer is valid for in-process transports and some deployments.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_module.app, client=None),
            base_url="http://localhost:8000",
        ) as c:
            registration = await c.post(
                "/oauth/register",
                json={"client_name": "No peer", "redirect_uris": [CALLBACK]},
            )
            assert registration.status_code == 201
            authorization = await c.get(
                "/oauth/authorize",
                params={
                    "client_id": registration.json()["client_id"],
                    "redirect_uri": CALLBACK,
                    "code_challenge": CHALLENGE,
                    "state": "no-peer-state",
                    "scope": "schoolwork:read",
                    "resource": "http://localhost:8000/mcp",
                },
            )
            assert authorization.status_code == 303, authorization.text

            for _ in range(120):
                assert await store.rate_limit(
                    "token-client:" + store.digest("busy-client"), 120, 60
                )
            request = {
                "grant_type": "refresh_token",
                "client_id": "busy-client",
                "refresh_token": "invalid",
            }
            assert (await c.post("/oauth/token", data=request)).status_code == 400
            request["client_id"] = "other-client"
            assert (await c.post("/oauth/token", data=request)).status_code == 400
            # The independent IP bucket still stops callers rotating client IDs.
            async with store.database(True) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO agent_rate_limits VALUES (?,?,?)",
                    (store.digest("token-ip:unknown"), 2000, time.time() + 60),
                )
            request["client_id"] = "third-client"
            assert (await c.post("/oauth/token", data=request)).status_code == 429

    asyncio.run(scenario())


def test_invalid_token_requests_cannot_charge_another_clients_limit(environment):
    async def scenario():
        async with client() as c:
            cid, code, _ = await request_grant(c)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=api_module.app, client=("198.51.100.7", 1234)
                ),
                base_url="http://localhost:8000",
            ) as attacker:
                for _ in range(121):
                    response = await attacker.post(
                        "/oauth/token",
                        data={
                            "grant_type": "refresh_token",
                            "client_id": cid,
                            "refresh_token": "not-a-token",
                        },
                    )
                    assert response.status_code == 400
                response = await exchange(attacker, cid, code, code_verifier="x" * 64)
                assert response.status_code == 400
            async with store.database() as db:
                assert (
                    await store.one(
                        db,
                        "SELECT count FROM agent_rate_limits WHERE id=?",
                        (store.digest("token-client:" + store.digest(cid)),),
                    )
                    is None
                )
            issued = await exchange(c, cid, code)
            assert issued.status_code == 200
            tokens = issued.json()
            refresh = await c.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": cid,
                    "refresh_token": tokens["refresh_token"],
                },
            )
            assert refresh.status_code == 200
            # Authenticated traffic is still bounded and rejection consumes no code.
            async with store.database(True) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO agent_rate_limits VALUES (?,?,?)",
                    (
                        store.digest("token-client:" + store.digest(cid)),
                        120,
                        time.time() + 60,
                    ),
                )
            retry = await c.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": cid,
                    "refresh_token": refresh.json()["refresh_token"],
                },
            )
            assert retry.status_code == 429

    asyncio.run(scenario())


def test_dotenv_loaded_before_mcp_construction(tmp_path):
    import os
    import subprocess
    import sys

    config = tmp_path / ".env"
    config.write_text(
        "LANIS_AGENT_ISSUER=https://dotenv-api.example\nLANIS_AGENT_UI_ORIGIN=https://dotenv-ui.example\n"
    )
    code = """
import os, sys
import dotenv
original = dotenv.load_dotenv
dotenv.load_dotenv = lambda *a, **kw: original(dotenv_path=sys.argv[1])
from api.api import app
from api.agent_mcp import mcp
from fastapi.testclient import TestClient
assert str(mcp.settings.auth.issuer_url).rstrip('/') == 'https://dotenv-api.example'
assert str(mcp.settings.auth.resource_server_url) == 'https://dotenv-api.example/mcp'
assert 'dotenv-api.example' in mcp.settings.transport_security.allowed_hosts
assert 'https://dotenv-ui.example' in mcp.settings.transport_security.allowed_origins
client = TestClient(app, base_url='https://dotenv-api.example')
metadata = client.get('/.well-known/oauth-authorization-server').json()
assert metadata['issuer'] == 'https://dotenv-api.example'
response = client.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
assert response.status_code == 401, response.text
assert 'https://dotenv-api.example/.well-known/oauth-protected-resource/mcp' in response.headers['www-authenticate']
"""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"LANIS_AGENT_ISSUER", "LANIS_AGENT_UI_ORIGIN"}
    }
    result = subprocess.run(
        [sys.executable, "-c", code, str(config)],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("badge", ["A", "Q1"])
@pytest.mark.parametrize("personal_plan", [True, False])
def test_first_agent_lesson_edit_preserves_portal_details(
    environment, monkeypatch, badge, personal_plan
):
    from types import SimpleNamespace

    from api import agent_mcp

    async def scenario():
        async def identity(scope=None):
            return {
                "id": "first-edit",
                "user_id": "5201:alice",
                "scopes": ["timetable:write"],
            }

        original = {
            "stunde": 1,
            "name": "Math",
            "teacher": "Teacher",
            "room": "Old",
            "duration": 2,
            "start_time": {"hour": 8, "minute": 0},
            "end_time": {"hour": 9, "minute": 30},
            "info": "Notes",
            "class_name": "9A",
            "course_id": "42",
            "badge": badge,
        }
        plan = {
            "success": True,
            "plan_for_own": [[original]] if personal_plan else None,
            "plan_for_all": [[original]],
        }

        async def session(_user):
            return SimpleNamespace(
                client=SimpleNamespace(stundenplan_get_plan=lambda: plan)
            )

        monkeypatch.setattr(agent_mcp, "identity", identity)
        monkeypatch.setattr(
            api_module.sessions, "_get_or_create_schulportal_client", session
        )
        edit = mcp._tool_manager.get_tool("lanis_update_lesson")
        result = await edit.fn(date="2026-10-19", period="1", room="New")
        assert result["success"] is True, result
        saved = result["lesson"]
        for key, expected in {
            "subject": "Math",
            "teacher": "Teacher",
            "room": "New",
            "duration": 2,
            "start_time": "08:00",
            "end_time": "09:30",
            "course_id": "42",
            "info": "Notes",
            "week_type": "A" if badge == "A" else None,
        }.items():
            assert saved[key] == expected
        assert original["room"] == "Old"
        again = await edit.fn(date="2026-10-26", period="1", teacher="New teacher")
        assert again["success"] is True
        assert again["lesson"]["date"] == "2026-10-19"
        assert again["lesson"]["room"] == "New"
        assert again["lesson"]["teacher"] == "New teacher"
        assert len(await auth_db.get_custom_lessons("5201:alice")) == 1

        # The same first-time edit is safe for another user; no overrides are shared.
        assert await auth_db.get_custom_lessons("5201:bob") == []
        plan["plan_for_own"] = [[], [original, dict(original, name="Other")]]
        ambiguous = await edit.fn(date="2026-10-20", period="1", teacher="Changed")
        assert ambiguous["error"] == "INVALID_REQUEST"
        assert len(await auth_db.get_custom_lessons("5201:alice")) == 1

    asyncio.run(scenario())


def test_empty_personal_plan_does_not_seed_another_classes_lesson():
    from api.agent_tools import portal_lesson_defaults

    portal = [[{"stunde": 1, "name": "Other class", "teacher": "Other"}]]
    for own in ([], [[]]):
        assert (
            portal_lesson_defaults(
                {"success": True, "plan_for_own": own, "plan_for_all": portal},
                "2026-10-19",
                "1",
            )
            == {}
        )
    for plan in (
        {"success": True, "plan_for_all": portal},
        {"success": True, "plan_for_own": None, "plan_for_all": portal},
    ):
        assert (
            portal_lesson_defaults(plan, "2026-10-19", "1")["subject"] == "Other class"
        )


@pytest.mark.parametrize("omitted", ["code_challenge", "state"])
def test_missing_authorization_parameters_return_oauth_error(environment, omitted):
    async def scenario():
        async with client() as c:
            params = {
                "client_id": "unused",
                "redirect_uri": CALLBACK,
                "code_challenge": CHALLENGE,
                "state": "state",
            }
            params.pop(omitted)
            response = await c.get("/oauth/authorize", params=params)
            assert response.status_code == 400
            assert response.json()["error"] == "invalid_request"
            assert response.headers["cache-control"] == "no-store"

    asyncio.run(scenario())


def test_successful_write_survives_metadata_database_failure(
    environment, monkeypatch, caplog
):
    import sqlite3
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(return_value={"id": "test", "user_id": "5201:alice"}),
        )
        monkeypatch.setattr(
            api_module.sessions,
            "_get_or_create_schulportal_client",
            AsyncMock(return_value=SimpleNamespace(client=None)),
        )
        result = {"success": True, "message_id": "sent-once"}
        writes = []

        @asynccontextmanager
        async def failed_database(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
            yield  # pragma: no cover

        async def send(_auth):
            writes.append("sent")
            monkeypatch.setattr(store, "database", failed_database)
            return result

        assert await agent_mcp.run("messages:write", send, write=True) is result
        assert writes == ["sent"]
        assert "Could not record agent last_used" in caplog.text

    asyncio.run(scenario())


def test_registration_reclaims_idle_capacity_without_breaking_pending_consent(
    environment,
):
    import json

    async def scenario():
        async with client() as c:
            cid, code, _ = await request_grant(c)
            async with store.database(True) as db:
                await db.executemany(
                    "INSERT INTO agent_clients VALUES(?,?,?)",
                    [
                        (
                            f"idle-{i:05}",
                            json.dumps(
                                {"client_name": "Idle", "redirect_uris": [CALLBACK]}
                            ),
                            time.time() + 86400,
                        )
                        for i in range(9999)
                    ],
                )
            pending = await c.get(
                "/oauth/authorize",
                params={
                    "client_id": "idle-00000",
                    "redirect_uri": CALLBACK,
                    "code_challenge": CHALLENGE,
                    "state": "pending-state",
                    "scope": "schoolwork:read",
                },
            )
            assert pending.status_code == 303
            rid = parse_qs(urlsplit(pending.headers["location"]).query)["request"][0]
            registered = await c.post(
                "/oauth/register",
                json={"client_name": "New", "redirect_uris": [CALLBACK]},
            )
            assert registered.status_code == 201
            async with store.database() as db:
                assert (
                    await store.one(db, "SELECT COUNT(*) AS count FROM agent_clients")
                )["count"] == 9001
                assert await store.one(
                    db, "SELECT id FROM agent_clients WHERE id=?", (cid,)
                )
                assert (
                    await store.one(
                        db, "SELECT id FROM agent_clients WHERE id=?", ("idle-00000",)
                    )
                    is None
                )
            assert (await exchange(c, cid, code)).status_code == 200
            approval = await c.post(
                "/agents/authorization/" + rid,
                headers=HEAD,
                json={
                    "approve": True,
                    "scopes": ["schoolwork:read"],
                    "name": "Pending",
                },
            )
            assert approval.status_code == 200
            pending_code = parse_qs(urlsplit(approval.json()["redirect_url"]).query)[
                "code"
            ][0]
            assert (await exchange(c, "idle-00000", pending_code)).status_code == 200

    asyncio.run(scenario())


def test_recurring_lesson_exact_match_and_ambiguous_fallback(environment):
    async def scenario():
        for period, room in [("1", "Single"), ("1–2", "Double")]:
            await auth_db.save_custom_lesson(
                "5201:alice",
                {
                    "date": "2026-10-19",
                    "period": period,
                    "subject": "Math",
                    "room": room,
                },
            )
        saved = await auth_db.save_custom_lesson(
            "5201:alice",
            {"date": "2026-10-26", "period": "1–2", "teacher": "Changed"},
            merge_and_validate=lambda values: values,
        )
        assert saved["period"] == "1–2"
        assert saved["room"] == "Double"
        before = await auth_db.get_custom_lessons("5201:alice")
        assert next(row for row in before if row["period"] == "1")["teacher"] == ""
        with pytest.raises(HTTPException) as error:
            await auth_db.save_custom_lesson(
                "5201:alice",
                {"date": "2026-11-02", "period": "1–3", "room": "Wrong"},
                merge_and_validate=lambda values: values,
            )
        assert error.value.status_code == 422
        assert await auth_db.get_custom_lessons("5201:alice") == before

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "redirect",
    [
        "http://school.example/callback",
        "javascript:alert(1)",
        "https://user:pass@example.com/cb",
        "https://[broken",
    ],
)
def test_registration_invalid_redirect_has_oauth_error(environment, redirect):
    async def scenario():
        async with client() as c:
            result = await c.post("/oauth/register", json={"redirect_uris": [redirect]})
            assert result.status_code == 400
            assert result.json()["error"] == "invalid_redirect_uri"
            assert result.headers["cache-control"] == "no-store"

    asyncio.run(scenario())


def test_preferences_validation_runs_after_permission_check(environment, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        identity = AsyncMock(return_value={"id": "test", "user_id": "5201:alice"})
        monkeypatch.setattr(agent_mcp, "identity", identity)
        monkeypatch.setattr(
            api_module.sessions,
            "_get_or_create_schulportal_client",
            AsyncMock(return_value=SimpleNamespace(client=None)),
        )
        tool = mcp._tool_manager.get_tool("lanis_update_preferences")
        result = await tool.fn(updates={"appearance": {"theme_mode": "invalid"}})
        assert result["error"] == "INVALID_REQUEST"
        identity.assert_awaited_with("preferences:write")
        identity.side_effect = HTTPException(403, "Not permitted")
        with pytest.raises(HTTPException) as error:
            await tool.fn(updates={"appearance": {"theme_mode": "invalid"}})
        assert error.value.status_code == 403

    asyncio.run(scenario())


@pytest.mark.parametrize("refresh", [False, True])
def test_concurrent_credential_redemption_has_single_winner(environment, refresh):
    async def scenario():
        async with client() as c:
            cid, code, _ = await request_grant(c)
            if refresh:
                tokens = (await exchange(c, cid, code)).json()

                async def redeem():
                    return await c.post(
                        "/oauth/token",
                        data={
                            "grant_type": "refresh_token",
                            "client_id": cid,
                            "refresh_token": tokens["refresh_token"],
                        },
                    )
            else:

                async def redeem():
                    return await exchange(c, cid, code)

            responses = await asyncio.gather(redeem(), redeem())
            assert sorted(r.status_code for r in responses) == [200, 400]
            winner = next(r for r in responses if r.status_code == 200)
            assert await agents.authenticate(winner.json()["access_token"]) is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "methods, expected",
    [
        (["stundenplan_get_plan"], {"lanis_timetable", "lanis_update_lesson"}),
        (["vertretungsplan_get_plan"], {"lanis_substitutions"}),
        (["lerngruppen_get_overview"], {"lanis_study_groups", "lanis_exams"}),
        ([], set()),
    ],
)
def test_module_discovery_is_independent_of_shared_scopes(
    environment, monkeypatch, methods, expected
):
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(
                return_value={
                    "id": "test",
                    "user_id": "5201:alice",
                    "scopes": list(agents.SCOPES),
                }
            ),
        )
        monkeypatch.setattr(
            agent_mcp,
            "fetch_module_access",
            lambda _: {"success": True, "modules": [{"usage": methods}]},
        )
        tools = {t.name for t in await agent_mcp.permitted_tools()}
        assert tools == expected | {
            "lanis_connection",
            "lanis_profile",
            "lanis_modules",
            "lanis_preferences",
            "lanis_update_preferences",
        }

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("lanis_substitutions", {}),
        ("lanis_message", {"conversation_id": "42"}),
        ("lanis_set_homework", {"course_id": "42", "entry_id": "1", "done": True}),
    ],
)
def test_hidden_module_cannot_be_called_directly(
    environment, monkeypatch, name, arguments
):
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(
                return_value={
                    "id": "test",
                    "user_id": "5201:alice",
                    "scopes": list(agents.SCOPES),
                }
            ),
        )
        monkeypatch.setattr(
            agent_mcp, "fetch_module_access", lambda _: {"success": True, "modules": []}
        )
        operation = AsyncMock(
            side_effect=AssertionError("Unavailable module must not execute")
        )
        monkeypatch.setattr(api_module, "get_vertretungsplan", operation)
        monkeypatch.setattr(api_module.sessions, "get_cached", operation)
        monkeypatch.setattr(api_module, "meinunterricht_homework_done", operation)
        registered = mcp._tool_manager.get_tool(name)
        assert (await registered.fn(**arguments))["error"] == "MODULE_UNAVAILABLE"
        operation.assert_not_called()

    asyncio.run(scenario())


def test_module_lookup_failure_fails_closed_before_write(environment, monkeypatch):
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(
                return_value={
                    "id": "test",
                    "user_id": "5201:alice",
                    "scopes": list(agents.SCOPES),
                }
            ),
        )
        monkeypatch.setattr(
            agent_mcp, "fetch_module_access", lambda _: {"success": False}
        )
        assert {t.name for t in await agent_mcp.permitted_tools()} == {
            "lanis_connection",
            "lanis_profile",
            "lanis_modules",
            "lanis_preferences",
            "lanis_update_preferences",
        }
        registered = mcp._tool_manager.get_tool("lanis_set_homework")
        assert (await registered.fn(course_id="42", entry_id="1", done=True))[
            "error"
        ] == "SCHOOL_UNAVAILABLE"

    asyncio.run(scenario())


def test_exam_tool_uses_lerngruppen_not_calendar(environment, monkeypatch):
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(
                return_value={
                    "id": "test",
                    "user_id": "5201:alice",
                    "scopes": list(agents.SCOPES),
                }
            ),
        )
        source = AsyncMock(
            return_value={
                "success": True,
                "exams": [
                    {"date": "2026-11-10", "type": "Arbeit"},
                    {"date": "2026-11-05", "type": "Lernkontrolle"},
                ],
                "grades": ["must not leak"],
            }
        )
        monkeypatch.setattr(api_module, "get_lerngruppen", source)
        calendar = AsyncMock(side_effect=AssertionError("Wrong exam source"))
        monkeypatch.setattr(api_module, "get_calendar_events", calendar)
        registered = mcp._tool_manager.get_tool("lanis_exams")
        result = await registered.fn()
        assert result["source"] == "lerngruppen"
        assert [e["date"] for e in result["exams"]] == ["2026-11-05", "2026-11-10"]
        assert "grades" not in result
        calendar.assert_not_called()

    asyncio.run(scenario())


def test_connector_branding_is_served_locally(environment):
    async def scenario():
        async with client() as c:
            response = await c.get("/agent-icon.png")
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"
            from pathlib import Path

            assert (
                response.content
                == (
                    Path(__file__).parents[1] / "plugins/lanis/assets/icon.png"
                ).read_bytes()
            )
            assert (await c.get("/favicon.ico")).content == response.content
        # The Docker runtime copies api/ but deliberately excludes plugins/.
        # The served asset must therefore live inside the API package.
        from pathlib import Path

        icon = await api_module.agent_icon()
        assert Path(icon.path).is_relative_to(
            Path(api_module.__file__).resolve().parent
        )
        assert mcp._mcp_server.icons[0].src.endswith("/agent-icon.png")
        assert mcp._mcp_server.website_url == agents.ui_url()

    asyncio.run(scenario())


def test_consent_identity_requires_operator_pinned_id_and_callback(monkeypatch):
    import json

    data = {
        "client_id": "known",
        "client_name": "Claude",
        "redirect_uri": "https://claude.ai/callback",
    }
    monkeypatch.delenv("LANIS_AGENT_TRUSTED_CLIENTS", raising=False)
    assert agents.consent_identity(data) == ("Claude", False)
    monkeypatch.setenv(
        "LANIS_AGENT_TRUSTED_CLIENTS",
        json.dumps(
            {
                "known": {
                    "name": "Confirmed provider",
                    "redirect_uris": [data["redirect_uri"]],
                }
            }
        ),
    )
    assert agents.consent_identity(data) == ("Confirmed provider", True)
    assert agents.consent_identity({**data, "client_id": "impostor"}) == (
        "Claude",
        False,
    )
    assert agents.consent_identity(
        {**data, "redirect_uri": "https://claude.ai.evil.example/callback"}
    ) == ("Claude", False)
    monkeypatch.setenv("LANIS_AGENT_TRUSTED_CLIENTS", "[]")
    assert agents.consent_identity(data) == ("Claude", False)


def test_portal_manifest_access_uses_paths_not_names_or_external_links():
    from types import SimpleNamespace

    from api.agent_mcp import fetch_module_access, module_methods

    client = SimpleNamespace(
        BASE_START_URL="https://start.schulportal.hessen.de",
        get_apps=lambda: {
            "success": True,
            "data": {
                "entrys": [
                    {
                        "Name": "Vertretungsplan",
                        "link": "https://external.example/vertretungsplan.php",
                    },
                    {"link": "other.php?redirect=vertretungsplan.php"},
                    {"link": "stundenplan.php?a=view"},
                    {"link": "https://start.schulportal.hessen.de/lerngruppen.php"},
                ]
            },
        },
    )
    assert module_methods(fetch_module_access(client)) == {
        "stundenplan_get_plan",
        "lerngruppen_get_overview",
    }


def test_module_access_is_per_account_and_refreshed(monkeypatch):
    from types import SimpleNamespace

    from api import agent_mcp

    async def scenario():
        alice_apps = [{"link": "stundenplan.php"}]
        alice = SimpleNamespace(
            school_id="test-school",
            client=SimpleNamespace(
                BASE_START_URL="https://start.schulportal.hessen.de",
                get_apps=lambda: {"success": True, "data": {"entrys": alice_apps}},
            ),
        )
        bob = SimpleNamespace(
            school_id="test-school",
            client=SimpleNamespace(
                BASE_START_URL="https://start.schulportal.hessen.de",
                get_apps=lambda: {
                    "success": True,
                    "data": {"entrys": [{"link": "lerngruppen.php"}]},
                },
            ),
        )
        assert await agent_mcp.account_modules(alice) == {"stundenplan_get_plan"}
        assert await agent_mcp.account_modules(bob) == {"lerngruppen_get_overview"}
        alice_apps.clear()
        assert await agent_mcp.account_modules(alice) == set()
        assert await agent_mcp.account_modules(bob) == {"lerngruppen_get_overview"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method,names",
    [
        ("videokonferenz_get_rooms", {"lanis_video_rooms"}),
        ("dsb_school_plan", {"lanis_dsb_substitutions"}),
        (
            "kalender_get_events",
            {"lanis_calendar", "lanis_calendar_views", "lanis_calendar_event"},
        ),
        (
            "dateiverteilung_get_overview",
            {"lanis_distributions", "lanis_distributed_file"},
        ),
    ],
)
def test_new_module_tools_are_filtered_by_actual_access(
    environment, monkeypatch, method, names
):
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(
                return_value={
                    "id": "test",
                    "user_id": "5201:alice",
                    "scopes": list(agents.SCOPES),
                }
            ),
        )
        monkeypatch.setattr(
            agent_mcp,
            "fetch_module_access",
            lambda _: {"success": True, "modules": [{"usage": [method]}]},
        )
        discovered = {t.name for t in await agent_mcp.permitted_tools()}
        assert discovered == names | {
            "lanis_connection",
            "lanis_profile",
            "lanis_modules",
            "lanis_preferences",
            "lanis_update_preferences",
        }

    asyncio.run(scenario())


def test_dsb_discovery_is_limited_to_configured_school(monkeypatch):
    from types import SimpleNamespace

    from api import agent_mcp

    async def scenario():
        monkeypatch.setenv("DSB_SCHOOL_ID", "5201")
        monkeypatch.setenv("DSB_USERNAME", "test-only")
        monkeypatch.setenv("DSB_PASSWORD", "test-only")
        client = SimpleNamespace(
            get_apps=lambda: {"success": True, "data": {"entrys": []}}
        )
        assert await agent_mcp.account_modules(
            SimpleNamespace(client=client, school_id="5201")
        ) == {"dsb_school_plan"}
        assert (
            await agent_mcp.account_modules(
                SimpleNamespace(client=client, school_id="other")
            )
            == set()
        )

    asyncio.run(scenario())


def test_module_catalog_distinguishes_unsupported_from_inaccessible(
    environment, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(
                return_value={
                    "id": "test",
                    "user_id": "5201:alice",
                    "scopes": ["profile:read"],
                }
            ),
        )
        portal = SimpleNamespace(
            BASE_START_URL="https://start.schulportal.hessen.de",
            get_apps=lambda: {
                "success": True,
                "data": {
                    "entrys": [
                        {"Name": "Videokonferenz", "link": "videokonferenz.php"},
                        {
                            "Name": "Moodle",
                            "link": "https://school.example/moodle?secret=must-not-leak",
                        },
                    ]
                },
            },
        )
        monkeypatch.setattr(
            api_module.sessions,
            "_get_or_create_schulportal_client",
            AsyncMock(return_value=SimpleNamespace(client=portal)),
        )
        result = await mcp._tool_manager.get_tool("lanis_modules").fn()
        assert result["modules"] == [
            {"name": "Videokonferenz", "integrated": True},
            {"name": "Moodle", "integrated": False},
        ]
        assert "secret" not in str(result)

    asyncio.run(scenario())


def test_video_rooms_require_separate_permission(environment, monkeypatch):
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        async def identity(scope=None):
            if scope == "meetings:read":
                raise ValueError("PERMISSION_REQUIRED")
            return {"id": "test", "user_id": "5201:alice", "scopes": ["profile:read"]}

        monkeypatch.setattr(agent_mcp, "identity", identity)
        operation = AsyncMock(side_effect=AssertionError("No meeting permission"))
        monkeypatch.setattr(api_module, "get_videokonferenz", operation)
        with pytest.raises(ValueError, match="PERMISSION_REQUIRED"):
            await mcp._tool_manager.get_tool("lanis_video_rooms").fn()
        operation.assert_not_called()

    asyncio.run(scenario())


def test_every_supported_portal_module_has_an_agent_integration():
    """New portal adapters must not silently omit their MCP counterpart."""
    import ast
    import inspect
    import textwrap

    from api.agent_mcp import PORTAL_MODULES, TOOL_MODULES
    from schulportal_hessen.base import SchulportalHessenAPI

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(SchulportalHessenAPI.get_available_modules))
    )
    assignment = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "usage_by_link"
            for target in node.targets
        )
    )
    supported = ast.literal_eval(assignment.value)
    assert set(supported) == set(PORTAL_MODULES) | {"benutzerverwaltung.php"}
    # Benutzerverwaltung is covered by the universal, own-account profile tool.
    assert set(PORTAL_MODULES.values()).issubset(set(TOOL_MODULES.values()))


def test_distributed_file_download_uses_bounded_file_pipeline(environment, monkeypatch):
    from io import BytesIO
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(
                return_value={
                    "id": "test",
                    "user_id": "5201:alice",
                    "scopes": ["files:read"],
                }
            ),
        )
        stream = BytesIO(b"School handout")
        closed = Mock()
        download = Mock(
            return_value={
                "success": True,
                "stream": stream,
                "close": closed,
                "content_type": "text/plain",
                "filename": "handout.txt",
            }
        )
        portal = SimpleNamespace(dateiverteilung_download_file=download)
        monkeypatch.setattr(
            api_module.sessions,
            "_get_or_create_schulportal_client",
            AsyncMock(return_value=SimpleNamespace(client=portal)),
        )
        result = await mcp._tool_manager.get_tool("lanis_distributed_file").fn(
            url="dateiverteilung.php?a=download&id=42"
        )
        assert result["text"] == "School handout"
        download.assert_called_once_with("dateiverteilung.php?a=download&id=42")
        closed.assert_called_once()
        assert stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "link,allowed",
    [
        ("https://START.SCHULPORTAL.HESSEN.DE/videokonferenz.php", True),
        ("https://start.schulportal.hessen.de:444/videokonferenz.php", False),
        ("https://user@start.schulportal.hessen.de/videokonferenz.php", False),
        ("https://start.schulportal.hessen.de.evil.example/videokonferenz.php", False),
    ],
)
def test_module_origin_ignores_host_case_but_not_userinfo_or_port(link, allowed):
    from types import SimpleNamespace

    from api.agent_mcp import fetch_module_access, module_methods

    portal = SimpleNamespace(
        BASE_START_URL="https://start.schulportal.hessen.de",
        get_apps=lambda: {"success": True, "data": {"entrys": [{"link": link}]}},
    )
    assert module_methods(fetch_module_access(portal)) == (
        {"videokonferenz_get_rooms"} if allowed else set()
    )


@pytest.mark.parametrize(
    "kind,expected,status",
    [
        ("authentication", "SCHOOL_SESSION_EXPIRED", 401),
        ("upstream", "SCHOOL_UNAVAILABLE", 503),
    ],
)
def test_manifest_failure_preserves_session_recovery(
    environment, monkeypatch, kind, expected, status
):
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(
                return_value={
                    "id": "test",
                    "user_id": "5201:alice",
                    "scopes": ["timetable:read"],
                }
            ),
        )
        monkeypatch.setattr(
            agent_mcp,
            "fetch_module_access",
            lambda _: {"success": False, "error_kind": kind},
        )
        invalidate = AsyncMock()
        monkeypatch.setattr(
            api_module.sessions, "invalidate_schulportal_client", invalidate
        )
        result = await mcp._tool_manager.get_tool("lanis_timetable").fn()
        assert result["error"] == expected
        assert invalidate.await_count == (1 if status == 401 else 0)
        tools, availability = await agent_mcp.available_tools(
            await agent_mcp.identity()
        )
        assert {t.name for t in tools} == {"lanis_connection"}
        assert availability["error"] == expected

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,expected",
    [(401, "authentication"), (403, "authentication"), (503, "upstream")],
)
def test_apps_client_classifies_http_failure(status, expected):
    from unittest.mock import Mock

    import requests

    from api.agent_mcp import fetch_module_access
    from schulportal_hessen.base import SchulportalHessenAPI

    portal = SchulportalHessenAPI()
    portal.logged_in = True
    response = requests.Response()
    response.status_code = status
    portal.session.get = Mock(side_effect=requests.HTTPError(response=response))
    try:
        assert fetch_module_access(portal)["error_kind"] == expected
        portal.logged_in = False
        assert fetch_module_access(portal)["error_kind"] == "authentication"
    finally:
        portal.close()


def test_module_catalog_recovers_expired_session(environment, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(
                return_value={
                    "id": "test",
                    "user_id": "5201:alice",
                    "scopes": ["profile:read"],
                }
            ),
        )
        portal = SimpleNamespace(
            get_apps=lambda: {"success": False, "error_kind": "authentication"}
        )
        monkeypatch.setattr(
            api_module.sessions,
            "_get_or_create_schulportal_client",
            AsyncMock(return_value=SimpleNamespace(client=portal)),
        )
        invalidate = AsyncMock()
        monkeypatch.setattr(
            api_module.sessions, "invalidate_schulportal_client", invalidate
        )
        result = await mcp._tool_manager.get_tool("lanis_modules").fn()
        assert result["error"] == "SCHOOL_SESSION_EXPIRED"
        invalidate.assert_awaited_once_with("5201:alice", portal)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (302, "", "authentication"),
        (
            200,
            '<form action="/login.php"><input type="password"></form>',
            "authentication",
        ),
        (200, "<h1>Temporary maintenance</h1>", "upstream"),
    ],
)
def test_apps_login_redirect_and_html_are_classified(status, body, expected):
    from unittest.mock import Mock

    import requests

    from schulportal_hessen.base import SchulportalHessenAPI

    portal = SchulportalHessenAPI()
    portal.logged_in = True
    response = requests.Response()
    response.status_code = status
    response.url = "https://start.schulportal.hessen.de/startseite.php?a=ajax&f=apps"
    response.headers["Content-Type"] = "text/html"
    response.headers["Location"] = "https://login.schulportal.hessen.de/"
    response._content = body.encode()
    response.close = Mock()
    portal.session.get = Mock(return_value=response)
    try:
        result = portal.get_apps()
        assert result["error_kind"] == expected
        portal.session.get.assert_called_once_with(
            response.url, allow_redirects=False, timeout=(10, 30)
        )
        response.close.assert_called_once()
    finally:
        portal.close()


@pytest.mark.parametrize(
    "location,expected",
    [
        ("https://login.schulportal.hessen.de/", "authentication"),
        ("/login.php", "authentication"),
        ("/new-app-manifest", "upstream"),
        ("https://login.schulportal.hessen.de.evil.example/", "upstream"),
        ("", "upstream"),
        ("https://[bad", "upstream"),
        ("https://example.com／invalid", "upstream"),
    ],
)
def test_apps_redirect_destination_controls_session_classification(location, expected):
    from unittest.mock import Mock

    import requests

    from schulportal_hessen.base import SchulportalHessenAPI

    portal = SchulportalHessenAPI()
    portal.logged_in = True
    response = requests.Response()
    response.status_code = 302
    response.headers["Location"] = location
    response.close = Mock()
    portal.session.get = Mock(return_value=response)
    try:
        assert portal.get_apps()["error_kind"] == expected
        response.close.assert_called_once()
    finally:
        portal.close()


def test_authorization_details_returns_operator_pinned_identity(
    environment, monkeypatch
):
    import json

    async def scenario():
        async with client() as c:
            cid, _, rid = await request_grant(c)
            # Start a second consent request because request_grant consumed the first.
            response = await c.get(
                "/oauth/authorize",
                params={
                    "client_id": cid,
                    "redirect_uri": CALLBACK,
                    "code_challenge": CHALLENGE,
                    "state": "test",
                    "scope": "schoolwork:read",
                    "resource": "http://localhost:8000/mcp",
                },
            )
            rid = parse_qs(urlsplit(response.headers["location"]).query)["request"][0]
            monkeypatch.setenv(
                "LANIS_AGENT_TRUSTED_CLIENTS",
                json.dumps(
                    {cid: {"name": "Verified provider", "redirect_uris": [CALLBACK]}}
                ),
            )
            response = await c.get("/agents/authorization/" + rid, headers=HEAD)
            assert response.status_code == 200
            assert response.json()["client_name"] == "Verified provider"
            assert response.json()["verified"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        [],
        {"success": True, "data": None},
        {"success": True, "data": {"entrys": {}}},
        {"success": True, "data": {}},
    ],
)
def test_invalid_manifest_envelope_is_unavailable(manifest):
    from api.agent_mcp import normalized_manifest

    assert normalized_manifest(manifest) == {"success": False, "error_kind": "upstream"}


def test_malformed_apps_do_not_hide_valid_modules(environment, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from api import agent_mcp

    async def scenario():
        portal = SimpleNamespace(
            BASE_START_URL="https://start.schulportal.hessen.de",
            get_apps=lambda: {
                "success": True,
                "data": {
                    "entrys": [
                        None,
                        [],
                        {"link": []},
                        {"link": "https://[broken"},
                        {"link": ""},
                        {"Name": "Stundenplan", "link": "stundenplan.php"},
                        {"Name": "Moodle", "link": "https://school.example/moodle"},
                    ]
                },
            },
        )
        # Exercise the real reader, independently of the fixture's access stub.
        manifest = agent_mcp.normalized_manifest(portal.get_apps())
        assert manifest["success"] is True
        assert [e["name"] for e in manifest["entries"]] == ["Stundenplan", "Moodle"]
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(
                return_value={
                    "id": "test",
                    "user_id": "5201:alice",
                    "scopes": ["profile:read"],
                }
            ),
        )
        monkeypatch.setattr(
            api_module.sessions,
            "_get_or_create_schulportal_client",
            AsyncMock(return_value=SimpleNamespace(client=portal)),
        )
        result = await mcp._tool_manager.get_tool("lanis_modules").fn()
        assert result["modules"] == [
            {"name": "Stundenplan", "integrated": True},
            {"name": "Moodle", "integrated": False},
        ]

    asyncio.run(scenario())


def test_access_manifest_skips_bad_entries_and_excludes_external_apps():
    from types import SimpleNamespace

    from api.agent_mcp import fetch_module_access, module_methods

    portal = SimpleNamespace(
        BASE_START_URL="https://start.schulportal.hessen.de",
        get_apps=lambda: {
            "success": True,
            "data": {
                "entrys": [
                    {"link": 42},
                    None,
                    {"link": "https://[bad"},
                    {"link": "stundenplan.php"},
                    {"link": "https://external.example/vertretungsplan.php"},
                ]
            },
        },
    )
    assert module_methods(fetch_module_access(portal)) == {"stundenplan_get_plan"}


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("authentication", "SCHOOL_SESSION_EXPIRED"),
        ("upstream", "FILE_UNAVAILABLE"),
        ("validation", "FILE_UNAVAILABLE"),
    ],
)
def test_distributed_download_failure_preserves_authentication(
    environment, monkeypatch, kind, expected
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(
            agent_mcp,
            "identity",
            AsyncMock(
                return_value={
                    "id": "test",
                    "user_id": "5201:alice",
                    "scopes": ["files:read"],
                }
            ),
        )
        portal = SimpleNamespace(
            dateiverteilung_download_file=Mock(
                return_value={
                    "success": False,
                    "error_kind": kind,
                    "error": "private upstream details",
                }
            )
        )
        monkeypatch.setattr(
            api_module.sessions,
            "_get_or_create_schulportal_client",
            AsyncMock(return_value=SimpleNamespace(client=portal)),
        )
        invalidate = AsyncMock()
        monkeypatch.setattr(
            api_module.sessions, "invalidate_schulportal_client", invalidate
        )
        result = await mcp._tool_manager.get_tool("lanis_distributed_file").fn(
            url="dateiverteilung.php?a=download&id=42"
        )
        assert result["error"] == expected
        assert "private upstream details" not in str(result)
        assert invalidate.await_count == (1 if kind == "authentication" else 0)

    asyncio.run(scenario())


def test_openai_domain_verification_is_exact_and_optional(environment, monkeypatch):
    async def scenario():
        async with client() as c:
            monkeypatch.delenv("LANIS_OPENAI_APPS_CHALLENGE", raising=False)
            assert (await c.get("/.well-known/openai-apps-challenge")).status_code == 404
            monkeypatch.setenv("LANIS_OPENAI_APPS_CHALLENGE", "public-portal-challenge")
            result = await c.get("/.well-known/openai-apps-challenge")
            assert result.status_code == 200
            assert result.text == "public-portal-challenge"
            assert result.headers["content-type"].startswith("text/plain")
            assert result.headers["cache-control"] == "no-store"
            monkeypatch.setenv("LANIS_OPENAI_APPS_CHALLENGE", "one\ntwo")
            assert (await c.get("/.well-known/openai-apps-challenge")).status_code == 404
    asyncio.run(scenario())


def test_sent_messages_are_annotated_as_irreversible():
    for name in ("lanis_send_message", "lanis_reply_message"):
        annotations = mcp._tool_manager.get_tool(name).annotations
        assert annotations.destructiveHint is True
        assert annotations.readOnlyHint is False
        assert annotations.openWorldHint is True


def test_connection_status_does_not_dispatch_or_discover_school_tools(monkeypatch):
    from unittest.mock import AsyncMock
    from api import agent_mcp

    async def scenario():
        monkeypatch.setattr(agent_mcp, "identity", AsyncMock(return_value={
            "name": "Review connection", "user_id": "5201:alice", "scopes": ["schoolwork:read"]
        }))
        forbidden = AsyncMock(side_effect=AssertionError("Metadata must not dispatch"))
        monkeypatch.setattr(agent_mcp, "run", forbidden)
        monkeypatch.setattr(agent_mcp, "available_tools", forbidden)
        result = await agent_mcp.lanis_connection()
        assert set(result) == {
            "success", "connection_name", "scopes", "current_time", "timezone",
            "settings_url", "demo", "writes_simulated",
        }
        assert result["timezone"] == "Europe/Berlin"
        assert result["scopes"] == ["schoolwork:read"]
        forbidden.assert_not_called()
        tools = {t.name: t for t in await agent_mcp.mcp.list_tools()}
        schema = tools["lanis_connection"].outputSchema
        assert set(schema["properties"]) == set(result) | {"demo", "writes_simulated"}
        assert set(schema["required"]) == set(result)
        modules = tools["lanis_modules"]
        assert "modules" in modules.outputSchema["properties"]
        assert "name" in modules.outputSchema["$defs"]["SchoolApp"]["properties"]
        assert modules.inputSchema["properties"] == {}
        # Inventory tools do not replace the individual action definitions.
        for name in ("lanis_courses", "lanis_send_message", "lanis_submit_election"):
            assert name in tools
            assert tools[name].inputSchema
            assert tools[name].annotations is not None

    asyncio.run(scenario())
