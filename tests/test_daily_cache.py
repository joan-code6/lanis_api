import asyncio
from datetime import datetime, timedelta

import pytest

from api import daily_cache, outage_cache


def test_next_run_uses_berlin_wall_clock_across_dst():
    for day in (
        datetime(2026, 3, 28, 4, tzinfo=daily_cache.ZONE),
        datetime(2026, 10, 24, 4, tzinfo=daily_cache.ZONE),
    ):
        target = daily_cache.next_run(day)
        assert target.date() == day.date() + timedelta(days=1)
        assert target.hour == 3
        assert target.timestamp() > day.timestamp()


def test_outage_or_unknown_status_skips_all_users(monkeypatch):
    from api import uptime

    async def users():
        raise AssertionError("Must not fetch users during an outage")

    monkeypatch.setattr(daily_cache.auth_db, "get_cache_warmup_users", users)
    for status in ("down", "degraded", "not_configured", "unknown"):

        async def check(status=status):
            return {"status": status}

        monkeypatch.setattr(uptime, "run_uptime_check", check)
        assert asyncio.run(daily_cache.warm_users()) is False


def test_encrypted_snapshots_survive_restart_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)
    path = str(tmp_path / "auth.db")
    key = ("school:user", "/kalender", (("category", "private"),))
    store = outage_cache.SnapshotStore()
    store.enable_persistence(path)
    now = outage_cache._utcnow()
    store.put(key, b'{"secret":"homework"}', now, store.version(key[0], key[1]))
    asyncio.run(store.close())
    assert b"homework" not in (tmp_path / "auth.db").read_bytes()
    restored = outage_cache.SnapshotStore()
    restored.enable_persistence(path)
    assert (
        restored.get(key, restored.version(key[0], key[1])).body
        == b'{"secret":"homework"}'
    )
    restored.delete_user_data(key[0])
    asyncio.run(restored.close())
    restarted = outage_cache.SnapshotStore()
    restarted.enable_persistence(path)
    assert not restarted.entries
    asyncio.run(restarted.close())


def test_preserved_snapshot_rejects_inflight_pre_refresh_write():
    store = outage_cache.SnapshotStore()
    key = ("user", "/kalender", ())
    version = store.version(*key[:2])
    now = outage_cache._utcnow()
    store.put(key, b"old", now, version)
    store.invalidate_endpoint(key[0], key[1], preserve=True)
    store.put(key, b"racing", now, version)
    assert store.get(key, store.version(*key[:2])).body == b"old"


@pytest.mark.parametrize("failed", [False, True])
def test_warmup_discovers_unvisited_details_and_replays_variants(monkeypatch, failed):
    from types import SimpleNamespace

    from fastapi import FastAPI, Request

    from api import api as api_module
    from api import uptime

    app = FastAPI()
    requested = []

    @app.get("/{path:path}")
    async def read(path: str, request: Request):
        requested.append((request.url.path, request.url.query))
        if path == "benutzer" and failed:
            return {"success": False, "error": "upstream unavailable"}
        if path == "meinunterricht":
            return {"success": True, "courses": [{"book_id": "new-course"}]}
        if path == "kalender/events":
            return {"success": True, "events": [{"id": "new-event"}]}
        return {"success": True}

    async def check():
        return {"status": "up"}

    async def users():
        return [
            {
                "user_id": "user",
                "school_id": "school",
                "username": "name",
                "session_id": "session",
            }
        ]

    async def no_delay(_):
        pass

    monkeypatch.setattr(uptime, "run_uptime_check", check)
    monkeypatch.setattr(daily_cache.auth_db, "get_cache_warmup_users", users)
    monkeypatch.setattr(api_module, "app", app)
    monkeypatch.setattr(
        api_module.sessions, "create_access_token", lambda **_: "private-token"
    )
    monkeypatch.setattr(daily_cache.asyncio, "sleep", no_delay)
    monkeypatch.setattr(
        outage_cache,
        "snapshots",
        SimpleNamespace(
            entries={
                ("user", "/kalender/events", (("category", "special"),)): None,
                ("user", "/nachrichten/private", ()): None,
                ("other", "/kalender/event/other", ()): None,
            }
        ),
    )
    assert asyncio.run(daily_cache.warm_users()) is (not failed)
    assert ("/meinunterricht/course/new-course", "") in requested
    assert ("/kalender/event/new-event", "") in requested
    assert ("/kalender/events", "category=special") in requested
    assert not any(
        path in {"/nachrichten/private", "/kalender/event/other"}
        for path, _ in requested
    )
    assert daily_cache.warming.get() is False


def test_failed_persistent_replacement_keeps_previous_snapshot(monkeypatch):
    import pytest

    store = outage_cache.SnapshotStore()
    key = ("user", "/kalender", ())
    version = store.version(*key[:2])
    store.put(key, b"previous", outage_cache._utcnow(), version)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_persist", fail)
    with pytest.raises(OSError):
        store.put(key, b"new", outage_cache._utcnow(), version)
    assert store.get(key, version).body == b"previous"
    assert store.total_bytes == len(b"previous")
    assert store.status("user")["retention_seconds"] == 0


def test_warmup_bypasses_live_cache_without_deleting_it():
    from api import api as api_module

    async def scenario():
        manager = api_module.AuthManager()
        await manager.set_cache("user", "/kalender", {"success": True})
        reset = daily_cache.warming.set(True)
        try:
            assert await manager.get_cached("user", "/kalender") is None
            assert await manager.get_cached_with_revalidate("user", "/kalender") == (
                None,
                False,
            )
        finally:
            daily_cache.warming.reset(reset)
        assert await manager.get_cached("user", "/kalender") == {"success": True}

    asyncio.run(scenario())


def test_snapshot_writer_allows_auth_transaction_to_release(tmp_path, monkeypatch):
    import aiosqlite

    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)

    async def scenario():
        path = str(tmp_path / "auth.db")
        store = outage_cache.SnapshotStore()
        store.enable_persistence(path)
        async with aiosqlite.connect(path) as auth:
            await auth.execute("BEGIN IMMEDIATE")
            key = ("user", "/kalender", ())
            store.put(key, b"{}", outage_cache._utcnow(), store.version(*key[:2]))
            # The worker may wait on SQLite; the event loop must remain free to commit.
            await asyncio.wait_for(auth.commit(), timeout=1)
            await asyncio.wait_for(store.close(), timeout=1)
        restored = outage_cache.SnapshotStore()
        restored.enable_persistence(path)
        assert restored.get(key, restored.version(*key[:2])).body == b"{}"
        await restored.close()

    asyncio.run(scenario())


def test_homework_change_invalidates_live_course_views_and_preserves_snapshots(
    monkeypatch,
):
    from types import SimpleNamespace

    from api import api as api_module

    async def scenario():
        manager = api_module.AuthManager()
        store = outage_cache.SnapshotStore()
        monkeypatch.setattr(api_module, "sessions", manager)
        monkeypatch.setattr(api_module, "snapshots", store)
        for endpoint in (
            "/meinunterricht",
            "/meinunterricht/course",
            "/meinunterricht/entry",
            "/meinunterricht/weekly",
            "/stundenplan",
        ):
            await manager.set_cache(
                "user", endpoint, {"success": True, "homework_done": False}
            )
            key = ("user", endpoint, ())
            store.put(key, b"{}", outage_cache._utcnow(), store.version(*key[:2]))
        client = SimpleNamespace(
            meinunterricht_set_homework_done=lambda *_: {"success": True}
        )
        auth = SimpleNamespace(user_id="user", client=client)
        await api_module.meinunterricht_homework_done(
            auth=auth, course_id="course", entry_id="entry", done=True
        )
        assert not manager._cache
        assert len(store.entries) == 5

    asyncio.run(scenario())


def test_restore_skips_and_removes_unreadable_rows(tmp_path, monkeypatch):
    import json
    import sqlite3

    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)
    path = str(tmp_path / "auth.db")
    store = outage_cache.SnapshotStore()
    store.enable_persistence(path)
    key = ("user", "/kalender", ())
    store.put(key, b"{}", outage_cache._utcnow(), store.version(*key[:2]))
    asyncio.run(store.close())
    with sqlite3.connect(path) as db:
        db.executemany(
            "INSERT INTO outage_snapshots VALUES (?, ?, ?)",
            [
                ("not-json", "bad", "bad"),
                (json.dumps(["user", "/bad", []]), "v1:invalid", "2026-10-22"),
                (json.dumps(["user", "/invalid-date", []]), "{}", "bad"),
                (json.dumps([[], "/bad-user", []]), "{}", "2026-10-22"),
                (json.dumps(["user", "/bad-params", [[[]]]]), "{}", "2026-10-22"),
            ],
        )
    restored = outage_cache.SnapshotStore()
    restored.enable_persistence(path)
    assert list(restored.entries) == [key]
    asyncio.run(restored.close())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM outage_snapshots").fetchone()[0] == 1


def test_flush_ignores_other_users_put_failure_but_reports_own_deletion(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)

    async def scenario():
        store = outage_cache.SnapshotStore()
        store.enable_persistence(str(tmp_path / "auth.db"))
        original = store._write_snapshot

        def fail_put(key, entry=None):
            if entry is not None:
                raise OSError("failed unrelated put")
            return original(key, entry)

        monkeypatch.setattr(store, "_write_snapshot", fail_put)
        key = ("other", "/kalender", ())
        store.put(key, b"{}", outage_cache._utcnow(), store.version(*key[:2]))
        store.delete_user_data("user")
        await store.flush("user")
        await store.flush()

        def fail_delete(user_id):
            raise OSError("failed requested deletion")

        monkeypatch.setattr(store, "_delete_persisted_user", fail_delete)
        store.delete_user_data("user")
        with pytest.raises(OSError, match="failed requested deletion"):
            await store.flush("user")
        # Retrying deletion can recover, without unrelated failures poisoning it.
        monkeypatch.undo()
        store.delete_user_data("user")
        await store.close()

    asyncio.run(scenario())


def test_logout_closes_client_and_clears_live_cache_even_if_deletion_fails(monkeypatch):
    from types import SimpleNamespace

    from api import api as api_module

    async def scenario():
        manager = api_module.AuthManager()
        store = outage_cache.SnapshotStore()
        monkeypatch.setattr(api_module, "snapshots", store)
        closed = []
        manager._schulportal_clients["school:user"] = SimpleNamespace(
            client=SimpleNamespace(close=lambda: closed.append(True))
        )
        await manager.set_cache("school:user", "/kalender", {"success": True})
        old_version = await manager.get_cache_version("school:user", "/kalender")

        async def fail_flush(user_id=None):
            raise OSError("deletion failed")

        monkeypatch.setattr(store, "flush", fail_flush)
        with pytest.raises(OSError):
            await manager.drop_schulportal_session("school:user")
        assert closed == [True]
        assert not manager._cache
        assert not manager._schulportal_clients
        assert (
            await manager.get_cache_version("school:user", "/kalender") != old_version
        )

    asyncio.run(scenario())


def test_expiry_deletes_snapshots_only_after_last_session(tmp_path, monkeypatch):
    import aiosqlite

    from api import auth_db

    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)

    async def scenario():
        path = str(tmp_path / "auth.db")
        monkeypatch.setattr(auth_db, "DB_PATH", path)
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()
        store = outage_cache.SnapshotStore()
        store.enable_persistence(path)
        monkeypatch.setattr(outage_cache, "snapshots", store)
        expired_token = await auth_db.store_refresh_token(
            "school:user", "school", "user", "password"
        )
        valid_token = await auth_db.store_refresh_token(
            "school:user", "school", "user", "password"
        )
        key = ("school:user", "/kalender", ())
        store.put(key, b"{}", outage_cache._utcnow(), store.version(*key[:2]))
        await store.flush()
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "UPDATE refresh_tokens SET expires_at = ? WHERE token = ?",
                ("2000-01-01", expired_token),
            )
            await db.commit()
        await auth_db._revoke_expired_sessions()
        assert store.entries
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "UPDATE refresh_tokens SET expires_at = ? WHERE token = ?",
                ("2000-01-01", valid_token),
            )
            await db.commit()
        await auth_db._revoke_expired_sessions()
        assert not store.entries
        await store.close()
        restored = outage_cache.SnapshotStore()
        restored.enable_persistence(path)
        assert not restored.entries
        await restored.close()

    asyncio.run(scenario())


def test_retry_budget_returns_to_next_daily_run(monkeypatch):
    attempts = []
    sleeps = []

    async def fail():
        attempts.append(True)
        return False

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(daily_cache, "MAX_BATCH_ATTEMPTS", 2)
    monkeypatch.setattr(daily_cache, "warm_users", fail)
    monkeypatch.setattr(daily_cache.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(daily_cache.run_daily_cache())
    assert len(attempts) == 2
    assert sleeps[1] == 15 * 60
    assert sleeps[0] > 15 * 60 and sleeps[2] > 15 * 60
