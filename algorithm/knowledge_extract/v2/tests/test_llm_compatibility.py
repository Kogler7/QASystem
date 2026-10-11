from __future__ import annotations

from types import SimpleNamespace
import unittest

from algorithm.knowledge_extract.v2.llm_client import OpenAICompatibleLLM, _is_deepseek_v4_flash


class _Completions:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response = response or SimpleNamespace(
            id="chat-1",
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="{}"),
                finish_reason="stop",
            )],
        )
        self.error = error

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            error = self.error
            if isinstance(error, list):
                error = error.pop(0)
            if error is not None:
                raise error
        return self.response


class _Responses:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(status="completed", output_text="{}")


class _Client:
    def __init__(self, *, chat=None):
        self.chat = SimpleNamespace(completions=chat or _Completions())
        self.responses = _Responses()


SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "string"}}},
    "required": ["items"],
    "additionalProperties": False,
}


class LLMCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _llm(client, *, base_url: str, model: str = "deepseek-v4-flash"):
        return OpenAICompatibleLLM(
            client,
            model=model,
            max_tokens=128,
            disable_thinking=True,
            base_url=base_url,
        )

    def test_organization_prefixed_v4_flash_uses_chat(self):
        """带组织前缀的 V4-Flash（如硅基流动）也必须走 chat.completions。

        兼容服务没有 /v1/responses，整串前缀匹配会让请求打到那里拿 404。
        """
        client = _Client()
        llm = self._llm(client, base_url="https://api.siliconflow.cn/v1",
                        model="deepseek-ai/DeepSeek-V4-Flash")

        result = llm.generate_structured(SCHEMA, [{"role": "user", "content": "Return JSON."}],
                                         {"schema_name": "extraction"})

        self.assertEqual(result.api, "chat.completions")
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertEqual(len(client.responses.calls), 0)

    def test_v4_flash_matching_ignores_organization_prefix(self):
        for model in ("deepseek-v4-flash", "deepseek-v4-flash-0731",
                      "DeepSeek-V4-Flash", "deepseek-ai/DeepSeek-V4-Flash",
                      "Pro/deepseek-ai/DeepSeek-V4-Flash"):
            with self.subTest(model=model):
                self.assertTrue(_is_deepseek_v4_flash(model))
        for model in ("deepseek-flash", "deepseek-v4-pro", "deepseek-ai/DeepSeek-V4-Pro",
                      "deepseek-v3.2", "Qwen/Qwen3-8B", ""):
            with self.subTest(model=model):
                self.assertFalse(_is_deepseek_v4_flash(model))

    def test_official_endpoint_uses_jsonl_chat(self):
        """官方端点不受前缀修复影响：仍走 Responses。"""
        client = _Client()
        llm = self._llm(client, base_url="https://api.deepseek.com/v1",
                        model="deepseek-ai/DeepSeek-V4-Flash")

        llm.generate_structured(SCHEMA, [{"role": "user", "content": "Return JSON."}])

        self.assertEqual(len(client.responses.calls), 0)
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_thinking_disabled_on_compatible_provider_still_uses_chat(self):
        """关思考的兼容服务模型同样优先 chat，避免 Responses 404。

        硅基流动实测同时接受 ``thinking={"type": "disabled"}``，效果与
        ``enable_thinking=false`` 一致（reasoning_tokens 归零）。
        """
        client = _Client()
        llm = self._llm(client, base_url="https://api.siliconflow.cn/v1",
                        model="deepseek-ai/DeepSeek-V4-Flash")

        llm._create([], response_format={"type": "json_object"})

        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertEqual(len(client.responses.calls), 0)
        self.assertEqual(client.chat.completions.calls[0]["extra_body"],
                         {"thinking": {"type": "disabled"}})


if __name__ == "__main__":
    unittest.main()
