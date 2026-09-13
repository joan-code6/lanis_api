"""Request-local transport facts, also available when applets catch exceptions."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime

import requests


@dataclass
class TransportObservation:
    fail_fast: bool = False
    cache_path: str | None = None
    cache_user_id: str | None = None
    cache_version: int | None = None
    unavailable: bool = False
    security_failure: bool = False
    cached_timestamps: list[datetime] = field(default_factory=list)


observation: ContextVar[TransportObservation | None] = ContextVar(
    "schulportal_transport_observation", default=None
)


class ObservedSession(requests.Session):
    def request(self, method, url, **kwargs):
        current = observation.get()
        if current and current.fail_fast and current.unavailable:
            raise requests.ConnectionError("Earlier Schulportal request unavailable")
        # An unavailable upstream must eventually return control to the caller.
        kwargs.setdefault("timeout", (5, 15))
        try:
            response = super().request(method, url, **kwargs)
        except requests.exceptions.SSLError:
            if current:
                current.security_failure = True
            raise
        except (requests.Timeout, requests.ConnectionError):
            if current:
                current.unavailable = True
            raise
        if current:
            if response.status_code in (401, 403):
                current.security_failure = True
            elif response.status_code >= 500:
                current.unavailable = True
        return response
