"""Regression tests for P1/P2, first-pass properties and bounded long inputs."""

from .stub_llm import source_documents
import json
import re
import threading

import pytest

from ..chunk_context import ExtractionChunk
from ..split_extractor import extract_split
from ..request_budget import message_bytes, windows
from .stub_llm import StubLLM, span_item

ONTOLOGY = '@startuml\nclass Person {\n + 职责 (duty): String\n}\n@enduml'


def extract(chunks, llm, **kwargs):
    # These fixtures exercise extraction budgets, with skill selection explicitly unbound.
    kwargs.setdefault('skill_refs', [])
    return extract_split(ONTOLOGY, chunks, llm=llm, retry_base_seconds=0, **kwargs)


def candidate(name, duty=None, quote=None):
    return {**span_item('Person', name, quote), 'properties': {'duty': duty} if duty else {}}


@pytest.mark.parametrize('quote', ['张三', '张三批准发射火箭。'])
@pytest.mark.parametrize('stage', ['entity', 'property'])
def test_name_or_forged_quote_cannot_certify_a_property(quote, stage):
    llm = StubLLM(entity_handler=lambda p, c: [candidate('张三', '批准发射火箭', quote)]
        if stage == 'entity' else [span_item('Person', '张三')],
        property_handler=lambda p, c: {'entities': [{'type': 'Person', 'name': '张三',
            'properties': {'duty': '批准发射火箭'}, 'evidence': {'quote': quote}}]})
    result = extract([ExtractionChunk('c', '张三负责检查。')], llm)
    assert result['raw']['entities'][0]['properties'] == {}
    assert any(w['code'] == 'PROPERTY_EVIDENCE_UNSUPPORTED' for w in result['warnings'])
    # Entity admission still runs; unsupported property quotes never reach review.
    assert all('property' not in item for t, p in llm.calls if t == 'evidence_verification'
               for item in json.loads(p)['task']['items'])


@pytest.mark.parametrize('verdict', ['no', 'uncertain', 'yes'])
def test_semantic_property_requires_explicit_support(verdict):
    llm = StubLLM(entity_handler=lambda p, c: [candidate('张三', '完成安全核查', c['text'])],
                  verification_handler=lambda item, chunk: verdict,
                  entity_verification_handler=lambda item, chunk: 'yes')
    result = extract([ExtractionChunk('c', '张三负责检查。')], llm)
    assert result['raw']['entities'][0]['properties'] == ({'duty': '完成安全核查'} if verdict == 'yes' else {})
    assert len([t for t, _ in llm.calls if t == 'property']) == 0


@pytest.mark.parametrize('bad', ['missing', 'duplicate'])
def test_missing_and_duplicate_keys_retry_before_acceptance(bad):
    count = 0
    def complete(prompt, chunk):
        nonlocal count
        count += 1
        row = {'type': 'Person', 'name': '张三', 'properties': {'duty': '检查'},
               'evidence': {'quote': '张三负责检查。'}}
        return {'entities': ([] if bad == 'missing' else [row, row]) if count == 1 else [row]}
    llm = StubLLM(entity_handler=lambda p, c: [span_item('Person', '张三')], property_handler=complete,
                  verification_handler=lambda item, chunk: 'yes' if item['value'] == '检查' else 'no', entity_verification_handler=lambda item, chunk: 'yes')
    result = extract([ExtractionChunk('c', '张三负责检查。')], llm)
    assert count == 2
    assert result['status'] == 'succeeded'
    assert result['raw']['entities'][0]['properties'] == {'duty': '检查'}
    assert not any(a['terminal'] for a in result['attempts'])


def test_verified_first_pass_skips_completion_and_preserves_all_sources():
    chunks = [ExtractionChunk(f'c{i}', '张三负责检查。', section_id='s') for i in range(100)]
    llm = StubLLM(entity_handler=lambda p, c: [candidate('张三', '检查', c['text'])],
                  verification_handler=lambda item, chunk: 'yes' if item['value'] == '检查' else 'no', entity_verification_handler=lambda item, chunk: 'yes')
    result = extract(chunks, llm)
    assert result['status'] == 'succeeded'
    assert len([t for t, _ in llm.calls if t == 'entity']) == 100
    assert len([t for t, _ in llm.calls if t == 'evidence_verification']) == 200
    assert result['profile']['property_enrichment']['calls'] == 0
    assert result['profile']['repair_rounds'] == []
    assert not result['coverage_gaps']
    for collection in [result['raw']['entities'][0]['evidences'],
                       result['graph']['nodes'][0]['evidences'],
                       result['intermediate_model']['objects'][0]['evidence']]:
        assert {cid for e in collection for cid in e['source_block_ids']} == {c.chunk_id for c in chunks}


def test_adjacent_pronoun_context_reaches_completion():
    chunks = [ExtractionChunk('a', '张三到岗。', document_id='d', section_id='s'),
              ExtractionChunk('b', '他负责检查并记录。', document_id='d', section_id='s'),
              ExtractionChunk('c', '他负责发射火箭。', document_id='other', section_id='s')]
    def complete(prompt, chunk):
        docs = json.loads(prompt)['documents']
        assert chunks[1].text in docs
        assert chunks[2].text not in docs
        return {'entities': [{'type': 'Person', 'name': '张三', 'properties': {'duty': '检查并记录'},
                'evidence': {'quote': chunks[1].text}}]}
    llm = StubLLM(entity_handler=lambda p, c: [candidate('张三', quote=c['text'])] if '张三' in c['text'] else [],
                  property_handler=complete, verification_handler=lambda item, chunk: 'yes')
    result = extract(chunks, llm, max_repair_rounds=0)
    assert result['raw']['entities'][0]['properties'] == {'duty': '检查并记录'}
    assert {cid for e in result['raw']['entities'][0]['evidences'] for cid in e['source_block_ids']} == {'a', 'b'}


def test_schema_lexical_hit_does_not_hide_other_allowed_types():
    ontology = '@startuml\nclass "人员" as Person\nclass "组织" as Organization\n@enduml'
    def entities(prompt, chunk):
        assert set(json.loads(prompt)['output_contract']['success_schema']['properties']['type']['enum']) == {'Person', 'Organization'}
        return [span_item('Person', '张三'), span_item('Organization', '中车公司')]
    result = extract_split(ontology, [ExtractionChunk('c', '人员张三在中车公司任职。')], llm=StubLLM(entity_handler=entities))
    assert {e['type'] for e in result['raw']['entities']} == {'Person', 'Organization'}


def test_very_long_single_chunk_keeps_tail_and_original_offsets(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '500')
    text = ''.join(f'人员{i:04}负责检查。\n' for i in range(600))
    llm = StubLLM(entity_handler=lambda p, c: [candidate(m.group(1), '检查', m.group())
        for m in re.finditer(r'(人员\d{4})负责检查。', c['text'])])
    result = extract([ExtractionChunk('big', text)], llm)
    assert result['status'] == 'succeeded'
    assert len(result['raw']['entities']) == 600
    assert any(e['name'] == '人员0599' for e in result['raw']['entities'])
    assert all(len(source_documents(json.loads(p))[0]['text']) <= 500 for t, p in llm.calls if t == 'entity')
    for row in result['raw']['entities']:
        for span in row['evidences']:
            assert text[span['start_char']:span['end_char']] == span['quote']
    assert result['profile']['property_enrichment']['calls'] == 0


def test_single_long_chunk_windows_use_bounded_worker_concurrency(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '500')
    barrier = threading.Barrier(2, timeout=2)
    lock = threading.Lock()
    count = 0

    def entities(prompt, chunk):
        nonlocal count
        with lock:
            count += 1
            current = count
        if current <= 2:
            barrier.wait()
        return [candidate('张三', '检查', '张三负责检查。')]

    result = extract([ExtractionChunk('big', '张三负责检查。\n' * 200)],
                     StubLLM(entity_handler=entities), max_workers=2)
    assert result['status'] == 'succeeded'
    assert len(result['raw']['entities']) == 1
    assert not result['attempts']
    assert count > 2


def test_repeated_conflicting_wording_revisits_distinct_support_only():
    text = '张三负责检查。' + '背景说明。' * 1000
    chunks = [ExtractionChunk(f'c{i}', text) for i in range(20)]
    values = set()

    def complete(prompt, chunk):
        payload = json.loads(prompt)
        for row in payload['task']['known_entities'][0]['occurrences']:
            values.add(row['properties']['duty'])
        return {'entities': [{'type': 'Person', 'name': '张三', 'properties': {'duty': '检查'},
                               'evidence': {'quote': '张三负责检查。'}}]}

    duties = iter(['负责检查', '检查'] * 10)
    llm = StubLLM(entity_handler=lambda p, c: [candidate('张三',
        next(duties), '张三负责检查。')]
        if '张三' in c['text'] else [], property_handler=complete,
        verification_handler=lambda item, chunk: 'yes' if item['value'] in ('负责检查', '检查') else 'no', entity_verification_handler=lambda item, chunk: 'yes')
    result = extract(chunks, llm, max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert values == {'检查', '负责检查'}
    assert result['profile']['property_enrichment']['calls'] == 1
    assert result['profile']['property_enrichment']['source_windows'] == 2
    assert len(result['raw']['entities'][0]['evidences']) >= len(chunks)


def test_expired_document_does_not_call_or_recursively_split(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '4000')
    llm = StubLLM()
    llm.deadline = 0.0
    result = extract([ExtractionChunk('large', '张三负责检查。' * 400)], llm, max_repair_rounds=0)
    assert not llm.calls
    assert result['status'] == 'failed'
    assert len(result['attempts']) == 1
    assert result['attempts'][0]['retryable'] is False


def test_candidate_property_meaning_survives_non_json_source_windows(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '500')
    ontology = '@startuml\nclass Rule {\n + antecedent: String // 规则成立所依赖的前提条件\n}\n@enduml'
    source = json.dumps({'rows': [{'condition': '温度升高', 'result': '停车检查'}] * 80}, ensure_ascii=False)

    def entities(prompt, chunk):
        payload = json.loads(prompt)
        assert '规则成立所依赖的前提条件' in payload['ontology']
        assert '同时提取实体候选和当前片段支持的本体属性' in payload['task']['discipline']
        return []

    llm = StubLLM(entity_handler=entities)
    extract_split(ontology, [ExtractionChunk('table', source)], llm=llm, max_repair_rounds=0)
    assert len(llm.calls) > 1


def test_candidate_schema_exposes_identifiers_and_retries_display_names():
    ontology = '@startuml\nclass Rule {\n + RuleID (规则ID): String\n}\n@enduml'
    count = 0

    def entities(prompt, chunk):
        nonlocal count
        count += 1
        fields = json.loads(prompt)['output_contract']['success_schema']['properties']['properties']
        assert fields == {'type': 'object', 'properties': {'规则ID': {'type': 'string'}},
                          'additionalProperties': False}
        return [{'type': 'Rule', 'mention': '停车检查',
                 'properties': {'RuleID' if count == 1 else '规则ID': 'R01'},
                 'evidence': {'quote': chunk['text']}}]

    result = extract_split(ontology, [ExtractionChunk('c', '规则 R01 停车检查。')],
                          llm=StubLLM(entity_handler=entities,
                              verification_handler=lambda item, chunk: 'yes' if item['value'] == 'R01' else 'no', entity_verification_handler=lambda item, chunk: 'yes'),
                          retry_base_seconds=0)
    assert count == 2
    assert result['status'] == 'succeeded'
    assert result['raw']['entities'][0]['properties'] == {'规则ID': 'R01'}


def test_candidate_property_schema_partition_and_shared_key_types():
    from ..ontology_compiler import compile_plan
    from ..output_contracts import output_schema_for_types, validate_candidate_properties
    from ..structured_output import validate_json_schema
    plan = compile_plan('@startuml\nclass A {\n + value: String\n}\n'
                        'class B {\n + value: Integer\n + count: Integer\n}\n@enduml')
    schema = output_schema_for_types(plan, 'entity_candidates', ['A', 'B'])
    payload = {'entities': [dict(span_item('A', '甲'), properties={'value': '文字'}),
                            dict(span_item('B', '乙'), properties={'value': 2})]}
    assert not validate_json_schema(payload, schema)
    assert not validate_candidate_properties(payload, plan)
    payload['entities'][1]['properties']['value'] = '错误类型'
    assert any('$.entities[1]' in error for error in validate_candidate_properties(payload, plan))
    subset = output_schema_for_types(plan, 'entity_candidates', ['A'])
    fields = subset['properties']['entities']['items']['properties']['properties']
    assert fields['properties'] == {'value': {'type': 'string'}}


def test_frequent_key_sources_are_bounded_even_for_legacy_candidates(monkeypatch):
    # This synthetic many-window case checks per-call size, independently of
    # the new document-wide call budget (covered by task-runtime tests).
    monkeypatch.setenv('KG_EXTRACT_V31_MAX_CALLS', '4096')
    monkeypatch.setenv('KG_EXTRACT_V31_MAX_INPUT_BYTES', '16000')
    chunks = [ExtractionChunk(f'c{i}', '张三负责检查。' + '正文。' * 1500) for i in range(40)]
    def complete(prompt, chunk):
        assert len(prompt.encode()) < 16000
        return {'entities': [{'type': 'Person', 'name': '张三', 'properties': {'duty': '检查'},
                              'evidence': {'quote': '张三负责检查。'}}]}
    llm = StubLLM(entity_handler=lambda p, c: [span_item('Person', '张三')] if '张三' in c['text'] else [],
                  property_handler=complete,
                  verification_handler=lambda item, chunk: 'yes' if item['value'] == '检查' else 'no', entity_verification_handler=lambda item, chunk: 'yes')
    result = extract(chunks, llm)
    assert result['status'] == 'succeeded'
    assert result['raw']['entities'][0]['properties'] == {'duty': '检查'}
    assert len([1 for t, _ in llm.calls if t == 'property']) > 1
    assert all(call['input_bytes'] <= 16000 for call in result['run_trace']['calls'])
    assert len([c for c in result['run_trace']['calls'] if 'messages' in c]) <= 32
    assert any(c.get('messages_omitted') for c in result['run_trace']['calls'])


@pytest.mark.parametrize('failure', ['incomplete', 'timeout'])
def test_incomplete_response_splits_instead_of_retrying_same_window(monkeypatch, failure):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '4000')
    class Truncated:
        def __init__(self):
            self.lengths = []
        def generate_structured(self, schema, messages, run_context):
            if run_context['pass_name'] in {'property_verification', 'entity_verification'}:
                return StubLLM(verification_handler=lambda item, chunk: 'yes')(messages)
            payload = json.loads(messages[1]['content'])
            text = source_documents(payload)[0]['text']
            self.lengths.append(len(text))
            if len(text) > 600:
                if failure == 'timeout':
                    raise TimeoutError('Request timed out.')
                from ..llm_client import StructuredLLMResponse
                return StructuredLLMResponse(text='', api='chat.completions', response_mode='plain',
                                             status='incomplete', finish_reason='length')
            return '\n'.join(json.dumps(candidate(m.group(1), '检查', m.group()), ensure_ascii=False)
                for m in re.finditer(r'(人员\d{3})负责检查。', text)) or '{}'
    text = ''.join(f'人员{i:03}负责检查。\n' for i in range(150))
    llm = Truncated()
    result = extract([ExtractionChunk('c', text)], llm)
    assert result['status'] == 'succeeded'
    assert len(result['raw']['entities']) == 150
    assert llm.lengths.count(len(text)) == 1
    assert any(a.get('recovery') == 'split_request' for a in result['attempts'])


def test_too_large_minimum_prompt_fails_without_call(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_MAX_INPUT_BYTES', '100')
    llm = StubLLM()
    result = extract([ExtractionChunk('c', '张三')], llm)
    assert result['error_code'] == 'LLM_INPUT_BUDGET_EXCEEDED'
    assert not llm.calls


def test_source_windows_cover_every_character():
    text = 'x' * 27001
    pieces = windows(ExtractionChunk('c', text), 1000)
    end = 0
    for part in pieces:
        start = part.metadata['_v31_window_start']
        assert start <= end
        assert text[start:start + len(part.text)] == part.text
        end = start + len(part.text)
    assert end == len(text)


def test_failed_key_keeps_discovery_and_does_not_erase_other_keys():
    def properties(prompt, chunk):
        return {'entities': [{'type': 'Person', 'name': row['name'],
            'properties': {'duty': '检查'}, 'evidence': {'quote': '李四负责检查。'}}
            for row in json.loads(prompt)['task']['known_entities'] if row['name'] != '张三']}
    llm = StubLLM(entity_handler=lambda p, c: [span_item('Person', c['text'][:2])], property_handler=properties,
                  verification_handler=lambda item, chunk: 'yes' if item['name'] == '李四' and item['value'] == '检查' else 'no', entity_verification_handler=lambda item, chunk: 'yes')
    result = extract([ExtractionChunk('a', '张三负责检查。'), ExtractionChunk('b', '李四负责检查。')],
                     llm, best_effort=True, max_attempts=2)
    assert result['status'] == 'partial'
    assert [(e['name'], e['properties']) for e in result['raw']['entities']] == [('张三', {}), ('李四', {'duty': '检查'})]
    assert len([p for t, p in llm.calls if t == 'property']) == 2


def test_candidate_property_types_retry_and_source_is_unchanged():
    calls = 0
    def entities(prompt, chunk):
        nonlocal calls
        calls += 1
        row = candidate('张三', '检查', chunk['text'])
        if calls == 1:
            row['properties']['duty'] = 123
        return [row]
    result = extract([ExtractionChunk('c', '张三负责检查。')], StubLLM(entity_handler=entities))
    assert result['status'] == 'succeeded'
    assert calls == 2
    assert result['profile']['property_enrichment']['calls'] == 0


def test_negated_literal_is_not_accepted_by_substring():
    llm = StubLLM(entity_handler=lambda p, c: [candidate('张三', '发射火箭', c['text'])],
                  verification_handler=lambda item, chunk: 'no',
                  entity_verification_handler=lambda item, chunk: 'yes')
    result = extract([ExtractionChunk('c', '张三不负责发射火箭。')], llm)
    assert result['raw']['entities'][0]['properties'] == {}
    assert len([t for t, _ in llm.calls if t == 'evidence_verification']) == 2


def test_large_schema_is_partitioned_without_dropping_types(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_MAX_INPUT_BYTES', '14000')
    ontology = '@startuml\n' + '\n'.join(f'class "业务类别{i:03}" as Type{i:03} {{\n + 描述 (description): String\n}}' for i in range(120)) + '\n@enduml'
    seen = set()
    def entities(prompt, chunk):
        types = json.loads(prompt)['output_contract']['success_schema']['properties']['type']['enum']
        seen.update(types)
        return [{'type': t, 'mention': '甲', 'properties': {}, 'evidence': {'quote': '甲'}} for t in types]
    result = extract_split(ontology, [ExtractionChunk('c', '甲')], llm=StubLLM(entity_handler=entities), retry_base_seconds=0, skill_refs=[])
    assert result['status'] == 'succeeded'
    assert seen == {f'Type{i:03}' for i in range(120)}
    assert len(result['raw']['entities']) == 120
    assert len([c for c in result['run_trace']['calls'] if c['pass'] == 'entity']) > 1
    assert all(c['input_bytes'] <= 14000 for c in result['run_trace']['calls'])


def test_property_overflow_splits_sources_and_retains_both_ends():
    from ..llm_client import StructuredLLMResponse
    class PropertyOverflow:
        def __init__(self):
            self.properties = []
        def generate_structured(self, schema, messages, run_context):
            if run_context['pass_name'] in {'property_verification', 'entity_verification'}:
                return StubLLM(verification_handler=lambda item, chunk: 'yes')(messages)
            user = json.loads(messages[1]['content'])
            if run_context['pass_name'] == 'entity':
                return json.dumps(span_item('Person', '张三'), ensure_ascii=False)
            text = user['documents']
            self.properties.append(text)
            if text.count('记录编号') > 2 and not user['task']['known_entities'][0].get('properties'):
                return StructuredLLMResponse(text='', api='chat.completions', response_mode='plain', status='incomplete', finish_reason='length')
            return json.dumps({'type': 'Person', 'name': '张三', 'properties': {'duty': '检查'},
                               'evidence': {'quote': '张三负责检查。'}}, ensure_ascii=False)
    chunks = [ExtractionChunk(str(i), '张三负责检查。' + f'记录编号{i}。') for i in range(6)]
    llm = PropertyOverflow()
    result = extract(chunks, llm)
    assert result['status'] == 'succeeded'
    assert result['raw']['entities'][0]['properties'] == {'duty': '检查'}
    assert {e['source_block_ids'][0] for e in result['raw']['entities'][0]['evidences']} == {str(i) for i in range(6)}
    assert any(chunks[-1].text in docs for docs in llm.properties[1:])
    assert any(a.get('recovery') == 'split_request' for a in result['attempts'])


def test_repair_no_progress_is_reported_and_has_bounded_context(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '500')
    llm = StubLLM(repair_handler=lambda prompt, chunk: {'entities': []})
    result = extract([ExtractionChunk('c', '这是结构章节。' * 200, section_id='s')], llm)
    assert result['profile']['repair_stop_reason'] == 'no_progress'
    assert result['coverage_gaps']
    repairs = [json.loads(p) for t, p in llm.calls if t == 'repair']
    assert len(repairs) > 1
    assert all(len(source_documents(p)[0]['text']) <= 500 for p in repairs)


@pytest.mark.parametrize('source', ['张三负责检查。李四负责发射火箭。', '张三负责检查，李四负责发射火箭。'])
def test_another_subjects_value_is_not_literal_support(source):
    llm = StubLLM(entity_handler=lambda p, c: [candidate('张三', '发射火箭', c['text'])],
                  verification_handler=lambda item, chunk: 'no',
                  entity_verification_handler=lambda item, chunk: 'yes')
    result = extract([ExtractionChunk('c', source)], llm)
    assert result['raw']['entities'][0]['properties'] == {}
    assert len([t for t, _ in llm.calls if t == 'evidence_verification']) == 2
