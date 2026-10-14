from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.app.schemas import ExtractionOptions
from backend.app.services import extraction_config as config
from algorithm.knowledge_extract import request_runtime as runtime
from external_services.llm import runtime as llm_runtime


@pytest.fixture
def providers(monkeypatch):
    profiles = {
        "siliconflow": {
            "base_url": "https://provider.example/v1", "api_key_env": "TEST_EXTRACT_KEY",
            "model": "provider-model", "quota_group": "test-account", "max_concurrency": 2,
            "api_mode": "chat", "thinking_parameter": "enable_thinking",
        },
    }
    from copy import deepcopy
    from external_services import model_routes
    from external_services.llm import distributed
    def snapshot():
        services = {'default-chat': {'kind': 'api', 'adapter': 'chat',
                     'base_url':'https://provider.example/v1','model':'default-model',
                     'api_key_env':'TEST_EXTRACT_KEY'}}
        accounts = {}
        for name, profile in profiles.items():
            limits = {k: profile.get(k, 4 if k=='max_concurrency' else 0) for k in ('max_concurrency','qps','qpm')}
            limits.update({k: profile[k] for k in ('tpm','quota_tiers','adaptive_initial_input_tokens') if k in profile})
            group = profile.get('quota_group','test-account')
            accounts[group] = limits
            services[name] = {'kind':'api','adapter':'chat','base_url':profile['base_url'],
                'model':profile['model'],'api_key_env':profile['api_key_env'],
                'limits':limits,'account_id':group,
                'capabilities': {k:profile[k] for k in ('api_mode','thinking_parameter','response_format','context_tokens','max_output_tokens') if k in profile}}
        return (deepcopy({'settings':{},'models':{},'services':services,'accounts':accounts,
                         'routes':{'extract':'default-chat'}}), 'unit-approved')
    from qasystem_config import configuration as declared_configuration
    monkeypatch.setattr(declared_configuration,'read_registry',lambda **kwargs: snapshot())
    monkeypatch.setattr(config,'registry_snapshot',snapshot)
    monkeypatch.setattr(model_routes,'registry_snapshot',snapshot)
    monkeypatch.setattr(distributed,'service_pool',lambda s: llm_runtime.get_pool(s.get('account_id') or s['service_id'],s.get('limits') or {'max_concurrency':4,'qps':0,'qpm':0}))
    monkeypatch.setenv("TEST_EXTRACT_KEY", "secret-must-not-appear")
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v2")
    return profiles


def test_defaults_and_partial_override_do_not_change_environment(providers, monkeypatch):
    original = config.resolve_config({})
    selected = config.resolve_config({"version": "v3.1", "provider": "siliconflow", "request_timeout": 17.0})
    assert selected["version"] == "v3.1"
    assert selected["model"] == "provider-model"
    assert selected["request_timeout"] == 17
    assert selected["document_timeout"] == 900
    assert config.resolve_config({}) == original
    assert "secret-must-not-appear" not in json.dumps(selected)
    public = config.public_config(selected)
    assert "api_key_env" not in public and "base_url" not in public


def test_separate_input_output_limits_and_legacy_alias(providers, monkeypatch):
    providers['siliconflow'].update(context_tokens=36864, max_output_tokens=4096)
    chosen = config.resolve_config({'version': 'v3', 'provider': 'siliconflow'})
    assert chosen['max_input_tokens'] == 32768
    assert chosen['max_output_tokens'] == chosen['max_tokens'] == 4096
    assert config.public_config(chosen)['max_input_tokens'] == 32768
    # The legacy request alias still rejects conflicting explicit budgets;
    # service capabilities are independently registered, not a provider ENV.
    with pytest.raises(ValueError, match='Conflicting'):
        config.ProviderProfile(base_url='https://provider.example',api_key_env='',model='m',quota_group='a',max_tokens=8192,max_output_tokens=4096)


def test_default_environment_separates_budgets(providers, monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_LLM_MAX_INPUT_TOKENS', '32000')
    monkeypatch.setenv('KG_EXTRACT_LLM_MAX_OUTPUT_TOKENS', '8000')
    chosen = config.resolve_config({'version': 'v3'})
    assert chosen['max_input_tokens'] == 32000
    assert chosen['max_tokens'] == chosen['max_output_tokens'] == 8000


def test_v31_decoder_policy_is_snapshotted_and_publicly_reviewable(providers, monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_STREAM_GENERATION', 'true')
    monkeypatch.setenv('KG_EXTRACT_V31_FAST_VERIFICATION', 'true')
    monkeypatch.setenv('KG_EXTRACT_V31_VERIFICATION_MAX_TOKENS', '4096')
    chosen = config.resolve_config({'version': 'v3.1', 'provider': 'siliconflow'})
    monkeypatch.setenv('KG_EXTRACT_V31_STREAM_GENERATION', 'false')
    public = config.public_config(chosen)
    assert public['stream_generation'] is True
    assert public['fast_verification'] is True
    assert public['verification_max_tokens'] == 4096
    assert config.resolve_config({'version': 'v3.1', 'provider': 'siliconflow'})['stream_generation'] is True


def test_oversized_input_never_consumes_quota_or_calls_provider(providers, monkeypatch):
    from algorithm.token_budget import InputTokenLimitError
    chosen = config.resolve_config({'version': 'v3', 'provider': 'siliconflow'})
    chosen['max_input_tokens'] = 300
    monkeypatch.setattr(llm_runtime, '_pools', {})
    calls = []
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: calls.append(kw))))
    client = runtime.ScheduledClient(sdk, chosen)
    with pytest.raises(InputTokenLimitError):
        client.chat.completions.create(messages=[{'role': 'user', 'content': '密集表格' * 100}])
    assert calls == [] and list(client.pool.starts) == []


@pytest.mark.parametrize("options", [
    {"version": "v1"}, {"provider": "../private"}, {"base_url": "https://arbitrary"},
    {"api_key": "secret"}, {"request_timeout": 0}, {"document_timeout": float("inf")},
    {"request_timeout": True}, {"qps": 1000},
])
def test_api_rejects_invalid_or_sensitive_override(options):
    with pytest.raises(ValidationError):
        ExtractionOptions.model_validate(options)


def test_provider_errors_do_not_echo_profile_contents(providers, monkeypatch):
    providers["siliconflow"]["api_key_env"] = "MISSING_TEST_EXTRACT_KEY"
    with pytest.raises(config.ExtractionConfigurationError) as error:
        config.snapshot_payload("graph.extract", {"ontology": "x", "options": {"provider": "siliconflow"}})
    assert "secret-must-not-appear" not in str(error.value)


def test_submission_snapshot_survives_queue_and_retry(providers, monkeypatch, tmp_path):
    from backend.app.execution.manager import ExecutionManager
    from backend.app.execution.store import MemoryExecutionStore
    from backend.app.execution.artifacts import ArtifactStore
    from backend.app.execution.hub import ExecutionEventHub

    monkeypatch.setenv("EXECUTION_MODE", "async")
    monkeypatch.setenv("EXECUTION_ALLOW_MEMORY_QUEUE", "true")
    store = MemoryExecutionStore()
    manager = ExecutionManager(store=store, hub=ExecutionEventHub(store), artifacts=ArtifactStore(tmp_path))
    job = manager.submit(operation="graph.extract", resource_class="io.llm", payload={
        "ontology": "x", "items": [{"text": "x"}],
        "options": {"version": "v3", "provider": "siliconflow"},
        "__extraction_config": {"version": "injected"},
    })
    reference = store.get_job(job.job_id)["payload_ref"]
    before = manager.read_payload(reference)
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "latest")
    monkeypatch.setenv("KG_EXTRACT_PROVIDERS_JSON", "{}")
    after = manager.read_payload(reference)
    assert before == after
    assert after["__extraction_config"]["version"] == "v3"
    assert after["__extraction_config"]["model"] == "provider-model"
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert b"secret-must-not-appear" not in path.read_bytes()


def test_pipeline_snapshot_and_unstructured_rejection(providers):
    data = config.snapshot_payload("knowledge.ingestion", {
        "ontology": "x", "options": {"extraction": {"provider": "siliconflow", "version": "v3.1"}},
    })
    assert data["__extraction_config"]["version"] == "v3.1"
    with pytest.raises(config.ExtractionConfigurationError):
        config.snapshot_payload("graph.extract", {"ontology": "", "options": {"version": "v3"}})


@pytest.mark.parametrize("version", ["v0", "v2", "v2.1", "v3", "latest", "v3.1"])
def test_each_version_receives_isolated_client_and_effective_config(providers, monkeypatch, version):
    from backend.app.services import knowledge_extract_adapter as adapter

    chosen = config.resolve_config({"version": version, "provider": "siliconflow", "model": "custom", "document_timeout": 60.0})
    fake_llm = object()
    monkeypatch.setattr(runtime, "create_request_llm", lambda c: fake_llm)
    seen = {}

    def runner(**kwargs):
        seen.update(kwargs)
        return {"status": "succeeded"}

    result = adapter.run_aggregate("ontology", ["text"], runner=runner, extraction_config=chosen)
    assert seen["llm"] is fake_llm and seen["use_scheduler"] is False
    assert seen["model"] == "custom"
    assert result["extract_version"] == version
    assert result["effective_config"]["provider"] == "siliconflow"
    assert "secret-must-not-appear" not in json.dumps(result)
    if version == "v3.1":
        assert seen["document_timeout"] == 60


def test_concurrent_versions_do_not_cross_contaminate(providers, monkeypatch):
    from backend.app.services import knowledge_extract_adapter as adapter

    barrier = threading.Barrier(2)
    monkeypatch.setattr(runtime, "create_request_llm", lambda c: SimpleNamespace(model=c["model"]))

    def run(version):
        chosen = config.resolve_config({"version": version, "provider": "siliconflow", "model": version + "-model"})
        def runner(**kwargs):
            barrier.wait(timeout=3)
            assert kwargs["llm"].model == version + "-model"
            return {"status": "succeeded"}
        return adapter.run_aggregate("ontology", ["text"], runner=runner, extraction_config=chosen)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["v2", "v3.1"]))
    assert [r["extract_version"] for r in results] == ["v2", "v3.1"]


def test_quota_pool_is_shared_across_models_and_wait_is_bounded(providers, monkeypatch):
    monkeypatch.setattr(llm_runtime, "_pools", {})
    c = config.resolve_config({"provider": "siliconflow"})
    c["max_concurrency"] = 1
    pool = runtime.get_pool(c["quota_group"], c)
    alias = {**c, "model": "other", "max_concurrency": 20}
    assert runtime.get_pool(c["quota_group"], alias) is pool
    assert pool.limits["max_concurrency"] == 1
    with pool.request_slot():
        with pytest.raises(TimeoutError, match="queue wait"):
            with pool.request_slot(time.monotonic() + 0.01):
                pytest.fail("Quota bypassed")
    assert pool.active == 0


def test_http_deadline_and_provider_thinking_parameter(providers, monkeypatch):
    monkeypatch.setattr(llm_runtime, "_pools", {})
    seen = []
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: seen.append(kw))))
    chosen = config.resolve_config({"provider": "siliconflow", "document_timeout": 2.0})
    wrapped = runtime.ScheduledClient(client, chosen)
    wrapped.chat.completions.create(extra_body={"thinking": {"type": "disabled"}})
    assert seen[0]["extra_body"] == {"enable_thinking": False}
    assert 0 < seen[0]["timeout"] <= 2
    wrapped.deadline = time.monotonic() - 1
    with pytest.raises(TimeoutError):
        wrapped.chat.completions.create()
    assert len(seen) == 1


def test_provider_json_format_override_is_request_local(providers, monkeypatch):
    monkeypatch.setattr(llm_runtime, "_pools", {})
    seen = []
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: seen.append(kw))))
    chosen = config.resolve_config({"provider": "siliconflow"})
    schema = {"type": "json_schema", "json_schema": {"name": "test", "schema": {"type": "object"}}}
    runtime.ScheduledClient(client, {**chosen, "response_format": "json_object"}).chat.completions.create(response_format=schema)
    runtime.ScheduledClient(client, chosen).chat.completions.create(response_format=schema)
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert seen[1]["response_format"] == schema
    assert schema["type"] == "json_schema"


@pytest.mark.parametrize("version", ["v0", "v2", "v2.1", "v3", "latest", "v3.1"])
@pytest.mark.parametrize("max_tokens", [4096, 16384])
def test_live_client_configuration_uses_chat_only_without_network(providers, monkeypatch, version, max_tokens):
    import openai
    from algorithm.output_batching import output_batch_size

    providers["siliconflow"]["max_output_tokens"] = max_tokens
    seen = []
    def create(**kwargs):
        seen.append(kwargs)
        return {"choices": [{"message": {"content": '{"entities": []}'}, "finish_reason": "stop"}]}
    def sdk(**kwargs):
        assert kwargs["api_key"] == "secret-must-not-appear"
        assert kwargs["base_url"] == "https://provider.example/v1"
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai, "OpenAI", sdk)
    monkeypatch.setattr(llm_runtime, "_pools", {})
    llm = runtime.create_request_llm(config.resolve_config({"version": version, "provider": "siliconflow"}))
    result = llm.generate_structured({"type": "object"}, [{"role": "user", "content": "x"}])
    assert result.finish_reason == "stop"
    assert seen[0]["max_tokens"] == max_tokens
    assert llm.max_tokens == max_tokens
    assert output_batch_size(llm, tokens_per_item=1024, ceiling=24) == int(max_tokens * 0.65) // 1024
    assert seen[0]["model"] == "provider-model"
    assert seen[0]["extra_body"] == {"enable_thinking": False}


def test_http_submission_returns_effective_config(providers, protocol_module):
    from fastapi.testclient import TestClient

    response = TestClient(protocol_module.app).post("/api/v1/graphs/extract", json={
        "ontology": "@startuml\nclass Term {\n name: String\n}\n@enduml",
        "items": [{"text": "test"}],
        "options": {"version": "v3.1", "provider": "siliconflow", "model": "chosen-model",
                    "request_timeout": 25, "document_timeout": 90},
    })
    assert response.status_code == 200, response.text
    effective = response.json()["data"]["quality"]["effective_config"]
    assert effective["version"] == "v3.1" and effective["model"] == "chosen-model"
    assert effective["request_timeout"] == 25 and effective["document_timeout"] == 90
    assert "secret-must-not-appear" not in response.text
    assert "api_key_env" not in response.text


def test_http_unknown_provider_is_configuration_error(providers, protocol_module):
    from fastapi.testclient import TestClient

    response = TestClient(protocol_module.app).post("/api/v1/graphs/extract", json={
        "ontology": "ontology", "items": [{"text": "test"}], "options": {"provider": "unknown"},
    })
    assert response.status_code == 422
    assert "secret-must-not-appear" not in response.text


def test_pipeline_http_preserves_same_overrides(providers, protocol_module):
    from fastapi.testclient import TestClient

    response = TestClient(protocol_module.app).post("/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "class Term", "options": json.dumps({"extraction": {
            "version": "v3", "provider": "siliconflow", "document_timeout": 60,
        }})},
    )
    assert response.status_code == 200, response.text
    effective = response.json()["data"]["extraction"]["quality"]["effective_config"]
    assert effective["version"] == "v3"
    assert effective["provider"] == "siliconflow" and effective["document_timeout"] == 60


def test_refactored_v31_default_config_and_live_factory(providers, monkeypatch):
    from algorithm.knowledge_extract.v3_1 import llm_client
    monkeypatch.setenv("KG_EXTRACT_TRACE_DIR", "")
    chosen = config.resolve_config({"version": "v3.1"})
    seen = {}
    def factory(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(client=SimpleNamespace())
    monkeypatch.setattr(llm_client, "create_llm_client", factory)
    runtime.create_request_llm(chosen)
    assert seen["model"] == chosen["model"]
    assert seen["disable_thinking"] == chosen["disable_thinking"]
    assert seen["schedule_requests"] is False
    assert seen["semantic_thinking"] is chosen["semantic_thinking"]
    assert seen["thinking_parameter"] == chosen["thinking_parameter"]
    assert config.public_config(chosen).get("output_format") != "jsonl"


def test_quota_curve_defaults_provider_override_and_snapshot(providers, monkeypatch):
    curve = [dict(max_input_tokens=8192, rpm=3, tpm=50000)]
    chosen = config.resolve_config({'version':'v3.1','provider':'siliconflow'})
    assert chosen['service_snapshot']['limits'].get('quota_tiers',[]) == []
    providers['siliconflow']['quota_tiers'] = curve
    data = config.snapshot_payload('graph.extract', {'ontology':'x','items':[{'text':'x'}],
        'options':{'version':'v3.1','provider':'siliconflow'}})
    providers['siliconflow']['quota_tiers'] = []
    assert data['__extraction_config']['service_snapshot']['limits']['quota_tiers'] == curve
    assert config.resolve_config({'provider':'siliconflow'})['service_snapshot']['limits']['quota_tiers'] == []


def test_quota_curve_is_bound_to_hot_registry_snapshot(providers):
    curve = [dict(max_input_tokens=8192,rpm=3,tpm=50000)]
    providers['siliconflow']['quota_tiers'] = curve
    registry, _ = config.registry_snapshot()
    chosen = config.resolve_config({'version':'v3.1','provider':'siliconflow'},registry=(registry,7))
    assert chosen['service_snapshot']['revision'] == 7
    assert chosen['service_snapshot']['limits']['quota_tiers'] == curve


def test_invalid_quota_configuration_is_redacted_at_submission(providers, monkeypatch):
    providers['siliconflow']['quota_tiers'] = 'private-invalid-quota'
    with pytest.raises(ValueError) as error:
        config.resolve_config({'version':'v3.1','provider':'siliconflow'})
    assert 'private' not in str(error.value)


def test_named_provider_rejects_increasing_quota_curve(providers, monkeypatch):
    providers['siliconflow']['quota_tiers'] = [dict(max_input_tokens=10,rpm=1,tpm=100),dict(max_input_tokens=20,rpm=2,tpm=80)]
    with pytest.raises(ValueError): config.resolve_config({'provider':'siliconflow'})


def test_v31_retry_factory_retains_submitted_quota_snapshot(providers, monkeypatch):
    import openai
    curve = [dict(max_input_tokens=8192,rpm=3,tpm=50000)]
    providers['siliconflow']['quota_tiers'] = curve
    chosen = config.resolve_config({'version':'v3.1','provider':'siliconflow'})
    providers['siliconflow']['quota_tiers'] = []
    monkeypatch.setattr(openai,'OpenAI',lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(llm_runtime,'_pools',{})
    llm = runtime.create_request_llm(chosen)
    assert [vars(t) for t in llm.client.pool.tiers] == curve
    assert not isinstance(llm.client.client,runtime.ScheduledClient)


def test_v31_semantic_thinking_defaults_ignore_retired_environment(providers, monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_SEMANTIC_THINKING', '0')
    options = {'version': 'v3.1', 'provider': 'siliconflow'}
    chosen = config.resolve_config(options)
    assert chosen['disable_thinking'] is True and chosen['semantic_thinking'] is True
    assert config.public_config(chosen)['semantic_thinking'] is True


def test_v31_semantic_thinking_submission_is_preserved_after_hot_configuration_changes(providers, monkeypatch):
    import openai
    monkeypatch.setenv("KG_EXTRACT_V31_SEMANTIC_THINKING", "0")
    chosen = config.resolve_config({"version": "v3.1", "provider": "siliconflow"})
    chosen["quota_tiers"] = []
    monkeypatch.setenv("KG_EXTRACT_V31_SEMANTIC_THINKING", "invalid-new-plan")
    monkeypatch.setenv("KG_EXTRACT_TRACE_DIR", "")
    seen = []
    def create(**kwargs):
        seen.append(kwargs)
        return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(llm_runtime, "_pools", {})
    llm = runtime.create_request_llm(chosen)
    result = llm.generate_structured({}, [{"role": "user", "content": "x"}],
                                     {"pass_name": "entity_verification"})
    assert llm.semantic_thinking is True
    assert seen[0]["extra_body"] == {"enable_thinking": True}
    assert result.thinking["sent"] == "enabled"
    assert config.resolve_config({"version": "v3.1"})["semantic_thinking"] is True


def test_v31_scheduled_client_preserves_enabled_semantic_pass_and_parameter_fallback(providers, monkeypatch):
    import openai
    chosen = config.resolve_config({"version": "v3.1", "provider": "siliconflow"})
    chosen["quota_tiers"] = []
    monkeypatch.setenv("KG_EXTRACT_TRACE_DIR", "")
    monkeypatch.setattr(llm_runtime, "_pools", {})
    seen = []
    def create(**kwargs):
        seen.append(kwargs)
        if len(seen) == 1:
            raise ValueError("unknown parameter enable_thinking")
        return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(openai, "OpenAI", lambda **kwargs: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    llm = runtime.create_request_llm(chosen)
    result = llm.generate_structured({}, [{"role": "user", "content": "x"}],
                                     {"pass_name": "property_verification"})
    assert seen[0]["extra_body"] == {"enable_thinking": True}
    assert "extra_body" not in seen[1]
    assert result.thinking["fallback"] is True
    assert len(llm.client.pool.starts) == 2


@pytest.mark.parametrize("enabled", [False, True])
def test_shared_scheduler_maps_per_call_thinking_instead_of_global_disable(providers, monkeypatch, enabled):
    chosen = config.resolve_config({"provider": "siliconflow"})
    chosen["quota_tiers"] = []
    seen = []
    sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: seen.append(kw))))
    monkeypatch.setattr(llm_runtime, "_pools", {})
    wrapped = runtime.ScheduledClient(sdk, chosen)
    wrapped.chat.completions.create(extra_body={"thinking": {"type": "enabled" if enabled else "disabled"},
                                              "other": "preserved"})
    assert seen[0]["extra_body"] == {"enable_thinking": enabled, "other": "preserved"}


def test_document_scale_reads_chunks_and_estimates_uploaded_files():
    assert config.document_scale("graph.extract", {"items": [{"text": "abc"}, {"text": "de"}]}) == (5, 2)
    assert config.document_scale("graph.extract", {"items": []}) is None
    # 文本上传按字符数精确计数，片数按请求自己的切片尺寸估算
    text = {"files": [{"file_name": "a.md", "content": "字" * 10_000}],
            "options": {"chunking": {"size": 4000}}}
    assert config.document_scale("knowledge.ingestion", text) == (10_000, 3)
    # 二进制（PDF 等）无法解码，退化为字节上界
    binary = {"files": [{"content": b"\xff\xfe\x00" * 100}]}
    assert config.document_scale("knowledge.ingestion", binary) == (300, 1)


def test_v31_document_timeout_adapts_to_input_size(providers, monkeypatch):
    monkeypatch.delenv("KG_EXTRACT_V31_DOCUMENT_TIMEOUT", raising=False)
    single = config.resolve_config({"version": "v3.1"}, document_scale=(1000, 1))
    medium = config.resolve_config({"version": "v3.1"}, document_scale=(4509, 3))
    large = config.resolve_config({"version": "v3.1"}, document_scale=(22170, 9))
    assert single["document_timeout"] == config.DOCUMENT_TIMEOUT_FLOOR
    assert medium["document_timeout"] == pytest.approx(config.adaptive_document_timeout(4509, 3))
    assert single["document_timeout"] < medium["document_timeout"] < large["document_timeout"]
    assert large["document_timeout"] == config.DOCUMENT_TIMEOUT_CEILING
    # 拿不到输入规模时保持既有单片默认，不让未知输入失去边界
    assert config.resolve_config({"version": "v3.1"})["document_timeout"] == config.DOCUMENT_TIMEOUT_FLOOR
    assert config.resolve_config({"version": "v3"}).__getitem__("document_timeout") is None


def test_explicit_document_timeout_still_overrides_adaptive_default(providers, monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_V31_DOCUMENT_TIMEOUT", "1500")
    assert config.resolve_config({"version": "v3.1"}, document_scale=(22170, 9))["document_timeout"] == 1500
    monkeypatch.delenv("KG_EXTRACT_V31_DOCUMENT_TIMEOUT")
    chosen = config.resolve_config({"version": "v3.1", "document_timeout": 60.0}, document_scale=(22170, 9))
    assert chosen["document_timeout"] == 60
    with pytest.raises(config.ExtractionConfigurationError):
        config.snapshot_payload("graph.extract", {
            "ontology": "@startuml\nclass Term {\n name: String\n}\n@enduml",
            "items": [{"text": "x"}], "options": {"version": "v3.1", "document_timeout": 0},
        })


def test_http_submission_reports_adaptive_document_timeout(providers, protocol_module, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("KG_EXTRACT_V31_DOCUMENT_TIMEOUT", raising=False)
    response = TestClient(protocol_module.app).post("/api/v1/graphs/extract", json={
        "ontology": "@startuml\nclass Term {\n name: String\n}\n@enduml",
        "items": [{"text": "x" * 4509}],
        "options": {"version": "v3.1", "provider": "siliconflow"},
    })
    assert response.status_code == 200, response.text
    effective = response.json()["data"]["quality"]["effective_config"]
    assert effective["document_timeout"] == pytest.approx(config.adaptive_document_timeout(4509, 1))
    assert effective["document_timeout"] > config.DOCUMENT_TIMEOUT_FLOOR


def test_hybrid_version_uses_registered_service_without_legacy_connection(providers, monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_LLM_BASE_URL', 'http://retired.invalid')
    chosen = config.resolve_config({'version': 'v2.2-v31'})
    assert chosen['version'] == 'v2.2-v31'
    assert chosen['base_url'] != 'http://retired.invalid'
    assert chosen['service_snapshot']['service_id'] == chosen['model_service']
