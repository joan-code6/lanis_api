"""Bounded HTTPS reads with public destination addresses pinned at connect time."""

import ipaddress
import socket
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.util.connection import create_connection

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024


def public_https_url(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or any(ord(c) <= 32 for c in url) or "\\" in url:
        raise ValueError("Invalid document URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        raise ValueError("Document URL must use HTTPS without credentials or fragments")
    host = parsed.hostname.encode("idna").decode("ascii")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = {entry[4][0] for entry in addresses}
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("Document destination must resolve to public addresses")
    return host, min(ips)


def fetch_document(session: requests.Session, url: str) -> requests.Response:
    host, address = public_https_url(url)

    class PinnedConnection(HTTPSConnection):
        def _new_conn(self):
            # Keep the original hostname for TLS/SNI and certificate checking,
            # while connecting to the address that passed the policy check.
            return create_connection(
                (address, self.port),
                self.timeout,
                source_address=self.source_address,
                socket_options=self.socket_options,
            )

    class PinnedAdapter(HTTPAdapter):
        def __init__(self):
            super().__init__(max_retries=0)
            self.pool = HTTPSConnectionPool(host, 443)
            self.pool.ConnectionCls = PinnedConnection

        def get_connection(self, url, proxies=None):
            return self.pool

        def get_connection_with_tls_context(
            self, request, verify, proxies=None, cert=None
        ):
            return self.pool

        def close(self):
            self.pool.close()
            super().close()

    # Prepare through the authenticated session, but isolate the transport so
    # concurrent applet calls cannot alter its adapters or proxy configuration.
    prepared = session.prepare_request(requests.Request("GET", url))
    with requests.Session() as transport:
        transport.trust_env = False
        transport.mount("https://", PinnedAdapter())
        response = transport.send(
            prepared,
            timeout=(5, 15),
            allow_redirects=False,
            stream=True,
        )
        try:
            if 300 <= response.status_code < 400:
                raise ValueError("Document redirects are not permitted")
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_content(64 * 1024):
                body.extend(chunk)
                if len(body) > MAX_DOCUMENT_BYTES:
                    raise ValueError("Document exceeds size limit")
            response._content = bytes(body)
            response._content_consumed = True
            session.cookies.update(response.cookies)
            return response
        finally:
            response.close()
