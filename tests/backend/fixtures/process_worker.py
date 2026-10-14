"""Test-only synthetic operation running through the real Redis worker lifecycle."""
import argparse
from pathlib import Path
import signal
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--source', required=True)
parser.add_argument('--redis-url', required=True)
parser.add_argument('--prefix', required=True)
parser.add_argument('--artifacts', required=True)
parser.add_argument('--ready', required=True)
args = parser.parse_args()
sys.path.insert(0, args.source)
from docker import runtime_config
# This isolated lifecycle test has no deployment configuration. Production guard
# behavior is covered separately; locate test defaults outside image scaffolding.
runtime_config.runtime_path = lambda *a, **kw: Path(args.artifacts) / 'test-empty-registry' / 'config.json'
from backend.app.execution import worker as worker_module
from backend.app.execution.artifacts import ArtifactStore
from backend.app.execution.hub import ExecutionEventHub
from backend.app.execution.manager import ExecutionManager
from backend.app.execution.store import RedisExecutionStore

store = RedisExecutionStore(args.redis_url, prefix=args.prefix)
manager = ExecutionManager(store, ArtifactStore(args.artifacts), ExecutionEventHub(store))
# Dependency injection only; all consumption, lifecycle, attempt, ACK and heartbeats are real.
worker_module.ExecutionManager = lambda: manager
runtime = worker_module.WorkerRuntime('cpu.chunk', 'cpu-0')

def operation(_operation, payload):
    marker = payload['marker']
    store.redis.set(store.prefix + ':started:' + marker, '1', ex=30)
    deadline = time.monotonic() + 10
    while not store.redis.get(store.prefix + ':release:' + marker):
        if time.monotonic() > deadline:
            raise TimeoutError('synthetic test release missing')
        time.sleep(.02)
    return {'completed': marker}

worker_module.run_operation = operation
signal.signal(signal.SIGTERM, runtime.stop)
signal.signal(signal.SIGINT, runtime.stop)
Path(args.ready).write_text(runtime.consumer_id, encoding='utf-8')
runtime.run()
