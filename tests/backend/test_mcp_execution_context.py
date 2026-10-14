import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from mcp_services import execution_context as context
from mcp_services.dispatch import _execution_mode


def test_nested_activity_and_exception_restore_state():
    assert not context.in_worker_activity()
    with pytest.raises(ValueError):
        with context.worker_activity():
            assert context.in_worker_activity()
            with context.worker_activity():
                assert context.in_worker_activity()
            assert context.in_worker_activity()
            raise ValueError('fixture failure')
    assert not context.in_worker_activity()


def test_worker_activity_does_not_change_parallel_request_context(monkeypatch):
    monkeypatch.setenv('MCP_EXECUTION_MODE', 'sync')
    barrier = Barrier(2)
    def worker():
        with context.worker_activity():
            barrier.wait(timeout=5)
            barrier.wait(timeout=5)
            return _execution_mode()
    def request():
        barrier.wait(timeout=5)
        result = _execution_mode()
        barrier.wait(timeout=5)
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker_result = pool.submit(worker)
        request_result = pool.submit(request)
        assert worker_result.result(timeout=5) == 'inline'
        assert request_result.result(timeout=5) == 'sync'


def test_worker_activity_isolated_between_async_tasks():
    async def run():
        started = asyncio.Event()
        observed = asyncio.Event()
        async def worker():
            with context.worker_activity():
                started.set()
                await observed.wait()
                return context.in_worker_activity()
        async def request():
            await started.wait()
            result = context.in_worker_activity()
            observed.set()
            return result
        return await asyncio.gather(worker(), request())
    assert asyncio.run(run()) == [True, False]


def test_worker_activity_does_not_mutate_process_configuration(monkeypatch):
    monkeypatch.setenv('MCP_EXECUTION_MODE', 'async')
    from qasystem_config.configuration import startup_getenv
    from backend.app.execution.operations import _mcp_activity_context
    with _mcp_activity_context():
        assert _execution_mode() == 'inline'
        assert startup_getenv('MCP_EXECUTION_MODE') == 'async'
    assert _execution_mode() == 'async'
