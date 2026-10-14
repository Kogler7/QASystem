"""Complete provider object streams normalize without extracting nested data."""
import json
from types import SimpleNamespace

import pytest

from ..candidate_generation import _jsonl_rows, decode_jsonl_entities
from ..llm_client import StructuredLLMResponse
from ..output_contracts import verification_schema
from .test_output_recovery import generate

ROWS = [{'item_id': 'first', 'supported': 'yes', 'reason': '原文含括号 }{，保留\\n和引号"。'},
        {'item_id': 'second', 'supported': 'no', 'reason': '否定与条件不符合'}]


@pytest.mark.parametrize('separator', ['', '\n', '\n\n', ' \t\r\n'])
@pytest.mark.parametrize('indent', [None, 2])
def test_live_complete_objects_normalize_without_semantic_changes(separator, indent):
    text = separator.join(json.dumps(row, ensure_ascii=False, indent=indent) for row in ROWS)
    normalized = []
    rows, errors = _jsonl_rows(text, 'verdicts', normalizations=normalized)
    assert rows == ROWS and not errors
    if indent is not None or '\n' not in separator:
        assert normalized == ['complete_object_stream']


def test_live_pretty_verdicts_finish_in_one_call_and_keep_no_verdict():
    text = ''.join(json.dumps(row, ensure_ascii=False, indent=2) for row in ROWS)
    result, attempts, calls = generate([text], verification_schema(ROWS))
    assert result == {'verdicts': ROWS} and not attempts
    assert calls[0]['response_normalizations'] == ['complete_object_stream']


def test_strict_decoder_still_rejects_multiline_or_adjacent_objects():
    row = {'type': 'Person', 'mention': '甲', 'evidence': {'quote': '甲'}}
    for text in [json.dumps(row, indent=2), json.dumps(row) + json.dumps(row)]:
        parsed, error = decode_jsonl_entities(text)
        assert parsed is None and error


@pytest.mark.parametrize('text', [
    '解释\n' + json.dumps(ROWS[0], indent=2),
    json.dumps(ROWS[0], indent=2) + '\n说明',
    json.dumps(ROWS[0], indent=2) + ',\n' + json.dumps(ROWS[1], indent=2),
    json.dumps(ROWS[0], indent=2) + '\n{"item_id":',
    '{\n"item_id":"first",\n"supported":"no",\n"supported":"yes",\n"reason":"x"\n}',
    '{\n"item_id":"first",\n"supported":"yes",\n"reason":"x",\n"value":NaN\n}',
    '{"item_id":"first","supported":"yes","reason":"x","value":Infinity}',
    '{"item_id":"first","supported":"yes","reason":"x","value":-Infinity}',
    '{"item_id":"first","supported":"yes","reason":"x","value":1e999}',
    '{"item_id":"first","supported":"yes","reason":"x","extra":{"a":1,"a":2}}',
    json.dumps(ROWS[0], indent=2) + '\n{}\n' + json.dumps(ROWS[1], indent=2),
    json.dumps(ROWS[0], indent=2) + '\n[]',
])
def test_live_invalid_stream_never_becomes_successful_format_normalization(text):
    normalized = []
    _, errors = _jsonl_rows(text, 'verdicts', normalizations=normalized)
    assert errors and 'complete_object_stream' not in normalized


@pytest.mark.parametrize('text', [
    '[\n' + json.dumps(ROWS[0]) + '\n]',
    '{"verdicts":[\n' + json.dumps(ROWS[0]) + '\n],"extra":1}',
    '{"verdicts":[\n' + json.dumps(ROWS[0]) + '\n]}\n' + json.dumps(ROWS[1]),
])
def test_live_collection_envelopes_cannot_expose_nested_valid_records(text):
    rows, errors = _jsonl_rows(text, 'verdicts', normalizations=[])
    assert not rows and errors


def test_live_only_reads_final_text_not_reasoning():
    response = SimpleNamespace(text='invalid final output',
                               reasoning_content=json.dumps(ROWS[0], indent=2))
    rows, errors = _jsonl_rows(response, 'verdicts', normalizations=[])
    assert not rows and errors


def test_pretty_format_does_not_hide_provider_truncation_metadata():
    response = StructuredLLMResponse(text=json.dumps(ROWS[0], indent=2), api='chat.completions',
        response_mode='plain', status='incomplete', finish_reason='length')
    result, attempts, _ = generate([response], verification_schema([ROWS[0]]))
    assert result == {'verdicts': [ROWS[0]]}
    assert attempts[-1]['terminal'] and attempts[-1]['type'] == 'incomplete'


def test_pretty_fenced_entity_with_final_marker_has_only_transport_normalizations():
    row = {'type': 'Person', 'mention': '甲', 'properties': {}, 'evidence': {'quote': '甲'}}
    text = '```json\n' + json.dumps(row, indent=2) + '\n{}\n```'
    result, attempts, calls = generate([text])
    assert result == {'entities': [row]} and not attempts
    assert calls[0]['response_normalizations'] == [
        'complete_markdown_fence', 'complete_object_stream', 'final_empty_marker']
