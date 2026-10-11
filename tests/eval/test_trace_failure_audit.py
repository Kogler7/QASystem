import json

from eval.tools.trace_failure_audit import audit_trace, audit_evaluation, http_category


def test_local_failure_artifacts_are_not_http_requests():
    assert http_category({"error_type": "input_limit", "http_seconds": None}) == "local_input_limit"
    assert http_category({"error_type": "TimeoutError", "http_seconds": None}) == "local_timeout"
    assert http_category({"error_type": "APITimeoutError", "http_seconds": 12}) == "http_timeout"
    assert http_category({"error_type": None, "http_seconds": 1}) == "http_returned"


def test_audit_keeps_real_length_work_budget_and_sensitive_content_separate(tmp_path):
    def save(name, value):
        (tmp_path / name).write_text(json.dumps(value), encoding="utf8")
    save("manifest.json", {"trace_id": "frozen"})
    save("000001-call.json", {"context": {"pass_name": "property"}, "messages": "secret-token"})
    text = '\n'.join(['{"entity_id":"one","properties":{"x":"same"}}'] * 50)
    save("000001-call-response.json", {"text": text, "finish_reason": "length"})
    save("000002-http.json", {"call_id": "000001-call", "http_seconds": 2})
    save("result.json", {"attempts": [{"terminal": True, "pass": "repair_relation", "type": "incomplete",
                                      "message": "Output split call budget exhausted", "pending_items": 89}]})
    result = audit_trace(tmp_path)
    assert result["totals"]["finish_length"] == 1
    assert result["totals"]["http_returned"] == 1
    assert result["totals"]["severe_repeat_responses"] == 1
    assert result["terminal_attempts"][0]["category"] == "work_budget_exhausted"
    assert result["terminal_attempts"][0]["pending_items"] == 89
    assert "secret-token" not in json.dumps(result)
    assert "properties" not in json.dumps(result)


def test_success_status_does_not_replace_numeric_quality_and_graph_is_not_exported(tmp_path):
    case = tmp_path / "rule"
    case.mkdir()
    (case / "scores.json").write_text(json.dumps({"status": "succeeded", "backend": {"modules": {"extract": {
        "metrics": {"entities": {"f1": 0, "golden": 40, "predicted": 48}, "predicted_graph": "secret"}}}}}), encoding="utf8")
    (case / "backend_extract.json").write_text(json.dumps({"data": {"quality": {
        "trace_id": "t", "status": "succeeded"}, "graph": "secret"}}), encoding="utf8")
    result = audit_evaluation(tmp_path, {"t": {"algorithm_status": "succeeded"}})
    assert result[0]["numeric_metrics"]["entities"]["f1"] == 0
    assert result[0]["public_status"] == "succeeded"
    assert "secret" not in json.dumps(result)


def test_interrupted_generation_is_counted_without_inventing_http_timeout(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"trace_id": "interrupted"}), encoding="utf8")
    (tmp_path / "000001-call.json").write_text(json.dumps({"context": {"pass_name": "relation"}}), encoding="utf8")
    result = audit_trace(tmp_path)
    assert result["result_present"] is False
    assert result["totals"]["generation_entries"] == 1
    assert result["totals"]["response_artifact_missing"] == 1
    assert result["totals"].get("actual_http", 0) == 0
    assert result["totals"].get("http_timeout", 0) == 0
