import asyncio
import csv
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import auth_db, beta


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("ANDROID_BETA_ENABLED", "true")
    asyncio.run(auth_db.initialize())


def test_validation():
    assert (
        beta.normalize_email(" Tester+Play@Example.COM ") == "tester+play@example.com"
    )
    for address in [
        "x",
        "x@",
        "a..b@example.com",
        "a.@example.com",
        "=x@example.com",
        "a@-example.com",
        "a@example.com\nBcc: foo",
    ]:
        with pytest.raises(HTTPException):
            beta.normalize_email(address)


def test_account_state_and_approval_lifecycle(database):
    async def scenario():
        first = await beta.change("1:one", "signup", "A@example.com")
        assert first["status"] == "pending"
        assert (await beta.get("1:two"))["email"] is None
        row = (await beta.listing())[0]
        await beta.approve([SimpleNamespace(**row)])
        approved = await beta.get("1:one")
        assert approved["approval_unseen"]
        await beta.change("1:one", "acknowledge", revision="stale")
        assert (await beta.get("1:one"))["approval_unseen"]
        await beta.change("1:one", "acknowledge", revision=approved["revision"])
        assert not (await beta.get("1:one"))["approval_unseen"]
        same = await beta.change("1:one", "signup", "A@example.com")
        assert same["status"] == "approved"
        changed = await beta.change("1:one", "signup", "b@example.com")
        assert changed["status"] == "pending"
        with pytest.raises(HTTPException):
            await beta.approve([SimpleNamespace(**row)])
        assert (await beta.change("1:one", "withdraw"))["status"] == "none"
        assert await beta.listing() == []
        await beta.change("1:one", "signup", "a@example.com")
        await auth_db.delete_user_data("1:one")
        assert await beta.listing() == []
        assert (await beta.get("1:one"))["invitation_due"]

    asyncio.run(scenario())


def test_unique_email_concurrency(database):
    async def scenario():
        results = await asyncio.gather(
            beta.change("1:one", "signup", "a@example.com"),
            beta.change("1:two", "signup", " A@EXAMPLE.COM "),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        conflict = next(
            result for result in results if isinstance(result, HTTPException)
        )
        assert conflict.status_code == 409
        assert "1:" not in conflict.detail
        assert len(await beta.listing()) == 1

    asyncio.run(scenario())


def test_cooldown_and_disable(database, monkeypatch):
    async def scenario():
        assert (await beta.get("1:one"))["invitation_due"]
        assert not (await beta.change("1:one", "later"))["invitation_due"]
        monkeypatch.setattr(beta.time, "time", lambda: 9999999999)
        assert (await beta.get("1:one"))["invitation_due"]
        assert not (await beta.change("1:one", "dismiss"))["invitation_due"]
        assert (await beta.change("1:one", "signup", "a@example.com"))[
            "status"
        ] == "pending"
        monkeypatch.setenv("ANDROID_BETA_ENABLED", "false")
        with pytest.raises(HTTPException):
            await beta.change("1:two", "signup", "b@example.com")
        assert (await beta.change("1:one", "withdraw"))["status"] == "none"

    asyncio.run(scenario())


def test_export_preserves_and_deduplicates_without_approval(database):
    async def scenario():
        await beta.change("1:one", "signup", "a@example.com")
        before = await beta.listing()
        output = beta.export_csv(before, ["existing@example.com", " A@EXAMPLE.COM "])
        assert not output.encode().startswith(b"\xef\xbb\xbf")
        assert list(csv.reader(io.StringIO(output))) == [
            ["a@example.com"],
            ["existing@example.com"],
        ]
        assert await beta.listing() == before

    asyncio.run(scenario())


def test_bulk_approval_atomic_on_stale_revision(database):
    async def scenario():
        await beta.change("1:one", "signup", "a@example.com")
        await beta.change("1:two", "signup", "b@example.com")
        rows = await beta.listing()
        rows[1]["revision"] = "stale"
        with pytest.raises(HTTPException):
            await beta.approve([SimpleNamespace(**row) for row in rows])
        assert all(row["approved_at"] is None for row in await beta.listing())

    asyncio.run(scenario())


def test_routes_require_account_and_admin_auth(database, monkeypatch):
    monkeypatch.setenv("LANIS_ADMIN_JWT_SECRET", "a" * 64)
    monkeypatch.setenv("LANIS_ADMIN_ACCOUNTS", "1:admin")
    import httpx

    from api.api import AuthSession, app, local_auth_dependency

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/settings/android-beta")).status_code in (
                401,
                422,
            )
            for method, path, body in [
                ("GET", "/admin/android-beta", None),
                (
                    "POST",
                    "/admin/android-beta/approve",
                    {"requests": [{"request_id": "x", "revision": "x"}]},
                ),
                ("POST", "/admin/android-beta/export", {"existing_addresses": []}),
            ]:
                response = await client.request(
                    method,
                    path,
                    json=body,
                    headers={"X-Admin-Token": "ordinary-user-token"},
                )
                assert response.status_code == 401
            app.dependency_overrides[local_auth_dependency] = lambda: AuthSession(
                client=None,
                user_id="1:one",
                school_id="1",
                username="one",
                session_id="test",
            )
            try:
                await client.post(
                    "/settings/android-beta",
                    json={
                        "action": "signup",
                        "email": "owner@example.com",
                        "user_id": "1:other",
                    },
                )
                assert (await client.get("/settings/android-beta")).json()[
                    "email"
                ] == "owner@example.com"
                assert (await beta.get("1:other"))["email"] is None
            finally:
                app.dependency_overrides.clear()

    asyncio.run(scenario())
