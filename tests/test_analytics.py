import asyncio
import json
import time

import aiosqlite
import httpx
import pytest

from api import analytics_store, auth_db


@pytest.fixture(autouse=True)
def isolated_database_lock(monkeypatch):
    # Tests run separate asyncio.run loops; locks must not retain an earlier loop.
    monkeypatch.setattr(auth_db, '_lock', asyncio.Lock())


def test_account_references_are_stable_opaque_and_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))

    async def scenario():
        await auth_db.initialize()
        first, second = await asyncio.gather(
            analytics_store.account_reference('5201:Student'),
            analytics_store.account_reference('5201:student'),
        )
        assert first == second
        assert first.startswith('la_') and len(first) == 35
        assert 'student' not in first and '5201' not in first
        assert await analytics_store.resolve_account(first) == '5201:student'
        assert await analytics_store.account_reference('5202:student') != first
        counts = await auth_db.delete_user_data('5201:student')
        assert counts['analytics_accounts'] == 1
        assert await analytics_store.resolve_account(first) is None
        assert await analytics_store.account_reference('5201:student') != first

    asyncio.run(scenario())


def test_diagnostics_are_encrypted_authenticated_bounded_and_expire(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))

    async def scenario():
        await auth_db.initialize()
        details = {'kind': 'error', 'message': 'PRIVATE STUDENT DATA', 'stack': 'private stack'}
        ref = await analytics_store.save_diagnostic('5201:student', details)
        async with aiosqlite.connect(auth_db.DB_PATH) as db:
            async with db.execute('SELECT encrypted_payload FROM analytics_diagnostics') as cursor:
                encrypted = (await cursor.fetchone())[0]
            assert 'PRIVATE' not in encrypted and 'private stack' not in encrypted
            assert analytics_store._decrypt(encrypted) == details
            raw = bytearray(__import__('base64').urlsafe_b64decode(encrypted[3:]))
            raw[-1] ^= 1
            with pytest.raises(ValueError):
                analytics_store._decrypt('v1:' + __import__('base64').urlsafe_b64encode(raw).decode())
            exported = await analytics_store.export_metadata('5201:student')
            assert 'PRIVATE' not in json.dumps(exported)
            assert exported['diagnostics'][0]['diagnostic_id'] == ref
            await db.execute('UPDATE analytics_diagnostics SET expires_at = ?', (int(time.time()) - 1,))
            await db.commit()
        assert await analytics_store.read_diagnostic(ref) is None
        for _ in range(20):
            await analytics_store.save_diagnostic('5201:student', details)
        with pytest.raises(ValueError, match='rate limit'):
            await analytics_store.save_diagnostic('5201:student', details)
        counts = await auth_db.delete_user_data('5201:student')
        assert counts['analytics_diagnostics'] == 20

    asyncio.run(scenario())


def test_endpoints_require_live_session_and_new_consent(tmp_path, monkeypatch):
    from api import api as api_module
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))

    async def scenario():
        await auth_db.initialize()
        refresh = await auth_db.store_refresh_token('5201:student', '5201', 'student', 'test-password')
        stored = await auth_db.get_refresh_token(refresh)
        token = api_module.sessions.create_access_token('5201:student', '5201', 'student', stored['session_id'])
        headers = {'X-Session-Token': token, 'X-LANIS-Analytics-Consent': 'account-v2'}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app), base_url='http://test') as client:
            assert (await client.get('/analytics/identity', headers={'X-Session-Token': token})).status_code == 403
            assert (await client.get('/analytics/identity', headers={'X-LANIS-Analytics-Consent': 'account-v2'})).status_code == 422
            response = await client.get('/analytics/identity', headers=headers)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['x-lanis-request-id']
            reference = response.json()['account_ref']
            assert (await client.get('/analytics/identity', headers=headers)).json()['account_ref'] == reference
            result = await client.post('/analytics/diagnostics', headers=headers, json={'kind': 'error', 'message': 'PRIVATE'})
            assert result.status_code == 201
            assert 'PRIVATE' not in result.text
            assert (await client.get('/admin/analytics/accounts/' + reference, headers=headers)).status_code in (401, 403, 422)
            assert (await client.get('/admin/analytics/diagnostics/' + result.json()['diagnostic_id'], headers=headers)).status_code in (401, 403, 422)
            assert (await client.post('/analytics/diagnostics', headers=headers, json={'kind': 'error', 'message': 'a' * 4097})).status_code == 422
            await auth_db.delete_refresh_token(refresh)
            assert (await client.get('/analytics/identity', headers=headers)).status_code == 401

    asyncio.run(scenario())


def test_admin_diagnostics_are_authorized_audited_and_request_correlated(tmp_path, monkeypatch):
    from api import api as api_module, admin as admin_module
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
    monkeypatch.setenv('LANIS_ADMIN_ACCOUNTS', '5201:operator')
    monkeypatch.setenv('LANIS_ADMIN_JWT_SECRET', 'a' * 64)
    audit = []

    async def verify(*_args):
        return True

    async def record(*args, **kwargs):
        audit.append(args)

    monkeypatch.setattr(admin_module, '_verify_sph_credentials', verify)
    monkeypatch.setattr(admin_module, '_record_login', record)
    monkeypatch.setattr(admin_module, '_record_admin_action', record)

    async def scenario():
        await auth_db.initialize()
        login = await admin_module.admin_login(admin_module.AdminLoginRequest(school_id='5201', username='operator', password='fake'))
        headers = {'X-Admin-Token': login.access_token}
        account = await analytics_store.account_reference('5201:student')
        request_id = '12345678-1234-1234-1234-123456789abc'
        diagnostic = await analytics_store.save_diagnostic('5201:student', {'kind': 'api', 'message': 'PRIVATE', 'request_id': request_id})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app), base_url='http://test') as client:
            resolved = await client.get('/admin/analytics/accounts/' + account, headers=headers)
            assert resolved.status_code == 200
            assert resolved.json()['user_id'] == '5201:student'
            investigated = await client.get('/admin/analytics/users/5201:student', headers=headers)
            assert investigated.status_code == 200 and 'PRIVATE' not in investigated.text
            assert investigated.json()['account_ref'] == account
            quality = await client.get('/admin/analytics/quality', headers=headers)
            assert quality.status_code == 200 and quality.headers['cache-control'] == 'no-store'
            details = await client.get('/admin/analytics/diagnostics/' + diagnostic, headers=headers)
            assert details.status_code == 200 and details.headers['cache-control'] == 'no-store'
            assert 'PRIVATE' in details.text
            correlated = await client.get('/admin/analytics/requests/' + request_id, headers=headers)
            assert correlated.status_code == 200 and diagnostic in correlated.text
            assert {row[1] for row in audit if len(row) > 1} >= {'analytics.account.resolve', 'analytics.diagnostic.read', 'analytics.request.read'}
            await auth_db.delete_user_data('5201:student')
            assert (await client.get('/admin/analytics/diagnostics/' + diagnostic, headers=headers)).status_code == 404
            assert (await client.get('/admin/analytics/accounts/' + account, headers=headers)).status_code == 404

    asyncio.run(scenario())


def test_server_crashes_keep_details_in_vault_only_with_consent(tmp_path, monkeypatch):
    from api import api as api_module
    from api.analytics_routes import register_request_diagnostics
    from fastapi import FastAPI
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
    monkeypatch.delenv('POSTHOG_PROJECT_KEY', raising=False)
    app = FastAPI()
    register_request_diagnostics(app)

    @app.get('/crash/{identifier}')
    async def crash(identifier: str):
        raise RuntimeError('PRIVATE ERROR')

    async def scenario():
        await auth_db.initialize()
        refresh = await auth_db.store_refresh_token('5201:student', '5201', 'student', 'fake')
        stored = await auth_db.get_refresh_token(refresh)
        token = api_module.sessions.create_access_token('5201:student', '5201', 'student', stored['session_id'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            denied = await client.get('/crash/private', headers={'X-Session-Token': token})
            assert denied.status_code == 500 and 'PRIVATE' not in denied.text
            assert not await analytics_store.diagnostic_metadata('5201:student')
            response = await client.get('/crash/private', headers={'X-Session-Token': token, 'X-LANIS-Analytics-Consent': 'account-v2'})
            assert response.status_code == 500 and 'PRIVATE' not in response.text
            rows = await analytics_store.diagnostics_for_request(response.headers['x-lanis-request-id'])
            assert len(rows) == 1
            record = await analytics_store.read_diagnostic(rows[0]['diagnostic_id'])
            assert 'PRIVATE ERROR' in str(record)

    asyncio.run(scenario())


def test_backend_events_require_consent_and_strip_request_contents(tmp_path, monkeypatch):
    from api import api as api_module
    from api.analytics_routes import register_request_diagnostics
    from fastapi import FastAPI
    import requests
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
    monkeypatch.setenv('POSTHOG_PROJECT_KEY', 'phc_fakepublic')
    monkeypatch.setenv('POSTHOG_HOST', 'https://t.lanis.arg-server.de')
    sent = []
    monkeypatch.setattr(requests, 'post', lambda url, **kwargs: sent.append((url, kwargs)))
    app = FastAPI()
    register_request_diagnostics(app)

    @app.get('/items/{identifier}')
    async def item(identifier: str):
        return {'success': True}

    async def scenario():
        await auth_db.initialize()
        refresh = await auth_db.store_refresh_token('5201:student', '5201', 'student', 'PRIVATE password')
        stored = await auth_db.get_refresh_token(refresh)
        token = api_module.sessions.create_access_token('5201:student', '5201', 'student', stored['session_id'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            await client.get('/items/PRIVATE?query=PRIVATE', headers={'X-Session-Token': token})
            await asyncio.sleep(0)
            assert not sent
            response = await client.get('/items/PRIVATE?query=PRIVATE', headers={'X-Session-Token': token, 'X-LANIS-Analytics-Consent': 'account-v2'})
            for _ in range(100):
                if sent:
                    break
                await asyncio.sleep(0.01)
            assert len(sent) == 1
            encoded = json.dumps(sent)
            assert 'PRIVATE' not in encoded and token not in encoded and 'student' not in encoded
            assert sent[0][0] == 'https://t.lanis.arg-server.de/capture/'
            props = sent[0][1]['json']['properties']
            assert props['endpoint'] == '/items/{identifier}'
            assert props['request_id'] == response.headers['x-lanis-request-id']
            assert props['distinct_id'].startswith('la_')
            assert props['status'] == 200

    asyncio.run(scenario())


def test_request_ledger_retention_quality_and_feedback_ownership(tmp_path, monkeypatch):
    from api import feedback_db
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
    monkeypatch.setattr(feedback_db, 'DB_PATH', tmp_path / 'feedback.db')

    async def scenario():
        await auth_db.initialize()
        ids = []
        for index in range(25):
            ref = str(__import__('uuid').uuid4()); ids.append(ref)
            await analytics_store.record_request(f'5201:student{index % 3}', ref, '/messages/{id}', 'GET', 500 if index < 10 else 200, 25)
        summary = await analytics_store.quality_summary()
        assert summary['requests'] == 25 and summary['server_failures'] == 10
        assert summary['affected_accounts'] == 3
        assert summary['alerts'][0]['kind'] == 'server_failure_rate'
        context = await analytics_store.validate_support_context('5201:student0', {'release': 'abcdef0', 'page': '/messages', 'request_ids': ids[:3]})
        assert context['request_ids'] == [ids[0]]
        report = await feedback_db.create_feedback(category='bug', title='Test', details='Example', page='/messages', submitter_user_id='5201:student0', support_context=context)
        assert report['support_context'] == context
        exported = await analytics_store.export_metadata('5201:student0')
        assert exported['recent_requests'] and 'student' not in str(exported['recent_requests'])
        async with aiosqlite.connect(auth_db.DB_PATH) as db:
            await db.execute('UPDATE analytics_requests SET created_at = ?', (int(time.time()) - analytics_store.RETENTION_SECONDS - 1,))
            await db.commit()
        assert not await analytics_store.recent_requests('5201:student0')
        await analytics_store.purge_expired()
        assert (await analytics_store.quality_summary())['requests'] == 0
        await analytics_store.record_request('5201:student0', ids[0], '/messages', 'GET', 200, 5)
        counts = await auth_db.delete_user_data('5201:student0')
        assert counts['analytics_requests'] == 1
        assert not await analytics_store.recent_requests('5201:student0')

    asyncio.run(scenario())


@pytest.mark.parametrize('crash', [False, True])
def test_analytics_reuses_request_lease_when_writer_is_waiting(tmp_path, monkeypatch, crash):
    from api import api as api_module
    from api.account_data import account_lifecycle_lock
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
    monkeypatch.delenv('POSTHOG_PROJECT_KEY', raising=False)

    async def scenario():
        await auth_db.initialize()
        refresh = await auth_db.store_refresh_token('5201:student', '5201', 'student', 'fake')
        stored = await auth_db.get_refresh_token(refresh)
        token = api_module.sessions.create_access_token('5201:student', '5201', 'student', stored['session_id'])
        lock = await account_lifecycle_lock('5201:student')
        original_auth = api_module.local_auth_dependency
        writer = None

        async def exclusive():
            async with lock:
                pass

        async def auth_with_queued_writer(token):
            nonlocal writer
            identity = await original_auth(token)
            if writer is None:
                assert lock._readers >= 1  # The ordinary middleware owns this lease.
                writer = asyncio.create_task(exclusive())
                await asyncio.sleep(0)
                assert lock._waiting_writers == 1
            return identity

        async def broken():
            raise RuntimeError('controlled diagnostic')

        monkeypatch.setattr(api_module, 'local_auth_dependency', auth_with_queued_writer)
        route = None
        if crash:
            api_module.app.add_api_route('/analytics-test-crash', broken)
            route = api_module.app.router.routes[-1]
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app), base_url='http://test') as client:
                response = await asyncio.wait_for(client.get('/analytics-test-crash' if crash else '/analytics/identity', headers={'X-Session-Token': token, 'X-LANIS-Analytics-Consent': 'account-v2'}), timeout=3)
                assert response.status_code == (500 if crash else 200)
            await asyncio.wait_for(writer, timeout=3)
            assert not lock.locked()
        finally:
            if route:
                api_module.app.router.routes.remove(route)
            if writer and not writer.done():
                writer.cancel()

    asyncio.run(scenario())


def test_admin_resolves_opaque_targets_before_taking_actor_lease(tmp_path, monkeypatch):
    from api import api as api_module, admin as admin_module
    from contextlib import asynccontextmanager
    monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
    monkeypatch.setenv('LANIS_ADMIN_ACCOUNTS', '5201:operator')
    monkeypatch.setenv('LANIS_ADMIN_JWT_SECRET', 'a' * 64)
    async def noop(*args, **kwargs):
        return True
    monkeypatch.setattr(admin_module, '_verify_sph_credentials', noop)
    monkeypatch.setattr(admin_module, '_record_login', noop)
    monkeypatch.setattr(admin_module, '_record_admin_action', noop)
    original_access = admin_module._admin_account_access
    acquisitions = []

    @asynccontextmanager
    async def observed_access(*ids):
        acquisitions.append(set(ids))
        async with original_access(*ids):
            yield

    monkeypatch.setattr(admin_module, '_admin_account_access', observed_access)

    async def scenario():
        await auth_db.initialize()
        login = await admin_module.admin_login(admin_module.AdminLoginRequest(school_id='5201', username='operator', password='fake'))
        account = await analytics_store.account_reference('5201:student')
        request_id = '12345678-1234-1234-1234-123456789abc'
        diagnostic = await analytics_store.save_diagnostic('5201:student', {'kind': 'api', 'message': 'private', 'request_id': request_id})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app), base_url='http://test') as client:
            for path in ['accounts/' + account, 'investigate/' + account, 'diagnostics/' + diagnostic, 'requests/' + request_id]:
                acquisitions.clear()
                result = await client.get('/admin/analytics/' + path, headers={'X-Admin-Token': login.access_token})
                assert result.status_code == 200
                assert acquisitions[0] == {'5201:operator', '5201:student'}, path
    asyncio.run(scenario())
