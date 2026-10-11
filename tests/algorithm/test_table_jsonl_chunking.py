import importlib
import json
from types import SimpleNamespace

import pytest

from algorithm.preproc.preproc_src.table_json import render_json_block
from algorithm.knowledge_pipeline.chunking import chunk_document_items
from algorithm.knowledge_pipeline.entity_count_chunking import refine_chunks
from algorithm.table_jsonl import decode_records, row_boundaries


def table(n=45):
    return render_json_block({'columns': ['设备', '故障'], 'rows': [
        {'设备': f'设备{i}', '故障': '通信中断'} for i in range(n)]})


def records(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith('{')]


@pytest.mark.parametrize('strategy', [0, 1, 2, 3, 4])
def test_public_chunking_splits_jsonl_on_rows_with_exact_source(strategy):
    text = '# 工作表一\n\n' + table() + '\n\n后续正文'
    items = chunk_document_items(text, chunk_type=strategy, chunk_size=300, chunk_overlap=0)
    found = []
    for item in items:
        a, b = item['range']
        assert item['text'] == text[a:b]
        if '{' in item['text']:
            assert row_boundaries(item['text']) is not None
            found.extend(records(item['text']))
    assert found == records(text)
    assert len(items) > 2
    assert sum(item['text'].count('```jsonl') for item in items) == 1
    assert sum(item['text'].count('```') for item in items) == 2


def test_complete_row_overlap_and_oversize_single_row():
    text = table(45)
    items = chunk_document_items(text, chunk_size=300, chunk_overlap=60)
    seen = set()
    for left, right in zip(items, items[1:]):
        overlap = left['range'][1] - right['range'][0]
        assert 0 <= overlap <= 60
        assert row_boundaries(right['text']) is not None
        seen.update(row['设备'] for row in records(left['text']))
    seen.update(row['设备'] for row in records(items[-1]['text']))
    assert len(seen) == 45
    huge = render_json_block({'columns': ['描述'], 'rows': [{'描述': '长' * 1000}]})
    result = chunk_document_items(huge, chunk_size=100)
    assert len(result) == 1 and result[0]['text'] == huge and result[0]['range'] == [0, len(huge)]


@pytest.mark.parametrize('version', ['v2', 'v2_2', 'v3', 'v3_1', 'latest'])
def test_table_router_understands_full_and_partial_jsonl(version):
    module = importlib.import_module(f'algorithm.knowledge_extract.{version}.structured_text')
    text = table(3)
    for candidate in (text, '\n'.join(text.splitlines()[1:-1]),
                      '# 工作表\n' + text, '\n'.join(text.splitlines()[1:])):
        payload = module.extract_json_payload(candidate)
        assert payload is not None
        assert list(payload.blocks) == records(text)
    # Historical ordinary JSON remains accepted.
    assert module.extract_json_payload('```json\n{"columns":["a"],"rows":[{"a":"1"}]}\n```')


@pytest.mark.parametrize('version', ['v3', 'latest'])
def test_jsonl_rule_evidence_uses_same_block_numbers_as_router(version):
    evidence = importlib.import_module(f'algorithm.knowledge_extract.{version}.table_evidence')
    router = importlib.import_module(f'algorithm.knowledge_extract.{version}.structured_text')
    text = '```jsonl\n{"设备":"正常设备","处理":"巡检"}\n{"设备":"故障设备","条件":"压力过高","处理":"停机"}\n```'
    payload = router.extract_json_payload(text)
    assert len(payload.blocks) == 2
    roots = evidence._source_roots(text)
    assert [json.loads(text[root.start:root.end]) for root in roots] == list(payload.blocks)
    decision = SimpleNamespace(rule_types=('Rule',), rule_rows=((2, 1),), reasons=())
    def entity(quote):
        a = text.index(quote)
        return SimpleNamespace(canonical_type='Rule', proposed_type='Rule',
                               evidence=[SimpleNamespace(start_char=a, end_char=a+len(quote))])
    assert evidence.rule_candidate_in_scope(SimpleNamespace(text=text), decision, entity('停机'))
    assert not evidence.rule_candidate_in_scope(SimpleNamespace(text=text), decision, entity('巡检'))


def test_adaptive_jsonl_refines_once_on_rows_and_logs_actual_overlap(tmp_path, monkeypatch):
    path = tmp_path / 'decisions.jsonl'
    monkeypatch.setenv('CHUNK_ENTITY_LOG_PATH', str(path))
    text = table(45)
    calls = []
    def estimator(value):
        calls.append(value)
        return {'bucket': 'gt40'}
    result = refine_chunks(text, [{'text': text, 'range': [0, len(text)]}], estimator, overlap=50)
    assert calls == [text]
    assert len(result) == 3
    assert all(row_boundaries(item['text']) is not None for item in result)
    assert {row['设备'] for item in result for row in records(item['text'])} == {f'设备{i}' for i in range(45)}
    entry = json.loads(path.read_text(encoding='utf8'))
    assert entry['status'] == 'refined_jsonl'
    assert entry['ranges'] == [item['range'] for item in result]
    assert entry['effective_overlaps'] == [left['range'][1]-right['range'][0]
                                          for left, right in zip(result, result[1:])]


def test_invalid_jsonl_and_other_fences_remain_protected():
    assert decode_records('{"a":1}\ninvalid') is None
    assert row_boundaries('```jsonl\n{"a":1}\n{"b":\n```') is None
    bad = '```jsonl\n{"a":1}\n{"b":\n```'
    assert chunk_document_items(bad, chunk_size=10) == [{'text': bad, 'range': [0, len(bad)]}]


def test_adaptive_jsonl_metadata_tracks_child_data_rows(tmp_path, monkeypatch):
    monkeypatch.setenv('CHUNK_ENTITY_LOG_PATH', str(tmp_path / 'decisions.jsonl'))
    text = "## Sheet-2\n" + table(20)
    items = chunk_document_items(text, chunk_size=4000)
    result = refine_chunks(text, items, lambda _: {'bucket': '21to40'}, overlap=0)
    assert [item['metadata']['table_data_row_start'] for item in result] == [1, 11]
    assert [item['metadata']['table_data_row_end'] for item in result] == [10, 20]
    assert [item['metadata']['section_title'] for item in result] == ['Sheet-2', 'Sheet-2']


@pytest.mark.parametrize('version', ['v3', 'latest'])
def test_mixed_json_and_jsonl_keep_evidence_root_order(version):
    evidence = importlib.import_module(f'algorithm.knowledge_extract.{version}.table_evidence')
    router = importlib.import_module(f'algorithm.knowledge_extract.{version}.structured_text')
    text = '```json\n{\n"rows":[\n{"设备":"旧记录"}\n]\n}\n```\n' + table(2)
    payload = router.extract_json_payload(text)
    roots = evidence._source_roots(text)
    assert len(roots) == len(payload.blocks) == 3
    assert [json.loads(text[root.start:root.end]) for root in roots] == list(payload.blocks)
