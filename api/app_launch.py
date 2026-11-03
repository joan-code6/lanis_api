"""Resolve portal-owned redirects without starting OAuth outside the browser."""
from urllib.parse import urljoin, urlparse


def browser_launch_url(client, portal_url: str) -> str:
    current = portal_url
    for _ in range(10):
        parsed = urlparse(current)
        if parsed.scheme != "https" or parsed.hostname not in {
            "start.schulportal.hessen.de", "login.schulportal.hessen.de",
        }:
            raise ValueError("Invalid portal launch URL")
        response = client.session.get(
            current, allow_redirects=False, timeout=15, stream=True,
        )
        try:
            response.raise_for_status()
            if not response.is_redirect:
                raise ValueError("Portal did not provide an external app destination")
            location = response.headers.get("Location")
            if not location:
                raise ValueError("Portal redirect has no destination")
            destination = urljoin(current, location)
        finally:
            response.close()
        target = urlparse(destination)
        if target.scheme != "https" or not target.hostname or target.username or target.password:
            raise ValueError("Invalid app destination")
        if target.hostname not in {"start.schulportal.hessen.de", "login.schulportal.hessen.de"}:
            return destination
        current = destination
    raise ValueError("Too many portal redirects")
