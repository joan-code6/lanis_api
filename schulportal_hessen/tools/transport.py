"""Request-local transport facts, also available when applets catch exceptions."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests


@dataclass
class TransportObservation:
    fail_fast: bool = False
    cache_path: str | None = None
    cache_user_id: str | None = None
    cache_version: int | None = None
    unavailable: bool = False
    security_failure: bool = False
    expired_sessions: list = field(default_factory=list)
    cached_timestamps: list[datetime] = field(default_factory=list)


observation: ContextVar[TransportObservation | None] = ContextVar(
    "schulportal_transport_observation", default=None
)


class UpstreamSessionExpired(requests.RequestException):
    """The portal returned its login gate instead of module content."""


def is_login_gate(response, requested_url, *, stream=False):
    requested = urlsplit(requested_url)
    final = urlsplit(response.url or "")
    if requested.hostname != "start.schulportal.hessen.de":
        return False
    if requested.path in {"/", "/index.php", "/forward.php"}:
        return False
    destination = urlsplit(urljoin(requested_url, response.headers.get("Location", "")))
    if 300 <= response.status_code < 400 and (
        destination.hostname == "login.schulportal.hessen.de"
        or (
            requested.path == "/startseite.php"
            and destination.hostname == requested.hostname
            and destination.path == "/index.php"
        )
    ):
        return True
    return (
        final.hostname == "login.schulportal.hessen.de"
        or (
            requested.path == "/startseite.php"
            and final.hostname == requested.hostname
            and final.path == "/index.php"
            and bool(response.history)
        )
        or (
            final.hostname == requested.hostname
            and final.path == "/forward.php"
            and not stream
            and 'id="loginButton"' in response.text
            and "Login nötig" in response.text
        )
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


def check_portal_session(session, response, url, *, stream=False):
    """Detect expiry for both ordinary requests and bounded document reads."""
    current = observation.get()
    # Mein Unterricht can return a database-selection error when the
    # session is gone, rather than redirecting to the login gate. Probe the
    # apps endpoint to distinguish expiry from a genuine module failure.
    parsed = urlsplit(url)
    if (
        parsed.hostname == "start.schulportal.hessen.de"
        and parsed.path != "/startseite.php"
        and not stream
        and "attachment" not in response.headers.get("Content-Disposition", "").lower()
        and "text/html" in response.headers.get("Content-Type", "")
        and "Datenbankauswahl nicht möglich" in response.text
    ):
        try:
            probe = session.get(
                "https://start.schulportal.hessen.de/startseite.php?a=ajax&f=apps"
            )
        except requests.RequestException:
            response.close()
            raise
        probe.close()
    if is_login_gate(response, url, stream=stream):
        session.upstream_expired = True
        if current:
            current.security_failure = True
            if session not in current.expired_sessions:
                current.expired_sessions.append(session)
        response.close()
        raise UpstreamSessionExpired("Schulportal session expired; login required")
    return response


class ObservedSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", (5, 15))

        def send():
            response = super(ObservedSession, self).request(method, url, **kwargs)
            return check_portal_session(
                self, response, url, stream=kwargs.get("stream", False)
            )

        return observe_request(send)
