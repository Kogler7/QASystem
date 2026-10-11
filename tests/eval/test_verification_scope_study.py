import hashlib
import json
from pathlib import Path

from eval.tools.verification_scope_study import audit, section


ROOT = Path("test_data/repetition/verifier-scope-v1")


def test_frozen_requests_keep_candidates_and_schema_with_only_scope_prompt_changed():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf8"))
    assert manifest["planned_calls"] == 24
    for entry in manifest["cases"]:
        path = ROOT / (entry["id"] + ".json")
        assert hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
        case = json.loads(path.read_text(encoding="utf8"))
        assert section(case["old_messages"], "candidates") == section(case["fixed_messages"], "candidates")
        assert section(case["fixed_messages"], "current chunk")["text"] == ""
        assert "不得借用其他 item" in case["fixed_messages"][0]["content"]
        assert "不开放整个 chunk" in section(case["old_messages"], "current chunk")["text"]
    plan = json.loads((ROOT / "plan.json").read_text(encoding="utf8"))
    assert len(plan) == 24
    assert {row["provider"] for row in plan} == {"matrix-deepseek", "matrix-qwen35"}


def test_control_requires_support_and_rejects_cross_item_borrowing():
    case = json.loads((ROOT / "synthetic-scope-control.json").read_text(encoding="utf8"))
    correct = '\n'.join(json.dumps({"item_id": key, "supported": value, "reason": "control"})
                        for key, value in case["labels"]["expected"].items())
    result = audit(case, correct)
    assert result["schema_valid"] == 3
    assert result["missing_items"] == 0
    assert result["control_correct"] == result["control_total"] == 3
    wrong = correct.replace('"supported": "no"', '"supported": "yes"')
    assert audit(case, wrong)["control_correct"] == 1


def test_invalid_or_missing_verdicts_do_not_count_as_completed():
    case = json.loads((ROOT / "synthetic-scope-control.json").read_text(encoding="utf8"))
    result = audit(case, '{"item_id":"control-supported","supported":"yes"}')
    assert result["schema_valid"] == 0
    assert result["missing_items"] == 3
