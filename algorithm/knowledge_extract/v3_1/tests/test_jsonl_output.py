"""JSONL output is plain text transport with strict local validation."""
import json

import pytest

from ..candidate_generation import decode_jsonl_entities, generate_structured
from ..ontology_compiler import compile_plan
from ..llm_client import StructuredLLMResponse
from .test_llm_compatibility import _Client, LLMCompatibilityTests

ROW = {'type': 'Person', 'mention': '张三', 'evidence': {'quote': '张三\n负责检查'}}
LINE = json.dumps(ROW, ensure_ascii=False)


def test_jsonl_preserves_multiple_rows_and_escaped_newlines():
    assert decode_jsonl_entities(LINE + '\n\n' + LINE) == ({'entities': [ROW, ROW]}, None)
    assert decode_jsonl_entities('{}') == ({'entities': []}, None)


@pytest.mark.parametrize('text', ['', '[]', '{"entities": []}', '```jsonl\n' + LINE + '\n```',
                                  LINE + '\n{"type":', LINE + '\n{}', '说明\n' + LINE])
def test_invalid_jsonl_never_silently_returns_partial_results(text):
    parsed, error = decode_jsonl_entities(text)
    assert parsed is None and error


@pytest.mark.parametrize('mode', ['json_schema', 'json_object', 'plain'])
def test_even_legacy_context_cannot_enable_json_mode(mode):
    client = _Client()
    llm = LLMCompatibilityTests._llm(client, base_url='https://api.siliconflow.cn/v1')
    llm.generate_structured({}, [{'role': 'user', 'content': '返回 JSONL'}], {'response_mode': mode})
    assert len(client.chat.completions.calls) == 1
    assert 'response_format' not in client.chat.completions.calls[0]
    assert client.responses.calls == []


def test_retry_is_plain_and_rejects_truncated_response():
    schema = compile_plan('@startuml\nclass Person\n@enduml').output_schemas['entity_candidates']
    contexts = []
    class LLM:
        def generate_structured(self, schema, messages, run_context):
            contexts.append(run_context)
            if len(contexts) == 1:
                return StructuredLLMResponse(text=LINE, api='chat.completions', response_mode='plain',
                                             status='incomplete', finish_reason='length')
            return LINE
    attempts = []
    result = generate_structured(LLM(), [{'role': 'system', 'content': 'JSONL'},
                                 {'role': 'user', 'content': '{"task": {"operation": "entity"}}'}],
                                 schema, use_scheduler=False, attempts=attempts, pass_name='entity',
                                 chunk_id='c1', max_attempts=2, retry_base_seconds=0)
    assert result == {'entities': [ROW]}
    assert attempts[0]['type'] == 'incomplete'
    assert [c['response_mode'] for c in contexts] == ['plain', 'plain']
