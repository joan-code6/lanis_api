"""Bounded, private HTTP snapshots for positively observed upstream outages."""

import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from fastapi.responses import Response
from fastapi.routing import APIRoute

from schulportal_hessen.tools.transport import TransportObservation, observation

RETENTION_SECONDS = 24 * 60 * 60
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_USER_ENTRIES = 100
READ_ROUTES = {
    "/apps",
    "/modules",
    "/benutzer",
    "/kalender",
    "/kalender/events",
    "/kalender/event/{event_id}",
    "/vertretungsplan",
    "/vertretungsplan/options",
    "/stundenplan",
    "/stundenplan/view",
    "/lerngruppen",
    "/meinunterricht",
    "/meinunterricht/attendance",
    "/meinunterricht/course/{course_id}",
    "/meinunterricht/entry",
    "/meinunterricht/weekly",
    "/meinunterricht/submissions",
    "/nachrichten/headers",
    "/nachrichten/{conversation_id}",
}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


DEPENDENT_SNAPSHOT_PATHS = {
    "/modules": {"/apps", "/modules", "/stundenplan/view"},
    "/benutzer": {"/benutzer", "/stundenplan/view"},
    "/vertretungsplan": {
        "/vertretungsplan",
        "/vertretungsplan/options",
        "/stundenplan/view",
    },
    "/stundenplan": {"/stundenplan", "/stundenplan/view"},
    "/meinunterricht": {
        "/meinunterricht",
        "/meinunterricht/attendance",
        "/meinunterricht/course/*",
        "/meinunterricht/entry",
        "/meinunterricht/weekly",
        "/meinunterricht/submissions",
        "/stundenplan",
        "/stundenplan/view",
    },
}


def utc_timestamp(value):
    return value.isoformat(timespec="seconds") + "Z" if value else None


@dataclass
class Snapshot:
    body: bytes
    fetched_at: datetime


class SnapshotStore:
    """Event-loop-owned store; no awaits during checks/mutations prevents races."""

    def __init__(self):
        self.entries = OrderedDict()
        self.user_versions = {}
        self.path_versions = {}
        self.total_bytes = 0

    def _remove(self, key):
        entry = self.entries.pop(key)
        self.total_bytes -= len(entry.body)

    def purge(self):
        cutoff = _utcnow() - timedelta(seconds=RETENTION_SECONDS)
        for key, entry in list(self.entries.items()):
            if entry.fetched_at < cutoff:
                self._remove(key)

    @staticmethod
    def _version_path(path):
        if path.startswith("/nachrichten/"):
            return "/nachrichten/*"
        if path.startswith("/meinunterricht/course/"):
            return "/meinunterricht/course/*"
        return path

    def version(self, user_id, path):
        version_path = self._version_path(path)
        return (
            self.user_versions.setdefault(user_id, 0),
            self.path_versions.setdefault((user_id, version_path), 0),
        )

    def invalidate(self, user_id):
        self.user_versions[user_id] = self.user_versions.get(user_id, 0) + 1
        for key in list(self.entries):
            if key[0] == user_id:
                self._remove(key)

    def invalidate_endpoint(self, user_id, endpoint):
        """Remove snapshots derived from an invalidated live-cache endpoint."""
        paths = DEPENDENT_SNAPSHOT_PATHS.get(endpoint, {endpoint})
        if endpoint.startswith("/nachrichten"):
            paths = {"/nachrichten/*"}
        for path in paths:
            version_path = self._version_path(path)
            key = (user_id, version_path)
            self.path_versions[key] = self.path_versions.get(key, 0) + 1
        for key in list(self.entries):
            if key[0] != user_id:
                continue
            path = key[1]
            if (
                path in paths
                or (
                    endpoint.startswith("/nachrichten")
                    and path.startswith("/nachrichten/")
                )
                or (
                    endpoint == "/meinunterricht"
                    and path.startswith("/meinunterricht/course/")
                )
            ):
                self._remove(key)

    def put(self, key, body, fetched_at, version):
        self.purge()
        if (
            self.version(key[0], key[1]) != version
            or len(body) > MAX_BODY_BYTES
            or _utcnow() - fetched_at > timedelta(seconds=RETENTION_SECONDS)
        ):
            return
        if key in self.entries:
            self._remove(key)
        self.entries[key] = Snapshot(body, fetched_at)
        self.total_bytes += len(body)
        owned = [candidate for candidate in self.entries if candidate[0] == key[0]]
        for candidate in owned[:-MAX_USER_ENTRIES]:
            self._remove(candidate)
        while self.total_bytes > MAX_TOTAL_BYTES:
            self._remove(next(iter(self.entries)))

    def get(self, key, version):
        self.purge()
        if self.version(key[0], key[1]) != version:
            return None
        return self.entries.get(key)

    def status(self, user_id):
        self.purge()
        timestamps = [
            entry.fetched_at for key, entry in self.entries.items() if key[0] == user_id
        ]
        return {
            "available": bool(timestamps),
            "last_successful_fetch_at": utc_timestamp(
                max(timestamps) if timestamps else None
            ),
            "snapshot_count": len(timestamps),
            "retention_seconds": RETENTION_SECONDS,
        }


snapshots = SnapshotStore()


def set_headers(response, fetched_at, cache_status):
    response.headers["X-LANIS-Cache"] = cache_status
    response.headers["X-LANIS-Fetched-At"] = utc_timestamp(fetched_at)
    response.headers["Cache-Control"] = "private, no-store"


class OutageCacheRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        if self.path not in READ_ROUTES or self.methods != {"GET"}:
            return original

        async def handler(request):
            # Import lazily: the application owns identity and session validation.
            from .api import local_auth_dependency

            token = request.headers.get("X-Session-Token")
            if not token:
                return await original(request)
            identity = await local_auth_dependency(token)
            key = (
                identity.user_id,
                request.url.path,
                tuple(
                    sorted(
                        (name, value)
                        for name, value in request.query_params.multi_items()
                        if name not in {"refresh", "force_refresh"}
                    )
                ),
            )
            version = snapshots.version(identity.user_id, request.url.path)
            current = TransportObservation()
            current.cache_path = self.path
            current.cache_user_id = identity.user_id
            current.cache_version = version
            current.fail_fast = snapshots.get(key, version) is not None
            reset = observation.set(current)
            response = None
            failure = None
            try:
                try:
                    response = await original(request)
                except Exception as exc:  # noqa: BLE001 - preserve FastAPI handling
                    failure = exc
            finally:
                observation.reset(reset)
            # Invalid JWT/session, permissions, TLS failures and ambiguous failures
            # must never be replaced with private cached data.
            forbidden = isinstance(failure, HTTPException) and failure.status_code < 500
            unavailable = (
                current.unavailable and not current.security_failure and not forbidden
            )
            if unavailable:
                entry = snapshots.get(key, version)
                # Revocation could have happened while upstream I/O was pending.
                if entry:
                    await local_auth_dependency(token)
                    entry = snapshots.get(key, version)
                if entry:
                    result = Response(entry.body, media_type="application/json")
                    set_headers(result, entry.fetched_at, "stale")
                    return result
            if failure:
                raise failure
            if (
                response.status_code == 200
                and not current.unavailable
                and not current.security_failure
            ):
                body = getattr(response, "body", b"")
                if len(body) <= MAX_BODY_BYTES:
                    try:
                        data = json.loads(body)
                    except (ValueError, TypeError):
                        data = None
                    if isinstance(data, dict) and data.get("success") is True:
                        fetched_at = (
                            min(current.cached_timestamps)
                            if current.cached_timestamps
                            else _utcnow()
                        )
                        snapshots.put(key, body, fetched_at, version)
                        set_headers(
                            response,
                            fetched_at,
                            "hit" if current.cached_timestamps else "fresh",
                        )
            return response

        return handler
