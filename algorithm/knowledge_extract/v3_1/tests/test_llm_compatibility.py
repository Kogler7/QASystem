from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from algorithm.knowledge_extract.v3_1.llm_client import OpenAICompatibleLLM, _is_deepseek_v4_flash, create_llm_client


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
    def test_factory_keeps_generation_sampling_out_of_sdk_constructor(self):
        # The factory is exercised without inheriting workstation proxy/auth
        # settings; this test never contacts the configured endpoint.
        with patch.dict('os.environ', {'KG_EXTRACT_V31_GENERATION_TEMPERATURE': '0.7'}, clear=True):
            llm = create_llm_client(base_url='http://127.0.0.1:18090/v1',
                api_key='not-needed', model='deepseek-v4-flash-0731')
        self.assertEqual(llm.generation_temperature, 0.7)
        client = _Client()
        llm.client.close()
        llm.client = client
        llm.generate_structured(SCHEMA, [{'role': 'user', 'content': 'source'}], {'pass_name': 'entity'})
        self.assertEqual(client.chat.completions.calls[0]['temperature'], 0.7)

    def test_generation_temperature_does_not_change_verification_or_selection(self):
        client = _Client()
        llm = OpenAICompatibleLLM(client, model='deepseek-v4-flash-0731',
            max_tokens=128, disable_thinking=True, generation_temperature=0.7)
        stages = ('entity', 'repair_entity', 'property', 'repair_property',
                  'candidate_identity', 'entity_verification', 'property_verification', 'skill_selection')
        for stage in stages:
            llm.generate_structured(SCHEMA, [{'role': 'user', 'content': 'source'}], {'pass_name': stage})
        self.assertEqual([c['temperature'] for c in client.chat.completions.calls], [0.7]*5+[0]*3)

    def test_invalid_generation_temperature_fails_before_network(self):
        for value in (-0.1, 2.1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                OpenAICompatibleLLM(_Client(), model='x', max_tokens=1,
                    disable_thinking=True, generation_temperature=value)

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

    def test_official_endpoint_uses_plain_chat_without_json_mode(self):
        """官方端点也使用无 response_format 的普通 chat。"""
        client = _Client()
        llm = self._llm(client, base_url="https://api.deepseek.com/v1",
                        model="deepseek-ai/DeepSeek-V4-Flash")

        llm.generate_structured(SCHEMA, [{"role": "user", "content": "Return JSON."}])

        self.assertEqual(len(client.responses.calls), 0)
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertNotIn("response_format", client.chat.completions.calls[0])

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
