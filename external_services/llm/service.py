"""The shared entry point used by graph, Agent and extraction transports."""
from __future__ import annotations

from contextlib import contextmanager
import time

from algorithm.token_budget import check_input_budget
from docker import configuration
from external_services.model_routes import resolve_service


def resolve_chat_service(purpose):
    service = resolve_service(purpose)
    if service is None:
        return None
    credential = configuration.getenv(service['api_key_env'], '') if service.get('api_key_env') else ''
    if service['kind'] == 'api' and not credential:
        raise ValueError('LLM service credential is not configured')
    return service, credential


def check_service_budget(payload, service):
    if not service:
        return
    capabilities = service.get('capabilities') or {}
    output = payload.get('max_output_tokens', payload.get('max_completion_tokens', payload.get('max_tokens', 0))) or 0
    if capabilities.get('max_output_tokens') and output > capabilities['max_output_tokens']:
        raise ValueError('Algorithm output request exceeds registered service capability')
    if capabilities.get('context_tokens'):
        remaining = capabilities['context_tokens'] - output
        if remaining < 1:
            raise ValueError('Output budget leaves no service input capacity')
        check_input_budget({'input': payload['state']} if 'state' in payload else payload, remaining)


@contextmanager
def model_call(service, payload, *, timeout, cancelled=None):
    check_service_budget(payload, service)
    deadline = time.monotonic()+timeout
    if service:
        from .distributed import service_pool
        with service_pool(service).request_slot(deadline, cancelled=cancelled):
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Model request budget exhausted')
            yield remaining
    else:
        yield timeout
