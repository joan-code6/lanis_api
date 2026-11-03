from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from api.app_launch import browser_launch_url


def response(location=None):
    return SimpleNamespace(
        is_redirect=location is not None,
        headers={"Location": location} if location else {},
        raise_for_status=Mock(), close=Mock(),
    )


def test_oauth_starts_in_browser_not_backend():
    first = response('vidis.php?tool=t108-1')
    second = response('https://school.bettermarks.com/auth/vidis/DE_vidis')
    client = SimpleNamespace(session=SimpleNamespace(get=Mock(side_effect=[first, second])))
    assert browser_launch_url(client, 'https://start.schulportal.hessen.de/index.php?a=f&e=t108-1') == 'https://school.bettermarks.com/auth/vidis/DE_vidis'
    assert client.session.get.call_count == 2
    first.close.assert_called_once()
    second.close.assert_called_once()


@pytest.mark.parametrize('destination', ['javascript:alert(1)', 'http://example.com', 'https://user:password@example.com'])
def test_rejects_invalid_destinations(destination):
    client = SimpleNamespace(session=SimpleNamespace(get=Mock(return_value=response(destination))))
    with pytest.raises(ValueError):
        browser_launch_url(client, 'https://start.schulportal.hessen.de/index.php')


def test_does_not_treat_upstream_login_page_as_success():
    client = SimpleNamespace(session=SimpleNamespace(get=Mock(return_value=response())))
    with pytest.raises(ValueError):
        browser_launch_url(client, 'https://start.schulportal.hessen.de/index.php')


def test_launch_route_uses_raw_catalog_and_returns_browser_redirect(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    import api.api as routes
    from starlette.requests import Request

    client = SimpleNamespace(
        BASE_START_URL='https://start.schulportal.hessen.de',
        get_apps=Mock(return_value={'success': True, 'data': {'entrys': [
            {'Name': 'bettermarks', 'link': 'index.php?a=f&e=t108-1'},
        ]}}),
        session=SimpleNamespace(get=Mock(side_effect=[
            response('vidis.php?tool=t108-1'),
            response('https://school.bettermarks.com/auth/vidis/DE_vidis'),
        ])),
    )
    monkeypatch.setattr(routes, 'local_auth_dependency', AsyncMock(return_value=SimpleNamespace(user_id='test')))
    monkeypatch.setattr(routes.sessions, '_get_or_create_schulportal_client', AsyncMock(return_value=SimpleNamespace(client=client, school_id='test', username='test')))
    monkeypatch.setattr(routes.user_metrics_db, 'record_module_open', AsyncMock())
    result = asyncio.run(routes.app_launch('bettermarks', Request({'type': 'http'}), token='test', x_session_token=None))
    assert result.status_code == 303
    assert result.headers['location'] == 'https://school.bettermarks.com/auth/vidis/DE_vidis'
    assert result.headers['cache-control'] == 'no-store'
    assert result.headers['referrer-policy'] == 'no-referrer'


def test_module_listing_does_not_consume_oauth_flow():
    from schulportal_hessen import SchulportalHessenAPI
    client = SchulportalHessenAPI()
    client.session.get = Mock(return_value=SimpleNamespace(
        status_code=302, headers={'Location': 'vidis.php?tool=t108-1'}, close=Mock(),
    ))
    original = 'https://start.schulportal.hessen.de/index.php?a=f&e=t108-1'
    assert client._resolve_direct_url(original) == original
    assert client.session.get.call_count == 1
