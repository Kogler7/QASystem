"""Frozen spreadsheet chunks must keep row boundaries and evidence identity."""
import json

import pytest

from ..chunk_context import ExtractionChunk
from ..property_grounding import _verification_context, locate_in_sources, locate_property_quote
from ..source_windows import source_parts, table_windows


@pytest.mark.parametrize('count', [1, 3])
@pytest.mark.parametrize('closed', [False, True])
def test_heading_and_partial_fence_keep_every_row_and_exact_offsets(count, closed):
    records = [json.dumps({'object': f'pump-{i}', 'padding': 'x' * 150, 'result': 'normal'})
               for i in range(count)]
    text = '## Sheet A\n\n```jsonl\n' + '\n'.join(records) + ('\n```' if closed else '\n')
    source = ExtractionChunk('original', text, metadata={'_v31_window_start': 37})
    windows = table_windows(source, 120)
    assert windows is not None
    rows = [w for w in windows if w.metadata.get('_v31_table_rows')]
    assert [w.text for w in rows] == records
    assert all(w.metadata['_v31_atomic_table'] for w in rows)
    assert any('## Sheet A' in w.text for w in windows)
    for window in windows:
        for part in source_parts(window):
            start = part.metadata.get('_v31_window_start', 0) - 37
            assert text[start:start + len(part.text)] == part.text
    spans = [locate_in_sources('normal', source_parts(rows[-1]))]
    context = _verification_context([({'type': 'Result', 'name': 'normal'}, '', None, spans, {})],
                                    source_parts(rows[-1]))
    assert records[-1] in '\n'.join(s.text for s in context)


def test_repeated_short_cell_quote_cannot_borrow_another_rows_subject():
    records = [{'subject': '泵甲', 'description': '已检查。分类：异常；结果待定。'},
               {'subject': '泵乙', 'description': '已检查。分类：异常；结果待定。'}]
    text = '\n'.join(json.dumps(r, ensure_ascii=False) for r in records)
    windows = table_windows(ExtractionChunk('c', text), 10000)
    assert locate_property_quote('分类：异常', windows) == []
    unique = locate_property_quote(json.dumps(records[1], ensure_ascii=False), windows)
    assert len(unique) == 1 and '泵乙' in unique[0]['quote']


def test_malformed_source_is_not_repaired_or_dropped():
    text = '## Sheet A\n```jsonl\n{"object":"pump"}\n{"object":"unterminated'
    assert table_windows(ExtractionChunk('c', text), 100) is None
