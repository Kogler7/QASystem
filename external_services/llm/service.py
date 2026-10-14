"""The shared entry point used by graph, Agent and extraction transports."""
from __future__ import annotations

from contextlib import contextmanager
import time

from algorithm.token_budget import check_input_budget
from qasystem_config import configuration
from external_services.model_routes import resolve_service


def resolve_chat_service(purpose, *, required=False):
    service = resolve_service(purpose)
    if service is None:
        if required:
            raise ValueError('功能未选择已登记的模型服务；请使用 ops 配置用途路由')
        return None
    credential = configuration.getenv(service['api_key_env'], '') if service.get('api_key_env') else ''
    if service['kind'] == 'api' and not credential:
        raise ValueError('LLM service credential is not configured')
    return service, credential


def check_service_budget(payload, service, *, measured_tokens=None):
    if not service:
        return
    capabilities = service.get('capabilities') or {}
    if capabilities.get('max_input_bytes'):
        from algorithm.token_budget import check_input_bytes
        check_input_bytes({'input': payload['state']} if 'state' in payload else payload, capabilities['max_input_bytes'])
    output = payload.get('max_output_tokens', payload.get('max_completion_tokens', payload.get('max_tokens', 0))) or 0
    if capabilities.get('max_output_tokens') and output > capabilities['max_output_tokens']:
        raise ValueError('Algorithm output request exceeds registered service capability')
    if capabilities.get('context_tokens'):
        remaining = capabilities['context_tokens'] - output
        if remaining < 1:
            raise ValueError('Output budget leaves no service input capacity')
        if measured_tokens is None:
            check_input_budget({'input': payload['state']} if 'state' in payload else payload, remaining)
        elif measured_tokens > remaining:
            raise ValueError('Input and output exceed registered service context capacity')


@contextmanager
def model_call(service, payload, *, timeout, cancelled=None):
    connection = (service or {}).get('connection') or {}
    timeout = min(timeout, connection.get('timeout_seconds', timeout))
    deadline = time.monotonic()+timeout
    if service:
        from .distributed import service_pool
        from .tokenization import TokenCounter
        caps = service.get('capabilities') or {}
        measured = TokenCounter(caps.get('tokenizer_dir', ''), caps.get('tokenizer_revision', ''), service.get('model', '')).measure(
            {'input': payload['state']} if 'state' in payload else payload)
        check_service_budget(payload, service, measured_tokens=measured.tokens)
        output = int(payload.get('max_output_tokens') or payload.get('max_completion_tokens') or payload.get('max_tokens') or 0)
        if not output:
            output = int(caps.get('max_output_tokens') or 0)
            if output and service.get('adapter', 'chat') == 'chat':
                payload['max_output_tokens' if 'input' in payload and 'messages' not in payload else 'max_tokens'] = output
            token_limited = any((limits or {}).get('tpm') or (limits or {}).get('quota_tiers')
                                for limits in (service.get('limits'), service.get('account_limits')))
            if token_limited and not output and service.get('adapter', 'chat') == 'chat':
                raise ValueError('Token quota requires a bounded output budget')
        check_service_budget(payload, service, measured_tokens=measured.tokens)
        pool = service_pool(service)
        with pool.request_slot(deadline, cancelled=cancelled,
                input_tokens=measured.tokens, max_output_tokens=output, measurement=measured.metadata()):
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Model request budget exhausted')
            try: yield remaining
            except Exception as exc:
                pool.report_error(exc)
                raise
    else:
        yield timeout
