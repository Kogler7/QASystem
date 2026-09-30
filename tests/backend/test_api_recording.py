"""API 请求完整记录：目录命名、落盘内容、保留期清理、开关与失败降级。"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from backend.app import api_recording, observability

# 与 test_observability.py 相同：protocol_module 的 v2 桩会顶掉包 __path__，
# 预导入真实本体解析器，保证单独跑本文件时行为与全量一致。
try:  # noqa: SIM105 - 有意的防御性导入
    import algorithm.knowledge_extract.v2.ontology_schema  # noqa: F401
except Exception:  # noqa: BLE001
    pass


@pytest.fixture
def record_root(tmp_path, monkeypatch):
    root = tmp_path / "records"
    monkeypatch.setenv("KG_API_RECORD_DIR", str(root))
    monkeypatch.setenv("KG_API_RECORD_ENABLED", "1")
    monkeypatch.setenv("KG_API_RECORD_RETENTION_DAYS", "7")
    return root


# ---------------------------------------------------------------------------
# 目录创建与命名
# ---------------------------------------------------------------------------


def test_record_directory_named_with_timestamp_and_request_id(record_root):
    with observability.request_id_scope("req-abc123"):
        with api_recording.record_request("POST", "/api/v1/test") as recorder:
            assert recorder.enabled
            assert recorder.directory is not None
            assert recorder.directory.parent == record_root
            assert re.fullmatch(r"\d{8}-\d{6}_req-abc123", recorder.directory.name)
            assert recorder.request_id == "req-abc123"


def test_record_directory_sanitizes_request_id(record_root):
    with observability.request_id_scope("job/evil:1"):
        with api_recording.record_request("POST", "/x") as recorder:
            assert recorder.enabled
            assert "/" not in recorder.directory.name
            assert ":" not in recorder.directory.name


# ---------------------------------------------------------------------------
# uploads / params / ontology / 中间产物 / timing / response
# ---------------------------------------------------------------------------


def test_uploads_params_and_ontology_written(record_root):
    with observability.request_id_scope("req-content"):
        with api_recording.record_request("POST", "/api/v1/pipelines/knowledge-ingestion") as recorder:
            recorder.save_uploads([("a.pdf", b"%PDF-123"), ("a.pdf", b"%PDF-456"), ("b.md", "# 标题".encode("utf-8"))])
            recorder.set_params({"options": {"chunking": {"size": 500}}, "files": ["a.pdf", "b.md"]})
            recorder.set_params({"blob": b"should-not-leak"})
            recorder.save_ontology("@startuml\nclass Term\n@enduml")
        directory = recorder.directory

    assert (directory / "uploads" / "a.pdf").read_bytes() == b"%PDF-123"
    assert (directory / "uploads" / "a__2.pdf").read_bytes() == b"%PDF-456"
    assert (directory / "uploads" / "b.md").read_bytes() == "# 标题".encode("utf-8")

    params = json.loads((directory / "params.json").read_text(encoding="utf-8"))
    assert params["method"] == "POST"
    assert params["endpoint"] == "/api/v1/pipelines/knowledge-ingestion"
    assert params["request_id"] == "req-content"
    assert params["started_at"]
    assert params["params"]["options"]["chunking"]["size"] == 500
    assert params["params"]["files"] == ["a.pdf", "b.md"]
    # bytes 参数绝不写原文，只留长度占位
    assert params["params"]["blob"] == "<bytes:15>"

    assert (directory / "ontology.txt").read_text(encoding="utf-8") == "@startuml\nclass Term\n@enduml"

    timing = json.loads((directory / "timing.json").read_text(encoding="utf-8"))
    assert timing["request_id"] == "req-content"
    assert timing["total_seconds"] >= 0
    assert timing["stages"] == []


def test_stage_artifacts_response_and_stage_timings(record_root):
    with observability.request_id_scope("req-stages"):
        with api_recording.record_request("POST", "/api/v1/documents/parse") as recorder:
            recorder.write_text("stage_parse/sample.md", "# 解析结果")
            recorder.write_json("stage_chunk/chunks.json", {"count": 1, "items": [{"text": "片段"}]})
            recorder.write_json("stage_extract/result.json", {"graph": {"nodes": [], "relationships": []}})
            observability.record_stage_duration("parse", 1.5)
            observability.record_stage_duration("chunk", 0.25)
            recorder.capture_response(JSONResponse(content={"code": 0, "data": {"markdown": "# 解析结果"}}))
        directory = recorder.directory

    assert (directory / "stage_parse" / "sample.md").read_text(encoding="utf-8") == "# 解析结果"
    chunks = json.loads((directory / "stage_chunk" / "chunks.json").read_text(encoding="utf-8"))
    assert chunks["items"][0]["text"] == "片段"
    extraction = json.loads((directory / "stage_extract" / "result.json").read_text(encoding="utf-8"))
    assert extraction["graph"] == {"nodes": [], "relationships": []}

    response_body = json.loads((directory / "response.json").read_text(encoding="utf-8"))
    assert response_body == {"code": 0, "data": {"markdown": "# 解析结果"}}

    timing = json.loads((directory / "timing.json").read_text(encoding="utf-8"))
    assert timing["status"] == "succeeded"
    by_stage = {item["stage"]: item["duration_seconds"] for item in timing["stages"]}
    assert by_stage == {"parse": 1.5, "chunk": 0.25}


def test_failed_response_marks_timing_failed(record_root):
    with observability.request_id_scope("req-fail"):
        with api_recording.record_request("POST", "/x") as recorder:
            recorder.capture_response(JSONResponse(status_code=422, content={"code": 4006}))
        timing = json.loads((recorder.directory / "timing.json").read_text(encoding="utf-8"))
    assert timing["status"] == "failed"


def test_path_traversal_is_contained(record_root):
    with observability.request_id_scope("req-path"):
        with api_recording.record_request("POST", "/x") as recorder:
            recorder.write_text("../escape.txt", "不该出去")
            recorder.write_text("stage_parse/..\\..\\evil.md", "也不该出去")
        directory = recorder.directory
    assert not (record_root / "escape.txt").exists()
    assert not (record_root / "evil.md").exists()
    assert (directory / "escape.txt").read_text(encoding="utf-8") == "不该出去"


# ---------------------------------------------------------------------------
# run.log
# ---------------------------------------------------------------------------


def test_run_log_captures_only_matching_request_logs(record_root):
    probe = logging.getLogger("tests.api_recording.probe")
    probe.setLevel(logging.INFO)
    with observability.request_id_scope("req-log-1"):
        with api_recording.record_request("POST", "/x") as recorder:
            probe.info("属于本请求的日志")
        directory = recorder.directory
    probe.info("请求结束后的日志")
    with observability.request_id_scope("req-log-2"):
        probe.info("其他请求的日志")

    run_log = (directory / "run.log").read_text(encoding="utf-8")
    assert "request_id=req-log-1" in run_log
    assert "属于本请求的日志" in run_log
    assert "请求结束后的日志" not in run_log
    assert "其他请求的日志" not in run_log


def test_run_log_captures_non_propagating_loggers(record_root):
    # ensure_logging 风格的 logger：propagate=False，root 挂载收不到，需直挂。
    direct = logging.getLogger("tests.api_recording.direct")
    direct.propagate = False
    try:
        with observability.request_id_scope("req-direct"):
            with api_recording.record_request("POST", "/x") as recorder:
                direct.warning("直挂 logger 的日志")
            directory = recorder.directory
        run_log = (directory / "run.log").read_text(encoding="utf-8")
        assert "直挂 logger 的日志" in run_log
    finally:
        direct.propagate = True


# ---------------------------------------------------------------------------
# 保留期清理
# ---------------------------------------------------------------------------


def _stamp(days_ago: float) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d-%H%M%S")


def test_cleanup_removes_expired_keeps_recent_and_unmatched(record_root):
    old_dir = record_root / f"{_stamp(8)}_oldreq"
    recent_dir = record_root / f"{_stamp(3)}_newreq"
    unmatched = record_root / "random-dir"
    for path in (old_dir, recent_dir, unmatched):
        path.mkdir(parents=True)
        (path / "keep.txt").write_text("x", encoding="utf-8")
    # 未命中命名规范的目录即使 mtime 很老也不动
    ancient = time.time() - 30 * 86400
    os.utime(unmatched, (ancient, ancient))

    removed = api_recording.cleanup_old_records(record_root, 7)

    assert removed == 1
    assert not old_dir.exists()
    assert (recent_dir / "keep.txt").is_file()
    assert (unmatched / "keep.txt").is_file()


def test_cleanup_disabled_when_retention_not_positive(record_root):
    old_dir = record_root / f"{_stamp(30)}_oldreq"
    old_dir.mkdir(parents=True)
    assert api_recording.cleanup_old_records(record_root, 0) == 0
    assert api_recording.cleanup_old_records(record_root, -1) == 0
    assert old_dir.is_dir()


def test_cleanup_uses_mtime_when_name_timestamp_unparseable(record_root):
    weird = record_root / "20269999-999999_broken"
    weird.mkdir(parents=True)
    ancient = time.time() - 30 * 86400
    os.utime(weird, (ancient, ancient))
    assert api_recording.cleanup_old_records(record_root, 7) == 1
    assert not weird.exists()


# ---------------------------------------------------------------------------
# 开关与失败降级
# ---------------------------------------------------------------------------


def test_disabled_recording_creates_nothing(record_root, monkeypatch):
    monkeypatch.setenv("KG_API_RECORD_ENABLED", "0")
    with observability.request_id_scope("req-off"):
        with api_recording.record_request("POST", "/x") as recorder:
            assert not recorder.enabled
            assert recorder.directory is None
            recorder.save_uploads([("a.bin", b"x")])
            recorder.write_text("a.txt", "x")
            recorder.capture_response(JSONResponse(content={"code": 0}))
    assert not record_root.exists()


def test_null_recorder_outside_request_scope_is_noop():
    recorder = api_recording.current_recorder()
    assert not recorder.enabled
    assert recorder.write_text("a.txt", "x") is None
    assert recorder.write_json("a.json", {"a": 1}) is None
    recorder.save_uploads([("a.bin", b"x")])
    recorder.set_params({"a": 1})
    recorder.capture_response(JSONResponse(content={"code": 0}))
    recorder.finalize()


def test_unwritable_root_degrades_without_raising(record_root, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("KG_API_RECORD_DIR", str(blocker))
    with observability.request_id_scope("req-nowrite"):
        with api_recording.record_request("POST", "/x") as recorder:
            assert not recorder.enabled
            assert recorder.directory is None
            recorder.save_uploads([("a.bin", b"x")])
            recorder.write_json("stage_chunk/chunks.json", {"items": []})
            recorder.capture_response(JSONResponse(content={"code": 0}))


# ---------------------------------------------------------------------------
# 端到端：真实路由 + inline 执行
# ---------------------------------------------------------------------------


@pytest.fixture
def recorded_app(protocol_module, record_root, monkeypatch, tmp_path):
    # 服务层自身的 StageRecorder 快照目录一并隔离，避免污染 backend/data。
    from backend.app.services import knowledge_precipitation as pipeline_module

    monkeypatch.setattr(pipeline_module, "EXTRACTION_SNAPSHOT_DIR", tmp_path / "snapshots")
    return TestClient(protocol_module.app)


def _only_record_dir(root: Path) -> Path:
    dirs = [path for path in root.iterdir() if path.is_dir()]
    assert len(dirs) == 1
    assert re.fullmatch(r"\d{8}-\d{6}_[0-9a-f]{32}", dirs[0].name)
    return dirs[0]


def test_pipeline_request_records_full_artifacts(recorded_app, record_root):
    ontology = "class Term"
    response = recorded_app.post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": ontology},
    )
    assert response.status_code == 200
    record = _only_record_dir(record_root)

    assert (record / "uploads" / "sample.csv").read_bytes() == b"name\nvalue\n"
    params = json.loads((record / "params.json").read_text(encoding="utf-8"))
    assert params["endpoint"] == "/api/v1/pipelines/knowledge-ingestion"
    assert params["params"]["files"] == ["sample.csv"]
    assert params["params"]["batch"] is False
    assert "options" in params["params"]
    assert (record / "ontology.txt").read_text(encoding="utf-8") == ontology

    # inline 执行：服务层钩子写中间产物
    parsed = (record / "stage_parse" / "sample.md").read_text(encoding="utf-8")
    assert "测试文档" in parsed
    chunks = json.loads((record / "stage_chunk" / "chunks.json").read_text(encoding="utf-8"))
    assert chunks["count"] >= 1 and chunks["items"][0]["text"]
    extraction = json.loads((record / "stage_extract" / "result.json").read_text(encoding="utf-8"))
    assert extraction["graph"] == {"nodes": [], "relationships": []}

    response_body = response.json()
    assert json.loads((record / "response.json").read_text(encoding="utf-8"))["code"] == response_body["code"] == 0
    timing = json.loads((record / "timing.json").read_text(encoding="utf-8"))
    assert timing["status"] == "succeeded"
    assert timing["total_seconds"] >= 0
    stages = {item["stage"] for item in timing["stages"]}
    assert {"parse", "chunk", "extract"} <= stages
    assert all(item["duration_seconds"] >= 0 for item in timing["stages"])

    run_log = (record / "run.log").read_text(encoding="utf-8")
    assert f"request_id={params['request_id']}" in run_log
    assert "知识沉淀" in run_log


def test_parse_request_records_upload_and_markdown(recorded_app, record_root):
    response = recorded_app.post(
        "/api/v1/documents/parse",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
    )
    assert response.status_code == 200
    record = _only_record_dir(record_root)
    assert (record / "uploads" / "sample.csv").read_bytes() == b"name\nvalue\n"
    params = json.loads((record / "params.json").read_text(encoding="utf-8"))
    assert params["params"]["file_name"] == "sample.csv"
    markdown = (record / "stage_parse" / "sample.md").read_text(encoding="utf-8")
    assert markdown == "# 测试文档\n测试内容"


def test_chunk_request_records_chunks(recorded_app, record_root):
    response = recorded_app.post(
        "/api/v1/documents/chunk",
        json={"file_name": "sample.md", "markdown": "第一段。第二段。", "options": {"size": 500}},
    )
    assert response.status_code == 200
    record = _only_record_dir(record_root)
    assert not (record / "uploads").exists()
    params = json.loads((record / "params.json").read_text(encoding="utf-8"))
    assert params["params"]["request"]["markdown"] == "第一段。第二段。"
    chunks = json.loads((record / "stage_chunk" / "chunks.json").read_text(encoding="utf-8"))
    assert chunks["items"][0]["range"] == [0, 8]


def test_extract_request_records_ontology_and_result(recorded_app, record_root):
    response = recorded_app.post(
        "/api/v1/graphs/extract",
        json={"ontology": "class Term", "items": [{"text": "条款一内容"}]},
    )
    assert response.status_code == 200
    record = _only_record_dir(record_root)
    assert (record / "ontology.txt").read_text(encoding="utf-8") == "class Term"
    result = json.loads((record / "stage_extract" / "result.json").read_text(encoding="utf-8"))
    assert "graph" in result or "graphs" in result


def test_disabled_recording_produces_no_directory(recorded_app, record_root, monkeypatch):
    monkeypatch.setenv("KG_API_RECORD_ENABLED", "0")
    response = recorded_app.post(
        "/api/v1/documents/chunk",
        json={"file_name": "sample.md", "markdown": "第一段。第二段。", "options": {"size": 500}},
    )
    assert response.status_code == 200
    assert not record_root.exists()


def test_recording_failure_does_not_affect_response(recorded_app, record_root, tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("file", encoding="utf-8")
    monkeypatch.setenv("KG_API_RECORD_DIR", str(blocker))
    response = recorded_app.post(
        "/api/v1/documents/chunk",
        json={"file_name": "sample.md", "markdown": "第一段。第二段。", "options": {"size": 500}},
    )
    assert response.status_code == 200
    assert response.json()["code"] == 0


def test_recording_internal_error_does_not_affect_response(recorded_app, record_root, monkeypatch):
    # 落盘方法内部抛错时必须被吞掉，接口照常返回。
    monkeypatch.setattr(
        api_recording.RequestRecorder,
        "_write_bytes",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    response = recorded_app.post(
        "/api/v1/pipelines/knowledge-ingestion",
        files={"file": ("sample.csv", b"name\nvalue\n", "text/csv")},
        data={"ontology": "class Term"},
    )
    assert response.status_code == 200
    assert response.json()["code"] == 0
