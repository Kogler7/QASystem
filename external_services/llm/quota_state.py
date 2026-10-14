"""Serializable scheduling state used identically by local and Redis pools."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
import math
from typing import Any, Callable
from uuid import uuid4

from .quota import QuotaLimitError, parse_quota_tiers, response_usage, select_tier, tighten_tiers


@dataclass(eq=False)
class Reservation:
    started: float
    input_tokens: int
    total_tokens: int
    finished: float | None = None
    id: str = field(default_factory=lambda: uuid4().hex)
    settled: bool = False
    usage_known: bool = False
    tier_floor: int = 0
    lease_until: float | None = None
    measurement: dict | None = None
    _callback: Callable | None = field(default=None, repr=False)

    def settle(self, response: Any) -> None:
        if self._callback is not None:
            self._callback(self, response)
        else:
            self.apply_usage(response)

    def apply_usage(self, response: Any) -> bool:
        if self.settled:
            return False
        self.settled = True
        usage = response_usage(response)
        if usage is None:
            return False
        prompt, total = usage
        # An all-zero/missing/partial report cannot cancel a real request.
        # A zero prompt for a known nonempty request is not credible either.
        if total <= 0 or (self.input_tokens > 0 and prompt == 0):
            return False
        if prompt is None and total < self.input_tokens:
            return False
        self.total_tokens = total
        if prompt is not None:
            self.input_tokens = prompt
        self.usage_known = True
        return True

    def data(self):
        return {key: value for key, value in vars(self).items() if key != '_callback'}

    def token_expiry(self):
        """Known completed usage follows admission time; live work never ages out."""
        if self.finished is not None:
            return (self.started if self.usage_known else self.finished) + 60
        if self.lease_until is not None:
            # This is a predicted orphan expiry only. A live worker renews
            # its lease, so admission must check the state again after waiting.
            return max(self.lease_until, self.started + 60) if self.usage_known else self.lease_until + 60
        return float('inf')


class QuotaState:
    # These are recovery policy invariants, not duplicated deployment defaults.
    recovery_samples = 8
    recovery_window = 60.0

    def __init__(self, limits: dict):
        self.active = 0
        self.starts: deque[float] = deque()
        self.limits = {key: int(limits[key]) for key in ('max_concurrency', 'qps', 'qpm')}
        self.tiers = parse_quota_tiers(limits.get('quota_tiers', []))
        self.reservations: list[Reservation] = []
        self.next_start = 0.0
        self.initial_floor = int(limits.get('adaptive_initial_input_tokens', 0))
        self.tier_floor = self.initial_floor
        self.scales = dict(rpm=1.0, tpm=1.0, concurrency=1.0)
        self.cooldown_until = 0.0
        self.stable_since: float | None = None
        self.small_successes = 0
        self.recovery_successes = 0
        self.recovery_since: float | None = None
        self.calibrations: dict[str, dict] = {}

    def tighten_state(self, limits: dict):
        for key, old in self.limits.items():
            new = int(limits[key])
            self.limits[key] = min(old, new) if old and new else old or new
        self.tiers = tighten_tiers(self.tiers, parse_quota_tiers(limits.get('quota_tiers', [])))
        initial = int(limits.get('adaptive_initial_input_tokens', 0))
        if initial > self.initial_floor:
            self.tier_floor = max(self.tier_floor, initial)
            self.initial_floor = initial
        if self.reservations and self.tiers:
            last = self.reservations[-1]
            self.next_start = max(self.next_start, last.started + 60 / self._tier(last).rpm / self.scales['rpm'])

    def _tier(self, record):
        return select_tier(self.tiers, max(record.input_tokens, record.tier_floor))

    def prune(self, now):
        while self.starts and now - self.starts[0] >= 60:
            self.starts.popleft()
        for record in self.reservations:
            if record.finished is None and record.lease_until is not None and now >= record.lease_until:
                # A vanished worker's unknown usage may have consumed its
                # entire reservation; retain that charge for another minute.
                record.finished = record.lease_until
        self.reservations[:] = [r for r in self.reservations
                                if r.finished is None or now < r.token_expiry()]
        self.active = sum(r.finished is None for r in self.reservations)

    def _quota_wait(self, now, input_tokens, total_tokens):
        tier = select_tier(self.tiers, max(input_tokens, self.tier_floor))
        if tier is None:
            return max(0.0, self.cooldown_until - now)
        if total_tokens > tier.tpm:
            raise QuotaLimitError('One extraction request exceeds the minute token quota')
        rpm_cost, tpm_cost = 1 / tier.rpm, total_tokens / tier.tpm
        # A request larger than the reduced TPM cap can make progress once the
        # account is otherwise empty. It still must fit the hard provider cap.
        tpm_cap = max(self.scales['tpm'], tpm_cost)
        expiries = []
        for record in self.reservations:
            previous = self._tier(record)
            if now - record.started < 60:
                cost = 1 / previous.rpm
                rpm_cost += cost
                expiries.append((record.started + 60, cost, 0.0))
            cost = record.total_tokens / previous.tpm
            tpm_cost += cost
            if record.token_expiry() != float('inf'):
                expiries.append((record.token_expiry(), 0.0, cost))
        rpm_cap = max(self.scales['rpm'], 1 / tier.rpm)
        pacing = max(self.next_start, self.cooldown_until)
        if rpm_cost <= rpm_cap + 1e-12 and tpm_cost <= tpm_cap + 1e-12:
            return max(0.0, pacing - now)
        for expiry, rpm, tpm in sorted(expiries):
            rpm_cost -= rpm
            tpm_cost -= tpm
            if rpm_cost <= rpm_cap + 1e-12 and tpm_cost <= tpm_cap + 1e-12:
                return max(expiry - now, pacing - now, 0.0)
        return float('inf')

    def attempt(self, now, input_tokens, max_output_tokens, *, lease_seconds=None, measurement=None):
        self.prune(now)
        waits = []
        for key, period in (('qps', 1), ('qpm', 60)):
            cap = self.limits[key]
            recent = [t for t in self.starts if now - t < period]
            if cap and len(recent) >= cap:
                waits.append(recent[-cap] + period - now)
        total_tokens = max(0, input_tokens) + max(0, max_output_tokens)
        quota_wait = self._quota_wait(now, input_tokens, total_tokens)
        if quota_wait > 0:
            waits.append(quota_wait)
        concurrent_cap = max(1, math.floor(self.limits['max_concurrency'] * self.scales['concurrency']))
        if self.active >= concurrent_cap:
            leases = [r.lease_until for r in self.reservations if r.finished is None and r.lease_until is not None]
            waits.append(min(leases) - now if leases else float('inf'))
        if waits:
            return None, max(waits)
        record = Reservation(now, max(0, input_tokens), total_tokens, tier_floor=self.tier_floor,
            lease_until=now + lease_seconds if lease_seconds is not None else None, measurement=measurement)
        self.active += 1
        self.starts.append(now)
        self.reservations.append(record)
        tier = self._tier(record)
        if tier is not None:
            self.next_start = now + 60 / tier.rpm / self.scales['rpm']
        return record, 0.0

    def settle_state(self, record, response, now):
        if not record.apply_usage(response):
            return
        usage = response_usage(response)
        if usage is not None and usage[0] is not None and usage[1] > 0:
            prompt = usage[0]
            measurement = record.measurement or {}
            key = measurement.get('calibration_key')
            if key and measurement.get('source') == 'local_deepseek_v4':
                calibration = self.calibrations.setdefault(key, {'samples': 0, 'max_underestimate': 0})
                calibration['samples'] += 1
                calibration['max_underestimate'] = max(calibration['max_underestimate'], prompt - measurement['tokens'])
                calibration.update(last_local=measurement['tokens'], last_provider=prompt)
            small_boundary = self.tiers[0].max_input_tokens if self.tiers else 0
            if prompt <= small_boundary:
                if self.stable_since is None:
                    self.stable_since = now
                self.small_successes += 1
                if self.small_successes >= self.recovery_samples and now - self.stable_since >= self.recovery_window:
                    self.tier_floor = 0
            else:
                self.stable_since, self.small_successes = None, 0
            if now >= self.cooldown_until:
                if self.recovery_since is None:
                    self.recovery_since = now
                self.recovery_successes += 1
                if self.recovery_successes >= self.recovery_samples and now - self.recovery_since >= self.recovery_window:
                    self.scales = {key: min(1.0, value * 1.25) for key, value in self.scales.items()}
                    self.recovery_since, self.recovery_successes = now, 0
        tier = self._tier(record)
        if tier is not None and self.reservations and self.reservations[-1] is record:
            self.next_start = record.started + 60 / tier.rpm / self.scales['rpm']

    def finish_state(self, record, now):
        if record.finished is None:
            record.finished = now
            self.active = max(0, self.active - 1)

    def error_state(self, signal, now):
        if signal is None:
            return
        kinds = self.scales.keys() if signal.kind == 'unknown' else (signal.kind,)
        for kind in kinds:
            self.scales[kind] = max(0.0625, self.scales[kind] * 0.5)
        cooldown = signal.retry_after if signal.retry_after is not None else (1.0 if signal.kind == 'concurrency' else 60.0)
        self.cooldown_until = max(self.cooldown_until, now + cooldown)
        self.stable_since = self.recovery_since = None
        self.small_successes = self.recovery_successes = 0
        self.tier_floor = self.initial_floor

    def calibration(self, key):
        return dict(self.calibrations.get(key, {'samples': 0, 'max_underestimate': 0}))

    def data(self):
        data = {key: value for key, value in vars(self).items() if key not in {'condition', 'tiers', 'starts', 'reservations'}}
        data.update(tiers=[asdict(t) for t in self.tiers], starts=list(self.starts),
                    reservations=[record.data() for record in self.reservations])
        return data

    @classmethod
    def restore(cls, data):
        state = cls({**data['limits'], 'quota_tiers': data['tiers']})
        state.__dict__.update({key: value for key, value in data.items() if key not in {'tiers', 'starts', 'reservations'}})
        state.starts = deque(data['starts'])
        state.reservations = [Reservation(**record) for record in data['reservations']]
        return state
