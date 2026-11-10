"""Exercise real Requests/urllib3 buffering with controlled sockets and TLS."""

import socket
import threading
import time

import pytest
import requests
import urllib3.connection

from schulportal_hessen.tools import safe_fetch
from schulportal_hessen.tools.transport import TransportObservation, observation


@pytest.mark.parametrize("mode", ["ordinary", "slow-body", "slow-headers"])
def test_real_adapter_keeps_pinning_and_enforces_deadline_inside_reads(
    monkeypatch, mode
):
    client_socket, server_socket = socket.socketpair()
    stopped = threading.Event()
    targets, tls_hosts, received = [], [], []
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        ],
    )

    def create(address, *_args, **_kwargs):
        targets.append(address)
        return client_socket

    def wrap(**kwargs):
        tls_hosts.append(kwargs["server_hostname"])
        assert kwargs["cert_reqs"] == "CERT_REQUIRED"
        assert kwargs["assert_hostname"] is not False
        return kwargs["sock"], True

    monkeypatch.setattr(safe_fetch, "create_connection", create)
    monkeypatch.setattr(urllib3.connection, "_ssl_wrap_socket_and_match_hostname", wrap)
    monkeypatch.setattr(
        safe_fetch, "MAX_DOCUMENT_SECONDS", 0.15 if mode != "ordinary" else 5
    )

    def serve():
        try:
            request = b""
            server_socket.settimeout(2)
            while b"\r\n\r\n" not in request:
                request += server_socket.recv(4096)
            received.append(request)
            headers = (
                b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\n"
            )
            if mode == "ordinary":
                server_socket.sendall(headers + b"plan")
                return
            if mode == "slow-body":
                server_socket.sendall(headers.replace(b"Length: 4", b"Length: 65536"))
            # Always stop the fixture even if the deadline implementation breaks.
            end = time.monotonic() + 0.8
            while time.monotonic() < end and not stopped.wait(0.025):
                server_socket.sendall(b"x" if mode == "slow-body" else b"H")
        except OSError:
            pass
        finally:
            server_socket.close()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    current = TransportObservation()
    token = observation.set(current)
    start = time.monotonic()
    try:
        # Session.send and HTTPAdapter.send are intentionally real here. Only
        # actual socket creation and TLS wrapping are replaced at the boundary.
        with requests.Session() as session:
            guarded = []
            session.module_guard = lambda url, **kwargs: guarded.append((url, kwargs))
            if mode == "ordinary":
                assert (
                    safe_fetch.fetch_document(
                        session, "https://plans.example/one"
                    ).content
                    == b"plan"
                )
                assert not current.unavailable and not current.security_failure
            else:
                with pytest.raises(requests.RequestException):
                    safe_fetch.fetch_document(session, "https://plans.example/one")
                assert time.monotonic() - start < 0.6
                assert current.unavailable and not current.security_failure
        assert guarded == [("https://plans.example/one", {"method": "GET"})]
        assert targets == [("93.184.216.34", 443)]
        assert tls_hosts == ["plans.example"]
        assert b"Host: plans.example\r\n" in received[0]
    finally:
        observation.reset(token)
        stopped.set()
        client_socket.close()
        server_socket.close()
        server.join(timeout=2)


def test_dns_deadline_bounds_both_caller_wait_and_outstanding_resolver_threads(
    monkeypatch,
):
    release = threading.Event()
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(safe_fetch, "_DNS_SLOTS", slots)
    monkeypatch.setattr(safe_fetch, "DNS_TIMEOUT_SECONDS", 0.02)

    def resolve(*_args, **_kwargs):
        assert release.wait(2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    try:
        for _ in range(2):
            start = time.monotonic()
            with pytest.raises(requests.Timeout, match="DNS lookup deadline"):
                safe_fetch.public_https_url("https://plans.example/one")
            assert time.monotonic() - start < 0.5
        with pytest.raises(requests.ConnectionError, match="resolver capacity"):
            safe_fetch.public_https_url("https://plans.example/one")
    finally:
        release.set()
        assert slots.acquire(timeout=1)
        assert slots.acquire(timeout=1)
        slots.release()
        slots.release()
    assert safe_fetch.public_https_url("https://plans.example/one") == (
        "plans.example",
        ("93.184.216.34",),
    )


def test_document_guard_rejects_unavailable_module_before_dns(monkeypatch):
    from schulportal_hessen.tools.capabilities import ModuleUnavailable
    from schulportal_hessen.tools.transport import ObservedSession

    def deny(url, *, method):
        assert url == "https://start.schulportal.hessen.de/meinunterricht.php"
        assert method == "GET"
        raise ModuleUnavailable("Schulportal module unavailable for this account")

    def unexpected_dns(*args):
        pytest.fail("Denied modules must not reach document DNS or transport")

    monkeypatch.setattr(safe_fetch, "_resolve", unexpected_dns)
    with ObservedSession() as session:
        session.module_guard = deny
        with pytest.raises(ModuleUnavailable):
            safe_fetch.fetch_document(
                session, "https://start.schulportal.hessen.de/meinunterricht.php"
            )
