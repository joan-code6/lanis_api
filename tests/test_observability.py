"""Exercise actual SDK batches with in-memory exporters: no fixture leaves the process."""
import asyncio
import json
import logging
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from api import observability as telemetry


@pytest.fixture
def capture(monkeypatch):
    from opentelemetry.sdk.trace.export import SpanExportResult
    from opentelemetry.sdk._logs.export import LogRecordExportResult
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as traces
    import opentelemetry.exporter.otlp.proto.http._log_exporter as logs

    class Captured(list):
        pass
    spans, records, destinations = Captured(), Captured(), []

    class SpanExporter:
        def __init__(self, **kwargs):
            destinations.append(kwargs['endpoint'])
            spans.exporter = self
            self.stopped = threading.Event()

        def export(self, batch):
            spans.extend(batch)
            return SpanExportResult.SUCCESS

        def shutdown(self):
            self.stopped.set()

    class LogExporter:
        def __init__(self, **kwargs):
            destinations.append(kwargs['endpoint'])
            records.exporter = self

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


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_prepared_session_keeps_method_without_forwarding_metadata(capture, monkeypatch, method):
    import requests
    from schulportal_hessen.tools.transport import ObservedSession
    spans, _, _ = capture
    received = []

    def send(session, request, **kwargs):
        received.append(kwargs)
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response._content = b'{}'
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, 'send', send)
    ObservedSession().request(method, 'https://start.schulportal.hessen.de/test')
    flush()
    outbound = [span for span in spans if span.attributes.get('operation') == 'http.outbound']
    assert outbound[-1].attributes['http.request.method'] == method
    assert 'telemetry_method' not in received[0]


def test_boolean_validation_and_handled_job_failures(capture, monkeypatch):
    from api import daily_cache, dsb_snapshot, uptime
    from schulportal_hessen.applets.login.api import sid_validator
    from unittest.mock import AsyncMock
    spans, _, _ = capture
    monkeypatch.setattr(uptime, 'run_uptime_check', AsyncMock(return_value={'status': 'down'}))
    assert asyncio.run(daily_cache.warm_users()) is False
    assert sid_validator(SimpleNamespace(logged_in=False))['valid'] is False
    for key in ['DSB_USERNAME', 'DSB_PASSWORD', 'DSB_SCHOOL_ID']:
        monkeypatch.setenv(key, 'fixture')
    monkeypatch.setattr(dsb_snapshot, 'SchulportalHessenAPI', lambda: (_ for _ in ()).throw(RuntimeError('PRIVATE')))
    assert asyncio.run(dsb_snapshot._fetch_and_store_dsb_snapshot()) is None
    flush()
    outcomes = {span.attributes.get('operation'): span.attributes.get('outcome') for span in spans}
    assert outcomes['api.daily_cache.warm_users'] == 'business_failure'
    assert outcomes['schulportal_hessen.applets.login.api.sid_validator'] == 'business_failure'
    assert outcomes['api.dsb_snapshot._fetch_and_store_dsb_snapshot'] == 'failed'


@pytest.mark.parametrize('fail', [False, True])
@pytest.mark.parametrize('distribution', [False, True])
def test_lazy_stream_span_survives_thread_handoff_and_late_failure(capture, fail, distribution):
    from concurrent.futures import ThreadPoolExecutor
    from schulportal_hessen.applets.dateispeicher.api import _iter_download_response
    from schulportal_hessen.applets.dateiverteilung.api import _stream
    spans, _, _ = capture
    closed = []
    iterator = _stream if distribution else _iter_download_response
    suffix = '.dateiverteilung_download_file' if distribution else '.dateispeicher_download_file'

    def chunks():
        yield b'second'
        if fail:
            raise RuntimeError('PRIVATE stream failure')

    stream = iterator(SimpleNamespace(close=lambda: closed.append(True)), b'first', chunks())
    with ThreadPoolExecutor(max_workers=1) as first, ThreadPoolExecutor(max_workers=1) as second:
        assert first.submit(next, stream).result() == b'first'
        flush()
        assert not [span for span in spans if span.name.endswith(suffix)]
        assert second.submit(next, stream).result() == b'second'
        with pytest.raises(RuntimeError if fail else StopIteration):
            first.submit(next, stream).result()
    flush()
    span = next(span for span in spans if span.name.endswith(suffix))
    assert span.attributes['outcome'] == ('failed' if fail else 'success')
    assert closed == [True]


@pytest.mark.parametrize('timeout', [False, True])
def test_dsb_failure_does_not_poison_schulportal_fail_fast(monkeypatch, timeout):
    import requests
    from schulportal_hessen.external.dsb.api import _make_dsb_session
    from schulportal_hessen.tools.transport import ObservedSession, TransportObservation, observation
    calls = []

    def send(session, request, **kwargs):
        calls.append(request.url)
        if timeout and 'dsbmobile' in request.url:
            raise requests.Timeout('fixture')
        response = requests.Response()
        response.status_code = 500
        response.url = request.url
        response._content = b'{}'
        response._content_consumed = True
        return response

    monkeypatch.setattr(requests.Session, 'send', send)
    current = TransportObservation(fail_fast=True)
    token = observation.set(current)
    try:
        if timeout:
            with pytest.raises(requests.Timeout):
                _make_dsb_session().get('https://www.dsbmobile.de/test')
        else:
            assert _make_dsb_session().get('https://www.dsbmobile.de/test').status_code == 500
        assert observation.get() is current and not current.unavailable
        assert ObservedSession().get('https://start.schulportal.hessen.de/test').status_code == 500
        assert current.unavailable
        with pytest.raises(requests.ConnectionError):
            ObservedSession().get('https://start.schulportal.hessen.de/test')
        assert len(calls) == 2
    finally:
        observation.reset(token)


def test_sdk_queue_drop_metrics_are_counted_once_for_both_signals(capture, monkeypatch):
    import threading
    from opentelemetry.sdk.trace.export import SpanExportResult
    from opentelemetry.sdk._logs.export import LogRecordExportResult
    spans, logs, _ = capture
    release, trace_entered, log_entered = threading.Event(), threading.Event(), threading.Event()
    before = telemetry.health()['delivery']

    def blocked_spans(batch):
        trace_entered.set()
        assert release.wait(5)
        return SpanExportResult.SUCCESS

    def blocked_logs(batch):
        log_entered.set()
        assert release.wait(5)
        return LogRecordExportResult.SUCCESS

    monkeypatch.setattr(spans.exporter, 'export', blocked_spans)
    monkeypatch.setattr(logs.exporter, 'export', blocked_logs)
    try:
        for _ in range(128):
            with telemetry._tracer.start_as_current_span('queue.fixture'):
                pass
            telemetry.event('request.completed')
        assert trace_entered.wait(1) and log_entered.wait(1)
        for _ in range(2200):
            with telemetry._tracer.start_as_current_span('queue.fixture'):
                pass
            telemetry.event('request.completed')
        counted = telemetry.health()['delivery']
        for signal in ['traces', 'logs']:
            assert counted[signal + '.dropped'] >= before.get(signal + '.dropped', 0) + 152
        assert telemetry.health()['delivery'] == counted
    finally:
        release.set()
    flush()


def test_shutdown_has_one_deadline_and_accounts_for_unfinished_export(capture, monkeypatch):
    import threading
    import time
    from opentelemetry.sdk.trace.export import SpanExportResult
    spans, _, _ = capture
    release, entered = threading.Event(), threading.Event()
    before = telemetry.health()['delivery'].get('traces.shutdown_unfinished', 0)

    def blocked(batch):
        entered.set()
        assert release.wait(5)
        return SpanExportResult.SUCCESS

    monkeypatch.setattr(spans.exporter, 'export', blocked)
    try:
        for _ in range(128):
            with telemetry._tracer.start_as_current_span('shutdown.fixture'):
                pass
        assert entered.wait(1)
        for _ in range(256):
            with telemetry._tracer.start_as_current_span('shutdown.fixture'):
                pass
        started = time.monotonic()
        telemetry.shutdown(timeout_millis=100)
        assert time.monotonic() - started < 0.75
        assert telemetry.health()['delivery']['traces.shutdown_unfinished'] >= before + 256
    finally:
        release.set()
    assert spans.exporter.stopped.wait(2)
    assert telemetry.health()['delivery']['traces.shutdown_dropped'] >= 256



def test_aiosqlite_shared_worker_keeps_each_requests_trace_context(capture):
    import aiosqlite
    from opentelemetry.trace import get_current_span
    spans, logs, _ = capture
    parents = {}
    observed = []
    main_thread = threading.get_ident()

    def worker_trace():
        observed.append((telemetry.trace_id(), threading.get_ident()))
        return telemetry.trace_id()

    async def scenario():
        # The worker exists before either request; context must be copied per call.
        async with aiosqlite.connect(':memory:') as db:
            await db.create_function('worker_trace', 0, worker_trace)

            async def request(name):
                with telemetry.operation(name):
                    parent = get_current_span().get_span_context()
                    parents[name] = parent
                    async with db.execute('SELECT worker_trace(), ?', ('PRIVATE',)) as cursor:
                        row = await cursor.fetchone()
                        assert row == (f'{parent.trace_id:032x}', 'PRIVATE')
                    # Also exercise the cursor API, which uses native SQLite tracing.
                    async with db.cursor() as cursor:
                        await cursor.execute('SELECT worker_trace()')
                        assert (await cursor.fetchone())[0] == f'{parent.trace_id:032x}'

            await asyncio.gather(request('test.async.request_a'), request('test.async.request_b'))
            with telemetry.operation('test.async.failed_request'):
                parents['test.async.failed_request'] = get_current_span().get_span_context()
                with pytest.raises(sqlite3.OperationalError):
                    await db.execute('SELECT PRIVATE_MISSING_COLUMN')

    asyncio.run(scenario())
    flush()
    for name, parent in parents.items():
        children = [span for span in spans if span.attributes.get('operation') == 'sqlite.async'
            and span.context.trace_id == parent.trace_id]
        assert children and all(span.parent.span_id == parent.span_id for span in children)
        if name.endswith('failed_request'):
            assert any(span.attributes['outcome'] == 'failed' for span in children)
    request_ids = {f'{parent.trace_id:032x}' for parent in parents.values()}
    assert len(request_ids) == 3
    assert all(trace in request_ids and thread != main_thread for trace, thread in observed)
    native = [span for span in spans if span.attributes.get('db.system') == 'sqlite'
        and span.attributes.get('operation') != 'sqlite.async']
    assert native and all(f'{span.context.trace_id:032x}' in request_ids for span in native)
    exported = repr([(span.name, dict(span.attributes), span.events) for span in spans])
    exported += repr([(record.body, dict(record.attributes)) for record in logs])
    assert 'PRIVATE' not in exported


def test_aiosqlite_context_bridge_is_removed_on_shutdown(capture):
    import aiosqlite
    original, installed = telemetry._aiosqlite_bridge
    assert aiosqlite.Connection._execute is installed
    telemetry.shutdown()
    assert aiosqlite.Connection._execute is original
