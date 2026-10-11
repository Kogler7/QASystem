"""Local, request-isolated extraction evidence; never persist SDK/auth objects."""
from __future__ import annotations

from datetime import datetime, timezone
from functools import wraps
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

logger = logging.getLogger(__name__)
_PRIVATE = re.compile(r"authorization|api.?key|credential|password|secret|access.?token|refresh.?token|headers", re.I)


class ExtractionTrace:
    def __init__(self, root, *, config=None, secrets=()):
        self.trace_id = uuid4().hex
        self.directory = Path(root) / datetime.now(timezone.utc).strftime('%Y%m%d') / self.trace_id
        self.secrets = tuple(s for s in secrets if s)
        self.lock = threading.Lock()
        self.local = threading.local()
        self.counter = 0
        self.write('manifest', {'trace_id': self.trace_id, 'started_at': datetime.now(timezone.utc).isoformat(),
                                'config': {k: v for k, v in (config or {}).items() if k in {
                                    'version', 'provider', 'model', 'max_input_tokens', 'max_output_tokens',
                                    'document_timeout', 'request_timeout'}}})

    def clean(self, value):
        if isinstance(value, dict):
            return {str(k): '[REDACTED]' if _PRIVATE.search(str(k)) else self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, '[REDACTED]')
            return re.sub(r'(?i)Bearer\s+[^\s"\']+', 'Bearer [REDACTED]', value)
        if value is None or isinstance(value, (int, float, bool)):
            return value
        # Do not serialize arbitrary clients, requests or exception objects.
        return {'object_type': type(value).__name__}

    def write(self, name, payload):
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.directory / (name + '.json')
            temporary = path.with_suffix('.' + uuid4().hex + '.tmp')
            with temporary.open('x', encoding='utf-8') as handle:
                os.chmod(temporary, 0o600)
                json.dump(self.clean(payload), handle, ensure_ascii=False, indent=2)
            temporary.replace(path)
        except OSError:
            # Observability must not turn a usable extraction into a failed one.
            logger.warning('Extraction trace write failed: trace_id=%s artifact=%s', self.trace_id, name)

    def event(self, kind, payload):
        with self.lock:
            self.counter += 1
            name = f'{self.counter:06d}-{kind}'
        self.write(name, {'recorded_at': datetime.now(timezone.utc).isoformat(), **payload})
        return name


def traced_generation(function):
    """Record before dispatch, so a crash/timeout leaves its pending input visible."""
    @wraps(function)
    def wrapped(llm, schema, messages, **context):
        trace = getattr(llm, 'extraction_trace', None)
        if not isinstance(trace, ExtractionTrace):
            return function(llm, schema, messages, **context)
        name = trace.event('call', {'context': context, 'messages': messages, 'schema': schema, 'status': 'pending'})
        trace.local.call_id = name
        logger.info('Extraction call: trace_id=%s call_id=%s pass=%s chunk=%s attempt=%s',
                    trace.trace_id, name, context.get('pass_name'), context.get('chunk_id'), context.get('attempt_number'))
        started = time.monotonic()
        try:
            response = function(llm, schema, messages, **context)
        except Exception as exc:
            trace.write(name + '-response', {'status': 'failed', 'exception_type': type(exc).__name__,
                        'error_type': getattr(exc, 'error_type', None), 'elapsed_seconds': time.monotonic() - started})
            raise
        text = response if isinstance(response, str) else getattr(response, 'text', '')
        try:
            decoded = json.loads(text)
        except (ValueError, TypeError):
            decoded = None
        trace.write(name + '-response', {'status': 'returned', 'text': text, 'decoded_json': decoded,
                    'elapsed_seconds': time.monotonic() - started,
                    **{key: getattr(response, key, None) for key in ('finish_reason', 'usage', 'status', 'incomplete_details')}})
        return response
    return wrapped


def trace_event(llm, kind, **payload):
    trace = getattr(llm, 'extraction_trace', None)
    if isinstance(trace, ExtractionTrace):
        trace.event(kind, {'call_id': getattr(trace.local, 'call_id', None), **payload})
