"""Exercise actual SDK batches with in-memory exporters: no fixture leaves the process."""
import asyncio
import json
import logging
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from api import observability as telemetry


@pytest.fixture
def capture(monkeypatch):
    from opentelemetry.sdk.trace.export import SpanExportResult
    from opentelemetry.sdk._logs.export import LogRecordExportResult
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as traces
    import opentelemetry.exporter.otlp.proto.http._log_exporter as logs

    spans, records, destinations = [], [], []

    class SpanExporter:
        def __init__(self, **kwargs):
            destinations.append(kwargs['endpoint'])

        def export(self, batch):
            spans.extend(batch)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            pass

    class LogExporter:
        def __init__(self, **kwargs):
            destinations.append(kwargs['endpoint'])

        def export(self, batch):
            records.extend(row.log_record for row in batch)
            return LogRecordExportResult.SUCCESS

        def shutdown(self):
            pass

    monkeypatch.setenv('POSTHOG_PROJECT_KEY', 'phc_fixture')
    monkeypatch.setenv('POSTHOG_OBSERVABILITY', '1')
    monkeypatch.setattr(traces, 'OTLPSpanExporter', SpanExporter)
    monkeypatch.setattr(logs, 'OTLPLogExporter', LogExporter)
    telemetry.configure()
    assert telemetry.health()['enabled']
    yield spans, records, destinations
    telemetry.shutdown()


def flush():
    assert telemetry._provider.force_flush()
    assert telemetry._logs.force_flush()


def test_real_exporters_strip_exception_sql_url_log_args_and_extras(capture):
    spans, logs, destinations = capture
    with pytest.raises(RuntimeError):
        with telemetry.operation('test.failure'):
            raise RuntimeError('PRIVATE password=SECRET')
    with telemetry._tracer.start_as_current_span('PRIVATE URL') as span:
        span.set_attribute('url.full', 'https://private.invalid/?token=SECRET')
        span.add_event('PRIVATE exception', {'message': 'SECRET'})
    with sqlite3.connect(':memory:') as connection:
        connection.execute("SELECT 'SECRET private student'").fetchone()
    record = logging.LogRecord('arbitrary-private-logger', logging.ERROR,
        str(Path(telemetry.__file__).with_name('api.py')), 123,
        'PRIVATE %s', ('SECRET',), (RuntimeError, RuntimeError('SECRET'), None))
    record.password = 'SECRET'
    telemetry._handler.handle(record)
    flush()
    exported = repr([(s.name, dict(s.attributes), list(s.events), s.status.description) for s in spans])
    exported += repr([(r.body, dict(r.attributes)) for r in logs])
    assert 'SECRET' not in exported and 'PRIVATE' not in exported
    assert any(s.attributes.get('operation') == 'test.failure' and s.attributes['outcome'] == 'failed' for s in spans)
    assert any(r.body == 'Application log' and r.attributes['source'] == 'api/api.py' for r in logs)
    assert set(destinations) == {'https://eu.i.posthog.com/i/v1/logs', 'https://eu.i.posthog.com/i/v1/traces'}


def test_request_span_covers_body_failure_and_uses_route_template(capture):
    spans, logs, _ = capture

    async def scenario(fail):
        scope = {'type': 'http', 'method': 'GET', 'path': '/files/PRIVATE',
            'query_string': b'password=SECRET', 'headers': [(b'authorization', b'SECRET')],
            'state': {'analytics_request_id': '12345678-1234-1234-1234-123456789abc'}}
        sent = []

        async def app(scope, receive, send):
            scope['route'] = SimpleNamespace(path='/files/{id}')
            with telemetry.operation('test.child'):
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'SECRET', 'more_body': fail})
                if fail:
                    raise RuntimeError('PRIVATE stream exception')

        async def send(message):
            sent.append(message)

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        middleware = telemetry.ObservabilityMiddleware(app)
        if fail:
            with pytest.raises(RuntimeError):
                await middleware(scope, receive, send)
        else:
            await middleware(scope, receive, send)
        trace_id = dict(sent[0]['headers'])[b'x-lanis-trace-id'].decode()
        return trace_id

    first = asyncio.run(scenario(False))
    second = asyncio.run(scenario(True))
    flush()
    requests = [s for s in spans if s.name == 'GET /files/{id}']
    assert len(requests) == 2
    assert {s.attributes['outcome'] for s in requests} == {'success', 'failed'}
    assert all(s.attributes['bytes'] == 6 for s in requests)
    assert {f'{s.context.trace_id:032x}' for s in requests} == {first, second}
    for s in requests:
        assert any(child.parent.span_id == s.context.span_id for child in spans if child.parent)
    assert 'PRIVATE' not in repr([(s.name, dict(s.attributes), s.events) for s in spans])


def test_client_context_requires_consent_and_valid_uuid(capture):
    spans, _, _ = capture

    async def scenario(consent):
        async def app(scope, receive, send):
            scope['route'] = SimpleNamespace(path='/test')
            await send({'type': 'http.response.start', 'status': 401})
            await send({'type': 'http.response.body', 'body': b''})

        async def ignore(_):
            pass

        headers = [(b'x-lanis-session-id', b'12345678-1234-1234-1234-123456789abc'),
            (b'x-lanis-client-request-id', b'PRIVATE')]
        if consent:
            headers.append((b'x-lanis-analytics-consent', b'account-v2'))
        await telemetry.ObservabilityMiddleware(app)({'type': 'http', 'method': 'GET', 'headers': headers}, ignore, ignore)

    asyncio.run(scenario(False))
    asyncio.run(scenario(True))
    flush()
    requests = [s for s in spans if s.name == 'GET /test']
    assert len(requests) == 2
    assert sum('sessionId' in s.attributes for s in requests) == 1
    assert all('client_request_id' not in s.attributes for s in requests)


def test_observed_http_does_not_inject_headers_and_keeps_business_failure(capture):
    spans, _, _ = capture
    options = []

    def provider(url, **kwargs):
        options.append(kwargs)
        return SimpleNamespace(status_code=503)

    telemetry.observed_http(provider, 'https://start.schulportal.hessen.de/private?token=SECRET', headers={'existing': 'unchanged'})

    @telemetry.observe('test.business')
    def business():
        return {'success': False, 'error': 'PRIVATE'}

    assert business()['success'] is False
    flush()
    assert options == [{'headers': {'existing': 'unchanged'}}]
    assert any(s.attributes.get('dependency') == 'schulportal' for s in spans)
    assert any(s.attributes.get('outcome') == 'business_failure' for s in spans)


def test_missing_config_does_not_require_optional_packages_or_break_boot(monkeypatch):
    monkeypatch.delenv('POSTHOG_PROJECT_KEY', raising=False)
    telemetry.configure()
    assert not telemetry.health()['enabled']
    with telemetry.operation('test.noop'):
        pass
