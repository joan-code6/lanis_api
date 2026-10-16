"""Android FCM transport; reuses notification queues and account cleanup."""
from __future__ import annotations

import os
import re
import threading
from typing import Any

PREFIX = "fcm://"
MARKER = "android-fcm-v1"
BINDING = re.compile(r"^[a-f0-9-]{36}$")
TOKEN = re.compile(r"^[A-Za-z0-9_:\-]{20,4096}$")
_lock = threading.Lock()
_credentials = None
_credentials_path = None


def android_push_configured() -> bool:
    return bool(os.getenv("LANIS_FIREBASE_PROJECT_ID", "").strip()
                and os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip())


def android_subscription(token: str, binding: str = "00000000-0000-0000-0000-000000000000") -> dict[str, Any]:
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ValueError("Invalid Android registration token")
    if not BINDING.fullmatch(binding):
        raise ValueError("Invalid Android session binding")
    return {"endpoint": PREFIX + token, "keys": {"p256dh": MARKER, "auth": binding}}


def is_android_subscription(subscription: dict[str, Any]) -> bool:
    endpoint = subscription.get("endpoint", "")
    return (isinstance(endpoint, str) and endpoint.startswith(PREFIX)
            and bool(TOKEN.fullmatch(endpoint[len(PREFIX):]))
            and isinstance(subscription.get("keys"), dict)
            and subscription["keys"].get("p256dh") == MARKER
            and bool(BINDING.fullmatch(str(subscription["keys"].get("auth", "")))))


class AndroidPushError(Exception):
    def __init__(self, permanent: bool = False):
        super().__init__("Android notification delivery failed")
        self.permanent = permanent


def _access_token() -> str:
    # Lock credential refresh because one notification cycle delivers concurrently.
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    global _credentials, _credentials_path
    path = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    with _lock:
        if _credentials is None or path != _credentials_path:
            _credentials = service_account.Credentials.from_service_account_file(
                path, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
            _credentials_path = path
        if not _credentials.valid:
            transport = Request()
            def bounded_request(*args, **kwargs):
                kwargs["timeout"] = 15
                return transport(*args, **kwargs)
            _credentials.refresh(bounded_request)
        return str(_credentials.token)


def send_android_push(subscription: dict[str, Any], payload: dict[str, Any]) -> None:
    import requests
    if not is_android_subscription(subscription):
        raise ValueError("Invalid Android subscription")
    project = os.environ["LANIS_FIREBASE_PROJECT_ID"].strip()
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,62}", project):
        raise ValueError("Invalid Firebase project ID")
    # A data message reaches our native service even with the WebView closed.
    # It displays the notification immediately, then schedules widget refresh.
    data = {key: str(payload.get(key) or "")[:limit] for key, limit in (("title", 100), ("body", 350), ("url", 200), ("tag", 100))}
    data["binding"] = subscription["keys"]["auth"]
    response = requests.post(
        f"https://fcm.googleapis.com/v1/projects/{project}/messages:send",
        headers={"Authorization": "Bearer " + _access_token()},
        json={"message": {
            "token": subscription["endpoint"][len(PREFIX):], "data": data,
            "android": {"priority": "high", "ttl": "3600s"},
        }}, timeout=15,
    )
    if response.ok:
        return
    try:
        details = response.json().get("error", {}).get("details", [])
        codes = {item.get("errorCode") for item in details if isinstance(item, dict)}
    except (ValueError, AttributeError, TypeError):
        codes = set()
    # Don't delete devices for permission/configuration/quota failures.
    raise AndroidPushError(permanent="UNREGISTERED" in codes)
