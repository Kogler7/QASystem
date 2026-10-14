"""Bounded workers with deterministic result and diagnostic merge order."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from itertools import islice


def ordered_map(function, items, *, max_workers=1, prefix="ke-review"):
    workers = max(1, int(max_workers))
    if workers == 1:
        yield from map(function, items)
        return
    iterator = iter(items)
    exhausted = object()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=prefix) as executor:
        pending = {executor.submit(function, item): index for index, item in enumerate(islice(iterator, workers))}
        submitted = len(pending)
        next_result = 0
        completed = {}
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                completed[pending.pop(future)] = future.result()
            # Bounded result buffering permits refill without waiting for the
            # earliest result. Backpressure still bounds memory for huge inputs.
            while len(pending) < workers and len(pending) + len(completed) < workers * 2:
                item = next(iterator, exhausted)
                if item is exhausted:
                    break
                pending[executor.submit(function, item)] = submitted
                submitted += 1
            while next_result in completed:
                yield completed.pop(next_result)
                next_result += 1
            while len(pending) < workers and len(pending) + len(completed) < workers * 2:
                item = next(iterator, exhausted)
                if item is exhausted:
                    break
                pending[executor.submit(function, item)] = submitted
                submitted += 1
        while next_result in completed:
            yield completed.pop(next_result)
            next_result += 1
