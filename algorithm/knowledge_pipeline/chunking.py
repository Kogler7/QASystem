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


DEFAULT_SEPARATORS = ["。", "！", "？", ".", "!", "?", "\n\n", "\n"]


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
    return [document.page_content for document in result]


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
        content, chunk_type, chunk_size, chunk_characters, similarity, chunk_overlap, model_path,
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
    return items
