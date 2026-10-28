import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api import android_push as android
from api import api as routes
from api import auth_db
from api import message_notifications as notifications

TOKEN = 'test-device-registration-token-1234567890'
BINDING = '12345678-1234-1234-1234-123456789abc'


def test_native_subscriptions_are_distinct_from_web_push():
    subscription = android.android_subscription(TOKEN, BINDING)
    assert android.is_android_subscription(subscription)
    assert not notifications.is_trusted_push_endpoint(subscription['endpoint'])
    assert not android.is_android_subscription({'endpoint': subscription['endpoint'], 'keys': {'p256dh': 'web', 'auth': 'web'}})
    assert not android.is_android_subscription({'endpoint': 'https://attacker.invalid', 'keys': subscription['keys']})


@pytest.mark.parametrize('token', ['', 'a' * 4097, 'https://example.org/token', '../token', 'token with spaces is invalid'])
def test_invalid_tokens_are_rejected(token):
    with pytest.raises(ValueError):
        android.android_subscription(token, BINDING)


def test_invalid_session_binding_is_rejected():
    with pytest.raises(ValueError):
        android.android_subscription(TOKEN, 'not-a-session-binding')


def test_android_delivery_uses_finite_timeout_and_session_binding(monkeypatch):
    captured = {}
    monkeypatch.setenv('LANIS_FIREBASE_PROJECT_ID', 'lanis-test-project')
    monkeypatch.setattr(android, '_access_token', lambda: 'oauth-test')

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(ok=True)

    monkeypatch.setattr('requests.post', post)
    android.send_android_push(android.android_subscription(TOKEN, BINDING), {'title': 'Test', 'body': 'Update', 'url': '/timetable'})
    assert captured['timeout'] == 15
    assert captured['url'] == 'https://fcm.googleapis.com/v1/projects/lanis-test-project/messages:send'
    message = captured['json']['message']
    assert message['token'] == TOKEN
    assert message['data']['binding'] == BINDING
    assert message['android'] == {'priority': 'high', 'ttl': '3600s'}
    assert captured['headers'] == {'Authorization': 'Bearer oauth-test'}


@pytest.mark.parametrize('error_code, permanent', [('UNREGISTERED', True), ('QUOTA_EXCEEDED', False), ('SENDER_ID_MISMATCH', False)])
def test_only_unregistered_tokens_are_permanent_failures(monkeypatch, error_code, permanent):
    monkeypatch.setenv('LANIS_FIREBASE_PROJECT_ID', 'lanis-test-project')
    monkeypatch.setattr(android, '_access_token', lambda: 'test')
    monkeypatch.setattr('requests.post', lambda *args, **kwargs: SimpleNamespace(
        ok=False, json=lambda: {'error': {'details': [{'errorCode': error_code}]}}))
    with pytest.raises(android.AndroidPushError) as error:
        android.send_android_push(android.android_subscription(TOKEN, BINDING), {})
    assert error.value.permanent is permanent
    assert TOKEN not in str(error.value)


def test_android_only_delivery_does_not_require_vapid(monkeypatch):
    subscription = android.android_subscription(TOKEN, BINDING)
    sent = []
    monkeypatch.setattr(notifications, 'push_configured', lambda: False)
    monkeypatch.setattr(notifications, 'android_push_configured', lambda: True)
    monkeypatch.setattr(notifications, 'send_android_push', lambda sub, payload: sent.append((sub, payload)))
    result = asyncio.run(notifications._send_push_to_subscriptions('user', [subscription], {'title': 'Test'}))
    assert result == {subscription['endpoint']: 'delivered'}
    assert len(sent) == 1


def test_firebase_config_error_does_not_delete_device(monkeypatch):
    subscription = android.android_subscription(TOKEN, BINDING)
    monkeypatch.setattr(notifications, 'android_push_configured', lambda: True)

    def fail(*args):
        raise ValueError('Invalid Firebase project ID')

    monkeypatch.setattr(notifications, 'send_android_push', fail)
    result = asyncio.run(notifications._send_push_to_subscriptions('user', [subscription], {}))
    assert result == {subscription['endpoint']: 'retry'}


def test_registration_rotation_and_account_cleanup(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
    monkeypatch.setattr(auth_db, '_lock', asyncio.Lock())
    monkeypatch.setattr(routes, 'android_push_configured', lambda: True)
    auth = SimpleNamespace(user_id='5201:Student', session_id='')

    async def scenario():
        await auth_db.initialize()
        refresh_token = await auth_db.store_refresh_token(auth.user_id, '5201', 'Student', 'test-password')
        auth.session_id = (await auth_db.get_refresh_token(refresh_token))['session_id']
        old = TOKEN + '-old'
        await routes.register_android_push(routes.AndroidPushRequest(token=old, binding=BINDING), auth)
        await routes.register_android_push(routes.AndroidPushRequest(token=TOKEN, binding=BINDING, previous_token=old), auth)
        subscriptions = await auth_db.get_push_subscriptions(auth.user_id)
        assert subscriptions == [android.android_subscription(TOKEN, BINDING)]
        # Another account cannot unregister this user's device.
        await routes.unregister_android_push(routes.AndroidPushRequest(token=TOKEN, binding=BINDING), SimpleNamespace(user_id='other'))
        assert len(await auth_db.get_push_subscriptions(auth.user_id)) == 1
        await auth_db.delete_user_tokens(auth.user_id)
        assert await auth_db.get_push_subscriptions(auth.user_id) == []
        # A request authenticated before revocation cannot restore its device registration.
        await routes.register_android_push(routes.AndroidPushRequest(token=TOKEN, binding=BINDING), auth)
        assert await auth_db.get_push_subscriptions(auth.user_id) == []

    asyncio.run(scenario())


def test_registration_reports_unconfigured_server(monkeypatch):
    monkeypatch.setattr(routes, 'android_push_configured', lambda: False)
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes.register_android_push(routes.AndroidPushRequest(token=TOKEN, binding=BINDING), SimpleNamespace(user_id='user')))
    assert error.value.status_code == 503


def test_android_route_requires_authentication():
    route = next(route for route in routes.app.routes if getattr(route, 'path', None) == '/notifications/android' and 'POST' in route.methods)
    assert any(dependency.call is routes.client_dependency for dependency in route.dependant.dependencies)


def test_payload_is_bounded_for_unicode(monkeypatch):
    captured = {}
    monkeypatch.setenv('LANIS_FIREBASE_PROJECT_ID', 'lanis-test-project')
    monkeypatch.setattr(android, '_access_token', lambda: 'test')
    monkeypatch.setattr('requests.post', lambda *args, **kwargs: (captured.update(kwargs) or SimpleNamespace(ok=True)))
    android.send_android_push(android.android_subscription(TOKEN, BINDING), {key: '🎒' * 2000 for key in ('title', 'body', 'url', 'tag')})
    assert len(json.dumps(captured['json']['message']['data'], ensure_ascii=False).encode()) < 4096
