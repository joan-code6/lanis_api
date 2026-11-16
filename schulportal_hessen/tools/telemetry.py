"""Optional observation hooks; standalone sph-client has no server dependency."""
from contextlib import nullcontext
from functools import wraps
import inspect

_observer = None
_http_observer = None


def set_observer(observer=None, http_observer=None):
    global _observer, _http_observer
    _observer, _http_observer = observer, http_observer


def observe(name):
    def decorate(fn):
        def context():
            return _observer(name) if _observer else nullcontext({})

        if inspect.iscoroutinefunction(fn):
            @wraps(fn)
            async def asynchronous(*args, **kwargs):
                with context() as attributes:
                    result = await fn(*args, **kwargs)
                    if isinstance(result, dict) and result.get('success') is False:
                        attributes['outcome'] = 'business_failure'
                    return result
            return asynchronous

        @wraps(fn)
        def synchronous(*args, **kwargs):
            with context() as attributes:
                result = fn(*args, **kwargs)
                if isinstance(result, dict) and result.get('success') is False:
                    attributes['outcome'] = 'business_failure'
                return result
        return synchronous
    return decorate


def observed_http(fn, url, *args, **kwargs):
    if _http_observer:
        return _http_observer(fn, url, *args, **kwargs)
    return fn(url, *args, **kwargs)
