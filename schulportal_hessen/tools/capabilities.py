"""Per-session module capabilities checked before applet HTTP requests."""
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests


# Authentication, app discovery, encryption, profile bootstrap and external
# providers are infrastructure, not applets. Guard every native applet used by
# this SDK, including calls made through redirects or directly on the session.
APPLET_PATHS = frozenset(
    "/" + name + ".php"
    for name in (
        "kalender", "nachrichten", "meinunterricht", "vertretungsplan",
        "stundenplan", "dateispeicher", "dateiverteilung", "lerngruppen",
        "oberstufenwahl", "videokonferenz", "wahl", "usesheets",
    )
)


class ModuleUnavailable(requests.RequestException):
    """The account does not advertise the requested native applet."""


class CapabilityLookupFailed(requests.RequestException):
    def __init__(self, result):
        super().__init__("Could not establish Schulportal module availability")
        self.result = result


class ModuleCapabilities:
    def __init__(self, fetch_apps, portal_url, ttl_seconds=300):
        self._fetch_apps = fetch_apps
        self._portal_url = portal_url.rstrip("/") + "/"
        self._host = urlsplit(portal_url).hostname
        self._ttl = ttl_seconds
        self._lock = threading.RLock()
        self._paths = None
        self._expires = 0.0
        self._failure = None
        self._retry_at = 0.0

    def clear(self):
        with self._lock:
            self._paths = None
            self._expires = 0.0
            self._failure = None
            self._retry_at = 0.0

    def _parse(self, result):
        if not isinstance(result, dict) or not result.get("success"):
            raise CapabilityLookupFailed(result if isinstance(result, dict) else {
                "success": False, "error": "Invalid Schulportal app list", "error_kind": "upstream",
            })
        try:
            entries = result["data"]["entrys"]
            if not isinstance(entries, list):
                raise ValueError
            paths = set()
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("link"), str):
                    raise ValueError
                target = urlsplit(urljoin(self._portal_url, entry["link"]))
                if target.scheme == "https" and target.hostname == self._host:
                    paths.add(target.path)
            return frozenset(paths)
        except (KeyError, TypeError, ValueError):
            raise CapabilityLookupFailed({
                "success": False, "error": "Invalid Schulportal app list", "error_kind": "upstream",
            }) from None

    def remember(self, result):
        """Reuse successful app discovery performed by ordinary API callers."""
        with self._lock:
            try:
                paths = self._parse(result)
            except CapabilityLookupFailed:
                # Never allow a prior snapshot to mask newly observed bad auth
                # or invalid app data. The next guarded call must revalidate.
                self.clear()
                return
            self._paths = paths
            self._failure = None
            self._expires = time.monotonic() + self._ttl

    def require(self, url):
        target = urlsplit(urljoin(self._portal_url, url))
        if target.hostname != self._host or target.path not in APPLET_PATHS:
            return
        with self._lock:
            if self._paths is None or time.monotonic() >= self._expires:
                if self._failure is not None and time.monotonic() < self._retry_at:
                    raise CapabilityLookupFailed(self._failure)
                try:
                    paths = self._parse(self._fetch_apps())
                except CapabilityLookupFailed as error:
                    # Share a failed lookup with the current request fan-out;
                    # do not serialize a network timeout for every waiter.
                    self._paths = None
                    self._failure = error.result
                    self._retry_at = time.monotonic() + 1
                    raise
                self._failure = None
                self._paths = paths
                self._expires = time.monotonic() + self._ttl
            if target.path not in self._paths:
                raise ModuleUnavailable("Schulportal module unavailable for this account")
