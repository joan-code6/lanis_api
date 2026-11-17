"""Optional observation hooks; standalone sph-client has no server dependency."""
from contextlib import nullcontext
from functools import wraps
import inspect

_observer = None
_http_observer = None


def set_observer(observer=None, http_observer=None):
    global _observer, _http_observer
    _observer, _http_observer = observer, http_observer


def observe(name, *, failed=None):
    def decorate(fn):
        def context():
            return _observer(name) if _observer else nullcontext({})

        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def asynchronous(*args, **kwargs):
                with context() as attributes:
                    result = await fn(*args, **kwargs)
                    if (failed(result) if failed else isinstance(result, dict) and result.get('success') is False):
                        attributes['outcome'] = 'business_failure'
                    return result
            return asynchronous

        @wraps(fn)
        def synchronous(*args, **kwargs):
            with context() as attributes:
                result = fn(*args, **kwargs)
                if (failed(result) if failed else isinstance(result, dict) and result.get('success') is False):
                    attributes['outcome'] = 'business_failure'
                return result
        return synchronous
    return decorate


def observed_http(fn, url, *args, telemetry_method=None, **kwargs):
    if _http_observer:
        return _http_observer(fn, url, *args, telemetry_method=telemetry_method, **kwargs)
    return fn(url, *args, **kwargs)


def observe_stream(name):
    """Measure an iterator while consumed, preserving lazy IO and cleanup."""
    def decorate(fn):
        @wraps(fn)
        def stream(*args, **kwargs):
            context = _observer(name, activate=False) if _observer else nullcontext({})
            with context:
                yield from fn(*args, **kwargs)
        return stream
    return decorate
