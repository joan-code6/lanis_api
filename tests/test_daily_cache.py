import asyncio
from datetime import datetime, timedelta

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
    assert b"homework" not in (tmp_path / "auth.db").read_bytes()
    restored = outage_cache.SnapshotStore()
    restored.enable_persistence(path)
    assert (
        restored.get(key, restored.version(key[0], key[1])).body
        == b'{"secret":"homework"}'
    )
    restored.delete_user_data(key[0])
    restarted = outage_cache.SnapshotStore()
    restarted.enable_persistence(path)
    assert not restarted.entries


def test_preserved_snapshot_rejects_inflight_pre_refresh_write():
    store = outage_cache.SnapshotStore()
    key = ("user", "/kalender", ())
    version = store.version(*key[:2])
    now = outage_cache._utcnow()
    store.put(key, b"old", now, version)
    store.invalidate_endpoint(key[0], key[1], preserve=True)
    store.put(key, b"racing", now, version)
    assert store.get(key, store.version(*key[:2])).body == b"old"


def test_warmup_discovers_unvisited_details_and_replays_variants(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI, Request

    from api import api as api_module
    from api import uptime

    app = FastAPI()
    requested = []

    @app.get("/{path:path}")
    async def read(path: str, request: Request):
        requested.append((request.url.path, request.url.query))
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
    assert asyncio.run(daily_cache.warm_users())
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
