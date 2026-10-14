"""Quota admission contracts with a virtual clock; no network or minute sleeps."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from docker.configuration import declarations
from external_services.llm import runtime
from external_services.llm.quota import parse_quota_tiers, select_tier, response_usage


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(runtime, "_pools", {})
    return clock


def pool(clock, tiers, **limits):
    result = runtime.QuotaPool({"max_concurrency": 16, "qps": 0, "qpm": 0,
                               "quota_tiers": tiers, **limits})
    def wait(timeout=None):
        assert timeout is not None and timeout > 0, "Unbounded quota wait"
        clock.now += timeout
    result.condition.wait = wait
    return result


def defaults():
    return declarations()["items"]["KG_EXTRACT_LLM_QUOTA_TIERS_JSON"]["default"]


@pytest.mark.parametrize("tokens,rpm,tpm", [
    (1, 56, 460000), (8192, 56, 460000), (8193, 26, 420000),
    (16384, 26, 420000), (16385, 11, 350000), (32768, 11, 350000),
    (32769, 4, 280000), (65536, 4, 280000), (65537, 4, 280000),
])
def test_default_input_curve_selects_ceiling(tokens, rpm, tpm):
    tier = select_tier(parse_quota_tiers(defaults()), tokens)
    assert (tier.rpm, tier.tpm) == (rpm, tpm)


@pytest.mark.parametrize("value", [None, {}, "private-invalid-json", [dict(max_input_tokens=True, rpm=2, tpm=100)],
    [dict(max_input_tokens=10, rpm=0, tpm=100)],
    [dict(max_input_tokens=10, rpm=2, tpm=100, api_key="private")],
    [dict(max_input_tokens=10, rpm=2, tpm=100), dict(max_input_tokens=10, rpm=1, tpm=80)],
    [dict(max_input_tokens=10, rpm=2, tpm=100), dict(max_input_tokens=20, rpm=3, tpm=80)],
])
def test_invalid_curve_is_explicit_and_redacted(value):
    with pytest.raises(ValueError, match="Invalid extraction quota tiers") as error:
        parse_quota_tiers(value)
    assert "private" not in str(error.value)


def test_rpm_pacing_and_rolling_window(clock):
    quota = pool(clock, defaults())
    for _ in range(56):
        with quota.request_slot(70, input_tokens=8192):
            pass
    assert clock.now == pytest.approx(55 * 60 / 56)
    with pytest.raises(TimeoutError):
        with quota.request_slot(59.9, input_tokens=8192):
            pytest.fail("57th call admitted inside the minute")
    with quota.request_slot(70, input_tokens=8192):
        assert clock.now == pytest.approx(60)


def test_tpm_counts_input_plus_reserved_output(clock):
    quota = pool(clock, defaults())
    for _ in range(18):
        with quota.request_slot(60, input_tokens=8192, max_output_tokens=16384):
            pass
    with pytest.raises(TimeoutError):
        with quota.request_slot(59, input_tokens=8192, max_output_tokens=16384):
            pytest.fail("Output reservation must count toward TPM")
    with quota.request_slot(61, input_tokens=8192, max_output_tokens=16384):
        assert clock.now == pytest.approx(60)


def test_mixed_input_sizes_share_one_account_budget(clock):
    tiers = [dict(max_input_tokens=10, rpm=2, tpm=100), dict(max_input_tokens=20, rpm=1, tpm=80)]
    quota = pool(clock, tiers)
    with quota.request_slot(61, input_tokens=10):
        pass
    with pytest.raises(TimeoutError):
        with quota.request_slot(59, input_tokens=20):
            pytest.fail("Large requests cannot use a separate RPM bucket")
    assert quota.active == 0
    with quota.request_slot(61, input_tokens=20):
        assert clock.now == 60


def test_inflight_tokens_cannot_expire_before_generation_finishes(clock):
    quota = pool(clock, [dict(max_input_tokens=100, rpm=100, tpm=100)])
    with quota.request_slot(200, input_tokens=10, max_output_tokens=90) as first:
        clock.now = 61
        with pytest.raises(TimeoutError):
            with quota.request_slot(62, input_tokens=1):
                pytest.fail("Pending output reservation expired")
        assert quota.active == 1
        first.settle({"usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}})
        with quota.request_slot(63, input_tokens=1):
            assert quota.active == 2
    assert quota.active == 0


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
])
def test_returned_usage_refunds_unused_reservation(clock, usage):
    quota = pool(clock, [dict(max_input_tokens=100, rpm=100, tpm=100)])
    with quota.request_slot(10, input_tokens=10, max_output_tokens=90) as first:
        first.settle(SimpleNamespace(usage=usage))
    with quota.request_slot(10, input_tokens=10, max_output_tokens=70):
        assert first.total_tokens == 15
    assert clock.now == pytest.approx(.6)


@pytest.mark.parametrize("response", [{}, {"usage": {}}, {"usage": {"total_tokens": -1}},
                                      {"usage": {"total_tokens": True}}])
def test_missing_or_invalid_usage_retains_reservation(clock, response):
    quota = pool(clock, [dict(max_input_tokens=100, rpm=100, tpm=100)])
    with quota.request_slot(100, input_tokens=10, max_output_tokens=90) as first:
        first.settle(response)
    with pytest.raises(TimeoutError):
        with quota.request_slot(59, input_tokens=1):
            pytest.fail("Unreported usage must not refund output")
    assert first.total_tokens == 100


def test_usage_total_cannot_undercount_input_and_output():
    assert response_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 1}}) == (10, 15)


def test_token_wait_does_not_claim_concurrency_or_block_a_small_request(clock):
    quota = pool(clock, [dict(max_input_tokens=100, rpm=100, tpm=100)])
    with quota.request_slot(100, input_tokens=50):
        pass
    with pytest.raises(TimeoutError):
        with quota.request_slot(1, input_tokens=60):
            pytest.fail("Token quota exceeded")
    assert quota.active == 0
    with quota.request_slot(2, input_tokens=10):
        assert quota.active == 1


def test_oversized_single_reservation_fails_without_consuming_quota(clock):
    quota = pool(clock, [dict(max_input_tokens=100, rpm=100, tpm=100)])
    with pytest.raises(ValueError, match="minute token quota"):
        with quota.request_slot(100, input_tokens=10, max_output_tokens=91):
            pytest.fail("Impossible request admitted")
    assert not quota.starts and quota.active == 0


def test_alias_cannot_reset_or_loosen_a_quota_curve(clock):
    config = dict(max_concurrency=16, qps=0, qpm=0, quota_tiers=defaults())
    quota = runtime.get_pool("one-account", config)
    quota.condition.wait = lambda timeout: setattr(clock, "now", clock.now + timeout)
    with quota.request_slot(100, input_tokens=65536):
        pass
    assert runtime.get_pool("one-account", {**config, "quota_tiers": []}) is quota
    assert select_tier(quota.tiers, 65536).rpm == 4
    strict = [dict(max_input_tokens=8192, rpm=10, tpm=400000),
              dict(max_input_tokens=65536, rpm=2, tpm=200000)]
    runtime.get_pool("one-account", {**config, "quota_tiers": strict})
    assert select_tier(quota.tiers, 16384).rpm == 2
    with quota.request_slot(100, input_tokens=10):
        assert clock.now == 30


def test_legacy_qpm_adds_a_limit_to_the_curve(clock):
    quota = pool(clock, defaults(), qpm=1)
    with quota.request_slot(100, input_tokens=10):
        pass
    with quota.request_slot(100, input_tokens=10):
        assert clock.now == 60


def test_queue_wait_uses_document_deadline_and_http_gets_full_timeout(clock):
    calls = []
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: calls.append(kw))))
    client = runtime.ScheduledClient(sdk, dict(quota_group=uuid4().hex, max_concurrency=1, qps=0, qpm=1,
        request_timeout=10, document_timeout=100, disable_thinking=True, quota_tiers=[]))
    client.pool.condition.wait = lambda timeout: setattr(clock, "now", clock.now + timeout)
    client.chat.completions.create()
    client.chat.completions.create()
    assert clock.now == 60
    assert [call["timeout"] for call in calls] == [10, 10]


def test_failed_http_attempt_counts_quota_and_releases_slot(clock):
    def fail(**kwargs):
        raise RuntimeError("provider failed")
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fail)))
    client = runtime.ScheduledClient(sdk, dict(quota_group=uuid4().hex, max_concurrency=1, qps=0, qpm=0,
        request_timeout=10, disable_thinking=True, quota_tiers=defaults(), max_tokens=1024))
    with pytest.raises(RuntimeError):
        client.chat.completions.create(messages=[dict(role="user", content="x")])
    assert client.pool.active == 0 and len(client.pool.starts) == 1
    assert client.pool.reservations[0].total_tokens > 1024


def test_token_reservations_are_atomic_across_real_threads():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    quota = runtime.QuotaPool(dict(max_concurrency=16, qps=0, qpm=0,
        quota_tiers=[dict(max_input_tokens=100, rpm=10000000, tpm=100)]))
    barrier = threading.Barrier(3)
    observations = []
    def call(index):
        with quota.request_slot(time.monotonic() + 3, input_tokens=10, max_output_tokens=20) as reservation:
            with quota.condition:
                observations.append(quota.active)
                ordinal = len(observations)
                assert sum(r.total_tokens for r in quota.reservations) <= 100
            if ordinal <= 3:
                barrier.wait(timeout=2)
            with quota.condition:
                reservation.settle({"usage": {"input_tokens": 10, "output_tokens": 0, "total_tokens": 10}})
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(call, range(8)))
    assert max(observations) == 3
    assert quota.active == 0


def test_initial_adaptive_cap_never_promotes_a_long_request(clock):
    quota = pool(clock, defaults(), adaptive_initial_input_tokens=16384)
    with quota.request_slot(100, input_tokens=1000) as first:
        assert (quota._tier(first).rpm, quota._tier(first).tpm) == (26, 420000)
    with quota.request_slot(100, input_tokens=16385) as second:
        assert (quota._tier(second).rpm, quota._tier(second).tpm) == (11, 350000)


def test_small_tier_recovers_only_after_stable_authoritative_usage(clock):
    quota = pool(clock, defaults(), adaptive_initial_input_tokens=16384)
    for index in range(8):
        clock.now = index * 10
        with quota.request_slot(200, input_tokens=8192) as reservation:
            reservation.settle({'usage': {'prompt_tokens': 8192, 'completion_tokens': 1}})
        if index < 7:
            assert quota.tier_floor == 16384
    assert quota.tier_floor == 0
    with quota.request_slot(200, input_tokens=8192) as recovered:
        assert (quota._tier(recovered).rpm, quota._tier(recovered).tpm) == (56, 460000)


def test_unknown_usage_does_not_promote_the_short_tier(clock):
    quota = pool(clock, defaults(), adaptive_initial_input_tokens=16384)
    for index in range(10):
        clock.now = index * 10
        with quota.request_slot(200, input_tokens=1000) as record:
            record.settle({'usage': {}})
    assert quota.tier_floor == 16384


@pytest.mark.parametrize('usage', [{'total_tokens': 0}, {'prompt_tokens': 0, 'completion_tokens': 0},
                                  {'prompt_tokens': 0, 'completion_tokens': 5},
                                  {'total_tokens': 1}, {'prompt_tokens': 10}])
def test_unknown_or_zero_usage_never_zeros_a_real_reservation(clock, usage):
    quota = pool(clock, defaults())
    with quota.request_slot(100, input_tokens=100, max_output_tokens=100) as record:
        record.settle({'usage': usage})
        assert record.total_tokens == 200


def test_settlement_is_exactly_once_even_if_called_again(clock):
    quota = pool(clock, defaults())
    with quota.request_slot(100, input_tokens=100, max_output_tokens=100) as record:
        record.settle({'usage': {'prompt_tokens': 90, 'completion_tokens': 50}})
        record.settle({'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})
    assert (record.input_tokens, record.total_tokens) == (90, 140)


@pytest.mark.parametrize('message,kind', [('RPM limit', 'rpm'), ('tokens per minute limit', 'tpm'),
                                        ('Requests rate limit exceeded', 'rpm'),
                                        ('concurrency limit', 'concurrency'), ('rate limit', 'unknown')])
def test_429_cools_and_reduces_only_the_identified_dimension(clock, message, kind):
    quota = pool(clock, defaults())
    error = RuntimeError(message)
    error.status_code = 429
    error.response = SimpleNamespace(headers={'Retry-After': '7'})
    signal = quota.report_error(error)
    assert signal.kind == kind
    assert quota.cooldown_until == 7
    for dimension, scale in quota.scales.items():
        assert scale == (0.5 if dimension == kind or kind == 'unknown' else 1.0)
    with quota.request_slot(100, input_tokens=1):
        assert clock.now == 7


def test_timeout_is_never_rate_limit_evidence(clock):
    quota = pool(clock, defaults())
    assert quota.report_error(TimeoutError('429 tokens per minute concurrency limit')) is None
    assert quota.scales == dict(rpm=1.0, tpm=1.0, concurrency=1.0)
    assert quota.cooldown_until == 0


def test_usage_calibration_is_version_scoped_and_never_reduces_estimates(clock):
    quota = pool(clock, defaults())
    measurement = {'source': 'local_deepseek_v4', 'tokens': 100, 'calibration_key': 'revision-a'}
    with quota.request_slot(100, input_tokens=100, measurement=measurement) as record:
        record.settle({'usage': {'prompt_tokens': 110, 'completion_tokens': 10}})
    assert quota.calibration('revision-a')['max_underestimate'] == 10
    assert quota.calibration('revision-b')['max_underestimate'] == 0
    with quota.request_slot(100, input_tokens=110, measurement=measurement) as record:
        record.settle({'usage': {'prompt_tokens': 90, 'completion_tokens': 10}})
    assert quota.calibration('revision-a') == {'samples': 2, 'max_underestimate': 10,
                                             'last_local': 100, 'last_provider': 90}


def test_stream_consumer_runs_before_settlement_and_slot_release(clock):
    sdk = SimpleNamespace()
    client = runtime.ScheduledClient(sdk, dict(quota_group=uuid4().hex, max_concurrency=1,
        qps=0, qpm=0, request_timeout=10, document_timeout=100, disable_thinking=True, max_tokens=100))
    def create(**kwargs):
        assert '_response_consumer' not in kwargs
        assert kwargs['stream'] is True
        return iter(['chunk'])
    def consume(response):
        assert client.call_deadline == 10
        assert client.pool.active == 1
        assert client.pool.reservations[-1].settled is False
        assert list(response) == ['chunk']
        clock.now = 5
        return {'usage': {'prompt_tokens': 20, 'completion_tokens': 5}}
    client._call(create, messages=[{'role': 'user', 'content': 'x'}], stream=True,
                 _response_consumer=consume)
    assert client.pool.active == 0
    assert client.pool.reservations[-1].total_tokens == 25
    assert client.call_deadline is None


def test_stream_timeout_retains_unknown_reservation_without_rate_penalty(clock):
    client = runtime.ScheduledClient(SimpleNamespace(), dict(quota_group=uuid4().hex, max_concurrency=1,
        qps=0, qpm=0, request_timeout=10, disable_thinking=True, max_tokens=100))
    def consume(response):
        raise TimeoutError('stream interrupted')
    with pytest.raises(TimeoutError):
        client._call(lambda **kw: iter([]), messages=[{'role': 'user', 'content': 'x'}], stream=True,
                     _response_consumer=consume)
    assert client.pool.active == 0
    assert client.pool.reservations[-1].total_tokens > 100
    assert client.pool.scales == dict(rpm=1.0, tpm=1.0, concurrency=1.0)


def test_live_local_consumer_cannot_expire_a_concurrency_slot(clock):
    quota = pool(clock, [], max_concurrency=1)
    with quota.request_slot(1, input_tokens=10, lease_seconds=.1):
        clock.now = 2
        with pytest.raises(TimeoutError):
            with quota.request_slot(3, input_tokens=1):
                pytest.fail('A live consumer was treated as a dead worker')
        assert quota.active == 1


@pytest.mark.parametrize('duration,known,admitted_at', [
    (20, True, 60), (80, True, 80), (20, False, 80), (80, False, 140),
])
def test_completed_token_window_uses_started_for_known_and_finished_for_unknown(clock, duration, known, admitted_at):
    quota = pool(clock, [dict(max_input_tokens=100, rpm=1000000, tpm=100)])
    with quota.request_slot(200, input_tokens=10, max_output_tokens=90) as first:
        clock.now = duration
        if known:
            first.settle({'usage': {'prompt_tokens': 10, 'completion_tokens': 90}})
        else:
            first.settle({'usage': {}})
    with quota.request_slot(200, input_tokens=1):
        assert clock.now == admitted_at


def test_authoritative_usage_does_not_expire_tokens_or_concurrency_while_active(clock):
    quota = pool(clock, [dict(max_input_tokens=100, rpm=1000000, tpm=100)], max_concurrency=1)
    with quota.request_slot(200, input_tokens=10, max_output_tokens=90) as first:
        first.settle({'usage': {'prompt_tokens': 10, 'completion_tokens': 90}})
        clock.now = 80
        with pytest.raises(TimeoutError):
            with quota.request_slot(81, input_tokens=1):
                pytest.fail('Known but active request released its reservation after 60 seconds')
        assert quota.active == 1 and first in quota.reservations
        assert first.total_tokens == 100
    # Both limits may release only now, once the old request actually finishes.
    with quota.request_slot(82, input_tokens=1):
        assert clock.now == 81
        assert first not in quota.reservations
