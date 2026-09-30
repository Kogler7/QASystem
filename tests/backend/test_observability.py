"""可观测性契约测试：request_id 上下文、empty_reason 分类、告警、指标端点与阶段产物。"""
from __future__ import annotations

import json
import queue as std_queue
import types
from collections import Counter

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import observability


@pytest.fixture
def clean_observability_state(monkeypatch):
    """隔离进程内指标/告警状态，避免用例之间相互污染。"""
    monkeypatch.setattr(observability, "_stage_samples", {})
    monkeypatch.setattr(observability, "_counters", Counter())
    monkeypatch.setattr(observability, "_alert_last_sent", {})
    yield


# ---------------------------------------------------------------------------
# request_id 上下文
# ---------------------------------------------------------------------------


def test_request_id_defaults_to_none_and_binds_in_scope():
    assert observability.get_request_id() is None
    with observability.request_id_scope() as rid:
        assert rid
        assert observability.get_request_id() == rid
        with observability.request_id_scope() as nested:
            # 已有上下文时沿用，不覆盖
            assert nested == rid
    assert observability.get_request_id() is None


def test_request_id_scope_respects_explicit_value():
    with observability.request_id_scope("job-123") as rid:
        assert rid == "job-123"
        assert observability.get_request_id() == "job-123"
    assert observability.get_request_id() is None


def test_context_prefix_omits_empty_fields():
    assert observability.context_prefix() == ""
    assert observability.context_prefix(request_id=None) == ""
    assert observability.context_prefix(request_id="abc") == "request_id=abc "
    assert (
        observability.context_prefix(request_id="abc", document="a.md#c0ffee12")
        == "request_id=abc document=a.md#c0ffee12 "
    )


# ---------------------------------------------------------------------------
# empty_reason 分类（三种来源 + unknown）
# ---------------------------------------------------------------------------


def _classify(items=None, profile=None, warnings=None):
    from backend.app.services.algorithm_capabilities import classify_empty_extraction

    return classify_empty_extraction(items, profile, warnings)


def test_classify_empty_extraction_all_chunks_failed():
    items = [
        {"index": 0, "error": "llm 请求失败", "graph": {"nodes": [], "relationships": []}},
        {"index": 1, "error": "llm 请求失败", "graph": {"nodes": [], "relationships": []}},
    ]
    assert _classify(items=items) == "all_chunks_failed"


def test_classify_empty_extraction_filtered_by_validation_from_profile():
    profile = {
        "entity_candidates": 4,
        "relation_candidates": 2,
        "accepted_entities": 0,
        "accepted_relations": 0,
        "candidate_selection": {"rejected_entities": 4, "rejected_relations": 2},
    }
    items = [{"index": 0, "graph": {"nodes": [], "relationships": []}}]
    assert _classify(items=items, profile=profile) == "filtered_by_validation"


def test_classify_empty_extraction_filtered_by_validation_from_warnings():
    # profile 计数缺失时，拒绝类告警也能说明"提议过、被校验打掉"。
    warnings = [{"code": "ENTITY_REJECTED_BY_MAPPING", "message": "映射拒绝"}]
    items = [{"index": 0, "graph": {"nodes": [], "relationships": []}}]
    assert _classify(items=items, profile={}, warnings=warnings) == "filtered_by_validation"


def test_classify_empty_extraction_llm_no_proposal():
    profile = {
        "entity_candidates": 0,
        "relation_candidates": 0,
        "accepted_entities": 0,
        "accepted_relations": 0,
        "llm_calls": 3,
    }
    items = [{"index": 0, "graph": {"nodes": [], "relationships": []}}]
    assert _classify(items=items, profile=profile) == "llm_no_proposal"


def test_classify_empty_extraction_unknown_without_profile():
    items = [{"index": 0, "graph": {"nodes": [], "relationships": []}}]
    assert _classify(items=items, profile={}) == "unknown"


def test_graph_extraction_service_marks_empty_reason(monkeypatch):
    from backend.app.schemas import TextChunk
    from backend.app.services import algorithm_capabilities as capabilities

    def fake_extract_chunks(*, ontology, chunks, **_kwargs):
        return [
            {
                "index": position,
                "text": text,
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "attempts": [],
                "warnings": [],
                "profile": {
                    "entity_candidates": 0,
                    "relation_candidates": 0,
                    "accepted_entities": 0,
                    "accepted_relations": 0,
                    "llm_calls": 2,
                },
                "quality": {
                    "status": "succeeded",
                    "error_code": None,
                    "verification": {},
                    "graph_publishable": True,
                    "repair_stop_reason": None,
                },
                "error": None,
                "error_code": None,
                "raw_response": "",
            }
            for position, text in enumerate(chunks)
        ]

    monkeypatch.setattr(capabilities, "extract_chunks", fake_extract_chunks)
    batch = capabilities.graph_extraction_service.extract(
        "class Term",
        [TextChunk(text="第一段", ranges=[0, 3]), TextChunk(text="第二段", ranges=[3, 6])],
    )
    assert batch.empty_reason == "llm_no_proposal"


def test_pipeline_response_includes_empty_reason_and_alert(
    protocol_module, monkeypatch, clean_observability_state
):
    from backend.app.services import algorithm_capabilities as capabilities
    from backend.app.services import knowledge_extract_adapter  # noqa: F401  (stubbed module)

    def fake_extract_chunks(*, ontology, chunks, **_kwargs):
        return [
            {
                "index": position,
                "text": text,
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "attempts": [],
                "warnings": [{"code": "ENTITY_REJECTED_BY_MAPPING", "message": "拒绝"}],
                "profile": {
                    "entity_candidates": 3,
                    "relation_candidates": 0,
                    "accepted_entities": 0,
                    "accepted_relations": 0,
                    "candidate_selection": {"rejected_entities": 3, "rejected_relations": 0},
                },
                "quality": {
                    "status": "succeeded",
                    "error_code": None,
                    "verification": {},
                    "graph_publishable": True,
                    "repair_stop_reason": None,
                },
                "error": None,
                "error_code": None,
                "raw_response": "",
            }
            for position, text in enumerate(chunks)
        ]

    monkeypatch.setattr(capabilities, "extract_chunks", fake_extract_chunks)
    alerts = []
    monkeypatch.setattr(
        observability, "send_alert", lambda event, payload=None: alerts.append((event, payload or {}))
    )

    response = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "class Term"},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["code"] == 0
    assert body["data"]["extraction"]["graph"] == {"nodes": [], "relationships": []}
    assert body["data"]["stats"]["extraction"]["empty_reason"] == "filtered_by_validation"
    # 既有结构保持不变：原有字段一个不少
    assert body["data"]["stats"]["extraction"]["nodes_extracted"] == 0
    assert body["data"]["stats"]["extraction"]["relationships_extracted"] == 0
    assert body["data"]["stats"]["extraction"]["quality"]["graph_publishable"] is True
    assert [event for event, _ in alerts].count("empty_extraction") == 1
    alert_payload = dict(alerts)["empty_extraction"]
    assert alert_payload["empty_reason"] == "filtered_by_validation"
    assert alert_payload["stage"] == "extract"
    assert alert_payload["node_count"] == 0
    assert alert_payload["request_id"]


# ---------------------------------------------------------------------------
# 本体解析错误提前阻断（ONTOLOGY_INVALID / 4006）
# ---------------------------------------------------------------------------


def test_pipeline_blocks_invalid_ontology_with_4006(protocol_module, monkeypatch):
    alerts = []
    monkeypatch.setattr(
        observability, "send_alert", lambda event, payload=None: alerts.append((event, payload or {}))
    )
    response = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "这段文本里没有任何类声明，不是合法本体"},
    )
    body = response.json()
    assert response.status_code == 422
    assert body["code"] == 4006
    assert body["error"]["type"] == "ONTOLOGY_INVALID"
    assert body["error"]["stage"] == "ontology"
    assert [event for event, _ in alerts] == ["ontology_invalid"]
    assert dict(alerts)["ontology_invalid"]["stage"] == "ontology"


# ---------------------------------------------------------------------------
# StageRecorder 扩展：03-extract.json / 04-quality.json 落盘
# ---------------------------------------------------------------------------


def test_stage_recorder_writes_extract_and_quality_artifacts(
    protocol_module, monkeypatch, tmp_path
):
    from backend.app.services import algorithm_capabilities as capabilities
    from backend.app.services import knowledge_precipitation as pipeline_module

    def fake_extract_chunks(*, ontology, chunks, **_kwargs):
        return [
            {
                "index": position,
                "text": text,
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "attempts": [],
                "warnings": [],
                "profile": {},
                "quality": {
                    "status": "succeeded",
                    "error_code": None,
                    "verification": {"valid": True, "hard_issue_count": 0, "repairable_issue_count": 0},
                    "graph_publishable": True,
                    "repair_stop_reason": None,
                },
                "error": None,
                "error_code": None,
                "raw_response": "",
            }
            for position, text in enumerate(chunks)
        ]

    monkeypatch.setattr(capabilities, "extract_chunks", fake_extract_chunks)
    monkeypatch.setattr(pipeline_module, "EXTRACTION_SNAPSHOT_DIR", tmp_path)

    response = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "class Term"},
    )
    assert response.status_code == 200
    run_dirs = [path for path in tmp_path.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    names = {path.name for path in run_dirs[0].iterdir()}
    assert {
        "00-ontology.puml",
        "01-parse.md",
        "02-chunking.json",
        "03-extract.json",
        "04-quality.json",
    } <= names
    extract_summary = json.loads((run_dirs[0] / "03-extract.json").read_text(encoding="utf-8"))
    assert extract_summary["nodes"]["count"] == 0
    assert extract_summary["relationships"]["count"] == 0
    assert extract_summary["graph_count"] == 1
    quality = json.loads((run_dirs[0] / "04-quality.json").read_text(encoding="utf-8"))
    assert quality["graph_publishable"] is True
    assert quality["verification"]["valid"] is True


def test_stage_recorder_still_writes_error_log_on_failure(protocol_module, monkeypatch, tmp_path):
    from backend.app.services import knowledge_precipitation as pipeline_module

    monkeypatch.setattr(pipeline_module, "EXTRACTION_SNAPSHOT_DIR", tmp_path)

    class FailingProcessor:
        def process_file(self, *_args, **_kwargs):
            return {"success": False, "error": "缺少 LibreOffice（soffice）"}

    from backend.app.schemas import PipelineOptions
    from backend.app.services.knowledge_precipitation import (
        KnowledgeIngestionRequest,
        KnowledgePrecipitationService,
        PipelineError,
    )

    service = KnowledgePrecipitationService(processor_factory=FailingProcessor)
    with pytest.raises(PipelineError):
        service.process(
            KnowledgeIngestionRequest(
                file_name="sample.doc",
                content=b"content",
                ontology="class Term",
                options=PipelineOptions(),
            )
        )
    error_logs = list(tmp_path.glob("*_error.log"))
    assert len(error_logs) == 1
    assert "文档解析失败" in error_logs[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 告警 webhook
# ---------------------------------------------------------------------------


def test_send_alert_skipped_without_webhook(monkeypatch, clean_observability_state):
    monkeypatch.delenv("QASYSTEM_ALERT_WEBHOOK", raising=False)
    posted = []
    monkeypatch.setattr(observability, "_post_webhook", lambda url, content: posted.append((url, content)))
    assert observability.send_alert("empty_extraction", {"request_id": "r1"}) is False
    assert posted == []


def test_send_alert_posts_multiline_text_and_dedupes(monkeypatch, clean_observability_state):
    monkeypatch.setenv("QASYSTEM_ALERT_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=x")
    posted = []
    monkeypatch.setattr(observability, "_post_webhook", lambda url, content: posted.append((url, content)))

    payload = {
        "request_id": "rid-1",
        "document": "a.md#c0ffee12",
        "stage": "extract",
        "duration_seconds": 12.5,
        "node_count": 0,
        "relationship_count": 0,
    }
    assert observability.send_alert("empty_extraction", payload) is True
    assert observability.send_alert("empty_extraction", payload) is False  # 60s 内去重
    assert len(posted) == 1

    url, content = posted[0]
    assert url.startswith("https://oapi.dingtalk.com/")
    lines = content.splitlines()
    assert lines[0] == "【QASystem】empty_extraction"
    assert "request_id: rid-1" in lines
    assert "document: a.md#c0ffee12" in lines
    assert "stage: extract" in lines
    assert "duration_seconds: 12.5" in lines
    assert "node_count: 0" in lines

    # 不同 request_id 不去重
    assert observability.send_alert("empty_extraction", {**payload, "request_id": "rid-2"}) is True
    assert len(posted) == 2


def test_send_alert_failure_only_logs(monkeypatch, clean_observability_state):
    monkeypatch.setenv("QASYSTEM_ALERT_WEBHOOK", "https://webhook.example/send")

    def boom(_url, _content):
        raise OSError("connection refused")

    monkeypatch.setattr(observability, "_post_webhook", boom)
    # 发送失败返回 False 且不抛异常，主流程不受影响
    assert observability.send_alert("slow_stage", {"request_id": "r9"}) is False


def test_slow_stage_alert_only_fires_over_budget(monkeypatch, clean_observability_state):
    monkeypatch.delenv("QASYSTEM_ALERT_STAGE_SLOW_PARSE_SECONDS", raising=False)
    posted = []
    monkeypatch.setattr(observability, "_post_webhook", lambda url, content: posted.append(content))
    monkeypatch.setenv("QASYSTEM_ALERT_WEBHOOK", "https://webhook.example/send")

    # 未配置预算：只记录样本，不告警
    observability.record_stage_duration("parse", 999.0, document="a.md#abc")
    assert posted == []
    assert observability.stage_duration_summary()["parse"]["count"] == 1

    # 配置预算后：超预算告警并计数，任务本身不受影响（无异常）
    monkeypatch.setenv("QASYSTEM_ALERT_STAGE_SLOW_PARSE_SECONDS", "0.001")
    observability.record_stage_duration("parse", 1.0, document="a.md#abc", extra={"chunk_count": 4})
    assert len(posted) == 1
    assert "【QASystem】slow_stage" in posted[0]
    assert "stage: parse" in posted[0]
    assert "duration_seconds: 1.0" in posted[0]
    assert "budget_seconds: 0.001" in posted[0]
    counters = dict(observability._counters)
    assert counters["slow_stage_total"] == 1


# ---------------------------------------------------------------------------
# 指标端点
# ---------------------------------------------------------------------------


def _fake_manager():
    pending = std_queue.Queue()
    pending.put({"message_id": "m1"})
    pending.put({"message_id": "m2"})
    store = types.SimpleNamespace(
        jobs={
            "j1": {"status": "succeeded"},
            "j2": {"status": "failed"},
            "j3": {"status": "running"},
        }
    )
    task_queue = types.SimpleNamespace(_queues={"cpu.document": pending})
    return types.SimpleNamespace(store=store, queue=task_queue)


def test_metrics_payload_fields(clean_observability_state):
    observability.record_stage_duration("extract", 1.0)
    observability.record_stage_duration("extract", 3.0)
    observability.record_stage_duration("extract", 9.0)
    observability.increment_counter("empty_extraction_total")

    payload = observability.metrics_payload(manager=_fake_manager())

    assert payload["queue_depth"] == 2
    assert payload["job_status_counts"] == {"failed": 1, "running": 1, "succeeded": 1}
    assert payload["counters"]["empty_extraction_total"] == 1
    samples = payload["stage_duration_samples"]["extract"]
    assert samples["count"] == 3
    assert samples["p50"] == 3.0
    assert samples["p90"] == 9.0
    assert samples["p99"] == 9.0
    assert samples["max"] == 9.0


def test_metrics_endpoint_route(monkeypatch, clean_observability_state):
    monkeypatch.setattr(observability, "_read_execution_manager", lambda: _fake_manager())
    app = FastAPI()
    app.add_api_route(
        "/api/v1/observability/metrics",
        observability.metrics_endpoint,
        methods=["GET"],
        include_in_schema=False,
    )
    response = TestClient(app).get("/api/v1/observability/metrics")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"queue_depth", "job_status_counts", "counters", "stage_duration_samples"}
    assert body["queue_depth"] == 2


def test_metrics_payload_survives_missing_execution_layer(monkeypatch, clean_observability_state):
    monkeypatch.setattr(observability, "_read_execution_manager", lambda: None)
    payload = observability.metrics_payload()
    assert payload["queue_depth"] is None
    assert payload["job_status_counts"] == {}
    assert payload["counters"] == {}
    assert payload["stage_duration_samples"] == {}


# ---------------------------------------------------------------------------
# request_id 贯穿 HTTP 入口
# ---------------------------------------------------------------------------


def test_router_binds_request_id_for_sync_calls(protocol_module, monkeypatch):
    from backend.app.services import algorithm_capabilities as capabilities

    captured = {}

    def fake_extract_chunks(*, ontology, chunks, **_kwargs):
        captured["request_id"] = observability.get_request_id()
        return [
            {
                "index": position,
                "text": text,
                "raw": {},
                "graph": {"nodes": [], "relationships": []},
                "attempts": [],
                "warnings": [],
                "profile": {},
                "quality": {
                    "status": "succeeded",
                    "error_code": None,
                    "verification": {},
                    "graph_publishable": True,
                    "repair_stop_reason": None,
                },
                "error": None,
                "error_code": None,
                "raw_response": "",
            }
            for position, text in enumerate(chunks)
        ]

    monkeypatch.setattr(capabilities, "extract_chunks", fake_extract_chunks)
    response = TestClient(protocol_module.app).post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "class Term"},
    )
    assert response.status_code == 200
    # 同步 HTTP 调用在执行线程里也能拿到入口绑定的 request_id
    assert captured["request_id"]
