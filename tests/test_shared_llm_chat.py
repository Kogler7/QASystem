"""Common chat transport contracts, independent of a running model service."""
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from external_services.llm.chat import create_chat_completion
from external_services.llm.runtime import (
    QuotaPool,
    ScheduledClient,
)
from qasystem_config.quota_schema import parse_quota_tiers, select_tier


def _call(create, **options):
    return create_chat_completion(create, model="local-model", messages=[{"role": "user", "content": "x"}],
                                  max_tokens=1024, disable_thinking=True, **options)


@pytest.mark.parametrize("parameter,extra", [
    ("thinking", {"thinking": {"type": "disabled"}}),
    ("enable_thinking", {"enable_thinking": False}),
    ("none", None),
])
def test_provider_switch_does_not_change_format_or_messages(parameter, extra):
    seen = []
    schema = {"type": "json_object"}
    _call(lambda **kw: seen.append(kw), thinking_parameter=parameter, response_format=schema)
    assert seen[0].get("extra_body") == extra
    assert seen[0]["response_format"] == schema
    assert seen[0]["messages"] == [{"role": "user", "content": "x"}]


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_authentication_throttling_and_outages_are_not_compatibility_retries(status):
    calls = []
    exc = RuntimeError("thinking is unsupported")
    exc.status_code = status
    def create(**kw):
        calls.append(kw)
        raise exc
    with pytest.raises(RuntimeError):
        _call(create)
    assert len(calls) == 1


@pytest.mark.parametrize("message", ["response_format is unsupported", "invalid JSON schema", "timed out"])
def test_other_failures_do_not_disable_thinking_and_retry(message):
    calls = []
    def create(**kw):
        calls.append(kw)
        raise RuntimeError(message)
    with pytest.raises(RuntimeError):
        _call(create)
    assert len(calls) == 1


def test_each_compatibility_attempt_acquires_shared_quota():
    seen = []
    def create(**kw):
        seen.append(kw)
        if len(seen) == 1:
            raise RuntimeError("thinking is not supported")
        return {"choices": []}
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = ScheduledClient(sdk, {"quota_group": uuid4().hex, "max_concurrency": 1,
                                  "qps": 0, "qpm": 0, "request_timeout": 10, "disable_thinking": True})
    _call(client.chat.completions.create, response_format={"type": "json_object"})
    assert len(client.pool.starts) == 2
    assert client.pool.active == 0
    assert "extra_body" not in seen[1]
    assert seen[1]["response_format"] == seen[0]["response_format"]


@pytest.mark.parametrize("tokens,rpm,tpm", [
    (8000, 56, 460_000),
    (8192, 56, 460_000),
    (9000, 26, 420_000),
    (20000, 11, 350_000),
    (40000, 4, 280_000),
])
def test_rate_tiers_follow_input_size(tokens, rpm, tpm):
    curve = [dict(max_input_tokens=bound, rpm=rate, tpm=budget) for bound, rate, budget in
             [(8192,56,460000),(16384,26,420000),(32768,11,350000),(65536,4,280000)]]
    tier = select_tier(parse_quota_tiers(curve), tokens)
    assert (tier.rpm, tier.tpm) == (rpm, tpm)
    pool = QuotaPool({'max_concurrency':4,'qps':0,'qpm':100,'tpm':500000,'quota_tiers':curve})
    with pool.request_slot(time.monotonic()+1, input_tokens=tokens) as reservation:
        assert pool._tier(reservation) == tier
        assert pool.next_start == pytest.approx(reservation.started + 60/rpm)
    unlimited = QuotaPool({'max_concurrency':4,'qps':0,'qpm':0})
    assert unlimited.tiers == () and unlimited.limits['tpm'] == 0


def test_tpm_window_blocks_until_budget_frees():
    pool = QuotaPool({"max_concurrency": 4, "qps": 0, "qpm": 0, "tpm": 1000})
    with pool.request_slot(input_tokens=900):
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="queue wait"):
            with pool.request_slot(started + 0.05, input_tokens=200):
                pytest.fail("TPM bypassed")
    assert pool.active == 0


def test_scheduled_client_reserves_input_plus_output_tokens(monkeypatch):
    from external_services.llm import runtime as llm_runtime
    monkeypatch.setattr(llm_runtime, "_pools", {})
    seen = []
    def create(**kw):
        seen.append(kw)
        return {"choices": []}
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = ScheduledClient(sdk, {
        "quota_group": uuid4().hex, "max_concurrency": 2, "qps": 0, "qpm": 0, "tpm": 1_000_000,
        "request_timeout": 10, "disable_thinking": True, "max_input_tokens": 65536,
    })
    client.chat.completions.create(
        model="m", messages=[{"role": "user", "content": "hello"}], max_tokens=128,
    )
    assert len(client.pool.reservations) == 1
    reserved = client.pool.reservations[0].total_tokens
    assert reserved > 128
    assert seen and seen[0]["max_tokens"] == 128
