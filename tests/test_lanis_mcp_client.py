import base64

import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from lanis_mcp import server
from lanis_mcp.client import LanisAPIError, LanisClient
from lanis_mcp.server import API_ROUTES, CredentialStore, mcp


@pytest.mark.asyncio
async def test_public_request_omits_auth_header():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health"
        assert "X-Session-Token" not in request.headers
        return httpx.Response(200, json={"status": "ok"})

    client = LanisClient(transport=httpx.MockTransport(handler))
    assert await client.request("GET", "/health", authenticated=False) == {
        "status": "ok"
    }


@pytest.mark.asyncio
async def test_authenticated_request_uses_explicit_token():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Session-Token"] == "secret"
        return httpx.Response(200, json={"success": True})

    client = LanisClient(transport=httpx.MockTransport(handler))
    assert await client.request("GET", "/modules", access_token="secret") == {
        "success": True
    }


@pytest.mark.asyncio
async def test_api_error_exposes_status_and_detail_without_token():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "expired"})

    client = LanisClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LanisAPIError) as caught:
        await client.request("GET", "/modules", access_token="do-not-leak")
    assert caught.value.status_code == 401
    assert "expired" in str(caught.value)
    assert "do-not-leak" not in str(caught.value)


@pytest.mark.asyncio
async def test_file_download_returns_base64_and_metadata():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"hello",
            headers={
                "content-type": "text/plain",
                "content-disposition": 'attachment; filename="note.txt"',
            },
        )

    client = LanisClient(transport=httpx.MockTransport(handler))
    result = await client.download_file("abc")
    assert result == {
        "filename": "note.txt",
        "content_type": "text/plain",
        "size": 5,
        "content_base64": base64.b64encode(b"hello").decode("ascii"),
    }


def test_launch_url_escapes_name_and_token():
    client = LanisClient(base_url="https://example.test/", access_token="a+b/c")
    assert client.app_launch_url("Better Marks") == (
        "https://example.test/app/Better%20Marks?token=a%2Bb%2Fc"
    )


@pytest.mark.asyncio
async def test_every_api_route_has_a_safe_structured_mcp_tool():
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert tools.keys() == API_ROUTES.keys()
    for tool in tools.values():
        assert tool.outputSchema is not None
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is not None
        assert tool.annotations.openWorldHint is not None
        assert tool.annotations.destructiveHint is not None
        assert "access_token" not in tool.inputSchema.get("properties", {})


def test_credentials_are_isolated_by_mcp_session():
    class Session:
        pass

    class Context:
        def __init__(self, session):
            self.session = session

    store = CredentialStore()
    first = store.get(Context(Session()))
    second_session = Session()
    second_context = Context(second_session)
    second = store.get(second_context)

    first.access_token = "first-token"

    assert second.access_token != "first-token"
    assert store.get(second_context) is second


@pytest.mark.asyncio
async def test_login_token_is_reused_inside_the_same_mcp_session(monkeypatch):
    authenticated_tokens = []

    class FakeClient:
        def __init__(self, access_token=None):
            self.access_token = access_token

        async def request(
            self,
            method,
            path,
            *,
            authenticated=True,
            params=None,
            json=None,
            data=None,
        ):
            if path == "/login":
                return {
                    "access_token": "session-access",
                    "refresh_token": "session-refresh",
                }
            authenticated_tokens.append(self.access_token)
            return {"success": True}

    monkeypatch.setattr(server, "_client", FakeClient)

    async with create_connected_server_and_client_session(mcp) as client:
        login = await client.call_tool(
            "lanis_login",
            {
                "school_id": "1234",
                "username": "student",
                "password": "secret",
            },
        )
        profile = await client.call_tool("lanis_get_user", {})

    assert not login.isError
    assert not profile.isError
    assert authenticated_tokens == ["session-access"]
