"""Self-service account export and deletion orchestration."""

import asyncio
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict

from . import auth_db
from .metrics import user_metrics_db
from .identity import canonicalize_user_id


@dataclass
class DeletionReport:
    success: bool
    deleted: Dict[str, int] = field(default_factory=dict)
    upstream_sph_data_deleted: bool = False


_lifecycle_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
_lifecycle_guard = asyncio.Lock()
_deleted_account_markers: Dict[str, float] = {}
_DELETION_MARKER_TTL_SECONDS = 60 * 60


def account_deletion_is_recent(user_id: str) -> bool:
    """Identify deletions during the remaining lifetime of admin JWTs."""
    now = time.monotonic()
    expired = [
        uid
        for uid, deleted_at in _deleted_account_markers.items()
        if now - deleted_at >= _DELETION_MARKER_TTL_SECONDS
    ]
    for uid in expired:
        _deleted_account_markers.pop(uid, None)
    user_id = canonicalize_user_id(user_id)
    deleted_at = _deleted_account_markers.get(user_id)
    if deleted_at is None:
        return False
    return True


def clear_account_deletion_marker(user_id: str) -> None:
    """Allow new activity after a fresh successful login."""
    _deleted_account_markers.pop(canonicalize_user_id(user_id), None)


async def account_lifecycle_lock(user_id: str) -> asyncio.Lock:
    """Return the per-account lock shared by requests and background writes."""
    user_id = canonicalize_user_id(user_id)
    async with _lifecycle_guard:
        return _lifecycle_locks.setdefault(user_id, asyncio.Lock())


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
        "preferences": persisted["preferences"],
        "notification_preferences": persisted["notification_preferences"],
        "custom_lessons": persisted["custom_lessons"],
        "class_link_overrides": persisted["class_link_overrides"],
        "push_subscriptions": persisted["push_subscriptions"],
        "whatsapp": persisted["whatsapp"],
        "notification_state": persisted["notification_state"],
        "cached_data": cached_data,
        "activity": activity,
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
    user_id: str, school_id: str | None = None, username: str | None = None
) -> DeletionReport:
    """Delete LANIS-held account data while preserving data in Schulportal Hessen."""
    user_id = canonicalize_user_id(user_id)
    lock = await account_lifecycle_lock(user_id)
    async with lock:
        credential = await auth_db.get_refresh_token_by_user_id(user_id)
        if credential:
            school_id = credential["school_id"]
            username = credential["username"]

        from .api import (
            clear_account_export_state,
            clear_whatsapp_user_queue,
            semantic_engine,
            sessions,
            task_queue,
            whatsapp_task_queue,
        )

        await task_queue.cancel_user_tasks(user_id)
        await whatsapp_task_queue.cancel_user_tasks(user_id)
        await clear_whatsapp_user_queue(user_id)
        runtime_counts = await sessions.delete_user_runtime_data(user_id)
        semantic_engine.invalidate(user_id)
        await clear_account_export_state(user_id)
        # Delete the metrics store first. If the second database fails, a retry
        # can safely repeat the idempotent metrics deletion while the auth row
        # still exists to authenticate the retry.
        metrics_counts = {"users": 0, "activity_events": 0}
        if school_id and username:
            metrics_counts = await user_metrics_db.delete_user_data(
                school_id, username, user_id
            )
        auth_counts = await auth_db.delete_user_data(user_id)
        account_deletion_is_recent(user_id)  # Prune expired tombstones.
        _deleted_account_markers[user_id] = time.monotonic()
        return DeletionReport(
            success=True,
            deleted={
                **{f"runtime_{key}": value for key, value in runtime_counts.items()},
                **{f"auth_{key}": value for key, value in auth_counts.items()},
                **{f"metrics_{key}": value for key, value in metrics_counts.items()},
            },
            upstream_sph_data_deleted=False,
        )
