import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse

from api import api as api_module
from api import auth_db, file_cache, message_read_state, outage_cache
from api.daily_cache import discover_files, discover_metadata, warming
from tests.test_outage_cache import asgi_request


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Isolate message flags, file mappings and private file bytes."""
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))
    monkeypatch.setattr(auth_db, "_lock", asyncio.Lock())
    monkeypatch.setattr(file_cache, "FILE_CACHE_DIR", tmp_path / "files")
    asyncio.run(auth_db.initialize())

    async def auth(token):
        if token not in {"a", "b"}:
            raise HTTPException(401)
        return SimpleNamespace(user_id=token, session_id=token)

    monkeypatch.setattr(api_module, "local_auth_dependency", auth)
    return tmp_path


def test_sync_preserves_unread_until_interactive_open_and_new_activity(state):
    async def scenario():
        original = {
            "success": True,
            "conversations": [{"id": "conversation", "unread": 1, "date": "first"}],
        }
        await message_read_state.overlay_headers("a", original, preserve=True)
        upstream_read = {
            "success": True,
            "conversations": [{"id": "conversation", "unread": 0, "date": "first"}],
        }
        assert (await message_read_state.overlay_headers("a", upstream_read))[
            "conversations"
        ][0]["unread"] == 1
        assert (await message_read_state.overlay_headers("b", upstream_read))[
            "conversations"
        ][0]["unread"] == 0
        # Repeating background sync never marks the preserved flag read.
        await message_read_state.overlay_headers("a", upstream_read, preserve=True)
        await message_read_state.mark_opened("a", "conversation")
        assert (await message_read_state.overlay_headers("a", original))[
            "conversations"
        ][0]["unread"] == 0
        new = {
            "success": True,
            "conversations": [{"id": "conversation", "unread": 1, "date": "second"}],
        }
        assert (await message_read_state.overlay_headers("a", new, preserve=True))[
            "conversations"
        ][0]["unread"] == 1
        await message_read_state.mark_opened("a", "conversation")
        assert (await message_read_state.overlay_headers("a", new))["conversations"][0][
            "unread"
        ] == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("opened_before_new_activity", [False, True])
def test_opening_new_activity_clears_cached_unread_after_restart(
    state, opened_before_new_activity
):
    async def scenario():
        original = {
            "success": True,
            "conversations": [{"id": "conversation", "unread": 1, "date": "nightly"}],
        }
        await message_read_state.overlay_headers("a", original, preserve=True)
        if opened_before_new_activity:
            await message_read_state.mark_opened("a", "conversation")
        current = {
            "success": True,
            "conversations": [
                {"id": "conversation", "unread": 1, "date": "new-message"}
            ],
        }
        # An ordinary header read observes activity arriving after the sync.
        headers = await message_read_state.overlay_headers("a", current)
        assert headers["conversations"][0]["unread"] == 1
        await message_read_state.mark_opened("a", "conversation")
        return current

    cached = asyncio.run(scenario())
    # Separate event loops and database connections also exercise persisted state.
    for _ in range(2):
        headers = asyncio.run(message_read_state.overlay_headers("a", cached))
        assert headers["conversations"][0]["unread"] == 0
        assert headers["conversations"][0]["read"] is True
    later = {
        "success": True,
        "conversations": [{"id": "conversation", "unread": 1, "date": "later-message"}],
    }
    assert (
        asyncio.run(message_read_state.overlay_headers("a", later))["conversations"][0][
            "unread"
        ]
        == 1
    )


def test_live_read_does_not_acknowledge_headers_observed_during_fetch(state):
    async def scenario():
        def headers(activity):
            return {
                "success": True,
                "conversations": [{"id": "c", "unread": 1, "date": activity}],
            }

        await message_read_state.overlay_headers("a", headers("first"), preserve=True)
        activity = await message_read_state.current_activity("a", "c")
        await message_read_state.overlay_headers("a", headers("second"))
        await message_read_state.mark_opened("a", "c", expected_activity=activity)
        assert (await message_read_state.overlay_headers("a", headers("second")))[
            "conversations"
        ][0]["unread"] == 1

    asyncio.run(scenario())


def test_old_cached_headers_cannot_restore_unread_or_replace_activity(state):
    async def scenario():
        def headers(activity):
            return {
                "success": True,
                "conversations": [{"id": "c", "date": activity, "unread": 1}],
            }

        old = headers("nightly")
        latest = headers("new message")
        await message_read_state.overlay_headers("a", old, preserve=True)
        await message_read_state.overlay_headers("a", latest)
        activity = await message_read_state.current_activity("a", "c")
        await message_read_state.mark_opened("a", "c", expected_activity=activity)
        for _ in range(2):
            cached = await message_read_state.overlay_headers("a", old, fresh=False)
            assert cached["conversations"][0]["unread"] == 0
            assert await message_read_state.current_activity("a", "c") == activity
        assert (await message_read_state.overlay_headers("a", latest))["conversations"][
            0
        ]["unread"] == 0
        newer = headers("next message")
        assert (await message_read_state.overlay_headers("a", newer))["conversations"][
            0
        ]["unread"] == 1
        # An older read header must not hide a newly observed unread message either.
        old["conversations"][0]["unread"] = 0
        assert (await message_read_state.overlay_headers("a", old, fresh=False))[
            "conversations"
        ][0]["unread"] == 1

    asyncio.run(scenario())


def test_reply_arriving_inside_body_fetch_is_preserved_without_header_change(state):
    async def scenario():
        headers = {"success": True, "conversations": [{"id": "c", "unread": 0}]}
        old = {"success": True, "messages": [{"content": "opened"}]}
        await message_read_state.record_body(
            "a", "c", old, background=False, activity=None
        )
        await message_read_state.record_body(
            "a", "c", old, background=True, activity=None
        )
        assert (await message_read_state.overlay_headers("a", headers))[
            "conversations"
        ][0]["unread"] == 0
        # Headers still say read, but upstream includes a reply that arrived
        # after those headers and marks it read while returning its content.
        new = {
            "success": True,
            "messages": [{"content": "opened"}, {"content": "new reply"}],
        }
        await message_read_state.record_body(
            "a", "c", new, background=True, activity=None
        )
        assert (await message_read_state.overlay_headers("a", headers))[
            "conversations"
        ][0]["unread"] == 1
        activity = await message_read_state.current_activity("a", "c")
        await message_read_state.record_body(
            "a", "c", new, background=False, activity=activity
        )
        await message_read_state.mark_opened("a", "c", expected_activity=activity)
        await message_read_state.record_body(
            "a", "c", new, background=True, activity=activity
        )
        assert (await message_read_state.overlay_headers("a", headers))[
            "conversations"
        ][0]["unread"] == 0

    asyncio.run(scenario())


def test_explicit_mark_read_acknowledges_synchronized_body(state):
    async def scenario():
        headers = {"success": True, "conversations": [{"id": "c", "unread": 1}]}
        body = {"success": True, "messages": [{"content": "synchronized"}]}
        await message_read_state.overlay_headers("a", headers, preserve=True)
        await message_read_state.record_body(
            "a", "c", body, background=True, activity=None
        )
        auth = SimpleNamespace(
            user_id="a",
            client=SimpleNamespace(nachrichten_mark_read=lambda _: {"success": True}),
        )
        assert (await api_module.mark_read("c", auth=auth))["success"]
        for _ in range(2):
            await message_read_state.record_body(
                "a", "c", body, background=True, activity=None
            )
            assert (await message_read_state.overlay_headers("a", headers))[
                "conversations"
            ][0]["unread"] == 0
        changed = {
            "success": True,
            "messages": [{"content": "synchronized"}, {"content": "reply"}],
        }
        await message_read_state.record_body(
            "a", "c", changed, background=True, activity=None
        )
        assert (await message_read_state.overlay_headers("a", headers))[
            "conversations"
        ][0]["unread"] == 1

    asyncio.run(scenario())


def test_successful_sync_recovers_despite_lifetime_evictions(state, monkeypatch):
    store = outage_cache.SnapshotStore()
    store.user_evictions["a"] = 3
    monkeypatch.setattr(api_module, "snapshots", store)

    async def scenario():
        auth = SimpleNamespace(user_id="a")
        await auth_db.save_cache_sync_state("a", 10, 0, 1)
        assert not (await api_module.cache_status(auth))["sync"]["complete"]
        await auth_db.save_cache_sync_state("a", 10, 0, 0)
        result = await api_module.cache_status(auth)
        assert result["sync"]["complete"] is True
        assert result["quota_evictions"] == 3

    asyncio.run(scenario())


def test_sync_completion_cannot_recreate_deleted_account_data(state):
    async def scenario():
        token = await auth_db.store_refresh_token("5201:a", "5201", "a", "unused")
        session = await auth_db.get_refresh_token(token)
        await auth_db.save_cache_sync_state(
            "5201:a", 3, 0, 0, session_id=session["session_id"]
        )
        assert await auth_db.get_cache_sync_state("5201:a") is not None
        await auth_db.delete_user_data("5201:a")
        # A delayed completion from the selected session must not resurrect data.
        await auth_db.save_cache_sync_state(
            "5201:a", 3, 1, 0, session_id=session["session_id"]
        )
        assert await auth_db.get_cache_sync_state("5201:a") is None
        await auth_db.store_refresh_token("5201:a", "5201", "a", "unused")
        await auth_db.save_cache_sync_state(
            "5201:a", 3, 1, 0, session_id=session["session_id"]
        )
        assert await auth_db.get_cache_sync_state("5201:a") is None

    asyncio.run(scenario())


def test_outage_dashboard_uses_current_local_acknowledgments(state, monkeypatch):
    app = FastAPI()
    app.router.route_class = outage_cache.OutageCacheRoute
    online = True
    store = outage_cache.SnapshotStore()
    monkeypatch.setattr(outage_cache, "snapshots", store)

    @app.get("/dashboard/notifications")
    async def inbox():
        if not online:
            outage_cache.observation.get().unavailable = True
            return {"success": False}
        return await api_module.saved_dashboard_notification_inbox("a")

    async def scenario():
        nonlocal online
        item = {"id": "message-1", "source": "messages", "title": "New message"}
        await auth_db.sync_dashboard_notifications("a", [item])
        initial = await asgi_request(
            app, "GET", "/dashboard/notifications", {"X-Session-Token": "a"}
        )
        assert initial.json()["unread_count"] == 1
        await auth_db.mark_dashboard_notifications_read("a", ["message-1"])
        online = False
        stale = await asgi_request(
            app, "GET", "/dashboard/notifications", {"X-Session-Token": "a"}
        )
        assert stale.headers["x-lanis-cache"] == "stale"
        assert stale.json()["unread_count"] == 0
        assert stale.json()["notifications"] == []
        # Direct helper covers the query variant even without a prior snapshot.
        assert (
            await api_module.saved_dashboard_notification_inbox("a", show_read="true")
        )["notifications"][0]["read"] is True

    asyncio.run(scenario())


def test_binary_files_are_content_deduplicated_but_privately_authorized(state):
    app = FastAPI()
    app.router.route_class = outage_cache.OutageCacheRoute
    calls = []
    online = True

    @app.get("/dateispeicher/file/{file_id}")
    async def download(file_id: int):
        calls.append(file_id)
        if not online:
            raise HTTPException(503)
        return Response(
            b"same-file",
            media_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="lesson.pdf"'},
        )

    @app.get("/dateiverteilung/file")
    async def distribution(url: str):
        async def chunks():
            yield b"same-"
            yield b"file"

        return StreamingResponse(chunks(), media_type="application/pdf")

    async def scenario():
        nonlocal online
        first = await asgi_request(
            app, "GET", "/dateispeicher/file/1", {"X-Session-Token": "a"}
        )
        assert first.status_code == 200 and first.body == b"same-file"
        second = await asgi_request(
            app, "GET", "/dateiverteilung/file?url=test", {"X-Session-Token": "b"}
        )
        assert second.status_code == 200 and second.body == first.body
        files = list(file_cache.FILE_CACHE_DIR.glob("*"))
        assert len([path for path in files if len(path.name) == 64]) == 1
        online = False
        cached = await asgi_request(
            app, "GET", "/dateispeicher/file/1", {"X-Session-Token": "a"}
        )
        assert cached.status_code == 200 and cached.body == first.body
        assert cached.headers["x-lanis-cache"] == "hit"
        denied = await asgi_request(
            app, "GET", "/dateispeicher/file/1", {"X-Session-Token": "b"}
        )
        assert denied.status_code == 503
        invalid = await asgi_request(
            app, "GET", "/dateispeicher/file/1", {"X-Session-Token": "invalid"}
        )
        assert invalid.status_code == 401
        assert calls == [1, 1]

    asyncio.run(scenario())


def test_files_are_discovered_but_message_bodies_are_not():
    assert discover_metadata(
        "/nachrichten/headers",
        (),
        {"success": True, "conversations": [{"id": "message"}]},
    ) == set()
    assert discover_files(
        "/dateispeicher", {"success": True, "files": [{"id": 1}]}
    ) == {("/dateispeicher/file/1", ())}
    assert discover_files(
        "/dateiverteilung",
        {
            "success": True,
            "distributions": [
                {
                    "files": [
                        {
                            "download_url": "https://start.schulportal.hessen.de/dateiverteilung.php?a=download"
                        }
                    ]
                }
            ],
        },
    ) == {
        (
            "/dateiverteilung/file",
            (
                (
                    "url",
                    "https://start.schulportal.hessen.de/dateiverteilung.php?a=download",
                ),
            ),
        )
    }
    assert discover_files(
        "/meinunterricht/course/c",
        {
            "success": True,
            "entries": [
                {
                    "files": [
                        {"download_url": "https://api.invalid/meinunterricht/file/abc"},
                        {"download_url": "https://external.invalid/video"},
                    ]
                }
            ],
        },
    ) == {("/meinunterricht/file/abc", ())}
    assert discover_files(
        "/meinunterricht/submissions/d",
        {
            "success": True,
            "submission": {
                "own_files": [{"download_ref": "mine"}],
                "public_files": [{"download_ref": "teacher"}],
            },
        },
    ) == {
        ("/meinunterricht/submissions/file/mine", ()),
        ("/meinunterricht/submissions/file/teacher", ()),
    }


def test_background_conversation_reads_never_touch_portal(state, monkeypatch):
    calls = []

    def conversation(*_):
        calls.append("body")
        return {"success": True, "messages": [{"content": "opened interactively"}]}

    auth = SimpleNamespace(
        user_id="a",
        session_id="a",
        client=SimpleNamespace(nachrichten_get_conversation=conversation),
    )
    monkeypatch.setattr(api_module, "sessions", api_module.AuthManager())

    async def scenario():
        reset = warming.set(True)
        try:
            result = await api_module.get_conversation("c", auth=auth)
            assert result["success"] is False
            assert calls == []
        finally:
            warming.reset(reset)
        result = await api_module.get_conversation("c", auth=auth)
        assert result["messages"][0]["content"] == "opened interactively"
        assert calls == ["body"]

    asyncio.run(scenario())


def test_file_refresh_replaces_mapping_and_releases_obsolete_reference(state):
    """Daily reads refresh files while keeping only currently mapped user references."""
    app = FastAPI()
    app.router.route_class = outage_cache.OutageCacheRoute
    payload = b"first"

    @app.get("/dateispeicher/file/{file_id}")
    async def download(file_id: int):
        return Response(payload, media_type="application/pdf")

    async def scenario():
        nonlocal payload
        await asgi_request(
            app, "GET", "/dateispeicher/file/1", {"X-Session-Token": "a"}
        )
        before = await auth_db.get_cached_file_references("a")
        payload = b"second"
        reset = warming.set(True)
        try:
            refreshed = await asgi_request(
                app, "GET", "/dateispeicher/file/1", {"X-Session-Token": "a"}
            )
        finally:
            warming.reset(reset)
        assert refreshed.body == b"second"
        after = await auth_db.get_cached_file_references("a")
        assert len(after) == 1 and after != before
        assert (
            await asgi_request(
                app, "GET", "/dateispeicher/file/1", {"X-Session-Token": "a"}
            )
        ).body == b"second"

    asyncio.run(scenario())


def test_oversized_binary_response_leaves_no_mapping_or_partial_file(
    state, monkeypatch
):
    """Reject an over-limit stream without making partial data available offline."""
    from api import download_cache

    monkeypatch.setattr(download_cache, "MAX_DOWNLOAD_BYTES", 4)
    app = FastAPI()
    app.router.route_class = outage_cache.OutageCacheRoute

    @app.get("/dateispeicher/file/{file_id}")
    async def download(file_id: int):
        return Response(b"too large")

    async def scenario():
        response = await asgi_request(
            app, "GET", "/dateispeicher/file/1", {"X-Session-Token": "a"}
        )
        assert response.status_code == 413
        assert not await auth_db.get_cached_file_references("a")
        assert not list(file_cache.FILE_CACHE_DIR.iterdir())

    asyncio.run(scenario())


def test_file_cleanup_preserves_active_references(state):
    """Age-based cleanup must not destroy a referenced offline file."""
    import os

    file_cache.save_file("a" * 64, b"keep", "text/plain", "a.txt")
    file_cache.save_file("b" * 64, b"remove", "text/plain", "b.txt")
    for path in file_cache.FILE_CACHE_DIR.iterdir():
        os.utime(path, (0, 0))
    file_cache.purge_expired_files({"a" * 64})
    assert file_cache.get_content_path("a" * 64).read_bytes() == b"keep"
    assert not file_cache.get_content_path("b" * 64).exists()


def test_sync_coverage_state_is_private_and_reports_incomplete_attempts(state):
    """Failures and quota evictions must be distinguishable from a complete sync."""

    async def scenario():
        await auth_db.save_cache_sync_state("a", 12, 0, 0)
        assert (await auth_db.get_cache_sync_state("a"))["complete"]
        assert await auth_db.get_cache_sync_state("b") is None
        await auth_db.save_cache_sync_state("a", 12, 1, 0)
        assert not (await auth_db.get_cache_sync_state("a"))["complete"]
        await auth_db.save_cache_sync_state("a", 12, 0, 1)
        assert not (await auth_db.get_cache_sync_state("a"))["complete"]

    asyncio.run(scenario())
