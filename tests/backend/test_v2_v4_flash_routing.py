"""v4-flash 在 MaaS 上的 chat/Responses 路由契约（移植自 zyx 分支 test_v2_thinking）。

zyx 分支的其余思考预算测试依赖重写版 llm_client 已移除的按 pass 思考
预算特性，未随合并恢复；本文件只覆盖已移植的「v4-flash chat json_schema
失败后不再回落 Responses」行为。
"""
from types import SimpleNamespace

from algorithm.knowledge_extract.v2.llm_client import OpenAICompatibleLLM


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


def test_v4_flash_does_not_fall_back_to_responses_when_chat_json_schema_unsupported():
    calls: list[str] = []
    replies = ['{"decisions":[]}']

    def chat_create(**kwargs):
        calls.append("chat")
        fmt = kwargs.get("response_format") or {}
        if fmt.get("type") == "json_schema":
            raise RuntimeError("json_schema is not supported")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=replies.pop(0)),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )

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
    )
    result = llm.generate_structured(
        COMPATIBLE_SCHEMA,
        MESSAGES,
        {"pass_name": "entity", "response_mode": "json_schema"},
    )
    assert result.api == "chat.completions"
    assert "responses" not in calls
    assert calls[-1] == "chat"
    assert result.text == '{"decisions":[]}'
