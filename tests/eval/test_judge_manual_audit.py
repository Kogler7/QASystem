import json

from eval.tools.judge_manual_audit import build_case


def test_worksheet_preserves_properties_duplicates_and_records_scope(tmp_path):
    golden = tmp_path / "golden.json"
    prediction = tmp_path / "prediction.json"
    item = {"type": "Term", "name": "A", "properties": {"definition": "meaning"}, "evidence": {"quote": "private"}}
    golden.write_text(json.dumps({"entities": [item], "relations": []}), encoding="utf-8")
    prediction.write_text(json.dumps({"data": {"graph": {"nodes": [
        {"uid": "1", "label": "Term", "properties": {"name": "A"}},
        {"uid": "2", "label": "Term", "properties": {"name": "A"}},
    ], "relationships": []}}}), encoding="utf-8")
    result = build_case(golden, prediction, {"clef": tmp_path / "missing.json"})
    assert result["independent_human_review"] is False
    assert "source text not reviewed" in result["scope"]
    assert len(result["collections"]["entities"]["predicted"]) == 2
    assert result["collections"]["entities"]["golden"][0]["properties"] == {"definition": "meaning"}
    assert "evidence" not in result["collections"]["entities"]["golden"][0]
    assert result["judges"] == {}
    assert len(result["hashes"]["golden"]) == 64


def test_worksheet_joins_saved_judge_without_inference(tmp_path):
    golden = tmp_path / "golden.json"
    prediction = tmp_path / "prediction.json"
    judge = tmp_path / "details.json"
    golden.write_text('{"entities": [], "relations": []}', encoding="utf-8")
    prediction.write_text('{"data": {"graph": {"nodes": [], "relationships": []}}}', encoding="utf-8")
    judge.write_text('{"backend_extract": {"entities": {"matched": []}}}', encoding="utf-8")
    result = build_case(golden, prediction, {"api": judge})
    assert result["judges"]["api"] == {"entities": {"matched": []}}
