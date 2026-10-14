"""Backend opt-in contract using the real splitter and adaptive refiner."""
import importlib
import logging

import pytest

from algorithm.knowledge_pipeline.chunking import chunk_document_items
from backend.app.schemas import ChunkOptions
from backend.app.services.algorithm_capabilities import DocumentChunkingService


@pytest.fixture(autouse=True)
def actual_splitter(monkeypatch, tmp_path):
    # Other backend protocol tests may retain a stub in method globals.
    monkeypatch.setitem(DocumentChunkingService.chunk.__globals__,
                        "chunk_document_items", chunk_document_items)
    monkeypatch.setenv("CHUNK_ENTITY_LOG_PATH", str(tmp_path / "entity-count.jsonl"))


def assert_source_coverage(source, items):
    seen = [False] * len(source)
    for item in items:
        start, end = item["range"]
        assert item["text"] == source[start:end]
        seen[start:end] = [True] * (end - start)
    assert all(seen)


def test_default_false_keeps_baseline_without_model_call(monkeypatch):
    estimator = importlib.import_module("algorithm.knowledge_pipeline.entity_count_estimator")
    monkeypatch.setattr(estimator, "estimate_entities", lambda _: pytest.fail("disabled model was called"))
    source = "甲乙丙丁" * 100
    options = ChunkOptions(strategy="symbols", size=800, chunk_overlap=10)
    assert options.adaptive_entity_count is False
    result = DocumentChunkingService().chunk(source, options)
    baseline = chunk_document_items(source, chunk_type=1, chunk_size=800, chunk_overlap=10)
    assert result == {"count": len(baseline), "items": baseline}


@pytest.mark.parametrize("bucket,parts", [("le20", 1), ("21to40", 2), ("gt40", 3)])
def test_enabled_refines_actual_short_document(monkeypatch, caplog, bucket, parts):
    estimator = importlib.import_module("algorithm.knowledge_pipeline.entity_count_estimator")
    calls = []
    source = "甲乙丙丁" * 100
    def estimate(text, *, model, service=None):
        assert model == 'clef-flash'
        calls.append(text)
        return {"bucket": bucket, "confidence": 0.99}
    monkeypatch.setattr(estimator, "estimate_entities", estimate)
    options = ChunkOptions(strategy="symbols", size=800, chunk_overlap=10,
                           adaptive_entity_count=True)
    with caplog.at_level(logging.INFO):
        result = DocumentChunkingService().chunk(source, options)
    assert calls == [source]
    assert result["count"] == parts
    assert_source_coverage(source, result["items"])
    assert bucket in caplog.text
    assert all(set(item) == {"text", "range"} for item in result["items"])
    if parts > 1:
        assert all(left["range"][1] > right["range"][0]
                   for left, right in zip(result["items"], result["items"][1:]))


def test_request_model_is_forwarded_without_service_configuration(monkeypatch):
    estimator = importlib.import_module("algorithm.knowledge_pipeline.entity_count_estimator")
    calls = []
    def estimate(text, *, model, service=None):
        calls.append(model)
        return {"bucket": "le20"}
    monkeypatch.setattr(estimator, "estimate_entities", estimate)
    DocumentChunkingService().chunk("正文", ChunkOptions(
        adaptive_entity_count=True, entity_count_model="nanojev"))
    assert calls == ["nanojev"]
    with pytest.raises(ValueError):
        ChunkOptions(entity_count_model="unknown")


def test_estimator_failure_preserves_baseline_and_logs_fallback(monkeypatch, caplog):
    estimator = importlib.import_module("algorithm.knowledge_pipeline.entity_count_estimator")
    def unavailable(_, *, model, service=None):
        raise RuntimeError("endpoint not configured")
    monkeypatch.setattr(estimator, "estimate_entities", unavailable)
    source = "短文需要切片。" * 30
    options = ChunkOptions(strategy="symbols", size=800, chunk_overlap=10,
                           adaptive_entity_count=True)
    with caplog.at_level(logging.INFO):
        result = DocumentChunkingService().chunk(source, options)
    assert result["items"] == chunk_document_items(source, chunk_type=1,
                                                   chunk_size=800, chunk_overlap=10)
    assert "fallback" in caplog.text.lower() or "回退" in caplog.text


def test_model_estimation_scoped_to_each_initial_chunk(monkeypatch):
    estimator = importlib.import_module("algorithm.knowledge_pipeline.entity_count_estimator")
    source = "甲" * 160 + "乙" * 160
    options = ChunkOptions(strategy="symbols", size=160, chunk_overlap=0,
                           adaptive_entity_count=True)
    calls = []
    def estimate(text, *, model, service=None):
        calls.append((text, model))
        return {"bucket": "21to40", "confidence": 0.99}
    monkeypatch.setattr(estimator, "estimate_entities", estimate)
    result = DocumentChunkingService().chunk(source, options)
    assert calls == [("甲" * 160, 'clef-flash'), ("乙" * 160, 'clef-flash')]
    assert result["count"] == 4
    assert all(set(item["text"]) in ({"甲"}, {"乙"}) for item in result["items"])
    assert_source_coverage(source, result["items"])
