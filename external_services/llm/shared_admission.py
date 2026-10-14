"""One atomic service/account FIFO for concurrency, rate and token reservations."""
from contextlib import contextmanager
from hashlib import sha256
import json
import threading
import time
import uuid

from .quota import QuotaLimitError, rate_limit_signal
from .quota_state import QuotaState, Reservation


class RedisAdmission:
    def __init__(self, service, *, redis_client=None, lease_seconds=30, poll_seconds=.05):
        if redis_client is None:
            import redis
            redis_client = redis.Redis.from_url('redis://redis:6379/1', socket_timeout=3, socket_connect_timeout=3)
        self.redis = redis_client
        self.lease_seconds, self.poll_seconds = lease_seconds, poll_seconds
        self.lease_ms = int(lease_seconds * 1000)
        if self.lease_ms < 100: raise ValueError('Model lease must be at least 100ms')
        from qasystem_config.api_services import LIMIT_DEFAULTS, validate_limits
        scopes = [('service:' + service['service_id'], service.get('limits') or LIMIT_DEFAULTS)]
        if service.get('account_id'): scopes.append(('account:' + service['account_id'], service['account_limits']))
        self.keys, self.state_keys, self.policies = [], [], []
        for name, limits in scopes:
            validate_limits(limits, required=True)
            prefix = 'qasystem:{model-admission}:' + sha256(name.encode()).hexdigest()
            self.keys.extend(prefix + ':' + suffix for suffix in ('queue', 'wait', 'active', 'starts'))
            self.state_keys.append(prefix + ':quota-state')
            self.policies.append(limits)
        self.revision = int(service.get('revision') or 0)

    def _transaction(self, operation):
        import redis
        from .distributed import ModelSchedulingError
        try:
            for _ in range(128):
                with self.redis.pipeline() as pipe:
                    try:
                        pipe.watch(*self.keys, *self.state_keys)
                        seconds, micros = pipe.time()
                        now = seconds + micros / 1_000_000
                        states = []
                        for key, policy in zip(self.state_keys, self.policies):
                            raw = pipe.get(key)
                            state = QuotaState.restore(json.loads(raw)) if raw else QuotaState(policy)
                            state.replace_policy(policy, self.revision)
                            state.prune(now)
                            states.append(state)
                        result, commands = operation(pipe, states, now)
                        pipe.multi()
                        for key, state in zip(self.state_keys, states):
                            pipe.set(key, json.dumps(state.data(), allow_nan=False), ex=3600)
                        for command, args, kwargs in commands: getattr(pipe, command)(*args, **kwargs)
                        pipe.execute()
                        return result
                    except redis.WatchError: continue
        except (QuotaLimitError, ModelSchedulingError): raise
        except (redis.RedisError, ValueError, KeyError, TypeError):
            raise ModelSchedulingError('Shared model queue unavailable or state invalid') from None
        raise ModelSchedulingError('Shared model queue transaction contention exceeded retry budget')

    def _attempt(self, ticket, sequence, input_tokens, output_tokens, measurement):
        def attempt(pipe, states, now):
            ms, expiry = now * 1000, (now + self.lease_seconds) * 1000
            commands, candidates, ready = [], [], True
            for index, state in enumerate(states):
                queue, wait, active, starts = self.keys[index * 4:index * 4 + 4]
                stale = set(pipe.zrangebyscore(wait, '-inf', ms))
                queued = list(pipe.zrange(queue, 0, -1))
                current = ticket.encode() if queued and isinstance(queued[0], bytes) else ticket
                queued = [item for item in queued if item not in stale]
                if current not in queued:
                    # Sequence establishes FIFO across different service IDs sharing one account.
                    ready = False
                elif queued[0] != current: ready = False
                concurrent = pipe.zcount(active, '(' + str(ms), '+inf')
                cap = max(1, int(state.limits['max_concurrency'] * state.scales['concurrency']))
                if concurrent >= cap: ready = False
                for key, period in (('qps', 1000), ('qpm', 60000)):
                    limit = state.limits[key]
                    if limit and pipe.zcount(starts, '(' + str(ms - period), '+inf') >= limit: ready = False
                candidate = QuotaState.restore(state.data())
                record, _ = candidate.attempt(now, input_tokens, output_tokens,
                    lease_seconds=self.lease_seconds, measurement=measurement)
                if record is None: ready = False
                else: record.id = ticket
                candidates.append((candidate, record))
                if stale: commands.append(('zrem', (queue, *stale), {}))
                for key, cutoff in ((wait, ms), (active, ms), (starts, ms - 60000)):
                    commands.append(('zremrangebyscore', (key, '-inf', cutoff), {}))
                commands += [('zadd', (queue, {ticket: sequence}), {'nx': True}),
                             ('zadd', (wait, {ticket: expiry}), {})]
                for key in (queue, wait, active, starts): commands.append(('pexpire', (key, max(self.lease_ms * 3, 120000)), {}))
            if ready:
                for index, (candidate, record) in enumerate(candidates):
                    states[index] = candidate
                    queue, wait, active, starts = self.keys[index * 4:index * 4 + 4]
                    commands += [('zrem', (queue, ticket), {}), ('zrem', (wait, ticket), {}),
                                 ('zadd', (active, {ticket: expiry}), {}), ('zadd', (starts, {ticket: ms}), {})]
                return candidates[0][1].data(), commands
            return None, commands
        return self._transaction(attempt)

    def _record(self, ticket, action, response=None):
        from .distributed import ModelSchedulingError
        def operate(pipe, states, now):
            commands, primary = [], None
            for index, state in enumerate(states):
                record = next((item for item in state.reservations if item.id == ticket), None)
                if record is None or record.finished is not None:
                    if action == 'finish': continue
                    raise ModelSchedulingError('Model reservation lost; result discarded')
                if action == 'settle': state.settle_state(record, response, now)
                elif action == 'renew':
                    record.lease_until = now + self.lease_seconds
                    commands.append(('zadd', (self.keys[index * 4 + 2], {ticket: record.lease_until * 1000}), {}))
                    commands.append(('pexpire', (self.keys[index * 4 + 2], max(self.lease_ms * 3, 120000)), {}))
                else: state.finish_state(record, now)
                if primary is None: primary = record.data()
            if action == 'finish':
                for index, key in enumerate(self.keys):
                    if index % 4 != 3: commands.append(('zrem', (key, ticket), {}))
            return primary, commands
        return self._transaction(operate)

    def calibration(self, key):
        return self._transaction(lambda pipe, states, now: (states[0].calibration(key), []))

    def report_error(self, error):
        signal = rate_limit_signal(error)
        if signal:
            def report(pipe, states, now):
                for state in states: state.error_state(signal, now)
                return None, []
            self._transaction(report)
        return signal

    @contextmanager
    def request_slot(self, deadline=None, cancelled=None, *, input_tokens=0, max_output_tokens=0,
                     measurement=None, lease_seconds=None):
        from .distributed import ModelSchedulingError
        ticket = uuid.uuid4().hex
        stopped, lost = threading.Event(), threading.Event()
        heartbeat, reservation = None, None
        # Prompts, URLs and credentials never enter Redis; keep only calibration inputs.
        measurement = {key: value for key, value in (measurement or {}).items()
                       if key in {'tokens', 'source', 'calibration_key'}}
        try:
            sequence = self.redis.incr('qasystem:{model-admission}:sequence')
            while True:
                if cancelled and cancelled(): raise ModelSchedulingError('Model request cancelled while waiting')
                if deadline is not None and time.monotonic() >= deadline: raise TimeoutError('Model queue wait budget exhausted')
                data = self._attempt(ticket, sequence, input_tokens, max_output_tokens, measurement)
                if data is not None: break
                stopped.wait(self.poll_seconds)
            reservation = Reservation(**data)
            def settle(record, response):
                record.__dict__.update(self._record(ticket, 'settle', response))
            reservation._callback = settle
            def renew():
                while not stopped.wait(min(5, self.lease_seconds / 3)):
                    try: self._record(ticket, 'renew')
                    except Exception:
                        lost.set()
                        return
            heartbeat = threading.Thread(target=renew, name='model-reservation-renew', daemon=True)
            heartbeat.start()
            yield reservation
            if lost.is_set(): raise ModelSchedulingError('Model admission lease was lost; result discarded')
        except (TimeoutError, ModelSchedulingError, QuotaLimitError): raise
        except Exception:
            if reservation is not None: raise
            raise ModelSchedulingError('Shared model queue unavailable') from None
        finally:
            stopped.set()
            if heartbeat is not None: heartbeat.join(timeout=3)
            try: self._record(ticket, 'finish')
            except Exception: pass  # Renewable finite leases recover crashed/disconnected callers.
