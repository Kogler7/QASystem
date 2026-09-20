from __future__ import annotations

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


def test_terminal_verification_blocks_public_graph_but_keeps_isolated_graph(monkeypatch):
    def invalid_runner(**_kwargs):
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

    monkeypatch.setattr(
        capabilities,
        "extract_chunks",
        lambda **kwargs: adapter.extract_chunks(
            kwargs["ontology"], kwargs["chunks"], runner=invalid_runner
        ),
    )
    batch = capabilities.GraphExtractionService().extract(
        "class Term", [TextChunk(text="内容")]
    )

    payload = batch.graph_data()
    assert payload["graph_publishable"] is False
    assert payload["graph"] == {"nodes": [], "relationships": []}
    assert [node["uid"] for node in payload["isolated_graph"]["nodes"]] == ["Term::a"]
    assert payload["quality"]["verification"]["valid"] is False


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
