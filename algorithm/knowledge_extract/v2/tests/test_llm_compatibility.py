from __future__ import annotations

from types import SimpleNamespace
import unittest

from algorithm.knowledge_extract.v2.llm_client import OpenAICompatibleLLM


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

    def test_official_v4_record_table_goes_directly_to_responses(self):
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

        self.assertEqual(result.api, "responses")
        self.assertEqual(len(client.responses.calls), 1)
        self.assertEqual(len(client.chat.completions.calls), 0)

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


if __name__ == "__main__":
    unittest.main()
