"""Decoder cycles and split retries cannot exhaust unbounded document work."""
import json
from types import SimpleNamespace

import pytest

from ..streaming import consume_entity_stream
from ..request_budget import RecoveryBudget, fair_source_windows
from ..chunk_context import ExtractionChunk
from ..llm_client import OpenAICompatibleLLM


class Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = False
        self.read = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.read += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


def delta(content='', **extra):
    return {'id': 'fixture', 'choices': [{'delta': {'content': content, **extra}}]}


def entity(name='通知', quote='通知', **extra):
    return json.dumps({'type': 'Action', 'mention': name, 'evidence': {'quote': quote}, **extra},
                      ensure_ascii=False) + '\n'


def test_full_record_cycle_stops_before_end_and_keeps_received_records():
    records = [entity(name) for name in ('报送', '审批', '通知')]
    stream = Stream(delta(text) for text in records * 100)
    body = consume_entity_stream(stream)
    assert stream.closed and stream.read == 15
    assert body['status'] == 'incomplete'
    assert body['incomplete_details']['reason'] == 'repetitive_entity_output'
    assert body['usage'] is None
    assert len(body['choices'][0]['message']['content'].splitlines()) == 15


def test_identical_name_with_different_properties_or_evidence_is_progress():
    chunks = [delta(entity(properties={'condition': str(i)}, quote=f'第{i}项通知')) for i in range(40)]
    chunks += [{'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
               {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}]
    stream = Stream(chunks)
    body = consume_entity_stream(stream)
    assert stream.closed and body['status'] == 'completed'
    assert body['usage']['total_tokens'] == 30


def test_consecutive_empty_quotes_stop_even_when_names_are_unique():
    stream = Stream(delta(entity(str(i), quote='')) for i in range(100))
    body = consume_entity_stream(stream)
    assert stream.read == 8
    assert body['incomplete_details']['reason'] == 'consecutive_empty_evidence'


def test_network_interruption_retains_received_body_without_exception_text():
    stream = Stream([delta(entity()), RuntimeError('Authorization private fixture')])
    body = consume_entity_stream(stream)
    assert body['incomplete_details']['reason'] == 'stream_interrupted'
    assert 'private' not in json.dumps(body)
    assert json.loads(body['choices'][0]['message']['content'])['mention'] == '通知'


def test_no_output_transport_failure_propagates_and_closes():
    stream = Stream([TimeoutError('fixture timeout')])
    with pytest.raises(TimeoutError):
        consume_entity_stream(stream)
    assert stream.closed


def test_close_failure_keeps_output_and_is_incomplete_without_leaking_exception():
    stream = Stream([delta(entity()), {'choices': [{'delta': {}, 'finish_reason': 'stop'}]}])
    def broken_close():
        raise RuntimeError('Authorization private fixture')
    stream.close = broken_close
    body = consume_entity_stream(stream)
    assert body['incomplete_details']['reason'] == 'stream_close_failed'
    assert 'private' not in json.dumps(body)


def test_completeness_counters_report_missing_fields_without_inventing_values():
    from ..quality_metrics import completeness_metrics
    plan = SimpleNamespace(class_specs={
        'Document': SimpleNamespace(properties=[SimpleNamespace(identifier='title'), SimpleNamespace(identifier='number')]),
        'Tag': SimpleNamespace(properties=[]),
    })
    metrics = completeness_metrics([
        {'type': 'Document', 'properties': {'title': '通知', 'number': ''}},
        {'type': 'Document', 'properties': {}},
        {'type': 'Tag', 'properties': {}},
    ], plan)
    assert metrics['entities_without_properties'] == 1
    assert metrics['filled_property_slots'] == 1
    assert metrics['eligible_property_slots'] == 4
    assert metrics['schema_fill_rate'] == .25


def test_clean_socket_end_without_finish_is_not_complete():
    body = consume_entity_stream(Stream([delta(entity())]))
    assert body['status'] == 'incomplete'
    assert body['incomplete_details']['reason'] == 'stream_ended_without_finish'


def test_reasoning_is_retained_and_counts_toward_stream_size_guard():
    stream = Stream([delta(reasoning_content='x' * 20), delta(entity())])
    body = consume_entity_stream(stream, character_limit=10)
    assert stream.read == 1 and body['choices'][0]['message']['reasoning_content'] == 'x' * 20
    assert body['incomplete_details']['reason'] == 'stream_character_budget'


def test_fragmented_lines_are_checked_after_complete_record():
    text = entity()
    chunks = [delta(text[:12]), delta(text[12:])] * 100
    stream = Stream(chunks)
    body = consume_entity_stream(stream)
    assert stream.read == 26 and body['streaming']['complete_records'] == 13


def test_original_sources_receive_round_robin_turns_and_keep_offsets():
    chunks = [ExtractionChunk('table', 'a' * 1600), ExtractionChunk('body', '关键正文')]
    parts = list(fair_source_windows(chunks, 300))
    assert [p.chunk_id for p in parts[:2]] == ['table', 'body']
    for part in parts:
        source = next(c for c in chunks if c.chunk_id == part.chunk_id)
        start = part.metadata.get('_v31_window_start', 0)
        assert source.text[start:start + len(part.text)] == part.text


def test_request_fits_keeps_byte_and_model_token_limits_separate(monkeypatch):
    from ..request_budget import fits
    monkeypatch.setenv('KG_EXTRACT_V31_MAX_INPUT_BYTES', '48000')
    class Counter:
        max_input_tokens = 100
        max_input_bytes = 48000
        tokens = 99
        def measure_input(self, payload):
            return SimpleNamespace(tokens=self.tokens)
    counter = Counter()
    messages = [{'role': 'user', 'content': '中文原文' * 100}]
    assert fits(messages, counter)
    counter.tokens = 101
    assert not fits(messages, counter)


def test_recovery_allowance_does_not_reset_for_children():
    budget = RecoveryBudget(2, branches=4)
    children = [budget, budget, budget]
    for i in range(9):
        assert children[i % 3].claim()
    assert not children[0].claim()
    assert budget.trace()['logical_attempt'] == budget.trace()['logical_attempt_limit'] == 9


@pytest.mark.parametrize('field,fast', [('文件标题', True), ('来文字号', True),
    ('拟办意见', False), ('领导批示', False), ('antecedent', False)])
def test_only_explicit_document_fields_use_short_semantic_review(field, fast):
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return {'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]}
    client = OpenAICompatibleLLM(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        model='deepseek-v4-flash-0731', max_tokens=16384, disable_thinking=True,
        fast_verification=True, verification_max_tokens=4096)
    messages = [{'role': 'user', 'content': json.dumps({'task': {'items': [{
        'type': 'OfficialDocument', 'property': field, 'value': '原文值',
        'evidence_quotes': ['文件标题：原文值']}]}}, ensure_ascii=False)}]
    client.generate_structured({}, messages, {'pass_name': 'property_verification'})
    # Support checks follow the regular switch, so thinking stays off either way;
    # the fast path is what still separates explicit header fields from the rest.
    assert calls[0]['max_tokens'] == (4096 if fast else 16384)
    assert calls[0]['extra_body']['thinking']['type'] == 'disabled'


def test_document_conflict_keeps_full_semantic_budget():
    from ..llm_client import _explicit_document_fields
    messages = [{'content': json.dumps({'task': {'items': [{'type': 'OfficialDocument',
        'property': '收文日期', 'value': '2026-10-14', 'evidence_quotes': ['日期'],
        'conflicting_values': ['2026-10-13', '2026-10-14']}]}})}]
    assert not _explicit_document_fields(messages, {'pass_name': 'property_verification'})


def test_mixed_document_review_keeps_short_fields_out_of_long_thinking_batch():
    from ..property_grounding import ground_properties
    calls = []
    def create(**kwargs):
        items = json.loads(kwargs['messages'][1]['content'])['task']['items']
        calls.append((items, kwargs))
        body = '\n'.join(json.dumps({'item_id': item['item_id'], 'supported': 'yes',
                                    'reason': '原文逐项支持'}) for item in items)
        return {'choices': [{'message': {'content': body}, 'finish_reason': 'stop'}]}
    client = OpenAICompatibleLLM(SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))),
        model='deepseek-v4-flash-0731', max_tokens=16384, disable_thinking=False,
        fast_verification=True, verification_max_tokens=4096)
    text = '文件标题：测试通知\n领导批示：请协调落实\n联系电话：12345678'
    row = {'type': 'OfficialDocument', 'name': '测试通知',
           'properties': {'文件标题': '测试通知', '领导批示': '请协调落实', '联系电话': '12345678'},
           'evidence': {'quote': text, 'property_quotes': {
               '文件标题': ['文件标题：测试通知'], '领导批示': ['领导批示：请协调落实'],
               '联系电话': ['联系电话：12345678']}}}
    attempts = []
    ground_properties([row], [ExtractionChunk('source', text)], client,
        attempts=attempts, warnings=[], max_attempts=1, retry_base_seconds=0)
    assert not attempts
    assert len(calls) == 3
    assert list(row['property_evidence']) == ['文件标题', '领导批示', '联系电话']
    assert [[i['property'] for i in items] for items, _ in calls] == [
        ['文件标题'], ['领导批示'], ['联系电话']]
    assert [kwargs['max_tokens'] for _, kwargs in calls] == [4096, 16384, 4096]
    # Ordinary fields now inherit the regular switch (enabled here), while the
    # short-field fast path stays disabled instead of sharing their batch.
    assert [kwargs['extra_body']['thinking']['type'] for _, kwargs in calls] == [
        'disabled', 'enabled', 'disabled']


def test_split_verification_shares_attempt_allowance_instead_of_resetting():
    from ..property_grounding import ground_properties
    calls, attempts = [], []
    def unavailable(messages):
        calls.append(messages)
        raise TimeoutError('fixture unavailable')
    row = {'type': 'Record', 'name': '甲', 'properties': {f'p{i}': str(i) for i in range(4)},
           'evidence': {'quote': '甲有四项字段0、1、2、3。'}}
    ground_properties([row], [ExtractionChunk('source', row['evidence']['quote'])], unavailable,
        attempts=attempts, warnings=[], max_attempts=2, retry_base_seconds=0)
    assert len(calls) <= 9
    exhausted = [a for a in attempts if a['type'] == 'recovery_budget']
    assert exhausted and all(a['logical_attempt'] == a['logical_attempt_limit'] for a in exhausted)
    assert row['properties'] == {}
