"""Small JSON-mode transport. Credentials never enter request traces.

The extraction runtime is SDK independent; callers can inject any messages
callable. The benchmark uses this same adapter for both V2 and V3.
"""
from __future__ import annotations
from qasystem_config.configuration import getenv as config_getenv, required as config_required

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

try:
    from ..v2.llm_client import StructuredLLMResponse
except ImportError:  # PYTHONPATH=algorithm, matching the existing standalone tools
    from algorithm.knowledge_extract.v2.llm_client import StructuredLLMResponse


class ProviderError(RuntimeError):
    """Sanitized transport failure; no provider response or headers attached."""

    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str = 'https://api.siliconflow.cn/v1'
    model: str = 'deepseek-ai/DeepSeek-V4-Flash'
    api_key: str = field(default='', repr=False)
    max_tokens: int = 16384
    timeout: float = 300
    thinking: bool = False
    temperature: float = 0

    def __post_init__(self):
        endpoint = urlparse(self.base_url)
        if endpoint.scheme != 'https' and not (endpoint.scheme == 'http' and endpoint.hostname in {'localhost','127.0.0.1','::1'}):
            raise ValueError('API endpoint must use HTTPS (except loopback development)')
        if not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError('API endpoint must not contain credentials, query or fragment')
        if self.max_tokens < 1 or self.timeout <= 0:
            raise ValueError('max_tokens and timeout must be positive')

    @classmethod
    def from_file(cls, path: str | Path = 'eval_api.md') -> 'ProviderConfig':
        path = Path(path)
        values = {}
        if path.exists():
            text = path.read_text(encoding='utf-8')
            for name, pattern in {
                'base_url': r'base_url\s*[:=]\s*[\"\x27]?([^\s\"\x27`]+)',
                'model': r'\bmodel\s*[:=]\s*[\"\x27]?([^\s\"\x27`]+)',
                'api_key': r'(?:APIKEY|api_key)\s*[:=]\s*[\"\x27]?([^\s\"\x27`]+)',
            }.items():
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match:
                    values[name] = match.group(1).rstrip(',')
        if not values:
            from external_services.llm.service import resolve_chat_service
            service, credential = resolve_chat_service('extract', required=True)
            values.update(base_url=service['base_url'], model=service['model'], api_key=credential)
        config = cls(**values)
        if not config.api_key:
            raise ValueError('JEV 抽取服务凭据未配置；请在 ops 登记服务引用')
        return config

    def safe_dict(self) -> dict:
        result = asdict(self)
        result.pop('api_key')
        result.update(response_format={'type':'json_object'}, stream=False)
        return result


class JSONClient:
    def __init__(self, config: ProviderConfig, *, trace_dir: str | Path | None = None):
        self.config = config
        self.model = config.model
        self.records: list[dict] = []
        self._lock = threading.Lock()
        self.trace_dir = Path(trace_dir) if trace_dir is not None else None
        self._sequence = 0

    @property
    def options(self) -> dict:
        return self.config.safe_dict()

    def request_body(self, messages: list[dict]) -> dict:
        return {
            'model': self.config.model, 'messages': messages,
            'max_tokens': self.config.max_tokens,
            'temperature': self.config.temperature,
            'response_format': {'type':'json_object'}, 'stream': False,
            'enable_thinking': self.config.thinking,
        }

    @staticmethod
    def decode_response(payload: dict) -> StructuredLLMResponse:
        if not isinstance(payload, dict):
            raise ProviderError('PROVIDER_INVALID_RESPONSE')
        choices = payload.get('choices') or []
        if not choices:
            raise ProviderError('PROVIDER_NO_CHOICES')
        choice = choices[0]
        if not isinstance(choice, dict) or not isinstance(choice.get('message'), dict):
            raise ProviderError('PROVIDER_INVALID_RESPONSE')
        content = (choice.get('message') or {}).get('content') or ''
        if not isinstance(content, str):
            raise ProviderError('PROVIDER_INVALID_RESPONSE')
        finish = choice.get('finish_reason')
        status = 'incomplete' if finish == 'length' else ('completed' if content else 'empty')
        return StructuredLLMResponse(text=content, api='chat_completions', response_mode='json_object', status=status, finish_reason=finish, usage=payload.get('usage'))

    def __call__(self, messages: list[dict]) -> StructuredLLMResponse:
        started = time.monotonic()
        record = {'model':self.model, 'response_mode':'json_object'}
        trace = {'messages':messages,'status':'started'}
        trace_path = None
        if self.trace_dir is not None:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self._sequence += 1
                trace_path = self.trace_dir / f'{self._sequence:04d}.json'
            self._write_trace(trace_path, trace)
        try:
            request = urllib.request.Request(
                self.config.base_url.rstrip('/') + '/chat/completions',
                data=json.dumps(self.request_body(messages), ensure_ascii=False).encode(),
                headers={'Authorization':'Bearer ' + self.config.api_key, 'Content-Type':'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                result = self.decode_response(json.load(response))
            record.update(status=result.status, finish_reason=result.finish_reason, usage=result.usage)
            trace.update(response=result.text,status=result.status,finish_reason=result.finish_reason,usage=result.usage)
            return result
        except urllib.error.HTTPError as error:
            code = f'HTTP_{error.code}'
            record['error_code'] = code
            raise ProviderError(code, retryable=error.code in {408,409,429} or error.code >= 500) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            record['error_code'] = 'TRANSPORT_ERROR'
            raise ProviderError('TRANSPORT_ERROR', retryable=True) from None
        except (ValueError, KeyError, TypeError):
            record['error_code'] = 'PROVIDER_INVALID_RESPONSE'
            raise ProviderError('PROVIDER_INVALID_RESPONSE') from None
        except ProviderError as error:
            record['error_code'] = error.code
            raise
        finally:
            if 'error_code' in record:
                record['status'] = 'failed'
            record['elapsed_seconds'] = round(time.monotonic()-started, 3)
            with self._lock:
                self.records.append(record)
            if trace_path:
                self._write_trace(trace_path, {**trace, **record})

    def _write_trace(self, path: Path, trace: dict):
        serialized = json.dumps(trace,ensure_ascii=False,indent=2)
        if self.config.api_key:
            serialized = serialized.replace(self.config.api_key,'[REDACTED]')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(serialized,encoding='utf-8')
        temporary.replace(path)

    def generate_structured(self, schema: dict, messages: list[dict], request_options: dict | None = None):
        # JSON syntax is provider-enforced; V2/V3 retain their local validation.
        return self(messages)
