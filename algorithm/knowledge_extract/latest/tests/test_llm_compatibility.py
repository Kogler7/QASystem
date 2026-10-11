from __future__ import annotations

from types import SimpleNamespace
import unittest

from algorithm.knowledge_extract.latest.llm_client import OpenAICompatibleLLM


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

    def test_official_deepseek_responses_explicitly_disables_thinking(self):
        client = _Client()
        llm = self._llm(client, base_url="https://api.deepseek.com/v1")

        llm._responses_create([{"role": "user", "content": "{}"}], response_format={
            "type": "json_schema", "name": "test", "strict": True, "schema": {
                "type": "object", "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"], "additionalProperties": False,
            },
        })

        self.assertEqual(client.responses.calls[0]["reasoning"], {"effort": "none"})

    def test_non_official_responses_preserves_provider_defaults(self):
        client = _Client()
        llm = self._llm(client, base_url="https://api.siliconflow.cn/v1")

        llm._responses_create([], response_format={"type": "json_object"})

        self.assertNotIn("reasoning", client.responses.calls[0])

    def test_official_v4_record_table_uses_jsonl_chat(self):
        client = _Client()
        llm = self._llm(client, base_url="https://api.deepseek.com/v1")
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
            "additionalProperties": False,
        }

        result = llm.generate_structured(
            schema,
            [{"role": "user", "content": "Return JSON."}],
            {"record_table": True, "schema_name": "record_table"},
        )

        self.assertEqual(result.api, "chat.completions")
        self.assertEqual(result.response_mode, "jsonl")
        self.assertNotIn("response_format", client.chat.completions.calls[0])
        self.assertEqual(len(client.responses.calls), 0)
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_response_format_error_does_not_retry_without_thinking(self):
        error = RuntimeError("response_format json_schema is unsupported")
        completions = _Completions(error=error)
        client = _Client(chat=completions)
        llm = self._llm(client, base_url="https://api.siliconflow.cn/v1")

        with self.assertRaisesRegex(RuntimeError, "response_format"):
            llm._create([], response_format={"type": "json_schema"})

        self.assertEqual(len(completions.calls), 1)

    def test_thinking_error_retries_without_thinking_toggle(self):
        completions = _Completions(error=[RuntimeError("thinking parameter unsupported"), None])
        client = _Client(chat=completions)
        llm = self._llm(client, base_url="https://api.siliconflow.cn/v1")

        llm._create([])

        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(completions.calls[0]["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("extra_body", completions.calls[1])


    def test_organization_prefixed_v4_flash_uses_chat(self):
        """带组织前缀的 V4-Flash（如硅基流动）也必须走 chat.completions。

        兼容服务没有 /v1/responses，整串前缀匹配会让请求打到那里拿 404。
        """
        client = _Client()
        llm = self._llm(client, base_url="https://api.siliconflow.cn/v1",
                        model="deepseek-ai/DeepSeek-V4-Flash")
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "items": {"type": "string"}}},
            "required": ["items"],
            "additionalProperties": False,
        }

        result = llm.generate_structured(schema, [{"role": "user", "content": "Return JSON."}],
                                         {"schema_name": "extraction"})

        self.assertEqual(result.api, "chat.completions")
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertEqual(len(client.responses.calls), 0)

    def test_v4_flash_matching_ignores_organization_prefix(self):
        from algorithm.knowledge_extract.latest.llm_client import _is_deepseek_v4_flash

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

        llm.generate_structured(
            {"type": "object", "properties": {"ok": {"type": "boolean"}},
             "required": ["ok"], "additionalProperties": False},
            [{"role": "user", "content": "Return JSON."}],
        )

        self.assertEqual(len(client.responses.calls), 0)


if __name__ == "__main__":
    unittest.main()
