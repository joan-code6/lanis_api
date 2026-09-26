"""Authenticated synthetic checks for the Schulportal Hessen client flow."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from fastapi.concurrency import run_in_threadpool

from schulportal_hessen.base import SchulportalHessenAPI

from .metrics import user_metrics_db

logger = logging.getLogger("uptime")

UPTIME_SERVICE_NAME = "Schulportal Hessen"
DEFAULT_UPTIME_URL = "https://login.schulportal.hessen.de/"
DEFAULT_UPTIME_INTERVAL_SECONDS = 5 * 60
DEFAULT_UPTIME_TIMEOUT_SECONDS = 15
INCIDENT_UPTIME_INTERVAL_SECONDS = 15
UPTIME_HISTORY_LIMIT = 100
UPTIME_INCIDENT_LIMIT = 100
UPTIME_SUMMARY_DAYS = 90
UPTIME_ALERT_RETRY_SECONDS = 300
DISCORD_WEBHOOK_ENV = "LANIS_UPTIME_DISCORD_WEBHOOK_URL"

_uptime_alert_lock = asyncio.Lock()
_uptime_scheduler_wake_event: asyncio.Event | None = None
_uptime_notification_tasks: set[asyncio.Task[None]] = set()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def wake_uptime_scheduler() -> None:
    """Wake the recurring scheduler after an external check confirms an incident."""
    if _uptime_scheduler_wake_event is not None:
        _uptime_scheduler_wake_event.set()


def _schedule_discord_notification(check: dict[str, Any]) -> None:
    """Deliver alerts in the background so webhook latency cannot delay probes."""
    task = asyncio.create_task(_notify_discord_on_transition(check))
    _uptime_notification_tasks.add(task)

    def _finish(completed: asyncio.Task[None]) -> None:
        _uptime_notification_tasks.discard(completed)
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            logger.error(
                "Unexpected error while sending Schulportal uptime notification",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(_finish)


async def drain_uptime_notification_tasks(timeout_seconds: float = 65.0) -> None:
    """Wait for active webhook deliveries before shutdown, with a fixed bound."""
    pending = set(_uptime_notification_tasks)
    if not pending:
        return
    _, pending = await asyncio.wait(pending, timeout=timeout_seconds)
    if pending:
        logger.warning(
            "Cancelling %d uptime notification task(s) after shutdown timeout",
            len(pending),
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


def get_uptime_url() -> str:
    """Return the Schulportal login URL used by the authenticated client."""
    return DEFAULT_UPTIME_URL


def _positive_env(name: str, default: float, minimum: float, maximum: float) -> float:
    """Read and clamp a finite positive numeric environment setting."""
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    return min(max(value, minimum), maximum)


def get_uptime_interval_seconds() -> int:
    """Return the scheduler interval in seconds."""
    return round(
        _positive_env(
            "LANIS_UPTIME_INTERVAL_SECONDS",
            DEFAULT_UPTIME_INTERVAL_SECONDS,
            60,
            24 * 60 * 60,
        )
    )


def get_uptime_timeout_seconds() -> float:
    """Return the maximum request timeout in seconds."""
    return _positive_env(
        "LANIS_UPTIME_TIMEOUT_SECONDS",
        DEFAULT_UPTIME_TIMEOUT_SECONDS,
        1,
        60,
    )


def _uptime_credentials() -> tuple[str, str, str] | None:
    """Read monitor credentials without ever returning them from an API."""
    # Dedicated monitor variables are preferred. The LANIS_API_* fallback
    # keeps existing local deployments working when they already have a test
    # account configured for the API client.
    school_id = (
        os.getenv("LANIS_UPTIME_SCHOOL_ID") or os.getenv("LANIS_API_SCHOOL_ID") or ""
    ).strip()
    username = (
        os.getenv("LANIS_UPTIME_USERNAME") or os.getenv("LANIS_API_USERNAME") or ""
    ).strip()
    password = os.getenv("LANIS_UPTIME_PASSWORD") or os.getenv("LANIS_API_PASSWORD") or ""
    if not school_id or not username or not password:
        return None
    return school_id, username, password


def uptime_is_configured() -> bool:
    """Return whether a complete monitor credential set is available."""
    return _uptime_credentials() is not None


def _discord_webhook_url() -> str | None:
    """Return a validated Discord webhook URL from the server environment."""
    value = os.getenv(DISCORD_WEBHOOK_ENV, "").strip()
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "discord.com"
        or not parsed.path.startswith("/api/webhooks/")
    ):
        return None
    return value


def _status_page_url() -> str:
    base = os.getenv("LANIS_UI_BASE_URL", "https://lanis.arg-server.de").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        base = "https://lanis.arg-server.de"
    return f"{base}/status"


def _error_code(error: Any, default: str) -> str:
    """Map client/requests failures to a safe, non-sensitive error code."""
    if isinstance(error, requests.Timeout):
        return "timeout"
    if isinstance(error, requests.ConnectionError):
        return "connection_error"
    if isinstance(error, requests.RequestException):
        return "request_error"
    return default


def _feature(
    name: str,
    status: str,
    started: float,
    *,
    error: str | None = None,
    module_count: int | None = None,
    opened_count: int | None = None,
) -> dict[str, Any]:
    """Build a normalized result for one synthetic feature check."""
    return {
        "name": name,
        "status": status,
        "is_available": True if status == "up" else False if status == "down" else None,
        "latency_ms": max(0, round((time.perf_counter() - started) * 1000)),
        "error": error,
        "module_count": module_count,
        "opened_count": opened_count,
    }


def _install_request_timeout(client: SchulportalHessenAPI) -> None:
    """Give client requests a default timeout where the client omits one."""
    original_request = client.session.request
    timeout = get_uptime_timeout_seconds()

    def request(method: str, url: str, **kwargs: Any):
        kwargs.setdefault("timeout", timeout)
        return original_request(method, url, **kwargs)

    client.session.request = request  # type: ignore[method-assign]


def _check_modules(client: SchulportalHessenAPI) -> dict[str, Any]:
    """Fetch the account's modules and verify that each module can open."""
    started = time.perf_counter()
    try:
        apps_result = client.get_apps()
        if not apps_result.get("success"):
            return _feature(
                "modules",
                "down",
                started,
                error="modules_request_failed",
                module_count=0,
                opened_count=0,
            )

        entries = apps_result.get("data", {}).get("entrys", [])
        if not isinstance(entries, list):
            return _feature(
                "modules",
                "down",
                started,
                error="modules_response_invalid",
                module_count=0,
                opened_count=0,
            )

        modules = client.get_available_modules(apps_result)
        if not modules:
            return _feature(
                "modules",
                "down",
                started,
                error="modules_empty",
                module_count=0,
                opened_count=0,
            )

        opened_count = 0
        for module in modules:
            module_url = str(module.get("url") or "")
            if not module_url:
                continue
            try:
                response = client.session.get(
                    module_url,
                    allow_redirects=False,
                    stream=True,
                )
                status_code = int(response.status_code)
                response.close()
                if 200 <= status_code < 400:
                    opened_count += 1
            except requests.RequestException:
                continue

        module_count = len(modules)
        return _feature(
            "modules",
            "up" if opened_count == module_count else "down",
            started,
            error=None if opened_count == module_count else "module_open_failed",
            module_count=module_count,
            opened_count=opened_count,
        )
    except Exception as error:  # noqa: BLE001 - a monitor must classify SDK failures
        return _feature(
            "modules",
            "down",
            started,
            error=_error_code(error, "modules_check_failed"),
            module_count=0,
            opened_count=0,
        )


def _send_discord_alert(webhook_url: str, check: dict[str, Any], recovered: bool) -> None:
    """Send one uptime transition notification to the configured webhook."""
    features = check.get("features") or []
    feature_status = ", ".join(
        f"{feature.get('name', 'feature')}: {feature.get('status', 'unknown')}"
        for feature in features
    )
    if recovered:
        content = (
            f"✅ **{UPTIME_SERVICE_NAME} ist wieder erreichbar.**\n"
            f"Alle synthetischen Checks sind wieder erfolgreich ({feature_status or 'keine Details'}).\n"
            f"Wiederhergestellt: {check.get('checked_at', 'unbekannt')}\n"
            f"Status-Seite: {_status_page_url()}"
        )
    else:
        error_text = f" · Fehler: `{check['error']}`" if check.get("error") else ""
        content = (
            f"🚨 **{UPTIME_SERVICE_NAME} ist beeinträchtigt.**\n"
            f"Status: `{check.get('status', 'unknown')}`"
            f"{error_text}\n"
            f"Checks: {feature_status or 'keine Details'}"
            f"\nErkannt: {check.get('checked_at', 'unbekannt')}"
            f"\nStatus-Seite: {_status_page_url()}"
        )
    response = requests.post(
        webhook_url,
        json={"content": content, "allowed_mentions": {"parse": []}},
        timeout=get_uptime_timeout_seconds(),
    )
    response.raise_for_status()


async def _notify_discord_on_transition(check: dict[str, Any]) -> None:
    """Notify Discord only when uptime changes between healthy and unhealthy."""
    webhook_url = _discord_webhook_url()
    if not webhook_url:
        return

    is_issue = check.get("status") != "up"
    async with _uptime_alert_lock:
        previous_issue, state_updated_at = await user_metrics_db.get_uptime_alert_state_details()
        if previous_issue is None and not is_issue:
            await user_metrics_db.set_uptime_alert_state(False)
            return
        if previous_issue is not None and previous_issue == is_issue:
            await user_metrics_db.touch_uptime_alert_state()
            return

        transition = "recovery" if not is_issue else "incident"
        for delivery in await user_metrics_db.get_uptime_alert_deliveries(limit=100):
            if delivery.get("transition") != transition:
                continue
            if delivery.get("outcome") == "failed":
                try:
                    failed_at = datetime.fromisoformat(
                        str(delivery.get("created_at")).replace("Z", "+00:00")
                    )
                    if failed_at.tzinfo:
                        failed_at = failed_at.astimezone(timezone.utc).replace(tzinfo=None)
                    state_at = (
                        datetime.fromisoformat(str(state_updated_at).replace("Z", "+00:00"))
                        if state_updated_at
                        else None
                    )
                    if state_at is not None and state_at.tzinfo:
                        state_at = state_at.astimezone(timezone.utc).replace(tzinfo=None)
                    is_current_pending = state_at is None or failed_at > state_at
                    if is_current_pending and (_utcnow() - failed_at).total_seconds() < UPTIME_ALERT_RETRY_SECONDS:
                        return
                except (TypeError, ValueError):
                    pass
            break

        try:
            await run_in_threadpool(
                _send_discord_alert,
                webhook_url,
                check,
                not is_issue,
            )
        except Exception:
            with contextlib.suppress(Exception):
                await user_metrics_db.record_uptime_alert_delivery(
                    transition, "failed", "delivery_failed"
                )
            logger.warning(
                "Could not deliver Schulportal uptime transition to Discord",
                exc_info=True,
            )
            return
        with contextlib.suppress(Exception):
            await user_metrics_db.record_uptime_alert_delivery(
                transition, "delivered"
            )
        await user_metrics_db.set_uptime_alert_state(is_issue)


def _probe_portal() -> dict[str, Any]:
    """Log in and open the configured account's available modules."""
    checked_at = _utcnow().isoformat()
    overall_started = time.perf_counter()
    if not uptime_is_configured():
        return {
            "checked_at": checked_at,
            "url": get_uptime_url(),
            "status": "not_configured",
            "is_available": None,
            "status_code": None,
            "latency_ms": 0,
            "error": "credentials_not_configured",
            "features": [
                _feature("login", "not_configured", overall_started, error="credentials_not_configured"),
                _feature("modules", "skipped", overall_started, error="login_not_configured"),
            ],
        }

    credentials = _uptime_credentials()
    if credentials is None:
        # The environment can change between the first check and this read.
        return {
            "checked_at": checked_at,
            "url": get_uptime_url(),
            "status": "not_configured",
            "is_available": None,
            "status_code": None,
            "latency_ms": 0,
            "error": "credentials_not_configured",
            "features": [
                _feature("login", "not_configured", overall_started, error="credentials_not_configured"),
                _feature("modules", "skipped", overall_started, error="login_not_configured"),
            ],
        }
    school_id, username, password = credentials
    client = SchulportalHessenAPI()
    _install_request_timeout(client)
    features: list[dict[str, Any]] = []
    try:
        login_started = time.perf_counter()
        try:
            # The client currently prints encryption setup diagnostics. Keep
            # those implementation details out of service logs as well.
            with contextlib.redirect_stdout(io.StringIO()):
                login_result = client.login(school_id, username, password)
            login_ok = bool(login_result.get("success"))
            login_error = None if login_ok else "login_failed"
        except Exception as error:  # noqa: BLE001 - a monitor must classify SDK failures
            login_ok = False
            login_error = _error_code(error, "login_check_failed")
        features.append(_feature("login", "up" if login_ok else "down", login_started, error=login_error))

        if login_ok:
            features.append(_check_modules(client))
        else:
            features.append(_feature("modules", "skipped", overall_started, error="login_failed"))

        failed = [feature for feature in features if feature["status"] == "down"]
        status = "down" if not login_ok else "degraded" if failed else "up"
        return {
            "checked_at": checked_at,
            "url": get_uptime_url(),
            "status": status,
            "is_available": status == "up",
            "status_code": None,
            "latency_ms": max(0, round((time.perf_counter() - overall_started) * 1000)),
            "error": failed[0]["error"] if failed else None,
            "features": features,
        }
    finally:
        try:
            client.close()
        except Exception:
            logger.debug("Schulportal monitor client close failed", exc_info=True)


def group_uptime_incidents(checks: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """Collapse consecutive confirmed failures into incidents with recovery times."""
    now = now or _utcnow()
    ordered = sorted(checks, key=lambda item: str(item.get("checked_at") or ""))
    incidents: list[dict[str, Any]] = []
    active: dict[str, Any] | None = None
    for check in ordered:
        checked_at = str(check.get("checked_at") or "")
        if check.get("status") in {"down", "degraded"}:
            failed_features = [
                str(feature.get("name"))
                for feature in check.get("features") or []
                if isinstance(feature, dict) and feature.get("status") == "down"
            ]
            if active is None:
                active = {
                    "started_at": checked_at,
                    "checked_at": checked_at,
                    "resolved_at": None,
                    "duration_seconds": None,
                    "status": check.get("status"),
                    "checks": 0,
                    "affected_features": [],
                    "last_checked_at": checked_at,
                    "error": check.get("error"),
                }
            active["checks"] += 1
            active["last_checked_at"] = checked_at
            active["checked_at"] = checked_at
            if check.get("status") == "down":
                active["status"] = "down"
            active["affected_features"] = sorted(set(active["affected_features"]) | set(failed_features))
            active["error"] = check.get("error") or active["error"]
            continue
        if active is not None and check.get("status") != "up":
            # Unknown or unconfigured observations do not confirm recovery.
            continue
        if active is not None:
            active["resolved_at"] = checked_at
            try:
                start = datetime.fromisoformat(active["started_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
                active["duration_seconds"] = max(0, int((end - start).total_seconds()))
            except (TypeError, ValueError):
                pass
            incidents.append(active)
            active = None
    if active is not None:
        try:
            start = datetime.fromisoformat(active["started_at"].replace("Z", "+00:00"))
            end = now.replace(tzinfo=start.tzinfo) if start.tzinfo else now.replace(tzinfo=None)
            active["duration_seconds"] = max(0, int((end - start).total_seconds()))
        except (TypeError, ValueError):
            pass
        incidents.append(active)
    return incidents


def _time_weighted_uptime(checks: list[dict[str, Any]], start: datetime, end: datetime) -> float | None:
    """Calculate availability by observed time, not raw sample count."""
    interval = get_uptime_interval_seconds()
    ordered = []
    seen_timestamps = set()
    for check in checks:
        try:
            stamp = datetime.fromisoformat(str(check.get("checked_at")).replace("Z", "+00:00"))
            if stamp.tzinfo:
                stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
            if stamp in seen_timestamps:
                continue
            seen_timestamps.add(stamp)
            ordered.append((stamp, check))
        except (TypeError, ValueError):
            continue
    ordered.sort(key=lambda item: item[0])
    observed = available = 0.0
    for index, (stamp, check) in enumerate(ordered):
        if check.get("status") not in {"up", "down", "degraded"}:
            continue
        next_stamp = ordered[index + 1][0] if index + 1 < len(ordered) else end
        cadence = _sample_interval_seconds(
            check,
            ordered[index - 1][0] if index else None,
            next_stamp if index + 1 < len(ordered) else None,
            interval,
        )
        span_end = min(next_stamp, stamp + timedelta(seconds=2 * cadence), end)
        seconds = max(0.0, (span_end - max(stamp, start)).total_seconds())
        observed += seconds
        if check.get("status") == "up":
            available += seconds
    if observed == 0:
        latest = next((check for stamp, check in reversed(ordered) if start <= stamp <= end and check.get("status") in {"up", "down", "degraded"}), None)
        if latest is not None:
            return 100.0 if latest.get("status") == "up" else 0.0
    return round(available / observed * 100, 2) if observed else None


def _sample_interval_seconds(
    check: dict[str, Any], previous: datetime | None, following: datetime | None, normal_interval: int | None = None
) -> int:
    """Use stored cadence, falling back to neighboring timestamps for legacy rows."""
    def utc_naive(value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    previous = utc_naive(previous)
    following = utc_naive(following)
    normal = normal_interval or get_uptime_interval_seconds()
    stored = check.get("sample_interval_seconds")
    if isinstance(stored, (int, float)) and stored > 0:
        return int(stored)
    if check.get("status") not in {"down", "degraded"}:
        return normal
    gaps = [
        (other - stamp).total_seconds()
        for other, stamp in ((previous, _parse_check_time(check)), (following, _parse_check_time(check)))
        if other is not None and stamp is not None and other != stamp
    ]
    if gaps:
        nearest = min(abs(gap) for gap in gaps)
        return max(INCIDENT_UPTIME_INTERVAL_SECONDS, min(normal, round(nearest)))
    return normal


def _parse_check_time(check: dict[str, Any]) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(check.get("checked_at")).replace("Z", "+00:00"))
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    except (TypeError, ValueError):
        return None


def _dedupe_uptime_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the newest database row for each timestamp."""
    newest_by_timestamp: dict[datetime, dict[str, Any]] = {}
    for check in checks:
        stamp = _parse_check_time(check)
        if stamp is not None:
            # Database reads order collisions by descending row id.
            newest_by_timestamp.setdefault(stamp, check)
    return [check for _, check in sorted(newest_by_timestamp.items(), reverse=True)]


def _latency_summary(checks: list[dict[str, Any]]) -> dict[str, Any]:
    def summarize(values: list[Any]) -> dict[str, float | None]:
        ordered = sorted(float(value) for value in values if isinstance(value, (int, float)))
        if not ordered:
            return {"median": None, "p95": None}
        middle = len(ordered) // 2
        median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2
        return {
            "median": round(median, 1),
            "p95": round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)], 1),
        }

    return {
        "overall": summarize([check.get("latency_ms") for check in checks]),
        "features": {
            name: summarize([
                feature.get("latency_ms")
                for check in checks
                for feature in (check.get("features") or [])
                if isinstance(feature, dict) and feature.get("name") == name
            ])
            for name in ("login", "modules")
        },
    }


def _uptime_window(checks: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any]:
    def timestamp(check: dict[str, Any]) -> datetime | None:
        try:
            value = datetime.fromisoformat(str(check.get("checked_at")).replace("Z", "+00:00"))
            return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
        except (TypeError, ValueError):
            return None

    timed = []
    seen_timestamps = set()
    for check in checks:
        stamp = timestamp(check)
        if stamp is not None and stamp <= end and stamp not in seen_timestamps:
            # get_uptime_checks orders colliding timestamps by newest row id.
            seen_timestamps.add(stamp)
            timed.append((stamp, check))
    timed.sort(key=lambda item: item[0])
    selected = [check for stamp, check in timed if start <= stamp <= end]
    prior = [(stamp, check) for stamp, check in timed if stamp < start]
    represented = ([prior[-1][1]] if prior else []) + selected
    available = sum(check.get("status") == "up" for check in selected)
    failed = sum(check.get("status") in {"down", "degraded"} for check in selected)
    interval = get_uptime_interval_seconds()
    parsed = [(timestamp(check), check) for check in represented]
    parsed.sort(key=lambda item: item[0])
    observed_seconds = available_seconds = 0.0
    for index, (stamp, _check) in enumerate(parsed):
        if _check.get("status") not in {"up", "down", "degraded"}:
            continue
        next_stamp = parsed[index + 1][0] if index + 1 < len(parsed) else end
        cadence = _sample_interval_seconds(
            _check,
            parsed[index - 1][0] if index else None,
            next_stamp if index + 1 < len(parsed) else None,
            interval,
        )
        seconds = max(0.0, (min(next_stamp, stamp + timedelta(seconds=2 * cadence), end) - max(stamp, start)).total_seconds())
        observed_seconds += seconds
        if _check.get("status") == "up":
            available_seconds += seconds
    if observed_seconds == 0 and selected:
        latest = max(selected, key=lambda check: timestamp(check) or datetime.min)
        if latest.get("status") in {"up", "down", "degraded"}:
            observed_seconds = 1.0
            if latest.get("status") == "up":
                available_seconds = 1.0
        else:
            available_seconds = 0.0
    return {
        "checks": len(selected),
        "available_checks": available,
        "failed_checks": failed,
        "uptime_percent": round(available_seconds / observed_seconds * 100, 2) if observed_seconds else None,
        "coverage_percent": round(100 * min(1.0, observed_seconds / max(1.0, (end - start).total_seconds())), 2),
        "latency": _latency_summary(selected),
    }


async def run_uptime_check() -> dict[str, Any]:
    """Retry failures once and persist the resulting healthy or confirmed state."""
    previous_checks = await user_metrics_db.get_uptime_checks(limit=1)
    previous_failure = bool(
        previous_checks and previous_checks[0].get("status") in {"down", "degraded"}
    )
    probe_cycle_started = time.perf_counter()
    check = await run_in_threadpool(_probe_portal)
    if check["status"] in {"down", "degraded"}:
        retry = await run_in_threadpool(_probe_portal)
        if retry["status"] == "up":
            check = retry
            if previous_failure:
                # A passing observation closes an already confirmed incident.
                logger.info("Schulportal incident recovered after successful retry")
            else:
                logger.info("Discarding transient Schulportal check failure after successful retry")
        else:
            check = retry
    if check["status"] != "not_configured":
        probe_cycle_seconds = max(0, math.ceil(time.perf_counter() - probe_cycle_started))
        next_interval = (
            INCIDENT_UPTIME_INTERVAL_SECONDS
            if check["status"] in {"down", "degraded"}
            else get_uptime_interval_seconds()
        )
        check["checked_at"] = _utcnow().isoformat()
        check["sample_interval_seconds"] = next_interval + probe_cycle_seconds
        await user_metrics_db.record_uptime_check(check)
        _schedule_discord_notification(check)
    logger.info(
        "Schulportal synthetic check: %s (%sms)",
        check["status"],
        check["latency_ms"],
    )
    return check


async def get_uptime_status(limit: int = UPTIME_HISTORY_LIMIT) -> dict[str, Any]:
    """Return current feature state and a rolling availability summary."""
    now = _utcnow()
    history = await user_metrics_db.get_uptime_checks(limit=limit)
    since = now - timedelta(days=UPTIME_SUMMARY_DAYS)
    incident_checks = await user_metrics_db.get_uptime_checks(limit=-1, since=since)
    get_incident_prefix = getattr(user_metrics_db, "get_uptime_incident_prefix", None)
    if get_incident_prefix is not None:
        incident_checks.extend(await get_incident_prefix(since))
    previous_check = await user_metrics_db.get_previous_uptime_check(since)
    if previous_check is not None:
        incident_checks.append(previous_check)
    incident_checks = _dedupe_uptime_checks(incident_checks)
    incidents = group_uptime_incidents(incident_checks)[-UPTIME_INCIDENT_LIMIT:][::-1]
    daily_by_day = {
        item["day"]: item
        for item in await user_metrics_db.get_uptime_daily_series(since)
    }
    daily = []
    day = since.date()
    while day <= now.date():
        day_key = day.isoformat()
        daily.append(
            daily_by_day.get(
                day_key,
                {
                    "day": day_key,
                    "checks": 0,
                    "available_checks": 0,
                    "failed_checks": 0,
                    "uptime_percent": None,
                    "status": "unknown",
                },
            )
        )
        day += timedelta(days=1)
    timed_checks = []
    for check in incident_checks:
        try:
            stamp = datetime.fromisoformat(str(check.get("checked_at")).replace("Z", "+00:00"))
            stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo else stamp
            timed_checks.append((stamp, check))
        except (TypeError, ValueError):
            continue
    timed_checks.sort(key=lambda item: item[0])
    cursor = 0
    previous_check = None
    for item in daily:
        day_start = datetime.fromisoformat(item["day"])
        window_start = max(day_start, since)
        day_end = min(day_start + timedelta(days=1), now)
        is_current_day = day_start.date() == now.date()
        while cursor < len(timed_checks) and timed_checks[cursor][0] < window_start:
            previous_check = timed_checks[cursor][1]
            cursor += 1
        day_checks = [previous_check] if previous_check else []
        while cursor < len(timed_checks) and (
            timed_checks[cursor][0] < day_end
            or (is_current_day and timed_checks[cursor][0] == now)
        ):
            day_checks.append(timed_checks[cursor][1])
            cursor += 1
        if day_checks:
            previous_check = day_checks[-1]
        window = _uptime_window(day_checks, window_start, day_end)
        available = window["available_checks"]
        failed = window["failed_checks"]
        item["checks"] = window["checks"]
        item["available_checks"] = available
        item["failed_checks"] = failed
        item["unknown_checks"] = window["checks"] - available - failed
        item["uptime_percent"] = window["uptime_percent"]
        item["coverage_percent"] = window["coverage_percent"]
        day_statuses = [
            check.get("status")
            for check in day_checks
            if (stamp := _parse_check_time(check)) is not None
            and window_start <= stamp
            and (stamp < day_end or (is_current_day and stamp == now))
        ]
        has_degraded = "degraded" in day_statuses
        has_down = "down" in day_statuses
        if available or failed:
            item["status"] = (
                "down"
                if not available and has_down
                else "degraded"
                if has_degraded or (available and failed)
                else "down"
                if not available
                else "up"
            )
        elif window["uptime_percent"] is None or window["coverage_percent"] == 0:
            item["status"] = "unknown"
        elif any(
            check.get("status") == "degraded"
            and (stamp := _parse_check_time(check)) is not None
            and stamp < window_start
            for check in day_checks
        ):
            item["status"] = "degraded"
        else:
            item["status"] = (
                "up"
                if window["uptime_percent"] >= 100
                else "down"
                if window["uptime_percent"] <= 0
                else "degraded"
            )
    windows = {
        key: _uptime_window(incident_checks, now - timedelta(days=days), now)
        for key, days in (("24h", 1), ("7d", 7), ("30d", 30), ("90d", 90))
    }
    summary_window = windows["90d"]
    current = history[0] if history else None
    configured = uptime_is_configured()
    if not configured:
        current = {
            "status": "not_configured",
            "is_available": None,
            "checked_at": None,
            "status_code": None,
            "latency_ms": None,
            "error": "credentials_not_configured",
            "features": [],
        }
    return {
        "success": True,
        "service": UPTIME_SERVICE_NAME,
        "configured": configured,
        "url": get_uptime_url(),
        "generated_at": _utcnow().isoformat() + "Z",
        "schedule": {
            "interval_seconds": get_uptime_interval_seconds(),
            "incident_interval_seconds": INCIDENT_UPTIME_INTERVAL_SECONDS,
            "timeout_seconds": get_uptime_timeout_seconds(),
        },
        "daily": daily,
        "summary_windows": windows,
        "current": current
        or {
            "status": "unknown",
            "is_available": None,
            "checked_at": None,
            "status_code": None,
            "latency_ms": None,
            "error": None,
            "features": [],
        },
        "summary": {
            "period_days": UPTIME_SUMMARY_DAYS,
            "period_hours": UPTIME_SUMMARY_DAYS * 24,
            "checks": summary_window["checks"],
            "available_checks": summary_window["available_checks"],
            "failed_checks": summary_window["failed_checks"],
            "unknown_checks": summary_window["checks"]
            - summary_window["available_checks"]
            - summary_window["failed_checks"],
            "uptime_percent": summary_window["uptime_percent"],
        },
        "history": history,
        "incidents": incidents,
        "alert_deliveries": await user_metrics_db.get_uptime_alert_deliveries(),
        "alert_configured": _discord_webhook_url() is not None,
    }


async def run_uptime_scheduler() -> asyncio.Task:
    """Start the recurring authenticated synthetic monitor task."""
    global _uptime_scheduler_wake_event
    wake_event = asyncio.Event()
    _uptime_scheduler_wake_event = wake_event

    async def _loop() -> None:
        incident_active = False
        while True:
            try:
                check = await run_uptime_check()
                if check.get("status") in {"down", "degraded"}:
                    incident_active = True
                elif check.get("status") == "up":
                    incident_active = False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Schulportal synthetic check failed unexpectedly")
                try:
                    latest = await user_metrics_db.get_uptime_checks(limit=1)
                    if latest and latest[0].get("status") in {"down", "degraded"}:
                        incident_active = True
                    elif latest and latest[0].get("status") == "up":
                        incident_active = False
                except Exception:
                    logger.warning("Could not reload persisted uptime state after scheduler failure", exc_info=True)
            interval = (
                INCIDENT_UPTIME_INTERVAL_SECONDS
                if incident_active
                else get_uptime_interval_seconds()
            )
            try:
                await asyncio.wait_for(wake_event.wait(), timeout=interval)
                wake_event.clear()
            except asyncio.TimeoutError:
                pass

    return asyncio.create_task(_loop(), name="schulportal-uptime")
