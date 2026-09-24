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
    result = {
        "checked_at": _iso(checked_at),
        "status": row.get("status")
        if row.get("status") in {"up", "down", "degraded"}
        else "unknown",
        "features": [],
    }
    if row.get("latency_ms") is not None:
        result["latency_ms"] = row["latency_ms"]
    if row.get("sample_interval_seconds") is not None:
        result["sample_interval_seconds"] = row["sample_interval_seconds"]
    for name in ("login", "modules"):
        item = next((
            value for value in features
            if isinstance(value, dict) and value.get("name") == name
        ), None)
        feature = {
            "name": name,
            "status": item.get("status") if item and item.get("status") in {"up", "down"} else "unknown",
        }
        if item and item.get("latency_ms") is not None:
            feature["latency_ms"] = item["latency_ms"]
        result["features"].append(feature)
    return result


def _aggregate(
    checks: list[tuple[datetime, dict[str, Any]]],
    start: datetime,
    end: datetime,
    interval: int,
) -> dict[str, Any]:
    in_window = [(timestamp, check) for timestamp, check in checks if start <= timestamp <= end]
    available = sum(check["status"] == "up" for _, check in in_window)
    failed = sum(check["status"] in {"down", "degraded"} for _, check in in_window)
    # Weight each result by the time it represents so 15-second incident checks
    # do not count more heavily than normal five-minute checks.
    expected_seconds = max(1.0, (end - start).total_seconds())
    ordered = sorted(checks, key=lambda item: item[0])
    observed_seconds = available_seconds = 0.0
    # Each observation represents its state until the next check, capped at
    # twice its stored or inferred sampling interval. This keeps incident-mode sampling from
    # overweighting downtime in the percentage while still exposing gaps.
    for index, (timestamp, check) in enumerate(ordered):
        if check["status"] == "unknown":
            continue
        next_timestamp = ordered[index + 1][0] if index + 1 < len(ordered) else end
        cadence = uptime._sample_interval_seconds(
            check,
            ordered[index - 1][0] if index else None,
            next_timestamp if index + 1 < len(ordered) else None,
            interval,
        )
        span_end = min(next_timestamp, timestamp + timedelta(seconds=max(1, cadence * 2)), end)
        seconds = max(0.0, (span_end - max(timestamp, start)).total_seconds())
        observed_seconds += seconds
        if check["status"] == "up":
            available_seconds += seconds
    observed = available + failed
    if observed_seconds == 0 and in_window:
        observed_seconds = 1.0
        latest_in_window = max(in_window, key=lambda item: item[0])[1]
        if latest_in_window["status"] == "up":
            available_seconds = 1.0
    return {
        "checks": len(in_window),
        "available_checks": available,
        "failed_checks": failed,
        "unknown_checks": len(in_window) - observed,
        "uptime_percent": round(100 * available_seconds / observed_seconds, 2) if observed_seconds else None,
        "coverage_percent": round(100 * min(observed_seconds, expected_seconds) / expected_seconds, 2),
    }


def _latencies(checks: list[tuple[datetime, dict[str, Any]]]) -> dict[str, Any]:
    def stats(values: list[Any]) -> dict[str, float | None]:
        values = sorted(float(value) for value in values if isinstance(value, (int, float)))
        if not values:
            return {"median": None, "p95": None}
        middle = len(values) // 2
        median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
        return {
            "median": round(median, 1),
            "p95": round(values[min(len(values) - 1, math.ceil(len(values) * 0.95) - 1)], 1),
        }

    return {
        "overall": stats([check.get("latency_ms") for _, check in checks]),
        "features": {
            name: stats([
                feature.get("latency_ms")
                for _, check in checks
                for feature in (check.get("features") or [])
                if isinstance(feature, dict) and feature.get("name") == name
            ])
            for name in ("login", "modules")
        },
    }


async def _build_public_status() -> dict[str, Any]:
    """Read stored observations; never trigger a login or a synthetic probe."""
    now = uptime._utcnow().replace(tzinfo=timezone.utc)
    start = now - timedelta(days=uptime.UPTIME_SUMMARY_DAYS)
    interval = uptime.get_uptime_interval_seconds()
    query_start = start - timedelta(seconds=uptime._uptime_predecessor_lookback_seconds())
    rows = await uptime.user_metrics_db.get_uptime_checks(
        limit=-1, since=query_start.replace(tzinfo=None)
    )
    checks = []
    for row in rows:
        timestamp = _timestamp(row.get("checked_at"))
        if timestamp is not None and query_start <= timestamp <= now:
            checks.append((timestamp, row))
    # Timestamps can collide when checks are written close together. Keep the
    # newest database row stable without exposing its internal identifier.
    checks.sort(key=lambda item: (item[0], str(item[1].get("id", ""))), reverse=True)
    checks = [(timestamp, _check(row, timestamp)) for timestamp, row in checks]
    latest = checks[0] if checks else None
    latest_status = latest[1]["status"] if latest else "unknown"
    if latest_status in {"down", "degraded"}:
        last_probe_seconds = (latest[1].get("latency_ms") or 0) / 1000
        stale_after = max(
            2 * uptime.INCIDENT_UPTIME_INTERVAL_SECONDS,
            uptime.INCIDENT_UPTIME_INTERVAL_SECONDS + 2 * last_probe_seconds,
        )
    else:
        stale_after = 2 * interval
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

    ordered_checks = sorted(checks, key=lambda item: item[0])
    daily = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    cursor = 0
    previous = None
    while day <= now:
        next_day = day + timedelta(days=1)
        day_start = max(day, start)
        while cursor < len(ordered_checks) and ordered_checks[cursor][0] < day_start:
            previous = ordered_checks[cursor]
            cursor += 1
        day_checks = [previous] if previous else []
        day_end = min(next_day, now)
        is_current_day = day.date() == now.date()
        while cursor < len(ordered_checks) and (
            ordered_checks[cursor][0] < day_end
            or (is_current_day and ordered_checks[cursor][0] == now)
        ):
            day_checks.append(ordered_checks[cursor])
            cursor += 1
        if day_checks:
            previous = day_checks[-1]
        aggregate = _aggregate(
            day_checks,
            day_start,
            day_end,
            interval,
        )
        available, failed = aggregate["available_checks"], aggregate["failed_checks"]
        has_degraded = any(
            check["status"] == "degraded"
            for timestamp, check in day_checks
            if day_start <= timestamp < day_end or (is_current_day and timestamp == now)
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
    windows = {}
    for key, period in (("24h", 1), ("7d", 7), ("30d", 30), ("90d", 90)):
        period_start = now - timedelta(days=period)
        period_checks = [(timestamp, check) for timestamp, check in checks if timestamp >= period_start]
        prior = [(timestamp, check) for timestamp, check in checks if timestamp < period_start]
        if prior:
            period_checks.insert(0, prior[0])
        windows[key] = {
            "period_days": period,
            **_aggregate(period_checks, period_start, now, interval),
            "latency": _latencies([
                item for item in period_checks if item[0] >= period_start
            ]),
        }
    return {
        "success": True,
        "service": uptime.UPTIME_SERVICE_NAME,
        "generated_at": _iso(now),
        "current": current,
        "summary": {
            "period_days": uptime.UPTIME_SUMMARY_DAYS,
            **_aggregate(checks, start, now, interval),
        },
        "summary_windows": windows,
        "daily": daily,
        "history": [check for _, check in checks[: uptime.UPTIME_HISTORY_LIMIT]],
        "incidents": uptime.group_uptime_incidents(
            [check for _, check in checks], now
        )[-uptime.UPTIME_INCIDENT_LIMIT :][::-1],
        "measurement": {
            "interval_seconds": interval,
            "incident_interval_seconds": uptime.INCIDENT_UPTIME_INTERVAL_SECONDS,
            "stale_after_seconds": stale_after,
            "period_start": _iso(start),
            "period_end": _iso(now),
            "description": "Authentifizierte Prüfungen von Anmeldung und Modulen mit einem einzelnen Testkonto. Die Verfügbarkeit wird zeitgewichtet aus bestätigten Messungen berechnet; fehlende Messzeiträume bleiben unbekannt und reduzieren die Abdeckung. Ein einzelner fehlgeschlagener Versuch wird wiederholt und bei erfolgreichem Retry verworfen. Störungen fassen aufeinanderfolgende bestätigte Ausfälle zusammen. LANIS-Verfügbarkeit wird hier nicht gemessen.",
        },
    }


async def get_public_status() -> dict[str, Any]:
    """Coalesce public polling and retain a sanitized snapshot briefly."""
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
        ttl = 5.0 if result["current"]["status"] in {"down", "degraded"} else 30.0
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
