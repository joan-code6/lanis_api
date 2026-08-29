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

    def raise_for_status(self):
        return None


class HttpErrorResponse:
    content = b""
    headers = {}

    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        raise requests.HTTPError("portal authentication failed", response=self)


def test_dateispeicher_download_uses_authenticated_portal_url():
    client = FakeClient()

    result = dateispeicher_download_file(client, 42)

    assert result["success"] is True
    assert result["file_id"] == 42
    assert result["filename"] == "Arbeitsblatt (2026).pdf"
    assert result["content_type"] == "application/pdf"
    assert result["content"] == b"file contents"
    assert client.session.calls == [
        (
            ("https://portal.example/dateispeicher.php",),
            {"params": {"a": "download", "f": "42"}},
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
        async def invalidate_schulportal_client(self, user_id):
            invalidated_users.append(user_id)

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
    assert invalidated_users == ["user-a"]

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
