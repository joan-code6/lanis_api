"""Bounded, private HTTP snapshots for positively observed upstream outages."""

import asyncio
import json
import logging
import sqlite3
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from fastapi.responses import Response
from fastapi.routing import APIRoute

from schulportal_hessen.tools.transport import TransportObservation, observation

RETENTION_SECONDS = 0  # Last successful snapshots have no time-based expiry.
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_USER_BYTES = 8 * 1024 * 1024
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
    "/meinunterricht/submissions/{detail_ref}",
    "/nachrichten/headers",
    "/nachrichten/{conversation_id}",
}


def _utcnow():
    """Return a naive UTC timestamp consistent with stored snapshot metadata."""
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
    "/meinunterricht/submissions": {
        "/meinunterricht/submissions",
        "/meinunterricht/submissions/*",
    },
    "/meinunterricht/submissions/detail": {
        "/meinunterricht/submissions",
        "/meinunterricht/submissions/*",
    },
}


def utc_timestamp(value):
    """Serialize a stored UTC timestamp for response metadata."""
    return value.isoformat(timespec="seconds") + "Z" if value else None


@dataclass
class Snapshot:
    body: bytes
    fetched_at: datetime


class SnapshotStore:
    """Event-loop-owned store; no awaits during checks/mutations prevents races."""

    def __init__(self):
        """Initialize bounded in-memory snapshots and optional persistence state."""
        self.database_path = None
        self._writer = None
        self._user_deletions = {}
        self.entries = OrderedDict()
        self.user_entries = {}
        self.user_bytes = {}
        self.user_versions = {}
        self.path_versions = {}
        self.total_bytes = 0
        self._generation = 0

    def enable_persistence(self, database_path):
        """Store encrypted private response bodies alongside the auth database."""
        from .auth_db import _credential_key, _decrypt_password

        try:
            _credential_key()
        except RuntimeError:
            logging.getLogger(__name__).warning(
                "Snapshot persistence disabled: encryption key unavailable"
            )
            return
        with sqlite3.connect(database_path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS outage_snapshot_revocations ("
                "user_id TEXT PRIMARY KEY, revoked_at TEXT NOT NULL)"
            )
            revocations = {
                user_id: datetime.fromisoformat(revoked_at)
                for user_id, revoked_at in db.execute(
                    "SELECT user_id, revoked_at FROM outage_snapshot_revocations"
                )
            }
            db.execute(
                "CREATE TABLE IF NOT EXISTS outage_snapshots (key TEXT PRIMARY KEY, body TEXT, fetched_at TEXT)"
            )
            rows = db.execute(
                "SELECT key, body, fetched_at FROM outage_snapshots"
            ).fetchall()
            # Production uses the initialized auth database. Standalone stores
            # without a session table can still be used by library callers.
            has_sessions = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'refresh_tokens'"
            ).fetchone()
            active_users = (
                {
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT user_id FROM refresh_tokens WHERE expires_at > ?",
                        (_utcnow(),),
                    )
                }
                if has_sessions
                else None
            )
        parsed_keys = {}
        protected_rows = set()
        for raw_key, body, fetched_at in rows:
            try:
                user_id, path, params = json.loads(raw_key)
                if not isinstance(user_id, str) or not isinstance(path, str):
                    raise TypeError("Invalid snapshot identity")
                if active_users is not None and user_id not in active_users:
                    continue
                if not isinstance(params, list) or any(
                    not isinstance(pair, list)
                    or len(pair) != 2
                    or any(not isinstance(value, str) for value in pair)
                    for pair in params
                ):
                    raise ValueError("Invalid snapshot parameters")
                key = (user_id, path, tuple(tuple(pair) for pair in params))
                timestamp = datetime.fromisoformat(fetched_at)
                if timestamp.tzinfo:
                    timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
                if user_id in revocations and timestamp <= revocations[user_id]:
                    continue
                try:
                    plaintext = _decrypt_password(body).encode()
                except RuntimeError:
                    # Failed authentication cannot distinguish corruption from a
                    # wrong deployment key. Preserve ciphertext for recovery.
                    protected_rows.add(raw_key)
                    logging.getLogger(__name__).warning(
                        "Leaving undecryptable snapshot on disk"
                    )
                    continue
                json.loads(plaintext)
                self.put(key, plaintext, timestamp, self.version(user_id, path))
                parsed_keys[raw_key] = key
            except (RuntimeError, ValueError, TypeError, AttributeError):
                logging.getLogger(__name__).warning(
                    "Dropping unreadable outage snapshot"
                )
        self.database_path = database_path
        # Reuse validated keys; corrupt rows must not break the cleanup pass.
        with sqlite3.connect(database_path) as db:
            for raw_key, _, _ in rows:
                if (
                    raw_key not in protected_rows
                    and parsed_keys.get(raw_key) not in self.entries
                ):
                    db.execute("DELETE FROM outage_snapshots WHERE key = ?", (raw_key,))

        self._writer = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="outage-snapshots"
        )

    def _persist(self, key, entry=None):
        """Queue an ordered disk update without blocking the event loop."""
        if self._writer is None:
            return
        future = self._writer.submit(self._write_snapshot, key, entry)
        future.add_done_callback(self._write_completed)

    def _write_completed(self, future):
        """Log background failures without retaining unrelated write exceptions."""
        error = future.exception()
        if error:
            logging.getLogger(__name__).warning("Outage snapshot persistence failed")

    async def flush(self, user_id=None):
        """Wait for writes; report only the requested user's deletion failure."""
        deletion = self._user_deletions.get(user_id)
        if deletion is not None:
            await asyncio.wrap_future(deletion)
            if self._user_deletions.get(user_id) is deletion:
                self._user_deletions.pop(user_id, None)
        if self._writer is not None:
            await asyncio.wrap_future(self._writer.submit(lambda: None))

    def _delete_persisted_user(self, user_id):
        """Delete all persisted rows for one user in the writer thread."""
        with sqlite3.connect(self.database_path) as db:
            db.execute(
                "DELETE FROM outage_snapshots WHERE "
                "CASE WHEN json_valid(key) THEN json_extract(key, '$[0]') END = ?",
                (user_id,),
            )

    async def close(self):
        """Drain pending deletions and writes before shutting down the writer."""
        try:
            for user_id in list(self._user_deletions):
                await self.flush(user_id)
            await self.flush()
        finally:
            if self._writer is not None:
                await asyncio.to_thread(self._writer.shutdown, wait=True)
                self._writer = None

    def _write_snapshot(self, key, entry=None):
        """Encrypt and commit one snapshot update or deletion in the worker."""
        if not self.database_path:
            return
        from .auth_db import _encrypt_password

        with sqlite3.connect(self.database_path) as db:
            if entry is None:
                db.execute(
                    "DELETE FROM outage_snapshots WHERE key = ?", (json.dumps(key),)
                )
            else:
                # Serialize this check with revocation's auth transaction: a
                # delayed pre-logout writer must not overwrite a newer snapshot.
                db.execute("BEGIN IMMEDIATE")
                cutoff = db.execute(
                    "SELECT revoked_at FROM outage_snapshot_revocations WHERE user_id = ?",
                    (key[0],),
                ).fetchone()
                if cutoff and entry.fetched_at <= datetime.fromisoformat(cutoff[0]):
                    return
                db.execute(
                    "INSERT OR REPLACE INTO outage_snapshots VALUES (?, ?, ?)",
                    (
                        json.dumps(key),
                        _encrypt_password(entry.body.decode()),
                        entry.fetched_at.isoformat(),
                    ),
                )

    def _discard_memory(self, key):
        """Remove one entry and keep global and per-user accounting consistent."""
        entry = self.entries.pop(key)
        user_id = key[0]
        self.total_bytes -= len(entry.body)
        self.user_bytes[user_id] -= len(entry.body)
        owned = self.user_entries[user_id]
        owned.pop(key)
        if not owned:
            self.user_entries.pop(user_id)
            self.user_bytes.pop(user_id)

    def _remove(self, key):
        """Queue deletion of a snapshot and update memory accounting."""
        self._persist(key)
        self._discard_memory(key)

    def purge(self):
        # Keep the last successful response through outages of any duration.
        # Size bounds and account deletion still remove private data.
        """Retain snapshots across long outages; size bounds govern eviction."""
        return

    @staticmethod
    def _version_path(path):
        """Group dynamic detail routes for invalidation of related requests."""
        if path.startswith("/nachrichten/"):
            return "/nachrichten/*"
        if path.startswith("/meinunterricht/course/"):
            return "/meinunterricht/course/*"
        if path.startswith("/meinunterricht/submissions/"):
            return "/meinunterricht/submissions/*"
        return path

    def version(self, user_id, path):
        """Return user and endpoint generations used to reject obsolete writes."""
        version_path = self._version_path(path)
        if user_id not in self.user_versions:
            self._generation += 1
            self.user_versions[user_id] = self._generation
        return (
            self.user_versions[user_id],
            self.path_versions.setdefault((user_id, version_path), 0),
        )

    def advance_path_version(self, user_id, path):
        """Reject older writes while retaining the last successful outage snapshot."""
        key = (user_id, self._version_path(path))
        self.path_versions[key] = self.path_versions.get(key, 0) + 1

    def invalidate(self, user_id):
        # Keep every user generation in the same monotonically increasing
        # sequence. Deletion removes the per-user key, so its next allocation
        # must never reuse the just-invalidated value.
        """Revoke a user generation and queue deletion of all owned snapshots."""
        self._generation += 1
        self.user_versions[user_id] = self._generation
        for key in list(self.entries):
            if key[0] == user_id:
                self._remove(key)
        if self._writer is not None:
            # One final user-scoped operation also deletes rows left by older failed writes.
            deletion = self._writer.submit(self._delete_persisted_user, user_id)
            deletion.add_done_callback(self._write_completed)
            self._user_deletions[user_id] = deletion

    def delete_user_data(self, user_id):
        """Remove user-owned snapshots and version keys without reopening races."""
        count = sum(1 for key in self.entries if key[0] == user_id)
        self.invalidate(user_id)
        self.user_versions.pop(user_id, None)
        for key in [key for key in self.path_versions if key[0] == user_id]:
            self.path_versions.pop(key, None)
        return count

    def invalidate_endpoint(self, user_id, endpoint, *, preserve=False):
        """Remove snapshots derived from an invalidated live-cache endpoint."""
        paths = DEPENDENT_SNAPSHOT_PATHS.get(endpoint, {endpoint})
        if endpoint.startswith("/nachrichten"):
            paths = {"/nachrichten/*"}
        for path in paths:
            version_path = self._version_path(path)
            key = (user_id, version_path)
            self.path_versions[key] = self.path_versions.get(key, 0) + 1
        if preserve:
            return
        for key in list(self.entries):
            if key[0] != user_id:
                continue
            path = key[1]
            if (
                path in paths
                or any(
                    pattern.endswith("/*") and path.startswith(pattern[:-1])
                    for pattern in paths
                )
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
        """Store a successful snapshot only if its request generation is current."""
        self.purge()
        if self.version(key[0], key[1]) != version or len(body) > min(
            MAX_BODY_BYTES, MAX_USER_BYTES
        ):
            return
        entry = Snapshot(body, fetched_at)
        # Queue replacements in order; a failed SQL transaction retains the disk copy.
        self._persist(key, entry)
        if key in self.entries:
            self._discard_memory(key)
        self.entries[key] = entry
        self.total_bytes += len(body)
        user_id = key[0]
        self.user_bytes[user_id] = self.user_bytes.get(user_id, 0) + len(body)
        owned = self.user_entries.setdefault(user_id, OrderedDict())
        owned[key] = None
        # A user's writes can evict only that user's oldest successful snapshots.
        while (
            len(owned) > MAX_USER_ENTRIES or self.user_bytes[user_id] > MAX_USER_BYTES
        ):
            self._remove(next(iter(owned)))

    def get(self, key, version):
        """Return a snapshot only for the currently authorized request generation."""
        self.purge()
        if self.version(key[0], key[1]) != version:
            return None
        return self.entries.get(key)

    def status(self, user_id):
        """Describe available snapshots and their latest successful fetch time."""
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
            "body_bytes": self.user_bytes.get(user_id, 0),
            "max_body_bytes": MAX_USER_BYTES,
            "max_snapshot_count": MAX_USER_ENTRIES,
        }

    def export_user_data(self, user_id):
        """Return JSON-safe private outage snapshots for account export."""
        self.purge()
        result = []
        for key, entry in self.entries.items():
            if key[0] != user_id:
                continue
            try:
                body = json.loads(entry.body)
            except (TypeError, ValueError):
                continue
            result.append(
                {
                    "endpoint": key[1],
                    "params": list(key[2]),
                    "fetched_at": utc_timestamp(entry.fetched_at),
                    "data": body,
                }
            )
        return result


snapshots = SnapshotStore()


def set_headers(response, fetched_at, cache_status):
    """Label private cache responses with their origin and original fetch time."""
    response.headers["X-LANIS-Cache"] = cache_status
    response.headers["X-LANIS-Fetched-At"] = utc_timestamp(fetched_at)
    response.headers["Cache-Control"] = "private, no-store"


class OutageCacheRoute(APIRoute):
    def get_route_handler(self):
        """Wrap allowlisted reads with authenticated outage snapshot handling."""
        original = super().get_route_handler()
        if self.path not in READ_ROUTES or self.methods != {"GET"}:
            return original

        async def handler(request):
            # Import lazily: the application owns identity and session validation.
            """Observe upstream failures and serve a private snapshot when permitted."""
            from .api import local_auth_dependency

            token = request.headers.get("X-Session-Token")
            if not token:
                return await original(request)
            identity = await local_auth_dependency(token)
            request.state.lanis_auth = identity
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
            if self.path in {
                "/meinunterricht/submissions",
                "/meinunterricht/submissions/{detail_ref}",
            } and request.query_params.get("refresh", "").lower() in {
                "true",
                "1",
                "on",
                "yes",
            }:
                snapshots.advance_path_version(identity.user_id, request.url.path)
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
                    request.state.lanis_auth = await local_auth_dependency(token)
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
