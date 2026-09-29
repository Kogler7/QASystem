from __future__ import annotations

from jsonschema import ValidationError, validate

from agentcode.delivery import (
    ANSWER_V1_ID,
    ANSWER_V1_SCHEMA,
    answer_v1_delivery,
    normalize_answer_payload,
)


def _valid_answer(**overrides):
    payload = {
        "conclusion": "故障率为 2.3%",
        "evidence": [{"name": "故障率", "id": "e1", "detail": "近 7 日 2.3%"}],
        "assumptions": [],
        "gaps": [],
        "confidence": "high",
        "confidence_reason": "知识空间✓ 实体✓ 方案类✓ 工具✓",
        "charts": [],
        "solution_path": ["步骤1 查询"],
    }
    payload.update(overrides)
    return payload


def test_answer_v1_schema_accepts_complete_object() -> None:
    validate(_valid_answer(), ANSWER_V1_SCHEMA)


def test_answer_v1_schema_rejects_prose_wrapper_and_empty_conclusion() -> None:
    try:
        validate({"text": "【结论】2.3%"}, ANSWER_V1_SCHEMA)
    except ValidationError:
        pass
    else:
        raise AssertionError("expected prose wrapper to fail")
    try:
        validate(_valid_answer(conclusion=""), ANSWER_V1_SCHEMA)
    except ValidationError:
        return
    raise AssertionError("expected empty conclusion to fail")


def test_answer_v1_schema_rejects_unknown_fields() -> None:
    payload = _valid_answer()
    payload["extra"] = "nope"
    try:
        validate(payload, ANSWER_V1_SCHEMA)
    except ValidationError:
        return
    raise AssertionError("expected additionalProperties to fail")


def test_normalize_maps_chinese_confidence() -> None:
    assert normalize_answer_payload(_valid_answer(confidence="高"))["confidence"] == "high"
    assert normalize_answer_payload(_valid_answer(confidence="中"))["confidence"] == "medium"
    assert normalize_answer_payload(_valid_answer(confidence="低"))["confidence"] == "low"
    assert normalize_answer_payload(_valid_answer(confidence="HIGH"))["confidence"] == "high"


def test_answer_v1_delivery_config_identity() -> None:
    delivery = answer_v1_delivery()
    assert delivery.output_schema_id == ANSWER_V1_ID
    assert delivery.json_schema == ANSWER_V1_SCHEMA
