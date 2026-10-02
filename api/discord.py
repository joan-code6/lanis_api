"""Discord notifications for application events."""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import requests
from fastapi.concurrency import run_in_threadpool

logger = logging.getLogger("discord")

NEW_USER_WEBHOOK_ENV = "LANIS_NEW_USER_DISCORD_WEBHOOK_URL"
FEEDBACK_WEBHOOK_ENV = "LANIS_FEEDBACK_DISCORD_WEBHOOK_URL"
LEGACY_WEBHOOK_ENV = "LANIS_UPTIME_DISCORD_WEBHOOK_URL"
WEBHOOK_TIMEOUT_SECONDS = 10


def _escape_discord_markdown(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for marker in ("*", "_", "~", "`", "|", ">", "[", "]", "(", ")"):
        escaped = escaped.replace(marker, f"\\{marker}")
    return escaped


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


def _feedback_webhook_url() -> str | None:
    """Return the dedicated feedback webhook after validating its host/path."""
    value = (
        os.getenv(FEEDBACK_WEBHOOK_ENV)
        or os.getenv(NEW_USER_WEBHOOK_ENV)
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


def _send_feedback(
    webhook_url: str,
    report_id: int,
    category: str,
    title: str,
    details: str,
    submitter_user_id: str,
) -> None:
    category_label = {
        "feature": "Feature-Idee",
        "bug": "Fehler",
        "general": "Allgemeines Feedback",
    }.get(category, "Feedback")
    content = (
        f"📬 **Neues Feedback #{report_id} · {category_label}**\n"
        f"**{_escape_discord_markdown(title[:180])}**\n"
        f"{_escape_discord_markdown(details[:900])}\n"
        f"Eingereicht von `{_escape_discord_markdown(submitter_user_id)}` · "
        "[Im Admin-Portal öffnen](https://admin.lanis.arg-server.de/#feedback)"
    )
    response = requests.post(
        webhook_url,
        json={"content": content, "allowed_mentions": {"parse": []}},
        timeout=WEBHOOK_TIMEOUT_SECONDS,
    )
    response.raise_for_status()


async def notify_feedback(report: dict[str, object]) -> None:
    """Send a notification without making Discord failure a submission failure."""
    webhook_url = _feedback_webhook_url()
    if not webhook_url:
        return
    try:
        await run_in_threadpool(
            _send_feedback,
            webhook_url,
            int(report["id"]),
            str(report["category"]),
            str(report["title"]),
            str(report["details"]),
            str(report["submitter_user_id"]),
        )
    except Exception:
        logger.warning(
            "Could not deliver feedback notification to Discord", exc_info=True
        )


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
