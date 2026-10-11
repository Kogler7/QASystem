import importlib
import json
from types import SimpleNamespace

import pytest

from algorithm.knowledge_extract.jsonl_protocol import decode_jsonl, messages_for_jsonl


SCHEMA = {
    "type": "object", "required": ["entities"], "additionalProperties": False,
    "properties": {"entities": {"type": "array", "items": {
        "type": "object", "required": ["name"], "additionalProperties": False,
        "properties": {"name": {"type": "string"}},
    }}},
}


def test_effective_configuration_reports_wire_format_not_legacy_profile():
    from backend.app.services.extraction_config import public_config
    result = public_config({"version": "v3", "response_format": "json_object", "api_mode": "auto"})
    assert result["response_format"] is None
    assert result["output_format"] == "jsonl"
    assert result["api_mode"] == "chat"


def test_truncated_line_does_not_destroy_prior_records_or_later_valid_lines():
    data, errors = decode_jsonl('{"name":"甲"}\n{"name":"broken\n{"name":"乙"}', SCHEMA)
    assert data == {"entities": [{"name": "甲"}, {"name": "乙"}]}
    assert len(errors) == 1


def test_duplicate_loop_and_escaped_newline():
    line = json.dumps({"name": "甲\n乙"}, ensure_ascii=False)
    data, errors = decode_jsonl("\n".join([line] * 248), SCHEMA)
    assert data == {"entities": [{"name": "甲\n乙"}]}
    assert not errors


def test_explicit_empty_distinguished_from_invalid_and_blank():
    assert decode_jsonl("null", SCHEMA) == ({"entities": []}, [])
    assert decode_jsonl("", SCHEMA)[1]
    assert decode_jsonl('null\n{"name":"甲"}', SCHEMA)[1]
    assert decode_jsonl('```jsonl\n{"name":"甲"}\n```', SCHEMA)[1]


def test_property_protocol_is_per_field_and_does_not_mutate_caller():
    schema = {"properties": {"updates": {"type": "array", "items": {"type": "object"}}}}
    messages = [{"role": "system", "content": "抽取"}]
    result = messages_for_jsonl(messages, schema)
    assert "每行只更新一个属性" in result[0]["content"]
    assert messages[0]["content"] == "抽取"


@pytest.mark.parametrize("version", ["v2", "v3", "latest"])
def test_live_adapter_never_sends_schema_or_json_response_format(version):
    module = importlib.import_module(f"algorithm.knowledge_extract.{version}.llm_client")
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"name":"甲"}'), finish_reason="stop")])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    llm = module.OpenAICompatibleLLM(client, model="deepseek-ai/DeepSeek-V4-Flash",
                                    max_tokens=128, disable_thinking=True, base_url="https://api.siliconflow.cn/v1")
    response = llm.generate_structured(SCHEMA, [{"role": "user", "content": "抽取"}],
                                       {"response_mode": "json_schema"})
    assert "response_format" not in calls[0]
    assert response.response_mode == "jsonl"
    assert response.text == '{"name":"甲"}'


@pytest.mark.parametrize("version", ["v2", "v3", "latest"])
@pytest.mark.parametrize("finish_reason", ["length", "stop"])
def test_recovery_validates_each_line_and_keeps_failure_visible(version, finish_reason):
    cg = importlib.import_module(f"algorithm.knowledge_extract.{version}.candidate_generation")
    response_type = importlib.import_module(f"algorithm.knowledge_extract.{version}.llm_client").StructuredLLMResponse
    response = response_type(text='{"name":"甲"}\n{"unknown":1}\n{"name":"unfinished',
                             api="chat.completions", response_mode="jsonl",
                             status="incomplete" if finish_reason == "length" else "completed",
                             finish_reason=finish_reason)
    llm = SimpleNamespace(generate_structured=lambda *args: response)
    attempts = []
    data = cg.generate_structured(llm, [{"role":"system","content":"抽取"}, {"role":"user","content":"甲"}],
                                  SCHEMA, use_scheduler=False, attempts=attempts, pass_name="entity",
                                  chunk_id="test", max_attempts=1, retry_base_seconds=0)
    assert data == {"entities": [{"name": "甲"}]}
    assert any(row.get("terminal") and row["type"] in {"incomplete", "json"} for row in attempts)
    assert any(row["type"] == "recovered_prefix" for row in attempts)
