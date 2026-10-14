"""Root-key duplication is detected incrementally, never repaired into facts."""
import json

import pytest

from ..streaming import RootObjectKeys, consume_entity_stream
from .test_streaming_recovery import Stream, delta


@pytest.mark.parametrize('key', ['type', 'mention', 'evidence', '属性'])
def test_repeated_root_key_stops_at_confirming_colon_without_waiting_for_value(key):
    first = json.dumps(key, ensure_ascii=False)
    prefix = '{' + first + ':"original",' + first + ':'
    chunks = [delta(char) for char in prefix] + [delta('"oversized continuation"' * 100)]
    stream = Stream(chunks)
    body = consume_entity_stream(stream)
    assert stream.read == len(prefix) and stream.closed
    assert body['status'] == 'incomplete'
    assert body['incomplete_details']['reason'] == 'repetitive_malformed_object'
    assert body['choices'][0]['finish_reason'] == 'cancelled'
    assert body['choices'][0]['message']['content'] == prefix
    assert body['usage'] is None and body['streaming']['usage_unavailable']


@pytest.mark.parametrize('prefix', [
    r'{"type":"Document","\u0074ype":',
    r'{"a\"b":1,"a\u0022b":',
    r'{"a\\b":null,"a\u005cb":',
    r'{"文号":true,"\u6587\u53f7":',
])
def test_key_escape_equivalence_survives_every_delta_boundary(prefix):
    guard = RootObjectKeys()
    assert all(guard.feed(char) is None for char in prefix[:-1])
    assert guard.feed(prefix[-1]) == 'repetitive_malformed_object'


def test_repeated_key_without_a_colon_is_not_declared_a_duplicate_member():
    guard = RootObjectKeys()
    assert guard.feed('{"type":"A","type"') is None
    assert guard.feed(' \n\t') is None
    assert guard.feed(':') == 'repetitive_malformed_object'


@pytest.mark.parametrize('separator', ['', '\n', '\n\r\t '])
def test_adjacent_and_jsonl_root_records_reset_the_key_scope(separator):
    records = [
        {'type': 'Document', 'mention': '通知', 'evidence': {'quote': '标题'}},
        {'type': 'Action', 'mention': '发送', 'evidence': {'quote': '发送通知'}},
        {},
        {'type': 'Entity', 'properties': {'flag': False, 'empty': None, 'number': -1.25e-12}},
    ]
    text = separator.join(json.dumps(row, ensure_ascii=False, indent=2) for row in records)
    guard = RootObjectKeys()
    assert all(guard.feed(char) is None for char in text)
    assert not guard.disabled


@pytest.mark.parametrize('record', [
    {'type': 'Outer', 'properties': {'type': 'Inner', 'evidence': {'type': 'Deep'}}, 'mention': '真实'},
    {'type': 'Outer', 'properties': [{'type': 'A'}, {'type': 'B'}], 'evidence': {'quote': '真实'}},
    {'type': 'Outer', 'evidence': {'quote': '原文含 "type": "A", "type": "B"，以及 }{[]'}},
    {'type': 'Outer', 'evidence': {'quote': '\\"type\\":\n"quoted", \\ end'}},
    {'type': 'Outer', 'Type': 'Distinct', 'ｔｙｐｅ': 'Distinct unicode key'},
])
def test_nested_scopes_quoted_keys_and_escape_sequences_are_not_root_duplicates(record):
    text = json.dumps(record, ensure_ascii=False, indent=2)
    stream = Stream([*(delta(char) for char in text),
                     {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}])
    body = consume_entity_stream(stream)
    assert body['status'] == 'completed' and body['choices'][0]['message']['content'] == text


def test_nested_values_then_root_duplication_is_detected():
    prefix = '{"type":"A","properties":{"type":"B","values":[{"type":"C"}]},"type":'
    guard = RootObjectKeys()
    for char in prefix[:-1]:
        assert guard.feed(char) is None
    assert guard.feed(':') == 'repetitive_malformed_object'


def test_primitive_values_complete_before_next_root_key():
    prefix = '{"number":-1.2e+3,"flag":false,"empty":null,"number":'
    guard = RootObjectKeys()
    assert guard.feed(prefix) == 'repetitive_malformed_object'


def test_reasoning_does_not_feed_the_root_key_guard_or_create_content():
    thought = '{"type":"A","type":"B"}'
    text = '{"type":"Document","mention":"通知","evidence":{"quote":"通知"}}'
    stream = Stream([delta(reasoning_content=thought), delta(text),
                     {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}])
    body = consume_entity_stream(stream)
    assert body['status'] == 'completed'
    assert body['choices'][0]['message'] == {'content': text, 'reasoning_content': thought}


def test_stop_keeps_received_delta_and_earlier_valid_record_without_repair():
    first = '{"type":"Document","mention":"通知","evidence":{"quote":"通知"}}\n'
    broken = '{"type":"A","type":"B","mention":"untrusted"}'
    stream = Stream([delta(first), delta(broken), delta('not received')])
    body = consume_entity_stream(stream)
    assert stream.read == 2 and body['choices'][0]['message']['content'] == first + broken
    assert body['status'] == 'incomplete'
    assert body['incomplete_details']['reason'] == 'repetitive_malformed_object'
    assert body['streaming']['complete_records'] == 1


@pytest.mark.parametrize('text', [
    '说明：{"type":"A","type":"B"}',
    '[{"type":"A"},{"type":"B"}]',
    '{"type" missing colon, "type": "B"}',
    '{"type": invalid value, "type":"B"}',
    '{"type":"A", "bad\\q":"x", "type":"B"}',
])
def test_ambiguous_or_invalid_prefix_disables_detection_instead_of_guessing(text):
    guard = RootObjectKeys()
    assert guard.feed(text) is None and guard.disabled
