"""Exercise deletion recovery through HTTP, including delayed DELETE requests."""
import asyncio
from datetime import datetime, timedelta

import httpx
import jwt

from api import api as api_module, auth_db, account_data
from api.metrics.user_metrics_db import UserMetricsDB


def test_recovery_fences_delayed_delete_and_is_retryable(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
        monkeypatch.setattr(auth_db, '_lock', asyncio.Lock())
        await auth_db.initialize()
        refresh = await auth_db.store_refresh_token('5201:student', '5201', 'student', 'test-password')
        stored = await auth_db.get_refresh_token(refresh)
        original = api_module.sessions.create_access_token('5201:student', '5201', 'student', stored['session_id'])
        # Expired access tokens can settle deletion but cannot delete an account.
        identity = jwt.decode(original, api_module.JWT_SECRET, algorithms=[api_module.JWT_ALGORITHM])
        identity['exp'] = datetime.utcnow() - timedelta(minutes=1)
        expired = jwt.encode(identity, api_module.JWT_SECRET, algorithm=api_module.JWT_ALGORITHM)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app), base_url='http://test') as client:
            other_refresh = await auth_db.store_refresh_token('5201:student', '5201', 'student', 'test-password')
            mismatched = await client.post('/account/deletion/recover', json={'access_token': original, 'refresh_token': other_refresh})
            assert mismatched.status_code == 409
            assert await auth_db.get_refresh_token_by_session_id(stored['session_id'])
            body = {'access_token': expired, 'refresh_token': refresh}
            for _ in range(2):
                recovered = await asyncio.wait_for(client.post('/account/deletion/recover', json=body, headers={'X-Session-Token': original}), timeout=2)
                assert recovered.status_code == 200
                assert recovered.json()['state'] == 'cancelled'
                assert recovered.headers['cache-control'] == 'no-store'
            rejected = await client.request('DELETE', '/account', headers={'X-Session-Token': original}, json={'confirmation': 'DELETE'})
            assert rejected.status_code == 401
        # Also fence a request that passed auth before recovery acquired the lock.
        try:
            await account_data.delete_account_data('5201:student', expected_session_id=stored['session_id'])
        except account_data.AccountDeletionSessionChanged:
            pass
        else:
            raise AssertionError('A delayed DELETE must not erase the surviving account')
        assert await auth_db.get_refresh_token(refresh)

    asyncio.run(scenario())


def test_account_routes_export_confirmation_deletion_and_recovery(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
        monkeypatch.setattr(auth_db, '_lock', asyncio.Lock())
        await auth_db.initialize()
        metrics = UserMetricsDB(tmp_path / 'metrics.db')
        await metrics.initialize()
        monkeypatch.setattr(account_data, 'user_metrics_db', metrics)
        refresh = await auth_db.store_refresh_token('5201:student', '5201', 'student', 'test-password')
        stored = await auth_db.get_refresh_token(refresh)
        access = api_module.sessions.create_access_token('5201:student', '5201', 'student', stored['session_id'])
        headers = {'X-Session-Token': access}
        api_module._account_export_last.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app), base_url='http://test') as client:
            assert (await client.get('/account/export')).status_code == 422
            exported = await client.get('/account/export', headers=headers)
            assert exported.status_code == 200
            assert 'attachment' in exported.headers['content-disposition']
            assert exported.json()['account']['username'] == 'student'
            assert refresh not in exported.text and 'test-password' not in exported.text
            invalid = await client.request('DELETE', '/account', headers=headers, json={'confirmation': 'no'})
            assert invalid.status_code == 400
            assert await auth_db.get_refresh_token(refresh)
            deleted = await client.request('DELETE', '/account', headers=headers, json={'confirmation': 'DELETE'})
            assert deleted.status_code == 200
            assert deleted.json()['success'] is True
            assert deleted.json()['upstream_sph_data_deleted'] is False
            recovered = await client.post('/account/deletion/recover', json={'access_token': access, 'refresh_token': refresh})
            assert recovered.json() == {'state': 'revoked'}
            assert await auth_db.get_refresh_token(refresh) is None
            forged = await client.post('/account/deletion/recover', json={'access_token': 'invalid'})
            assert forged.status_code == 401

    asyncio.run(scenario())


def test_revoked_jwt_does_not_hold_account_lock_during_downstream_body_wait(tmp_path, monkeypatch):
    from starlette.requests import Request
    from starlette.responses import Response
    async def scenario():
        monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
        monkeypatch.setattr(auth_db, '_lock', asyncio.Lock())
        await auth_db.initialize()
        refresh = await auth_db.store_refresh_token('5201:student', '5201', 'student', 'test-password')
        stored = await auth_db.get_refresh_token(refresh)
        token = api_module.sessions.create_access_token('5201:student', '5201', 'student', stored['session_id'])
        await auth_db.delete_user_tokens('5201:student')
        request = Request({'type':'http', 'method':'PUT', 'scheme':'https', 'path':'/settings/preferences',
                           'query_string':b'', 'headers':[(b'x-session-token', token.encode())]})
        entered = asyncio.Event()
        body_ready = asyncio.Event()
        lock = await account_data.account_lifecycle_lock('5201:student')
        async def delayed_body(_request):
            entered.set()
            await body_ready.wait()
            return Response(status_code=401)
        work = asyncio.create_task(api_module.serialize_account_requests(request, delayed_body))
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0.2)
            lock.release()
        finally:
            body_ready.set()
            await work
    asyncio.run(scenario())


def test_admin_reads_drain_before_deletion_and_metrics_rechecks_revocation(tmp_path, monkeypatch):
    from api import admin

    async def scenario():
        monkeypatch.setattr(auth_db, 'DB_PATH', str(tmp_path / 'auth.db'))
        monkeypatch.setattr(auth_db, '_lock', asyncio.Lock())
        monkeypatch.setattr(account_data, 'admin_bulk_read_gate', account_data.AccountLifecycleLock())
        await auth_db.initialize()
        monkeypatch.setenv('LANIS_ADMIN_ACCOUNTS', '5201:admin')
        monkeypatch.setenv('LANIS_ADMIN_JWT_SECRET', 'x' * 64)
        session_id = await auth_db.create_admin_session('5201:admin', datetime.utcnow() + timedelta(minutes=30))
        token = admin._issue_token(admin.AdminPrincipal('5201:admin', '5201', 'admin', session_id))
        entered, finish = asyncio.Event(), asyncio.Event()

        async def rows(**kwargs):
            entered.set()
            await finish.wait()
            return []

        monkeypatch.setattr(admin, '_metric_rows', rows)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_module.app), base_url='http://test') as client:
            request = asyncio.create_task(client.get('/admin/users', headers={'X-Admin-Token': token}))
            await asyncio.wait_for(entered.wait(), 2)
            gate = account_data.admin_bulk_read_gate.shared()
            deletion = asyncio.create_task(gate.acquire())
            await asyncio.sleep(0)
            assert not deletion.done()
            finish.set()
            assert (await asyncio.wait_for(request, 2)).status_code == 200
            await asyncio.wait_for(deletion, 2)
            gate.release()

            # The legacy metrics route must reject a token revoked while it waits.
            actor = await account_data.account_lifecycle_lock('5201:admin')
            async with actor:
                metrics_request = asyncio.create_task(client.get('/metrics/stats', headers={'X-Admin-Token': token}))
                await asyncio.sleep(0.05)
                await auth_db.delete_user_data('5201:admin')
            assert (await asyncio.wait_for(metrics_request, 2)).status_code == 401

    asyncio.run(scenario())
