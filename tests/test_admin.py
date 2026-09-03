import asyncio

import pytest

from api import admin as admin_module
from api.admin import AdminLoginRequest, AdminPrincipal, admin_dependency, admin_login


def test_admin_login_requires_allowlisted_sph_identity(monkeypatch):
    monkeypatch.setenv("LANIS_ADMIN_ACCOUNTS", "5201:Bennet.Wegener,1234:Other.Admin")
    monkeypatch.setenv("LANIS_ADMIN_JWT_SECRET", "a" * 64)

    async def verify(_school_id, _username, _password):
        return True

    async def record(_school_id, _username):
        return None

    monkeypatch.setattr(admin_module, "_verify_sph_credentials", verify)
    monkeypatch.setattr(admin_module, "_record_login", record)
    monkeypatch.setattr(admin_module, "_record_admin_action", record)

    response = asyncio.run(
        admin_login(
            AdminLoginRequest(
                school_id="5201",
                username="Bennet.Wegener",
                password="x",
            )
        )
    )

    assert response.school_id == "5201"
    assert response.username == "bennet.wegener"
    principal = asyncio.run(admin_dependency(response.access_token))
    assert principal == AdminPrincipal("5201:bennet.wegener", "5201", "bennet.wegener")


def test_admin_login_rejects_non_allowlisted_identity(monkeypatch):
    monkeypatch.setenv("LANIS_ADMIN_ACCOUNTS", "5201:admin.one")
    monkeypatch.setenv("LANIS_ADMIN_JWT_SECRET", "a" * 64)

    with pytest.raises(Exception) as exc_info:
        asyncio.run(
            admin_login(
                AdminLoginRequest(
                    school_id="5201", username="student", password="password"
                )
            )
        )

    assert exc_info.value.status_code == 401


def test_admin_token_is_invalidated_when_identity_is_removed(monkeypatch):
    monkeypatch.setenv("LANIS_ADMIN_ACCOUNTS", "5201:admin.one")
    monkeypatch.setenv("LANIS_ADMIN_JWT_SECRET", "a" * 64)
    token = admin_module._issue_token(
        AdminPrincipal("5201:admin.one", "5201", "admin.one")
    )
    monkeypatch.setenv("LANIS_ADMIN_ACCOUNTS", "")

    with pytest.raises(Exception) as exc_info:
        asyncio.run(admin_dependency(token))

    assert exc_info.value.status_code == 403
