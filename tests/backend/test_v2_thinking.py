from types import SimpleNamespace

from algorithm.knowledge_extract.v2.candidate_generation import (
    RELATION_PAIR_BATCH_SIZE,
    generate_structured,
    relation_pair_batch_size,
)
from algorithm.knowledge_extract.v2.llm_client import OpenAICompatibleLLM, _usage_dict


INCOMPATIBLE_SCHEMA = {
    "type": "object",
    "properties": {"decisions": {"type": "array", "minItems": 0}},
    "required": ["decisions"],
    "additionalProperties": False,
}
COMPATIBLE_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pair_id": {"type": "string"},
                    "label": {"type": "string"},
                },
                "required": ["pair_id", "label"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["decisions"],
    "additionalProperties": False,
}
MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "user"},
]


def _response(text: str, *, finish_reason: str = "stop", reasoning_tokens: int = 4):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=8,
            completion_tokens=12,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        ),
    )


def _llm(*, disable_thinking: bool, thinking_passes=frozenset({"relation", "repair_relation"})):
    calls: list[dict] = []
    replies: list[object] = []

    def create(**kwargs):
        calls.append(kwargs)
        if not replies:
            raise AssertionError("unexpected LLM call")
        return replies.pop(0)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
        responses=None,
    )
    llm = OpenAICompatibleLLM(
        client,
        model="deepseek-v4-flash",
        max_tokens=16384,
        disable_thinking=disable_thinking,
        thinking_passes=thinking_passes,
        thinking_max_tokens=32768,
    )
    return llm, calls, replies


def test_entity_pass_sends_thinking_disabled_even_when_allowlist_includes_relation():
    llm, calls, replies = _llm(disable_thinking=False)
    replies.append(_response('{"decisions":[]}'))
    result = llm.generate_structured(
        INCOMPATIBLE_SCHEMA,
        MESSAGES,
        {"pass_name": "entity", "response_mode": "json_schema"},
    )
    assert result.thinking == "disabled"
    assert calls[0]["max_tokens"] == 16384
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_relation_pass_uses_thinking_budget_when_global_switch_is_off():
    llm, calls, replies = _llm(disable_thinking=False)
    replies.append(_response('{"decisions":[]}'))
    result = llm.generate_structured(
        INCOMPATIBLE_SCHEMA,
        MESSAGES,
        {"pass_name": "relation", "response_mode": "json_schema"},
    )
    assert result.thinking == "enabled"
    assert result.max_tokens == 32768
    assert calls[0]["max_tokens"] == 32768
    assert calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}


def test_global_disable_thinking_overrides_pass_allowlist():
    llm, calls, replies = _llm(disable_thinking=True)
    replies.append(_response('{"decisions":[]}'))
    llm.generate_structured(
        INCOMPATIBLE_SCHEMA,
        MESSAGES,
        {"pass_name": "relation", "response_mode": "json_schema"},
    )
    assert calls[0]["max_tokens"] == 16384
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert relation_pair_batch_size(llm) == RELATION_PAIR_BATCH_SIZE


def test_thinking_relation_batch_shrinks_when_thinking_is_on():
    llm, _, _ = _llm(disable_thinking=False)
    assert relation_pair_batch_size(llm) == 8


def test_length_retry_bumps_budget_then_disables_thinking():
    llm, calls, replies = _llm(disable_thinking=False)
    replies.extend(
        [
            _response("{", finish_reason="length"),
            _response("{", finish_reason="length"),
            _response('{"decisions":[]}', finish_reason="stop"),
        ]
    )
    attempts: list[dict] = []
    parsed = generate_structured(
        llm,
        MESSAGES,
        INCOMPATIBLE_SCHEMA,
        use_scheduler=False,
        attempts=attempts,
        pass_name="relation",
        chunk_id="chunk-0",
        max_attempts=3,
        retry_base_seconds=0,
    )
    assert parsed == {"decisions": []}
    assert [item["max_tokens"] for item in calls] == [32768, 65536, 16384]
    assert calls[0]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert calls[1]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert calls[2]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert attempts[0]["type"] == "incomplete"
    assert attempts[1]["length_retry"] == "budget_bump"


def test_length_retry_does_not_bump_budget_for_entity_pass():
    llm, calls, replies = _llm(disable_thinking=False)
    replies.extend(
        [
            _response("{", finish_reason="length"),
            _response('{"decisions":[]}', finish_reason="stop"),
        ]
    )
    attempts: list[dict] = []
    parsed = generate_structured(
        llm,
        MESSAGES,
        INCOMPATIBLE_SCHEMA,
        use_scheduler=False,
        attempts=attempts,
        pass_name="entity",
        chunk_id="chunk-0",
        max_attempts=2,
        retry_base_seconds=0,
    )
    assert parsed == {"decisions": []}
    assert [item["max_tokens"] for item in calls] == [16384, 16384]
    assert all(item.get("extra_body") == {"thinking": {"type": "disabled"}} for item in calls)


def test_disable_thinking_false_does_not_bypass_pass_allowlist():
    llm, calls, replies = _llm(disable_thinking=False)
    replies.append(_response('{"decisions":[]}'))
    result = llm.generate_structured(
        INCOMPATIBLE_SCHEMA,
        MESSAGES,
        {"pass_name": "entity", "disable_thinking": False, "response_mode": "json_schema"},
    )
    assert result.thinking == "disabled"
    assert calls[0]["max_tokens"] == 16384
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_v4_flash_relation_uses_chat_json_schema_not_responses():
    calls: list[tuple[str, dict]] = []
    replies = [_response('{"decisions":[]}')]

    def chat_create(**kwargs):
        calls.append(("chat", kwargs))
        return replies.pop(0)

    def responses_create(**kwargs):
        calls.append(("responses", kwargs))
        raise AssertionError("deepseek-v4-flash 不应走 Responses 路径")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
        responses=SimpleNamespace(create=responses_create),
    )
    llm = OpenAICompatibleLLM(
        client,
        model="deepseek-v4-flash",
        max_tokens=16384,
        disable_thinking=False,
        thinking_passes=frozenset({"relation", "repair_relation"}),
        thinking_max_tokens=32768,
    )
    result = llm.generate_structured(
        COMPATIBLE_SCHEMA,
        MESSAGES,
        {"pass_name": "relation", "response_mode": "json_schema"},
    )
    assert result.thinking == "enabled"
    assert result.api == "chat.completions"
    assert [item[0] for item in calls] == ["chat"]
    assert calls[0][1]["max_tokens"] == 32768
    assert calls[0][1]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert calls[0][1]["response_format"]["type"] == "json_schema"
    assert "json_schema" in calls[0][1]["response_format"]


def test_v4_flash_entity_disables_thinking_on_chat_json_schema():
    calls: list[tuple[str, dict]] = []
    replies = [_response('{"decisions":[]}')]

    def chat_create(**kwargs):
        calls.append(("chat", kwargs))
        return replies.pop(0)

    def responses_create(**kwargs):
        raise AssertionError("deepseek-v4-flash 不应走 Responses 路径")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
        responses=SimpleNamespace(create=responses_create),
    )
    llm = OpenAICompatibleLLM(
        client,
        model="deepseek-v4-flash-0731",
        max_tokens=16384,
        disable_thinking=False,
        thinking_passes=frozenset({"relation", "repair_relation"}),
        thinking_max_tokens=32768,
    )
    result = llm.generate_structured(
        COMPATIBLE_SCHEMA,
        MESSAGES,
        {"pass_name": "entity", "response_mode": "json_schema"},
    )
    assert result.thinking == "disabled"
    assert result.api == "chat.completions"
    assert calls[0][1]["max_tokens"] == 16384
    assert calls[0][1]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_v4_flash_does_not_fall_back_to_responses_when_chat_json_schema_unsupported():
    calls: list[str] = []
    replies = [_response('{"decisions":[]}')]

    def chat_create(**kwargs):
        calls.append("chat")
        fmt = kwargs.get("response_format") or {}
        if fmt.get("type") == "json_schema":
            raise RuntimeError("json_schema is not supported")
        return replies.pop(0)

    def responses_create(**kwargs):
        calls.append("responses")
        raise AssertionError("deepseek-v4-flash 不应回落 Responses")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
        responses=SimpleNamespace(create=responses_create),
    )
    llm = OpenAICompatibleLLM(
        client,
        model="deepseek-v4-flash",
        max_tokens=16384,
        disable_thinking=False,
        thinking_passes=frozenset({"relation", "repair_relation"}),
        thinking_max_tokens=32768,
    )
    result = llm.generate_structured(
        COMPATIBLE_SCHEMA,
        MESSAGES,
        {"pass_name": "entity", "response_mode": "json_schema"},
    )
    assert result.thinking == "disabled"
    assert result.api == "chat.completions"
    assert "responses" not in calls
    assert calls[-1] == "chat"
    assert result.text == '{"decisions":[]}'


def test_usage_dict_exposes_reasoning_tokens():
    usage = _usage_dict(
        SimpleNamespace(
            completion_tokens=20,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=17),
        )
    )
    assert usage["reasoning_tokens"] == 17
    assert usage["completion_tokens"] == 20
