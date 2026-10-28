"""Regression coverage for the October 2026 security findings."""

import asyncio
import json
import socket
import subprocess
import threading
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
        safe_fetch, "public_https_url", lambda _: ("plans.example", "93.184.216.34")
    )
    monkeypatch.setattr(safe_fetch, "MAX_DOCUMENT_BYTES", 1024)
    response = requests.Response()
    response.status_code = status
    response._content, response._content_consumed = body, True
    monkeypatch.setattr(requests.Session, "send", lambda *_a, **_k: response)
    with requests.Session() as session, pytest.raises(ValueError):
        safe_fetch.fetch_document(session, "https://plans.example/plan")


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
    assert calls[0].startswith("https://start.schulportal.hessen.de/")


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
    start = source.index('    DIRECT_LOG="$(mktemp')
    end = source.index("\n    sleep 1", start)
    import os

    result = subprocess.run(
        ["bash", "-euc", source[start:end] + "\nwait\n"],
        env={
            **os.environ,
            "TMPDIR": str(tmp_path),
            "PYTHON_BIN": str(python),
            "HOST": "127.0.0.1",
            "PORT": "9999",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    log = Path(result.stdout.strip().removeprefix("Log: "))
    assert log != tmp_path / "lanis-api.log"
    assert log.read_text() == "started\n"
    assert log.stat().st_mode & 0o777 == 0o600
    assert victim.read_text() == "keep this"


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
