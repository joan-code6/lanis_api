"""Discord notifications for application events."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import requests
from fastapi.concurrency import run_in_threadpool

logger = logging.getLogger("discord")

NEW_USER_WEBHOOK_ENV = "LANIS_NEW_USER_DISCORD_WEBHOOK_URL"
LEGACY_WEBHOOK_ENV = "LANIS_UPTIME_DISCORD_WEBHOOK_URL"
WEBHOOK_TIMEOUT_SECONDS = 10


def _webhook_url() -> str | None:
    """Return the configured Discord webhook, rejecting non-Discord URLs."""
    value = (
        os.getenv(NEW_USER_WEBHOOK_ENV)
        or os.getenv(LEGACY_WEBHOOK_ENV)
        or ""
    ).strip()
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "discord.com"
        or not parsed.path.startswith("/api/webhooks/")
    ):
        return None
    return value


def _send_new_user(webhook_url: str, school_id: str, username: str) -> None:
    response = requests.post(
        webhook_url,
        json={
            "content": f"New LANIS user: `{username}` (school `{school_id}`)",
            "allowed_mentions": {"parse": []},
        },
        timeout=WEBHOOK_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


async def notify_new_user(school_id: str, username: str) -> None:
    """Send a new-user notification without blocking or breaking login work."""
    webhook_url = _webhook_url()
    if not webhook_url:
        return
    try:
        await run_in_threadpool(_send_new_user, webhook_url, school_id, username)
    except Exception:
        logger.warning("Could not deliver new-user notification to Discord", exc_info=True)
