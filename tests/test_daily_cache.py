import asyncio
from datetime import datetime, timedelta

import pytest

from api import daily_cache, outage_cache


def test_next_run_uses_berlin_wall_clock_across_dst():
    """Verify next run uses berlin wall clock across dst."""
    for day in (
        datetime(2026, 3, 28, 4, tzinfo=daily_cache.ZONE),
        datetime(2026, 10, 24, 4, tzinfo=daily_cache.ZONE),
    ):
        target = daily_cache.next_run(day)
        assert target.date() == day.date() + timedelta(days=1)
        assert target.hour == 3
        assert target.timestamp() > day.timestamp()


def test_outage_or_unknown_status_skips_all_users(monkeypatch):
    """Verify outage or unknown status skips all users."""
    from api import uptime

    async def users():
        """Provide test session identities without accessing real credentials."""
        raise AssertionError("Must not fetch users during an outage")

    monkeypatch.setattr(daily_cache.auth_db, "get_cache_warmup_users", users)
    for status in ("down", "degraded", "not_configured", "unknown"):

        async def check(status=status):
            """Provide a controlled synthetic portal status."""
            return {"status": status}

        monkeypatch.setattr(uptime, "run_uptime_check", check)
        assert asyncio.run(daily_cache.warm_users()) is False


def test_encrypted_snapshots_survive_restart_and_delete(tmp_path, monkeypatch):
    """Verify encrypted snapshots survive restart and delete."""
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
    """Verify preserved snapshot rejects inflight pre refresh write."""
    store = outage_cache.SnapshotStore()
    key = ("user", "/kalender", ())
    version = store.version(*key[:2])
    now = outage_cache._utcnow()
    store.put(key, b"old", now, version)
    store.invalidate_endpoint(key[0], key[1], preserve=True)
    store.put(key, b"racing", now, version)
    assert store.get(key, store.version(*key[:2])).body == b"old"


@pytest.mark.parametrize("failed", [None, "body", 302, 401, 403, 404, 422, 500, 503])
def test_warmup_discovers_unvisited_details_and_replays_variants(monkeypatch, failed):
    """Verify warmup discovers unvisited details and replays variants."""
    from types import SimpleNamespace

    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    from api import api as api_module
    from api import uptime

    app = FastAPI()
    requested = []

    @app.get("/{path:path}")
    async def read(path: str, request: Request):
        """Serve deterministic read responses for cache warming integration tests."""
        requested.append((request.url.path, request.url.query))
        if path == "benutzer" and failed is not None:
            if failed == "body":
                return {"success": False, "error": "upstream unavailable"}
            return JSONResponse({"detail": "read rejected"}, status_code=failed)
        if path == "meinunterricht":
            return {"success": True, "courses": [{"book_id": "new-course"}]}
        if path == "kalender/events":
            return {"success": True, "events": [{"id": "new-event"}]}
        return {"success": True}

    async def check():
        """Provide a controlled synthetic portal status."""
        return {"status": "up"}

    async def users():
        """Provide test session identities without accessing real credentials."""
        return [
            {
                "user_id": "user",
                "school_id": "school",
                "username": "name",
                "session_id": "session",
            }
        ]

    async def no_delay(_):
        """Skip pacing delays while exercising the warm-up requests."""

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
                (
                    "user",
                    "/kalender/events",
                    (("category", "special"), ("view_id", "second")),
                ): None,
                ("user", "/nachrichten/private", ()): None,
                ("other", "/kalender/event/other", ()): None,
            }
        ),
    )
    assert asyncio.run(daily_cache.warm_users()) is (failed is None)
    assert ("/meinunterricht/course/new-course", "") in requested
    assert ("/kalender/event/new-event", "") in requested
    assert ("/kalender/event/new-event", "view_id=second") in requested
    assert ("/kalender/events", "category=special&view_id=second") in requested
    assert not any(
        path in {"/nachrichten/private", "/kalender/event/other"}
        for path, _ in requested
    )
    assert daily_cache.warming.get() is False


def test_failed_persistent_replacement_keeps_previous_snapshot(monkeypatch):
    """Verify failed persistent replacement keeps previous snapshot."""
    import pytest

    store = outage_cache.SnapshotStore()
    key = ("user", "/kalender", ())
    version = store.version(*key[:2])
    store.put(key, b"previous", outage_cache._utcnow(), version)

    def fail(*args):
        """Simulate a failure to verify recovery behavior."""
        raise OSError("disk full")

    monkeypatch.setattr(store, "_persist", fail)
    with pytest.raises(OSError):
        store.put(key, b"new", outage_cache._utcnow(), version)
    assert store.get(key, version).body == b"previous"
    assert store.total_bytes == len(b"previous")
    assert store.status("user")["retention_seconds"] == 0


def test_warmup_bypasses_live_cache_without_deleting_it():
    """Verify warmup bypasses live cache without deleting it."""
    from api import api as api_module

    async def scenario():
        """Exercise the asynchronous lifecycle within a single event loop."""
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
    """Verify snapshot writer allows auth transaction to release."""
    import aiosqlite

    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)

    async def scenario():
        """Exercise the asynchronous lifecycle within a single event loop."""
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
    """Verify homework change invalidates live course views and preserves snapshots."""
    from types import SimpleNamespace

    from api import api as api_module

    async def scenario():
        """Exercise the asynchronous lifecycle within a single event loop."""
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
    """Verify restore skips and removes unreadable rows."""
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
        assert db.execute("SELECT COUNT(*) FROM outage_snapshots").fetchone()[0] == 2


def test_flush_ignores_other_users_put_failure_but_reports_own_deletion(
    tmp_path, monkeypatch
):
    """Verify flush ignores other users put failure but reports own deletion."""
    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)

    async def scenario():
        """Exercise the asynchronous lifecycle within a single event loop."""
        store = outage_cache.SnapshotStore()
        store.enable_persistence(str(tmp_path / "auth.db"))
        original = store._write_snapshot

        def fail_put(key, entry=None):
            """Simulate an unrelated failed snapshot write while allowing deletions."""
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
            """Simulate failure of the requested persisted user deletion."""
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
    """Verify logout closes client and clears live cache even if deletion fails."""
    from types import SimpleNamespace

    from api import api as api_module

    async def scenario():
        """Exercise the asynchronous lifecycle within a single event loop."""
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
            """Simulate failed persistence after local client and cache cleanup."""
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


@pytest.mark.parametrize("revoke", ["expire", "logout"])
def test_expiry_deletes_snapshots_only_after_last_session(
    tmp_path, monkeypatch, revoke
):
    """Delete snapshots on final-session expiry or logout, preserving active users."""
    import aiosqlite

    from api import auth_db

    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)

    async def scenario():
        """Exercise the asynchronous lifecycle within a single event loop."""
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
        for token in (expired_token, valid_token):
            if revoke == "logout":
                session = await auth_db.get_refresh_token(token)
                await auth_db.delete_refresh_token_by_session_id(session["session_id"])
            else:
                async with aiosqlite.connect(path) as db:
                    await db.execute(
                        "UPDATE refresh_tokens SET expires_at = ? WHERE token = ?",
                        ("2000-01-01", token),
                    )
                    await db.commit()
                await auth_db._revoke_expired_sessions()
            assert bool(store.entries) is (token == expired_token)
        await store.close()
        restored = outage_cache.SnapshotStore()
        restored.enable_persistence(path)
        assert not restored.entries
        await restored.close()

    asyncio.run(scenario())


def test_retry_budget_returns_to_next_daily_run(monkeypatch):
    """Verify retry budget returns to next daily run."""
    attempts = []
    sleeps = []

    async def fail():
        """Simulate a failure to verify recovery behavior."""
        attempts.append(True)
        return False

    async def sleep(seconds):
        """Record scheduler waits and stop after the next daily scheduling point."""
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


def test_course_warming_does_not_queue_attachment_downloads(monkeypatch, tmp_path):
    """Keep course JSON and file links without scheduling bulk file transfers."""
    from types import SimpleNamespace

    from api import account_data, file_cache
    from api import api as api_module

    async def scenario():
        """Fetch one course with an uncached attachment under the warm-up context."""
        monkeypatch.setattr(file_cache, "FILE_CACHE_DIR", tmp_path)
        manager = api_module.AuthManager()
        monkeypatch.setattr(api_module, "sessions", manager)
        monkeypatch.setattr(api_module, "_course_file_interests", {})
        monkeypatch.setattr(api_module, "is_file_cached", lambda _: False)
        monkeypatch.setattr(api_module, "is_file_pending", lambda _: False)

        async def add_reference(*_):
            """Avoid writing test attachment references to the application database."""

        async def queue(*_):
            """Reject any unsolicited attachment transfer during cache warming."""
            raise AssertionError("Warm-up must not queue downloads")

        monkeypatch.setattr(api_module, "add_cached_file_reference", add_reference)
        monkeypatch.setattr(api_module, "_queue_course_file_download", queue)
        data = {
            "success": True,
            "entries": [
                {"files": [{"download_url": "https://school.invalid/attachment"}]}
            ],
        }
        auth = SimpleNamespace(
            user_id="school:user",
            session_id="session",
            client=SimpleNamespace(meinunterricht_get_course=lambda _: data),
        )
        reset = daily_cache.warming.set(True)
        try:
            result = await api_module.meinunterricht_course("course", auth=auth)
        finally:
            daily_cache.warming.reset(reset)
        assert result["success"]
        assert (
            "/meinunterricht/file/" in result["entries"][0]["files"][0]["download_url"]
        )
        assert manager._cache
        assert not api_module._course_file_interests
        file_hash = result["entries"][0]["files"][0]["file_hash"]
        assert (
            file_cache.get_meta(file_hash)["download_url"]
            == "https://school.invalid/attachment"
        )
        assert not file_cache.get_content_path(file_hash).exists()
        # A normal cached course read must still provide a usable on-demand link.
        await api_module.meinunterricht_course("course", auth=auth)
        downloads = []

        def download(url):
            """Return detached file bytes only after the user opens the attachment."""
            downloads.append(url)
            return {
                "success": True,
                "content": b"attachment",
                "filename": "lesson.txt",
                "content_type": "text/plain",
            }

        auth.client.meinunterricht_download_file = download

        async def identity(*_):
            """Authorize the test user without an external session database."""
            return auth

        async def owns_file(*_):
            """Grant the file reference registered by the synthetic course read."""
            return True

        async def active_account(*_):
            """Keep account-deletion gating inactive for this valid download."""
            return False

        async def client_for_user(*_):
            """Restore the same upstream client for the authenticated file click."""
            return SimpleNamespace(client=auth.client)

        monkeypatch.setattr(api_module, "local_auth_dependency", identity)
        monkeypatch.setattr(api_module, "has_cached_file_reference", owns_file)
        monkeypatch.setattr(account_data, "account_deletion_is_recent", active_account)
        monkeypatch.setattr(
            manager, "_get_or_create_schulportal_client", client_for_user
        )
        response = await api_module.meinunterricht_file(
            file_hash, x_session_token="test-token"
        )
        assert downloads == ["https://school.invalid/attachment"]
        assert file_cache.get_content_path(file_hash).read_bytes() == b"attachment"
        assert str(response.path) == str(file_cache.get_content_path(file_hash))

    asyncio.run(scenario())


@pytest.mark.parametrize("synthetic", [False, True])
def test_only_real_requests_record_activity(monkeypatch, synthetic):
    """Synthetic cache reads must not alter last-seen or user engagement metrics."""
    from types import SimpleNamespace

    from api import api as api_module

    async def scenario():
        """Run the normal client dependency with an already authenticated identity."""
        activity = []
        identity = SimpleNamespace(user_id="school:user", session_id="session")
        session = SimpleNamespace(client=object(), school_id="school", username="user")

        async def restore(_):
            """Supply an upstream session without performing a portal login."""
            return session

        async def record(*args):
            """Capture activity writes so synthetic and interactive reads can be compared."""
            activity.append(args)

        monkeypatch.setattr(
            api_module.sessions, "_get_or_create_schulportal_client", restore
        )
        monkeypatch.setattr(api_module.user_metrics_db, "record_activity", record)
        request = SimpleNamespace(state=SimpleNamespace(lanis_auth=identity))
        reset = daily_cache.warming.set(synthetic)
        try:
            result = await api_module.client_dependency(
                request, x_session_token="test-token"
            )
        finally:
            daily_cache.warming.reset(reset)
        assert result.user_id == "school:user"
        assert activity == ([] if synthetic else [("school", "user")])

    asyncio.run(scenario())


def test_snapshot_byte_quota_is_isolated_across_300_users(monkeypatch):
    """Adding 300 accounts and overflowing one must preserve every other account."""
    monkeypatch.setattr(outage_cache, "MAX_USER_BYTES", 8)
    store = outage_cache.SnapshotStore()
    now = outage_cache._utcnow()
    for user in range(300):
        key = (f"user-{user}", "/kalender", ())
        store.put(key, b"12345678", now, store.version(*key[:2]))
    assert store.total_bytes == 300 * 8
    assert len(store.entries) == 300
    new = ("user-0", "/stundenplan", ())
    store.put(new, b"new-data", now, store.version(*new[:2]))
    assert ("user-0", "/kalender", ()) not in store.entries
    for user in range(1, 300):
        key = (f"user-{user}", "/kalender", ())
        assert store.get(key, store.version(*key[:2])).body == b"12345678"
    assert len(store.entries) == 300
    assert store.status("user-0")["body_bytes"] == 8
    assert store.status("user-0")["max_body_bytes"] == 8
    store.put(new, b"small", now, store.version(*new[:2]))
    assert store.user_bytes["user-0"] == 5
    assert store.total_bytes == 299 * 8 + 5
    # Reject an entry that cannot fit, without destroying the previous good value.
    store.put(new, b"oversized", now, store.version(*new[:2]))
    assert store.get(new, store.version(*new[:2])).body == b"small"
    store.delete_user_data("user-0")
    assert "user-0" not in store.user_bytes
    assert "user-0" not in store.user_entries
    assert store.total_bytes == 299 * 8


def test_per_user_byte_limits_survive_persistence_restore(tmp_path, monkeypatch):
    """Disk restoration applies quotas independently and retains unaffected users."""
    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)
    monkeypatch.setattr(outage_cache, "MAX_USER_BYTES", 8)
    path = str(tmp_path / "auth.db")
    store = outage_cache.SnapshotStore()
    store.enable_persistence(path)
    now = outage_cache._utcnow()
    for user_id in ("a", "b"):
        key = (user_id, "/kalender", ())
        store.put(key, b"12345678", now, store.version(*key[:2]))
    key = ("a", "/stundenplan", ())
    store.put(key, b"1234", now, store.version(*key[:2]))
    asyncio.run(store.close())
    restored = outage_cache.SnapshotStore()
    restored.enable_persistence(path)
    assert restored.user_bytes == {"b": 8, "a": 4}
    assert restored.total_bytes == 12
    assert set(restored.entries) == {("b", "/kalender", ()), ("a", "/stundenplan", ())}
    asyncio.run(restored.close())


@pytest.mark.parametrize("logout", [False, True])
def test_revocation_survives_one_snapshot_deletion_failure(
    tmp_path, monkeypatch, logout
):
    """A failed disk cleanup cannot block logout or another expired account's cleanup."""
    import aiosqlite

    from api import auth_db

    async def scenario():
        """Fail the first deletion after clearing memory, then process the next user."""
        path = str(tmp_path / "auth.db")
        monkeypatch.setattr(auth_db, "DB_PATH", path)
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()
        store = outage_cache.SnapshotStore()
        monkeypatch.setattr(outage_cache, "snapshots", store)
        tokens = []
        for user in ("a", "b"):
            token = await auth_db.store_refresh_token(
                f"school:{user}", "school", user, "password"
            )
            tokens.append(await auth_db.get_refresh_token(token))
            key = (f"school:{user}", "/kalender", ())
            store.put(key, b"{}", outage_cache._utcnow(), store.version(*key[:2]))
        flushed = []

        async def flush(user_id=None):
            """Fail one user's persisted deletion while allowing subsequent users."""
            flushed.append(user_id)
            if len(flushed) == 1:
                raise OSError("disk unavailable")

        monkeypatch.setattr(store, "flush", flush)
        if logout:
            for token in tokens:
                await auth_db.delete_refresh_token_by_session_id(token["session_id"])
        else:
            async with aiosqlite.connect(path) as db:
                await db.execute("UPDATE refresh_tokens SET expires_at = '2000-01-01'")
                await db.commit()
            await auth_db._revoke_expired_sessions()
        assert set(flushed) == {"school:a", "school:b"}
        assert not store.entries
        async with aiosqlite.connect(path) as db:
            assert (
                await (
                    await db.execute("SELECT COUNT(*) FROM refresh_tokens")
                ).fetchone()
            )[0] == 0

    asyncio.run(scenario())


def test_startup_removes_snapshots_without_active_sessions(tmp_path, monkeypatch):
    """Orphaned persisted snapshots must not be resurrected after failed deletion."""
    import sqlite3

    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)
    path = str(tmp_path / "auth.db")
    store = outage_cache.SnapshotStore()
    store.enable_persistence(path)
    for user in ("active", "expired", "logged-out"):
        key = (user, "/kalender", ())
        store.put(key, b"{}", outage_cache._utcnow(), store.version(*key[:2]))
    asyncio.run(store.close())
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE refresh_tokens (user_id TEXT, expires_at TEXT)")
        db.executemany(
            "INSERT INTO refresh_tokens VALUES (?, ?)",
            [("active", "2999-01-01"), ("expired", "2000-01-01")],
        )
    restored = outage_cache.SnapshotStore()
    restored.enable_persistence(path)
    assert list(restored.entries) == [("active", "/kalender", ())]
    asyncio.run(restored.close())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM outage_snapshots").fetchone()[0] == 1


@pytest.mark.parametrize("key_problem", ["wrong", "missing"])
def test_bad_deployment_key_preserves_recoverable_ciphertext(
    tmp_path, monkeypatch, key_problem
):
    """A temporary encryption misconfiguration must not destroy saved responses."""
    import sqlite3

    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)
    path = str(tmp_path / "auth.db")
    key = ("user", "/kalender", ())
    store = outage_cache.SnapshotStore()
    store.enable_persistence(path)
    store.put(key, b'{"saved":true}', outage_cache._utcnow(), store.version(*key[:2]))
    asyncio.run(store.close())
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT * FROM outage_snapshots").fetchall()
    if key_problem == "wrong":
        monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "y" * 32)
    else:
        monkeypatch.delenv("LANIS_CREDENTIAL_ENCRYPTION_KEY")
        monkeypatch.setenv("JWT_SECRET", "")
    unavailable = outage_cache.SnapshotStore()
    unavailable.enable_persistence(path)
    assert not unavailable.entries
    asyncio.run(unavailable.close())
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT * FROM outage_snapshots").fetchall() == original
    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)
    recovered = outage_cache.SnapshotStore()
    recovered.enable_persistence(path)
    assert recovered.get(key, recovered.version(*key[:2])).body == b'{"saved":true}'
    asyncio.run(recovered.close())


@pytest.mark.parametrize("revoke", ["logout", "expiry"])
def test_failed_deletion_then_relogin_cannot_restore_revoked_snapshot(
    tmp_path, monkeypatch, revoke
):
    """A durable cutoff survives deletion failure and a subsequent valid login."""
    import sqlite3

    import aiosqlite

    from api import auth_db

    monkeypatch.setenv("LANIS_CREDENTIAL_ENCRYPTION_KEY", "x" * 32)

    async def scenario():
        """Revoke a user while all snapshot deletes fail, then log in and restart."""
        path = str(tmp_path / "auth.db")
        monkeypatch.setattr(auth_db, "DB_PATH", path)
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()
        token = await auth_db.store_refresh_token(
            "school:user", "school", "user", "password"
        )
        session = await auth_db.get_refresh_token(token)
        store = outage_cache.SnapshotStore()
        store.enable_persistence(path)
        monkeypatch.setattr(outage_cache, "snapshots", store)
        key = ("school:user", "/kalender", ())
        old_time = outage_cache._utcnow()
        store.put(key, b'{"old":true}', old_time, store.version(*key[:2]))
        await store.flush()
        original_write = store._write_snapshot

        def fail_key_deletion(key, entry=None):
            """Leave the old ciphertext on disk while allowing ordinary puts."""
            if entry is None:
                raise OSError("delete failed")
            original_write(key, entry)

        def fail_user_deletion(_):
            """Simulate failure of the final user-scoped persistence deletion."""
            raise OSError("delete failed")

        monkeypatch.setattr(store, "_write_snapshot", fail_key_deletion)
        monkeypatch.setattr(store, "_delete_persisted_user", fail_user_deletion)
        if revoke == "logout":
            await auth_db.delete_refresh_token_by_session_id(session["session_id"])
        else:
            async with aiosqlite.connect(path) as db:
                await db.execute("UPDATE refresh_tokens SET expires_at = '2000-01-01'")
                await db.commit()
            await auth_db._revoke_expired_sessions()
        assert not store.entries
        await auth_db.store_refresh_token("school:user", "school", "user", "password")
        # A delayed write queued before revocation must not bypass its cutoff.
        await asyncio.to_thread(
            original_write, key, outage_cache.Snapshot(b'{"late":true}', old_time)
        )
        with pytest.raises(OSError):
            await store.close()
        with sqlite3.connect(path) as db:
            rows = db.execute("SELECT body FROM outage_snapshots").fetchall()
            assert len(rows) == 1  # Ensure this test really left stale data on disk.
        restored = outage_cache.SnapshotStore()
        restored.enable_persistence(path)
        assert not restored.entries
        # Successful fetches after the cutoff are still persistable.
        restored.put(
            key, b'{"new":true}', outage_cache._utcnow(), restored.version(*key[:2])
        )
        await restored.close()
        restarted = outage_cache.SnapshotStore()
        restarted.enable_persistence(path)
        assert restarted.get(key, restarted.version(*key[:2])).body == b'{"new":true}'
        await restarted.close()

    asyncio.run(scenario())
