"""Request-local cooperative tasks; threads execute one provider attempt only."""
from __future__ import annotations
from docker.configuration import getenv as config_getenv

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import wraps
import threading
from uuid import uuid4


class CallBudgetExceeded(RuntimeError):
    pass


class CallBudget:
    def __init__(self):
        self.limit = max(1, int(config_getenv('KG_EXTRACT_V31_MAX_CALLS', '512')))
        self.used = 0
        self.lock = threading.Lock()

    def claim(self):
        with self.lock:
            if self.used >= self.limit:
                raise CallBudgetExceeded('document model call budget exhausted')
            self.used += 1


_budget = ContextVar('v31_call_budget', default=None)
_runtime = ContextVar('v31_task_runtime', default=None)
_review_cache = ContextVar('v31_review_cache', default=None)


def document_tasks(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _budget.set(CallBudget())
        cache_token = _review_cache.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _budget.reset(token)
            _review_cache.reset(cache_token)
    return wrapped


class TaskRuntime:
    def __init__(self, workers, trace=None):
        from algorithm.extraction_trace import ExtractionTrace
        self.workers = max(1, int(workers))
        self.trace = trace if isinstance(trace, ExtractionTrace) else None
        self.budget = _budget.get() or CallBudget()
        # COSEC: Reuse verdicts only within this request, never across users/runs.
        self.review_cache = _review_cache.get()
        if self.review_cache is None:
            self.review_cache = {}
        self.executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix='ke-attempt')
        self.queues = {name: deque() for name in ('review', 'property', 'entity', 'skill')}
        self.order = deque(('review', 'review', 'property', 'entity', 'skill'))
        self.event = asyncio.Event()
        self.changed = asyncio.Event()
        self.delayed = 0
        self.capacity = asyncio.Semaphore(self.workers * 2)
        self.sequence = 0
        self.prefix = uuid4().hex[:12]
        self.closed = False
        self.metrics = {'submitted': 0, 'completed': 0, 'failed': 0, 'retry_delays': 0,
                        'retry_delay_seconds': 0.0, 'peak_active': 0, 'active': 0, 'reused_verdicts': 0}

    async def call(self, function, *, stage, llm):
        await self.capacity.acquire()
        future = asyncio.get_running_loop().create_future()
        self.sequence += 1
        task_id = f'v31-{self.prefix}-{self.sequence:06d}'
        category = ('review' if 'verification' in stage else 'property' if 'property' in stage
                    else 'skill' if stage == 'skill_selection' else 'entity')
        self.queues[category].append((future, function, task_id, llm, copy_context()))
        self.metrics['submitted'] += 1
        self.event.set()
        self.changed.set()
        try:
            return await future
        finally:
            self.capacity.release()

    def _pick(self):
        for _ in range(len(self.order)):
            category = self.order[0]
            self.order.rotate(-1)
            if self.queues[category]:
                return self.queues[category].popleft()
        return None

    async def _worker(self):
        while not self.closed:
            item = self._pick()
            if item is None:
                self.event.clear()
                await self.event.wait()
                continue
            future, function, task_id, llm, context = item
            if future.cancelled():
                continue
            def invoke():
                self.budget.claim()
                trace = getattr(llm, 'extraction_trace', None)
                old_id = getattr(getattr(trace, 'local', None), 'call_id', None)
                if trace is not None and hasattr(trace, 'local'):
                    trace.local.call_id = task_id
                try:
                    return function()
                except StopIteration as exc:
                    # asyncio futures cannot carry StopIteration. A synchronous
                    # adapter ending without a result must fail, not strand the
                    # awaiting request indefinitely.
                    raise RuntimeError('model call ended without a result') from exc
                finally:
                    if trace is not None and hasattr(trace, 'local'):
                        trace.local.call_id = old_id
            self.metrics['active'] += 1
            self.metrics['peak_active'] = max(self.metrics['peak_active'], self.metrics['active'])
            try:
                result = await asyncio.get_running_loop().run_in_executor(self.executor, context.run, invoke)
            except Exception as exc:
                self.metrics['failed'] += 1
                if not future.done():
                    future.set_exception(exc)
            else:
                if not future.done():
                    future.set_result(result)
            finally:
                self.metrics['active'] -= 1
                self.metrics['completed'] += 1
                self.changed.set()

    async def delay(self, seconds):
        if seconds > 0:
            self.metrics['retry_delays'] += 1
            self.metrics['retry_delay_seconds'] += seconds
            # No model slot or worker remains occupied during the retry delay.
            self.delayed += 1
            self.changed.set()
            try:
                await asyncio.sleep(seconds)
            finally:
                self.delayed -= 1
                self.changed.set()

    async def run(self, coroutine):
        token = _runtime.set(self)
        workers = [asyncio.create_task(self._worker()) for _ in range(self.workers)]
        try:
            return await coroutine
        finally:
            self.closed = True
            for queue in self.queues.values():
                for future, *_ in queue:
                    if not future.done():
                        future.cancel()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            self.executor.shutdown(wait=True, cancel_futures=True)
            if self.trace is not None:
                self.trace.event('task-runtime', {'runtime_id': self.prefix, 'workers': self.workers,
                    **self.metrics, 'document_calls': self.budget.used, 'call_limit': self.budget.limit})
            _runtime.reset(token)


def current_runtime():
    return _runtime.get()


def synchronous(function):
    """Keep the public sync contract; internal phases await the same coroutine."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        workers = kwargs.get('max_workers', 1)
        llm = kwargs.get('llm') or next((a for a in args if hasattr(a, 'extraction_trace')), None)
        trace = getattr(llm, 'extraction_trace', None)
        def run():
            return asyncio.run(TaskRuntime(workers, trace).run(function(*args, **kwargs)))
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return run()
        # Sync algorithm entry points may be called from an existing ASGI loop.
        # Copy the request context, but never serialize SDK/auth objects into tasks.
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='ke-coordinator') as executor:
            return executor.submit(context.run, run).result()
    wrapped.async_function = function
    return wrapped


async def invoke(function, *args, **kwargs):
    asynchronous = getattr(function, 'async_function', None)
    if asynchronous is not None:
        return await asynchronous(*args, **kwargs)
    # Compatible with existing in-process test/custom hooks.
    return function(*args, **kwargs)


async def ordered_tasks(function, items, *, limit):
    """Refill on completion; only the returned result order follows the input."""
    iterator = iter(enumerate(items))
    active = {}
    results = {}
    def fill():
        runtime = current_runtime()
        allowance = max(1, limit) + (runtime.delayed if runtime is not None else 0)
        while len(active) < allowance:
            try:
                index, item = next(iterator)
            except StopIteration:
                return
            active[asyncio.create_task(function(item))] = index
    fill()
    try:
        while active:
            runtime = current_runtime()
            wake = None
            if runtime is not None:
                runtime.changed.clear()
                wake = asyncio.create_task(runtime.changed.wait())
            done, _ = await asyncio.wait([*active, *([wake] if wake else [])], return_when=asyncio.FIRST_COMPLETED)
            if wake is not None:
                done.discard(wake)
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
            for task in done:
                results[active.pop(task)] = task.result()
            fill()
    finally:
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
    return [results[index] for index in sorted(results)]


class TaskBatcher:
    """Coalesce ready compatible keys; completed keys advance independently."""
    def __init__(self, execute, batch_size):
        self.execute = execute
        self.batch_size = max(1, int(batch_size))
        self.pending = {}
        self.flushing = False
        self.jobs = set()

    async def submit(self, key, ticket):
        future = asyncio.get_running_loop().create_future()
        self.pending.setdefault(key, []).append((ticket, future))
        if not self.flushing:
            self.flushing = True
            asyncio.get_running_loop().call_soon(self._flush)
        return await future

    def _flush(self):
        pending, self.pending = self.pending, {}
        self.flushing = False
        for rows in pending.values():
            for start in range(0, len(rows), self.batch_size):
                task = asyncio.create_task(self._execute(rows[start:start + self.batch_size]))
                self.jobs.add(task)
                task.add_done_callback(self.jobs.discard)

    async def _execute(self, rows):
        try:
            results = await self.execute([ticket for ticket, _ in rows])
            values = {id(ticket): value for ticket, value in results}
            if set(values) != {id(ticket) for ticket, _ in rows}:
                raise RuntimeError('completion batch did not return every ticket')
        except Exception as exc:
            for _, future in rows:
                if not future.done():
                    future.set_exception(exc)
        else:
            for ticket, future in rows:
                if not future.done():
                    future.set_result(values[id(ticket)])

    async def close(self):
        if self.jobs:
            await asyncio.gather(*self.jobs, return_exceptions=True)
