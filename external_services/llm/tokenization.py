"""Offline, versioned DeepSeek V4 counting; provider usage remains authoritative.

Assets must be provisioned separately as a read-only local directory. Nothing
here calls from_pretrained or downloads code. The manifest pins both tokenizer
and the official Python message encoder by SHA256 to one model revision.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import re

from algorithm.token_budget import estimate_input_tokens, input_bytes

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenMeasurement:
    tokens: int
    source: str
    input_bytes: int
    model: str
    revision: str = ''
    fingerprint: str = ''
    fallback_reason: str | None = None

    def metadata(self):
        return asdict(self)


def _model_name(value):
    return str(value).rsplit('/', 1)[-1].lower()


@lru_cache(maxsize=8)
def _load_assets(directory: str, revision: str, model: str):
    root = Path(directory)
    manifest = json.loads((root / 'tokenizer_manifest.json').read_text())
    if not re.fullmatch(r'[a-f0-9]{40}', revision) or manifest['revision'] != revision:
        raise ValueError('revision_mismatch')
    if _model_name(manifest['model']) != _model_name(model):
        raise ValueError('model_mismatch')
    if not _model_name(model).startswith('deepseek-v4'):
        raise ValueError('unsupported_model')
    digests = {}
    for name in ('tokenizer.json', 'encoding/encoding_dsv4.py'):
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        if manifest['files'][name] != digest:
            raise ValueError('asset_hash_mismatch')
        digests[name] = digest
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file(str(root / 'tokenizer.json'))
    # Load only a hash-checked, explicitly configured local encoder. Its
    # revision is recorded with every measurement, never discovered online.
    spec = importlib.util.spec_from_file_location('_qasystem_deepseek_encoder', root / 'encoding/encoding_dsv4.py')
    encoder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(encoder)
    fingerprint = hashlib.sha256(json.dumps(digests, sort_keys=True).encode()).hexdigest()
    return tokenizer, encoder, fingerprint


def _chat_messages(payload: dict):
    extra = payload.get('extra_body') or {}
    if any(key in extra for key in ('messages', 'input', 'instructions', 'response_format', 'tools')):
        raise ValueError('unsupported_extra_input')
    if not isinstance(payload.get('messages'), list) or not payload['messages']:
        raise ValueError('unsupported_input_format')
    messages = deepcopy(payload['messages'])
    for msg in messages:
        if msg.get('content') is not None and not isinstance(msg['content'], str):
            raise ValueError('unsupported_content_format')
    # Official encoder consumes tools and response_format on system/developer
    # messages. API wrappers provide them at the request root.
    additions = {key: deepcopy(payload[key]) for key in ('tools', 'response_format') if payload.get(key)}
    if additions:
        if messages[0].get('role') != 'system':
            messages.insert(0, {'role': 'system', 'content': ''})
        messages[0].update(additions)
    return messages


class TokenCounter:
    def __init__(self, directory='', revision='', model=''):
        self.directory, self.revision, self.model = str(directory), str(revision), str(model)
        self.warned = set()

    def measure(self, payload: dict) -> TokenMeasurement:
        model = str(payload.get('model') or self.model)
        reason = 'tokenizer_not_configured'
        if self.directory:
            try:
                tokenizer, encoder, fingerprint = _load_assets(self.directory, self.revision, model)
                messages = _chat_messages(payload)
                extra = payload.get('extra_body') or {}
                thinking = extra.get('thinking', payload.get('thinking', {}))
                enabled = (thinking.get('type') == 'enabled' if isinstance(thinking, dict) else False)
                enabled = bool(extra.get('enable_thinking', enabled))
                prompt = encoder.encode_messages(messages, thinking_mode='thinking' if enabled else 'chat',
                    reasoning_effort=payload.get('reasoning_effort') or extra.get('reasoning_effort'))
                tokens = len(tokenizer.encode(prompt, add_special_tokens=False).ids)
                return TokenMeasurement(tokens, 'local_deepseek_v4', input_bytes(payload), model,
                                        self.revision, fingerprint)
            except (OSError, ImportError):
                reason = 'tokenizer_assets_or_dependency_unavailable'
            except (ValueError, KeyError, TypeError, AttributeError, AssertionError, NotImplementedError):
                # Never leak paths, prompts, schema or locally supplied source.
                reason = 'tokenizer_assets_or_payload_incompatible'
        if reason not in self.warned:
            logger.warning('LLM token accounting fallback: %s; conservative UTF-8 estimate', reason)
            self.warned.add(reason)
        return TokenMeasurement(estimate_input_tokens(payload), 'utf8_fallback', input_bytes(payload),
                                model, fallback_reason=reason)
