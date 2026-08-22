from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from appwrite_functions.src.backend import BackendSettings, CipherError, _Cipher
from appwrite_functions.src.main import main


class _Response:
    def __init__(self) -> None:
        self.calls = []

    def json(self, payload, status_code=200, headers=None, **kwargs):
        self.calls.append((payload, status_code, headers, kwargs))
        return {"payload": payload, "status_code": status_code}


class _Request:
    method: ClassVar[str] = "GET"
    path: ClassVar[str] = "/health"
    query: ClassVar[dict] = {}
    headers: ClassVar[dict] = {}
    body_json: ClassVar[dict] = {}


class _Context:
    req = _Request()

    def __init__(self):
        self.res = _Response()
        self.errors = []

    def error(self, message):
        self.errors.append(message)


def test_health_does_not_initialize_appwrite(monkeypatch):
    monkeypatch.delenv("LANIS_APPWRITE_ENDPOINT", raising=False)
    result = asyncio.run(main(_Context()))
    assert result["payload"] == {"status": "ok"}
    assert result["status_code"] == 200


def test_backend_settings_require_encryption_key():
    with pytest.raises(ValueError):
        BackendSettings.from_env({"LANIS_APPWRITE_ENDPOINT": "https://cloud.example/v1", "LANIS_APPWRITE_PROJECT_ID": "project"})


def test_cipher_round_trip_and_authentication():
    cipher = _Cipher("correct horse battery staple with entropy")
    encrypted = cipher.encrypt("sph-password")
    assert encrypted != "sph-password"
    assert cipher.decrypt(encrypted) == "sph-password"
    with pytest.raises(CipherError):
        cipher.decrypt(encrypted[:-1] + ("A" if encrypted[-1] != "A" else "B"))
