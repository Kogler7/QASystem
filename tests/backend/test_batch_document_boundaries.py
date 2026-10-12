"""Regression coverage for the batch ingestion document boundary contract."""

from __future__ import annotations

import os
import importlib
from pathlib import Path

import pytest

from backend.app.schemas import PipelineOptions, ChunkOptions
from backend.app.services.algorithm_capabilities import GraphExtractionBatch
from backend.app.services.knowledge_precipitation import FileItem
from algorithm.knowledge_pipeline.chunking import chunk_document_items as real_chunk_document_items


@pytest.fixture(autouse=True)
def _real_chunking_contract(monkeypatch):
    """Protocol's session fixture stubs the pipeline; these test real splitting.

    Its reimported DocumentChunkingService can retain the stub while the full
    suite runs. Bind the actual production implementation for these tests only,
    preserving protocol isolation and restoring its binding on teardown.
    """
    for name in ['backend.app.services.knowledge_precipitation','algorithm.knowledge_precipitation']:
        service_module=importlib.import_module(name)
        monkeypatch.setitem(service_module.DocumentChunkingService.chunk.__globals__,
                            'chunk_document_items',real_chunk_document_items)


class _Parser:
    def process_file(self, file_path, **_kwargs):
        markdown = Path(file_path).read_bytes().decode("utf-8")
        return {
            "success": True,
            "content": markdown,
            "markdown": markdown,
            "content_length": len(markdown),
            "file_type": Path(file_path).suffix.lstrip("."),
            "metadata": {"sheets": [], "model_used": "text"},
        }


class _CapturingExtraction:
    def __init__(self):
        self.items = []

    def extract(self, ontology, items, **_kwargs):
        assert ontology
        self.items = list(items)
        rows = [
            {
                "index": index,
                "text": item.text,
                "ranges": item.ranges,
                "metadata": item.metadata,
                "warnings": [],
                "profile": {},
                "graph": {"nodes": [], "relationships": []},
            }
            for index, item in enumerate(self.items)
        ]
        return GraphExtractionBatch(
            items=rows,
            attempts=[],
            scheduler={},
            graph={"nodes": [], "relationships": []},
            quality={"status": "succeeded", "graph_publishable": True},
        )


@pytest.mark.parametrize("module_name", ['backend.app.services.knowledge_precipitation','algorithm.knowledge_precipitation'])
@pytest.mark.parametrize("strategy", ['symbols','recursive'])
def test_batch_chunks_keep_boundaries_ranges_and_duplicate_names(monkeypatch, tmp_path, module_name, strategy):
    module=importlib.import_module(module_name)
    monkeypatch.setattr(module, "EXTRACTION_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(module, "get_application_service_url", lambda: "")

    extraction = _CapturingExtraction()
    service = module.KnowledgePrecipitationService(
        processor_factory=_Parser,
        extraction=extraction,
    )
    # Every source is below the old 12000-char bypass but must still be split.
    options = PipelineOptions(
        chunking=ChunkOptions(strategy=strategy,size=10, chunk_overlap=0),
    )
    request = module.BatchKnowledgeIngestionRequest(
        files=[
            FileItem("same.md", b"A" * 30),
            FileItem("same.md", b"B" * 40),
            FileItem("later.md", b"C" * 25),
        ],
        ontology="@startuml\nclass Thing\n@enduml",
        options=options,
    )

    result = service.process_batch(request)
    combined = next(
        (tmp_path / name / "01-parse.md")
        for name in os.listdir(tmp_path)
        if (tmp_path / name / "01-parse.md").exists()
    ).read_text(encoding="utf-8")

    assert {item.metadata["document_id"] for item in extraction.items} == {
        "batch-1:same.md",
        "batch-2:same.md",
        "batch-3:later.md",
    }
    assert all(start < end <= len(combined) for start, end in (item.ranges for item in extraction.items))
    assert all(combined[start:end] == item.text for item in extraction.items for start, end in [item.ranges])

    # Every chunk is wholly contained in one source segment, including short
    # documents that exceed only the configured chunk size.
    ranges_by_id = {}
    for item in extraction.items:
        ranges_by_id.setdefault(item.metadata["document_id"], []).append(item.ranges)
    for document_id, ranges in ranges_by_id.items():
        assert len(ranges) >= 2, document_id
    if strategy=='symbols':
        assert all(len(item.text)<=10 for item in extraction.items)
    # Distinct names retain the service's filename ordering; duplicate names
    # retain their original input order as the tie-breaker.
    ordered_ids = ["batch-3:later.md", "batch-1:same.md", "batch-2:same.md"]
    offset = 0
    for document_id, name, text in [
        ("batch-3:later.md", "later.md", "C" * 25),
        ("batch-1:same.md", "same.md", "A" * 30),
        ("batch-2:same.md", "same.md", "B" * 40),
    ]:
        segment = f"\n## 文件: {name}\n\n{text}"
        assert combined[offset:offset + len(segment)] == segment
        assert all(offset <= start < end <= offset + len(segment) for start, end in ranges_by_id[document_id])
        offset += len(segment) + 1
    for left_id, right_id in zip(ordered_ids, ordered_ids[1:]):
        assert max(end for _start, end in ranges_by_id[left_id]) < min(
            start for start, _end in ranges_by_id[right_id]
        )

    documents = result["stats"]["documents"]
    sizes = {(row["file_name"], row["file_size"]) for row in documents}
    assert ("same.md", 30) in sizes
    assert ("same.md", 40) in sizes
    assert 'threshold' not in result['stats']['chunking']


@pytest.mark.parametrize("module_name", ['backend.app.services.knowledge_precipitation','algorithm.knowledge_precipitation'])
@pytest.mark.parametrize("length", [8,45])
def test_single_short_document_always_calls_splitter_and_obeys_size(monkeypatch,tmp_path,module_name,length):
    module=importlib.import_module(module_name)
    monkeypatch.setattr(module,'EXTRACTION_SNAPSHOT_DIR',tmp_path)
    monkeypatch.setattr(module,'get_application_service_url',lambda:'')
    extraction=_CapturingExtraction()
    service=module.KnowledgePrecipitationService(processor_factory=_Parser,extraction=extraction)
    actual_chunk=service.chunking.chunk
    calls=[]
    def capture(markdown,options):
        calls.append(markdown)
        return actual_chunk(markdown,options)
    monkeypatch.setattr(service.chunking,'chunk',capture)
    text=''.join(chr(0x4e00+i) for i in range(length))
    result=service.process(module.KnowledgeIngestionRequest(
        file_name='short.md',content=text.encode('utf8'),ontology='@startuml\nclass Thing\n@enduml',
        options=PipelineOptions(chunking=ChunkOptions(strategy='symbols',size=10,chunk_overlap=0))))
    assert calls==[text]
    assert len(extraction.items)==(length+9)//10
    assert all(len(item.text)<=10 for item in extraction.items)
    assert ''.join(item.text for item in extraction.items)==text
    assert all(text[a:b]==item.text for item in extraction.items for a,b in [item.ranges])
    assert 'threshold' not in result['stats']['chunking']


def test_removed_threshold_is_rejected_in_strict_chunk_options():
    from pydantic import ValidationError
    assert 'chunk_threshold' not in ChunkOptions.model_fields
    with pytest.raises(ValidationError,match='Extra inputs are not permitted'):
        ChunkOptions(chunk_threshold=12000)
