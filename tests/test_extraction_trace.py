import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from algorithm.extraction_trace import ExtractionTrace, traced_generation
from algorithm.knowledge_pipeline.chunking import chunk_document_items, extraction_segment_ranges


def test_trace_parallel_calls_preserve_inputs_responses_and_redact(tmp_path):
    trace = ExtractionTrace(tmp_path, secrets=('private-provider-key',))
    llm = SimpleNamespace(extraction_trace=trace)

    @traced_generation
    def call(llm, schema, messages, **context):
        return SimpleNamespace(text='{"value":"private-provider-key"}', finish_reason='length', usage={'completion_tokens': 10})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: call(llm, {}, [{'content': f'input {i}', 'Authorization': 'private-auth'}],
                                    pass_name='entity', chunk_id=str(i), attempt_number=1), range(8)))
    inputs = list(trace.directory.glob('*-call.json'))
    outputs = list(trace.directory.glob('*-response.json'))
    assert len(inputs) == len(outputs) == 8
    assert {json.loads(p.read_text())['context']['chunk_id'] for p in inputs} == {str(i) for i in range(8)}
    text = ''.join(p.read_text() for p in trace.directory.glob('*.json'))
    assert 'private-provider-key' not in text and 'private-auth' not in text
    assert all(json.loads(p.read_text())['finish_reason'] == 'length' for p in outputs)


def test_trace_failure_keeps_pending_request_without_serializing_exception_secrets(tmp_path):
    llm = SimpleNamespace(extraction_trace=ExtractionTrace(tmp_path))

    @traced_generation
    def call(*args, **kwargs):
        raise TimeoutError('Authorization: very-private')

    with pytest.raises(TimeoutError):
        call(llm, {}, [{'content': '原文'}], pass_name='property')
    files = list(llm.extraction_trace.directory.glob('*.json'))
    assert len(files) == 3
    assert 'very-private' not in ''.join(p.read_text(encoding='utf-8') for p in files)
    assert json.loads(next(llm.extraction_trace.directory.glob('*-response.json')).read_text())['status'] == 'failed'


def test_trace_disk_failure_does_not_fail_generation(tmp_path):
    root = tmp_path / 'file'
    root.write_text('not a directory')
    llm = SimpleNamespace(extraction_trace=ExtractionTrace(root))

    @traced_generation
    def call(*args, **kwargs):
        return 'ok'

    assert call(llm, {}, []) == 'ok'


@pytest.mark.parametrize('text', ['重复段。\n' * 120, '|列1|列2|\n' + '|甲|乙|\n' * 90,
                                json.dumps({'rows': [{'name': '甲' * 60}] * 20}, ensure_ascii=False),
                                '甲' * 1000, '# 标题\n\n' + '一句话。\n\n' * 80], ids=['repeat', 'table', 'json', 'long-line', 'markdown'])
def test_recovery_segments_have_exact_ranges_cover_all_text_and_shrink(text):
    ranges = extraction_segment_ranges(text, 100, 20)
    assert len(ranges) > 1
    coverage = set()
    for start, end in ranges:
        assert 0 <= start < end <= len(text)
        assert end - start <= 100
        coverage.update(range(start, end))
    assert all(i in coverage for i, ch in enumerate(text) if not ch.isspace())


def test_upstream_range_metadata_disambiguates_repeated_overlapping_text():
    text = '相同内容。\n' * 100
    items = chunk_document_items(text, chunk_type=1, chunk_size=80, chunk_overlap=20)
    starts = [item['range'][0] for item in items]
    assert starts == sorted(set(starts))
    assert items[-1]['range'][1] == len(text)
    assert all(text[s:e] == item['text'] for item in items for s, e in [item['range']])
