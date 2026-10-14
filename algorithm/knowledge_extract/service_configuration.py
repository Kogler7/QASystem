"""Resolve transport registration separately from each algorithm's policy."""
from __future__ import annotations


def fill_service(policy):
    from external_services.llm.service import resolve_chat_service
    result = dict(policy)
    result.update(base_url='', model='', api_key='')
    routed = resolve_chat_service('extract')
    if routed:
        service, credential = routed
        result.update(base_url=service['base_url'], model=service['model'], api_key=credential,
                      service_snapshot=service)
        result.update(service.get('limits') or {})
        caps = service.get('capabilities') or {}
        for field in ('api_mode','thinking_parameter','response_format'):
            if field in caps:
                result[field] = caps[field]
        if caps.get('max_output_tokens'):
            result['max_tokens'] = min(result['max_tokens'], caps['max_output_tokens'])
    return result


def standalone_sdk(client, config):
    """Standalone algorithm calls use the same admission as queued Backend calls."""
    from external_services.llm.runtime import ScheduledClient
    if not config.get('service_snapshot'):
        raise ValueError('知识抽取未选择已登记的模型服务；请使用 ops 配置抽取用途')
    values = dict(config, quota_group=config['service_snapshot']['service_id'])
    return ScheduledClient(client, values)
