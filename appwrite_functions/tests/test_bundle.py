from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest

from appwrite_functions.src.backend import (
    BackendSettings,
    CipherError,
    CredentialStore,
    IdentityService,
    _Cipher,
    _identity_user_id,
)
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
        BackendSettings.from_env(
            {
                "LANIS_APPWRITE_ENDPOINT": "https://cloud.example/v1",
                "LANIS_APPWRITE_PROJECT_ID": "project",
            }
        )


def test_cipher_round_trip_and_authentication():
    cipher = _Cipher("correct horse battery staple with entropy")
    encrypted = cipher.encrypt("sph-password")
    assert encrypted != "sph-password"
    assert cipher.decrypt(encrypted) == "sph-password"
    with pytest.raises(CipherError):
        cipher.decrypt(encrypted[:-1] + ("A" if encrypted[-1] != "A" else "B"))


def test_worker_treats_an_empty_scheduled_body_as_an_empty_payload():
    from appwrite_functions.src.worker import _body

    class EmptyRequest:
        body_text = ""
        body_binary = b""

        @property
        def body_json(self):
            raise ValueError("empty body")

    class ScheduledContext:
        req = EmptyRequest()

    assert _body(ScheduledContext()) == {}


def _settings() -> BackendSettings:
    return BackendSettings(
        endpoint="https://cloud.example/v1",
        project_id="project",
        encryption_key="correct horse battery staple with entropy",
    )


def test_appwrite_identity_is_case_insensitive():
    assert _identity_user_id("5201", "Bennet.Wegener") == _identity_user_id(
        " 5201 ", " bennet.wegener "
    )


def test_appwrite_credentials_upsert_one_canonical_row_for_case_variants():
    class Tables:
        def __init__(self):
            self.calls = []

        def upsert_row(self, **kwargs):
            self.calls.append(kwargs)

    tables = Tables()
    store = CredentialStore(
        tables, object(), _settings(), _Cipher(_settings().encryption_key)
    )

    asyncio.run(
        store.store_refresh_token("legacy-a", "5201", "Bennet.Wegener", "secret")
    )
    asyncio.run(
        store.store_refresh_token("legacy-b", "5201", "bennet.wegener", "secret")
    )

    assert tables.calls[0]["row_id"] == tables.calls[1]["row_id"]
    assert tables.calls[1]["data"]["username"] == "bennet.wegener"
    assert tables.calls[1]["data"]["user_id"] == _identity_user_id(
        "5201", "bennet.wegener"
    )


def test_identity_service_reuses_user_for_case_variants():
    class MissingUser(Exception):
        code = 404

    class Users:
        def __init__(self):
            self.users = set()
            self.created = []

        def get(self, *, user_id):
            if user_id not in self.users:
                raise MissingUser()
            return {"$id": user_id}

        def create(self, *, user_id, name):
            self.users.add(user_id)
            self.created.append((user_id, name))
            return {"$id": user_id}

        def create_token(self, *, user_id, length, expire):
            return {"userId": user_id, "secret": "token", "expire": "later"}

    users = Users()
    identity = IdentityService(users, _settings())
    first = asyncio.run(identity.ensure_user_and_create_token("5201", "Bennet.Wegener"))
    second = asyncio.run(
        identity.ensure_user_and_create_token("5201", "bennet.wegener")
    )

    assert first.user_id == second.user_id
    assert users.created == [(first.user_id, "bennet.wegener")]
