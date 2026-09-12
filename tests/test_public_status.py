import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from api import public_status, uptime
from api.metrics.user_metrics_db import UserMetricsDB


@pytest.fixture
def monitor(tmp_path, monkeypatch):
    db = UserMetricsDB(tmp_path / "metrics.db")
    monkeypatch.setattr(uptime, "user_metrics_db", db)
    monkeypatch.setattr(uptime, "uptime_is_configured", lambda: True)
    monkeypatch.setattr(uptime, "get_uptime_interval_seconds", lambda: 300)
    monkeypatch.setattr(public_status, "_cached_status", None)
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    monkeypatch.setattr(uptime, "_utcnow", lambda: now)
    return db, now


async def record(db, at, status="up", **extra):
    await db.record_uptime_check(
        {
            "checked_at": at.isoformat(),
            "status": status,
            "is_available": status == "up",
            **extra,
        }
    )


def test_empty_monitor_and_unobserved_days_are_unknown(monitor):
    result = asyncio.run(public_status.get_public_status())
    assert result["current"]["status"] == "unknown"
    assert result["current"]["stale"] is True
    assert result["summary"]["uptime_percent"] is None
    assert result["summary"]["coverage_percent"] == 0
    assert all(day["status"] == "unknown" for day in result["daily"])
    assert not result["history"] and not result["incidents"]


def test_public_projection_excludes_private_fields_and_preserves_partial_failure(
    monitor,
):
    db, now = monitor

    async def scenario():
        await record(
            db,
            now,
            "degraded",
            url="private-url",
            error="private-error",
            features=[
                {"name": "login", "status": "up", "error": "secret"},
                {"name": "modules", "status": "down", "module_count": 123},
                {"name": "private-name", "status": "up"},
            ],
        )
        result = await public_status.get_public_status()
        assert result["current"]["status"] == "degraded"
        assert result["current"]["features"] == [
            {"name": "login", "status": "up"},
            {"name": "modules", "status": "down"},
        ]
        assert result["daily"][-1]["status"] == "degraded"
        assert result["summary"]["uptime_percent"] == 0
        assert result["incidents"][0]["checked_at"].endswith("Z")
        encoded = json.dumps(result)
        for secret in (
            "private-url",
            "private-error",
            "secret",
            "private-name",
            "module_count",
        ):
            assert secret not in encoded

    asyncio.run(scenario())


def test_stale_and_disabled_current_do_not_rewrite_history(monitor, monkeypatch):
    db, now = monitor

    async def scenario():
        await record(db, now - timedelta(seconds=601))
        result = await public_status.get_public_status()
        assert result["current"]["status"] == "unknown"
        assert result["current"]["stale"] is True
        assert result["history"][0]["status"] == "up"
        assert result["summary"]["uptime_percent"] == 100
        await record(db, now)
        monkeypatch.setattr(uptime, "uptime_is_configured", lambda: False)
        result = await public_status.get_public_status()
        assert result["current"]["status"] == "unknown"
        assert result["current"]["stale"] is False

    asyncio.run(scenario())


def test_unknown_results_and_duplicate_checks_do_not_inflate_coverage(monitor):
    db, now = monitor

    async def scenario():
        await record(db, now - timedelta(seconds=1), "not_configured")
        await record(db, now - timedelta(seconds=10))
        first = await public_status._build_public_status()
        await record(db, now - timedelta(seconds=11))
        second = await public_status._build_public_status()
        assert second["summary"]["unknown_checks"] == 1
        assert second["summary"]["uptime_percent"] == 100
        assert (
            second["summary"]["coverage_percent"]
            == first["summary"]["coverage_percent"]
        )
        assert (
            second["daily"][-1]["coverage_percent"]
            == first["daily"][-1]["coverage_percent"]
        )
        assert not second["incidents"]

    asyncio.run(scenario())


def test_public_route_needs_no_auth_and_does_not_probe(monitor, monkeypatch):
    from api.api import app

    def fail(*args, **kwargs):
        raise AssertionError("Public status must never trigger a Schulportal login")

    monkeypatch.setattr(uptime, "_probe_portal", fail)

    async def scenario():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/status",
                "raw_path": b"/status",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 1234),
                "server": ("test", 80),
            },
            receive,
            send,
        )
        assert messages[0]["status"] == 200
        body = b"".join(message.get("body", b"") for message in messages)
        assert json.loads(body)["current"]["status"] == "unknown"

    asyncio.run(scenario())


def test_polling_coalesces_and_cache_expires_before_stale_transition(
    monitor, monkeypatch
):
    db, now = monitor
    builds = 0
    build = public_status._build_public_status
    ticks = [1000.0]
    monkeypatch.setattr(public_status.time, "monotonic", lambda: ticks[0])

    async def counting_build():
        nonlocal builds
        builds += 1
        return await build()

    monkeypatch.setattr(public_status, "_build_public_status", counting_build)

    async def scenario():
        await record(db, now - timedelta(seconds=590))
        results = await asyncio.gather(
            *(public_status.get_public_status() for _ in range(5))
        )
        assert builds == 1
        assert all(result["current"]["status"] == "up" for result in results)
        ticks[0] += 11
        monkeypatch.setattr(uptime, "_utcnow", lambda: now + timedelta(seconds=11))
        result = await public_status.get_public_status()
        assert builds == 2
        assert result["current"]["status"] == "unknown"

    asyncio.run(scenario())
