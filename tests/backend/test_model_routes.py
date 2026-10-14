"""Approved routes, explicit choices and queued snapshots are production contracts."""
from __future__ import annotations

import json
from copy import deepcopy

import pytest

from external_services import model_routes
from backend.app.services import extraction_config


@pytest.fixture
def registry(monkeypatch):
    data = {'settings': {}, 'models': {
        'qwen': {'enabled': True, 'engine': 'vllm', 'adapter': 'chat', 'manager': 'ops'},
        'clef-flash': {'enabled': True, 'engine': 'native', 'adapter': 'clef', 'manager': 'ops'},
        'nanojev': {'enabled': True, 'engine': 'native', 'adapter': 'nanojev', 'manager': 'ops'},
    }, 'services': {
        'chat': {'kind': 'local', 'model': 'qwen'},
        'clef': {'kind': 'local', 'model': 'clef-flash'},
        'nano': {'kind': 'local', 'model': 'nanojev'},
    }, 'routes': {'extract': 'chat', 'entity_count': 'clef'}}
    monkeypatch.setattr(model_routes, 'registry_snapshot', lambda: (deepcopy(data), 'revision-1'))
    monkeypatch.setattr(extraction_config, 'registry_snapshot', lambda: (deepcopy(data), 'revision-1'))
    from docker import model_services
    monkeypatch.setattr(model_services, 'local_model_endpoint', lambda name, record: {
        'base_url': f'http://local-{name}:8000' + ('/v1' if record['adapter'] == 'chat' else ''),
        'adapter': record['adapter'], 'model': name})
    return data


def test_registered_extraction_route_wins_legacy_address_and_has_no_key(registry, monkeypatch):
    monkeypatch.setenv('KG_EXTRACT_LLM_BASE_URL', 'https://legacy.invalid/v1')
    monkeypatch.setenv('KG_EXTRACT_LLM_MODEL', 'legacy')
    monkeypatch.setenv('KG_EXTRACT_LLM_API_KEY', 'secret-legacy')
    result = extraction_config.resolve_config({'version': 'v3'})
    assert result['base_url'] == 'http://local-qwen:8000/v1'
    assert result['model'] == 'qwen'
    assert result['api_key_env'] == '' and result['credential_required'] is False
    assert result['config_revision'] == 'revision-1'
    assert 'secret-legacy' not in json.dumps(result)


def test_local_factory_never_inherits_remote_api_key(registry, monkeypatch):
    from types import SimpleNamespace
    from algorithm.knowledge_extract import request_runtime
    from algorithm.knowledge_extract.v3 import llm_client
    monkeypatch.setenv('KG_EXTRACT_LLM_API_KEY', 'remote-secret')
    monkeypatch.setenv('KG_EXTRACT_TRACE_DIR', '')
    calls = []
    def factory(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(client=SimpleNamespace())
    monkeypatch.setattr(llm_client, 'create_llm_client', factory)
    request_runtime.create_request_llm(extraction_config.resolve_config({'version': 'v3'}))
    assert calls[0]['api_key'] == 'not-needed'
    assert 'remote-secret' not in json.dumps(calls)


def test_route_change_affects_new_request_only(registry):
    first = extraction_config.resolve_config({'version': 'v3'})
    registry['models']['qwen2'] = registry['models']['qwen'].copy()
    registry['services']['chat']['model'] = 'qwen2'
    second = extraction_config.resolve_config({'version': 'v3'})
    assert first['model'] == 'qwen' and second['model'] == 'qwen2'


def test_services_keep_explicit_account_quota_group_and_share_pool(registry, monkeypatch):
    from external_services.llm import runtime
    registry['accounts'] = {'shared-account': {'max_concurrency':4,'qps':0,'qpm':0}}
    registry['services']['chat']['account_id']='shared-account'
    monkeypatch.setattr(runtime, '_pools', {})
    first = extraction_config.resolve_config({'version': 'v3'})
    registry['models']['qwen2'] = registry['models']['qwen'].copy()
    registry['services']['chat2'] = {'kind': 'local', 'model': 'qwen2','account_id':'shared-account'}
    registry['routes']['extract'] = 'chat2'
    second = extraction_config.resolve_config({'version': 'v3'})
    assert first['model_service'] != second['model_service']
    assert first['quota_group'] == second['quota_group'] == 'shared-account'
    assert runtime.get_pool(first['quota_group'], first) is runtime.get_pool(second['quota_group'], second)


@pytest.mark.parametrize('mutation', ['missing', 'disabled', 'wrong_adapter', 'worker'])
def test_bad_route_never_falls_back_to_legacy(registry, mutation):
    if mutation == 'missing': registry['routes']['extract'] = 'missing'
    if mutation == 'disabled': registry['models']['qwen']['enabled'] = False
    if mutation == 'wrong_adapter': registry['routes']['extract'] = 'clef'
    if mutation == 'worker': registry['models']['qwen']['manager'] = 'backend-worker'
    with pytest.raises(model_routes.ModelRouteError):
        extraction_config.resolve_config({'version': 'v3'})


def test_explicit_provider_profile_wins_default_route(registry, monkeypatch):
    registry['services']['chosen'] = {'kind':'api',
        'base_url': 'https://api.example/v1', 'model': 'explicit-model',
        'api_key_env': 'KG_EXTRACT_LLM_API_KEY'}
    monkeypatch.setenv('KG_EXTRACT_LLM_API_KEY', 'secret-api')
    result = extraction_config.resolve_config({'version': 'v3', 'provider': 'chosen'})
    assert result['model'] == 'explicit-model' and result['model_service']=='chosen'


def test_entity_model_explicit_selection_and_default_route(registry):
    registry['routes']['entity_count'] = 'nano'
    assert model_routes.resolve_entity_service()['model'] == 'nanojev'
    assert model_routes.resolve_entity_service('clef-flash', explicit=True)['model'] == 'clef-flash'
    registry['models']['clef-flash']['enabled'] = False
    with pytest.raises(model_routes.ModelRouteError, match='唯一'):
        model_routes.resolve_entity_service('clef-flash', explicit=True)


def test_explicit_entity_choice_does_not_require_a_default_route(registry):
    registry['routes'].pop('entity_count')
    assert model_routes.resolve_entity_service('nanojev', explicit=True)['service_id'] == 'nano'


def test_submission_replaces_injected_snapshot_and_retains_explicit_entity_choice(registry):
    registry['routes']['entity_count'] = 'nano'
    payload = {'ontology': 'x', 'options': {'chunking': {
        'adaptive_entity_count': True, 'entity_count_model': 'clef-flash'}},
        '__entity_count_service': {'api_key': 'injected'}}
    result = extraction_config.snapshot_payload('knowledge.ingestion', payload)
    assert result['__entity_count_service']['model'] == 'clef-flash'
    assert 'injected' not in json.dumps(result)
    registry['services']['clef']['model'] = 'nanojev'
    assert result['__entity_count_service']['model'] == 'clef-flash'


def test_local_decision_snapshot_does_not_read_legacy_credentials(registry, monkeypatch):
    from external_services.clef import ClefFlashClient
    monkeypatch.setenv('CLEF_SERVICE_PATH', 'invalid-legacy-path')
    monkeypatch.setenv('CLEF_SERVICE_API_KEY', 'legacy-secret')
    monkeypatch.setenv('CLEF_SERVICE_TIMEOUT_SECONDS', 'invalid')
    service = model_routes.resolve_service('entity_count')
    client = ClefFlashClient(service=service)
    assert client.model == 'clef-flash' and client.api_key == ''
    assert client.path == '/v1/systemone' and client.timeout == 120


def test_api_routes_reference_credentials_without_snapshotting_secret(registry, monkeypatch):
    registry['services']['api'] = {'kind': 'api', 'model': 'api-model',
        'base_url': 'https://api.example/v1', 'api_key_env': 'KG_EXTRACT_LLM_API_KEY'}
    registry['routes']['extract'] = 'api'
    monkeypatch.setenv('KG_EXTRACT_LLM_API_KEY', 'private-key')
    result = extraction_config.resolve_config({'version': 'v3'})
    assert result['credential_required'] is True
    assert 'private-key' not in json.dumps(result)
    monkeypatch.delenv('KG_EXTRACT_LLM_API_KEY')
    with pytest.raises(ValueError, match='credential'):
        extraction_config.resolve_config({'version': 'v3'})


def test_chunk_uses_one_service_snapshot_for_all_original_slices(registry, monkeypatch, tmp_path):
    from backend.app.services.algorithm_capabilities import DocumentChunkingService
    from backend.app.schemas import ChunkOptions
    from algorithm.knowledge_pipeline import entity_count_estimator
    from algorithm.knowledge_pipeline.chunking import chunk_document_items
    # Protocol fixtures reimport services with a stub splitter; this test must
    # exercise actual splitting while checking the request's fixed route.
    monkeypatch.setitem(DocumentChunkingService.chunk.__globals__,
                        'chunk_document_items', chunk_document_items)
    monkeypatch.setenv('CHUNK_ENTITY_LOG_PATH', str(tmp_path / 'decisions.jsonl'))
    calls = []
    def estimate(text, *, model, service):
        calls.append(service.copy())
        registry['routes']['entity_count'] = 'nano'
        return {'bucket': 'le20', 'model': service['model'],
                'service_id': service['service_id'], 'config_revision': service['revision']}
    monkeypatch.setattr(entity_count_estimator, 'estimate_entities', estimate)
    options = ChunkOptions(strategy='symbols', size=100, chunk_overlap=0, adaptive_entity_count=True)
    DocumentChunkingService().chunk('文' * 200, options)
    assert len(calls) == 2 and {c['model'] for c in calls} == {'clef-flash'}
    lines = [json.loads(line) for line in (tmp_path / 'decisions.jsonl').read_text(encoding='utf8').splitlines()]
    assert len(lines) == 2 and all(line['model'] == 'clef-flash' for line in lines)
    calls.clear()
    DocumentChunkingService().chunk('下一次请求', options)
    assert calls[0]['model'] == 'nanojev'


def test_document_chunk_submission_snapshot_is_fixed_before_queue(registry):
    payload = extraction_config.snapshot_payload('document.chunk', {
        'markdown': '正文', 'options': {'adaptive_entity_count': True}})
    assert payload['__entity_count_service']['model'] == 'clef-flash'
    registry['routes']['entity_count'] = 'nano'
    assert payload['__entity_count_service']['model'] == 'clef-flash'


def test_queued_chunk_execution_uses_submitted_route(registry, monkeypatch, tmp_path):
    from backend.app.execution.operations import _chunk_document
    from algorithm.knowledge_pipeline import entity_count_estimator
    monkeypatch.setenv('CHUNK_ENTITY_LOG_PATH', str(tmp_path / 'decisions.jsonl'))
    payload = extraction_config.snapshot_payload('document.chunk', {
        'markdown': '正文', 'options': {'adaptive_entity_count': True}})
    registry['routes']['entity_count'] = 'nano'
    calls = []
    def estimate(text, *, model, service):
        calls.append(service)
        return {'bucket': 'le20', 'model': service['model']}
    monkeypatch.setattr(entity_count_estimator, 'estimate_entities', estimate)
    assert _chunk_document(payload)['count'] == 1
    assert calls[0]['model'] == 'clef-flash'


def test_unmigrated_queued_request_keeps_absence_of_route(registry, monkeypatch):
    monkeypatch.setenv('CLEF_SERVICE_URL', 'http://legacy.example')
    monkeypatch.setenv('CLEF_SERVICE_API_KEY', 'private-legacy-key')
    registry['routes'] = {}
    payload = extraction_config.snapshot_payload('document.chunk', {
        'markdown': '正文', 'options': {'adaptive_entity_count': True}})
    assert payload['__entity_count_service']['kind'] == 'legacy'
    assert payload['__entity_count_service']['model'] == 'clef-flash'
    assert payload['__entity_count_service']['base_url'] == 'http://legacy.example'
    assert 'private-legacy-key' not in json.dumps(payload)
    registry['routes']['entity_count'] = 'nano'
    monkeypatch.setenv('CLEF_SERVICE_URL', 'http://later.example')
    from external_services.clef import ClefFlashClient
    client = ClefFlashClient(service=payload['__entity_count_service'])
    assert client.base_url == 'http://legacy.example' and client.model == 'clef-flash'


def test_default_native_clients_use_central_registration(registry, monkeypatch):
    from external_services.clef import ClefFlashClient
    from external_services.nanojev import NanoJevServiceClient
    monkeypatch.setenv('CLEF_SERVICE_URL', 'https://obsolete.invalid')
    monkeypatch.setenv('NANOJEV_SERVICE_URL', 'https://obsolete.invalid')
    assert ClefFlashClient().base_url == 'http://local-clef-flash:8000'
    assert NanoJevServiceClient().base_url == 'http://local-nanojev:8000'
    assert ClefFlashClient(base_url='http://explicit.example').base_url == 'http://explicit.example'


def test_duplicate_native_registration_requires_explicit_purpose(registry):
    registry['services']['clef-second'] = registry['services']['clef'].copy()
    from external_services.clef import ClefFlashClient
    with pytest.raises(model_routes.ModelRouteError, match='唯一'):
        ClefFlashClient()
    assert ClefFlashClient(purpose='entity_count').model == 'clef-flash'


def test_v22_gate_uses_registered_clef_without_legacy_url(registry, monkeypatch):
    from algorithm.knowledge_extract.v2_2.clef_gate import ClefGate
    monkeypatch.setenv('KG_EXTRACT_V22_CLEF_ENABLED', '1')
    monkeypatch.delenv('CLEF_SERVICE_URL', raising=False)
    gate = ClefGate.from_env()
    assert gate.enabled and gate.client.base_url == 'http://local-clef-flash:8000'


@pytest.mark.parametrize('endpoint', ['chunk', 'ingestion'])
@pytest.mark.parametrize('explicit', [False, True])
def test_http_boundary_preserves_whether_entity_model_was_explicit(
    registry, monkeypatch, protocol_module, endpoint, explicit,
):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    import importlib
    registry['routes']['entity_count'] = 'nano'
    captured = []
    module = importlib.import_module('backend.app.routers.' + (
        'algorithm_capabilities' if endpoint == 'chunk' else 'knowledge_precipitation'))
    async def dispatch(**kwargs):
        captured.append(extraction_config.snapshot_payload(kwargs['operation'], kwargs['payload']))
        return SimpleNamespace(data={}, execution=None, status_code=200)
    monkeypatch.setattr(module, 'dispatch_operation', dispatch)
    options = {'adaptive_entity_count': True}
    if explicit:
        options['entity_count_model'] = 'clef-flash'
    client = TestClient(protocol_module.app)
    if endpoint == 'chunk':
        response = client.post('/api/v1/documents/chunk', json={'markdown': '正文', 'options': options})
    else:
        response = client.post('/api/v1/pipelines/knowledge-ingestion',
            files={'file': ('sample.txt', b'sample', 'text/plain')},
            data={'options': json.dumps({'chunking': options})})
    assert response.status_code == 200
    assert captured[0]['__entity_count_service']['model'] == ('clef-flash' if explicit else 'nanojev')
