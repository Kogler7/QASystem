from __future__ import annotations

import json
from typing import Any

from langchain_core.documents import Document

from algorithm.NL_to_cypher.splitter import split_by_characters, split_by_markdown, split_recursively


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
) -> list[str]:
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if not 0 <= similarity <= 1:
        raise ValueError("similarity 必须在 0 到 1 之间")
    if not content:
        return []
    documents = [Document(page_content=content, metadata={})]
    if chunk_type == 0:
        result = split_recursively(documents, chunk_size=chunk_size)
    elif chunk_type == 1:
        result = split_by_characters(
            documents,
            chunk_size=chunk_size,
            separators=chunk_characters or DEFAULT_SEPARATORS,
        )
    elif chunk_type == 2:
        result = split_recursively(
            documents,
            chunk_size=chunk_size,
            separators=chunk_characters,
        )
    elif chunk_type == 3:
        result = split_by_markdown(documents, chunk_size=chunk_size)
    else:
        raise ValueError("chunk_type 仅支持 0、1、2、3")
    return [document.page_content for document in result]
