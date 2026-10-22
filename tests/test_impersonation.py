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
