from __future__ import annotations

import json
import logging

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

    documents = _dispatch(
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

    documents = _dispatch(
        content, chunk_type, chunk_size, chunk_characters,
        similarity, chunk_overlap, model_path, breakpoint_method,
    )

    cursor = 0
    items: list[dict] = []
    for doc in documents:
        text = doc.page_content
        if not text:
            continue
        start = content.find(text, cursor)
        if start < 0:
            start = content.find(text)
        if start < 0:
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