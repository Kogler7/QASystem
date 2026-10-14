"""Resolve approved model-service routes into non-secret request snapshots."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from qasystem_config import configuration


class ModelRouteError(ValueError):
    """Safe routing failure; never echo a URL, credential or registry contents."""


def registry_snapshot() -> tuple[dict, Any]:
    """One approved read for every consumer participating in a request."""
    return configuration.read_registry(with_revision=True)


def resolve_service(
    purpose: str, *, registry: dict | None = None, revision: Any = None,
    service_id: str | None = None,
) -> dict | None:
    if registry is None:
        registry, revision = registry_snapshot()
    services = registry.get('services', {})
    selected = service_id if service_id is not None else registry.get('routes', {}).get(purpose)
    if not selected:
        return None
    service = services.get(selected)
    if not isinstance(service, dict):
        raise ModelRouteError('模型服务路由指向未登记服务')
    if service.get('enabled', True) is not True:
        raise ModelRouteError('模型服务路由指向已关闭服务')
    kind = service.get('kind')
    if kind == 'local':
        model_id = service.get('model')
        record = registry.get('models', {}).get(model_id)
        if not isinstance(record, dict) or not record.get('enabled'):
            raise ModelRouteError('模型服务路由指向未启用模型')
        if record.get('manager', 'ops') != 'ops':
            raise ModelRouteError('模型服务路由不能启动或调用 Backend worker')
        from qasystem_config.model_registry import local_model_endpoint
        endpoint = local_model_endpoint(model_id, record)
        base_url, adapter, model = endpoint['base_url'], endpoint['adapter'], endpoint['model']
    elif kind == 'api':
        base_url, model = service.get('base_url'), service.get('model')
        adapter = service.get('adapter', 'chat')
    else:
        raise ModelRouteError('此路由不支持 worker 或未知服务类型')
    if purpose in {'extract', 'graph', 'agent'} and adapter != 'chat':
        raise ModelRouteError('抽取路由必须指向聊天推理服务')
    if purpose == 'entity_count' and adapter not in {'clef', 'nanojev'}:
        raise ModelRouteError('实体数量路由必须指向 Clef 或 NanoJev')
    capabilities = deepcopy(service.get('capabilities', {}))
    if kind == 'local' and adapter == 'chat':
        from qasystem_config.runtime_config import MODEL_DEFAULTS
        actual_context = int(record.get('context', MODEL_DEFAULTS['context']))
        capabilities['context_tokens'] = min(capabilities.get('context_tokens', actual_context), actual_context)
    snapshot = {
        'service_id': selected, 'kind': kind, 'adapter': adapter,
        'base_url': base_url, 'model': model,
        'api_key_env': service.get('api_key_env', ''),
        'revision': revision,
        'capabilities': capabilities,
        'limits': service.get('limits', {}),
        'account_id': service.get('account_id', ''),
        'connection': service.get('connection', {}),
    }
    if snapshot['account_id']:
        account = registry.get('accounts', {}).get(snapshot['account_id'])
        if not isinstance(account, dict):
            raise ModelRouteError('服务引用了未登记账号额度')
        snapshot['account_limits'] = account
    return deepcopy(snapshot)


def resolve_entity_service(
    model: str = 'clef-flash', *, explicit: bool = False,
    registry: dict | None = None, revision: Any = None,
) -> dict | None:
    if registry is None:
        registry, revision = registry_snapshot()
    routed = resolve_service('entity_count', registry=registry, revision=revision)
    if not explicit or (routed and routed['model'] == model):
        return routed
    if not routed and not registry.get('services'):
        return None
    candidates = []
    for service_id in registry.get('services', {}):
        service = registry['services'][service_id]
        if service.get('kind') == 'local':
            record = registry.get('models', {}).get(service.get('model'), {})
            adapter = record.get('adapter')
            enabled = record.get('enabled')
        else:
            adapter, enabled = service.get('adapter'), service.get('kind') == 'api'
        if adapter not in {'clef', 'nanojev'} or not enabled:
            continue
        resolved = resolve_service('entity_count', registry=registry, revision=revision, service_id=service_id)
        if resolved['model'] == model:
            candidates.append(resolved)
    if len(candidates) != 1:
        raise ModelRouteError('显式实体数量模型没有唯一的已启用服务登记')
    return candidates[0]


def resolve_adapter_service(adapter: str, *, purpose: str | None = None) -> dict | None:
    """Default native clients use registered services, never a guessed endpoint."""
    registry, revision = registry_snapshot()
    if not registry.get('services') and not registry.get('routes'):
        return None
    if purpose and registry.get('routes', {}).get(purpose):
        service = resolve_service(purpose, registry=registry, revision=revision)
        if service['adapter'] != adapter:
            raise ModelRouteError('用途路由与客户端推理协议不兼容')
        return service
    candidates = []
    for service_id, service in registry.get('services', {}).items():
        if service.get('kind') == 'local':
            record = registry.get('models', {}).get(service.get('model'), {})
            matches = record.get('enabled') and record.get('adapter') == adapter
        else:
            matches = service.get('kind') == 'api' and service.get('adapter') == adapter
        if matches:
            candidates.append(resolve_service('native', registry=registry,
                                              revision=revision, service_id=service_id))
    if len(candidates) != 1:
        raise ModelRouteError('原生模型服务需要唯一已启用登记或明确用途路由')
    return candidates[0]


def snapshot_entity_service(model: str = 'clef-flash', **kwargs) -> dict:
    """Freeze legacy endpoints too, so queued requests never select a later route."""
    service = resolve_entity_service(model, **kwargs)
    if service is not None:
        return service
    from .clef import ClefFlashClient
    prefix = 'NANOJEV' if model == 'nanojev' else 'CLEF'
    client = ClefFlashClient(model=model, env_prefix=prefix, resolve_registered=False,
        capability='models.nanojev.create' if prefix == 'NANOJEV' else 'models.decisions.create')
    # Deliberately retain only the credential reference, never client.api_key.
    return {'service_id': 'legacy:' + prefix.lower(), 'kind': 'legacy',
        'adapter': 'nanojev' if prefix == 'NANOJEV' else 'clef',
        'base_url': client.base_url, 'model': client.model,
        'path': client.path, 'timeout': client.timeout,
        'api_key_env': prefix + '_SERVICE_API_KEY', 'revision': None}
