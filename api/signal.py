"""Signal transport through an operator-owned signal-cli-rest-api bridge."""

import json
import os
import re
from urllib.parse import quote, urlsplit

import requests
from fastapi.concurrency import run_in_threadpool


def config():
    return (
        os.getenv("SIGNAL_BRIDGE_URL", "http://127.0.0.1:8081").rstrip("/"),
        os.getenv("SIGNAL_BOT_NUMBER", "").strip(),
    )


def configured():
    url, number = config()
    parsed = urlsplit(url)
    return bool(
        parsed.scheme in ("http", "https")
        and parsed.hostname
        and not parsed.username
        and re.fullmatch(r"\+[1-9]\d{6,14}", number)
    )


def headers():
    token = os.getenv("SIGNAL_BRIDGE_TOKEN", "").strip()
    return {"Authorization": "Bearer " + token} if token else {}


async def send_text(recipient, body, *, continuation_check=None):
    url, number = config()
    if not configured():
        raise RuntimeError("Signal is not configured")
    for start in range(0, len(body), 3500):
        if continuation_check and not await continuation_check():
            return

        def send(start=start):
            response = requests.post(
                url + "/v2/send",
                headers=headers(),
                json={
                    "number": number,
                    "recipients": [recipient],
                    "message": body[start : start + 3500],
                },
                timeout=(5, 30),
                allow_redirects=False,
            )
            try:
                response.raise_for_status()
                if response.is_redirect or response.json().get("errors"):
                    raise RuntimeError("Signal delivery failed")
            finally:
                response.close()

        await run_in_threadpool(send)


def incoming(payload):
    """Accept direct text messages, excluding groups, receipts and sent syncs."""
    envelope = payload.get("envelope", {}) if isinstance(payload, dict) else {}
    data = envelope.get("dataMessage") or {}
    sender = envelope.get("sourceNumber") or envelope.get("source")
    text = data.get("message")
    stamp = data.get("timestamp") or envelope.get("timestamp")
    if (
        data.get("groupInfo")
        or not isinstance(sender, str)
        or not re.fullmatch(r"\+[1-9]\d{6,14}", sender)
        or not isinstance(text, str)
        or not text.strip()
        or not isinstance(stamp, int)
    ):
        return None
    return (sender, str(stamp), text[:12000])


async def receive_loop():
    import asyncio
    import logging

    from websockets.asyncio.client import connect

    from .hosted_agents import receive

    tasks = set()
    try:
        while True:
            try:
                url, number = config()
                ws = (
                    ("wss" if url.startswith("https:") else "ws")
                    + url[url.index(":") :]
                    + "/v1/receive/"
                    + quote(number, safe="")
                )
                async with connect(
                    ws,
                    additional_headers=headers(),
                    max_size=256 * 1024,
                    open_timeout=10,
                ) as socket:
                    async for message in socket:
                        event = incoming(json.loads(message))
                        if not event:
                            continue
                        if event[2].strip().upper() == "STOP":
                            await receive("signal", *event)
                        elif len(tasks) < 8:
                            task = asyncio.create_task(receive("signal", *event))
                            tasks.add(task)

                            def done(task):
                                tasks.discard(task)
                                if not task.cancelled():
                                    task.exception()

                            task.add_done_callback(done)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect on transport or malformed event errors
                logging.getLogger(__name__).warning(
                    "Signal receiver disconnected; reconnecting"
                )
                await asyncio.sleep(5)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
