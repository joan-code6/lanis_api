"""Request-local transport facts, also available when applets catch exceptions."""

import codecs
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import UnicodeDammit


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
    if (
        requested.path == "/startseite.php"
        and 300 <= response.status_code < 400
        and (
            destination.hostname == "login.schulportal.hessen.de"
            or (
                destination.hostname == requested.hostname
                and destination.path == "/index.php"
            )
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


def set_html_encoding(response, *, stream=False):
    """Decode portal HTML from its bytes before callers access response.text.

    Some portal pages advertise UTF-8 while emitting legacy Western European
    bytes. Requests replaces those bytes irreversibly unless we select the
    encoding first. Keep JSON, attachments and streaming responses untouched.
    """
    if stream or "attachment" in response.headers.get("Content-Disposition", "").lower():
        return
    if "text/html" not in response.headers.get("Content-Type", "").lower():
        return
    content = response.content
    if not isinstance(content, bytes) or not content:
        return
    try:
        content.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Honor explicit HTTP charsets before statistical detection. Requests'
        # implicit Latin-1 default is not an explicit declaration.
        match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)",
                          response.headers.get("Content-Type", ""), re.I)
        if match:
            declared = match.group(1)
            try:
                codec = codecs.lookup(declared).name
                content.decode(declared, errors="strict")
            except (LookupError, UnicodeError):
                pass
            else:
                if codec not in {"utf-8", "utf-8-sig"}:
                    response.encoding = declared
                    return
        detected = UnicodeDammit(content, is_html=True)
        if detected.original_encoding and not detected.contains_replacement_characters:
            response.encoding = detected.original_encoding
    else:
        response.encoding = "utf-8-sig"


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
        set_html_encoding(response, stream=kwargs.get("stream", False))
        # Mein Unterricht can return a database-selection error when the
        # session is gone, rather than redirecting to the login gate. Probe the
        # apps endpoint to distinguish expiry from a genuine module failure.
        parsed = urlsplit(url)
        if (
            parsed.hostname == "start.schulportal.hessen.de"
            and parsed.path != "/startseite.php"
            and not kwargs.get("stream", False)
            and "attachment"
            not in response.headers.get("Content-Disposition", "").lower()
            and "text/html" in response.headers.get("Content-Type", "")
            and "Datenbankauswahl nicht möglich" in response.text
        ):
            try:
                probe = self.get(
                    "https://start.schulportal.hessen.de/startseite.php?a=ajax&f=apps"
                )
            except requests.RequestException:
                response.close()
                raise
            probe.close()
        if is_login_gate(response, url, stream=kwargs.get("stream", False)):
            self.upstream_expired = True
            if current:
                current.security_failure = True
                if self not in current.expired_sessions:
                    current.expired_sessions.append(self)
            response.close()
            raise UpstreamSessionExpired("Schulportal session expired; login required")
        if current:
            if response.status_code in (401, 403):
                current.security_failure = True
            elif response.status_code >= 500:
                current.unavailable = True
        return response
