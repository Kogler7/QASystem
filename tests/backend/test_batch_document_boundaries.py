"""Regression coverage for the batch ingestion document boundary contract."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.app.schemas import PipelineOptions, ChunkOptions
from backend.app.services import knowledge_precipitation as module
from backend.app.services.algorithm_capabilities import GraphExtractionBatch
from backend.app.services.knowledge_precipitation import (
    BatchKnowledgeIngestionRequest,
    FileItem,
    KnowledgePrecipitationService,
)


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


@pytest.mark.parametrize("threshold", [5, 12000])
def test_batch_chunks_keep_boundaries_ranges_and_duplicate_names(monkeypatch, tmp_path, threshold):
    monkeypatch.setattr(module, "EXTRACTION_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(module, "get_application_service_url", lambda: "")

    extraction = _CapturingExtraction()
    service = KnowledgePrecipitationService(
        processor_factory=_Parser,
        extraction=extraction,
    )
    # Exercise both actual splitting and the short-document threshold bypass.
    options = PipelineOptions(
        chunking=ChunkOptions(size=10, chunk_overlap=0, chunk_threshold=threshold),
    )
    request = BatchKnowledgeIngestionRequest(
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

    # Every chunk is wholly contained in one source segment, including when the
    # segment must be split because its length exceeds chunk_threshold.
    ranges_by_id = {}
    for item in extraction.items:
        ranges_by_id.setdefault(item.metadata["document_id"], []).append(item.ranges)
    for document_id, ranges in ranges_by_id.items():
        if threshold == 5:
            assert len(ranges) >= 2, document_id
        else:
            assert len(ranges) == 1, document_id
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
