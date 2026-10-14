"""Bounded LLM requests with shared token, rate and concurrency accounting."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

from algorithm.token_budget import InputTokenLimitError, check_input_bytes
from .quota import rate_limit_signal
from .quota_state import QuotaState, Reservation
from .settings import scheduler_settings
from .tokenization import TokenCounter
from .stream_prefix import capture_stream


class QuotaPool(QuotaState):
    def __init__(self, limits):
        super().__init__(limits)
        self.condition = threading.Condition()

    def tighten(self, limits):
        with self.condition:
            self.tighten_state(limits)
            self.condition.notify_all()

    def calibration(self, key):
        with self.condition:
            return super().calibration(key)

    def settle(self, reservation, response):
        with self.condition:
            self.settle_state(reservation, response, time.monotonic())
            self.condition.notify_all()

    def report_error(self, error):
        signal = rate_limit_signal(error)
        if signal is not None:
            with self.condition:
                self.error_state(signal, time.monotonic())
                self.condition.notify_all()
        return signal

    @contextmanager
    def request_slot(self, deadline=None, *, input_tokens=0, max_output_tokens=0,
                     lease_seconds=None, measurement=None):
        with self.condition:
            while True:
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    raise TimeoutError('Extraction queue wait budget exhausted')
                reservation, wait = self.attempt(now, input_tokens, max_output_tokens,
                    # A live local context owns its slot until finally. Only
                    # shared workers need expiring, renewable orphan leases.
                    lease_seconds=None, measurement=measurement)
                if reservation is not None:
                    reservation._callback = self.settle
                    break
                wait = None if wait == float('inf') else wait
                if deadline is not None:
                    wait = min(wait, deadline - now) if wait is not None else deadline - now
                self.condition.wait(timeout=wait)
        try:
            yield reservation
        finally:
            with self.condition:
                self.finish_state(reservation, time.monotonic())
                self.condition.notify_all()


_lock = threading.Lock()
_pools: dict[str, Any] = {}


def get_pool(group: str, limits: dict[str, Any]):
    from .shared_quota import RedisQuotaPool
    backend = limits.get('quota_backend', 'local')
    with _lock:
        if group not in _pools:
            _pools[group] = RedisQuotaPool(group, limits) if backend == 'redis' else QuotaPool(limits)
        else:
            if isinstance(_pools[group], RedisQuotaPool) != (backend == 'redis'):
                raise ValueError('One quota group cannot mix local and shared backends')
            if backend == 'redis' and _pools[group].limits['quota_redis_url'] != limits['quota_redis_url']:
                raise ValueError('One quota group cannot use different shared stores')
            _pools[group].tighten(limits)
        return _pools[group]


class ScheduledClient:
    """Wrap SDK endpoints, so fallback HTTP attempts also acquire quota."""
    def __init__(self, client: Any, config: dict[str, Any]):
        config = scheduler_settings(config)
        self.service_snapshot = config.get('service_snapshot')
        self.client = client.with_options(max_retries=0) if self.service_snapshot and hasattr(client, 'with_options') else client
        connection = (self.service_snapshot or {}).get('connection') or {}
        verify = connection.get('ca_bundle') or connection.get('verify_ssl', True)
        if verify is not True and hasattr(self.client, 'with_options'):
            import httpx
            self.client = self.client.with_options(http_client=httpx.Client(verify=verify, trust_env=False))
        if self.service_snapshot:
            from .distributed import service_pool
            self.pool = service_pool(self.service_snapshot)
        else:
            self.pool = get_pool(config['quota_group'], config)
        self.timeout = min(float(config['request_timeout']), float(connection.get('timeout_seconds', config['request_timeout'])))
        self.thinking_parameter = config.get("thinking_parameter", "thinking")
        self.disable_thinking = config["disable_thinking"]
        self.response_format = config.get("response_format", "auto")
        self.max_input_tokens = int(config.get("max_input_tokens", 65536))
        self.max_input_bytes = config['max_input_bytes']
        self.token_counter = TokenCounter(config['tokenizer_dir'], config['tokenizer_revision'], config.get('model', ''))
        self._call_local = threading.local()
        self.max_output_tokens = int(config.get("max_output_tokens") or config.get("max_tokens") or 0)
        budget = config.get("document_timeout")
        self.deadline = time.monotonic() + float(budget) if budget is not None else None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._chat))
        self.responses = SimpleNamespace(create=self._responses)

    def __getattr__(self, name):
        return getattr(self.client, name)

    @property
    def call_deadline(self):
        return getattr(self._call_local, 'deadline', None)

    def measure_input(self, payload):
        return self.token_counter.measure(payload)

    def _call(self, method, **kwargs):
        from algorithm.extraction_trace import ExtractionTrace
        trace = self.__dict__.get("extraction_trace")
        started = time.monotonic()
        acquired = None
        error_type = None
        input_tokens = output_tokens = None
        reservation = None
        measurement = None
        calibration = None
        rate_signal = None
        consumer = kwargs.pop('_response_consumer', None)
        deadline_prefix = False
        try:
            if kwargs.get('stream') and consumer is None:
                raise ValueError('Scheduled streaming requires a response consumer')
            if self.max_input_bytes is not None: check_input_bytes(kwargs, self.max_input_bytes)
            measured = self.measure_input(kwargs)
            measurement = measured.metadata()
            key = hashlib.sha256(json.dumps({
                'model': measured.model, 'revision': measured.revision,
                'fingerprint': measured.fingerprint,
                'schema': (kwargs.get('response_format') or {}).get('type'),
                'tools': bool(kwargs.get('tools')), 'extra': kwargs.get('extra_body') or {},
            }, sort_keys=True, default=str).encode()).hexdigest()
            measurement['calibration_key'] = key
            calibration = self.pool.calibration(key)
            input_tokens = measured.tokens + calibration['max_underestimate']
            if input_tokens > self.max_input_tokens:
                raise InputTokenLimitError(f'Input tokens {input_tokens} exceed max_input_tokens {self.max_input_tokens} ({measured.source})')
            output_tokens = int(kwargs.get("max_output_tokens") or kwargs.get("max_completion_tokens")
                                or kwargs.get("max_tokens") or self.max_output_tokens)
            from .service import check_service_budget
            check_service_budget(kwargs, self.service_snapshot, measured_tokens=input_tokens)
            # Rate waiting belongs to the document budget. Give an admitted
            # HTTP request its full timeout, bounded by the remaining document.
            queue_deadline = self.deadline if self.deadline is not None else started + self.timeout
            with self.pool.request_slot(queue_deadline, input_tokens=input_tokens,
                                        max_output_tokens=output_tokens, measurement=measurement,
                                        lease_seconds=self.timeout + 60) as reservation:
                acquired = time.monotonic()
                call_deadline = acquired + self.timeout
                if self.deadline is not None:
                    call_deadline = min(call_deadline, self.deadline)
                self._call_local.deadline = call_deadline
                remaining = call_deadline - acquired
                if remaining <= 0:
                    raise TimeoutError("Extraction request budget exhausted")
                kwargs["timeout"] = remaining
                request_record = None
                if isinstance(trace, ExtractionTrace):
                    request_record = trace.event("http-request", {
                        "call_id": getattr(trace.local, "call_id", None),
                        "request": {key: value for key, value in kwargs.items() if key in {
                            "model", "messages", "input", "instructions", "response_format",
                            "text", "temperature", "max_tokens", "max_output_tokens", "extra_body",
                            "stream", "stream_options"}},
                    })
                try:
                    response = method(**kwargs)
                    if consumer is not None:
                        capture = (capture_stream(response, deadline=call_deadline, clock=time.monotonic)
                                   if kwargs.get('stream') else None)
                        response = consumer(capture if capture is not None else response)
                        prefix = capture.redeem(response) if capture is not None else None
                        if prefix is not None:
                            response, deadline_prefix = prefix, True
                    reservation.settle(response)
                except Exception as exc:
                    if request_record:
                        trace.write(request_record + "-response", {"status": "failed", "exception_type": type(exc).__name__})
                    raise
                if request_record:
                    try:
                        body = response if isinstance(response, dict) else response.model_dump(mode="json")
                        trace.write(request_record + "-response", {"status": "returned", "body": body})
                    except Exception:
                        # Trace serialization must never change the generation result.
                        trace.write(request_record + "-response", {"status": "unserializable"})
                finished_at = time.monotonic()
                if ((self.deadline is not None and finished_at >= self.deadline)
                        or (finished_at >= call_deadline and not deadline_prefix)):
                    raise TimeoutError("Extraction response arrived after deadline")
                return response
        except Exception as exc:
            error_type = getattr(exc, "error_type", None) or type(exc).__name__
            if acquired is not None:
                rate_signal = self.pool.report_error(exc)
            raise
        finally:
            self._call_local.deadline = None
            if isinstance(trace, ExtractionTrace):
                finished = time.monotonic()
                trace.event("http", {"call_id": getattr(trace.local, "call_id", None),
                            "error_type": error_type, "elapsed_seconds": finished - started,
                            "queue_seconds": (acquired - started) if acquired is not None else None,
                            "http_seconds": (finished - acquired) if acquired is not None else None,
                            "estimated_input_tokens": input_tokens,
                            "token_measurement": measurement,
                            "token_calibration": calibration,
                            "rate_limit_kind": rate_signal.kind if rate_signal is not None else None,
                            "reserved_output_tokens": output_tokens,
                            "usage_known": reservation.usage_known if reservation is not None else False,
                            "accounted_input_tokens": reservation.input_tokens if reservation is not None else None,
                            "accounted_tokens": reservation.total_tokens if reservation is not None else None})

    def _chat(self, **kwargs):
        if self.response_format == "json_object" and (kwargs.get("response_format") or {}).get("type") == "json_schema":
            kwargs["response_format"] = {"type": "json_object"}
        extra = dict(kwargs.get("extra_body") or {})
        # Translate the caller's per-request decision. A global disable flag
        # must not overwrite semantic passes, or reinsert a rejected parameter
        # on a capability fallback that intentionally omits it.
        if "thinking" in extra and self.thinking_parameter != "thinking":
            thinking = extra.pop("thinking")
            if self.thinking_parameter == "enable_thinking":
                extra["enable_thinking"] = thinking["type"] == "enabled"
        if self.thinking_parameter == "none":
            extra.pop("enable_thinking", None)
        if extra:
            kwargs["extra_body"] = extra
        else:
            kwargs.pop("extra_body", None)
        return self._call(self.client.chat.completions.create, **kwargs)

    def _responses(self, **kwargs):
        return self._call(self.client.responses.create, **kwargs)
