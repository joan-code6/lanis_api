"""Regression coverage for the October 2026 security findings."""

import asyncio
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from bs4 import BeautifulSoup

from api import api as app
from api import auth_db
from api import message_notifications as push
from schulportal_hessen.applets.mein_unterricht.api import (
    meinunterricht_get_entry_details,
)
from schulportal_hessen.applets.stundenplan.api import parse_timetable_html
from schulportal_hessen.external.dsb import api as dsb
from schulportal_hessen.tools import safe_fetch
from schulportal_hessen.tools.transport import TransportObservation, observation


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/plan",
        "https://user@example.org/plan",
        "https://example.org:8443/plan",
        "https://example.org/plan#fragment",
        "https://example.org\\@127.0.0.1/",
        "https://example.org/\nplan",
    ],
)
def test_document_url_rejects_ambiguous_or_insecure_forms(url):
    with pytest.raises(ValueError):
        safe_fetch.public_https_url(url)


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["93.184.216.34", "10.0.0.1"],
    ],
)
def test_document_url_rejects_nonpublic_dns_answers(monkeypatch, addresses):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses
        ],
    )
    with pytest.raises(ValueError):
        safe_fetch.public_https_url("https://plans.example/plan")


def test_fetch_pins_checked_address_preserves_tls_host_and_bounds_transport(
    monkeypatch,
):
    dns_calls, connections = [], []

    def resolve(*_a, **_k):
        dns_calls.append(True)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(
        safe_fetch,
        "create_connection",
        lambda address, *_a, **_k: connections.append(address),
    )

    def send(transport, prepared, **kwargs):
        assert transport.trust_env is False
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        assert prepared.headers["Cookie"] == "session=private"
        adapter = transport.get_adapter(prepared.url)
        assert adapter.get_connection(prepared.url) is adapter.pool
        assert (
            adapter.get_connection_with_tls_context(prepared, verify=True)
            is adapter.pool
        )
        adapter.cert_verify(adapter.pool, prepared.url, True, None)
        assert adapter.pool.cert_reqs == "CERT_REQUIRED"
        conn = adapter.pool.ConnectionCls("plans.example", port=443)
        assert conn.host == "plans.example"
        conn._new_conn()
        response = requests.Response()
        response.status_code = 200
        response._content = b"ordinary plan"
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    with requests.Session() as session:
        session.cookies.set("session", "private")
        assert (
            safe_fetch.fetch_document(session, "https://plans.example/plan").content
            == b"ordinary plan"
        )
    assert len(dns_calls) == 1
    assert connections == [("93.184.216.34", 443)]


@pytest.mark.parametrize("status,body", [(302, b""), (200, b"x" * 1025)])
def test_document_redirect_and_size_limits(monkeypatch, status, body):
    monkeypatch.setattr(
        safe_fetch, "public_https_url", lambda _: ("plans.example", ("93.184.216.34",))
    )
    monkeypatch.setattr(safe_fetch, "MAX_DOCUMENT_BYTES", 1024)
    response = requests.Response()
    response.status_code = status
    response._content, response._content_consumed = body, True
    monkeypatch.setattr(requests.Session, "send", lambda *_a, **_k: response)
    with requests.Session() as session, pytest.raises(ValueError):
        safe_fetch.fetch_document(session, "https://plans.example/plan")


@pytest.mark.parametrize("exhausted", [False, True])
@pytest.mark.parametrize("ip_count", [2, 6])
def test_fetch_tries_only_vetted_addresses_in_resolver_order(
    monkeypatch, exhausted, ip_count
):
    ips = ["2606:4700:4700::1111", "93.184.216.34"] + [
        f"93.184.216.{i}" for i in range(35, 35 + ip_count - 2)
    ]
    lookups, connections = [], []
    connected = object()

    def resolve(*args, **_kwargs):
        lookups.append(args[0])
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (ip, 443),
            )
            for ip in ips
        ]

    def connect(address, *_args, **_kwargs):
        connections.append(address[0])
        if exhausted or address[0] == ips[0]:
            raise OSError("address unreachable")
        return connected

    def send(transport, prepared, **_kwargs):
        pool = transport.get_adapter(prepared.url).pool
        connection = pool.ConnectionCls("plans.example", port=443)
        if exhausted:
            with pytest.raises(OSError, match="unreachable"):
                connection._new_conn()
        else:
            assert connection._new_conn() is connected
        response = requests.Response()
        response.status_code = 200
        response._content, response._content_consumed = b"plan", True
        return response

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(safe_fetch, "create_connection", connect)
    monkeypatch.setattr(requests.Session, "send", send)
    with requests.Session() as session:
        safe_fetch.fetch_document(session, "https://plans.example/plan")
    assert lookups == ["plans.example"]
    assert (
        connections == ips[: safe_fetch.MAX_CONNECT_ATTEMPTS]
        if exhausted
        else connections == ips[:2]
    )


@pytest.mark.parametrize(
    "failure,unavailable,security",
    [
        (requests.ReadTimeout("timeout"), True, False),
        (requests.ConnectionError("connection failed"), True, False),
        (requests.exceptions.SSLError("invalid certificate"), False, True),
        (503, True, False),
        (401, False, True),
        (403, False, True),
        (200, False, False),
    ],
)
@pytest.mark.parametrize("stream_failure", [False, True])
def test_pinned_transport_preserves_outage_and_security_observations(
    monkeypatch, failure, unavailable, security, stream_failure
):
    monkeypatch.setattr(
        safe_fetch, "public_https_url", lambda _: ("plans.example", ("93.184.216.34",))
    )

    def send(*_args, **_kwargs):
        if isinstance(failure, Exception) and not stream_failure:
            raise failure
        response = requests.Response()
        response.status_code = failure if isinstance(failure, int) else 200
        response._content, response._content_consumed = b"plan", True
        if isinstance(failure, Exception):

            def chunks(*_args, **_kwargs):
                raise failure

            response.iter_content = chunks
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    current = TransportObservation()
    token = observation.set(current)
    try:
        with requests.Session() as session:
            if failure == 200:
                assert (
                    safe_fetch.fetch_document(
                        session, "https://plans.example/plan"
                    ).content
                    == b"plan"
                )
            else:
                with pytest.raises(requests.RequestException):
                    safe_fetch.fetch_document(session, "https://plans.example/plan")
        assert current.unavailable is unavailable
        assert current.security_failure is security
    finally:
        observation.reset(token)


def test_pinned_transport_observes_dns_failure_and_honors_fail_fast(monkeypatch):
    calls = []

    def resolve(*_args, **_kwargs):
        calls.append(True)
        raise socket.gaierror("lookup failed")

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    current = TransportObservation(fail_fast=True)
    token = observation.set(current)
    try:
        with requests.Session() as session:
            with pytest.raises(requests.ConnectionError, match="lookup failed"):
                safe_fetch.fetch_document(session, "https://plans.example/plan")
            assert current.unavailable and not current.security_failure
            with pytest.raises(requests.ConnectionError, match="Earlier"):
                safe_fetch.fetch_document(session, "https://plans.example/plan")
        assert calls == [True]
    finally:
        observation.reset(token)


def test_dsb_authorizes_explicit_and_discovered_plans(monkeypatch):
    calls = []
    client = SimpleNamespace(
        dsb_session=object(),
        dsb_get_plan_urls=lambda **_: {
            "success": True,
            "plan_urls": ["https://plans.example/one"],
        },
    )
    response = SimpleNamespace(
        content=b"<table><tr><td>Math</td></tr></table>", raise_for_status=lambda: None
    )
    monkeypatch.setattr(
        dsb, "fetch_document", lambda _s, url: calls.append(url) or response
    )
    assert not dsb.dsb_get_substitution_plan(
        client, plan_url="https://127.0.0.1/private"
    )["success"]
    assert calls == []
    assert dsb.dsb_get_substitution_plan(client)["success"]
    assert dsb.dsb_get_substitution_plan(client, plan_url="https://plans.example/one")[
        "success"
    ]
    assert calls == ["https://plans.example/one"] * 2


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/meinunterricht.php",
        "//127.0.0.1/index.php",
        "https://start.schulportal.hessen.de@evil.example/index.php",
        "https://start.schulportal.hessen.de/private",
        "http://start.schulportal.hessen.de/index.php",
    ],
)
def test_course_entry_rejects_destinations_outside_portal_pages(monkeypatch, url):
    def unexpected(*_):
        pytest.fail("unapproved URL reached the transport")

    monkeypatch.setattr(
        "schulportal_hessen.applets.mein_unterricht.api.fetch_document", unexpected
    )
    assert not meinunterricht_get_entry_details(
        SimpleNamespace(logged_in=True, session=None), url
    )["success"]


@pytest.mark.parametrize(
    "url",
    [
        "index.php?a=f&e=l4",
        "/dateiverteilung.php",
        "https://start.schulportal.hessen.de/meinunterricht.php?id=42",
        "https://START.SCHULPORTAL.HESSEN.DE:443/meinunterricht.php?id=42",
    ],
)
def test_course_entry_preserves_supported_portal_links(monkeypatch, url):
    calls = []
    response = SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"entry": "lesson"}
    )
    monkeypatch.setattr(
        "schulportal_hessen.applets.mein_unterricht.api.fetch_document",
        lambda _s, u: calls.append(u) or response,
    )
    result = meinunterricht_get_entry_details(
        SimpleNamespace(logged_in=True, session=None), url
    )
    assert result["success"] and result["content"] == {"entry": "lesson"}
    from urllib.parse import urlsplit

    assert urlsplit(calls[0]).hostname == "start.schulportal.hessen.de"


@pytest.mark.parametrize("table_id", ["all", "own"])
@pytest.mark.parametrize("header,rows", [(8, 1), (1, 101)])
def test_timetable_rejects_dimensions_before_allocation(table_id, header, rows):
    bad = (
        f'<div id="{table_id}"><table><thead><tr><th>Time</th>'
        + "<th>Day</th>" * header
        + "</tr></thead><tbody>"
        + "<tr><td></td></tr>" * rows
        + "</tbody></table></div>"
    )
    if table_id == "own":
        bad = (
            '<div id="all"><table><tbody><tr><th>Time</th><th>Day</th></tr></tbody></table></div>'
            + bad
        )
    with pytest.raises(ValueError, match="dimensions"):
        parse_timetable_html(bad)


def test_timetable_rejects_billion_row_span():
    html = '<div id="all"><table><tbody><tr><th>Time</th><th>Day</th></tr><tr><td class="VonBis">08:00 - 08:45</td><td rowspan="1000000000"></td></tr></tbody></table></div>'
    with pytest.raises(ValueError, match="rowspan"):
        parse_timetable_html(html)


def test_dsb_rejects_nested_expansion_and_excessive_output():
    with pytest.raises(ValueError, match="Nested"):
        dsb._parse_plan_tables(
            "<table><tr><td>" * 50 + "data" + "</td></tr></table>" * 50
        )
    with pytest.raises(ValueError, match="limit"):
        dsb._parse_plan_tables("<table><tr>" + "<td>x</td>" * 20001 + "</tr></table>")
    result = dsb._parse_plan_tables(
        "<table><tbody><tr><th>Klasse</th></tr><tr><td>10A</td></tr></tbody></table>"
    )
    assert result["tables"][0]["rows"] == [{"Klasse": "10A"}]


def test_script_json_keeps_url_roundtrip_without_html_terminators():
    url = 'https://app.example/</ScRiPt><script>alert(1)</script>?q="&ä'
    encoded = app._html_script_json(url)
    assert "<" not in encoded
    assert json.loads(encoded) == url


def test_launch_page_keeps_untrusted_labels_and_redirects_as_data(monkeypatch):
    name = '<img src=x onerror="alert(1)">'
    url = "https://app.example/</ScRiPt><script>alert(1)</script>"

    async def identity(_token):
        return SimpleNamespace(user_id="user")

    async def restore(_user):
        return SimpleNamespace(
            client=SimpleNamespace(
                get_available_modules=lambda: [{"name": name, "url": "portal"}]
            ),
            school_id="school",
            username="user",
        )

    async def metrics(*_args):
        pass

    monkeypatch.setattr(app, "local_auth_dependency", identity)
    monkeypatch.setattr(app.sessions, "_get_or_create_schulportal_client", restore)
    monkeypatch.setattr(
        app, "_build_launch_urls", lambda *_: {"cookie_url": url, "callback_url": url}
    )
    monkeypatch.setattr(app.user_metrics_db, "record_module_open", metrics)
    response = asyncio.run(
        app.app_launch(name, None, token="token", x_session_token=None)
    )
    document = BeautifulSoup(response.body, "html.parser")
    assert document.title.get_text() == name
    assert document.find("img") is None
    assert len(document.find_all("script")) == 1
    assert "</script>" not in document.script.string.lower()


def test_deploy_log_does_not_follow_predictable_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("keep this")
    (tmp_path / "lanis-api.log").symlink_to(victim)
    python = tmp_path / "fake-python"
    python.write_text("#!/bin/sh\necho started\n")
    python.chmod(0o700)
    source = (Path(__file__).resolve().parents[1] / "deploy.sh").read_text()
    start = source.index("    DIRECT_LOG_DIR=")
    end = source.index("\n    sleep 1", source.index("    nohup", start))
    log_directory = tmp_path / ".lanis-logs"
    log_directory.mkdir(mode=0o777)
    old_log = log_directory / "run.OLD.log"
    old_log.write_text("old")
    old_time = time.time() - 9 * 24 * 60 * 60
    os.utime(old_log, (old_time, old_time))
    result = subprocess.run(
        ["bash", "-euc", "lsof() { return 1; }\n" + source[start:end] + "\nwait\n"],
        env={
            **os.environ,
            "PROJECT_DIR": str(tmp_path),
            "TMPDIR": str(tmp_path / "missing-untrusted-temp"),
            "PYTHON_BIN": str(python),
            "HOST": "127.0.0.1",
            "PORT": "9999",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    log = Path(
        next(
            line.strip().removeprefix("Log: ")
            for line in result.stdout.splitlines()
            if line.strip().startswith("Log: ")
        )
    )
    assert log.parent == log_directory
    assert log.read_text() == "started\n"
    assert log.stat().st_mode & 0o777 == 0o600
    assert log_directory.stat().st_mode & 0o777 == 0o700
    assert not old_log.exists()
    assert victim.read_text() == "keep this"


@pytest.mark.parametrize("failure", ["symlink", "mktemp"])
def test_deploy_log_setup_failure_keeps_running_server_untouched(tmp_path, failure):
    source = (Path(__file__).resolve().parents[1] / "deploy.sh").read_text()
    start = source.index("    DIRECT_LOG_DIR=")
    end = source.index('    echo ">>> Starting uvicorn', start)
    target = tmp_path / "other"
    target.mkdir()
    if failure == "symlink":
        (tmp_path / ".lanis-logs").symlink_to(target, target_is_directory=True)
    result = subprocess.run(
        [
            "bash",
            "-euc",
            'lsof() { touch "$PROJECT_DIR/stopped"; echo 999999; }\n'
            + ("mktemp() { return 1; }\n" if failure == "mktemp" else "")
            + source[start:end],
        ],
        env={**os.environ, "PROJECT_DIR": str(tmp_path), "PORT": "9999"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not (tmp_path / "stopped").exists()
    assert not list(target.iterdir())


def test_model_conversation_reads_only_cache_and_opening_requires_proposal(monkeypatch):
    cached = None
    opened, pending = [], []

    async def current(_sender):
        return {
            "user_id": "user",
            "show_message_previews": True,
            "linked_at": "generation",
        }

    async def get_cached(*_):
        return cached

    async def open_conversation(**kwargs):
        opened.append(kwargs)
        return {"success": True}

    async def create(*_a, **_k):
        return "ABC123"

    monkeypatch.setattr(app, "get_whatsapp_link_for_sender", current)
    monkeypatch.setattr(app.sessions, "get_cached", get_cached)
    monkeypatch.setattr(app, "get_conversation", open_conversation)
    monkeypatch.setattr(app, "create_whatsapp_pending_action", create)

    async def scenario():
        nonlocal cached
        auth = SimpleNamespace(user_id="user")
        tools = {
            tool.name: tool
            for tool in app._whatsapp_agent_tools(
                auth, {"linked_at": "generation"}, "sender", pending
            )
        }
        assert (await tools["get_conversation"].handler({"conversation_id": "42"}))[
            "error"
        ] == "MESSAGE_NOT_CACHED"
        cached = {"success": True, "conversation_id": "42", "messages": ["hello"]}
        assert (
            await tools["get_conversation"].handler({"conversation_id": "42"}) == cached
        )
        proposal = await tools["prepare_action"].handler(
            {"action": "open_message", "payload": {"conversation_id": "42"}}
        )
        assert (
            "open_message"
            in tools["prepare_action"].parameters["properties"]["action"]["enum"]
        )
        assert proposal["confirmation_required"]
        assert opened == []
        assert pending[0]["action"] == "open_message"
        await app._execute_whatsapp_pending_action(
            {"action": "open_message", "payload": {"conversation_id": "42"}},
            auth,
            sender_id="sender",
        )
        assert opened[0]["conversation_id"] == "42"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://fcm.googleapis.com/token#one",
        "https://fcm.googleapis.com/token#",
        "https://fcm.googleapis.com/\ntoken",
        "https://fcm.googleapis.com\\evil/token",
    ],
)
def test_push_rejects_fragment_and_parser_aliases(endpoint):
    assert not push.is_trusted_push_endpoint(endpoint)


def test_push_storage_normalizes_identity_and_limits_devices(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "auth.db"))

    async def scenario():
        await auth_db.initialize()
        token = await auth_db.store_refresh_token("user", "school", "user", "password")
        session = await auth_db.get_refresh_token(token)

        async def save(endpoint):
            await auth_db.save_push_subscription(
                "user",
                {"endpoint": endpoint, "keys": {"p256dh": "key", "auth": "auth"}},
                session["session_id"],
            )

        for i in range(push.MAX_PUSH_SUBSCRIPTIONS):
            await save(f"https://fcm.googleapis.com/{i}")
        await save("https://FCM.GOOGLEAPIS.COM.:443/0")
        await save("https://fcm.googleapis.com/path/../%30")
        assert (
            len(await auth_db.get_push_subscriptions("user"))
            == push.MAX_PUSH_SUBSCRIPTIONS
        )
        with pytest.raises(ValueError, match="limit"):
            await save("https://fcm.googleapis.com/extra")
        with pytest.raises(ValueError):
            await save("https://fcm.googleapis.com/0#alias")
        await auth_db.delete_push_subscription(
            "user", "https://FCM.GOOGLEAPIS.COM.:443/0"
        )
        assert (
            len(await auth_db.get_push_subscriptions("user"))
            == push.MAX_PUSH_SUBSCRIPTIONS - 1
        )

    asyncio.run(scenario())


def test_legacy_push_aliases_merge_before_limit_check_and_keep_latest_data(
    tmp_path, monkeypatch
):
    import aiosqlite

    monkeypatch.setattr(auth_db, "DB_PATH", str(tmp_path / "legacy.db"))

    async def scenario():
        await auth_db.initialize()
        token = await auth_db.store_refresh_token("user", "school", "user", "password")
        session = await auth_db.get_refresh_token(token)

        async def save(endpoint, key="original"):
            await auth_db.save_push_subscription(
                "user",
                {"endpoint": endpoint, "keys": {"p256dh": key, "auth": "auth"}},
                session["session_id"],
            )

        for index in range(push.MAX_PUSH_SUBSCRIPTIONS):
            await save(f"https://fcm.googleapis.com/{index}")
        async with aiosqlite.connect(auth_db.DB_PATH) as db:
            await db.execute(
                "INSERT INTO push_subscriptions (endpoint,user_id,session_id,p256dh,auth,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (
                    "https://FCM.GOOGLEAPIS.COM:443/0",
                    "user",
                    session["session_id"],
                    "latest",
                    "latest-auth",
                    "2026-01-01",
                    "2099-01-01",
                ),
            )
            await db.commit()
        await auth_db.initialize()
        rows = await auth_db.get_push_subscriptions("user")
        assert len(rows) == push.MAX_PUSH_SUBSCRIPTIONS
        assert next(row for row in rows if row["endpoint"].endswith("/0"))["keys"] == {
            "p256dh": "latest",
            "auth": "latest-auth",
        }
        # Also repair legacy spellings written during a rolling upgrade before
        # checking the limit, rather than requiring a process restart.
        async with aiosqlite.connect(auth_db.DB_PATH) as db:
            await db.execute(
                "UPDATE push_subscriptions SET endpoint=? WHERE endpoint=?",
                ("https://FCM.GOOGLEAPIS.COM/0", "https://fcm.googleapis.com/0"),
            )
            await db.commit()
        await save("https://fcm.googleapis.com/0", key="renewed")
        rows = await auth_db.get_push_subscriptions("user")
        assert len(rows) == push.MAX_PUSH_SUBSCRIPTIONS
        assert (
            next(row for row in rows if row["endpoint"].endswith("/0"))["keys"][
                "p256dh"
            ]
            == "renewed"
        )
        with pytest.raises(ValueError, match="limit"):
            await save("https://fcm.googleapis.com/new")

    asyncio.run(scenario())


def test_push_round_robin_delivers_to_grandfathered_devices_during_fresh_changes(
    monkeypatch,
):
    delivered = set()
    monkeypatch.setattr(push, "_PUSH_CURSORS", push.OrderedDict())
    monkeypatch.setattr(push, "push_configured", lambda: True)
    device_count = push.MAX_PUSH_SUBSCRIPTIONS + 3
    subscriptions = [
        {"endpoint": f"https://fcm.googleapis.com/{index}", "keys": {}}
        for index in range(device_count)
    ]

    async def dispatch(subscription, _payload):
        delivered.add(subscription["endpoint"])

    monkeypatch.setattr(push, "_dispatch_push", dispatch)

    async def scenario():
        for poll in range(device_count):
            before = len(delivered)
            statuses = await push._send_push_to_subscriptions(
                "grandfathered-user", subscriptions, {"title": f"new change {poll}"}
            )
            assert (
                sum(status == "delivered" for status in statuses.values())
                == push.MAX_PUSH_SUBSCRIPTIONS
            )
            assert len(delivered) >= before
        assert delivered == {row["endpoint"] for row in subscriptions}

    asyncio.run(scenario())


def test_pending_push_retries_survive_endpoint_canonicalization():
    endpoint = "https://fcm.googleapis.com/token"
    state = {
        "pending_deliveries": {
            "https://FCM.GOOGLEAPIS.COM:443/token": [{"title": "pending"}]
        }
    }
    assert push._get_pending_deliveries(state, {endpoint}) == {
        endpoint: [{"title": "pending"}]
    }


def test_push_isolates_workers_and_rejects_excess_queueing(monkeypatch):
    started = 0
    release = threading.Event()

    def send(*_):
        nonlocal started
        started += 1
        assert release.wait(5)

    monkeypatch.setattr(push, "_send_push_sync", send)

    async def scenario():
        tasks = [asyncio.create_task(push._dispatch_push({}, {})) for _ in range(4)]
        try:
            # Let admitted coroutines submit their workers.
            await asyncio.sleep(0)
            with pytest.raises(RuntimeError, match="capacity"):
                await push._dispatch_push({}, {})
        finally:
            release.set()
            await asyncio.gather(*tasks)
        assert started == 4

    asyncio.run(scenario())


def test_stored_push_fragments_are_removed_without_delivery(monkeypatch):
    deleted = []

    async def delete(*args):
        deleted.append(args)

    monkeypatch.setattr(push, "delete_push_subscription", delete)
    endpoint = "https://fcm.googleapis.com/token#alias"
    result = asyncio.run(
        push._send_push_payloads("user", {endpoint: ({"endpoint": endpoint}, {})})
    )
    assert result == {endpoint: "gone"}
    assert deleted == [("user", endpoint)]
