"""Small synthetic regressions for the 2026-10-13 live eval failures."""
import json

from .test_review_regressions import candidate, run
from .stub_llm import StubLLM, span_item
from ..chunk_context import ExtractionChunk, ChunkContextManager
from ..request_budget import windows
from ..source_windows import source_parts
from ..property_grounding import locate_in_sources


def test_neighbour_completion_batches_keys_without_losing_subjects():
    names = [f'人员{i}' for i in range(12)]
    chunks = [ExtractionChunk('a', '、'.join(names) + '到岗。'),
              ExtractionChunk('b', '他们负责检查。')]
    def complete(prompt, chunk):
        targets = json.loads(prompt)['task']['known_entities']
        return {'entities': [{'type': 'Person', 'name': row['name'],
            'properties': {'duty': '检查'}, 'evidence': {'quote': chunks[1].text}}
            for row in targets]}
    llm = StubLLM(entity_handler=lambda p, c: [candidate(n, c['text']) for n in names]
        if '到岗' in c['text'] else [], property_handler=complete,
        verification_handler=lambda i, c: 'yes')
    result = run(chunks, llm)
    assert result['status'] == 'succeeded'
    assert len(result['raw']['entities']) == 12
    assert all(e['properties'] == {'duty': '检查'} for e in result['raw']['entities'])
    property_calls = [json.loads(p)['task']['known_entities'] for t, p in llm.calls if t == 'property']
    assert len(property_calls) == 3
    assert all(len(batch) == 4 for batch in property_calls)
    assert {row['name'] for batch in property_calls for row in batch} == set(names)
    for row in result['raw']['entities']:
        assert any(e['source_block_ids'] == ['b'] for e in row['evidences'])


def test_nfkc_quotes_use_original_source_offsets_without_synonym_matching():
    text = '前缀：技术部负责⽂件审核及⼯作检查。后缀'
    chunk = ExtractionChunk('c', text)
    quote = '技术部负责文件审核及工作检查。'
    span = locate_in_sources(quote, [chunk])
    assert span['quote'] == text[span['start_char']:span['end_char']]
    assert '⽂' in span['quote']
    assert ChunkContextManager([chunk]).find_span('c', '技术部负责材料审核及工作检查。') is None
    llm = StubLLM(entity_handler=lambda p, c: [candidate('技术部', quote, {'duty': '文件审核及工作检查'})],
                  verification_handler=lambda i, c: 'yes')
    result = run([chunk], llm)
    assert result['status'] == 'succeeded'
    assert result['raw']['entities'][0]['properties']['duty'] == '文件审核及工作检查'


def test_table_fences_travel_with_rows_and_never_become_empty_calls():
    text = '```json\n' + json.dumps({'columns': ['职责'], 'rows': [
        {'职责': '人员甲负责检查。'}, {'职责': '人员乙负责审批。'}]}, ensure_ascii=False) + '\n```'
    parts = windows(ExtractionChunk('t', text), 65)
    assert len(parts) == 2
    assert all('负责' in p.text for p in parts)
    for p in parts:
        for source in source_parts(p):
            offset = source.metadata.get('_v31_window_start', 0)
            assert source.text == text[offset:offset + len(source.text)]


def test_upstream_split_closing_fence_does_not_force_character_cell_slicing():
    rows = [{'职责': '人员甲负责检查。' * 12}, {'职责': '人员乙负责审批。' * 12}]
    text = json.dumps({'rows': rows}, ensure_ascii=False) + '\n```'
    parts = windows(ExtractionChunk('t', text), 80)
    assert len(parts) == 2
    assert all(p.metadata['_v31_atomic_table'] for p in parts)
    assert [json.loads(p.text) for p in parts] == rows


def test_truncated_candidate_repartitions_once_and_preserves_parent_facts():
    from ..split_extractor import extract_split
    from ..llm_client import StructuredLLMResponse
    calls = []
    def llm(messages):
        payload = json.loads(messages[1]['content'])
        if payload['task']['operation'] == 'evidence_verification':
            return StubLLM()(messages)
        types = payload['output_contract']['success_schema']['properties']['type']['enum']
        calls.append(types)
        if len(types) == 2:
            return StructuredLLMResponse(text=json.dumps(span_item('A', '甲')), api='chat.completions',
                response_mode='plain', status='incomplete', finish_reason='length')
        return '{}' if types == ['A'] else json.dumps(span_item('B', '乙'))
    result = extract_split('@startuml\nclass A\nclass B\n@enduml',
        [ExtractionChunk('c', '甲与乙')], llm=llm, max_attempts=3, max_repair_rounds=0, retry_base_seconds=0, skill_refs=[])
    assert calls.count(['A', 'B']) == 1
    assert {e['name'] for e in result['raw']['entities']} == {'甲', '乙'}
    assert result['status'] == 'succeeded'
    assert any(a.get('recovery') == 'split_request' for a in result['attempts'])


def test_literal_official_candidates_require_independent_type_admission():
    from ..split_extractor import extract_split
    text = '关于开展巡检的通知。登记签收：已阅。'
    ontology = '@startuml\nclass OfficialDocument\n@enduml'
    llm = StubLLM(entity_handler=lambda p, c: [span_item('OfficialDocument', name)
        for name in ['关于开展巡检的通知', '已阅。']],
        verification_handler=lambda item, chunk: 'yes' if item['name'] == '关于开展巡检的通知' else 'no')
    result = extract_split(ontology, [ExtractionChunk('c', text)], llm=llm,
        max_attempts=1, max_repair_rounds=0, best_effort=True)
    assert [e['name'] for e in result['raw']['entities']] == ['关于开展巡检的通知']
    verification = [json.loads(p) for t, p in llm.calls if t == 'evidence_verification']
    assert len(verification) == 1
    assert len(verification[0]['task']['items']) == 2
    assert result['status'] == 'succeeded'
    assert any(a['type'] == 'candidate_rejected' and not a['terminal'] for a in result['attempts'])
    assert any(w['code'] == 'ENTITY_SEMANTIC_UNSUPPORTED' for w in result['warnings'])


def test_batched_property_verification_keeps_each_prior_and_conflict_separate():
    from ..property_grounding import ground_properties
    from ..ontology_compiler import compile_plan
    ontology = '@startuml\nclass Person {\n + duty: String\n}\n@enduml'
    chunk = ExtractionChunk('c', '甲负责检查。乙负责记录。乙改为审批。')
    a = locate_in_sources('甲负责检查。', [chunk]); b = locate_in_sources('乙负责记录。', [chunk])
    def prior(name, value, span):
        return {'type': 'Person', 'name': name, 'properties': {'duty': value},
                'property_evidence': {'duty': {'value': value, 'support': 'llm', 'sources': [span]}}}
    priors = {('Person', '甲'): prior('甲', '检查', a), ('Person', '乙'): prior('乙', '记录', b)}
    seen = []
    def verdict(item, context):
        seen.append(item)
        assert item['name'] == '乙'
        assert item['previous_value'] == '记录'
        assert item['conflicting_values'] == ['记录', '审批']
        return 'no'
    llm = StubLLM(verification_handler=verdict)
    rows = ground_properties([
        {'type': 'Person', 'name': '甲', 'properties': {'duty': '检查'}, 'evidence': {'quote': '甲负责检查。'}},
        {'type': 'Person', 'name': '乙', 'properties': {'duty': '审批'}, 'evidence': {'quote': '乙改为审批。'}},
    ], [chunk], llm, attempts=[], warnings=[], plan=compile_plan(ontology), priors=priors,
        conflicts_by_key={('Person', '乙'): {'duty': ['记录', '审批']}})
    assert rows[0]['properties'] == {'duty': '检查'}
    assert rows[0]['property_evidence']['duty']['sources'] == [a]
    assert rows[1]['properties'] == {}
    assert len(seen) == 1
