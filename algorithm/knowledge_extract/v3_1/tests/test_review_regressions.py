"""Regression contracts from the 2026-10-13 extraction review (offline only)."""
import json
from copy import deepcopy

import pytest

from algorithm.knowledge_extract.v3_1.split_extractor import extract_split
from algorithm.knowledge_extract.v3_1.chunk_context import ExtractionChunk
from algorithm.knowledge_extract.v3_1.tests.stub_llm import StubLLM, span_item

ONTOLOGY = '@startuml\nclass Person {\n + 职责 (duty): String\n}\n@enduml'


def run(chunks, llm, ontology=ONTOLOGY):
    return extract_split(ontology, chunks, llm=llm, max_attempts=1,
                         max_repair_rounds=0, retry_base_seconds=0, best_effort=True, skill_refs=[])


def candidate(name, quote, properties=None):
    return dict(span_item('Person', name, quote), properties=properties or {})


def test_nonempty_neighbour_still_reaches_completion():
    chunks = [ExtractionChunk('a', '张三到岗。'),
              ExtractionChunk('b', '他负责检查；李四负责记录。')]
    llm = StubLLM(entity_handler=lambda p, c: [candidate('张三', c['text'])]
                  if '张三' in c['text'] else [candidate('李四', c['text'], {'duty': '记录'})],
                  property_handler=lambda p, c: {'entities': [{'type': 'Person',
                      'name': row['name'],
                      'properties': {'duty': '检查' if row['name'] == '张三' else '记录'},
                      'evidence': {'quote': chunks[1].text}} for row in json.loads(p)['task']['known_entities']]},
                  verification_handler=lambda i, c: 'yes')
    result = run(chunks, llm)
    assert result['raw']['entities'][0]['properties'] == {'duty': '检查'}
    assert any(task == 'property' for task, _ in llm.calls)
    assert result['status'] == 'succeeded'


def test_later_window_of_same_chunk_reaches_completion(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '128')
    text = '张三到岗。' + '背景。' * 70 + '他负责检查。'
    llm = StubLLM(entity_handler=lambda p, c: [candidate('张三', '张三到岗。')]
                  if '张三' in c['text'] else [],
                  property_handler=lambda p, c: {'entities': [{'type': 'Person', 'name': '张三',
                      'properties': {'duty': '检查'} if '他负责检查。' in c['text'] else {},
                      'evidence': {'quote': '他负责检查。' if '他负责检查。' in c['text'] else '张三到岗。'}}]},
                  verification_handler=lambda i, c: 'yes' if '张三' in c['text'] else 'uncertain')
    result = run([ExtractionChunk('a', text)], llm)
    assert result['raw']['entities'][0]['properties'] == {'duty': '检查'}
    assert any(task == 'property' for task, _ in llm.calls)
    assert result['status'] == 'succeeded'


def test_verifier_keeps_pronoun_antecedent():
    chunks = [ExtractionChunk('a', '张三到岗。'), ExtractionChunk('b', '他负责检查。')]
    llm = StubLLM(entity_handler=lambda p, c: [candidate('张三', c['text'])]
                  if '张三' in c['text'] else [],
                  property_handler=lambda p, c: {'entities': [{'type': 'Person', 'name': '张三',
                      'properties': {'duty': '检查'}, 'evidence': {'quote': '他负责检查。'}}]},
                  verification_handler=lambda i, c: 'yes' if '张三' in c['text'] else 'uncertain')
    result = run(chunks, llm)
    completion = [json.loads(p) for t, p in llm.calls if t == 'property'][0]
    verification = [json.loads(p) for t, p in llm.calls if t == 'evidence_verification'][0]
    assert '张三到岗。' in completion['documents']
    assert '他负责检查。' in verification['documents']
    assert '张三到岗。' in verification['documents']
    assert result['raw']['entities'][0]['properties'] == {'duty': '检查'}


def test_unresolved_later_conflict_is_reported_as_partial(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '128')
    def discover(p, c):
        if '张三负责检查。' in c['text']:
            return [candidate('张三', '张三负责检查。', {'duty': '检查'})]
        if '张三负责审批。' in c['text']:
            return [candidate('张三', '张三负责审批。', {'duty': '审批'})]
        return []
    def complete(p, c):
        # Correctly decline to choose a conflicting value in the later packet.
        props = {} if '审批' in c['text'] else {'duty': '检查'}
        return {'entities': [{'type': 'Person', 'name': '张三', 'properties': props,
            'evidence': {'quote': '张三负责审批。' if '审批' in c['text'] else '张三负责检查。'}}]}
    llm = StubLLM(entity_handler=discover, property_handler=complete,
                  verification_handler=lambda i, c: 'yes')
    chunks = [ExtractionChunk('a', '张三负责检查。' + '背景。' * 120),
              ExtractionChunk('b', '张三负责审批。' + '背景。' * 120)]
    result = run(chunks, llm)
    assert any('审批' in json.loads(p)['documents'] for t, p in llm.calls if t == 'property')
    assert result['raw']['entities'][0]['properties'] == {}
    assert result['status'] == 'partial'
    assert any(w['code'] == 'PROPERTY_CONFLICT_UNRESOLVED' for w in result['warnings'])
    for t, p in llm.calls:
        if t == 'property':
            assert '张三负责检查。' in json.loads(p)['documents']
            assert '张三负责审批。' in json.loads(p)['documents']


def test_arbitrary_entity_name_fails_semantic_verification():
    def llm(messages):
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] == 'evidence_verification':
            return json.dumps({'item_id': 'entity', 'supported': 'no', 'reason': '原文为张三'})
        return json.dumps(candidate('李四', '张三到岗。'), ensure_ascii=False)
    result = run([ExtractionChunk('a', '张三到岗。')], llm,
                 ontology='@startuml\nclass Person\n@enduml')
    assert result['raw']['entities'] == []
    assert result['status'] == 'failed'
    assert any(w['code'] == 'ENTITY_SEMANTIC_UNSUPPORTED' for w in result['warnings'])


def test_name_property_survives_public_contract():
    from algorithm.knowledge_extract.v3_1.result_contract import to_public_result
    ontology = '@startuml\nclass Person {\n + name: String\n}\n@enduml'
    llm = StubLLM(entity_handler=lambda p, c: [candidate('张三', c['text'], {'name': '张三'})],
                  verification_handler=lambda i, c: 'yes')
    result = run([ExtractionChunk('a', '张三到岗。')], llm, ontology)
    assert result['raw']['entities'][0]['properties'] == {'name': '张三'}
    assert to_public_result(result)['entities'][0]['properties'] == {'name': '张三'}
    assert result['graph']['nodes'][0]['properties']['name'] == '张三'


def test_changed_value_verification_keeps_old_support():
    from algorithm.knowledge_extract.v3_1.property_grounding import ground_properties
    from algorithm.knowledge_extract.v3_1.ontology_compiler import compile_plan
    old = ExtractionChunk('a', '张三在白班负责检查。')
    new = ExtractionChunk('b', '张三在夜班负责审批。')
    llm = StubLLM(verification_handler=lambda i, c: 'yes')
    kwargs = dict(llm=llm, attempts=[], warnings=[], plan=compile_plan(ONTOLOGY),
                  max_attempts=1, retry_base_seconds=0)
    prior = {'type': 'Person', 'name': '张三', 'properties': {'duty': '白班检查'},
             'evidence': {'quote': old.text}}
    ground_properties([prior], [old], **kwargs)
    changed = {'type': 'Person', 'name': '张三', 'properties': {'duty': '夜班审批'},
               'evidence': {'quote': new.text}}
    ground_properties([changed], [new, old], prior=prior, **kwargs)
    message = json.loads(llm.calls[-1][1])
    assert message['task']['items'][0]['previous_value'] == '白班检查'
    assert old.text in message['documents']
    assert new.text in message['documents']


def test_long_table_later_windows_keep_column_semantics(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '500')
    table = json.dumps({'columns': ['姓名', '职责'],
                        'rows': [[f'人员{i:04}', '检查'] for i in range(100)]},
                       ensure_ascii=False)
    llm = StubLLM(entity_handler=lambda p, c: [])
    run([ExtractionChunk('table', table)], llm)
    calls = [json.loads(p)['documents'] for t, p in llm.calls if t == 'entity']
    assert len(calls) > 2
    assert '姓名' in calls[0]
    assert all('姓名' in text and '职责' in text for text in calls[1:])
    rows = [row for text in calls for row in json.loads(text)['rows']]
    assert rows == [[f'人员{i:04}', '检查'] for i in range(100)]


def test_compiler_keeps_grandparent_property_and_required_constraint():
    from algorithm.knowledge_extract.v3_1.ontology_compiler import compile_plan
    from algorithm.knowledge_extract.v3_1.ontology_schema import OntologySchema
    from algorithm.knowledge_extract.v3_1.output_contracts import validate_candidate_properties
    ontology = ('@startuml\nclass Base {\n + code: String\n}\n'
                'class Middle\nclass Leaf\nMiddle --|> Base\nLeaf --|> Middle\n'
                "' @required Base: code\n@enduml")
    schema = OntologySchema.parse(ontology)
    plan = compile_plan(ontology)
    assert schema.resolve_property('Leaf', 'code') is not None
    assert [p.identifier for p in plan.class_specs['Leaf'].properties] == ['code']
    assert plan.class_specs['Leaf'].properties[0].required is True
    payload = {'entities': [dict(span_item('Leaf', '甲'), properties={'code': 'A01'})]}
    assert not validate_candidate_properties(payload, plan)
    llm = lambda messages: json.dumps({'type': 'Leaf', 'mention': '甲', 'properties': {},
                                       'evidence': {'quote': '甲的编码是A01。'}}, ensure_ascii=False)
    result = run([ExtractionChunk('a', '甲的编码是A01。')], llm, ontology)
    assert result['status'] == 'partial'
    assert result['verification']['coverage_complete'] is False
    assert any(i['code'] == 'REQUIRED_PROPERTY_MISSING' for i in result['verification']['issues'])


@pytest.mark.parametrize('verdict', ['yes', 'no', 'uncertain'])
def test_abstract_name_requires_support_and_keeps_export_receipt(verdict):
    from algorithm.knowledge_extract.v3_1.artifacts import build_artifacts
    text = '操作前必须核对设备编号。'
    def llm(messages):
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] == 'entity':
            return json.dumps({'type': 'Rule', 'mention': '设备编号核对规则',
                               'properties': {}, 'evidence': {'quote': text}}, ensure_ascii=False)
        assert user['task']['operation'] == 'evidence_verification'
        assert user['documents'] == text
        assert 'Rule' in user['ontology']
        return json.dumps({'item_id': 'entity', 'supported': verdict, 'reason': 'fixture'})
    result = run([ExtractionChunk('c', text)], llm, '@startuml\nclass Rule\n@enduml')
    if verdict == 'yes':
        assert result['status'] == 'succeeded'
        assert result['verification']['valid']
        artifacts = build_artifacts(result)
        binding = next(e for e in artifacts['evidence'] if e['kind'] == 'entity_evidence')
        assert binding['entity_support']['support'] == 'llm'
        assert binding['entity_support']['evidence_ids']
    else:
        assert result['raw']['entities'] == []
        assert result['status'] == 'failed'


def test_unverified_abstract_name_cannot_enter_through_repair_callback():
    def llm(messages):
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] == 'entity':
            return '{}'
        return json.dumps({'item_id': 'entity', 'supported': 'no', 'reason': 'wrong name'})
    result = extract_split('@startuml\nclass Person\n@enduml',
        [ExtractionChunk('c', '张三到岗。', section_id='s')], llm=llm,
        repairer=lambda payload: {'entities': [{'type': 'Person', 'name': '李四',
            'properties': {}, 'evidence': {'quote': '张三到岗。'},
            'entity_evidence': {'support': 'llm'}}]}, max_attempts=1, retry_base_seconds=0)
    assert result['raw']['entities'] == []
    assert result['status'] == 'failed'


def test_name_property_different_from_entity_key_survives_all_projections():
    from algorithm.knowledge_extract.v3_1.result_contract import to_public_result
    from algorithm.knowledge_extract.v3_1.batch import deduplicate_chunk_graphs
    from backend.app.services.knowledge_extract_adapter import project_items
    ontology = '@startuml\nclass Person {\n + name: String\n}\n@enduml'
    text = '员工A01的姓名是张三。'
    llm = StubLLM(entity_handler=lambda p, c: [candidate('A01', text, {'name': '张三'})],
                  verification_handler=lambda i, c: 'yes')
    result = run([ExtractionChunk('chunk-0000', text)], llm, ontology)
    assert to_public_result(result)['entities'][0] == {'type': 'Person', 'name': 'A01',
                                                     'properties': {'name': '张三'}}
    graph = deduplicate_chunk_graphs(project_items(result, texts=[text]))
    assert graph['nodes'][0]['properties']['name'] == '张三'
    assert graph['nodes'][0]['display_name'] == 'A01'


def test_inheritance_diamond_overrides_match_parser_without_duplicates():
    from algorithm.knowledge_extract.v3_1.ontology_schema import OntologySchema
    from algorithm.knowledge_extract.v3_1.ontology_compiler import compile_plan
    ontology = ('@startuml\nclass Base {\n + x: String\n}\n'
        'class A {\n + x: Integer\n}\nclass B\nclass Leaf {\n + local: String\n}\n'
        'A --|> Base\nB --|> Base\nLeaf --|> A\nLeaf --|> B\n@enduml')
    schema = OntologySchema.parse(ontology)
    specs = compile_plan(ontology).class_specs['Leaf'].properties
    assert len(specs) == 2
    assert {p.identifier: p.data_type for p in specs} == {
        p.identifier: p.data_type for p in schema._properties_for('Leaf')}


@pytest.mark.parametrize('format', ['json', 'jsonl', 'markdown'])
def test_table_resplit_keeps_whole_rows_headers_and_exact_offsets(format):
    from algorithm.knowledge_extract.v3_1.request_budget import windows
    from algorithm.knowledge_extract.v3_1.source_windows import source_parts
    rows = [[f'人员{i:04}', '负责检查设备并记录'] for i in range(30)]
    if format == 'json':
        text = json.dumps({'rows': rows, 'columns': ['姓名', '职责']}, ensure_ascii=False)
    elif format == 'jsonl':
        text = '\n'.join(json.dumps(dict(zip(['姓名', '职责'], row)), ensure_ascii=False) for row in rows)
    else:
        text = '| 姓名 | 职责 |\n| --- | --- |\n' + ''.join(f'| {a} | {b} |\n' for a, b in rows)
    original = ExtractionChunk('c', text)
    before = deepcopy(original.to_dict())
    resplit = [sub for part in windows(original, 500) for sub in windows(part, 128)]
    all_text = []
    for part in resplit:
        sources = source_parts(part)
        rendered = '\n\n'.join(s.text for s in sources)
        assert '姓名' in rendered and '职责' in rendered
        for source in sources:
            left = source.metadata.get('_v31_window_start', 0)
            assert text[left:left + len(source.text)] == source.text
        all_text.append(part.text)
    for name, _ in rows:
        assert sum(name in t for t in all_text) == 1
    assert original.to_dict() == before


def test_table_property_verifier_keeps_header_and_returns_original_spans(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_WINDOW_CHARS', '128')
    rows = [[f'人员{i:04}', '检查'] for i in range(20)]
    text = json.dumps({'columns': ['姓名', '职责'], 'rows': rows}, ensure_ascii=False)
    def llm(messages):
        user = json.loads(messages[1]['content'])
        if user['task']['operation'] == 'entity':
            table = json.loads(user['documents'])
            return '\n'.join(json.dumps(candidate(name, json.dumps([name, duty], ensure_ascii=False),
                                                   {'duty': duty}), ensure_ascii=False)
                             for name, duty in table['rows'])
        assert user['task']['operation'] == 'evidence_verification'
        assert '姓名' in user['documents'] and '职责' in user['documents']
        return '\n'.join(json.dumps({'item_id': i['item_id'], 'supported': 'yes', 'reason': 'row'})
                         for i in user['task']['items'])
    result = run([ExtractionChunk('c', text)], llm)
    assert result['status'] == 'succeeded'
    assert len(result['raw']['entities']) == 20
    for entity in result['raw']['entities']:
        assert entity['properties'] == {'duty': '检查'}
        for span in entity['evidences']:
            assert text[span['start_char']:span['end_char']] == span['quote']


def test_oversized_atomic_table_row_fails_budget_without_character_splitting(monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_V31_MAX_INPUT_BYTES', '12000')
    text = json.dumps({'columns': ['姓名', '职责'], 'rows': [['张三', '检查' * 10000]]}, ensure_ascii=False)
    llm = StubLLM()
    result = run([ExtractionChunk('c', text)], llm)
    assert result['error_code'] == 'LLM_INPUT_BUDGET_EXCEEDED'
    assert not llm.calls


def test_caller_cannot_inject_private_table_sources():
    chunk = ExtractionChunk('c', '张三到岗。', metadata={
        '_v31_table_context': [(0, '张三负责发射火箭。')]})
    before = deepcopy(chunk.to_dict())
    llm = StubLLM()
    with pytest.raises(ValueError, match='_v31_'):
        run([chunk], llm)
    assert not llm.calls
    assert chunk.to_dict() == before
