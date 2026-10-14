"""One exact verification wrapper is transport syntax, not a weaker verdict contract."""
import json

import pytest

from ..candidate_generation import _jsonl_rows, decode_jsonl_entities
from ..llm_client import StructuredLLMResponse
from ..output_contracts import verification_schema
from .test_jsonl_object_stream import ROWS
from .test_output_recovery import generate


@pytest.mark.parametrize('indent', [None, 2, 4])
@pytest.mark.parametrize('escaped_key', [False, True])
def test_live_exact_verdict_envelope_preserves_all_values_and_finishes_in_one_call(indent, escaped_key):
    text = json.dumps({'verdicts': ROWS}, ensure_ascii=False, indent=indent)
    if escaped_key:
        text = text.replace('"verdicts"', r'"\u0076erdicts"', 1)
    result, attempts, calls = generate([' \n' + text + '\t '], verification_schema(ROWS))
    assert result == {'verdicts': ROWS} and not attempts
    assert calls[0]['response_normalizations'] == ['complete_verdicts_envelope']


@pytest.mark.parametrize('text', [
    json.dumps({'verdicts': ROWS, 'extra': 'not allowed'}, indent=2),
    '{"extra":true,"verdicts":[\n' + json.dumps(ROWS[0]) + '\n],"extra":false}',
    '{"verdicts":[\n' + json.dumps(ROWS[0]) + '\n],"verdicts":[]}',
    '{"verdicts":[\n' + json.dumps(ROWS[0]) + '\n,{"item_id":"second","supported":"yes","supported":"no"}]}',
    '{"verdicts":[\n' + json.dumps(ROWS[0]) + '\n,{"item_id":"second","value":NaN}]}',
    '{"verdicts":[\n' + json.dumps(ROWS[0]) + '\n,{"item_id":"second","value":Infinity}]}',
    '{"verdicts":[\n' + json.dumps(ROWS[0]) + '\n,{"item_id":"second","value":1e999}]}',
    '{"verdicts":[\n' + json.dumps(ROWS[0]) + '\n',
    json.dumps({'verdicts': ROWS}, indent=2) + '\n说明',
    '说明\n' + json.dumps({'verdicts': ROWS}, indent=2),
    json.dumps({'verdicts': ROWS}) + json.dumps({'verdicts': ROWS}),
    json.dumps({'verdicts': ROWS}) + '\n' + json.dumps(ROWS[0]),
    json.dumps({'verdicts': [ROWS[0], 'string is not a record']}, indent=2),
    json.dumps({'verdicts': {'items': ROWS}}, indent=2),
    '[\n' + json.dumps(ROWS[0]) + '\n]',
])
def test_bad_envelope_fails_atomically_without_exposing_nested_verdict_rows(text):
    normalizations = []
    rows, errors = _jsonl_rows(text, 'verdicts', normalizations=normalizations)
    assert rows == [] and errors
    assert 'complete_verdicts_envelope' not in normalizations


def test_wrapper_does_not_authorize_duplicate_or_unknown_item_ids():
    data = [ROWS[0], {**ROWS[0], 'supported': 'no'}, {**ROWS[1], 'item_id': 'foreign'}]
    result, attempts, _ = generate([json.dumps({'verdicts': data})], verification_schema(ROWS))
    assert result is None and attempts[-1]['terminal']


def test_wrapper_does_not_authorize_missing_ids_or_extra_record_fields():
    data = [ROWS[0], {**ROWS[1], 'extra': 'not allowed'}]
    result, attempts, _ = generate([json.dumps({'verdicts': data})], verification_schema(ROWS))
    assert result == {'verdicts': [ROWS[0]]}
    assert attempts[-1]['terminal'] and attempts[-1]['missing_item_ids'] == ['second']


def test_nested_wrapper_is_not_recursively_unwrapped():
    result, attempts, _ = generate([json.dumps({'verdicts': [{'verdicts': ROWS}]})], verification_schema(ROWS))
    assert result is None and attempts[-1]['terminal']


def test_complete_wrapper_does_not_hide_provider_incomplete_status():
    response = StructuredLLMResponse(text=json.dumps({'verdicts': ROWS}), api='chat.completions',
        response_mode='plain', status='incomplete', finish_reason='length')
    result, attempts, _ = generate([response], verification_schema(ROWS))
    assert result == {'verdicts': ROWS}
    assert attempts[-1]['terminal'] and attempts[-1]['type'] == 'incomplete'


def test_strict_paths_and_entity_wrappers_remain_rejected():
    rows, errors = _jsonl_rows(json.dumps({'verdicts': ROWS}), 'verdicts')
    assert rows == [] and errors
    row = {'type': 'Person', 'mention': '甲', 'evidence': {'quote': '甲'}}
    text = json.dumps({'entities': [row]}, indent=2)
    rows, errors = _jsonl_rows(text, 'entities', normalizations=[])
    assert rows == [] and errors
    result, error = decode_jsonl_entities(text)
    assert result is None and error


def test_collection_word_inside_quotes_or_nested_properties_is_not_a_root_wrapper():
    row = {**ROWS[0], 'reason': '引用 "verdicts": [ 字样，不能充当封套'}
    rows, errors = _jsonl_rows(json.dumps(row, ensure_ascii=False), 'verdicts', normalizations=[])
    assert rows == [row] and not errors
    entity = {'type': 'Person', 'mention': '甲', 'properties': {'entities': '原文值'}}
    rows, errors = _jsonl_rows(json.dumps(entity), 'entities', normalizations=[])
    assert rows == [entity] and not errors
