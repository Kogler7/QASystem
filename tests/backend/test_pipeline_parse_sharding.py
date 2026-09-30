"""KNOWLEDGE_PIPELINE_PARSE_SHARDING：沉淀流水线解析段的可选页分片编排。

开关默认关闭，此时解析必须保持既有整篇路径；开启后仅 PDF 在满足编排条件时
走 document_sharding.orchestrate_document_parse，其余情况安静回退整篇解析。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.execution.document_sharding import PageShardError
from backend.app.execution.manager import ExecutionCancelled
from backend.app.responses import ErrorCode
from backend.app.schemas import ParserConfig, PipelineOptions
from backend.app.services import knowledge_precipitation as module
from backend.app.services.algorithm_capabilities import GraphExtractionBatch
from backend.app.services.knowledge_precipitation import (
    KnowledgeIngestionRequest,
    KnowledgePrecipitationService,
    PipelineError,
)

_PDF_NAME = "scan.pdf"


class _WholeParsing:
    """整篇解析桩：记录调用，返回与整篇路径一致的结果形状。"""

    def __init__(self):
        self.calls: list[str] = []

    def parse(self, file_name, content, config, **_kwargs):
        self.calls.append(file_name)
        markdown = "# whole-document"
        return {
            "file_name": file_name,
            "file_size": len(content),
            "file_type": Path(file_name).suffix.lower().lstrip("."),
            "markdown": markdown,
            "content_length": len(markdown),
            "parser": {"preferred_engine": "vlm", "final_engine": "vlm"},
        }


class _CapturingExtraction:
    def __init__(self):
        self.items = []

    def extract(self, ontology, items, **_kwargs):
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


def _merged_shard_result(file_name: str) -> dict:
    """模拟 orchestrate_document_parse 的页分片合并输出（与整篇结果同形）。"""
    markdown = "# 第 1 页\n\n# 第 8 页"
    return {
        "file_name": file_name,
        "file_size": 256,
        "file_type": "pdf",
        "markdown": markdown,
        "content_length": len(markdown),
        "parser": {
            "preferred_engine": "vlm",
            "final_engine": "vlm",
            "sharding": {
                "enabled": True,
                "shard_count": 2,
                "page_ranges": [[0, 7], [8, 15]],
                "empty_shards": [],
            },
        },
        "sharding": {
            "enabled": True,
            "page_count": 16,
            "shard_count": 2,
            "page_ranges": [[0, 7], [8, 15]],
            "empty_shards": [],
        },
    }


@pytest.fixture
def orchestrate_spy(monkeypatch):
    """替换编排入口并记录调用（不触碰真实队列/Redis）。"""
    calls: list[dict] = []

    def _fake(payload):
        calls.append(payload)
        return _merged_shard_result(str(payload.get("file_name") or _PDF_NAME))

    monkeypatch.setattr(module, "orchestrate_document_parse", _fake)
    return calls


def _make_service(monkeypatch, tmp_path, *, parsing=None, extraction=None, async_manager=True, cancelled=False):
    monkeypatch.setattr(module, "EXTRACTION_SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(module, "get_application_service_url", lambda: "")
    monkeypatch.setattr(
        module,
        "get_execution_manager",
        lambda: SimpleNamespace(
            is_async=async_manager,
            is_cancel_requested=lambda _job_id: cancelled,
        ),
    )
    return KnowledgePrecipitationService(
        parsing=parsing or _WholeParsing(),
        extraction=extraction or _CapturingExtraction(),
    )


def test_sharding_disabled_keeps_whole_document_parse(monkeypatch, tmp_path, orchestrate_spy):
    monkeypatch.delenv("KNOWLEDGE_PIPELINE_PARSE_SHARDING", raising=False)
    whole = _WholeParsing()
    service = _make_service(monkeypatch, tmp_path, parsing=whole)

    parsed = service._parse_document(_PDF_NAME, b"%PDF-fake", PipelineOptions(), fingerprint="fp")

    assert whole.calls == [_PDF_NAME]
    assert orchestrate_spy == []
    assert parsed["markdown"] == "# whole-document"


def test_sharding_enabled_pdf_uses_orchestrate_with_compatible_shape(monkeypatch, tmp_path, orchestrate_spy):
    monkeypatch.setenv("KNOWLEDGE_PIPELINE_PARSE_SHARDING", "true")
    whole = _WholeParsing()
    service = _make_service(monkeypatch, tmp_path, parsing=whole)

    parsed = service._parse_document(
        _PDF_NAME, b"%PDF-fake", PipelineOptions(), fingerprint="fp", job_id="job-9"
    )

    assert whole.calls == []
    assert len(orchestrate_spy) == 1
    payload = orchestrate_spy[0]
    assert payload["file_name"] == _PDF_NAME
    assert payload["content"] == b"%PDF-fake"
    assert payload["config"]["page_sharding"] == "auto"
    assert payload["__execution"] == {"job_id": "job-9"}
    # 与整篇路径同形：process() 下游消费的键一个不缺，仅附加分片明细。
    assert parsed["markdown"] == "# 第 1 页\n\n# 第 8 页"
    assert parsed["content_length"] == len(parsed["markdown"])
    assert parsed["file_type"] == "pdf"
    assert parsed["parser"]["final_engine"] == "vlm"
    assert parsed["parser"]["sharding"]["shard_count"] == 2
    assert parsed["sharding"]["page_count"] == 16


def test_sharding_result_flows_through_pipeline(monkeypatch, tmp_path, orchestrate_spy):
    monkeypatch.setenv("KNOWLEDGE_PIPELINE_PARSE_SHARDING", "1")
    extraction = _CapturingExtraction()
    service = _make_service(monkeypatch, tmp_path, extraction=extraction)

    result = service.process(
        KnowledgeIngestionRequest(
            file_name=_PDF_NAME,
            content=b"%PDF-fake",
            ontology="@startuml\nclass Term\n@enduml",
            options=PipelineOptions(),
            job_id="job-9",
        )
    )

    assert orchestrate_spy and orchestrate_spy[0]["__execution"] == {"job_id": "job-9"}
    document = result["stats"]["document"]
    assert document["parser"]["sharding"]["shard_count"] == 2
    assert document["content_length"] == len("# 第 1 页\n\n# 第 8 页")
    assert document["file_type"] == "pdf"
    assert [item.text for item in extraction.items] == ["# 第 1 页\n\n# 第 8 页"]
    assert result["extraction"]["graph"] == {"nodes": [], "relationships": []}


def test_sharding_falls_back_for_non_pdf(monkeypatch, tmp_path, orchestrate_spy):
    monkeypatch.setenv("KNOWLEDGE_PIPELINE_PARSE_SHARDING", "true")
    whole = _WholeParsing()
    service = _make_service(monkeypatch, tmp_path, parsing=whole)

    parsed = service._parse_document("data.csv", b"a,b\n", PipelineOptions(), fingerprint="fp")

    assert whole.calls == ["data.csv"]
    assert orchestrate_spy == []
    assert parsed["markdown"] == "# whole-document"


def test_sharding_falls_back_when_page_sharding_explicitly_off(monkeypatch, tmp_path, orchestrate_spy):
    monkeypatch.setenv("KNOWLEDGE_PIPELINE_PARSE_SHARDING", "true")
    whole = _WholeParsing()
    service = _make_service(monkeypatch, tmp_path, parsing=whole)
    options = PipelineOptions(parser=ParserConfig(page_sharding="off"))

    parsed = service._parse_document(_PDF_NAME, b"%PDF-fake", options, fingerprint="fp")

    assert whole.calls == [_PDF_NAME]
    assert orchestrate_spy == []


def test_sharding_falls_back_in_inline_execution_mode(monkeypatch, tmp_path, orchestrate_spy):
    monkeypatch.setenv("KNOWLEDGE_PIPELINE_PARSE_SHARDING", "true")
    whole = _WholeParsing()
    service = _make_service(monkeypatch, tmp_path, parsing=whole, async_manager=False)

    parsed = service._parse_document(_PDF_NAME, b"%PDF-fake", PipelineOptions(), fingerprint="fp")

    assert whole.calls == [_PDF_NAME]
    assert orchestrate_spy == []
    assert parsed["markdown"] == "# whole-document"


def test_page_shard_error_maps_to_existing_parse_error_code(monkeypatch, tmp_path):
    monkeypatch.setenv("KNOWLEDGE_PIPELINE_PARSE_SHARDING", "true")

    def _boom(_payload):
        raise PageShardError(
            "一个或多个文档页段解析失败",
            failed=[{"child_job_id": "job-shard-1", "status": "failed"}],
            code=ErrorCode.DOCUMENT_PARSE_FAILED,
        )

    monkeypatch.setattr(module, "orchestrate_document_parse", _boom)
    service = _make_service(monkeypatch, tmp_path)

    with pytest.raises(PipelineError) as err:
        service.process(
            KnowledgeIngestionRequest(
                file_name=_PDF_NAME,
                content=b"%PDF-fake",
                ontology="class Term",
            )
        )

    assert err.value.code == ErrorCode.DOCUMENT_PARSE_FAILED
    assert err.value.stage == "document_parse"
    assert err.value.details["failed"][0]["child_job_id"] == "job-shard-1"


def test_parse_stage_boundary_aborts_cleanly_when_cancelled(monkeypatch, tmp_path, orchestrate_spy):
    monkeypatch.setenv("KNOWLEDGE_PIPELINE_PARSE_SHARDING", "true")
    whole = _WholeParsing()
    service = _make_service(monkeypatch, tmp_path, parsing=whole, cancelled=True)

    with pytest.raises(ExecutionCancelled):
        service.process(
            KnowledgeIngestionRequest(
                file_name=_PDF_NAME,
                content=b"%PDF-fake",
                ontology="class Term",
                job_id="job-cancel",
            )
        )

    assert whole.calls == []  # 解析未开始
    assert orchestrate_spy == []  # 编排未开始
    assert not list(tmp_path.rglob("*_error.log"))  # 协作式取消不写报错现场
