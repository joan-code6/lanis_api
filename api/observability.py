"""Bounded, content-free operational logs and traces. No account lookup or payload capture.

Only this module owns exporters. Product analytics retains its separate consent
and deletion contract. In particular, raw application logs never reach OTLP.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import contextmanager, nullcontext
from functools import wraps
import inspect
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit

_provider = None
_logs = None
_handler = None
_tracer = None
_sqlite = None
_lock = threading.Lock()
_counts = Counter()
_last_success = {}
_operations = set()
_routes = set()
_root = Path(__file__).resolve().parents[1]
_sources = {str(p.resolve()): p.relative_to(_root).as_posix()
            for folder in ('api', 'schulportal_hessen', 'sph_client')
            for p in (_root / folder).rglob('*.py')}
_uuid = re.compile(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$')
_hex = re.compile(r'^[a-f0-9]{32}$')
_methods = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}
_outcomes = {'success', 'failed', 'cancelled', 'business_failure', 'incomplete'}
_cache = {'hit', 'fresh', 'stale', 'miss'}
_events = {'request.completed', 'operation.completed', 'service.started', 'service.stopped'}


def count(name):
    with _lock:
        _counts[name] += 1
        if name.endswith('.sent'):
            _last_success[name.split('.')[0]] = int(time.time())


def health():
    with _lock:
        return {'enabled': _tracer is not None, 'delivery': dict(_counts),
                'last_success_unix': dict(_last_success),
                'events_configured': bool(re.fullmatch(r'phc_[A-Za-z0-9]+', os.getenv('POSTHOG_PROJECT_KEY', ''))
                    and os.getenv('POSTHOG_HOST', 'https://t.lanis.arg-server.de') in
                    {'https://t.lanis.arg-server.de', 'https://eu.i.posthog.com'})}


def release_labels():
    attributes = _provider.resource.attributes if _provider else {}
    release = attributes.get('service.version', os.getenv('LANIS_RELEASE', ''))
    environment = os.getenv('LANIS_ENVIRONMENT', 'production')
    return {'release': release if re.fullmatch(r'[a-f0-9]{7,40}', release) else 'unknown',
            'environment': environment if environment in {'production', 'staging', 'development', 'test'} else 'other'}


def safe_attributes(values):
    """Last-mile allowlist, including spans created by instrumented libraries."""
    safe = {}
    for key, value in values.items():
        if key in {'http.request.method', 'method'} and value in _methods:
            safe[key] = value
        elif key in {'http.route', 'endpoint'} and value in _routes:
            safe[key] = value
        elif key == 'db.system' and value == 'sqlite':
            safe[key] = value
        elif key == 'db.operation' and value in {'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'PRAGMA', 'COMMIT', 'BEGIN'}:
            safe[key] = value
        elif key == 'operation' and value in _operations:
            safe[key] = value
        elif key == 'outcome' and value in _outcomes:
            safe[key] = value
        elif key == 'cache' and value in _cache:
            safe[key] = value
        elif key == 'dependency' and value in {'schulportal', 'dsb', 'google', 'whatsapp', 'discord', 'external'}:
            safe[key] = value
        elif key in {'http.response.status_code', 'status'} and isinstance(value, int) and 100 <= value <= 599:
            safe[key] = value
        elif key in {'duration_ms', 'ttfb_ms', 'bytes', 'attempt', 'queue_wait_ms'} and isinstance(value, (int, float)) and math.isfinite(value):
            safe[key] = max(0, min(value, 3_600_000_000))
        elif key in {'request_id', 'client_request_id', 'sessionId'} and isinstance(value, str) and _uuid.fullmatch(value):
            safe[key] = value
        elif key == 'trace_id' and isinstance(value, str) and _hex.fullmatch(value):
            safe[key] = value
    return safe


def event(name, **attributes):
    if name not in _events or _logs is None:
        return
    # Use a dedicated logger directly: root filtering must never re-export it.
    from opentelemetry._logs import SeverityNumber
    try:
        _logs.get_logger('lanis.operations').emit(
            body=name, severity_number=SeverityNumber.INFO,
            attributes=safe_attributes(attributes),
        )
    except Exception:
        count('logs.emit_failed')


def observe(name):
    """Decorator for known IO/job boundaries, never arguments or return contents."""
    _operations.add(name)

    def decorate(fn):
        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def asynchronous(*args, **kwargs):
                with operation(name) as span:
                    result = await fn(*args, **kwargs)
                    if isinstance(result, dict) and result.get('success') is False:
                        span['outcome'] = 'business_failure'
                    return result
            return asynchronous

        @wraps(fn)
        def synchronous(*args, **kwargs):
            with operation(name) as span:
                result = fn(*args, **kwargs)
                if isinstance(result, dict) and result.get('success') is False:
                    span['outcome'] = 'business_failure'
                return result
        return synchronous
    return decorate


@contextmanager
def operation(name, **attributes):
    _operations.add(name)
    values = {'operation': name, **attributes, 'outcome': 'success'}
    started = time.monotonic()
    if _tracer:
        from opentelemetry.trace import SpanKind
    manager = (_tracer.start_as_current_span(name, kind=SpanKind.CLIENT if name == 'http.outbound' else SpanKind.INTERNAL, record_exception=False,
                set_status_on_exception=False) if _tracer else nullcontext(None))
    with manager as span:
        try:
            yield values
        except BaseException as error:
            values['outcome'] = 'cancelled' if isinstance(error, asyncio.CancelledError) else 'failed'
            raise
        finally:
            values['duration_ms'] = round((time.monotonic() - started) * 1000)
            if span:
                from opentelemetry.trace import Status, StatusCode
                span.set_attributes(safe_attributes(values))
                if values['outcome'] in {'failed', 'business_failure'}:
                    span.set_status(Status(StatusCode.ERROR))
            event('operation.completed', **values)


def observed_http(fn, url, *args, **kwargs):
    """Outbound IO without propagating LANIS identity/trace headers to providers."""
    host = urlsplit(url).hostname or ''
    dependency = ('schulportal' if host.endswith('.schulportal.hessen.de') else
                  'dsb' if host.endswith('.dsbmobile.de') else
                  'google' if host.endswith('.googleapis.com') else
                  'whatsapp' if host == 'graph.facebook.com' else
                  'discord' if host == 'discord.com' else 'external')
    name = getattr(fn, '__name__', '')
    method = name.upper() if isinstance(name, str) else ''
    with operation('http.outbound', dependency=dependency, **{'http.request.method': method}) as attributes:
        response = fn(url, *args, **kwargs)
        status = getattr(response, 'status_code', 0)
        if isinstance(status, int):
            attributes['status'] = status
            attributes['http.response.status_code'] = status
        if isinstance(status, int) and status >= 400:
            attributes['outcome'] = 'failed'
        return response


def trace_id():
    if _tracer:
        from opentelemetry.trace import get_current_span
        context = get_current_span().get_span_context()
        if context.is_valid:
            return f'{context.trace_id:032x}'
    return None


class ObservabilityMiddleware:
    """ASGI lifecycle instrumentation: includes stream completion and disconnects."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or _tracer is None:
            return await self.app(scope, receive, send)
        from opentelemetry.trace import SpanKind, Status, StatusCode
        started = time.monotonic()
        status, size, complete, ttfb = 500, 0, False, 0
        attributes = {'method': scope.get('method')}
        headers = dict(scope.get('headers', []))
        consent = headers.get(b'x-lanis-analytics-consent') == b'account-v2'
        if consent:
            for header, key in ((b'x-lanis-client-request-id', 'client_request_id'), (b'x-lanis-session-id', 'sessionId')):
                value = headers.get(header, b'').decode('ascii', errors='ignore')
                if _uuid.fullmatch(value):
                    attributes[key] = value
        # Start a local trace. Do not accept arbitrary baggage or external IDs.
        with _tracer.start_as_current_span('http.request', kind=SpanKind.SERVER,
                record_exception=False, set_status_on_exception=False) as span:
            async def observed_send(message):
                nonlocal status, size, complete, ttfb
                if message['type'] == 'http.response.start':
                    status = message['status']
                    ttfb = round((time.monotonic() - started) * 1000)
                    message = {**message, 'headers': list(message.get('headers', []))}
                    message['headers'].append((b'x-lanis-trace-id', f'{span.get_span_context().trace_id:032x}'.encode()))
                    for key, value in message['headers']:
                        if key.lower() == b'x-lanis-cache':
                            attributes['cache'] = value.decode('ascii', errors='ignore')
                await send(message)
                if message['type'] == 'http.response.body':
                    size += len(message.get('body', b''))
                    complete = not message.get('more_body', False)

            outcome = 'success'
            try:
                await self.app(scope, receive, observed_send)
            except BaseException as error:
                outcome = 'cancelled' if isinstance(error, asyncio.CancelledError) else 'failed'
                raise
            finally:
                route = getattr(scope.get('route'), 'path', '/unmatched')
                _routes.add(route)
                request_id = scope.get('state', {}).get('analytics_request_id')
                attributes.update(endpoint=route, request_id=request_id, status=status,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    ttfb_ms=ttfb, bytes=size, outcome=(outcome if outcome != 'success' else
                        'failed' if status >= 500 else 'incomplete' if not complete else 'success'))
                attributes.update({'http.request.method': scope.get('method'),
                    'http.route': route, 'http.response.status_code': status})
                span.update_name(f'{scope.get("method") if scope.get("method") in _methods else "HTTP"} {route}')
                span.set_attributes(safe_attributes(attributes))
                if attributes['outcome'] in {'failed', 'incomplete'}:
                    span.set_status(Status(StatusCode.ERROR))
                event('request.completed', **attributes)


def configure():
    """Called once at startup. Explicit providers avoid altering global tracing."""
    global _provider, _logs, _handler, _tracer, _sqlite
    if _provider is not None:
        return
    key = os.getenv('POSTHOG_PROJECT_KEY', '')
    if not re.fullmatch(r'phc_[A-Za-z0-9]+', key):
        if os.getenv('LANIS_ENVIRONMENT', 'development') != 'production':
            logging.getLogger(__name__).warning('POSTHOG_PROJECT_KEY variable required by PostHog is missing or un-configured, this causes events to be silently missed. This error stops appearing once POSTHOG_PROJECT_KEY is configured')
        return
    if os.getenv('POSTHOG_OBSERVABILITY', '1') != '1':
        return
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult
        from opentelemetry.trace import Status, StatusCode
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter, LogRecordExportResult
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

        release = os.getenv('LANIS_RELEASE', '')
        if not release:
            try:
                release = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=_root,
                    capture_output=True, text=True, timeout=1, check=True).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                release = ''
        environment = os.getenv('LANIS_ENVIRONMENT', 'production')
        resource = Resource({'service.name': 'lanis_api', 'service.version': release if re.fullmatch(r'[a-f0-9]{7,40}', release) else 'unknown',
            'deployment.environment': environment if environment in {'production', 'staging', 'development', 'test'} else 'other'})

        class SafeSpans(SpanExporter):
            def __init__(self, exporter):
                self.exporter = exporter

            def export(self, spans):
                # Rebuild rather than mutate immutable spans. Drop events, links,
                # status descriptions and instrumentation metadata (may contain SQL/URLs).
                sanitized = []
                for span in spans:
                    attrs = safe_attributes(span.attributes or {})
                    name = attrs.get('operation', 'operation')
                    if attrs.get('endpoint'):
                        name = f'{attrs.get("method", "HTTP")} {attrs["endpoint"]}'
                    sanitized.append(ReadableSpan(name=name, context=span.context,
                        parent=span.parent, resource=resource, attributes=attrs,
                        kind=span.kind, status=Status(span.status.status_code),
                        start_time=span.start_time, end_time=span.end_time))
                try:
                    result = self.exporter.export(sanitized)
                except Exception:
                    result = SpanExportResult.FAILURE
                count('traces.sent' if result == SpanExportResult.SUCCESS else 'traces.failed')
                return result

            def shutdown(self):
                self.exporter.shutdown()

        class CountedLogs(LogRecordExporter):
            def __init__(self, exporter):
                self.exporter = exporter

            def export(self, batch):
                try:
                    result = self.exporter.export(batch)
                except Exception:
                    result = LogRecordExportResult.FAILURE
                count('logs.sent' if result == LogRecordExportResult.SUCCESS else 'logs.failed')
                return result

            def shutdown(self):
                self.exporter.shutdown()

            def force_flush(self, timeout_millis=3000):
                flush = getattr(self.exporter, 'force_flush', None)
                return flush(timeout_millis) if flush else True

        auth = {'Authorization': f'Bearer {key}'}
        # Send OTLP directly to EU: the analytics proxy need not support OTLP.
        _provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        _provider.add_span_processor(BatchSpanProcessor(SafeSpans(OTLPSpanExporter(
            endpoint='https://eu.i.posthog.com/i/v1/traces', headers=auth, timeout=3)),
            max_queue_size=2048, max_export_batch_size=128, schedule_delay_millis=5000))
        _logs = LoggerProvider(resource=resource, shutdown_on_exit=False)
        _logs.add_log_record_processor(BatchLogRecordProcessor(CountedLogs(OTLPLogExporter(
            endpoint='https://eu.i.posthog.com/i/v1/logs', headers=auth, timeout=3)),
            max_queue_size=2048, max_export_batch_size=128, schedule_delay_millis=5000))
        _tracer = _provider.get_tracer('lanis.operations')
        from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
        _sqlite = SQLite3Instrumentor()
        _sqlite.instrument(tracer_provider=_provider)
        bridge = LoggingHandler(logger_provider=_logs)

        class SafeLoggingHandler(logging.Handler):
            def emit(self, record):
                source = _sources.get(record.pathname)
                if source is None or record.name.startswith('api.observability'):
                    return  # Includes exporter/HTTP-library logs: no recursion.
                try:
                    # Never format the original record or forward args/exc_info/extras.
                    clone = logging.LogRecord('lanis.application', record.levelno,
                        '', 0, 'Application log', (), None)
                    clone.source = source
                    clone.source_line = record.lineno
                    clone.created = record.created
                    bridge.handle(clone)
                except Exception:
                    count('logs.emit_failed')

        _handler = SafeLoggingHandler(level=logging.INFO)
        logging.getLogger().addHandler(_handler)
        from schulportal_hessen.tools.telemetry import set_observer
        set_observer(operation, observed_http)
        event('service.started')
    except Exception:
        count('configuration.failed')
        logging.getLogger(__name__).warning('PostHog observability unavailable; inspect telemetry health')
        shutdown()


def shutdown():
    global _provider, _logs, _handler, _tracer, _sqlite
    if _sqlite:
        _sqlite.uninstrument()
        _sqlite = None
    if _handler:
        logging.getLogger().removeHandler(_handler)
        _handler = None
    event('service.stopped')
    from schulportal_hessen.tools.telemetry import set_observer
    set_observer()
    providers = (_provider, _logs)
    _tracer = _provider = _logs = None
    for provider in providers:
        if provider:
            try:
                provider.force_flush(timeout_millis=3000)
                provider.shutdown()
            except Exception:
                count('shutdown.failed')
