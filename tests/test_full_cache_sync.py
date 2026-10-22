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


def test_files_and_message_bodies_are_discovered_without_external_link_crawling():
    assert discover_metadata(
        "/nachrichten/headers",
        (),
        {"success": True, "conversations": [{"id": "message"}]},
    ) == {("/nachrichten/message", ())}
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


def test_message_warmup_body_read_preserves_flags_until_user_opens(state, monkeypatch):
    """Exercise normal routes while sync's upstream read changes the portal flag."""
    app = FastAPI()
    app.router.route_class = outage_cache.OutageCacheRoute
    unread = True

    def headers(*_):
        return {
            "success": True,
            "conversations": [{"id": "c", "unread": int(unread), "date": "first"}],
        }

    def conversation(*_):
        nonlocal unread
        unread = False
        return {"success": True, "messages": [{"content": "saved body"}]}

    auth = SimpleNamespace(
        user_id="a",
        session_id="a",
        client=SimpleNamespace(
            nachrichten_get_headers=headers, nachrichten_get_conversation=conversation
        ),
    )
    app.dependency_overrides[api_module.client_dependency] = lambda: auth
    app.add_api_route("/nachrichten/headers", api_module.get_message_headers)
    app.add_api_route("/nachrichten/{conversation_id}", api_module.get_conversation)
    monkeypatch.setattr(api_module, "sessions", api_module.AuthManager())
    monkeypatch.setattr(outage_cache, "snapshots", outage_cache.SnapshotStore())

    async def no_task(_):
        pass

    monkeypatch.setattr(api_module.task_queue, "add_task", no_task)

    async def scenario():
        reset = warming.set(True)
        try:
            await asgi_request(
                app, "GET", "/nachrichten/headers", {"X-Session-Token": "a"}
            )
            body = await asgi_request(
                app, "GET", "/nachrichten/c", {"X-Session-Token": "a"}
            )
            assert body.json()["messages"][0]["content"] == "saved body"
        finally:
            warming.reset(reset)
        assert not unread  # Upstream changed, while LANIS must still report unread.
        result = await asgi_request(
            app, "GET", "/nachrichten/headers", {"X-Session-Token": "a"}
        )
        assert result.json()["conversations"][0]["unread"] == 1
        await asgi_request(app, "GET", "/nachrichten/c", {"X-Session-Token": "a"})
        result = await asgi_request(
            app, "GET", "/nachrichten/headers", {"X-Session-Token": "a"}
        )
        assert result.json()["conversations"][0]["unread"] == 0

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
