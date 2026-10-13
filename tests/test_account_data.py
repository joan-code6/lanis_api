import asyncio
import json

from api import account_data, auth_db
from api.admin import router as admin_router
from api.metrics.user_metrics_db import UserMetricsDB


def test_account_export_excludes_authentication_secrets(tmp_path, monkeypatch):
    async def scenario():
        auth_path = tmp_path / "auth.db"
        metrics_path = tmp_path / "metrics.db"
        monkeypatch.setattr(auth_db, "DB_PATH", str(auth_path))
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()
        metrics = UserMetricsDB(metrics_path)
        await metrics.initialize()
        monkeypatch.setattr(account_data, "user_metrics_db", metrics)

        token = await auth_db.store_refresh_token(
            "5201:student", "5201", "student", "secret-password"
        )
        await auth_db.save_notification_preferences("5201:student", {"enabled": True})
        await metrics.upsert_user("5201", "student", {"name": "Student"})

        exported = await account_data.build_account_export("5201:student")
        serialized = json.dumps(exported)

        assert exported["profile"] == {"name": "Student"}
        assert isinstance(exported["preferences"], dict)
        assert exported["notification_preferences"]["enabled"] is True
        assert "secret-password" not in serialized
        assert token not in serialized
        assert "refresh tokens" in exported["excluded_secrets"]

    asyncio.run(scenario())


def test_account_deletion_removes_auth_and_metrics_data(tmp_path, monkeypatch):
    async def scenario():
        auth_path = tmp_path / "auth.db"
        metrics_path = tmp_path / "metrics.db"
        monkeypatch.setattr(auth_db, "DB_PATH", str(auth_path))
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()
        metrics = UserMetricsDB(metrics_path)
        await metrics.initialize()
        monkeypatch.setattr(account_data, "user_metrics_db", metrics)

        token = await auth_db.store_refresh_token(
            "5201:student", "5201", "student", "secret-password"
        )
        await metrics.upsert_user("5201", "student", {"name": "Student"})

        report = await account_data.delete_account_data("5201:student")

        assert report.success is True
        assert await auth_db.get_refresh_token(token) is None
        assert await metrics.get_user("5201", "student") is None

    asyncio.run(scenario())


def test_deletion_purges_orphaned_course_files_but_keeps_shared_files(tmp_path, monkeypatch):
    from api import file_cache

    async def scenario():
        monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        monkeypatch.setattr(file_cache, "FILE_CACHE_DIR", tmp_path / "files")
        await auth_db.initialize()
        file_hash = "a" * 64
        file_cache.save_file(file_hash, b"private attachment", "text/plain", "file.txt")
        await auth_db.add_cached_file_reference("5201:a", file_hash)
        await auth_db.add_cached_file_reference("5201:b", file_hash)

        await auth_db.delete_user_data("5201:a")
        assert file_cache.is_file_cached(file_hash)
        await auth_db.delete_user_data("5201:b")
        assert not file_cache.is_file_cached(file_hash)

    asyncio.run(scenario())


def test_account_deletion_uses_authenticated_identity_after_logout_race(
    tmp_path, monkeypatch
):
    async def scenario():
        monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()
        metrics = UserMetricsDB(tmp_path / "metrics.db")
        await metrics.initialize()
        monkeypatch.setattr(account_data, "user_metrics_db", metrics)

        await auth_db.store_refresh_token(
            "5201:student", "5201", "student", "secret-password"
        )
        await auth_db.save_notification_preferences(
            "5201:student", {"enabled": True}
        )
        await metrics.upsert_user("5201", "student", {"name": "Student"})
        await auth_db.delete_user_tokens("5201:student")

        report = await account_data.delete_account_data(
            "5201:student", school_id="5201", username="student"
        )

        assert report.success
        assert (
            await auth_db.get_notification_preferences("5201:student")
        )["enabled"] is False
        assert await metrics.get_user("5201", "student") is None

    asyncio.run(scenario())


def test_deletion_marker_survives_auth_database_reinitialization(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
        monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
        await auth_db.initialize()
        await auth_db.delete_user_data("5201:student")

        assert await auth_db.account_deletion_marker_is_active("5201:student")
        await auth_db.initialize()
        assert await auth_db.account_deletion_marker_is_active("5201:student")

        await auth_db.clear_account_deletion_marker("5201:student")
        assert not await auth_db.account_deletion_marker_is_active("5201:student")

    asyncio.run(scenario())


def test_course_file_task_stops_before_starting_after_deletion(monkeypatch):
    from api import api as api_module

    async def scenario():
        async def get_lock(_user_id):
            return account_data.AccountLifecycleLock()

        async def deleted(_user_id):
            return True

        async def current_generation(_user_id, _generation):
            return True

        def should_not_fetch(*_args, **_kwargs):
            raise AssertionError("deleted account task must not fetch files")

        pending = []
        monkeypatch.setattr(account_data, "account_lifecycle_lock", get_lock)
        monkeypatch.setattr(account_data, "account_deletion_is_recent", deleted)
        monkeypatch.setattr(
            api_module.task_queue,
            "is_user_generation_current",
            current_generation,
        )
        monkeypatch.setattr(api_module, "is_file_cached", lambda _file_hash: False)
        monkeypatch.setattr(api_module, "unmark_pending", pending.append)
        monkeypatch.setattr(api_module, "write_pending_meta", should_not_fetch)
        await api_module._download_course_file(
            "5201:student", "https://sph.invalid/file", "file-hash"
        )
        assert pending == ["file-hash"]

    asyncio.run(scenario())


def test_admin_cannot_reveal_credentials_or_request_step_up_tokens():
    paths = {route.path for route in admin_router.routes}
    assert "/admin/users/{user_id:path}/credentials/reveal" not in paths
    assert "/admin/auth/step-up" not in paths


def test_deletion_anonymizes_legacy_admin_actor_and_removes_target_rows(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
        monkeypatch.setattr(auth_db, '_lock', asyncio.Lock())
        await auth_db.initialize()
        await auth_db.record_admin_action('5201:admin', 'user_view', '5201:student')
        await auth_db.record_admin_action('5201:admin', 'admin_login')
        await auth_db.record_admin_action('5201:other', 'user_view', '5201:admin')
        await auth_db.record_admin_action('5201:admin', 'self_view', '5201:admin')
        await auth_db.record_admin_action('5201:other', 'school_view', '5201:student')
        before = await auth_db.get_admin_audit()

        counts = await auth_db.delete_user_data('5201:admin')

        after = await auth_db.get_admin_audit()
        assert counts['admin_audit'] == 2
        assert counts['admin_audit_anonymized'] == 2
        assert len(after) == 3
        assert all(row['actor_user_id'] != '5201:admin' and row['target_user_id'] != '5201:admin' for row in after)
        for row in after:
            original = next(item for item in before if item['action'] == row['action'] and item['target_user_id'] == row['target_user_id'])
            assert row['occurred_at'] == original['occurred_at']
            assert row['actor_user_id'] == ('' if original['actor_user_id'] == '5201:admin' else original['actor_user_id'])
        repeated = await auth_db.delete_user_data('5201:admin')
        assert repeated['admin_audit'] == repeated['admin_audit_anonymized'] == 0
        assert await auth_db.get_admin_audit() == after

    asyncio.run(scenario())


def test_shared_download_never_requeues_a_deleted_interest_after_relogin(tmp_path, monkeypatch):
    from api import api as api_module
    from api.queue.task_queue import TaskQueue

    async def scenario():
        monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
        monkeypatch.setattr(auth_db, '_lock', asyncio.Lock())
        await auth_db.initialize()
        queue = TaskQueue()
        monkeypatch.setattr(api_module, 'task_queue', queue)
        file_hash = 'b' * 64
        revoked_refresh = await auth_db.store_refresh_token('5201:deleted', '5201', 'deleted', 'test-password')
        revoked = await auth_db.get_refresh_token(revoked_refresh)
        live_refresh = await auth_db.store_refresh_token('5201:live', '5201', 'live', 'test-password')
        live = await auth_db.get_refresh_token(live_refresh)
        await auth_db.add_cached_file_reference('5201:deleted', file_hash)
        await auth_db.add_cached_file_reference('5201:live', file_hash)
        interests = {file_hash: {
            '5201:owner': ('https://sph.invalid/owner', 'owner-session'),
            '5201:deleted': ('https://sph.invalid/deleted', revoked['session_id']),
            '5201:live': ('https://sph.invalid/live', live['session_id']),
        }}
        monkeypatch.setattr(api_module, '_course_file_interests', interests)
        await auth_db.delete_user_data('5201:deleted')
        # Cover an old entry left before withdrawal was added, including a fresh
        # login that removes the tombstone and has an unrelated session ID.
        await auth_db.store_refresh_token('5201:deleted', '5201', 'deleted', 'test-password')
        await auth_db.clear_account_deletion_marker('5201:deleted')
        await api_module._reschedule_shared_course_file(file_hash, '5201:owner')
        task = queue._queue.get_nowait()
        queue._queue.task_done()
        assert task.user_id == '5201:live'
        assert '5201:deleted' not in interests[file_hash]
        # The chosen account can be deleted after enqueueing, too. A new login
        # must not make this original download eligible to fetch private data.
        await auth_db.delete_user_tokens('5201:live')
        await auth_db.store_refresh_token('5201:live', '5201', 'live', 'test-password')
        await auth_db.clear_account_deletion_marker('5201:live')
        monkeypatch.setattr(api_module, 'is_file_cached', lambda _hash: False)
        def should_not_fetch(*args):
            raise AssertionError('A revoked download session must never fetch attachments')
        monkeypatch.setattr(api_module, 'write_pending_meta', should_not_fetch)
        await task.func(*task.args, **task.kwargs)
        assert queue._queue.empty()

    asyncio.run(scenario())


def test_deletion_withdraws_non_owning_download_interests(monkeypatch):
    from api import api as api_module
    async def scenario():
        interests = {'file': {'5201:deleted': ('url-a', 'session-a'), '5201:live': ('url-b', 'session-b')}}
        monkeypatch.setattr(api_module, '_course_file_interests', interests)
        await api_module.clear_course_file_interests('5201:deleted')
        assert interests == {'file': {'5201:live': ('url-b', 'session-b')}}
    asyncio.run(scenario())


def test_deletion_waits_for_tracked_revalidation_and_purges_queued_clients(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace
    from api import api as api_module
    from api.queue.task_queue import TaskQueue
    async def scenario():
        monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
        monkeypatch.setattr(auth_db, '_lock', asyncio.Lock())
        await auth_db.initialize()
        refresh = await auth_db.store_refresh_token('5201:student', '5201', 'student', 'test-password')
        stored = await auth_db.get_refresh_token(refresh)
        metrics = UserMetricsDB(tmp_path / 'metrics.db')
        await metrics.initialize()
        monkeypatch.setattr(account_data, 'user_metrics_db', metrics)
        queue = TaskQueue(max_concurrent=1)
        monkeypatch.setattr(api_module, 'task_queue', queue)
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        class Client:
            def get_apps(self):
                started.set()
                assert release.wait(timeout=3)
                completed.set()
                return {'success': True, 'apps': ['new']}
            def benutzer_get_data(self):
                raise AssertionError('Queued work must be removed before fetching')
        client = Client()
        class Sessions:
            def __init__(self): self.cache = {'success': True, 'apps': ['old']}
            async def _get_or_create_schulportal_client(self, user_id):
                return SimpleNamespace(client=client)
            async def get_cached(self, *args): return self.cache
            async def set_cache(self, user_id, endpoint, data, **kwargs): self.cache = data
            async def delete_user_runtime_data(self, user_id):
                assert completed.is_set(), 'Deletion must wait for the upstream thread to finish'
                self.cache = None
                return {'cache': 1}
        session_manager = Sessions()
        monkeypatch.setattr(api_module, 'sessions', session_manager)
        auth = api_module.AuthSession(client, '5201:student', '5201', 'student', stored['session_id'])
        await api_module._schedule_account_revalidation(auth, '/apps')
        queued = next(iter(queue._queue._queue))
        assert all(arg is not client for arg in queued.args)
        await queue.start()
        deletion = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            await api_module._schedule_account_revalidation(auth, '/benutzer')
            waiting = next(iter(queue._queue._queue))
            deletion = asyncio.create_task(account_data.delete_account_data('5201:student'))
            await asyncio.sleep(0)
            assert not deletion.done()
            release.set()
            report = await asyncio.wait_for(deletion, timeout=3)
            assert report.success
            await asyncio.wait_for(queue._queue.join(), timeout=2)
            assert session_manager.cache is None
            assert waiting.args == () and waiting.func is None
            assert queue._completed_tasks == {}
        finally:
            release.set()
            if deletion is not None and not deletion.done(): await deletion
            await queue.stop(wait=False)
    asyncio.run(scenario())
