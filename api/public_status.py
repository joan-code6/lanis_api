"""Public, allowlisted projections of the existing synthetic monitor."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import uptime

_cache_lock = asyncio.Lock()
_cached_status: dict[str, Any] | None = None
_cache_deadline = 0.0
_cache_configuration: tuple | None = None


def _timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (
            result.replace(tzinfo=timezone.utc)
            if result.tzinfo is None
            else result.astimezone(timezone.utc)
        )
    except (ValueError, TypeError):
        return None


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _check(row: dict[str, Any], checked_at: datetime) -> dict[str, Any]:
    # Never forward database dictionaries: legacy data may contain diagnostics,
    # module/account names, URLs, or other fields unsuitable for public access.
    features = row.get("features") or []
    return {
        "checked_at": _iso(checked_at),
        "status": row.get("status")
        if row.get("status") in {"up", "down", "degraded"}
        else "unknown",
        "features": [
            {
                "name": name,
                "status": next(
                    (
                        item.get("status")
                        for item in features
                        if isinstance(item, dict)
                        and item.get("name") == name
                        and item.get("status") in {"up", "down"}
                    ),
                    "unknown",
                ),
            }
            for name in ("login", "modules")
        ],
    }


def _aggregate(
    checks: list[tuple[datetime, dict[str, Any]]],
    start: datetime,
    end: datetime,
    interval: int,
) -> dict[str, Any]:
    available = sum(check["status"] == "up" for _, check in checks)
    failed = sum(check["status"] in {"down", "degraded"} for _, check in checks)
    # Coverage counts occupied interval slots, so manual/duplicate checks cannot
    # hide missing observations. Unknown results do not cover a slot.
    expected = max(1, math.ceil((end - start).total_seconds() / interval))
    slots = {
        min(expected - 1, int((timestamp - start).total_seconds() // interval))
        for timestamp, check in checks
        if check["status"] != "unknown"
    }
    observed = available + failed
    return {
        "checks": len(checks),
        "available_checks": available,
        "failed_checks": failed,
        "unknown_checks": len(checks) - observed,
        "uptime_percent": round(100 * available / observed, 2) if observed else None,
        "coverage_percent": round(100 * len(slots) / expected, 2),
    }


async def _build_public_status() -> dict[str, Any]:
    """Read stored observations; never trigger a login or a synthetic probe."""
    now = uptime._utcnow().replace(tzinfo=timezone.utc)
    start = now - timedelta(days=uptime.UPTIME_SUMMARY_DAYS)
    interval = uptime.get_uptime_interval_seconds()
    stale_after = 2 * interval
    rows = await uptime.user_metrics_db.get_uptime_checks(
        limit=-1, since=start.replace(tzinfo=None)
    )
    checks = []
    for row in rows:
        timestamp = _timestamp(row.get("checked_at"))
        if timestamp is not None and start <= timestamp <= now:
            checks.append((timestamp, _check(row, timestamp)))
    checks.sort(key=lambda item: item[0], reverse=True)
    latest = checks[0] if checks else None
    stale = latest is None or (now - latest[0]).total_seconds() > stale_after
    current = (
        dict(latest[1])
        if latest
        else {
            "checked_at": None,
            "status": "unknown",
            "features": [],
        }
    )
    if stale or not uptime.uptime_is_configured():
        current["status"] = "unknown"
        current["features"] = [
            {"name": name, "status": "unknown"} for name in ("login", "modules")
        ]
    current["stale"] = stale

    by_day: dict[str, list] = {}
    for timestamp, check in checks:
        by_day.setdefault(timestamp.date().isoformat(), []).append((timestamp, check))
    daily = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= now:
        next_day = day + timedelta(days=1)
        aggregate = _aggregate(
            by_day.get(day.date().isoformat(), []),
            max(day, start),
            min(next_day, now),
            interval,
        )
        available, failed = aggregate["available_checks"], aggregate["failed_checks"]
        has_degraded = any(
            check["status"] == "degraded"
            for _, check in by_day.get(day.date().isoformat(), [])
        )
        status = (
            "unknown"
            if not available and not failed
            else "degraded"
            if has_degraded or (available and failed)
            else "down"
            if failed
            else "up"
        )
        daily.append({"day": day.date().isoformat(), "status": status, **aggregate})
        day = next_day
    return {
        "success": True,
        "service": uptime.UPTIME_SERVICE_NAME,
        "generated_at": _iso(now),
        "current": current,
        "summary": {
            "period_days": uptime.UPTIME_SUMMARY_DAYS,
            **_aggregate(checks, start, now, interval),
        },
        "daily": daily,
        "history": [check for _, check in checks[: uptime.UPTIME_HISTORY_LIMIT]],
        "incidents": [
            check for _, check in checks if check["status"] in {"down", "degraded"}
        ][: uptime.UPTIME_INCIDENT_LIMIT],
        "measurement": {
            "interval_seconds": interval,
            "stale_after_seconds": stale_after,
            "period_start": _iso(start),
            "period_end": _iso(now),
            "description": "Authentifizierte Prüfungen von Anmeldung und Modulen mit einem einzelnen Testkonto. Die Verfügbarkeit ist der Anteil vollständig erfolgreicher beobachteter Prüfungen, keine Garantie für alle Schulen oder Konten. Fehlende Prüfungen bleiben unbekannt. Die Abdeckung zählt Zeitfenster mit bekannten Ergebnissen. Störungen sind einzelne fehlgeschlagene Prüfungen, keine durchgehend bestätigten Ausfallzeiten. LANIS-Verfügbarkeit wird hier nicht gemessen.",
        },
    }


async def get_public_status() -> dict[str, Any]:
    """Coalesce public polling and retain only the sanitized projection for 30s."""
    global _cached_status, _cache_deadline, _cache_configuration
    configuration = (
        id(uptime.user_metrics_db),
        uptime.uptime_is_configured(),
        uptime.get_uptime_interval_seconds(),
    )
    async with _cache_lock:
        if (
            _cached_status is not None
            and configuration == _cache_configuration
            and time.monotonic() < _cache_deadline
        ):
            return _cached_status
        result = await _build_public_status()
        ttl = 30.0
        checked_at = _timestamp(result["current"]["checked_at"])
        if checked_at is not None and not result["current"]["stale"]:
            now = uptime._utcnow().replace(tzinfo=timezone.utc)
            ttl = min(
                ttl,
                max(
                    0.0,
                    result["measurement"]["stale_after_seconds"]
                    - (now - checked_at).total_seconds(),
                ),
            )
        _cached_status = result
        _cache_configuration = configuration
        _cache_deadline = time.monotonic() + ttl
        return result
