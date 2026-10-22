"""Daily, sequential read-only cache warm-up in the school's local timezone."""

import asyncio
import logging
from contextvars import ContextVar
from datetime import datetime, time, timedelta
from urllib.parse import quote, urlsplit
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
    "/settings/class-links",
    "/dashboard/notifications",
    "/notifications/vertretungsplan/options",
)


def next_run(now):
    """Return the next 03:00 Berlin wall-clock run, including DST changes."""
    target = datetime.combine(now.date(), time(3), ZONE)
    if target <= now:
        target = datetime.combine(now.date() + timedelta(days=1), time(3), ZONE)
    return target


def discover_metadata(path, params, data):
    """Enumerate supported read views from portal-owned identifiers and capabilities."""
    targets = set()
    if data.get("success") is not True:
        return targets
    if path == "/modules":
        routes = {
            "dateispeicher_get_node": "/dateispeicher",
            "dateiverteilung_get_overview": "/dateiverteilung",
            "wahlen_get_overview": "/wahlen",
        }
        for module in data.get("modules", []):
            for method in module.get("usage", []):
                if method in routes:
                    targets.add((routes[method], ()))
    if path == "/nachrichten/headers":
        from .message_read_state import conversation_id

        for row in data.get("conversations", []):
            identifier = conversation_id(row)
            if identifier:
                targets.add(("/nachrichten/" + quote(identifier, safe=""), ()))
    if path == "/wahlen":
        for election in data.get("elections", []):
            if election.get("id"):
                targets.add(("/wahlen/" + quote(str(election["id"]), safe=""), ()))
    if path == "/dateispeicher":
        for folder in data.get("folders", []):
            if folder.get("id") is not None:
                targets.add(("/dateispeicher", (("folder_id", str(folder["id"])),)))
    if path == "/meinunterricht":
        for course in [*data.get("courses", []), *data.get("entries", [])]:
            if course.get("book_id"):
                targets.add(
                    (
                        "/meinunterricht/course/"
                        + quote(str(course["book_id"]), safe=""),
                        (),
                    )
                )
    submissions = (
        list(data.get("submissions", []))
        if path == "/meinunterricht/submissions"
        else []
    )
    if path.startswith("/meinunterricht/course/"):
        for entry in data.get("entries", []):
            submissions.extend(entry.get("uploads", []))
    for submission in submissions:
        if submission.get("detail_ref"):
            targets.add(
                (
                    "/meinunterricht/submissions/"
                    + quote(str(submission["detail_ref"]), safe=""),
                    (),
                )
            )
    if path == "/kalender":
        first_view = data.get("calendar", {}).get("first_id")
        for year in ("0", "1"):
            values = (("year", year),)
            targets.add(("/kalender/events", values))
            if first_view:
                targets.add(
                    ("/kalender/events", (("view_id", str(first_view)), *values))
                )
    if path == "/kalender/events":
        views = {tuple((name, value) for name, value in params if name == "view_id")}
        selected_view = data.get("filters", {}).get("view_id")
        if selected_view:
            views.add((("view_id", str(selected_view)),))
        for event in data.get("events", []):
            if event.get("id"):
                for view_params in views:
                    targets.add(
                        (
                            "/kalender/event/" + quote(str(event["id"]), safe=""),
                            view_params,
                        )
                    )
    return targets


def binary_path(path):
    """Identify only supported authenticated file endpoints, not arbitrary links."""
    return path == "/dateiverteilung/file" or path.startswith(
        (
            "/dateispeicher/file/",
            "/meinunterricht/file/",
            "/meinunterricht/submissions/file/",
        )
    )


def discover_files(path, data):
    """Find downloadable files advertised in successful authenticated metadata."""
    targets = set()
    if data.get("success") is not True:
        return targets
    if path == "/dateispeicher":
        for item in data.get("files", []):
            if item.get("id") is not None:
                targets.add(
                    ("/dateispeicher/file/" + quote(str(item["id"]), safe=""), ())
                )
    if path == "/dateiverteilung":
        for distribution in data.get("distributions", []):
            for item in distribution.get("files", []):
                if item.get("download_url"):
                    targets.add(
                        ("/dateiverteilung/file", (("url", item["download_url"]),))
                    )
    if path.startswith("/meinunterricht/course/"):
        for entry in data.get("entries", []):
            for item in entry.get("files", []):
                target = urlsplit(item.get("download_url", "")).path
                if target.startswith("/meinunterricht/file/"):
                    targets.add((target, ()))
    if path.startswith("/meinunterricht/submissions/") and not binary_path(path):
        submission = data.get("submission", {})
        for item in [
            *submission.get("own_files", []),
            *submission.get("public_files", []),
        ]:
            if item.get("download_ref"):
                targets.add(
                    (
                        "/meinunterricht/submissions/file/"
                        + quote(str(item["download_ref"]), safe=""),
                        (),
                    )
                )
    return targets


async def warm_users():
    """Refresh authenticated read views without discarding successful snapshots."""
    from .api import _school_dsb_credentials, app, sessions
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
            if _school_dsb_credentials(user["school_id"]) is not None:
                targets.add(("/dsb/school-plan", ()))
            targets.update(
                (key[1], key[2])
                for key in list(snapshots.entries)
                if key[0] == user["user_id"]
            )
            attempted = failed = 0
            evictions_before = getattr(snapshots, "user_evictions", {}).get(
                user["user_id"], 0
            )
            reset = warming.set(True)
            try:
                # Body reads mark conversations read upstream. Preserve the current
                # header flags before replaying even previously saved bodies.
                pending = [("/nachrichten/headers", ())] + sorted(targets)
                headers_preserved = False
                seen = set()
                while pending:
                    path, params = pending.pop(0)
                    if (path, params) in seen:
                        continue
                    seen.add((path, params))
                    attempted += 1
                    if (
                        path.startswith("/nachrichten/")
                        and path != "/nachrichten/headers"
                        and not headers_preserved
                    ):
                        failed += 1
                        continue
                    read_failed = False
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
                            response.status_code != 200
                            or response.headers.get("X-LANIS-Cache") == "stale"
                        ):
                            read_failed = True
                        if response.status_code == 200 and not binary_path(path):
                            data = response.json()
                            if data.get(
                                "success"
                            ) is not True or not response.headers.get("X-LANIS-Cache"):
                                read_failed = True
                            pending.extend(
                                sorted(discover_metadata(path, params, data))
                            )
                            pending.extend(sorted(discover_files(path, data)))
                            if path == "/nachrichten/headers" and not read_failed:
                                headers_preserved = True
                    except Exception:  # noqa: BLE001 - isolate per-user endpoint failures
                        read_failed = True
                        logger.warning("Daily cache read failed for %s", path)
                    if read_failed:
                        failed += 1
                    await asyncio.sleep(0.2)
            finally:
                warming.reset(reset)
            evicted = (
                getattr(snapshots, "user_evictions", {}).get(user["user_id"], 0)
                - evictions_before
            )
            await auth_db.save_cache_sync_state(
                user["user_id"], attempted, failed, max(0, evicted)
            )
            if failed or evicted:
                completed = False
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
