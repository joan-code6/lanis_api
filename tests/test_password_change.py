import asyncio
from urllib.parse import urljoin

import pytest
import requests
from requests.adapters import BaseAdapter
from fastapi import FastAPI
from starlette.responses import JSONResponse

from api.api import detect_required_password_change
from schulportal_hessen.tools.transport import (
    ObservedSession, PasswordChangeObservation, PasswordChangeRequired,
    TransportObservation, observation, password_change_observation,
)
from test_outage_cache import asgi_request

BASE = 'https://start.schulportal.hessen.de/'
TARGET = 'benutzerverwaltung.php?a=userChangePassword'


class PortalAdapter(BaseAdapter):
    def __init__(self, location):
        self.location = location
        self.visited = []

    def send(self, request, **kwargs):
        self.visited.append(request.url)
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = 302 if len(self.visited) == 1 else 200
        response._content = b''
        response._content_consumed = True
        if response.status_code == 302:
            response.headers['Location'] = self.location
        return response

    def close(self):
        pass


@pytest.mark.parametrize('location', [TARGET, '/' + TARGET, urljoin(BASE, TARGET)])
@pytest.mark.parametrize('follow', [True, False])
def test_password_redirect_stops_before_following(location, follow):
    session = ObservedSession()
    adapter = PortalAdapter(location)
    session.mount('https://', adapter)
    signal = PasswordChangeObservation()
    reset = password_change_observation.set(signal)
    facts = TransportObservation()
    transport_reset = observation.set(facts)
    try:
        with pytest.raises(PasswordChangeRequired):
            session.get(BASE + 'meinunterricht.php', allow_redirects=follow)
        assert signal.required
        assert facts.security_failure  # Never substitute an outage snapshot.
        assert adapter.visited == [BASE + 'meinunterricht.php']
    finally:
        observation.reset(transport_reset)
        password_change_observation.reset(reset)


@pytest.mark.parametrize('location', [
    'benutzerverwaltung.php?a=userData',
    'https://example.com/' + TARGET,
    'https://start.schulportal.hessen.de.example.com/' + TARGET,
])
def test_unrelated_redirect_is_not_password_gate(location):
    session = ObservedSession()
    session.mount('https://', PortalAdapter(location))
    assert session.get(BASE + 'meinunterricht.php', allow_redirects=False).status_code == 302


@pytest.mark.parametrize('swallow', [True, False])
def test_api_returns_password_code_even_if_applet_catches_error(swallow):
    app = FastAPI()
    app.middleware('http')(detect_required_password_change)

    @app.get('/probe')
    async def probe():
        def fetch():
            session = ObservedSession()
            session.mount('https://', PortalAdapter(TARGET))
            session.get(BASE + 'meinunterricht.php')
        try:
            await asyncio.to_thread(fetch)
        except PasswordChangeRequired:
            if not swallow:
                raise
        return JSONResponse({'success': True})

    response = asyncio.run(asgi_request(app, 'GET', '/probe'))
    assert response.status_code == 403
    assert response.json() == {'detail': {'code': 'password_change_required'}}
    assert response.headers['cache-control'] == 'no-store'
    assert password_change_observation.get() is None


@pytest.mark.parametrize('success', [True, False])
def test_retry_probes_live_portal(success):
    from types import SimpleNamespace
    from api.api import check_password_change, client_dependency

    calls = []

    class Client:
        def get_apps(self):
            calls.append('live')
            return {'success': success}

    app = FastAPI()
    app.add_api_route('/check', check_password_change)
    app.dependency_overrides[client_dependency] = lambda: SimpleNamespace(client=Client())
    response = asyncio.run(asgi_request(app, 'GET', '/check'))
    assert response.status_code == (200 if success else 503)
    assert calls == ['live']
