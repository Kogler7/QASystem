"""Per-call receipts for prefixes actually read before a client deadline.

These objects never come from provider JSON. A consumer cannot authorize a late
response by filling in status fields: only its current stream capture can issue
and redeem an opaque receipt, using the captured text and usage snapshot.
"""
from copy import deepcopy


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


class _StreamDeadlineReached(TimeoutError):
    pass


class _PrefixReceipt:
    __slots__ = ()


class _StreamCapture:
    def __init__(self, stream, deadline, clock):
        self._stream = stream
        self._iterator = iter(stream)
        self._deadline = deadline
        self._clock = clock
        self._content = []
        self._reasoning = []
        self._usage = self._response_id = None
        self._deadline_reached = self._finished = False
        self._receipt = self._snapshot = None

    def __iter__(self):
        return self

    def _check_deadline(self):
        if self._clock() >= self._deadline:
            self._deadline_reached = True
            raise _StreamDeadlineReached('Client stream deadline reached')

    def __next__(self):
        self._check_deadline()
        chunk = next(self._iterator)
        # A blocking next() may return after the deadline. That chunk must not
        # enter either the consumer or the independently captured prefix.
        self._check_deadline()
        response_id = _field(chunk, 'id')
        if isinstance(response_id, str):
            self._response_id = response_id
        usage = _field(chunk, 'usage')
        if usage is not None:
            dumped = usage if isinstance(usage, dict) else usage.model_dump()
            self._usage = deepcopy(dumped) if isinstance(dumped, dict) else None
        choices = _field(chunk, 'choices', []) or []
        if choices:
            choice = choices[0]
            self._finished = self._finished or _field(choice, 'finish_reason') is not None
            delta = _field(choice, 'delta', {})
            text = _field(delta, 'content', '') or ''
            thought = _field(delta, 'reasoning_content', '') or ''
            if isinstance(text, str) and isinstance(thought, str):
                self._content.append(text)
                self._reasoning.append(thought)
        return chunk

    def close(self):
        close = getattr(self._stream, 'close', None)
        if callable(close):
            close()

    def _seal(self, body):
        text, thought = ''.join(self._content), ''.join(self._reasoning)
        message = (((body.get('choices') or [{}])[0]).get('message') or {})
        if (not self._deadline_reached or self._finished or not text.strip()
                or message.get('content') != text or message.get('reasoning_content') != thought
                or body.get('status') != 'incomplete'
                or (body.get('incomplete_details') or {}).get('reason') != 'stream_deadline'):
            return body
        records = (body.get('streaming') or {}).get('complete_records', 0)
        records = records if type(records) is int and records >= 0 else 0
        # Rebuild the returned body from captured data. In particular, a
        # consumer-supplied usage or finish marker cannot replace the snapshot.
        self._snapshot = {
            'id': self._response_id,
            'choices': [{'message': {'content': text, 'reasoning_content': thought},
                         'finish_reason': 'cancelled'}],
            'usage': deepcopy(self._usage), 'status': 'incomplete',
            'incomplete_details': {'reason': 'stream_deadline'},
            'streaming': {'received_characters': len(text) + len(thought),
                          'complete_records': records, 'stop_reason': 'stream_deadline',
                          'usage_unavailable': self._usage is None},
        }
        self._receipt = _PrefixReceipt()
        return self._receipt

    def redeem(self, response):
        if type(response) is _PrefixReceipt and response is self._receipt:
            body = self._snapshot
            self._receipt = self._snapshot = None
            return body
        return None


def capture_stream(response, *, deadline, clock):
    # Compatible providers may ignore stream=true and return a full body. Such
    # bodies remain ordinary responses and never acquire a prefix capability.
    if isinstance(response, dict) or _field(response, 'choices') is not None:
        return None
    return _StreamCapture(response, deadline, clock)


def is_captured_stream(stream):
    return type(stream) is _StreamCapture


def preserve_deadline_prefix(stream, body):
    return stream._seal(body) if is_captured_stream(stream) else body
