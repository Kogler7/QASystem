import copy
import importlib
import json
from types import SimpleNamespace

import pytest

from algorithm.knowledge_extract.repair_scope import relation_repair_scope


VERSIONS = ['v2', 'v2_2', 'v3', 'v3_1', 'latest']
ONTOLOGY = '''@startuml
class Constraint {}
class Agent {}
class Document {}
Constraint --> Agent : CONSTRAINS_AGENT
Document --> Agent : APPLIES_TO
@enduml'''


def issue(rel='CONSTRAINS_AGENT'):
    return {'code': 'REQUIRED_RELATION_MISSING', 'repair_type': 'required_relation',
            'context': {'target_type': 'Agent', 'expected_relations': [rel]}}


def fixtures(version):
    root = 'algorithm.knowledge_extract.' + version
    compiler = importlib.import_module(root + '.ontology_compiler')
    cg = importlib.import_module(root + '.candidate_generation')
    plan = compiler.compile_plan(ONTOLOGY)
    catalog = [{'id': 'entity-0', 'type': 'Document', 'name': '通知', 'properties': {}},
               {'id': 'entity-1', 'type': 'Agent', 'name': '部门', 'properties': {}}]
    schema = cg.output_schema_for_types(plan, 'relation_candidates', {e['type'] for e in catalog})
    return plan, catalog, schema


@pytest.mark.parametrize('version', VERSIONS)
def test_missing_constraint_skipped_without_widening_or_mutating_inputs(version):
    plan, catalog, schema = fixtures(version)
    issues = [issue(), issue('APPLIES_TO')]
    snapshot = copy.deepcopy((catalog, issues, schema, plan.output_schemas))
    result = relation_repair_scope(plan, catalog, issues, schema)
    assert result.issues == [issue('APPLIES_TO')]
    assert len(result.skipped) == 1
    assert result.skipped[0]['unavailable_relations'] == ['CONSTRAINS_AGENT']
    item = result.schema['properties']['relations']['items']['properties']
    assert item['rel_type']['enum'] == ['APPLIES_TO']
    assert item['from_id']['enum'] == ['entity-0', 'entity-1']
    assert '`Constraint`' not in result.signatures
    assert (catalog, issues, schema, plan.output_schemas) == snapshot


@pytest.mark.parametrize('version', VERSIONS)
def test_valid_endpoint_after_25_is_visible_and_executable(version):
    plan, catalog, _ = fixtures(version)
    catalog += [{'id': f'entity-{i}', 'type': 'Agent', 'name': str(i), 'properties': {}} for i in range(2, 26)]
    catalog += [{'id': 'entity-26', 'type': 'Constraint', 'name': '合规要求', 'properties': {}}]
    schema = plan.output_schemas['relation_candidates']
    scope = relation_repair_scope(plan, catalog, [issue()], schema)
    assert scope.issues == [issue()]
    assert not scope.skipped
    item = scope.schema['properties']['relations']['items']['properties']
    assert 'entity-26' in item['from_id']['enum']
    assert 'CONSTRAINS_AGENT' in item['rel_type']['enum']
    pr = importlib.import_module('algorithm.knowledge_extract.' + version + '.prompt_router')
    cc = importlib.import_module('algorithm.knowledge_extract.' + version + '.chunk_context')
    chunk = cc.ExtractionChunk('body', '合规要求适用于部门。')
    route = pr.resolve_document_route(plan, [chunk])
    messages = pr.build_chat_messages(task='repair', plan=plan, route=route, chunk=chunk,
                                     known_entities=catalog, output_schema=scope.schema, repair_issues=scope.issues)
    text = messages[1]['content']
    assert 'entity-26' in text
    assert 'CONSTRAINS_AGENT' in text
    if version == 'v3_1':
        user = json.loads(text)
        assert len(user['task']['known_entities']) == 27
        assert user['output_contract']['success_schema'] == scope.schema
    else:
        known = text.split('## known entities\n', 1)[1].split('\n\n##', 1)[0]
        assert '`entity-26`' in known


def test_required_set_not_partially_silenced():
    plan, catalog, schema = fixtures('v2')
    broken = issue('APPLIES_TO')
    broken['context']['expected_relations'].append('CONSTRAINS_AGENT')
    scoped = relation_repair_scope(plan, catalog, [broken], schema)
    assert not scoped.issues
    assert scoped.skipped[0]['issue'] == broken


def test_existing_enum_never_expanded_even_with_both_endpoint_types():
    plan, catalog, schema = fixtures('v2')
    catalog.append({'id': 'entity-2', 'type': 'Constraint'})
    scoped = relation_repair_scope(plan, catalog, [issue()], schema)
    assert not scoped.issues
    assert scoped.schema['properties']['relations']['items']['properties']['rel_type']['enum'] == ['APPLIES_TO']


@pytest.mark.parametrize('version', VERSIONS)
def test_builtin_repair_skips_impossible_request_and_preserves_unresolved_gap(version, monkeypatch):
    root = 'algorithm.knowledge_extract.' + version
    se = importlib.import_module(root + '.split_extractor')
    verification = importlib.import_module(root + '.verification')
    _, catalog, _ = fixtures(version)
    forced = verification.VerificationIssue(**issue(), message='forced missing constraint')
    monkeypatch.setattr(se.ExtractionVerifier, 'verify', lambda *args, **kwargs:
                        verification.VerificationReport((forced,)))
    monkeypatch.setattr(se, 'relation_catalog_for_context', lambda *args, **kwargs: catalog)
    prompts = []
    def llm(messages):
        prompts.append(messages)
        return '{"entities": []}'
    result = se.extract_split(ONTOLOGY, chunks=[se.ExtractionChunk('body', '通知适用于部门。')],
                              llm=llm, max_repair_rounds=1)
    assert any(w['code'] == 'REPAIR_ISSUE_OUT_OF_SCOPE' for w in result['warnings'])
    if version in {'v3', 'latest'}:
        assert result['repair']['stop_reason'] == 'repair_no_new_facts'
        assert result['repair']['unresolved_repairable_count'] == 1
    else:
        assert result['profile']['repair_stop_reason'] == 'repair_no_new_facts'
        assert result['verification']['repairable_issue_count'] == 1
    for messages in prompts:
        user = messages[1]['content']
        if version == 'v3_1':
            assert json.loads(user)['task']['operation'] != 'repair'
        else:
            assert '## task\nrepair' not in user
