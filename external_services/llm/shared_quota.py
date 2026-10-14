"""Redis WATCH/MULTI quota admission shared by Backend and Agent processes.

Only numeric accounting and hashes are stored, never prompts or credentials.
Redis server time is authoritative; local monotonic time bounds caller waits.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import threading
import time

from .quota_state import QuotaState, Reservation
from .quota import QuotaLimitError, rate_limit_signal


class SharedQuotaUnavailable(RuntimeError):
    error_type = 'quota_backend_unavailable'


class RedisQuotaPool:
    def __init__(self, group, limits, *, redis_client=None):
        if redis_client is None:
            import redis
            redis_client = redis.Redis.from_url(limits['quota_redis_url'], decode_responses=True,
                                               socket_timeout=2, socket_connect_timeout=2)
        self.redis = redis_client
        self.key = 'qasystem:llm-quota:v1:' + hashlib.sha256(group.encode()).hexdigest()
        self.limits = dict(limits)

    def _transaction(self, operation):
        import redis
        try:
            for _ in range(128):
                with self.redis.pipeline() as pipe:
                    try:
                        pipe.watch(self.key)
                        raw = pipe.get(self.key)
                        seconds, microseconds = pipe.time()
                        now = seconds + microseconds / 1_000_000
                        state = QuotaState.restore(json.loads(raw)) if raw else QuotaState(self.limits)
                        state.tighten_state(self.limits)
                        state.prune(now)
                        result = operation(state, now)
                        lease_end = max([now, *(r.lease_until or now for r in state.reservations)])
                        ttl = max(3600, math.ceil(lease_end - now + 120))
                        pipe.multi()
                        pipe.set(self.key, json.dumps(state.data(), allow_nan=False), ex=ttl)
                        pipe.execute()
                        return result
                    except redis.WatchError:
                        continue
        except QuotaLimitError:
            raise
        except (redis.RedisError, OSError, AttributeError, ValueError, KeyError, TypeError):
            raise SharedQuotaUnavailable('Shared LLM quota backend is unavailable or has invalid state') from None
        raise SharedQuotaUnavailable('Shared LLM quota transaction contention exceeded its retry budget')

    def tighten(self, limits):
        # Shared state tightens atomically on every transaction. A newly created
        # worker must never replace active state with its own fresh counters.
        state = QuotaState(self.limits)
        state.tighten_state(limits)
        self.limits.update(state.limits, quota_tiers=[vars(t) for t in state.tiers],
                           adaptive_initial_input_tokens=state.initial_floor)

    def calibration(self, key):
        return self._transaction(lambda state, now: state.calibration(key))

    def snapshot(self):
        return self._transaction(lambda state, now: state.data())

    def _record_operation(self, reservation, operation):
        def apply(state, now):
            record = next((r for r in state.reservations if r.id == reservation.id), None)
            if record is None:
                raise SharedQuotaUnavailable('Shared LLM quota reservation is no longer available')
            operation(state, record, now)
            return record.data()
        reservation.__dict__.update(self._transaction(apply))

    def settle(self, reservation, response):
        self._record_operation(reservation, lambda state, record, now: state.settle_state(record, response, now))

    def report_error(self, error):
        signal = rate_limit_signal(error)
        if signal is not None:
            self._transaction(lambda state, now: state.error_state(signal, now))
        return signal

    def _renew_lease(self, reservation_id, lease_seconds):
        def renew(state, now):
            record = next((r for r in state.reservations if r.id == reservation_id), None)
            if record is None or record.finished is not None:
                raise SharedQuotaUnavailable('Shared LLM quota lease expired before renewal')
            record.lease_until = max(record.lease_until, now + lease_seconds)
        self._transaction(renew)

    @contextmanager
    def request_slot(self, deadline=None, *, input_tokens=0, max_output_tokens=0,
                     lease_seconds=None, measurement=None):
        # Every Redis reservation must have a finite orphan lease. Scheduled
        # callers supply HTTP timeout + grace; direct callers need a deadline.
        if lease_seconds is None:
            if deadline is None:
                raise ValueError('Shared quota requests require a bounded lifetime')
            lease_seconds = max(0, deadline - time.monotonic()) + 60
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError('Shared quota lease must be finite and positive')
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('Extraction queue wait budget exhausted')
            def attempt(state, now):
                record, wait = state.attempt(now, input_tokens, max_output_tokens,
                    lease_seconds=lease_seconds, measurement=measurement)
                return record.data() if record is not None else None, wait
            data, wait = self._transaction(attempt)
            if data is not None:
                reservation = Reservation(**data)
                reservation._callback = self.settle
                break
            remaining = deadline - time.monotonic() if deadline is not None else .1
            time.sleep(max(0, min(.1, wait, remaining)))
        stopped, failed = threading.Event(), threading.Event()
        def renew():
            while not stopped.wait(min(5.0, lease_seconds / 3)):
                try:
                    self._renew_lease(reservation.id, lease_seconds)
                except SharedQuotaUnavailable:
                    failed.set()
                    return
        heartbeat = threading.Thread(target=renew, name='llm-quota-lease', daemon=True)
        heartbeat.start()
        try:
            yield reservation
            if failed.is_set():
                raise SharedQuotaUnavailable('Shared LLM quota lease renewal failed')
        finally:
            stopped.set()
            self._record_operation(reservation, lambda state, record, now: state.finish_state(record, now))
            heartbeat.join(timeout=.1)
