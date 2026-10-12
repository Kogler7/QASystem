from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from algorithm.knowledge_extract.version import LATEST, get_extract_version, get_extract_version_label
from backend.app.schemas import TextChunk
from backend.app.services import algorithm_capabilities as capabilities
from backend.app.services import knowledge_extract_adapter as adapter


def test_embedded_attachment_projection_keeps_original_chunk_and_file_reference():
    identity = '{"document_id":"doc","source_file":"通知.docx"}'
    uid = adapter._uid_for("OperationalInformation", identity)
    refs = [{"document_id": "doc", "source_file": "通知.docx"}]
    result = {
        "status": "succeeded",
        "graph": {"nodes": [{"uid": uid, "label": "OperationalInformation",
                              "properties": {"信息类型": "附件"}, "source_files": refs}],
                  "relationships": []},
        "intermediate_model": {"objects": [{
            "id": "attachment", "type": "OperationalInformation", "identity": identity,
            "evidence": [{"source_block_ids": ["chunk-0001::attachment"]}],
        }]},
        "profile": {"chunk_source_ids": {"chunk-0001::attachment": "chunk-0001"}},
    }
    items = adapter.project_items(result, texts=["正文", "# 附件1\n原文"])
    assert not items[0]["graph"]["nodes"]
    node = items[1]["graph"]["nodes"][0]
    assert node["chunk_indices"] == [1]
    assert node["source_files"] == refs
    assert capabilities.deduplicate_chunk_graphs(items)["nodes"][0]["source_files"] == refs


def test_extract_chunks_preserves_structured_error_code():
    """V2 结构化错误（ONTOLOGY_EMPTY 等）不被泛化成 EXTRACTION_EXCEPTION。"""

    class StructuredError(RuntimeError):
        error_code = "ONTOLOGY_EMPTY"

    def failing_runner(**_kwargs):
        raise StructuredError("本体未解析出任何 class 声明")

    items = adapter.extract_chunks("", ["第一段", "第二段"], runner=failing_runner)

    assert [item["error_code"] for item in items] == ["ONTOLOGY_EMPTY", "ONTOLOGY_EMPTY"]
    assert all(item["error"] == "本体未解析出任何 class 声明" for item in items)
    assert all(item["graph"] == {"nodes": [], "relationships": []} for item in items)


def test_extract_chunks_plain_exception_stays_generic():
    def failing_runner(**_kwargs):
        raise RuntimeError("boom")

    items = adapter.extract_chunks("", ["第一段"], runner=failing_runner)

    assert items[0]["error_code"] == "EXTRACTION_EXCEPTION"
    assert items[0]["error"] == "boom"


def test_build_chunks_carries_ranges_and_spreadsheet_provenance():
    text = (
        '## 故障数据\n\n```json\n'
        '{"columns":["编号","现象"],"rows":[{"编号":"1","现象":"异响"}]}\n'
        '```'
    )
    supplied = {"source_file": "faults.xlsx", "source_type": "xlsx"}

    chunk = adapter.build_chunks([text], [[10, 90]], [supplied])[0]

    assert chunk.start_offset == 10 and chunk.end_offset == 90
    assert chunk.document_id == "faults.xlsx"
    assert chunk.section_title == "故障数据"
    assert chunk.metadata["sheet_name"] == "故障数据"
    assert chunk.metadata["source_row_start"] == 2
    assert chunk.metadata["source_row_end"] == 2
    assert chunk.metadata["tables"][0]["columns"] == ["编号", "现象"]
    assert supplied == {"source_file": "faults.xlsx", "source_type": "xlsx"}


def test_build_chunks_recovers_spreadsheet_provenance_from_batch_header():
    text = (
        '## 文件: batch.xlsx\n\n## Sheet-2\n\n```json\n'
        '{"columns":["id"],"rows":[{"id":"A"},{"id":"B"}]}\n```'
    )

    chunk = adapter.build_chunks(
        [text],
        metadata=[{"document_id": "batch-run", "source_type": "batch"}],
    )[0]

    assert chunk.document_id == "batch-run"
    assert chunk.metadata["source_file"] == "batch.xlsx"
    assert chunk.metadata["sheet_name"] == "Sheet-2"
    assert chunk.metadata["source_row_start"] == 2
    assert chunk.metadata["source_row_end"] == 3


def test_build_chunks_keeps_batch_spreadsheet_provenance_after_file_header_chunk():
    text = '## Sheet-2\n\n```json\n{"columns":["编号"],"rows":[{"编号":"00123"}]}\n```'
    chunk = adapter.build_chunks([text], metadata=[{
        "document_id": "batch-2:table.xlsx", "source_file": "table.xlsx",
        "source_type": "batch", "source_file_type": "xlsx", "signal_source": "parser",
    }])[0]
    assert chunk.metadata["sheet_name"] == "Sheet-2"
    assert chunk.metadata["source_row_start"] == 2
    assert chunk.metadata["source_row_end"] == 2
    assert chunk.metadata["source_file"] == "table.xlsx"


def test_run_aggregate_feeds_field_dicts_to_v3_and_latest(monkeypatch):
    """v3/latest 入口只认各自包内的 ExtractionChunk 或字段 dict，适配器给字段 dict。"""
    from algorithm.knowledge_extract import latest as latest_pkg
    from algorithm.knowledge_extract import v3 as v3_pkg

    captured = {}

    def fake_runner(**kwargs):
        captured.update(kwargs)
        return {"status": "succeeded"}

    for label, pkg in (("v3", v3_pkg), ("latest", latest_pkg)):
        monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", label)
        captured.clear()
        adapter.run_aggregate("class Term", ["第一段内容"], runner=fake_runner)
        chunks = captured["chunks"]
        assert chunks and all(isinstance(chunk, dict) for chunk in chunks)
        for chunk in chunks:
            resolved = pkg.ExtractionChunk.from_dict(chunk)
            assert resolved.chunk_id == chunk["chunk_id"]
            assert resolved.text == chunk["text"]


@pytest.mark.parametrize("label", ["v2", "v2.2", "v3.1"])
def test_run_aggregate_feeds_version_specific_chunk_objects(monkeypatch, label):
    """各版本接收各自包的 ExtractionChunk，保留来源元数据。"""
    from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk as V2Chunk
    from algorithm.knowledge_extract.v2_2.chunk_context import ExtractionChunk as V22Chunk

    from algorithm.knowledge_extract.v3_1.chunk_context import ExtractionChunk as V31Chunk
    expected_type = {"v2": V2Chunk, "v2.2": V22Chunk, "v3.1": V31Chunk}[label]

    captured = {}

    def fake_runner(**kwargs):
        captured.update(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", label)
    adapter.run_aggregate(
        "class Term", ["第一段内容"], ranges=[[10, 15]],
        metadata=[{"document_id": "doc-1", "source_file": "source.md"}],
        runner=fake_runner,
    )
    assert captured["chunks"] and all(type(chunk) is expected_type for chunk in captured["chunks"])
    chunk = captured["chunks"][0]
    assert chunk.start_offset == 10 and chunk.end_offset == 15
    assert chunk.document_id == "doc-1"
    assert chunk.metadata["source_file"] == "source.md"


def test_v31_input_preserves_structural_fields_and_caller_metadata(monkeypatch):
    from copy import deepcopy

    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v3.1")
    text = "  # 第一章\n设备甲\n"
    metadata = {
        "document_id": "doc-1", "section_id": "section-1",
        "section_title": "第一章", "sheet_name": "sheet fallback",
        "clause_number": "1.2", "page_no": 3,
        "start_offset": 100, "end_offset": 120,
        "custom": {"labels": ["原始标记"]},
    }
    original = deepcopy(metadata)

    def runner(**kwargs):
        chunk = kwargs["chunks"][0]
        assert chunk.text == text
        assert chunk.document_id == "doc-1"
        assert chunk.section_id == "section-1"
        assert chunk.section_title == "第一章"
        assert chunk.clause_number == "1.2"
        assert chunk.page_no == 3
        assert (chunk.start_offset, chunk.end_offset) == (100, 120)
        chunk.metadata["custom"]["labels"].append("内部标记")
        return {"status": "succeeded"}

    adapter.run_aggregate("class Term", [text], metadata=[metadata], runner=runner)
    assert metadata == original


def test_v31_input_ranges_override_metadata_offsets(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v3.1")

    def runner(**kwargs):
        chunk = kwargs["chunks"][0]
        assert (chunk.start_offset, chunk.end_offset) == (0, 4)
        return {"status": "succeeded"}

    adapter.run_aggregate(
        "class Term", ["设备甲。"], ranges=[[0, 4]],
        metadata=[{"start_offset": 10, "end_offset": 14}], runner=runner,
    )


def test_graph_service_forwards_ranges_and_metadata(monkeypatch):
    captured = {}

    def fake_extract_chunks(**kwargs):
        captured.update(kwargs)
        return [
            {
                "index": 0,
                "text": kwargs["chunks"][0],
                "ranges": kwargs["ranges"][0],
                "metadata": kwargs["metadata"][0],
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "attempts": [],
                "warnings": [],
                "profile": {},
                "error": None,
                "error_code": None,
                "raw_response": "",
            }
        ]

    monkeypatch.setattr(capabilities, "extract_chunks", fake_extract_chunks)
    item = TextChunk(
        text="内容",
        ranges=[4, 6],
        metadata={"source_file": "sample.xlsx", "source_type": "xlsx"},
    )

    result = capabilities.GraphExtractionService().extract("ontology", [item])

    assert captured["ranges"] == [[4, 6]]
    assert captured["metadata"] == [{"source_file": "sample.xlsx", "source_type": "xlsx"}]
    assert result.items[0]["ranges"] == [4, 6]
    assert result.items[0]["metadata"]["source_file"] == "sample.xlsx"


def test_partial_verification_quality_reaches_public_batch_contract(monkeypatch):
    # 固定 v2，验证 V2 部分失败场景下的 quality 契约不受 latest 默认影响
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v2")

    def partial_runner(**_kwargs):
        return {
            "status": "partial",
            "error_code": "LLM_SCHEMA_INVALID",
            "verification": {
                "valid": False,
                "hard_issue_count": 1,
                "repairable_issue_count": 0,
                "issues": [],
                "coverage_gaps": [],
            },
            "profile": {"repair_stop_reason": "hard_gap_not_repairable"},
            "graph": {"nodes": [], "relationships": []},
            "raw": {"entities": [], "relations": []},
            "intermediate_model": {"objects": [], "relations": []},
            "attempts": [
                {
                    "attempt": 1,
                    "type": "schema_validation",
                    "pass": "verification",
                    "chunk": "final",
                    "message": "invalid final graph",
                    "terminal": True,
                }
            ],
            "warnings": [],
        }

    monkeypatch.setattr(
        capabilities,
        "extract_chunks",
        lambda **kwargs: adapter.extract_chunks(
            kwargs["ontology"],
            kwargs["chunks"],
            ranges=kwargs.get("ranges"),
            metadata=kwargs.get("metadata"),
            runner=partial_runner,
        ),
    )

    batch = capabilities.GraphExtractionService().extract(
        "class Term",
        [TextChunk(text="内容", ranges=[0, 2], metadata={})],
    )

    assert batch.items[0]["error"] is None
    assert batch.quality == {
        "status": "partial",
        "error_code": "LLM_SCHEMA_INVALID",
        "extract_version": "v2",
        "verification": {
            "valid": False,
            "hard_issue_count": 1,
            "repairable_issue_count": 0,
            "issues": [],
            "coverage_gaps": [],
        },
        "graph_publishable": False,
        "repair_stop_reason": "hard_gap_not_repairable",
    }
    assert batch.pipeline_data()["quality"] == batch.quality


def test_degraded_chunk_failure_is_explicit_in_batch_quality(monkeypatch):
    monkeypatch.delenv("KG_EXTRACT_STRICT_CHUNK_FAILURES", raising=False)
    monkeypatch.setattr(
        capabilities,
        "extract_chunks",
        lambda **_kwargs: [
            {
                "index": 0,
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "attempts": [],
                "error": None,
                "error_code": None,
                "quality": {"status": "succeeded", "verification": {}},
            },
            {
                "index": 1,
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "attempts": [],
                "error": "请求失败",
                "error_code": "LLM_REQUEST_FAILED",
                "quality": {"status": "failed", "verification": {}},
            },
        ],
    )

    batch = capabilities.GraphExtractionService().extract(
        "class Term",
        [TextChunk(text="ok"), TextChunk(text="failed")],
    )

    assert batch.quality["status"] == "partial"
    assert batch.quality["error_code"] == "PARTIAL_EXTRACTION_FAILED"
    assert batch.quality["failed_chunk_count"] == 1


def _invalid_verification_runner(**_kwargs):
    """verification 有硬错误但 LLM 实际抽出了节点的聚合结果（best-effort 素材）。"""
    return {
        "status": "partial",
        "error_code": "LLM_SCHEMA_INVALID",
        "verification": {"valid": False, "hard_issue_count": 1},
        "profile": {},
        "graph": {"nodes": [{"uid": "Term::a"}], "relationships": []},
        "raw": {"entities": [], "relations": []},
        "intermediate_model": {"objects": [], "relations": []},
        "attempts": [],
        "warnings": [],
    }


def _extract_with_runner(monkeypatch, runner):
    monkeypatch.setattr(
        capabilities,
        "extract_chunks",
        lambda **kwargs: adapter.extract_chunks(
            kwargs["ontology"], kwargs["chunks"], runner=runner
        ),
    )
    return capabilities.GraphExtractionService().extract(
        "class Term", [TextChunk(text="内容")]
    )


def test_terminal_verification_publishes_best_effort_graph_with_quality_gate(monkeypatch):
    """新默认：verification 未过仍发布 best-effort 图谱，graph_publishable 如实为 False。"""
    monkeypatch.delenv("KG_EXTRACT_PUBLISH_BEST_EFFORT", raising=False)
    batch = _extract_with_runner(monkeypatch, _invalid_verification_runner)

    payload = batch.graph_data()
    assert payload["graph_publishable"] is False
    assert [node["uid"] for node in payload["graph"]["nodes"]] == ["Term::a"]
    # isolated_graph 契约保留：未过校验的图谱同步出现在隔离字段里
    assert [node["uid"] for node in payload["isolated_graph"]["nodes"]] == ["Term::a"]
    assert payload["quality"]["status"] == "partial"
    assert payload["quality"]["error_code"] == "LLM_SCHEMA_INVALID"
    assert payload["quality"]["verification"]["valid"] is False


def test_terminal_verification_clears_graph_when_best_effort_disabled(monkeypatch):
    """KG_EXTRACT_PUBLISH_BEST_EFFORT=0 恢复旧行为：graph 置空，图谱只在 isolated_graph。"""
    monkeypatch.setenv("KG_EXTRACT_PUBLISH_BEST_EFFORT", "0")
    batch = _extract_with_runner(monkeypatch, _invalid_verification_runner)

    payload = batch.graph_data()
    assert payload["graph_publishable"] is False
    assert payload["graph"] == {"nodes": [], "relationships": []}
    assert [node["uid"] for node in payload["isolated_graph"]["nodes"]] == ["Term::a"]
    assert payload["quality"]["verification"]["valid"] is False


def test_verified_graph_publishes_normally_regardless_of_best_effort_flag(monkeypatch):
    """验证通过路径不受开关影响：graph 正常发布，不出现 isolated_graph。"""
    def valid_runner(**_kwargs):
        return {
            "status": "succeeded",
            "verification": {"valid": True},
            "profile": {},
            "graph": {"nodes": [{"uid": "Term::a"}], "relationships": []},
            "raw": {"entities": [], "relations": []},
            "intermediate_model": {"objects": [], "relations": []},
            "attempts": [],
            "warnings": [],
        }

    for flag in ("0", "1"):
        monkeypatch.setenv("KG_EXTRACT_PUBLISH_BEST_EFFORT", flag)
        batch = _extract_with_runner(monkeypatch, valid_runner)

        payload = batch.graph_data()
        assert payload["graph_publishable"] is True
        assert [node["uid"] for node in payload["graph"]["nodes"]] == ["Term::a"]
        assert "isolated_graph" not in payload


def test_run_single_document_exposes_quality_and_verification_on_failure(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "run_aggregate",
        lambda *_args, **_kwargs: {
            "status": "partial",
            "error_code": "LLM_SCHEMA_INVALID",
            "verification": {"valid": False, "hard_issue_count": 1},
            "profile": {"repair_stop_reason": "repair_no_new_facts"},
            "attempts": [],
            "warnings": [],
        },
    )

    result = adapter.run_single_document("class Term", "内容")

    assert result["error_code"] == "LLM_SCHEMA_INVALID"
    assert result["quality"]["graph_publishable"] is False
    assert result["quality"]["verification"]["valid"] is False
    assert result["verification"] == result["quality"]["verification"]


def test_run_single_document_publishes_best_effort_graph_on_verification_failure(monkeypatch):
    """MCP 工具路径与 HTTP 一致：verification 未过返回 best-effort 图谱而非工具错误。"""
    monkeypatch.delenv("KG_EXTRACT_PUBLISH_BEST_EFFORT", raising=False)
    monkeypatch.setattr(
        adapter,
        "run_aggregate",
        lambda *_args, **_kwargs: {
            "status": "partial",
            "error_code": "LLM_SCHEMA_INVALID",
            "verification": {"valid": False, "hard_issue_count": 1},
            "profile": {},
            "graph": {"nodes": [{"uid": "Term::a"}], "relationships": []},
            "attempts": [],
            "warnings": [],
        },
    )

    result = adapter.run_single_document("class Term", "内容")

    assert "_error" not in result
    assert [node["uid"] for node in result["graph"]["nodes"]] == ["Term::a"]
    assert result["status"] == "partial"
    assert result["error_code"] == "LLM_SCHEMA_INVALID"
    assert result["quality"]["graph_publishable"] is False
    assert result["quality"]["verification"]["valid"] is False


def test_run_single_document_keeps_error_shape_when_best_effort_disabled(monkeypatch):
    """KG_EXTRACT_PUBLISH_BEST_EFFORT=0 时 MCP 路径恢复旧的 _error 形态。"""
    monkeypatch.setenv("KG_EXTRACT_PUBLISH_BEST_EFFORT", "0")
    monkeypatch.setattr(
        adapter,
        "run_aggregate",
        lambda *_args, **_kwargs: {
            "status": "partial",
            "error_code": "LLM_SCHEMA_INVALID",
            "verification": {"valid": False, "hard_issue_count": 1},
            "profile": {},
            "graph": {"nodes": [{"uid": "Term::a"}], "relationships": []},
            "attempts": [],
            "warnings": [],
        },
    )

    result = adapter.run_single_document("class Term", "内容")

    assert result["_error"] == "知识抽取失败"
    assert result["error_code"] == "LLM_SCHEMA_INVALID"
    assert "graph" not in result
    assert result["quality"]["graph_publishable"] is False


def test_projected_cross_chunk_relationships_are_closed_and_keep_source_provenance():
    result = {
        "status": "succeeded",
        "graph": {
            "nodes": [
                {"uid": "Term::a", "label": "Term", "properties": {"name": "A"}},
                {"uid": "Term::b", "label": "Term", "properties": {"name": "B"}},
            ],
            "relationships": [
                {"from_uid": "Term::a", "to_uid": "Term::b", "type": "RELATED"}
            ],
        },
        "raw": {"entities": [], "relations": []},
        "intermediate_model": {
            "objects": [
                {
                    "id": "a",
                    "type": "Term",
                    "identity": "A",
                    "evidence": [{"source_block_ids": ["chunk-0000"]}],
                },
                {
                    "id": "b",
                    "type": "Term",
                    "identity": "B",
                    "evidence": [{"source_block_ids": ["chunk-0001"]}],
                },
            ],
            "relations": [],
        },
        "attempts": [],
        "warnings": [],
        "profile": {},
        "verification": {"valid": True},
    }

    items = adapter.project_items(result, texts=["A", "B"])

    for item in items:
        node_uids = {node["uid"] for node in item["graph"]["nodes"]}
        assert node_uids == {"Term::a", "Term::b"}
        assert all(
            relation["from_uid"] in node_uids and relation["to_uid"] in node_uids
            for relation in item["graph"]["relationships"]
        )

    merged = capabilities.deduplicate_chunk_graphs(items)
    source_chunks = {node["uid"]: node["chunk_indices"] for node in merged["nodes"]}
    assert source_chunks == {"Term::a": [0], "Term::b": [1]}
    assert all(
        relation["provenance_status"] == "inferred_from_endpoints"
        and relation["chunk_indices"] == []
        for item in items
        for relation in item["graph"]["relationships"]
    )


def test_relationship_evidence_controls_projection_and_closure_context():
    result = {
        "status": "succeeded",
        "graph": {
            "nodes": [
                {"uid": "Term::a", "label": "Term", "properties": {"name": "A"}},
                {"uid": "Term::b", "label": "Term", "properties": {"name": "B"}},
            ],
            "relationships": [
                {
                    "from_uid": "Term::a",
                    "to_uid": "Term::b",
                    "type": "RELATED",
                    "evidences": [
                        {
                            "source_block_ids": ["chunk-0002"],
                            "quote": "A 与 B 相关",
                        }
                    ],
                }
            ],
        },
        "raw": {"entities": [], "relations": []},
        "intermediate_model": {
            "objects": [
                {
                    "id": "a",
                    "type": "Term",
                    "identity": "A",
                    "evidence": [{"source_block_ids": ["chunk-0000"]}],
                },
                {
                    "id": "b",
                    "type": "Term",
                    "identity": "B",
                    "evidence": [{"source_block_ids": ["chunk-0001"]}],
                },
            ],
            "relations": [],
        },
        "attempts": [],
        "warnings": [],
        "profile": {},
        "verification": {"valid": True},
    }

    items = adapter.project_items(result, texts=["A", "B", "A 与 B 相关"])

    assert [len(item["graph"]["relationships"]) for item in items] == [0, 0, 1]
    relation = items[2]["graph"]["relationships"][0]
    assert relation["chunk_indices"] == [2]
    assert relation["provenance_status"] == "evidence"
    assert {node["uid"] for node in items[2]["graph"]["nodes"]} == {
        "Term::a",
        "Term::b",
    }


def test_document_raw_repeated_per_chunk_does_not_mask_later_relation_provenance():
    evidence = {"source_block_ids": ["chunk-0001"], "quote": "甲联系乙"}
    result = {
        "status": "succeeded",
        "graph": {
            "nodes": [
                {"uid": "Term::甲", "label": "Term", "properties": {"name": "甲"}},
                {"uid": "Term::乙", "label": "Term", "properties": {"name": "乙"}},
            ],
            "relationships": [{
                "from_uid": "Term::甲", "to_uid": "Term::乙", "type": "RELATED",
                "evidences": [evidence],
            }],
        },
        "raw": {"entities": [], "relations": [{
            "from_type": "Term", "from_name": "甲", "rel_type": "RELATED",
            "to_type": "Term", "to_name": "乙", "evidence": evidence,
        }]},
        "intermediate_model": {"objects": [
            {"id": "a", "type": "Term", "identity": "甲",
             "evidence": [{"source_block_ids": ["chunk-0000"]}]},
            {"id": "b", "type": "Term", "identity": "乙",
             "evidence": [{"source_block_ids": ["chunk-0001"]}]},
        ], "relations": []},
        "verification": {"valid": True},
    }
    original_result = deepcopy(result)

    items = adapter.project_items(result, texts=["甲", "甲联系乙"])

    assert all(item["raw"] == result["raw"] for item in items)
    assert [len(item["graph"]["relationships"]) for item in items] == [0, 1]
    before_merge = deepcopy(items)
    merged = capabilities.deduplicate_chunk_graphs(items)

    assert len(merged["relationships"]) == 1
    relation = merged["relationships"][0]
    assert (relation["from_uid"], relation["type"], relation["to_uid"]) == (
        "Term::甲", "RELATED", "Term::乙",
    )
    assert relation["evidences"] == [evidence]
    assert relation["chunk_indices"] == [1]
    assert relation["provenance_status"] == "evidence"
    assert {node["uid"]: node["chunk_indices"] for node in merged["nodes"]} == {
        "Term::甲": [0], "Term::乙": [1],
    }
    assert items == before_merge
    assert result == original_result


def test_extract_version_defaults_to_latest_and_keeps_latest_label(monkeypatch):
    monkeypatch.delenv("KG_EXTRACT_ALGO_VERSION", raising=False)
    assert get_extract_version_label() == "latest"
    assert get_extract_version() == "latest"


@pytest.mark.parametrize("label", ["v2", "v2.2", "v3", "v3.1"])
def test_extract_version_label_can_be_pinned(monkeypatch, label):
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", label)
    assert get_extract_version_label() == label
    assert get_extract_version() == label


def test_jev_version_dispatches_to_jev_extract(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "jev")
    assert get_extract_version_label() == "jev"
    assert get_extract_version() == "jev"

    def fake(**_kwargs):
        return {
            "status": "succeeded",
            "graph": {"nodes": [], "relationships": []},
            "intermediate_model": {"objects": []},
            "attempts": [],
            "warnings": [],
        }

    monkeypatch.setattr("algorithm.knowledge_extract.jev_extract.extract_aggregate", fake)
    result = adapter.run_aggregate("class Term", ["内容"])
    assert result["extract_version"] == "jev"
    assert result["status"] == "succeeded"


def test_extract_version_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v9")
    with pytest.raises(ValueError, match="KG_EXTRACT_ALGO_VERSION"):
        get_extract_version()
    # 配置错误直接抛出，不降级成逐 chunk 的抽取失败
    with pytest.raises(ValueError, match="KG_EXTRACT_ALGO_VERSION"):
        adapter.extract_chunks("class Term", ["内容"])



def test_extract_chunks_stamps_extract_version_in_quality(monkeypatch):
    monkeypatch.delenv("KG_EXTRACT_ALGO_VERSION", raising=False)

    def empty_runner(**_kwargs):
        return {
            "status": "succeeded",
            "graph": {"nodes": [], "relationships": []},
            "intermediate_model": {"objects": []},
            "attempts": [],
            "warnings": [],
        }

    items = adapter.extract_chunks("class Term", ["内容"], runner=empty_runner)

    assert items[0]["error"] is None
    assert items[0]["quality"]["extract_version"] == "latest"


def _stub_v3_client(monkeypatch):
    """v3 的 create_llm_client 在 fake 路径下不应被调用；测试里替换成哑对象兜底。"""

    class DummyLLM:
        max_tokens = 16384

        def generate_structured(self, *_args, **_kwargs):  # pragma: no cover - 不会被调到
            raise AssertionError("fake extract_aggregate 不应真的调用 LLM")

    monkeypatch.setattr(
        "algorithm.knowledge_extract.v3.llm_client.create_llm_client",
        lambda **_kwargs: DummyLLM(),
    )


def test_v3_result_is_converted_and_projected_onto_chunks(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v3")
    _stub_v3_client(monkeypatch)

    def fake_extract_aggregate(ontology, *, chunks, **_kwargs):
        assert ontology == "class Term"
        # 适配器给 v3/latest 传字段 dict（版本包入口只认自家 ExtractionChunk 或 dict）
        assert [chunk["chunk_id"] for chunk in chunks] == ["chunk-0000", "chunk-0001"]
        # 新契约：extract_aggregate 返回 harness 聚合契约（graph/raw/
        # intermediate_model/attempts/warnings/profile/verification）。
        return {
            "status": "succeeded",
            "graph": {
                "nodes": [
                    {"uid": "Term::a", "label": "Term", "properties": {"name": "A", "定义": "甲"}},
                    {"uid": "Term::b", "label": "Term", "properties": {"name": "B"}},
                ],
                "relationships": [
                    {
                        "from_uid": "Term::a",
                        "to_uid": "Term::b",
                        "type": "RELATED",
                        "evidence": {"source_block_ids": ["chunk-0001"], "quote": "A 与 B 相关"},
                    }
                ],
            },
            "raw": {
                "entities": [],
                "relations": [
                    {
                        "from_type": "Term",
                        "from_name": "A",
                        "rel_type": "RELATED",
                        "to_type": "Term",
                        "to_name": "B",
                    }
                ],
            },
            "intermediate_model": {
                "objects": [
                    {"id": "e0", "type": "Term", "identity": "A",
                     "evidence": [{"source_block_ids": ["chunk-0000"], "quote": "A"}]},
                    {"id": "e1", "type": "Term", "identity": "B",
                     "evidence": [{"source_block_ids": ["chunk-0001"], "quote": "B"}]},
                ],
                "relations": [],
            },
            "attempts": [
                {"type": "invalid_json", "chunk": "chunk-0001",
                 "message": "return one complete JSON object"},
            ],
            "warnings": [],
            "profile": {},
            "verification": {"valid": True},
        }

    monkeypatch.setattr("algorithm.knowledge_extract.v3.extract_aggregate", fake_extract_aggregate)

    items = adapter.extract_chunks("class Term", ["文本A", "文本B"])

    assert all(item["error"] is None for item in items)
    assert all(item["quality"]["extract_version"] == "v3" for item in items)
    assert all(item["quality"]["status"] == "succeeded" for item in items)

    # 实体按证据 source_block_ids 投影：A 在 chunk 0，B 在 chunk 1
    assert [node["uid"] for node in items[0]["graph"]["nodes"]] == ["Term::a"]
    assert items[0]["graph"]["nodes"][0]["properties"] == {"name": "A", "定义": "甲"}
    assert items[0]["graph"]["relationships"] == []

    # 关系的 evidence 落在 chunk 1，两个端点作为闭包上下文带入
    assert {node["uid"] for node in items[1]["graph"]["nodes"]} == {"Term::a", "Term::b"}
    relation = items[1]["graph"]["relationships"][0]
    assert (relation["from_uid"], relation["to_uid"], relation["type"]) == (
        "Term::a",
        "Term::b",
        "RELATED",
    )
    assert relation["provenance_status"] == "evidence"
    assert relation["chunk_indices"] == [1]

    # raw 里的五元组关系让 deduplicate_chunk_graphs 能恢复跨 chunk 关系
    merged = capabilities.deduplicate_chunk_graphs(items)
    assert {node["uid"] for node in merged["nodes"]} == {"Term::a", "Term::b"}

    # 非终态 attempt（invalid_json）只作观察，不把 chunk 标失败
    assert any(attempt["type"] == "invalid_json" for attempt in items[1]["attempts"])
    assert items[1]["error"] is None


def test_v3_incomplete_status_keeps_chunks_publishable(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v3")
    _stub_v3_client(monkeypatch)

    def fake_extract_aggregate(ontology, *, chunks, **_kwargs):
        return {
            "status": "incomplete",
            "graph": {
                "nodes": [{"uid": "Term::a", "label": "Term", "properties": {"name": "A"}}],
                "relationships": [],
            },
            "raw": {"entities": [], "relations": []},
            "intermediate_model": {
                "objects": [
                    {"id": "e0", "type": "Term", "identity": "A",
                     "evidence": [{"source_block_ids": ["chunk-0000"]}]},
                ],
                "relations": [],
            },
            "attempts": [],
            "warnings": [],
            "profile": {},
            "verification": {"valid": True},
        }

    monkeypatch.setattr("algorithm.knowledge_extract.v3.extract_aggregate", fake_extract_aggregate)

    items = adapter.extract_chunks("class Term", ["文本A", "文本B"])

    assert all(item["error"] is None for item in items)
    # quality.status 由聚合 status 原样透传：incomplete -> partial 语义由评测侧解释
    assert all(item["quality"]["status"] == "incomplete" for item in items)
    assert all(item["quality"]["graph_publishable"] is True for item in items)
    assert [node["uid"] for node in items[0]["graph"]["nodes"]] == ["Term::a"]


def test_v3_failed_status_fails_every_chunk(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_ALGO_VERSION", "v3")
    _stub_v3_client(monkeypatch)

    def fake_extract_aggregate(ontology, *, chunks, **_kwargs):
        return {
            "status": "failed",
            "graph": {"nodes": [], "relationships": []},
            "raw": {"entities": [], "relations": []},
            "intermediate_model": {"objects": [], "relations": []},
            "attempts": [],
            "warnings": [],
            "profile": {},
            "verification": {"valid": False},
        }

    monkeypatch.setattr("algorithm.knowledge_extract.v3.extract_aggregate", fake_extract_aggregate)

    items = adapter.extract_chunks("class Term", ["文本A", "文本B"])

    assert all(item["error"] for item in items)
    assert all(item["error_code"] == "EXTRACTION_EXCEPTION" for item in items)
    assert all(item["quality"]["status"] == "failed" for item in items)
    assert all(item["quality"]["graph_publishable"] is False for item in items)


def test_jsonl_chunks_keep_sheet_and_data_row_provenance():
    from algorithm.preproc.preproc_src.table_json import render_json_block
    from algorithm.knowledge_pipeline.chunking import chunk_document_items
    text = "## Sheet-2\n" + render_json_block({"columns": ["编号"], "rows": [{"编号": str(i)} for i in range(45)]})
    items = chunk_document_items(text, chunk_size=4000)
    chunks = adapter.build_chunks([item["text"] for item in items], metadata=[
        {**item["metadata"], "source_file": "book.xlsx", "source_type": "xlsx"} for item in items])
    assert [chunk.metadata["sheet_name"] for chunk in chunks] == ["Sheet-2"] * 3
    assert [chunk.metadata["source_row_start"] for chunk in chunks] == [2, 22, 42]
    assert [chunk.metadata["source_row_end"] for chunk in chunks] == [21, 41, 46]
    assert [chunk.metadata["tables"][0]["row_count"] for chunk in chunks] == [20, 20, 5]
