import asyncio

import pytest
import requests

from schulportal_hessen.tools.transport import (
    ObservedSession,
    TransportObservation,
    UpstreamSessionExpired,
    observation,
)


@pytest.mark.parametrize(
    "module",
    ["stundenplan.php", "meinunterricht.php", "kalender.php", "nachrichten.php"],
)
def test_login_gate_never_reaches_module_parser(monkeypatch, module):
    def request(self, method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        response.url = "https://start.schulportal.hessen.de/forward.php"
        response._content = (
            '<h1>Login nötig</h1><a id="loginButton">Zum Login</a>'.encode()
        )
        response.encoding = "utf-8"
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    session = ObservedSession()
    current = TransportObservation()
    reset = observation.set(current)
    try:
        with pytest.raises(UpstreamSessionExpired):
            session.get("https://start.schulportal.hessen.de/" + module)
        assert current.expired_sessions == [session]
        assert current.security_failure
        assert not current.unavailable
    finally:
        observation.reset(reset)


def test_expiry_discard_preserves_concurrently_restored_client():
    from types import SimpleNamespace

    from api.api import AuthManager

    async def scenario():
        manager = AuthManager()
        old_session = ObservedSession()
        replacement = SimpleNamespace(client=SimpleNamespace(session=ObservedSession()))
        manager._schulportal_clients["5201:student"] = replacement
        await manager.discard_expired_upstream_sessions("5201:student", [old_session])
        assert manager._schulportal_clients["5201:student"] is replacement

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path", ["/stundenplan", "/meinunterricht", "/kalender", "/nachrichten/headers"]
)
@pytest.mark.parametrize("persistent", [False, True])
def test_read_recovery_is_bounded_and_cannot_return_false_success(
    monkeypatch, path, persistent
):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from test_outage_cache import asgi_request

    from api import api as api_module
    from api.outage_cache import OutageCacheRoute

    state = SimpleNamespace(calls=0, discarded=0)
    session = ObservedSession()

    async def auth(_token):
        return SimpleNamespace(user_id="5201:student")

    async def discard(user_id, expired):
        assert user_id == "5201:student"
        assert expired == [session]
        state.discarded += 1

    monkeypatch.setattr(api_module, "local_auth_dependency", auth)
    monkeypatch.setattr(
        api_module,
        "sessions",
        SimpleNamespace(discard_expired_upstream_sessions=discard),
    )
    app = FastAPI()
    app.router.route_class = OutageCacheRoute

    @app.get(path)
    async def read():
        state.calls += 1
        if persistent or state.calls == 1:
            current = observation.get()
            current.expired_sessions.append(session)
            current.security_failure = True
            # Some parsers previously treated login HTML as an empty success.
            return {"success": True, "entries": []}
        return {"success": True, "entries": ["restored"]}

    result = asyncio.run(asgi_request(app, "GET", path, {"X-Session-Token": "token"}))
    assert state.calls == 2
    assert state.discarded == (2 if persistent else 1)
    if persistent:
        assert result.status_code == 503
        assert result.headers["retry-after"] == "5"
    else:
        assert result.status_code == 200
        assert result.json()["entries"] == ["restored"]


def test_expired_client_is_removed_without_revoking_local_login():
    from types import SimpleNamespace

    from api.api import AuthManager

    async def scenario():
        manager = AuthManager()
        session = ObservedSession()
        closed = []
        client = SimpleNamespace(session=session, close=lambda: closed.append(True))
        manager._schulportal_clients["5201:student"] = SimpleNamespace(client=client)
        await manager.set_cache("5201:student", "/kalender", {"success": True})
        await manager.discard_expired_upstream_sessions("5201:student", [session])
        assert "5201:student" not in manager._schulportal_clients
        assert closed == [True]
        assert await manager.get_cached("5201:student", "/kalender") == {
            "success": True
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("expired", [False, True])
def test_database_error_requires_positive_authentication_probe(monkeypatch, expired):
    calls = []

    def request(self, method, url, **kwargs):
        calls.append(url)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.encoding = "utf-8"
        response._content_consumed = True
        if "meinunterricht.php" in url:
            response._content = "<h1>Fehler</h1>Datenbankauswahl nicht möglich".encode()
        elif expired:
            response.url = "https://start.schulportal.hessen.de/index.php"
            response.history = [requests.Response()]
            response._content = b"<html>School selection</html>"
        else:
            response._content = b'{"error":"0","entrys":[]}'
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    session = ObservedSession()
    current = TransportObservation()
    reset = observation.set(current)
    try:
        if expired:
            with pytest.raises(UpstreamSessionExpired):
                session.get("https://start.schulportal.hessen.de/meinunterricht.php")
            assert current.expired_sessions == [session]
        else:
            response = session.get(
                "https://start.schulportal.hessen.de/meinunterricht.php"
            )
            assert "Datenbankauswahl" in response.text
            assert not current.expired_sessions
        assert len(calls) == 2
    finally:
        observation.reset(reset)


@pytest.mark.parametrize(
    "location", ["/index.php", "https://login.schulportal.hessen.de/"]
)
def test_apps_redirect_without_following_is_expiry(monkeypatch, location):
    def request(self, method, url, **kwargs):
        assert kwargs["allow_redirects"] is False
        response = requests.Response()
        response.status_code = 302
        response.url = url
        response.headers["Location"] = location
        response._content = b""
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    with pytest.raises(UpstreamSessionExpired):
        ObservedSession().get(
            "https://start.schulportal.hessen.de/startseite.php?a=ajax&f=apps",
            allow_redirects=False,
        )


@pytest.mark.parametrize("attachment", [False, True])
def test_streamed_html_download_does_not_read_body(monkeypatch, attachment):
    class StreamingResponse(requests.Response):
        @property
        def text(self):
            raise AssertionError("Transport must not consume the download stream")

    response = StreamingResponse()
    response.status_code = 200
    response.url = "https://start.schulportal.hessen.de/dateispeicher.php"
    response.headers["Content-Type"] = "text/html"
    if attachment:
        response.headers["Content-Disposition"] = 'attachment; filename="lesson.html"'

    def request(self, method, url, **kwargs):
        assert kwargs["stream"] is True
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    assert ObservedSession().get(response.url, stream=True) is response
