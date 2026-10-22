"""Daily, sequential read-only cache warm-up in the school's local timezone."""

import asyncio
import logging
from contextvars import ContextVar
from datetime import datetime, time, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from . import auth_db

logger = logging.getLogger(__name__)
warming = ContextVar("daily_cache_warming", default=False)
MAX_BATCH_ATTEMPTS = 24  # Bound repeated full batches after permanent failures.
ZONE = ZoneInfo("Europe/Berlin")
BASE_PATHS = (
    "/apps",
    "/modules",
    "/benutzer",
    "/kalender",
    "/kalender/events",
    "/vertretungsplan",
    "/vertretungsplan/options",
    "/stundenplan",
    "/stundenplan/view",
    "/lerngruppen",
    "/meinunterricht",
    "/meinunterricht/attendance",
    "/meinunterricht/weekly",
    "/meinunterricht/submissions",
    "/nachrichten/headers",
)


def next_run(now):
    """Return the next 03:00 Berlin wall-clock run, including DST changes."""
    target = datetime.combine(now.date(), time(3), ZONE)
    if target <= now:
        target = datetime.combine(now.date() + timedelta(days=1), time(3), ZONE)
    return target


async def warm_users():
    """Refresh authenticated read views without discarding successful snapshots."""
    from .api import app, sessions
    from .outage_cache import snapshots
    from .uptime import run_uptime_check

    # Unknown/degraded is not evidence that it is safe to refresh.
    status = (await run_uptime_check()).get("status")
    if status != "up":
        logger.info("Daily cache warm-up deferred: portal status %s", status)
        return False
    users = await auth_db.get_cache_warmup_users()
    completed = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://cache.local",
        timeout=60,
    ) as client:
        for user in users:
            token = sessions.create_access_token(**user)
            targets = {(path, ()) for path in BASE_PATHS}
            targets.update(
                (key[1], key[2])
                for key in list(snapshots.entries)
                if key[0] == user["user_id"] and not key[1].startswith("/nachrichten/")
            )
            reset = warming.set(True)
            try:
                pending = sorted(targets)
                seen = set()
                while pending:
                    path, params = pending.pop(0)
                    if (path, params) in seen:
                        continue
                    seen.add((path, params))
                    try:
                        response = await asyncio.wait_for(
                            client.get(
                                path,
                                params=list(params),
                                headers={"X-Session-Token": token},
                            ),
                            timeout=60,
                        )
                        if (
                            response.status_code >= 500
                            or response.headers.get("X-LANIS-Cache") == "stale"
                        ):
                            completed = False
                        if response.status_code == 200:
                            data = response.json()
                            if data.get("success") is False:
                                completed = False
                            if path == "/meinunterricht" and data.get("success"):
                                for course in data.get("courses", []):
                                    course_id = course.get("book_id")
                                    if course_id:
                                        pending.append(
                                            (
                                                "/meinunterricht/course/"
                                                + quote(str(course_id), safe=""),
                                                (),
                                            )
                                        )
                            if path == "/kalender/events" and data.get("success"):
                                for event in data.get("events", []):
                                    if event.get("id"):
                                        pending.append(
                                            (
                                                "/kalender/event/"
                                                + quote(str(event["id"]), safe=""),
                                                tuple(
                                                    (name, value)
                                                    for name, value in params
                                                    if name == "view_id"
                                                ),
                                            )
                                        )
                    except Exception:  # noqa: BLE001 - isolate per-user endpoint failures
                        completed = False
                        logger.warning("Daily cache read failed for %s", path)
                    await asyncio.sleep(0.2)
            finally:
                warming.reset(reset)
    return completed


async def run_daily_cache():
    """Schedule daily warming with a bounded budget of outage retries."""
    while True:
        now = datetime.now(ZONE)
        await asyncio.sleep(next_run(now).timestamp() - now.timestamp())
        # Permanent account/module failures must not prevent tomorrow's scheduled run.
        for attempt in range(MAX_BATCH_ATTEMPTS):
            try:
                if await warm_users():
                    break
            except Exception:
                logger.warning("Daily cache warm-up failed", exc_info=True)
            if attempt + 1 < MAX_BATCH_ATTEMPTS:
                await asyncio.sleep(15 * 60)
        else:
            logger.warning("Daily cache retry budget exhausted; resuming at next 03:00")
