import pytest
from langchain_core.documents import Document

from algorithm.NL_to_cypher.splitter import split_by_characters, split_recursively


@pytest.mark.parametrize('split', [split_by_characters, split_recursively])
def test_repeated_documents_keep_independent_offsets_and_crlf(split):
    source = '段落。\r\n' * 30
    documents = [Document(page_content=source, metadata={'document_id': name}) for name in ['a', 'b']]
    pieces = split(documents, chunk_size=40, chunk_overlap=8)
    for name in ['a', 'b']:
        selected = [piece for piece in pieces if piece.metadata['document_id'] == name]
        assert selected[0].metadata['__source_range'][0] == 0
        assert selected[-1].metadata['__source_range'][1] == len(source)
        restored = ''
        cursor = 0
        for piece in selected:
            start, end = piece.metadata['__source_range']
            assert source[start:end] == piece.page_content
            assert len(piece.page_content) <= 40
            assert start <= cursor
            restored += piece.page_content[max(0, cursor - start):]
            cursor = end
        assert restored == source


def test_recovery_can_split_protected_json_without_reserializing():
    source = '{"value": "' + '中文' * 100 + '"}'
    pieces = split_by_characters([Document(page_content=source)], chunk_size=40,
                                 chunk_overlap=0, protect_json=False)
    assert len(pieces) > 1
    assert ''.join(piece.page_content for piece in pieces) == source
    assert all(source[s:e] == piece.page_content
               for piece in pieces for s, e in [piece.metadata['__source_range']])
