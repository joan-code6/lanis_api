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

import requests
from fastapi.concurrency import run_in_threadpool

from schulportal_hessen.base import SchulportalHessenAPI

from .metrics import user_metrics_db

logger = logging.getLogger("uptime")

UPTIME_SERVICE_NAME = "Schulportal Hessen"
DEFAULT_UPTIME_URL = "https://login.schulportal.hessen.de/"
DEFAULT_UPTIME_INTERVAL_SECONDS = 5 * 60
DEFAULT_UPTIME_TIMEOUT_SECONDS = 15
UPTIME_HISTORY_LIMIT = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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


async def run_uptime_check() -> dict[str, Any]:
    """Run and persist one authenticated synthetic check."""
    check = await run_in_threadpool(_probe_portal)
    if check["status"] != "not_configured":
        await user_metrics_db.record_uptime_check(check)
    logger.info(
        "Schulportal synthetic check: %s (%sms)",
        check["status"],
        check["latency_ms"],
    )
    return check


async def get_uptime_status(limit: int = UPTIME_HISTORY_LIMIT) -> dict[str, Any]:
    """Return current feature state and a rolling availability summary."""
    history = await user_metrics_db.get_uptime_checks(limit=limit)
    since = _utcnow() - timedelta(hours=24)
    recent = await user_metrics_db.get_uptime_checks(limit=10_000, since=since)
    available = sum(1 for check in recent if check["is_available"] is True)
    failed = sum(1 for check in recent if check["is_available"] is False)
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
    observed = available + failed
    return {
        "success": True,
        "service": UPTIME_SERVICE_NAME,
        "configured": configured,
        "url": get_uptime_url(),
        "generated_at": _utcnow().isoformat() + "Z",
        "schedule": {
            "interval_seconds": get_uptime_interval_seconds(),
            "timeout_seconds": get_uptime_timeout_seconds(),
        },
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
            "period_hours": 24,
            "checks": len(recent),
            "available_checks": available,
            "failed_checks": failed,
            "uptime_percent": round(available / observed * 100, 2) if observed else None,
        },
        "history": history,
    }


async def run_uptime_scheduler() -> asyncio.Task:
    """Start the recurring authenticated synthetic monitor task."""
    interval = get_uptime_interval_seconds()

    async def _loop() -> None:
        while True:
            try:
                await run_uptime_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Schulportal synthetic check failed unexpectedly")
            await asyncio.sleep(interval)

    return asyncio.create_task(_loop(), name="schulportal-uptime")
