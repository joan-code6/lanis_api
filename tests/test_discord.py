import asyncio

from api import discord


def test_new_user_notification_posts_to_configured_webhook(monkeypatch):
    webhook = "https://discord.com/api/webhooks/id/token"
    calls = []

    class Response:
        def raise_for_status(self):
            pass

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setenv(discord.NEW_USER_WEBHOOK_ENV, webhook)
    monkeypatch.setattr(discord.requests, "post", post)

    asyncio.run(discord.notify_new_user("5201", "new.user"))

    assert calls == [
        (
            webhook,
            {
                "json": {
                    "content": "New LANIS user: `new.user` (school `5201`)",
                    "allowed_mentions": {"parse": []},
                },
                "timeout": 10,
            },
        )
    ]


def test_new_user_notification_does_not_raise_on_discord_failure(monkeypatch):
    monkeypatch.setenv(
        discord.NEW_USER_WEBHOOK_ENV,
        "https://discord.com/api/webhooks/id/token",
    )

    def post(*args, **kwargs):
        raise RuntimeError("Discord unavailable")

    monkeypatch.setattr(discord.requests, "post", post)

    asyncio.run(discord.notify_new_user("5201", "new.user"))
