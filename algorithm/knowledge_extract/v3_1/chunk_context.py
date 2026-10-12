"""Upstream chunk contract and bounded extraction context."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

_WS = re.compile(r"\s+")


@dataclass
class ExtractionChunk:
    """§5：上游解析与切片后的知识片段（Harness 只消费，不产生）。"""

    chunk_id: str
    text: str
    document_id: str | None = None
    section_id: str | None = None
    section_title: str | None = None
    clause_number: str | None = None
    page_no: int | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    parent_chunk_id: str | None = None
    previous_chunk_id: str | None = None
    next_chunk_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "metadata": dict(self.metadata),
        }
        for name in (
            "document_id",
            "section_id",
            "section_title",
            "clause_number",
            "page_no",
            "start_offset",
            "end_offset",
            "parent_chunk_id",
            "previous_chunk_id",
            "next_chunk_id",
        ):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ExtractionChunk":
        allowed = {
            "chunk_id",
            "text",
            "document_id",
            "section_id",
            "section_title",
            "clause_number",
            "page_no",
            "start_offset",
            "end_offset",
            "parent_chunk_id",
            "previous_chunk_id",
            "next_chunk_id",
            "metadata",
        }
        return cls(**{key: value for key, value in (payload or {}).items() if key in allowed})


@dataclass
class ExtractionRequest:
    """Extraction options attached to caller-provided chunks."""

    chunks: list[ExtractionChunk]
    target_types: list[str] | None = None


class ChunkContextManager:
    """§6：chunk 索引、局部上下文与 evidence 定位。"""

    def __init__(self, chunks: Iterable[ExtractionChunk]):
        self._chunks: dict[str, ExtractionChunk] = {}
        self._order: list[str] = []
        self._positions: dict[str, int] = {}
        for chunk in chunks:
            chunk_id = (chunk.chunk_id or "").strip()
            if not chunk_id:
                raise ValueError("chunk_id 不能为空")
            if chunk_id in self._chunks:
                raise ValueError(f"chunk_id 重复: {chunk_id!r}")
            self._chunks[chunk_id] = chunk
            self._positions[chunk_id] = len(self._order)
            self._order.append(chunk_id)

    @property
    def chunk_ids(self) -> list[str]:
        return list(self._order)

    def __len__(self) -> int:
        return len(self._order)

    def __iter__(self):
        return iter(self._chunks[chunk_id] for chunk_id in self._order)

    def has_chunk(self, chunk_id: str) -> bool:
        return chunk_id in self._chunks

    def get_chunk(self, chunk_id: str) -> ExtractionChunk:
        try:
            return self._chunks[chunk_id]
        except KeyError:
            raise KeyError(f"未知 chunk_id: {chunk_id!r}") from None

    def index_of(self, chunk_id: str) -> int | None:
        return self._positions.get(chunk_id)

    def _previous_id(self, chunk_id: str) -> str | None:
        chunk = self.get_chunk(chunk_id)
        if chunk.previous_chunk_id:
            return chunk.previous_chunk_id if self.has_chunk(chunk.previous_chunk_id) else None
        index = self.index_of(chunk_id)
        return self._order[index - 1] if index is not None and index > 0 else None

    def _next_id(self, chunk_id: str) -> str | None:
        chunk = self.get_chunk(chunk_id)
        if chunk.next_chunk_id:
            return chunk.next_chunk_id if self.has_chunk(chunk.next_chunk_id) else None
        index = self.index_of(chunk_id)
        return self._order[index + 1] if index is not None and index + 1 < len(self._order) else None

    def local_context(
        self,
        chunk_id: str,
        *,
        previous: int = 1,
        next: int = 1,
        same_section: bool = True,
    ) -> list[ExtractionChunk]:
        """返回以 ``chunk_id`` 为中心的有界局部上下文（§6）。"""
        self.get_chunk(chunk_id)
        ids: list[str] = []
        current = chunk_id
        for _ in range(max(0, previous)):
            previous_id = self._previous_id(current)
            if previous_id is None:
                break
            ids.insert(0, previous_id)
            current = previous_id
        ids.append(chunk_id)
        current = chunk_id
        for _ in range(max(0, next)):
            next_id = self._next_id(current)
            if next_id is None:
                break
            ids.append(next_id)
            current = next_id

        chunks = [self.get_chunk(item_id) for item_id in ids]
        anchor = self.get_chunk(chunk_id)
        chunks = [chunk for chunk in chunks if chunk.document_id == anchor.document_id]
        if same_section:
            chunks = [chunk for chunk in chunks if chunk.section_id == anchor.section_id]
        return chunks

    def context_text(
        self,
        chunk_id: str,
        *,
        previous: int = 1,
        next: int = 1,
        same_section: bool = True,
        join: str = "\n",
    ) -> str:
        """有界拼接局部上下文文本；不拼接整篇文档。"""
        return join.join(chunk.text for chunk in self.local_context(
            chunk_id,
            previous=previous,
            next=next,
            same_section=same_section,
        ))

    def find_span(self, chunk_id: str, quote: str) -> tuple[int, int] | None:
        """在 chunk 原文中定位 evidence quote，返回原始字符区间（空白不敏感）。"""
        chunk = self.get_chunk(chunk_id)
        text = chunk.text or ""
        quote = _WS.sub(" ", (quote or "").strip())
        if not quote:
            return None
        pattern = r"\s+".join(re.escape(part) for part in quote.split(" "))
        match = re.search(pattern, text)
        if match is None:
            return None
        return match.start(), match.end()

    def evidence_text(
        self,
        chunk_id: str,
        start_offset: int | None = None,
        end_offset: int | None = None,
    ) -> str:
        """按 chunk 内偏移取证据原文（越界自动收敛）。"""
        chunk = self.get_chunk(chunk_id)
        text = chunk.text or ""
        start = 0 if start_offset is None else max(0, int(start_offset))
        end = len(text) if end_offset is None else min(len(text), int(end_offset))
        if start >= end:
            return ""
        return text[start:end]
