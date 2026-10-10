"""Provider compatibility cannot silently multiply V3.1 model calls."""
from types import SimpleNamespace

import pytest

from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk
from algorithm.knowledge_extract.v2.llm_client import StructuredLLMResponse
from algorithm.knowledge_extract.v3_1 import extract_harness, llm_client
from algorithm.knowledge_extract.v3_1.llm_client import SingleRequestLLM


class FakeCompletions:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"choices": [{"message": {"content": '{"entities": [], "relations": []}'}, "finish_reason": "stop"}]}


@pytest.mark.parametrize("setting,explicit,expected", [
    (None, None, True), ("1", None, True), ("0", None, False),
    ("0", True, True), ("1", False, False),
])
def test_client_defaults_thinking_off_independently_of_shared_config(monkeypatch, setting, explicit, expected):
    monkeypatch.setenv("KG_EXTRACT_LLM_DISABLE_THINKING", "0")
    monkeypatch.delenv("KG_EXTRACT_V31_DISABLE_THINKING", raising=False)
    if setting is not None:
        monkeypatch.setenv("KG_EXTRACT_V31_DISABLE_THINKING", setting)
    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    def factory(**kwargs):
        return SimpleNamespace(client=client, model="test", max_tokens=100,
                               disable_thinking=kwargs["disable_thinking"],
                               base_url="https://api.siliconflow.cn/v1")

    monkeypatch.setattr(llm_client, "create_llm_client", factory)
    llm = llm_client.create_client(disable_thinking=explicit)
    result = extract_harness("class Thing", [ExtractionChunk("c1", "No entities")], llm=llm)
    assert llm.disable_thinking is expected
    assert result["profile"]["disable_thinking"] is expected
    assert completions.calls[0].get("extra_body") == ({"enable_thinking": False} if expected else None)


def test_invalid_thinking_setting_fails_before_client_creation(monkeypatch):
    monkeypatch.setenv("KG_EXTRACT_V31_DISABLE_THINKING", "maybe")
    with pytest.raises(ValueError, match="KG_EXTRACT_V31_DISABLE_THINKING"):
        llm_client.create_client()


@pytest.mark.parametrize("base_url,expected", [
    ("https://api.siliconflow.cn/v1", {"enable_thinking": False}),
    ("https://api.siliconflow.com/v1", {"enable_thinking": False}),
    ("https://api.deepseek.com", {"thinking": {"type": "disabled"}}),
    ("http://localhost:9999/v1", {"thinking": {"type": "disabled"}}),
])
def test_extraction_makes_one_chat_request_with_provider_thinking_parameter(base_url, expected):
    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    llm = SingleRequestLLM(client, model="deepseek-v4-flash", max_tokens=100,
                           disable_thinking=True, base_url=base_url)
    result = extract_harness("class Thing", [ExtractionChunk("c1", "No entities")], llm=llm)
    assert result["status"] == "succeeded"
    assert result["profile"]["llm_calls"] == 1
    assert len(completions.calls) == 1
    assert completions.calls[0]["extra_body"] == expected
    assert "response_format" not in completions.calls[0]


def test_unsupported_thinking_parameter_never_triggers_hidden_client_retry():
    """provider 不支持 thinking 参数时每阶段只发一次 HTTP，客户端不隐式重试。"""
    completions = FakeCompletions(ValueError("thinking parameter is unsupported"))
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    llm = SingleRequestLLM(client, model="any", max_tokens=100, disable_thinking=True)
    result = extract_harness("class Thing", [ExtractionChunk("c1", "Thing")], llm=llm)
    assert result["status"] == "failed"
    assert result["error_code"] == "LLM_REQUEST_FAILED"
    # 实体阶段失败后只允许一轮有界 repair，每个阶段各一次 HTTP 调用。
    passes = [a["pass"] for a in result["attempts"] if a["provider"] == "llm"]
    assert passes == ["entities", "repair"]
    assert result["profile"]["llm_http_calls"] == len(completions.calls) == 2


@pytest.mark.parametrize("status,text,finish_reason", [
    ("empty", "", "stop"),
    ("incomplete", '{"entities": [], "relations": []}', "length"),
    ("completed", '{"entities": [], "relations": []}', "content_filter"),
])
def test_envelope_failure_cannot_be_accepted_as_a_successful_empty_result(status, text, finish_reason):
    response = StructuredLLMResponse(text=text, api="chat", response_mode="plain",
                                     status=status, finish_reason=finish_reason)
    result = extract_harness("class Thing", [ExtractionChunk("c1", "Thing")],
                             llm=lambda messages: response)
    assert result["status"] == "failed"
    assert result["error_code"]
    # 失败 envelope 绝不降级成“成功但空”：实体阶段失败后只补一轮有界 repair。
    assert result["profile"]["llm_http_calls"] == 2
    assert result["raw"]["entities"] == [] and result["raw"]["relations"] == []
