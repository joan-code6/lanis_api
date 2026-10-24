"""Consent-bound local analytics endpoints; no upstream school requests."""
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from . import analytics_store
from .account_data import account_lifecycle_lock

router = APIRouter(prefix='/analytics', tags=['analytics'])


async def analytics_auth(
    x_session_token: str = Header(..., alias='X-Session-Token'),
    x_lanis_analytics_consent: str = Header('', alias='X-LANIS-Analytics-Consent'),
):
    if x_lanis_analytics_consent != 'account-v2':
        raise HTTPException(403, 'Account analytics consent is required')
    from .api import local_auth_dependency
    identity = await local_auth_dependency(x_session_token)
    async with (await account_lifecycle_lock(identity.user_id)).shared():
        # A delete/logout may have completed while we waited for the lease.
        identity = await local_auth_dependency(x_session_token)
        yield identity


class DiagnosticPayload(BaseModel):
    kind: Literal['error', 'rejection', 'react', 'api']
    message: str = Field('', max_length=4096)
    stack: str = Field('', max_length=16384)
    request_id: str = Field('', max_length=36, pattern=r'^(?:[a-f0-9-]{36})?$')


@router.get('/identity')
async def identity(auth=Depends(analytics_auth)):
    return JSONResponse({'account_ref': await analytics_store.account_reference(auth.user_id)},
                        headers={'Cache-Control': 'no-store'})


@router.post('/diagnostics', status_code=201)
async def diagnostics(payload: DiagnosticPayload, auth=Depends(analytics_auth)):
    try:
        reference = await analytics_store.save_diagnostic(auth.user_id, payload.model_dump())
    except ValueError:
        raise HTTPException(429, 'Diagnostic rate limit exceeded')
    return JSONResponse({'diagnostic_id': reference, 'retention_days': 7}, status_code=201,
                        headers={'Cache-Control': 'no-store'})


def register_request_diagnostics(app):
    """Correlate every response with private server logs; encrypt consented crashes."""
    import asyncio
    import logging
    import os
    import time
    import traceback
    import uuid
    import requests
    from fastapi.concurrency import run_in_threadpool

    logger = logging.getLogger('lanis.diagnostics')
    pending = set()

    async def backend_event(request, route, status, duration, request_id):
        key = os.getenv('POSTHOG_PROJECT_KEY', '')
        host = os.getenv('POSTHOG_HOST', 'https://t.lanis.arg-server.de')
        if not key.startswith('phc_') or host not in {'https://t.lanis.arg-server.de', 'https://eu.i.posthog.com'}:
            return
        if request.headers.get('X-LANIS-Analytics-Consent') != 'account-v2':
            return
        token = request.headers.get('X-Session-Token')
        if not token:
            return
        from .api import local_auth_dependency
        try:
            identity = await local_auth_dependency(token)
            async with (await account_lifecycle_lock(identity.user_id)).shared():
                await local_auth_dependency(token)
                reference = await analytics_store.account_reference(identity.user_id)
                payload = {'api_key': key, 'event': 'backend_request_completed', 'properties': {
                    'distinct_id': reference, 'app': 'lanis_api', 'schema_version': 3, 'account_ref': reference,
                    'endpoint': route, 'method': request.method, 'status': status,
                    'duration_ms': duration, 'request_id': request_id,
                    '$process_person_profile': True, '$geoip_disable': True, '$ip': None,
                }}
                # Keep the deletion lease through the send: no post-deletion event writes.
                await run_in_threadpool(requests.post, host + '/capture/', json=payload, timeout=3)
        except Exception:
            # Telemetry is best effort; never log credentials or request contents.
            logger.debug('Backend analytics delivery unavailable')

    @app.middleware('http')
    async def request_diagnostics(request, call_next):
        request_id = str(uuid.uuid4())
        request.state.analytics_request_id = request_id
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as error:
            # The original message/stack stays exclusively in the encrypted vault.
            if request.headers.get('X-LANIS-Analytics-Consent') == 'account-v2':
                token = request.headers.get('X-Session-Token')
                if token:
                    try:
                        from .api import local_auth_dependency
                        identity = await local_auth_dependency(token)
                        async with (await account_lifecycle_lock(identity.user_id)).shared():
                            await local_auth_dependency(token)
                            await analytics_store.save_diagnostic(identity.user_id, {
                                'kind': 'api', 'request_id': request_id,
                                'message': str(error)[:4096],
                                'stack': ''.join(traceback.format_exception(type(error), error, error.__traceback__))[:16384],
                            })
                    except Exception:
                        logger.debug('Encrypted server diagnostic unavailable')
            response = JSONResponse({'detail': 'Internal server error', 'request_id': request_id}, status_code=500)
        route = getattr(request.scope.get('route'), 'path', '/unmatched')
        duration = min(3_600_000, max(0, round((time.monotonic() - started) * 1000)))
        response.headers['X-LANIS-Request-ID'] = request_id
        if (request.headers.get('X-LANIS-Analytics-Consent') == 'account-v2'
                and request.headers.get('X-Session-Token') and not route.startswith('/admin')):
            try:
                from .api import local_auth_dependency
                identity = await local_auth_dependency(request.headers['X-Session-Token'])
                async with (await account_lifecycle_lock(identity.user_id)).shared():
                    await local_auth_dependency(request.headers['X-Session-Token'])
                    await analytics_store.record_request(identity.user_id, request_id, route, request.method, response.status_code, duration)
            except Exception:
                logger.debug('Technical request ledger unavailable')
        if response.status_code >= 400:
            logger.warning('request_id=%s method=%s route=%s status=%s duration_ms=%s',
                           request_id, request.method, route, response.status_code, duration)
        if (len(pending) < 64 and not route.startswith(('/analytics', '/admin'))
                and os.getenv('POSTHOG_PROJECT_KEY', '').startswith('phc_')
                and request.headers.get('X-LANIS-Analytics-Consent') == 'account-v2'
                and request.headers.get('X-Session-Token')):
            task = asyncio.create_task(backend_event(request, route, response.status_code, duration, request_id))
            pending.add(task)
            task.add_done_callback(pending.discard)
        return response
