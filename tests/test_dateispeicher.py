from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import requests
from fastapi import HTTPException

from api import api as api_module
from schulportal_hessen.applets.dateispeicher.api import dateispeicher_download_file
from api.api import AuthSession


class FakeResponse:
    content = b"file contents"
    headers = {
        "Content-Disposition": "attachment; filename*=UTF-8''Arbeitsblatt%20%282026%29.pdf",
        "Content-Type": "application/pdf",
    }

    def __init__(self):
        self.closed = False

    def iter_content(self, chunk_size=8192):
        yield self.content

    def close(self):
        self.closed = True

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or FakeResponse()

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class FakeClient:
    BASE_START_URL = "https://portal.example"
    logged_in = True

    def __init__(self, response=None):
        self.session = FakeSession(response)


class LoginResponse:
    content = b"<html><body><form action='/login'>Anmelden</form></body></html>"
    headers = {"Content-Type": "text/html; charset=utf-8"}

    def iter_content(self, chunk_size=8192):
        yield self.content

    def close(self):
        self.closed = True

    def raise_for_status(self):
        return None


class HtmlAttachmentResponse(LoginResponse):
    headers = {
        "Content-Disposition": 'attachment; filename="hinweise.html"',
        "Content-Type": "text/html; charset=utf-8",
    }


class HttpErrorResponse:
    content = b""
    headers = {}

    def __init__(self, status_code):
        self.status_code = status_code
        self.closed = False

    def close(self):
        self.closed = True

    def raise_for_status(self):
        raise requests.HTTPError("portal authentication failed", response=self)


def test_dateispeicher_download_uses_authenticated_portal_url():
    client = FakeClient()

    result = dateispeicher_download_file(client, 42)

    assert result["success"] is True
    assert result["file_id"] == 42
    assert result["filename"] == "Arbeitsblatt (2026).pdf"
    assert result["content_type"] == "application/pdf"
    assert b"".join(result["stream"]) == b"file contents"
    assert client.session.response.closed is True
    assert client.session.calls == [
        (
            ("https://portal.example/dateispeicher.php",),
            {"params": {"a": "download", "f": "42"}, "stream": True},
        )
    ]


def test_dateispeicher_download_rejects_invalid_file_ids():
    result = dateispeicher_download_file(FakeClient(), 0)

    assert result == {"success": False, "error": "Invalid file id"}


def test_dateispeicher_download_rejects_login_page_as_authentication_failure():
    result = dateispeicher_download_file(FakeClient(LoginResponse()), 42)

    assert result["success"] is False
    assert result["error_kind"] == "authentication"
    assert "login page" in result["error"]


def test_dateispeicher_download_accepts_html_attachment():
    result = dateispeicher_download_file(FakeClient(HtmlAttachmentResponse()), 42)

    assert result["success"] is True
    assert result["filename"] == "hinweise.html"
    assert b"".join(result["stream"]) == HtmlAttachmentResponse.content


@pytest.mark.parametrize("status_code", [401, 403])
def test_dateispeicher_download_classifies_http_auth_failures(status_code):
    result = dateispeicher_download_file(FakeClient(HttpErrorResponse(status_code)), 42)

    assert result["success"] is False
    assert result["error_kind"] == "authentication"
    assert result["upstream_status"] == status_code


def test_dateispeicher_route_distinguishes_authentication_and_upstream_failures(monkeypatch):
    auth = AuthSession(
        client=SimpleNamespace(dateispeicher_download_file=lambda _file_id: None),
        user_id="user-a",
        school_id="school",
        username="user",
    )
    invalidated_users = []

    class FakeSessions:
        async def invalidate_schulportal_client(self, user_id, expected_client):
            invalidated_users.append((user_id, expected_client))

    monkeypatch.setattr(api_module, "sessions", FakeSessions())

    async def run_in_threadpool(_func, _file_id):
        return {
            "success": False,
            "error": "portal session expired",
            "error_kind": "authentication",
        }

    monkeypatch.setattr(api_module, "run_in_threadpool", run_in_threadpool)
    with pytest.raises(HTTPException) as authentication_error:
        asyncio.run(api_module.download_dateispeicher_file(42, auth=auth))
    assert authentication_error.value.status_code == 401
    assert invalidated_users == [("user-a", auth.client)]

    async def run_in_threadpool_upstream(_func, _file_id):
        return {
            "success": False,
            "error": "portal timeout",
            "error_kind": "upstream",
        }

    monkeypatch.setattr(api_module, "run_in_threadpool", run_in_threadpool_upstream)
    with pytest.raises(HTTPException) as upstream_error:
        asyncio.run(api_module.download_dateispeicher_file(42, auth=auth))
    assert upstream_error.value.status_code == 502


def test_dateispeicher_refresh_bypasses_cached_folder(monkeypatch):
    client = SimpleNamespace(
        dateispeicher_get_node=lambda folder_id: {
            "success": True,
            "folder_id": folder_id,
            "files": [],
            "folders": [],
        }
    )
    auth = AuthSession(
        client=client, user_id="user-a", school_id="school", username="user"
    )

    class FakeSessions:
        def __init__(self):
            self.cached = []

        async def get_cached(self, *_args, **_kwargs):
            raise AssertionError("refresh must bypass the response cache")

        async def set_cache(self, user_id, endpoint, data, params=""):
            self.cached.append((user_id, endpoint, data, params))

    fake_sessions = FakeSessions()
    monkeypatch.setattr(api_module, "sessions", fake_sessions)

    result = asyncio.run(api_module.get_dateispeicher(7, refresh=True, auth=auth))

    assert result["folder_id"] == 7
    assert len(fake_sessions.cached) == 1


def test_dateispeicher_search_does_not_cache_failures(monkeypatch):
    client = SimpleNamespace(
        dateispeicher_search_files=lambda _query: {
            "success": False,
            "error": "temporary failure",
        }
    )
    auth = AuthSession(
        client=client, user_id="user-a", school_id="school", username="user"
    )

    class FakeSessions:
        def __init__(self):
            self.cached = []

        async def get_cached(self, *_args, **_kwargs):
            return None

        async def set_cache(self, *args, **_kwargs):
            self.cached.append(args)

    fake_sessions = FakeSessions()
    monkeypatch.setattr(api_module, "sessions", fake_sessions)

    result = asyncio.run(api_module.search_dateispeicher("zeugnis", auth=auth))

    assert result["success"] is False
    assert fake_sessions.cached == []
