import json

import pytest

from eval.tools.judge_freeze_predictions import freeze


def setup(tmp_path, nodes):
    inputs = tmp_path / "inputs"
    output = tmp_path / "run"
    case = inputs / "group/case/extract"
    case.mkdir(parents=True)
    (case / "golden.json").write_text('{"entities": [], "relations": []}', encoding="utf-8")
    predicted = output / "group/case"
    predicted.mkdir(parents=True)
    (predicted / "backend_extract.json").write_text(json.dumps({"data": {"graph": {"nodes": nodes, "relationships": []}}}), encoding="utf-8")
    return inputs, output


def test_freeze_preserves_existing_predictions_and_failure_state(tmp_path):
    inputs, output = setup(tmp_path, [{"uid": "Term::A", "label": "Term", "properties": {"name": "A"}}])
    (output / "group/case/scores.json").write_text('{"backend":{"modules":{"extract":{"status":"partial"}}}}', encoding="utf-8")
    data = freeze(inputs, output)
    assert data["cases"]["group__case"]["entities"]["predicted"][0]["name"] == "A"
    assert data["sources"]["group__case"]["extraction_status"] == "partial"
    assert data["source_text_reviewed"] is False


def test_bad_rows_fail_instead_of_improving_score_by_discarding(tmp_path):
    inputs, output = setup(tmp_path, [{"label": "", "properties": {}}])
    with pytest.raises(ValueError, match="discard"):
        freeze(inputs, output)


def test_no_saved_predictions_never_runs_extraction(tmp_path):
    with pytest.raises(ValueError, match="no saved predictions"):
        freeze(tmp_path, tmp_path)


def test_bad_golden_rows_fail_instead_of_silently_changing_denominator(tmp_path):
    inputs, output = setup(tmp_path, [])
    (inputs / "group/case/extract/golden.json").write_text(
        '{"nodes":[{"label":"","properties":{}}],"relationships":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="golden.*discard"):
        freeze(inputs, output)
