from __future__ import annotations

import json
import logging
import re

from langchain_core.documents import Document

from algorithm.NL_to_cypher.splitter import (
    split_by_characters,
    split_by_markdown,
    split_recursively,
    split_semantically,
)

logger = logging.getLogger(__name__)

DEFAULT_SEPARATORS = ["。", "！", "？", ".", "!", "?", "\n\n", "\n"]

CHUNK_ALGORITHMS = {
    0: "split_recursively",
    1: "split_by_characters",
    2: "split_recursively(自定义分隔符)",
    3: "split_by_markdown",
    4: "split_semantically",
}


def parse_chunk_characters(raw: str | list[str] | None) -> list[str]:
    if raw is None or raw == "":
        return list(DEFAULT_SEPARATORS)
    if isinstance(raw, list):
        values = raw
    else:
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("chunk_character 必须是 JSON 字符串数组") from exc
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError("chunk_character 必须是字符串数组")
    separators = [item for item in values if item]
    if not separators:
        raise ValueError("chunk_character 至少包含一个非空分隔符")
    return separators


def _dispatch(
    content: str,
    chunk_type: int,
    chunk_size: int,
    chunk_characters: list[str] | None,
    similarity: float,
    chunk_overlap: int,
    model_path: str | None,
    breakpoint_method: str,
) -> list[Document]:
    documents = [Document(page_content=content, metadata={})]
    if chunk_type == 0:
        return split_recursively(documents, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    if chunk_type == 1:
        return split_by_characters(
            documents, chunk_size=chunk_size,
            separators=chunk_characters or DEFAULT_SEPARATORS,
            chunk_overlap=chunk_overlap,
        )
    if chunk_type == 2:
        return split_recursively(
            documents, chunk_size=chunk_size,
            separators=chunk_characters, chunk_overlap=chunk_overlap,
        )
    if chunk_type == 3:
        return split_by_markdown(documents, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    if chunk_type == 4:
        return split_semantically(
            documents, chunk_size=chunk_size, similarity=similarity,
            chunk_overlap=chunk_overlap, model_path=model_path,
            breakpoint_method=breakpoint_method,
        )
    raise ValueError("chunk_type 仅支持 0、1、2、3、4")


def _log_chunks(algo: str, chunks: list[str]) -> None:
    """输出切片效果：片数、总长、每片长度、超限/碎片统计。"""
    sizes = [len(c) for c in chunks]
    logger.info(
        "[步骤][切片] 算法=%s 切片数=%d 总字符=%d 每片长度=%s",
        algo, len(chunks), sum(sizes), sizes,
    )
    if any(s > 10000 for s in sizes):
        logger.warning("[步骤][切片] 存在超长片: %s", [s for s in sizes if s > 10000])
    if any(s < 500 for s in sizes):
        logger.info("[步骤][切片] 存在碎片片(<500): %s", [s for s in sizes if s < 500])


_FENCE_CHAR = chr(96)
_FENCE_MARKS = (_FENCE_CHAR * 3, "~~~")


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """扫描围栏符配对，返回每个围栏块的 ``[start, end)`` 字符区间。"""
    spans: list[tuple[int, int]] = []
    open_at: int | None = None
    position = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith(_FENCE_MARKS):
            if open_at is None:
                open_at = position
            else:
                spans.append((open_at, position + len(line)))
                open_at = None
        position += len(line)
    return spans


def _heading_start(text: str, fence_start: int) -> int:
    """把紧邻表格上方的 Markdown 标题并进表格片，避免标题与表格分家。"""
    head_lines = text[:fence_start].splitlines(keepends=True)
    index = len(head_lines) - 1
    back = 0
    while index >= 0 and not head_lines[index].strip():
        back += len(head_lines[index])
        index -= 1
    if index >= 0 and head_lines[index].lstrip().startswith("#"):
        return fence_start - back - len(head_lines[index])
    return fence_start


def _table_aware_spans(content: str) -> list[tuple[bool, int, int]] | None:
    """把每个围栏块（含紧邻标题）标成不可按长度切分的表格区间。

    按字符切会在表格中间下刀：JSON 变成非法片段、围栏只剩一半、工作表标题与表格分家，
    抽取必然失败（几十行 Excel 抽不出来就是此因）。超长表格不在这里切，改由抽取层
    ``_structured_row_segments`` 按行拆成多个自洽子表消化——切片层保证"表格完整"、
    抽取层负责"超长表格按行拆"，两段式契约缺一不可。

    返回 ``None`` 表示没有围栏块（走原逻辑）；否则返回有序的 ``(is_table, start, end)``。
    """
    spans = _fenced_spans(content)
    if not spans:
        return None
    segments: list[tuple[bool, int, int]] = []
    cursor = 0
    for start, end in spans:
        # 相邻表格共用标题等情况下 head 可能落到上一片内，钳制以免区间重叠。
        head = max(cursor, _heading_start(content, start))
        if head > cursor:
            segments.append((False, cursor, head))
        segments.append((True, head, end))
        cursor = end
    if cursor < len(content):
        segments.append((False, cursor, len(content)))
    return [item for item in segments if content[item[1]:item[2]].strip()]


def _chunk_units(
    content: str,
    chunk_type: int,
    chunk_size: int,
    chunk_characters: list[str] | None,
    similarity: float,
    chunk_overlap: int,
    model_path: str | None,
    breakpoint_method: str,
) -> list[Document]:
    """表格感知切分：围栏表格整块成片，其余文本交给 splitter。

    表格片不经拆分器改写（既不重新序列化、也不会丢掉半截围栏），并带上精确的
    ``__source_range``，保证下游 ``chunk_document_items`` 与 evidence 始终指回原文。
    """
    from algorithm.table_jsonl import pack_ranges, row_boundaries

    # Bare row windows (including windows carrying only one fence edge).
    if row_boundaries(content) is not None and "```jsonl" not in content.lower() and len(content.splitlines()) > 1:
        return [Document(page_content=content[a:b], metadata={"__source_range": [a, b]})
                for a, b in pack_ranges(content, chunk_size, chunk_overlap)]
    segments = _table_aware_spans(content)
    if segments is None:
        documents = _dispatch(
            content, chunk_type, chunk_size, chunk_characters,
            similarity, chunk_overlap, model_path, breakpoint_method,
        )
        return [doc for doc in documents if doc.page_content]

    units: list[Document] = []
    table_count = 0
    for is_table, start, end in segments:
        if is_table:
            table_count += 1
            table_text = content[start:end]
            ranges = (pack_ranges(table_text, chunk_size, chunk_overlap)
                      if "```jsonl" in table_text.lower() and row_boundaries(table_text) is not None
                      else [[0, len(table_text)]])
            boundaries = row_boundaries(table_text) if "```jsonl" in table_text.lower() else None
            headings = re.findall(r"(?m)^#{1,6}\s+(.+)$", table_text[:table_text.find("```")])
            for a, b in ranges:
                metadata = {"__source_range": [start + a, start + b]}
                if boundaries is not None:
                    metadata.update(table_format="jsonl", table_data_row_start=boundaries.index(a)+1,
                                    table_data_row_end=boundaries.index(b))
                    if headings:
                        metadata["section_title"] = headings[-1]
                units.append(Document(page_content=table_text[a:b], metadata=metadata))
            continue
        for doc in _chunk_units(
            content[start:end], chunk_type, chunk_size, chunk_characters,
            similarity, chunk_overlap, model_path, breakpoint_method,
        ):
            meta = dict(doc.metadata)
            source_range = meta.get("__source_range")
            if isinstance(source_range, (list, tuple)) and len(source_range) == 2:
                # 子片偏移相对片段起点，平移回整篇坐标系。
                meta["__source_range"] = [start + int(source_range[0]), start + int(source_range[1])]
            units.append(Document(page_content=doc.page_content, metadata=meta))
    logger.info(
        "[步骤][切片] 表格感知切分: 片段=%d 表格片=%d 切片=%d",
        len(segments), table_count, len(units),
    )
    return units


def chunk_document(
    content: str,
    chunk_type: int = 0,
    chunk_size: int = 500,
    chunk_characters: list[str] | None = None,
    similarity: float = 0.6,
    chunk_overlap: int = 0,
    model_path: str | None = None,
    breakpoint_method: str = "percentile",
) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if not 0 <= similarity <= 1:
        raise ValueError("similarity 必须在 0 到 1 之间")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap 不能为负数")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap 必须小于 chunk_size")
    if not content:
        return []

    algo = CHUNK_ALGORITHMS.get(chunk_type, f"chunk_type={chunk_type}")
    logger.info(
        "[步骤][切片] 输入: 算法=%s 字符数=%d size=%d overlap=%d 分隔符=%s",
        algo, len(content), chunk_size, chunk_overlap,
        chunk_characters or DEFAULT_SEPARATORS,
    )

    documents = _chunk_units(
        content, chunk_type, chunk_size, chunk_characters,
        similarity, chunk_overlap, model_path, breakpoint_method,
    )
    chunks = [doc.page_content for doc in documents if doc.page_content]

    _log_chunks(algo, chunks)
    return chunks


def chunk_document_items(
    content: str,
    chunk_type: int = 0,
    chunk_size: int = 500,
    chunk_characters: list[str] | None = None,
    similarity: float = 0.6,
    chunk_overlap: int = 0,
    model_path: str | None = None,
    breakpoint_method: str = "percentile",
) -> list[dict]:
    """带原文偏移的切块输出。

    若下游不再需要 range，可直接改用 chunk_document，本函数可废弃。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if not 0 <= similarity <= 1:
        raise ValueError("similarity 必须在 0 到 1 之间")
    if chunk_overlap < 0:
        raise ValueError("chunk_overlap 不能为负数")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap 必须小于 chunk_size")
    if not content:
        return []

    algo = CHUNK_ALGORITHMS.get(chunk_type, f"chunk_type={chunk_type}")
    logger.info("[步骤][切片][items] 输入: 算法=%s 字符数=%d size=%d", algo, len(content), chunk_size)

    documents = _chunk_units(
        content, chunk_type, chunk_size, chunk_characters,
        similarity, chunk_overlap, model_path, breakpoint_method,
    )

    cursor = 0
    items: list[dict] = []
    for doc in documents:
        text = doc.page_content
        if not text:
            continue
        source_range = doc.metadata.get("__source_range")
        if (isinstance(source_range, (list, tuple)) and len(source_range) == 2
                and all(isinstance(v, int) for v in source_range)
                and 0 <= source_range[0] <= source_range[1] <= len(content)
                and content[source_range[0]:source_range[1]] == text):
            start, end = source_range
            item = {"text": text, "range": [start, end]}
            if doc.metadata.get("table_format") == "jsonl":
                item["metadata"] = {key: value for key, value in doc.metadata.items() if key != "__source_range"}
            items.append(item)
            cursor = end
            continue
        start = content.find(text, cursor)
        if start < 0:
            start = content.find(text)
        if start < 0:
            # 定位失败会污染下游 evidence 的字符范围，必须留痕（历史实现是静默回退）。
            logger.warning(
                "[步骤][切片][items] 偏移定位失败，已回退到游标位置: 切片=%d 字符数=%d",
                len(items), len(text),
            )
            start = cursor
        end = min(len(content), start + len(text))
        items.append({"text": text, "range": [start, end]})
        cursor = end

    sizes = [r[1] - r[0] for r in (it["range"] for it in items)]
    logger.info(
        "[步骤][切片][items] 输出: 算法=%s 切片数=%d 总长=%d 每片长度=%s range=[%s...%s]",
        algo, len(items), sum(sizes), sizes,
        items[0]["range"] if items else None, items[-1]["range"] if items else None,
    )
    return items

def extraction_segment_ranges(content: str, target: int, overlap: int = 50) -> list[tuple[int, int]]:
    """Reuse upstream lossless delimiter splitting with a strict recovery size cap.

    Oversized protected JSON is split as raw source text only as a last resort.
    Never reserialize it: downstream quotes/ranges must still refer to the input.
    """
    if not content:
        return []
    size = max(1, int(target))
    overlap = min(max(0, int(overlap)), size - 1)
    def split(text, protect=True):
        return split_by_characters([Document(page_content=text)], chunk_size=size,
                                   chunk_overlap=overlap, separators=DEFAULT_SEPARATORS,
                                   protect_json=protect)
    ranges = []
    for doc in split(content):
        start, end = doc.metadata["__source_range"]
        if end - start <= size:
            ranges.append((start, end))
        else:
            for child in split(content[start:end], False):
                left, right = child.metadata["__source_range"]
                ranges.append((start + left, start + right))
    return ranges
