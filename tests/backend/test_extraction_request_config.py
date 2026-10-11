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


@pytest.fixture
def providers(monkeypatch):
    profiles = {
        "siliconflow": {
            "base_url": "https://provider.example/v1", "api_key_env": "TEST_EXTRACT_KEY",
            "model": "provider-model", "quota_group": "test-account", "max_concurrency": 2,
            "api_mode": "chat", "thinking_parameter": "enable_thinking",
        },
    }
    monkeypatch.setenv("KG_EXTRACT_PROVIDERS_JSON", json.dumps(profiles))
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


@pytest.mark.parametrize("options", [
    {"version": "v1"}, {"provider": "../private"}, {"base_url": "https://arbitrary"},
    {"api_key": "secret"}, {"request_timeout": 0}, {"document_timeout": float("inf")},
    {"request_timeout": True}, {"qps": 1000},
])
def test_api_rejects_invalid_or_sensitive_override(options):
    with pytest.raises(ValidationError):
        ExtractionOptions.model_validate(options)


def test_provider_errors_do_not_echo_profile_contents(providers, monkeypatch):
    providers["siliconflow"]["api_key"] = "secret-must-not-appear"
    monkeypatch.setenv("KG_EXTRACT_PROVIDERS_JSON", json.dumps(providers))
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


@pytest.mark.parametrize("version", ["v2", "v3", "latest", "v3.1"])
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
    monkeypatch.setattr(runtime, "_pools", {})
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
    monkeypatch.setattr(runtime, "_pools", {})
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


@pytest.mark.parametrize("version", ["v2", "v3", "latest", "v3.1"])
def test_live_client_configuration_uses_chat_only_without_network(providers, monkeypatch, version):
    import openai

    seen = []
    def create(**kwargs):
        seen.append(kwargs)
        return {"choices": [{"message": {"content": '{"entities": []}'}, "finish_reason": "stop"}]}
    def sdk(**kwargs):
        assert kwargs["api_key"] == "secret-must-not-appear"
        assert kwargs["base_url"] == "https://provider.example/v1"
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(openai, "OpenAI", sdk)
    monkeypatch.setattr(runtime, "_pools", {})
    llm = runtime.create_request_llm(config.resolve_config({"version": version, "provider": "siliconflow"}))
    result = llm.generate_structured({"type": "object"}, [{"role": "user", "content": "x"}])
    assert result.finish_reason == "stop"
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
