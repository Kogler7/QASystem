from __future__ import annotations

import json
from typing import Any

from langchain_core.documents import Document

from algorithm.NL_to_cypher.splitter import (
    split_by_characters,
    split_by_markdown,
    split_recursively,
    split_semantically,
)
from algorithm.logging_setup import get_step_logger, preview


logger = get_step_logger(__name__)

DEFAULT_SEPARATORS = ["。", "！", "？", ".", "!", "?", "\n\n", "\n"]

#: chunk_type → 实际使用的切分算法，日志里直接打出算法名。
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
    logger.info(
        "[步骤][切片] 输入: %d字符 算法=%s size=%d overlap=%d 分隔符=%s",
        len(content),
        CHUNK_ALGORITHMS.get(chunk_type, f"chunk_type={chunk_type}"),
        chunk_size,
        chunk_overlap,
        preview(chunk_characters or DEFAULT_SEPARATORS, 60),
    )
    documents = [Document(page_content=content, metadata={})]
    if chunk_type == 0:
        result = split_recursively(documents, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    elif chunk_type == 1:
        result = split_by_characters(
            documents,
            chunk_size=chunk_size,
            separators=chunk_characters or DEFAULT_SEPARATORS,
            chunk_overlap=chunk_overlap,
        )
    elif chunk_type == 2:
        result = split_recursively(
            documents,
            chunk_size=chunk_size,
            separators=chunk_characters,
            chunk_overlap=chunk_overlap,
        )
    elif chunk_type == 3:
        result = split_by_markdown(documents, chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    elif chunk_type == 4:
        result = split_semantically(
            documents,
            chunk_size=chunk_size,
            similarity=similarity,
            chunk_overlap=chunk_overlap,
            model_path=model_path,
            breakpoint_method=breakpoint_method,
        )
    else:
        raise ValueError("chunk_type 仅支持 0、1、2、3、4")
    chunks = [document.page_content for document in result]
    logger.info(
        "[步骤][切片] 输出: 算法=%s 切片=%d 总字符=%d 单片字符=%s",
        CHUNK_ALGORITHMS.get(chunk_type, f"chunk_type={chunk_type}"),
        len(chunks),
        sum(len(item) for item in chunks),
        preview([len(item) for item in chunks], 80),
    )
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
) -> list[dict[str, Any]]:
    """Return ordered chunks with source character ranges."""
    chunks = chunk_document(
        content,
        chunk_type=chunk_type,
        chunk_size=chunk_size,
        chunk_characters=chunk_characters,
        similarity=similarity,
        chunk_overlap=chunk_overlap,
        model_path=model_path,
        breakpoint_method=breakpoint_method,
    )
    items: list[dict[str, Any]] = []
    cursor = 0
    for index, text in enumerate(chunks):
        start = content.find(text, cursor)
        if start < 0:
            start = content.find(text)
        if start < 0:
            raise ValueError(f"无法定位切片 {index} 在原始 Markdown 中的字符偏移")
        end = min(len(content), start + len(text))
        items.append(
            {
                "text": text,
                "range": [start, end],
            }
        )
        cursor = end
    logger.info(
        "[步骤][切片] 偏移对齐输出: %d片 range=%s…%s 首片预览=%s",
        len(items),
        items[0]["range"] if items else None,
        items[-1]["range"] if items else None,
        preview(items[0]["text"], 60) if items else "",
    )
    return items
