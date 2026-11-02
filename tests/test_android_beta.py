import asyncio

import aiosqlite
import httpx
import pytest

from api import auth_db, beta
from api.api import AuthSession, app, local_auth_dependency


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setenv("ANDROID_BETA_ENABLED", "true")
    asyncio.run(auth_db.initialize())


def test_preferences_ownership_cooldown_and_deletion(database, monkeypatch):
    async def scenario():
        assert (await beta.get("1:one"))["invitation_due"]
        assert not (await beta.change("1:one", "presented"))["invitation_due"]
        assert (await beta.get("1:two"))["invitation_due"]
        monkeypatch.setattr(beta.time, "time", lambda: 9999999999)
        assert (await beta.get("1:one"))["invitation_due"]
        await beta.change("1:one", "later")
        assert not (await beta.get("1:one"))["invitation_due"]
        await beta.change("1:one", "dismiss")
        assert (await beta.get("1:one"))["dismissed"]
        await auth_db.delete_user_data("1:one")
        assert not (await beta.get("1:one"))["dismissed"]

    asyncio.run(scenario())


def test_concurrent_preferences_do_not_lose_permanent_dismissal(database):
    async def scenario():
        await asyncio.gather(
            beta.change("1:one", "dismiss"), beta.change("1:one", "later")
        )
        assert (await beta.get("1:one"))["dismissed"]

    asyncio.run(scenario())


def test_disabled_default_and_urls(database, monkeypatch):
    monkeypatch.delenv("ANDROID_BETA_ENABLED", raising=False)
    state = asyncio.run(beta.get("1:one"))
    assert not state["available"]
    assert state["group_url"] == "https://groups.google.com/g/lanis-beta/about"
    assert state["join_url"] == "https://play.google.com/apps/testing/de.joancode.lanis"
    assert set(state) == {
        "available",
        "group_url",
        "join_url",
        "dismissed",
        "prompt_after",
        "invitation_due",
    }


def test_legacy_signup_data_erased_preferences_preserved(database):
    async def scenario():
        async with aiosqlite.connect(auth_db.DB_PATH) as db:
            await db.execute(
                "CREATE TABLE android_beta(user_id TEXT, email TEXT, dismissed INTEGER, prompt_after INTEGER)"
            )
            await db.execute(
                "INSERT INTO android_beta VALUES('1:one','private@example.com',1,123)"
            )
            await db.commit()
        await auth_db.initialize()
        state = await beta.get("1:one")
        assert state["dismissed"] and state["prompt_after"] == 123
        async with aiosqlite.connect(auth_db.DB_PATH) as db:
            assert not await (
                await db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='android_beta'"
                )
            ).fetchone()
        await auth_db.initialize()  # idempotent migration

    asyncio.run(scenario())


def test_routes_require_auth_and_cannot_set_membership(database):
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/settings/android-beta")).status_code in (
                401,
                422,
            )
            app.dependency_overrides[local_auth_dependency] = lambda: AuthSession(
                client=None,
                user_id="1:one",
                school_id="1",
                username="one",
                session_id="test",
            )
            try:
                assert (
                    await client.post(
                        "/settings/android-beta",
                        json={"action": "signup", "email": "x@example.com"},
                    )
                ).status_code == 422
                assert (
                    await client.post(
                        "/settings/android-beta", json={"action": "approve"}
                    )
                ).status_code == 422
                await client.post(
                    "/settings/android-beta",
                    json={"action": "dismiss", "user_id": "1:other"},
                )
                assert (await client.get("/settings/android-beta")).json()["dismissed"]
                assert not (await beta.get("1:other"))["dismissed"]
            finally:
                app.dependency_overrides.clear()

    asyncio.run(scenario())
