"""Local token assets, explicit fallback and byte-vs-token admission contracts."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from algorithm.token_budget import InputTokenLimitError, check_input_bytes
from external_services.llm import tokenization
from external_services.llm.runtime import ScheduledClient


@pytest.fixture
def assets(tmp_path, monkeypatch):
    tokenizer = SimpleNamespace(encode=lambda text, **kwargs: SimpleNamespace(ids=list(text)))
    monkeypatch.setitem(sys.modules, 'tokenizers', SimpleNamespace(
        Tokenizer=SimpleNamespace(from_file=lambda path: tokenizer)))
    encoder = '''import json
def encode_messages(messages, thinking_mode, reasoning_effort=None):
    return '<bos>' + json.dumps(messages, ensure_ascii=False) + thinking_mode + str(reasoning_effort)
'''
    (tmp_path / 'encoding').mkdir()
    files = {'tokenizer.json': '{}', 'encoding/encoding_dsv4.py': encoder}
    for name, content in files.items():
        (tmp_path / name).write_bytes(content.encode('utf-8'))
    manifest = {'model': 'deepseek-v4-flash-0731', 'revision': 'a' * 40,
                'files': {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}}
    (tmp_path / 'tokenizer_manifest.json').write_text(json.dumps(manifest))
    tokenization._load_assets.cache_clear()
    yield str(tmp_path), manifest['revision']
    tokenization._load_assets.cache_clear()


def test_versioned_encoder_receives_messages_schema_and_tools(assets):
    directory, revision = assets
    counter = tokenization.TokenCounter(directory, revision, 'deepseek-v4-flash-0731')
    payload = {'messages': [{'role': 'user', 'content': '设备'}]}
    base = counter.measure(payload)
    assert base.source == 'local_deepseek_v4'
    assert base.revision == revision and len(base.fingerprint) == 64
    payload['response_format'] = {'type': 'json_schema', 'json_schema': {'description': '属性' * 20}}
    with_schema = counter.measure(payload)
    payload['tools'] = [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}]
    with_tools = counter.measure(payload)
    assert base.tokens < with_schema.tokens < with_tools.tokens
    assert payload['messages'] == [{'role': 'user', 'content': '设备'}]


@pytest.mark.parametrize('field,value', [('revision', 'b' * 40), ('model', 'deepseek-v3'),
                                        ('directory', '/absent/qasystem/tokenizer')])
def test_wrong_assets_cannot_silently_masquerade_as_exact_tokens(assets, field, value):
    directory, revision = assets
    values = dict(directory=directory, revision=revision, model='deepseek-v4-flash-0731')
    values[field] = value
    measured = tokenization.TokenCounter(**values).measure({'messages': [{'role': 'user', 'content': '设备'}]})
    assert measured.source == 'utf8_fallback' and measured.fallback_reason
    assert measured.revision == ''


def test_tampered_encoder_is_rejected_before_execution(assets):
    directory, revision = assets
    Path(directory, 'encoding/encoding_dsv4.py').write_text('raise RuntimeError("must not execute")')
    measured = tokenization.TokenCounter(directory, revision, 'deepseek-v4-flash-0731').measure({'messages': []})
    assert measured.source == 'utf8_fallback'


def test_fallback_is_explicit_and_counts_schema_tools(caplog):
    counter = tokenization.TokenCounter()
    payload = {'messages': [{'role': 'user', 'content': '设备'}]}
    before = counter.measure(payload)
    payload['tools'] = [{'function': {'description': '属性' * 100}}]
    after = counter.measure(payload)
    assert after.tokens > before.tokens + 500
    assert after.source == 'utf8_fallback'
    assert after.fallback_reason == 'tokenizer_not_configured'
    assert 'conservative UTF-8 estimate' in caplog.text


def test_byte_guard_is_independent_from_token_admission(assets):
    directory, revision = assets
    payload = {'model': 'deepseek-v4-flash-0731', 'messages': [{'role': 'user', 'content': '设' * 200}]}
    counter = tokenization.TokenCounter(directory, revision, payload['model'])
    measured = counter.measure(payload)
    assert measured.tokens < measured.input_bytes
    client = ScheduledClient(SimpleNamespace(), dict(quota_group=uuid4().hex, max_concurrency=1,
        qps=0, qpm=0, request_timeout=10, disable_thinking=True, tokenizer_dir=directory,
        tokenizer_revision=revision, max_input_tokens=measured.tokens, max_input_bytes=10000))
    assert client._call(lambda **kwargs: 'ok', **payload) == 'ok'
    with pytest.raises(InputTokenLimitError, match='max_input_bytes'):
        check_input_bytes(payload, measured.input_bytes - 1)


def test_unsupported_multimodal_payload_falls_back_explicitly(assets):
    directory, revision = assets
    counter = tokenization.TokenCounter(directory, revision, 'deepseek-v4-flash-0731')
    measured = counter.measure({'messages': [{'role': 'user', 'content': [{'type': 'image_url'}]}]})
    assert measured.source == 'utf8_fallback'


def test_provider_usage_calibrates_next_admission_and_records_version(assets, tmp_path):
    from algorithm.extraction_trace import ExtractionTrace
    directory, revision = assets
    payload = {'model': 'deepseek-v4-flash-0731', 'messages': [{'role': 'user', 'content': '设备'}]}
    client = ScheduledClient(SimpleNamespace(), dict(quota_group=uuid4().hex, max_concurrency=1,
        qps=0, qpm=0, request_timeout=10, disable_thinking=True, tokenizer_dir=directory,
        tokenizer_revision=revision, quota_tiers=[]))
    client.extraction_trace = ExtractionTrace(tmp_path / 'traces')
    measured = client.measure_input(payload).tokens
    for _ in range(2):
        client._call(lambda **kwargs: {'usage': {'prompt_tokens': measured + 10, 'completion_tokens': 2}},
                     **payload)
    records = sorted(client.extraction_trace.directory.glob('*-http.json'))
    first, second = (json.loads(record.read_text()) for record in records)
    assert first['estimated_input_tokens'] == measured
    assert second['estimated_input_tokens'] == measured + 10
    assert second['token_measurement']['revision'] == revision
    assert second['token_measurement']['source'] == 'local_deepseek_v4'
    assert second['usage_known'] is True
    assert second['accounted_input_tokens'] == measured + 10
