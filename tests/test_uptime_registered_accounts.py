import asyncio
import sqlite3
from datetime import timedelta

import aiosqlite

from api import auth_db, uptime
from api.metrics.user_metrics_db import UserMetricsDB


async def setup_accounts(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.db"
    metrics = UserMetricsDB(tmp_path / "metrics.db")
    monkeypatch.setattr(auth_db, "DB_PATH", str(auth_path))
    monkeypatch.setattr(uptime, "user_metrics_db", metrics)
    await auth_db.initialize()
    await metrics.initialize()
    now = uptime._utcnow()
    monkeypatch.setattr(uptime, "_utcnow", lambda: now)

    async def add(username, status="completed", age=1, credentials=True):
        identity = f"5201:{username.casefold()}"
        await metrics.record_login("5201", username)
        async with aiosqlite.connect(metrics.db_path) as db:
            await db.execute(
                "UPDATE users SET last_seen = ? WHERE login = ?",
                ((now - timedelta(days=age)).isoformat(), username.casefold()),
            )
            await db.commit()
        if status is not None:
            await auth_db.save_user_preferences(
                identity, {"onboarding": {"status": status}}
            )
        if credentials:
            await auth_db.store_refresh_token(
                identity, "5201", username, "private-test-password"
            )
        return identity

    return auth_path, metrics, now, add


def test_pool_requires_completed_onboarding_and_recent_real_activity(
    tmp_path, monkeypatch
):
    async def scenario():
        path, _metrics, now, add = await setup_accounts(tmp_path, monkeypatch)
        eligible = await add("Eligible")
        boundary = await add("Boundary", age=30)
        await add("Inactive", age=31)
        await add("Future", age=-1)
        for state in ("not_started", "in_progress", "skipped", None):
            await add("onboarding-" + str(state), status=state)
        await add("NoCredentials", credentials=False)
        expired = await add("Expired")
        deleted = await add("Deleted")
        malformed = await add("Malformed")
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "UPDATE refresh_tokens SET expires_at = ? WHERE user_id = ?",
                ((now - timedelta(seconds=1)).isoformat(), expired),
            )
            await db.execute(
                "INSERT INTO account_deletion_markers(user_id,expires_at) VALUES (?,?)",
                (deleted, (now + timedelta(days=1)).isoformat()),
            )
            await db.execute(
                "UPDATE user_preferences SET preferences = ? WHERE user_id = ?",
                ("{broken json", malformed),
            )
            await db.commit()
        # Two browser sessions are still one user in the sampling pool.
        await auth_db.store_refresh_token(
            eligible, "5201", "ELIGIBLE", "new-private-password"
        )
        pool = uptime._monitor_accounts()
        assert {a[2] for a in pool} == {eligible, boundary}
        assert len(pool) == 2
        assert "private-password" not in str(pool)
        assert "private-test-password" not in str(pool)
        assert uptime.uptime_is_configured()
        assert uptime._monitor_accounts(eligible)[0][2] == eligible

    asyncio.run(scenario())


def test_only_selected_password_is_decrypted_and_retry_rechecks_eligibility(
    tmp_path, monkeypatch
):
    async def scenario():
        _path, _metrics, _now, add = await setup_accounts(tmp_path, monkeypatch)
        identity = await add("selected")
        await add("other")
        decrypt = auth_db._decrypt_password
        decrypted = []

        def track(value):
            decrypted.append(value)
            return decrypt(value)

        monkeypatch.setattr(auth_db, "_decrypt_password", track)
        pool = uptime._monitor_accounts()
        assert not decrypted
        account = next(a for a in pool if a[2] == identity)
        assert await uptime._registered_credentials(account) == (
            "5201",
            "selected",
            "private-test-password",
        )
        assert len(decrypted) == 1
        # Onboarding changed between initial selection and confirmation.
        await auth_db.save_user_preferences(
            identity, {"onboarding": {"status": "in_progress"}}
        )
        assert await uptime._registered_credentials(account) is None
        assert len(decrypted) == 1

    asyncio.run(scenario())


def test_probe_does_not_make_an_inactive_account_recent(tmp_path, monkeypatch):
    async def scenario():
        _path, metrics, now, add = await setup_accounts(tmp_path, monkeypatch)
        await add("first", age=29)
        await add("second", age=29)
        pool = uptime._monitor_accounts()

        def probe(credentials, stopped=None):
            return {"status": "up", "features": []}

        monkeypatch.setattr(uptime, "_isolated_probe", probe)
        await uptime._probe_round(pool, {a[:2]: str(i) for i, a in enumerate(pool)})
        with sqlite3.connect(metrics.db_path) as db:
            timestamps = [row[0] for row in db.execute("SELECT last_seen FROM users")]
        assert timestamps == [(now - timedelta(days=29)).isoformat()] * 2
        monkeypatch.setattr(uptime, "_utcnow", lambda: now + timedelta(days=2))
        assert uptime._monitor_accounts() == []

    asyncio.run(scenario())


def test_deleted_account_is_not_probed_after_selection(tmp_path, monkeypatch):
    async def scenario():
        path, _metrics, _now, add = await setup_accounts(tmp_path, monkeypatch)
        await add("first")
        await add("second")
        pool = uptime._monitor_accounts()
        async with aiosqlite.connect(path) as db:
            await db.execute("DELETE FROM refresh_tokens")
            await db.commit()

        def must_not_probe(*args):
            raise AssertionError("Deleted/revoked credentials must not be used")

        monkeypatch.setattr(uptime, "_isolated_probe", must_not_probe)
        results = await uptime._probe_round(
            pool, {a[:2]: str(i) for i, a in enumerate(pool)}
        )
        assert all(
            r["status"] == "unknown" and r["error"] == "account_no_longer_eligible"
            for r in results
        )

    asyncio.run(scenario())


def test_missing_stores_do_not_create_a_database(tmp_path, monkeypatch):
    auth_path = tmp_path / "missing-auth.db"
    monkeypatch.setattr(auth_db, "DB_PATH", str(auth_path))
    monkeypatch.setattr(
        uptime, "user_metrics_db", UserMetricsDB(tmp_path / "missing-metrics.db")
    )
    assert uptime._monitor_accounts() == []
    assert not auth_path.exists()


def test_shutdown_does_not_wait_for_a_busy_account_lease(monkeypatch):
    from api.account_data import account_lifecycle_lock

    async def scenario():
        account = ("5201", "busy", "5201:busy")
        lifecycle = await account_lifecycle_lock(account[2])
        await lifecycle.acquire()
        try:
            task = asyncio.create_task(
                uptime._probe_round([account], {account[:2]: "Account 1"})
            )
            await asyncio.sleep(0.02)
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=1)
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("Expected cancelled round")
        finally:
            lifecycle.release()

    asyncio.run(scenario())
