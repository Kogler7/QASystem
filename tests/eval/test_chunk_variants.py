import json
from unittest.mock import Mock

import pytest

from eval import run
from eval.tools.chunk_variants import convert_record_tables, prepare, validate_ranges


def test_conversion_changes_only_record_table_serialization():
    rows = [{'名称': '甲\n乙', '值': '"1"'}, {'名称': '丙', '值': ''}]
    table = {'columns': ['名称', '值'], 'rows': rows}
    text = '# 标题\n```json\n' + json.dumps(table, ensure_ascii=False, indent=2) + '\n```\n正文'
    result, count = convert_record_tables(text)
    assert count == 1
    assert result.startswith('# 标题\n```jsonl\n') and result.endswith('\n```\n正文')
    actual = [json.loads(line) for line in result.splitlines() if line.startswith('{')]
    assert actual == rows
    for payload in ({'doc_type': 'form', 'fields': {'名称': '通知'}}, {'columns': ['名称'], 'rows': []}):
        old = '```json\n' + json.dumps(payload) + '\n```'
        assert convert_record_tables(old) == (old, 0)
    with pytest.raises(ValueError, match='extra fields'):
        convert_record_tables('```json\n' + json.dumps({**table, 'caption': '来源'}) + '\n```')


def test_freeze_uses_real_backend_options_and_rejects_bad_ranges(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    case = source / 'group/case'
    case.mkdir(parents=True)
    (case / 'document.md').write_text('正文', encoding='utf8')
    captured = []
    def chunk(url, text, name, options, params, output):
        captured.append(options)
        items = [{'text': text, 'range': [0, len(text)]}]
        run.write_json(output / 'chunks.json', {'data': {'items': items}})
        return items, {'status': 'succeeded', 'elapsed_seconds': 1}
    monkeypatch.setattr(run, '_chunk_markdown', chunk)
    params = {'backend_url': 'http://backend', 'options': {'chunking': {'size': 4000, 'chunk_overlap': 50}}}
    records = prepare(source, tmp_path / 'tmp/frozen', ['group/case'], params, 'jsonl-adaptive', 'abc')
    assert captured[0]['chunking'] == {'size': 4000, 'chunk_overlap': 50,
                                      'adaptive_entity_count': True, 'entity_count_model': 'clef-flash'}
    assert records[0]['chunk_ranges'] == [[0, 2]]
    assert 'adaptive_entity_count' not in params['options']['chunking']
    with pytest.raises(ValueError, match='overwrite'):
        prepare(source, tmp_path / 'tmp/frozen', ['group/case'], params, 'json', 'abc')
    with pytest.raises(ValueError, match='mismatch'):
        validate_ranges('正文', [{'text': '错误', 'range': [0, 2]}])


def test_frozen_ranges_require_full_coverage_and_allow_overlap():
    validate_ranges('abcdef', [{'text': 'abcd', 'range': [0, 4]},
                               {'text': 'cdef', 'range': [2, 6]}])
    for items in ([{'text': 'bcdef', 'range': [1, 6]}],
                  [{'text': 'abcd', 'range': [0, 4]}],
                  [{'text': 'ab', 'range': [0, 2]}, {'text': 'def', 'range': [3, 6]}]):
        with pytest.raises(ValueError, match='coverage gap'):
            validate_ranges('abcdef', items)


def test_boundary_whitespace_trimming_is_not_lost_content():
    validate_ranges(' \n甲\n\n乙\t', [{'text': '甲', 'range': [2, 3]},
                                  {'text': '乙', 'range': [5, 6]}])


@pytest.mark.parametrize('mode', ['extract', 'chunk-extract'])
def test_reused_chunks_pass_provenance_through_runner(tmp_path, monkeypatch, mode):
    source = tmp_path / 'frozen'
    item = {'text': '{"设备":"泵"}', 'range': [20, 30],
            'metadata': {'table_format': 'jsonl', 'section_title': 'Sheet-2'}}
    run.write_json(source / 'chunks.json', {'data': {'items': [item]}})
    run.write_text(source / 'document.md', item['text'])
    output = tmp_path / 'output'
    if mode == 'chunk-extract':
        run.write_json(output / 'chunks.json', {'data': {'items': [item]}})
    extract = Mock(return_value={'status': 'succeeded'})
    monkeypatch.setattr(run, '_extract_items', extract)
    run.run_backend_case(base_url='http://backend', sources=[], ontology='ontology', options={}, params={},
                         golden=None, out_dir=output, modes=[mode], reuse_dir=source)
    actual = extract.call_args.args[2]
    assert actual == [{'text': item['text'], 'ranges': [20, 30], 'metadata': item['metadata']}]
