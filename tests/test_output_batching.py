"""Truncated model responses must shrink work, retain siblings and fail honestly."""
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from algorithm.output_batching import generate_batched, output_batch_size


@pytest.fixture(params=['v2', 'v3'])
def cg(request):
    name = '_output_batch_test_' + request.param
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / 'algorithm' / 'knowledge_extract' / request.param)]
    sys.modules[name] = package
    return importlib.import_module(name + '.candidate_generation')


def build_request(items):
    return ([{'role': 'system', 'content': 'Extract'},
             {'role': 'user', 'content': json.dumps(items)}],
            {'type': 'object', 'properties': {'updates': {'type': 'array', 'items': {
                'type': 'object', 'properties': {'id': {'type': 'integer', 'enum': items}},
                'required': ['id'], 'additionalProperties': False}}},
             'required': ['updates'], 'additionalProperties': False})


class Response:
    def __init__(self, text, reason):
        self.text = text
        self.finish_reason = reason

    def __str__(self):
        return self.text


class Model:
    max_tokens = 16384

    def __init__(self, *, fail_id=None, transport_once=False):
        self.calls = []
        self.fail_id = fail_id
        self.transport_once = transport_once

    def generate_structured(self, schema, messages, context):
        ids = schema['properties']['updates']['items']['properties']['id']['enum']
        self.calls.append(list(ids))
        if self.transport_once:
            self.transport_once = False
            raise TimeoutError('temporary timeout')
        if len(ids) > 1 or ids == [self.fail_id]:
            return Response('{"updates":[{"id":', 'length')
        return Response(json.dumps({'updates': [{'id': ids[0]}]}), 'stop')


def run(cg, model, **options):
    attempts = []
    metrics = []
    result = generate_batched(
        [1, 2, 3, 4], build_request, cg.generate_structured, model,
        batch_size=4, attempts=attempts, pass_name='property', chunk_id='c1',
        max_attempts=3, retry_base_seconds=0, use_scheduler=False,
        call_metrics=metrics, **options,
    )
    return result, attempts, metrics


def test_first_truncation_splits_without_repeating_parent(cg):
    model = Model()
    result, attempts, metrics = run(cg, model)
    assert model.calls == [[1, 2, 3, 4], [1, 2], [1], [2], [3], [4]]
    assert result == {'updates': [{'id': i} for i in [1, 2, 3, 4]]}
    assert not any(row['terminal'] for row in attempts)
    assert len(metrics) == 6  # Every actual model call stays observable.


def test_truncation_never_retries_identical_work_without_split_caller(cg):
    model = Model()
    messages, schema = build_request([1, 2])
    attempts = []
    result = cg.generate_structured(
        model, messages, schema, use_scheduler=False, attempts=attempts,
        pass_name='entity', chunk_id='c1', max_attempts=3, retry_base_seconds=0,
    )
    assert result is None
    assert model.calls == [[1, 2]]
    assert attempts[-1]['type'] == 'incomplete' and attempts[-1]['terminal']
    assert attempts[-1]['retryable'] is False


def test_failed_singleton_preserves_successful_siblings_and_failure(cg):
    model = Model(fail_id=2)
    result, attempts, _ = run(cg, model)
    assert result == {'updates': [{'id': i} for i in [1, 3, 4]]}
    assert model.calls.count([2]) == 1
    terminal = [row for row in attempts if row['terminal']]
    assert len(terminal) == 1 and terminal[0]['type'] == 'incomplete'


def test_transport_failure_retries_same_batch_before_split(cg):
    model = Model(transport_once=True)
    result, attempts, _ = run(cg, model)
    assert model.calls[:2] == [[1, 2, 3, 4], [1, 2, 3, 4]]
    assert len(result['updates']) == 4
    assert any(row['type'] == 'timeout' for row in attempts)
    assert not any(row['terminal'] for row in attempts)


@pytest.mark.parametrize('limits', [{'max_calls': 3}, {'max_depth': 1}])
def test_split_budget_never_silently_reports_success(cg, limits):
    model = Model()
    result, attempts, _ = run(cg, model, **limits)
    assert any(row['terminal'] for row in attempts)
    if 'max_calls' in limits:
        assert len(model.calls) == 3
        assert result == {'updates': [{'id': 1}]}
    else:
        assert len(model.calls) == 3 and result is None


def test_batch_budget_tracks_output_limit_and_density():
    assert output_batch_size(SimpleNamespace(max_tokens=4096), tokens_per_item=1024, ceiling=24) == 2
    assert output_batch_size(SimpleNamespace(max_tokens=16384), tokens_per_item=1024, ceiling=24) == 10
    assert output_batch_size(SimpleNamespace(max_tokens=16384), tokens_per_item=2048, ceiling=24) == 5


def test_expired_document_does_not_start_more_batches(cg):
    model = Model()
    model.client = SimpleNamespace(deadline=0.0)
    result, attempts, _ = run(cg, model)
    assert result is None and model.calls == []
    assert attempts[-1]['terminal'] and attempts[-1]['type'] == 'timeout'


def test_input_limit_splits_without_identical_retries(cg):
    from algorithm.token_budget import InputTokenLimitError

    class InputModel(Model):
        def generate_structured(self, schema, messages, context):
            ids = schema['properties']['updates']['items']['properties']['id']['enum']
            if len(ids) > 1:
                self.calls.append(list(ids))
                raise InputTokenLimitError('Estimated input exceeds budget')
            return super().generate_structured(schema, messages, context)

    model = InputModel()
    result, attempts, _ = run(cg, model)
    assert len(result['updates']) == 4
    assert model.calls == [[1, 2, 3, 4], [1, 2], [1], [2], [3], [4]]
    assert all(row['type'] == 'input_limit' and not row['terminal'] for row in attempts)


def test_input_estimate_includes_schema_and_utf8_text():
    from algorithm.token_budget import check_input_budget, estimate_input_tokens, InputTokenLimitError
    payload = {'messages': [{'role': 'user', 'content': '设备'}]}
    before = estimate_input_tokens(payload)
    payload['response_format'] = {'type': 'json_schema', 'json_schema': {'description': '属性' * 100}}
    after = estimate_input_tokens(payload)
    assert after > before + 500
    with pytest.raises(InputTokenLimitError):
        check_input_budget(payload, before)
    assert check_input_budget(payload, after) == after


def test_property_stage_rebuilds_catalog_and_schema_after_truncation(cg, monkeypatch):
    package = cg.__package__
    pe = importlib.import_module(package + '.property_enrichment')
    compiler = importlib.import_module(package + '.ontology_compiler')
    cc = importlib.import_module(package + '.chunk_context')
    evidence = importlib.import_module(package + '.evidence')
    plan = compiler.compile_plan('''@startuml
class "设备" as Device {
  + 用途说明 (description): String
}
@enduml''')
    chunk = cc.ExtractionChunk(chunk_id='c1', text='设备A、设备B用于检测。')
    manager = cc.ChunkContextManager([chunk])
    entities = [SimpleNamespace(
        candidate_id=key, canonical_type='Device', proposed_type='Device', name=name,
        properties={}, property_evidence={}, source='llm',
        evidence=[evidence.Evidence(span_id='c1:0:12', source_block_ids=['c1'],
                                    quote=chunk.text, start_char=0, end_char=len(chunk.text))],
    ) for key, name in [('a', '设备A'), ('b', '设备B')]]
    observed = []
    original = pe.build_chat_messages

    def build(**kwargs):
        ids = kwargs['output_schema']['properties']['updates']['items']['properties']['entity_id']['enum']
        assert ids == [row['id'] for row in kwargs['known_entities']]
        observed.append(list(ids))
        return original(**kwargs)

    monkeypatch.setattr(pe, 'build_chat_messages', build)
    monkeypatch.setattr(pe, 'verify_items_with_llm',
                        lambda _llm, **kw: {item['item_id']: 'yes' for item in kw['items']})

    class PropertyModel:
        max_tokens = 16384

        def generate_structured(self, schema, messages, context):
            ids = schema['properties']['updates']['items']['properties']['entity_id']['enum']
            if len(ids) > 1:
                return Response('{"updates":[', 'length')
            return Response(json.dumps({'updates': [{'entity_id': ids[0],
                'properties': {'description': '用于检测'}, 'evidence': {'quote': chunk.text}}]}), 'stop')

    attempts = []
    kwargs = {'max_coverage_rounds': 0} if package.endswith('v3') else {}
    summary = pe.enrich_entity_properties(
        SimpleNamespace(accepted_entities=lambda: entities), manager, plan, PropertyModel(),
        route=pe.DocumentRoute(kind='generic', source='fallback', scores={}, reasons=[]), attempts=attempts,
        warnings=[], max_attempts=3, retry_base_seconds=0, **kwargs,
    )
    assert observed == [['a', 'b'], ['a'], ['b']]
    assert summary['calls'] == 3
    assert summary['applied'] == 2
    assert all(entity.properties == {'description': '用于检测'} for entity in entities)
    assert not any(row['terminal'] for row in attempts)
