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


def _observe_response(current, response):
    if current is not None and response is not None:
        if response.status_code in (401, 403):
            current.security_failure = True
        elif response.status_code >= 500:
            current.unavailable = True


def observe_request(send):
    """Preserve outage/security facts across ordinary and pinned transports."""
    current = observation.get()
    if current and current.fail_fast and current.unavailable:
        raise requests.ConnectionError("Earlier Schulportal request unavailable")
    try:
        response = send()
    except requests.exceptions.SSLError:
        if current:
            current.security_failure = True
        raise
    except (requests.Timeout, requests.ConnectionError):
        if current:
            current.unavailable = True
        raise
    except requests.HTTPError as error:
        # Bounded document reads raise before returning failed responses.
        _observe_response(current, error.response)
        raise
    _observe_response(current, response)
    return response


class ObservedSession(requests.Session):
    def request(self, method, url, **kwargs):
        # An unavailable upstream must eventually return control to the caller.
        kwargs.setdefault("timeout", (5, 15))
        return observe_request(
            lambda: super(ObservedSession, self).request(method, url, **kwargs)
        )
