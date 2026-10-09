"""Observation windows over immutable upstream chunks, with original offsets."""
from __future__ import annotations

try:
    from ..v2.chunk_context import ExtractionChunk
except ImportError:
    from algorithm.knowledge_extract.v2.chunk_context import ExtractionChunk


_LOCATION_KEYS = {'source_file', 'source_kind', 'sheet', 'sheet_name', 'row', 'row_start', 'row_end', 'column', 'columns', 'table_id', 'headers', 'units'}


def _location_value(value):
    # Coordinates and table headings are scalars/lists, never request-header maps.
    if isinstance(value, (str,int,float,bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_location_value(item) for item in value if not isinstance(item,dict)]
    return None


def _window(chunk: ExtractionChunk, start: int, end: int, *, context: bool = False) -> dict:
    return {
        'chunk_id':chunk.chunk_id, 'document_id':chunk.document_id,
        'text':chunk.text[start:end], 'start_offset':start, 'end_offset':end,
        'document_start_offset':None if chunk.start_offset is None else chunk.start_offset + start,
        'page_no':chunk.page_no, 'section_id':chunk.section_id,
        'section_title':chunk.section_title, 'context_only':context,
        'metadata':{key:_location_value(value) for key,value in chunk.metadata.items() if key in _LOCATION_KEYS and not isinstance(value,dict)},
    }


def plan_observations(chunks, *, window_chars: int = 6000) -> tuple[list[dict], list[dict]]:
    if isinstance(chunks, (str, bytes, dict)):
        raise TypeError('chunks must be an iterable of ExtractionChunk')
    if window_chars < 128:
        raise ValueError('window_chars must be at least 128')
    chunks = list(chunks)
    if any(not isinstance(chunk, ExtractionChunk) for chunk in chunks):
        raise TypeError('chunks must contain ExtractionChunk objects')
    documents, diagnostics, seen = {}, [], set()
    for chunk in chunks:
        if not isinstance(chunk.chunk_id, str) or not chunk.chunk_id.strip() or chunk.chunk_id in seen:
            raise ValueError('chunk_id must be nonempty and unique')
        if not isinstance(chunk.text, str):
            raise TypeError('chunk.text must be a string')
        seen.add(chunk.chunk_id)
        document_id = chunk.document_id or f'isolated:{chunk.chunk_id}'
        if not chunk.document_id:
            diagnostics.append({'code':'DOCUMENT_SCOPE_MISSING', 'chunk_id':chunk.chunk_id})
        documents.setdefault(document_id, []).append(chunk)
    observations = []
    for document_id, members in documents.items():
        for index, chunk in enumerate(members):
            start = 0
            # Empty chunks still get a covered observation; no synthetic facts.
            while start < len(chunk.text) or (not chunk.text and start == 0):
                end = min(len(chunk.text), start + window_chars)
                if end < len(chunk.text):
                    boundary = chunk.text.rfind('\n', start + window_chars // 2, end)
                    if boundary >= 0:
                        end = boundary + 1
                windows = [_window(chunk, start, end)]
                if start:
                    windows.insert(0, _window(chunk, 0, min(500, start), context=True))
                    if start > 500:
                        windows.insert(1, _window(chunk, max(500, start-400), start, context=True))
                elif index:
                    previous = members[index-1]
                    windows.insert(0, _window(previous, max(0, len(previous.text)-800), len(previous.text), context=True))
                if end < len(chunk.text):
                    windows.append(_window(chunk, end, min(end+400, len(chunk.text)), context=True))
                elif index+1 < len(members):
                    following = members[index+1]
                    windows.append(_window(following, 0, min(800, len(following.text)), context=True))
                observations.append({'id':f'o{len(observations)+1}', 'document_id':document_id, 'source_windows':windows})
                if end == len(chunk.text):
                    break
                start = end
    return observations, diagnostics
