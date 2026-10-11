"""Generic decision lifecycle regressions with deterministic semantic peers."""
from copy import deepcopy
import time

import pytest

from algorithm.knowledge_extract.v2.ontology_schema import OntologySchema
from algorithm.knowledge_extract.v3_1 import ExtractionChunk, extract_aggregate
from algorithm.knowledge_extract.v3_1.controller import Controller
from algorithm.knowledge_extract.v3_1.harness import Harness
from algorithm.knowledge_extract.v3_1.tests.test_semantic_contracts import (
    ScriptedLLM, SemanticDecider, _entity, _empty, _catalog_id, _relation,
)


TEXT = '甲以旧范围协助乙，修订后仍可协助；甲的补充值为新。'
ONTOLOGY = '@startuml\nclass A {\nscope: String\nnote: String\nextra: String\n}\nclass B\nA --> B : serves\n@enduml'


def harness(model=None, selector=None, *, max_calls=8, ontology=ONTOLOGY):
    schema = OntologySchema.parse(ontology)
    llm = ScriptedLLM(model or (lambda stage, payload, context: _empty(stage)))
    control = Controller(llm, deadline=time.monotonic() + 60, timeout=10, max_calls=max_calls)
    decider = SemanticDecider(selector)
    h = Harness(ontology=ontology, chunks=[ExtractionChunk('c', TEXT), ExtractionChunk('d', TEXT)],
                schema=schema, allowed={cls.identifier for cls in schema.classes},
                controller=control, decider=decider, mode='strict')
    h.entities([_entity('a', 'A', '甲', TEXT, chunk='c', properties={'scope': '旧'}),
                _entity('b', 'B', '乙', TEXT, chunk='c')], 'c', 'entities')
    return h, llm, decider


def add_relation(h, *, chunk='c', **extra):
    h.relation_items([{'from_id': 'A::甲', 'to_id': 'B::乙', 'rel_type': 'serves',
                       'evidence': [{'chunk_id': chunk, 'quote': TEXT}],
                       'fact': '甲在所述范围内协助乙', **extra}], chunk)
    return h.tasks[-1]


def duplicate(h, old, **changes):
    args = {'kind': old['kind'], 'claim': deepcopy(old['claim']),
            'evidence': deepcopy(old['original_evidence']), 'chunk': old['chunk'],
            'criteria': deepcopy(old['criteria']), 'positive': old['positive_proposal']}
    args.update(changes)
    return h.task(**args)


def test_identical_supported_claim_reuses_across_revision_without_new_attempt():
    h, _, decider = harness()
    old = add_relation(h)
    before = len(decider.tasks)
    h.revision += 1
    new = duplicate(h, old)
    h.decide([new])
    h.materialize_resolved([new])
    assert new['status'] == 'accepted' and new['reused_from'] == old['task_id']
    assert len(decider.tasks) == before and h.control.calls == 0
    assert old['status'] == 'accepted'
    assert h.clef_trace[-1]['type'] == 'decision_reuse'


@pytest.mark.parametrize('change', ['quote', 'offset', 'source', 'properties', 'condition', 'fact', 'criteria', 'schema', 'ontology', 'anchor'])
def test_partial_similarity_never_reuses_full_semantic_task(change):
    h, _, decider = harness()
    old = add_relation(h)
    args = {}
    if change in {'quote', 'offset', 'source'}:
        evidence = deepcopy(old['original_evidence'])
        if change == 'quote': evidence[0]['quote'] = '甲'
        if change == 'offset': evidence[0]['start_char'] += 1
        if change == 'source': evidence[0]['source_block_ids'] = ['d']
        args['evidence'] = evidence
    elif change in {'properties', 'condition', 'fact'}:
        claim = deepcopy(old['claim'])
        if change == 'properties': claim['source']['properties']['note'] = '新'
        if change == 'condition': claim['condition_status'] = 'represented'
        if change == 'fact': claim['fact'] = '甲仅在特殊范围协助乙'
        args['claim'] = claim
    elif change == 'criteria': args['criteria'] = dict(reversed(list(old['criteria'].items())))
    elif change == 'schema': h.contract['classes'][0]['properties'][0]['description'] = '新的属性意义'
    elif change == 'ontology': h.ontology += '\n新约束'
    elif change == 'anchor': args['chunk'] = 'd'
    before = len(decider.tasks)
    new = duplicate(h, old, **args)
    h.decide([new], allow_resolve=False)
    assert 'reused_from' not in new and len(decider.tasks) == before + 1


@pytest.mark.parametrize('status,selected', [('superseded', 'supported'), ('unresolved', 'supported'),
                                          ('decided', 'insufficient'), ('resolved_no_support', 'insufficient')])
def test_nonfinal_or_superseded_history_cannot_be_reused(status, selected):
    h, _, decider = harness()
    old = add_relation(h)
    old.update(status=status, selected=selected)
    new = duplicate(h, old)
    before = len(decider.tasks)
    h.decide([new], allow_resolve=False)
    assert 'reused_from' not in new and len(decider.tasks) == before + 1


def test_same_batch_unreviewed_duplicates_do_not_share_an_answer():
    h, _, decider = harness()
    claim = {'candidate_id': 'A::甲', 'type': 'A', 'name': '甲', 'properties': {'note': '新'}}
    first = h.task('attribute_support', claim, h.objects['A::甲']['evidence'], 'c')
    second = duplicate(h, first)
    before = len(decider.tasks)
    h.decide([first, second])
    assert len(decider.tasks) == before + 2
    assert 'reused_from' not in first and 'reused_from' not in second


def test_llm_expanded_evidence_keeps_original_input_key_and_verified_output():
    def model(stage, payload, context):
        if stage == 'resolve':
            return {'decisions': [{'task_id': task['task_id'], 'selected': 'supported', 'context_complete': True,
                                  'evidence': [{'chunk_id': 'c', 'quote': TEXT}]} for task in payload['tasks']]}
        return _empty(stage)
    h, _, decider = harness(model, lambda task: 'insufficient' if task.kind == 'relation_support' else 'supported')
    old = add_relation(h)
    # Real decision was based on a narrow anchored input, expanded by the resolver.
    # Obtain a fresh such task through the same public lifecycle.
    h.relations.clear()
    old['status'] = 'superseded'
    from algorithm.knowledge_extract.v3_1.evidence import locate
    narrow = locate([{'chunk_id': 'c', 'quote': '甲以旧范围协助乙'}], h.context, 'c')
    first = duplicate(h, old, evidence=narrow)
    h.decide([first])
    h.materialize_resolved([first])
    assert first['evidence'] != first['original_evidence']
    count = len(decider.tasks)
    next_task = duplicate(h, first)
    h.decide([next_task])
    assert next_task['reused_from'] == first['task_id'] and next_task['evidence'] == first['evidence']
    assert next_task['original_evidence'] == narrow and len(decider.tasks) == count
    different_input = duplicate(h, first, evidence=first['evidence'])
    h.decide([different_input], allow_resolve=False)
    assert 'reused_from' not in different_input


@pytest.mark.parametrize('change', ['name', 'type', 'identity', 'delete', 'property'])
def test_entity_repair_cannot_resurrect_old_accepted_assertion(change):
    h, _, decider = harness()
    old = h.tasks[0]
    obj = h.objects['A::甲']
    if change == 'name': obj['display_name'] = '另一名'
    if change == 'type': obj['type'] = 'B'
    if change == 'identity': obj['identity'] = '另一身份'
    if change == 'delete': del h.objects['A::甲']
    if change == 'property': obj['properties']['scope'] = '新'
    new = duplicate(h, old)
    before = len(decider.tasks)
    h.decide([new], allow_resolve=False)
    assert 'reused_from' not in new and len(decider.tasks) == before + 1


def test_same_accepted_attribute_value_does_not_spend_or_attach_unreviewed_quote():
    h, _, decider = harness()
    before = deepcopy(h.objects['A::甲']['evidence'])
    count = len(decider.tasks)
    h.attributes([{'candidate_id': 'A::甲', 'properties': {'scope': '旧'},
                   'evidence': [{'chunk_id': 'd', 'quote': TEXT}]}], 'd')
    assert h.objects['A::甲']['evidence'] == before and len(decider.tasks) == count
    h.attributes([{'candidate_id': 'A::甲', 'properties': {'scope': '旧', 'note': '新'},
                   'evidence': [{'chunk_id': 'd', 'quote': TEXT}]}], 'd')
    assert decider.tasks[-1].state['claims'][decider.tasks[-1].task_id]['properties'] == {'note': '新'}
    assert h.objects['A::甲']['properties']['note'] == '新'
    assert len(decider.tasks) == count + 1


@pytest.mark.parametrize('verdict', ['supported', 'contradicted', 'insufficient'])
def test_attribute_only_repair_rechecks_prior_fact_before_search_even_after_resolve_quota(verdict):
    def model(stage, payload, context):
        if stage == 'repair':
            return {'updates': [{'candidate_id': 'A::甲', 'properties': {'note': '新'},
                                 'evidence': [{'chunk_id': 'c', 'quote': TEXT}]}]}
        if stage == 'resolve':
            assert payload['purpose'] == 'repair_relation_reverification'
            claim = payload['tasks'][0]['claim']
            assert claim['source']['properties'] == {'scope': '旧', 'note': '新'}
            assert claim['fact'] == '甲在所述范围内协助乙'
            task = payload['tasks'][0]
            assert all(key not in task for key in ('selected', 'provider', 'clef', 'revalidated_from'))
            ref = task['evidence_refs'][0]
            source = next(source for source in payload['sources'] if source['chunk_id'] == ref['chunk_id'])
            original = ''.join(span['text'] for span in source['spans'])
            assert original[ref['start_char']:ref['end_char']] == TEXT
            return {'decisions': [{'task_id': task['task_id'], 'selected': verdict, 'context_complete': True,
                                  'evidence': [{'chunk_id': 'c', 'quote': TEXT}]} for task in payload['tasks']]}
        return _empty(stage)
    def select(task):
        if task.kind == 'relation_support' and 'note' in task.state['claims'][task.task_id]['source']['properties']:
            return 'insufficient'
        return 'supported'
    h, llm, _ = harness(model, select, max_calls=3)
    old = add_relation(h)
    h.resolve_calls['c'] = 2
    h.issue('SEMANTIC_SCOPE_UNRESOLVED', 'c', 'attributes')
    h.repair()
    assert [call['task'] for call in llm.calls] == ['repair', 'resolve', 'relations']
    assert h.control.calls == 3 and h.resolve_calls['c'] == 2
    assert old['status'] == 'superseded'
    new = next(task for task in h.tasks if task.get('revalidated_from') == old['task_id'])
    assert 'reused_from' not in new
    assert new['status'] == {'supported': 'accepted', 'contradicted': 'rejected', 'insufficient': 'unresolved'}[verdict]
    assert bool(h.relations) == (verdict == 'supported')


def test_recheck_respects_total_budget_and_never_keeps_unverified_old_edge():
    h, _, _ = harness(selector=lambda task: 'supported', max_calls=1)
    old = add_relation(h)
    h.objects['A::甲']['properties']['note'] = '新'
    h.decider = SemanticDecider(lambda task: 'insufficient')
    h.control.calls = 1
    h.recheck_relations()
    assert old['status'] == 'superseded' and not h.relations
    assert h.control.calls == 1
    assert any(issue['code'] == 'LLM_CALL_BUDGET_EXHAUSTED' for issue in h.issues)


def test_recheck_packs_multiple_chunks_once_and_preserves_condition_snapshots():
    def model(stage, payload, context):
        if stage == 'resolve':
            assert len(payload['tasks']) == 2
            assert {source['chunk_id'] for source in payload['sources']} == {'c', 'd'}
            return {'decisions': [{'task_id': item['task_id'], 'selected': 'supported', 'context_complete': True,
                                  'evidence': [{'chunk_id': 'c', 'quote': TEXT}]} for item in payload['tasks']]}
        return _empty(stage)
    h, llm, _ = harness(model)
    first = add_relation(h, condition_status='represented', condition_entity_ids=['A::甲'])
    add_relation(h, chunk='d')
    h.objects['A::甲']['properties']['note'] = '新'
    h.decider = SemanticDecider(lambda task: 'insufficient')
    h.recheck_relations()
    assert len(llm.calls) == 1 and len(h.relations) == 2
    current = next(task for task in h.tasks if task.get('revalidated_from') == first['task_id'])
    assert current['claim']['condition_entities'][0]['properties']['note'] == '新'
    h.objects['A::甲']['properties']['note'] = '再变'
    h.recheck_relations()
    assert len(llm.calls) == 1 and not h.relations


def test_unrelated_object_change_does_not_trigger_relation_recheck():
    ontology = ONTOLOGY.replace('@enduml', 'class C {\nnote: String\n}\n@enduml')
    h, llm, decider = harness(ontology=ontology)
    h.entities([_entity('z', 'C', '丙', TEXT, chunk='c')], 'c', 'entities')
    old = add_relation(h)
    count = len(decider.tasks)
    h.objects['C::丙']['properties']['note'] = '新'
    h.recheck_relations()
    assert old['status'] == 'accepted' and len(h.relations) == 1
    assert len(decider.tasks) == count and not llm.calls


def test_default_document_budget_preserves_reverified_relation_without_attribute_rescan():
    def model(stage, payload, context):
        if stage == 'entities':
            return {'entities': [_entity('a', 'A', '甲', TEXT, chunk='c', properties={'scope': '旧'}),
                                 _entity('b', 'B', '乙', TEXT, chunk='c')]}
        if stage == 'attributes':
            return {'updates': [{'candidate_id': 'A::甲', 'properties': {'note': '新'},
                                 'evidence': [{'chunk_id': 'c', 'quote': TEXT}]}]}
        if stage == 'relations':
            return {'relations': [_relation(payload, '甲', 'serves', '乙', TEXT, chunk='c', fact='甲在所述范围内协助乙')]}
        if stage in {'resolve', 'repair'}:
            tasks = [task for task in payload['tasks']
                     if stage == 'repair' or task['kind'] == 'relation_support']
            return {'decisions': [{'task_id': task['task_id'], 'selected': 'supported', 'context_complete': True,
                                  'evidence': [{'chunk_id': 'c', 'quote': TEXT}]} for task in tasks]}
        return _empty(stage)
    llm = ScriptedLLM(model)
    result = extract_aggregate(ONTOLOGY, [ExtractionChunk('c', TEXT)], llm=llm,
                               decider=SemanticDecider(lambda task: 'supported' if task.kind == 'entity_support' else 'insufficient'),
                               use_scheduler=False)
    assert result['status'] == 'succeeded'
    assert len(result['raw']['relations']) == 1 and result['profile']['llm_calls'] == 8
    assert [call['task'] for call in llm.calls] == ['entities', 'attributes', 'resolve', 'relations', 'resolve', 'repair', 'resolve', 'relations']
    relations = [task for task in result['run_trace']['tasks'] if task['kind'] == 'relation_support']
    assert [task['status'] for task in relations] == ['superseded', 'accepted', 'accepted']
    assert relations[1]['revalidated_from'] == relations[0]['task_id']
    assert relations[2]['reused_from'] == relations[1]['task_id']


def test_llm_result_cannot_reuse_after_its_resolve_source_context_changes():
    from algorithm.knowledge_extract.v2.chunk_context import ChunkContextManager
    def model(stage, payload, context):
        if stage == 'resolve':
            return {'decisions': [{'task_id': task['task_id'], 'selected': 'supported', 'context_complete': True,
                                  'evidence': [{'chunk_id': 'c', 'quote': TEXT}]} for task in payload['tasks']]}
        return _empty(stage)
    h, _, decider = harness(model, lambda task: 'insufficient' if task.kind == 'relation_support' else 'supported')
    h.chunks = [ExtractionChunk(key, TEXT) for key in ('c', 'n', 'm', 'd')]
    h.context = ChunkContextManager(h.chunks)
    old = add_relation(h)
    assert old['resolution_source_chunk_ids'] == ['c', 'n']
    # Same anchor and exact old assertion, but an additional task brings a
    # remote original into this resolve batch. Its context is not the old read.
    first = duplicate(h, old)
    second = duplicate(h, old, evidence=[{**old['original_evidence'][0], 'source_block_ids': ['d']}])
    before = len(decider.tasks)
    h.decide([first, second], allow_resolve=False)
    assert 'reused_from' not in first and len(decider.tasks) == before + 2


def test_attribute_conflict_reuse_keeps_new_issue_binding_and_never_overwrites_a_newer_value():
    def select(task):
        return 'value_0' if task.kind == 'attribute_conflict' else 'supported'
    h, _, decider = harness(selector=select)
    first_issue = h.issue('PROPERTY_CONFLICT', 'c', 'attributes', claim={
        'candidate_id': 'A::甲', 'property': 'scope', 'values': ['旧', '新']})
    h.resolve_property_conflicts()
    first = h.tasks[-1]
    second_issue = h.issue('PROPERTY_CONFLICT', 'c', 'attributes', claim=first_issue['claim'])
    before = len(decider.tasks)
    h.resolve_property_conflicts()
    second = h.tasks[-1]
    assert second['reused_from'] == first['task_id'] and second['issue_id'] == second_issue['id']
    assert second_issue['status'] == 'resolved' and len(decider.tasks) == before
    h.objects['A::甲']['properties']['scope'] = '后续修复'
    third_issue = h.issue('PROPERTY_CONFLICT', 'c', 'attributes', claim=first_issue['claim'])
    h.decider = SemanticDecider(lambda task: 'UNKNOWN')
    h.resolve_calls['c'] = 2
    h.resolve_property_conflicts()
    assert 'reused_from' not in h.tasks[-1]
    assert third_issue['status'] == 'unresolved' and h.objects['A::甲']['properties']['scope'] == '后续修复'


def test_boolean_and_numeric_map_values_are_distinct_changes_and_invalidate_prior_relation():
    h, _, decider = harness(ontology=ONTOLOGY.replace('note: String', 'note: Map'))
    update = {'candidate_id': 'A::甲', 'properties': {'note': {'flag': True}},
              'evidence': [{'chunk_id': 'c', 'quote': TEXT}]}
    h.attributes([update], 'c')
    old = add_relation(h)
    count = len(decider.tasks)
    numeric = {**update, 'properties': {'note': {'flag': 1}}}
    h.attributes([numeric], 'c')
    assert len(decider.tasks) == count + 1  # This is not an unchanged assertion.
    assert any(issue['code'] == 'PROPERTY_CONFLICT' for issue in h.issues)
    assert h.objects['A::甲']['properties']['note']['flag'] is True
    assert h.attributes([numeric], 'c', stage='repair') is True
    assert type(h.objects['A::甲']['properties']['note']['flag']) is int
    h.retire_stale_relations()
    assert old['status'] == 'superseded' and not h.relations


@pytest.mark.parametrize('stage', ['resolve', 'repair'])
@pytest.mark.parametrize('foreign_document', [False, True])
def test_semantic_decisions_cannot_cite_a_valid_quote_outside_their_actual_read(stage, foreign_document):
    from algorithm.knowledge_extract.v2.chunk_context import ChunkContextManager
    def model(current_stage, payload, context):
        if current_stage == stage:
            assert 'd' not in {source['chunk_id'] for source in payload['sources']}
            return {'decisions': [{'task_id': task['task_id'], 'selected': 'supported', 'context_complete': True,
                                  'evidence': [{'chunk_id': 'd', 'quote': TEXT}]} for task in payload['tasks']]}
        return _empty(current_stage)
    h, _, _ = harness(model, lambda task: 'insufficient' if task.kind == 'relation_support' else 'supported')
    h.chunks = [ExtractionChunk(key, TEXT, document_id='foreign' if key == 'd' and foreign_document else 'same')
                for key in ('c', 'n', 'm', 'd')]
    h.context = ChunkContextManager(h.chunks)
    relation = add_relation(h)
    if stage == 'repair': h.repair()
    assert relation['status'] == 'unresolved' and not h.relations
    assert any(issue['code'] == 'DECISION_EVIDENCE_NOT_READ' and issue['status'] == 'unresolved' for issue in h.issues)
