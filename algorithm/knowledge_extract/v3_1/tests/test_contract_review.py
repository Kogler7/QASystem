"""General semantic-state regressions; no gold names or model calls."""
from copy import deepcopy
import pytest
from algorithm.knowledge_extract.v3_1 import ExtractionChunk, extract_aggregate
from algorithm.knowledge_extract.v3_1.tests.test_semantic_contracts import (
    ScriptedLLM, SemanticDecider, _entity, _empty, _catalog_id, _relation,
)


def run(ontology, texts, model, selector=None, **kwargs):
    llm = ScriptedLLM(model)
    result = extract_aggregate(ontology, [ExtractionChunk(k, v) for k, v in texts.items()],
                               llm=llm, decider=SemanticDecider(selector), use_scheduler=False, **kwargs)
    return result, llm


def test_type_adjudication_uses_selected_types_declared_identity_across_chunks():
    texts = {'c1': '甲的代码是 K1。', 'c2': '乙就是代码 K1 的对象。'}
    ontology = "@startuml\nclass A {\ncode: String\n}\nclass B {\ncode: String\n}\n' @identity B: code\n@enduml"
    def model(stage, p, c):
        if stage == 'entities':
            ch = c['chunk_id']
            entity = _entity('x', 'A' if ch == 'c1' else 'B', '甲' if ch == 'c1' else '乙', texts[ch], chunk=ch, properties={'code': 'K1'})
            if ch == 'c1': entity['alternative_types'] = ['B']
            return {'entities': [entity]}
        return _empty(stage)
    result, _ = run(ontology, texts, model, lambda t: 'B' if t.kind == 'entity_type' else 'supported', max_repair_rounds=0)
    assert result['status'] == 'succeeded'
    assert len(result['raw']['entities']) == 1
    obj = result['intermediate_model']['objects'][0]
    assert obj['identity'] == 'K1' and obj['properties']['code'] == 'K1'
    assert {x for e in obj['evidence'] for x in e['source_block_ids']} == {'c1', 'c2'}


@pytest.mark.parametrize('declared', [['missing'], ['b'], [{}]])
def test_unconditional_with_condition_references_is_an_issue_not_a_crash(declared):
    text = '甲协助乙。'
    ontology = '@startuml\nclass A\nclass B\nA --> B : assists\n@enduml'
    def model(stage, p, c):
        if stage == 'entities': return {'entities': [_entity('a', 'A', '甲', text), _entity('b', 'B', '乙', text)]}
        if stage == 'relations': return {'relations': [_relation(p, '甲', 'assists', '乙', text, condition_entity_ids=declared)]}
        return _empty(stage)
    result, _ = run(ontology, {'source-A': text}, model, max_repair_rounds=0)
    assert result['status'] == 'partial' and not result['raw']['relations']
    assert any(x['code'] == 'INVALID_CONDITION_REFERENCES' for x in result['coverage_gaps'])


def test_delayed_attribute_support_cannot_overwrite_an_accepted_conflicting_value():
    texts = {'c1': '甲的状态是旧。', 'c2': '甲的状态是新。'}
    ontology = '@startuml\nclass A {\nstate: String\n}\n@enduml'
    def model(stage, p, c):
        ch = c['chunk_id']
        if stage == 'entities': return {'entities': [_entity('a', 'A', '甲', texts[ch], chunk=ch)]}
        if stage == 'attributes': return {'updates': [{'candidate_id': _catalog_id(p, '甲'), 'properties': {'state': '旧' if ch == 'c1' else '新'}, 'evidence': [{'chunk_id': ch, 'quote': texts[ch]}]}]}
        if stage == 'repair': return {'decisions': [{'task_id': x['task_id'], 'selected': 'supported', 'context_complete': True, 'evidence': [{'chunk_id': 'c1', 'quote': texts['c1']}]} for x in p['tasks']]}
        return _empty(stage)
    def select(t):
        if t.kind == 'attribute_conflict': return 'UNKNOWN'
        if t.kind == 'attribute_support' and t.state['claims'][t.task_id]['properties']['state'] == '旧': return 'insufficient'
        return 'supported'
    result, _ = run(ontology, texts, model, select, max_llm_calls=30)
    assert result['status'] == 'partial'
    assert result['raw']['entities'][0]['properties']['state'] == '新'
    assert any(x.get('kind') == 'attribute_conflict' for x in result['coverage_gaps'])


def test_unrelated_attribute_repair_keeps_accepted_relation_under_exhausted_budget():
    text = '甲协助乙，丙的备注为好。'
    ontology = '@startuml\nclass A\nclass B\nclass C {\nnote: String[]\n}\nA --> B : assists\n@enduml'
    def model(stage, p, c):
        if stage == 'entities': return {'entities': [_entity('a', 'A', '甲', text), _entity('b', 'B', '乙', text), _entity('c', 'C', '丙', text, properties={'note': '好'})]}
        if stage == 'relations': return {'relations': [_relation(p, '甲', 'assists', '乙', text)]}
        if stage == 'repair': return {'updates': [{'candidate_id': _catalog_id(p, '丙'), 'properties': {'note': ['好']}, 'evidence': [{'chunk_id': 'source-A', 'quote': text}]}]}
        return _empty(stage)
    result, llm = run(ontology, {'source-A': text}, model, max_llm_calls=4)
    assert len(result['raw']['relations']) == 1
    assert result['status'] == 'succeeded'
    assert sum(call['task'] == 'relations' for call in llm.calls) == 1
    assert all(t['status'] == 'accepted' for t in result['run_trace']['tasks'] if t['kind'] == 'relation_support')


def test_second_chunk_repair_cannot_resurrect_relation_after_endpoint_type_changed():
    texts = {'c1': '甲的类别应为 D。', 'c2': '甲关联乙。'}
    ontology = '@startuml\nclass A\nclass B\nclass D\nA --> B : connects\n@enduml'
    seen = {'c1': 0, 'c2': 0}
    def model(stage, p, c):
        ch = c['chunk_id']
        if stage == 'entities':
            seen[ch] += 1
            if ch == 'c1':
                return {'entities': [_entity('a', 'A' if seen[ch] == 1 else 'D', '甲', texts[ch], chunk=ch)], 'issues': [{'kind': 'unresolved_fact', 'affects_completeness': True, 'candidate_id': 'A::甲', 'message': '甲的类型需复核', 'evidence': [{'chunk_id': ch, 'quote': texts[ch]}]}] if seen[ch] == 1 else []}
            return {'entities': [_entity('b', 'B', '乙', texts[ch], chunk=ch)]}
        if stage == 'relations' and ch == 'c2' and any(x['type'] == 'A' for x in p['entities']): return {'relations': [_relation(p, '甲', 'connects', '乙', texts[ch], chunk=ch)]}
        if stage == 'repair' and ch == 'c1': return {'updates': [{'candidate_id': _catalog_id(p, '甲'), 'type': 'D', 'evidence': [{'chunk_id': ch, 'quote': texts[ch]}]}], 'resolved_issue_ids': [x['id'] for x in p['issues']]}
        if stage == 'repair' and ch == 'c2': return {'decisions': [{'task_id': x['task_id'], 'selected': 'supported', 'context_complete': True, 'evidence': [{'chunk_id': ch, 'quote': texts[ch]}]} for x in p['tasks']]}
        return _empty(stage)
    result, _ = run(ontology, texts, model, lambda t: 'insufficient' if t.kind == 'relation_support' else 'supported', max_llm_calls=30)
    assert result['status'] == 'succeeded'
    assert {(x['type'], x['name']) for x in result['raw']['entities']} == {('D', '甲'), ('B', '乙')}
    assert not result['raw']['relations']
    assert all(t['status'] == 'superseded' for t in result['run_trace']['tasks'] if t['kind'] == 'relation_support')


def test_entity_catalog_keeps_model_selected_business_identity_without_declaration():
    texts = {'c1': '甲的代码是 K1。', 'c2': '乙是代码 K1 的别名。'}
    ontology = '@startuml\nclass A {\ncode: String\ndescription: String\n}\n@enduml'
    def model(stage, p, c):
        ch = c['chunk_id']
        if stage == 'entities':
            if ch == 'c2':
                existing = next(x for x in p['entities'] if x['name'] == '甲')
                assert existing['properties'] == {'code': 'K1'}
                assert 'description' in existing['available_property_ids']
                return {'entities': [_entity('alias', 'A', '乙', texts[ch], chunk=ch, properties={'code': 'K1'})], 'alias_pairs': [{'left': existing['candidate_id'], 'right': 'alias'}]}
            entity = _entity('a', 'A', '甲', texts[ch], chunk=ch, properties={'code': 'K1', 'description': '说明'})
            entity['context_property_ids'] = ['code']
            return {'entities': [entity]}
        return _empty(stage)
    result, _ = run(ontology, texts, model, max_repair_rounds=0)
    assert result['status'] == 'succeeded' and len(result['raw']['entities']) == 1


@pytest.mark.parametrize('change', ['name', 'property', 'condition'])
def test_semantic_endpoint_or_condition_change_retires_old_relation(change):
    text = '甲在旧范围内协助乙，条件是丙。新范围需要重新判断。'
    ontology = '@startuml\nclass A {\nscope: String\nnote: String[]\n}\nclass B\nclass C {\nscope: String\nnote: String[]\n}\nA --> B : assists\nA --> C : constrainedBy\n@enduml'
    relation_calls = 0
    def model(stage, p, c):
        nonlocal relation_calls
        if stage == 'entities': return {'entities': [_entity('a', 'A', '甲', text, properties={'scope': '旧', 'note': '待修复'}), _entity('b', 'B', '乙', text), _entity('c', 'C', '丙', text, properties={'scope': '旧'})]}
        if stage == 'relations':
            relation_calls += 1
            if relation_calls > 1: return _empty(stage)
            return {'relations': [_relation(p, '甲', 'assists', '乙', text, condition_status='represented', condition_entity_ids=[_catalog_id(p, '丙')]), _relation(p, '甲', 'constrainedBy', '丙', text)]}
        if stage == 'repair':
            updates = [{'candidate_id': _catalog_id(p, '甲'), 'properties': {'note': ['已修复']}, 'evidence': [{'chunk_id': 'source-A', 'quote': text}]}]
            if change == 'name': updates[0]['name'] = '甲新称'
            if change == 'property': updates[0]['properties']['scope'] = '新'
            if change == 'condition': updates.append({'candidate_id': 'C::丙', 'properties': {'scope': '新'}, 'evidence': [{'chunk_id': 'source-A', 'quote': text}]})
            return {'updates': updates}
        return _empty(stage)
    def select(task):
        if task.kind == 'relation_support' and 'note' in task.state['claims'][task.task_id]['source']['properties']:
            return 'contradicted'  # The semantic peer rejects the revised scope.
        return 'supported'
    result, _ = run(ontology, {'source-A': text}, model, select, max_llm_calls=30)
    assert relation_calls == 2
    assert not result['raw']['relations']
    tasks = [t for t in result['run_trace']['tasks'] if t['kind'] == 'relation_support']
    assert all(t['status'] == 'superseded' for t in tasks if not t.get('revalidated_from'))
    assert len([t for t in tasks if t.get('revalidated_from') and t['status'] == 'rejected']) == 2


def test_alias_rebinding_retires_old_claim_and_revalidates_new_endpoint():
    text = '甲也称丙，甲协助乙；其他说明为好。'
    ontology = '@startuml\nclass A\nclass B\nclass C {\nnote: String[]\n}\nA --> B : assists\n@enduml'
    relation_calls = 0
    def model(stage, p, c):
        nonlocal relation_calls
        if stage == 'entities': return {'entities': [_entity('a', 'A', '甲', text), _entity('alias', 'A', '丙', text), _entity('b', 'B', '乙', text), _entity('c', 'C', '其他', text, properties={'note': '好'})]}
        if stage == 'relations':
            relation_calls += 1
            return {'relations': [_relation(p, '甲' if relation_calls == 1 else '丙', 'assists', '乙', text)]}
        if stage == 'repair': return {'updates': [{'candidate_id': _catalog_id(p, '其他'), 'properties': {'note': ['好']}, 'evidence': [{'chunk_id': 'source-A', 'quote': text}]}], 'alias_pairs': [{'left': 'A::丙', 'right': 'A::甲'}]}
        return _empty(stage)
    result, _ = run(ontology, {'source-A': text}, model, max_llm_calls=30)
    assert result['status'] == 'succeeded'
    assert relation_calls == 2
    assert [(x['from_name'], x['rel_type'], x['to_name']) for x in result['raw']['relations']] == [('丙', 'assists', '乙')]
    tasks = [x for x in result['run_trace']['tasks'] if x['kind'] == 'relation_support']
    assert [x['status'] for x in tasks] == ['superseded', 'accepted', 'accepted']
    assert tasks[1]['revalidated_from'] == tasks[0]['task_id']
    assert tasks[2]['reused_from'] == tasks[1]['task_id']


def test_explicit_rename_with_unchanged_declared_identity_updates_name_and_rechecks_relation():
    text = '代码K1的甲正式名称是甲新称，甲曾协助乙。'
    ontology = "@startuml\nclass A {\ncode: String\nnote: String[]\n}\nclass B\n' @identity A: code\nA --> B : assists\n@enduml"
    relation_calls = 0
    def model(stage, p, c):
        nonlocal relation_calls
        if stage == 'entities': return {'entities': [_entity('a', 'A', '甲', text, properties={'code': 'K1', 'note': '好'}), _entity('b', 'B', '乙', text)]}
        if stage == 'relations':
            relation_calls += 1
            return {'relations': [_relation(p, '甲', 'assists', '乙', text)]} if relation_calls == 1 else _empty(stage)
        if stage == 'repair': return {'updates': [{'candidate_id': _catalog_id(p, '甲'), 'name': '甲新称', 'properties': {'note': ['好']}, 'evidence': [{'chunk_id': 'source-A', 'quote': text}]}]}
        return _empty(stage)
    def select(task):
        if task.kind == 'relation_support' and task.state['claims'][task.task_id]['source']['name'] == '甲新称':
            return 'contradicted'
        return 'supported'
    result, _ = run(ontology, {'source-A': text}, model, select, max_llm_calls=30)
    obj = next(x for x in result['intermediate_model']['objects'] if x['type'] == 'A')
    assert result['status'] == 'succeeded'
    assert obj['identity'] == 'K1' and obj['display_name'] == '甲新称'
    assert relation_calls == 2 and not result['raw']['relations']
    tasks = [t for t in result['run_trace']['tasks'] if t['kind'] == 'relation_support']
    assert [t['status'] for t in tasks] == ['superseded', 'rejected']
    assert tasks[1]['revalidated_from'] == tasks[0]['task_id']
