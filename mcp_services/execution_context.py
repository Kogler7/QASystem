"""Worker execution metadata is task-local, not process configuration."""
from contextlib import contextmanager
from contextvars import ContextVar

_worker_activity = ContextVar('mcp_worker_activity', default=False)


def in_worker_activity():
    return _worker_activity.get()


@contextmanager
def worker_activity():
    token = _worker_activity.set(True)
    try:
        yield
    finally:
        _worker_activity.reset(token)
