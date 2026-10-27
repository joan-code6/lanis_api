"""Self-service account export and deletion orchestration."""

import asyncio
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict

from . import auth_db, feedback_db, agent_store
from .metrics import user_metrics_db
from .identity import canonicalize_user_id


@dataclass
class DeletionReport:
    success: bool
    deleted: Dict[str, int] = field(default_factory=dict)
    upstream_sph_data_deleted: bool = False


class AccountDeletionCleanupError(RuntimeError):
    """Account rows were removed, but cached attachment cleanup is incomplete."""


class AccountDeletionSessionChanged(RuntimeError):
    """Recovery or login revoked the session that requested deletion."""


class AccountLifecycleLock:
    """Allow concurrent account work, but drain it before exclusive changes.

    Login, recovery and deletion use the default exclusive context. Ordinary
    requests and background jobs use shared contexts. Waiting exclusive work
    blocks new shared entrants so deletion cannot starve behind page fan-out.
    State changes are synchronous within the event loop, including release.
    """

    def __init__(self):
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0
        self._changed = asyncio.Event()

    def _wake(self):
        previous = self._changed
        self._changed = asyncio.Event()
        previous.set()

    async def acquire(self):
        self._waiting_writers += 1
        try:
            while self._writer or self._readers:
                await self._changed.wait()
            self._writer = True
            return True
        finally:
            self._waiting_writers -= 1
            self._wake()

    def release(self):
        if not self._writer:
            raise RuntimeError("Account lifecycle lock is not held")
        self._writer = False
        self._wake()

    def locked(self):
        return self._writer or bool(self._readers)

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        self.release()

    def shared(self):
        return _AccountOperation(self)


class _AccountOperation:
    """One shared lease; it also supports manual streaming-response release."""

    def __init__(self, lifecycle):
        self._lifecycle = lifecycle
        self._held = False

    async def acquire(self):
        lifecycle = self._lifecycle
        if self._held:
            raise RuntimeError("Account operation is already held")
        while lifecycle._writer or lifecycle._waiting_writers:
            await lifecycle._changed.wait()
        lifecycle._readers += 1
        self._held = True
        return True

    def release(self):
        if not self._held:
            raise RuntimeError("Account operation is not held")
        self._held = False
        self._lifecycle._readers -= 1
        self._lifecycle._wake()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        self.release()


_lifecycle_locks: weakref.WeakValueDictionary[str, AccountLifecycleLock] = (
    weakref.WeakValueDictionary()
)
_lifecycle_guard = asyncio.Lock()

# Bulk admin reads take exclusive access; deletions share access with each other.
admin_bulk_read_gate = AccountLifecycleLock()


async def account_deletion_is_recent(user_id: str) -> bool:
    """Identify deletions during the remaining lifetime of admin JWTs."""
    return await auth_db.account_deletion_marker_is_active(user_id)


async def clear_account_deletion_marker(user_id: str) -> None:
    """Allow new activity after a fresh successful login."""
    await auth_db.clear_account_deletion_marker(user_id)


async def account_lifecycle_lock(user_id: str) -> AccountLifecycleLock:
    """Return the per-account gate for ordinary work and exclusive changes."""
    user_id = canonicalize_user_id(user_id)
    async with _lifecycle_guard:
        return _lifecycle_locks.setdefault(user_id, AccountLifecycleLock())


async def build_account_export(user_id: str) -> dict:
    """Build a JSON-safe export without returning authentication secrets."""
    user_id = canonicalize_user_id(user_id)
    persisted = await auth_db.get_user_account_data(user_id)
    credential = persisted["account"]
    profile = {}
    activity = {
        "login_count": 0,
        "session_count": 0,
        "total_active_seconds": 0,
        "events": [],
    }
    if credential["school_id"] and credential["username"]:
        metrics = await user_metrics_db.get_account_data(
            credential["school_id"], credential["username"], user_id
        )
        profile = metrics["profile"]
        activity = metrics["activity"]

    # Import lazily: api.py imports this module and owns the runtime session manager.
    from .api import sessions

    cached_data = await sessions.export_user_cache(user_id)
    return {
        "schema_version": 1,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "account": credential,
        "profile": profile,
        "feedback": await feedback_db.get_account_data(user_id),
        "agents": await agent_store.connections(user_id),
        "hosted_agents": await export_hosted_agents(user_id),
        "preferences": persisted["preferences"],
        "notification_preferences": persisted["notification_preferences"],
        "custom_lessons": persisted["custom_lessons"],
        "class_link_overrides": persisted["class_link_overrides"],
        "push_subscriptions": persisted["push_subscriptions"],
        "whatsapp": persisted["whatsapp"],
        "notification_state": persisted["notification_state"],
        "cached_data": cached_data,
        "cached_file_references": persisted["cached_file_references"],
        "message_read_overrides": persisted["message_read_overrides"],
        "message_body_receipts": persisted["message_body_receipts"],
        "cached_downloads": persisted["cached_downloads"],
        "cache_sync": persisted["cache_sync"],
        "activity": activity,
        "analytics": persisted["analytics"],
        "excluded_secrets": [
            "SPH password",
            "refresh tokens",
            "access tokens",
            "SPH cookies",
            "push authentication keys",
            "WhatsApp hashes and decrypted history",
            "encryption keys",
            "admin credentials",
        ],
    }


async def delete_account_data(
    user_id: str,
    school_id: str | None = None,
    username: str | None = None,
    *,
    expected_session_id: str | None = None,
) -> DeletionReport:
    """Delete LANIS-held account data while preserving data in Schulportal Hessen."""
    user_id = canonicalize_user_id(user_id)
    lock = await account_lifecycle_lock(user_id)
    async with admin_bulk_read_gate.shared(), lock:
        if expected_session_id is not None:
            session = await auth_db.get_refresh_token_by_session_id(expected_session_id)
            if not session or canonicalize_user_id(session["user_id"]) != user_id:
                raise AccountDeletionSessionChanged("Deletion session was revoked")
        credential = await auth_db.get_refresh_metadata_by_user_id(user_id)
        if credential:
            school_id = credential["school_id"]
            username = credential["username"]

        from .api import (
            clear_account_export_state,
            clear_course_file_interests,
            clear_whatsapp_user_queue,
            semantic_engine,
            sessions,
            task_queue,
            whatsapp_task_queue,
        )

        await clear_course_file_interests(user_id)
        await task_queue.cancel_user_tasks(user_id)
        await whatsapp_task_queue.cancel_user_tasks(user_id)
        await clear_whatsapp_user_queue(user_id)
        runtime_counts = await sessions.delete_user_runtime_data(user_id)
        semantic_engine.invalidate(user_id)
        await clear_account_export_state(user_id)
        # Delete the metrics store first. If the second database fails, a retry
        # can safely repeat the idempotent metrics deletion while the auth row
        # still exists to authenticate the retry.
        feedback_counts = await feedback_db.delete_user_data(user_id)
        metrics_counts = {"users": 0, "activity_events": 0}
        if school_id and username:
            metrics_counts = await user_metrics_db.delete_user_data(
                school_id, username, user_id
            )
        try:
            auth_counts = await auth_db.delete_user_data(user_id)
        except OSError as error:
            raise AccountDeletionCleanupError(
                "Account data was deleted, but cached attachment cleanup is incomplete."
            ) from error
        return DeletionReport(
            success=True,
            deleted={
                **{f"feedback_{key}": value for key, value in feedback_counts.items()},
                **{f"runtime_{key}": value for key, value in runtime_counts.items()},
                **{f"auth_{key}": value for key, value in auth_counts.items()},
                **{f"metrics_{key}": value for key, value in metrics_counts.items()},
            },
            upstream_sph_data_deleted=False,
        )


async def export_hosted_agents(user_id):
    from . import hosted_store

    async with agent_store.database() as db:
        async with db.execute(
            "SELECT id,channel,history,history_at FROM hosted_links WHERE user_id=?",
            (user_id,),
        ) as cursor:
            links = await cursor.fetchall()
        async with db.execute(
            "SELECT id,grant_id,started,finished,status FROM hosted_runs WHERE user_id=?",
            (user_id,),
        ) as cursor:
            runs = await cursor.fetchall()
    return {
        "connections": [
            {
                "id": row["id"],
                "channel": row["channel"],
                "history": hosted_store.unseal(row["history"])
                if row["history"] and (row["history_at"] or 0) > time.time() - 86400
                else [],
                "schedules": await hosted_store.schedules(row["id"]),
            }
            for row in links
        ],
        "runs": [dict(row) for row in runs],
    }
