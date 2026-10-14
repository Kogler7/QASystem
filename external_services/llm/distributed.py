"""Redis FIFO admission for model HTTP calls; no nested execution jobs.

Only opaque tickets and hashed service/account IDs enter Redis. A caller owns
one lease for the entire HTTP response, including streamed responses.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import threading
import time
import uuid


ADMIT = r'''
local clock=redis.call('TIME')
local now=clock[1]*1000+math.floor(clock[2]/1000)
local ticket=ARGV[1]
local expiry=now+tonumber(ARGV[2])
local seq=tonumber(ARGV[3])
local groups=(#KEYS)/4
for i=1,groups do
 local j=(i-1)*4
 local stale=redis.call('ZRANGEBYSCORE',KEYS[j+2],'-inf',now)
 for _,id in ipairs(stale) do redis.call('ZREM',KEYS[j+1],id) end
 redis.call('ZREMRANGEBYSCORE',KEYS[j+2],'-inf',now)
 redis.call('ZREMRANGEBYSCORE',KEYS[j+3],'-inf',now)
 redis.call('ZREMRANGEBYSCORE',KEYS[j+4],'-inf',now-60000)
 redis.call('ZADD',KEYS[j+1],'NX',seq,ticket)
 redis.call('ZADD',KEYS[j+2],expiry,ticket)
 for k=1,4 do redis.call('PEXPIRE',KEYS[j+k],math.max(tonumber(ARGV[2])*3,120000)) end
end
for i=1,groups do
 local j=(i-1)*4
 local a=4+(i-1)*3
 if redis.call('ZRANGE',KEYS[j+1],0,0)[1]~=ticket then return 0 end
 if redis.call('ZCARD',KEYS[j+3])>=tonumber(ARGV[a]) then return 0 end
 if tonumber(ARGV[a+1])>0 and redis.call('ZCOUNT',KEYS[j+4],'('..(now-1000),'+inf')>=tonumber(ARGV[a+1]) then return 0 end
 if tonumber(ARGV[a+2])>0 and redis.call('ZCARD',KEYS[j+4])>=tonumber(ARGV[a+2]) then return 0 end
end
for i=1,groups do
 local j=(i-1)*4
 redis.call('ZREM',KEYS[j+1],ticket)
 redis.call('ZREM',KEYS[j+2],ticket)
 redis.call('ZADD',KEYS[j+3],expiry,ticket)
 redis.call('ZADD',KEYS[j+4],now,ticket)
end
return 1
'''

RENEW = r'''
local clock=redis.call('TIME')
local now=clock[1]*1000+math.floor(clock[2]/1000)
for _,key in ipairs(KEYS) do
 local score=redis.call('ZSCORE',key,ARGV[1])
 if not score or tonumber(score)<=now then return 0 end
end
for _,key in ipairs(KEYS) do
 redis.call('ZADD',key,now+tonumber(ARGV[2]),ARGV[1])
 redis.call('PEXPIRE',key,math.max(tonumber(ARGV[2])*3,120000))
end
return 1
'''

RELEASE = "for _,key in ipairs(KEYS) do redis.call('ZREM',key,ARGV[1]) end return 1"


class ModelSchedulingError(RuntimeError):
    """Safe failure without Redis URLs, credentials or downstream payloads."""


class RedisAdmission:
    def __init__(self, service, *, redis_client=None, lease_seconds=30, poll_seconds=.05):
        if redis_client is None:
            import redis
            # The deployment's internal Redis also hosts execution jobs. No
            # new environment address or independent provider queue is created.
            redis_client = redis.Redis.from_url('redis://redis:6379/1', socket_timeout=3,
                                                 socket_connect_timeout=3)
        self.redis = redis_client
        self.lease_ms = int(lease_seconds * 1000)
        self.poll_seconds = poll_seconds
        if self.lease_ms < 100:
            raise ValueError('Model lease must be at least 100ms')
        from qasystem_config.api_services import LIMIT_DEFAULTS, validate_limits
        limits = service.get('limits') or LIMIT_DEFAULTS
        validate_limits(limits, required=True)
        scopes = [('service:' + service['service_id'], limits)]
        if service.get('account_id'):
            scopes.append(('account:' + service['account_id'], service['account_limits']))
        self.keys, self.limits = [], []
        for name, limit in scopes:
            prefix = 'qasystem:{model-admission}:' + sha256(name.encode()).hexdigest()
            self.keys.extend(prefix + ':' + suffix for suffix in ('queue', 'wait', 'active', 'starts'))
            self.limits.extend(int(limit.get(key, 0)) for key in ('max_concurrency', 'qps', 'qpm'))

    @contextmanager
    def request_slot(self, deadline=None, cancelled=None):
        ticket = uuid.uuid4().hex
        stop, lost = threading.Event(), threading.Event()
        active_keys = self.keys[2::4]
        cleanup_keys = [key for i, key in enumerate(self.keys) if i % 4 != 3]

        def renew():
            while not stop.wait(self.lease_ms / 3000):
                try:
                    if not self.redis.eval(RENEW, len(active_keys), *active_keys, ticket, self.lease_ms):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    return

        renewal = None
        try:
            sequence = self.redis.incr('qasystem:{model-admission}:sequence')
            while True:
                if cancelled and cancelled():
                    raise ModelSchedulingError('Model request cancelled while waiting')
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('Model queue wait budget exhausted')
                admitted = self.redis.eval(ADMIT, len(self.keys), *self.keys,
                                           ticket, self.lease_ms, sequence, *self.limits)
                if admitted:
                    break
                stop.wait(min(self.poll_seconds, max(0, deadline-time.monotonic()))
                          if deadline is not None else self.poll_seconds)
            renewal = threading.Thread(target=renew, daemon=True, name='model-lease-renewal')
            renewal.start()
            yield
            if lost.is_set():
                raise ModelSchedulingError('Model admission lease was lost; result discarded')
        except (TimeoutError, ModelSchedulingError):
            raise
        except Exception as exc:
            # Preserve downstream exceptions; only mask admission connectivity.
            if renewal is not None:
                raise
            raise ModelSchedulingError('Shared model queue unavailable') from None
        finally:
            stop.set()
            if renewal is not None:
                renewal.join(timeout=3)
            try:
                self.redis.eval(RELEASE, len(cleanup_keys), *cleanup_keys, ticket)
            except Exception:
                # Expiring lease recovers a disconnected/crashed caller.
                pass


def service_pool(service):
    return RedisAdmission(service)
