from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from algorithm.NL_to_cypher.vector import batch_text_to_vectors
from algorithm.knowledge_extract import deduplicate_chunk_graphs, extract_chunks
from algorithm.knowledge_extract.config import get_llm_config
from algorithm.knowledge_pipeline import chunk_document_items, md_to_text, normalize_document_content
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor

from backend.app.responses import ErrorCode
from backend.app.schemas import (
    ChunkOptions,
    GraphExtractionItem,
    GraphItem,
    ParserConfig,
    TextChunk,
    TextEmbeddingItem,
)


class CapabilityError(Exception):
    def __init__(self, code: ErrorCode, info: str, stage: str, details: Any = None):
        super().__init__(info)
        self.code = code
        self.info = info
        self.stage = stage
        self.details = details


@dataclass(slots=True)
class GraphExtractionBatch:
    items: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    scheduler: dict[str, Any]
    graph: dict[str, Any] | None = None
    graphs: list[dict[str, Any]] | None = None

    def graph_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.graph is not None:
            data["graph"] = self.graph
        else:
            data["graphs"] = self.graphs or []
        return data

    def pipeline_data(self) -> dict[str, Any]:
        return {
            "ranges": [item.get("ranges") for item in self.items],
            **self.graph_data(),
        }


def _document_error(reason: str) -> ErrorCode:
    lowered = reason.lower()
    if "不支持" in reason and ("文件" in reason or "类型" in reason):
        return ErrorCode.UNSUPPORTED_FILE_TYPE
    if any(keyword in lowered for keyword in ("libreoffice", "soffice", "model", "dependency")) or any(
        keyword in reason for keyword in ("未安装", "不可用", "依赖缺失", "模型缺失")
    ):
        return ErrorCode.PARSER_UNAVAILABLE
    if "转换" in reason or "convert" in lowered:
        return ErrorCode.DOCUMENT_CONVERT_FAILED
    return ErrorCode.DOCUMENT_PARSE_FAILED


def _extraction_error(failed: list[dict[str, Any]], total_count: int) -> ErrorCode:
    if len(failed) < total_count:
        return ErrorCode.PARTIAL_EXTRACTION_FAILED
    error_codes = {item.get("error_code") for item in failed}
    messages = " ".join(
        str(attempt.get("message") or "")
        for item in failed
        for attempt in item.get("attempts", [])
    ).lower()
    if "timeout" in messages or "超时" in messages:
        return ErrorCode.LLM_TIMEOUT
    if "429" in messages or "rate limit" in messages or "限流" in messages:
        return ErrorCode.LLM_RATE_LIMITED
    if error_codes == {"LLM_JSON_PARSE_FAILED"}:
        return ErrorCode.LLM_RESPONSE_INVALID
    if error_codes == {"LLM_REQUEST_FAILED"}:
        return ErrorCode.LLM_REQUEST_FAILED
    return ErrorCode.GRAPH_EXTRACTION_FAILED


class DocumentParsingService:
    def __init__(self, processor_factory=DocumentProcessor):
        self.processor = processor_factory()
        self._lock = threading.Lock()

    @staticmethod
    def parser_summary(metadata: dict[str, Any]) -> dict[str, Any]:
        selection = metadata.get("engine_selection") or {}
        summary: dict[str, Any] = {
            "preferred_engine": selection.get("preferred_engine") or metadata.get("model_used"),
            "final_engine": metadata.get("model_used"),
        }
        if selection.get("requested_engine"):
            summary["requested_engine"] = selection["requested_engine"]
            summary["request_handling"] = selection.get("request_role")
        if metadata.get("fallback_from"):
            fallback = metadata["fallback_from"]
            summary["fallback"] = {
                "from_engine": fallback.get("engine"),
                "reason": fallback.get("error"),
            }
        if metadata.get("encoding"):
            summary["encoding"] = metadata["encoding"]
        return summary

    def parse(self, file_name: str, content: bytes, config: ParserConfig) -> dict[str, Any]:
        if not content:
            raise CapabilityError(ErrorCode.INVALID_REQUEST, "上传文件不能为空", "validation")
        safe_name = Path(file_name or "upload.bin").name
        suffix = Path(safe_name).suffix.lower()
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                handle.write(content)
                temp_path = handle.name
            with self._lock:
                result = self.processor.process_file(
                    temp_path,
                    model_name=config.engine,
                    lang=config.mineru_options.language,
                    ocr_mode=config.mineru_options.ocr_mode,
                    table_enable=config.mineru_options.table_enable,
                    formula_enable=config.mineru_options.formula_enable,
                )
            if not result.get("success"):
                reason = str(result.get("error") or "未知原因")
                raise CapabilityError(
                    _document_error(reason),
                    f"文档解析失败：{reason}",
                    "document_parse",
                    result,
                )
            markdown = normalize_document_content(result.get("content"))
            if not markdown.strip():
                raise CapabilityError(ErrorCode.DOCUMENT_CONTENT_EMPTY, "文档解析结果为空", "document_parse")
            return {
                "file_name": safe_name,
                "file_size": len(content),
                "file_type": suffix.lstrip("."),
                "markdown": markdown,
                "content_length": len(markdown),
                "parser": self.parser_summary(result.get("metadata") or {}),
            }
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)


class DocumentChunkingService:
    def chunk(self, markdown: str, options: ChunkOptions) -> dict[str, Any]:
        try:
            items = chunk_document_items(
                markdown,
                options.legacy_chunk_type(),
                options.size,
                options.separators,
                options.similarity,
                options.chunk_overlap,
                breakpoint_method=options.breakpoint_method,
            )
        except ValueError as exc:
            code = ErrorCode.CHUNK_OFFSET_FAILED if "字符偏移" in str(exc) else ErrorCode.INVALID_CHUNK_CONFIG
            raise CapabilityError(code, str(exc), "chunking") from exc
        if not items:
            raise CapabilityError(ErrorCode.CHUNK_RESULT_EMPTY, "文档切片结果为空", "chunking")
        return {"count": len(items), "items": items}


class GraphExtractionService:
    def extract(
        self,
        ontology: str,
        items: list[GraphExtractionItem | TextChunk],
        model: str | None = None,
        base_url: str | None = None,
        merge: bool = True,
    ) -> GraphExtractionBatch:
        if not ontology.strip():
            raise CapabilityError(ErrorCode.ONTOLOGY_INVALID, "ontology 不能为空", "validation")
        results = extract_chunks(
            ontology=ontology,
            chunks=[md_to_text(item.text) for item in items],
            model=model,
            base_url=base_url,
        )
        for position, (source, result) in enumerate(zip(items, results)):
            result["index"] = position
            result["text"] = source.text
            ranges = getattr(source, "ranges", None)
            result["ranges"] = list(ranges) if ranges is not None else None
        failed = [item for item in results if item.get("error")]
        if failed:
            raise CapabilityError(
                _extraction_error(failed, len(results)),
                "部分文本切片知识抽取失败",
                "knowledge_extract",
                {
                    "failed_count": len(failed),
                    "failed_items": [
                        {
                            "position": item["index"],
                            "error_code": item.get("error_code"),
                            "info": item.get("error"),
                            "attempts": item.get("attempts", []),
                            "raw_response": item.get("raw_response", ""),
                        }
                        for item in failed
                    ],
                },
            )
        config = get_llm_config()
        attempts = [
                {"position": item["index"], **attempt}
                for item in results
                for attempt in item.get("attempts", [])
            ]
        scheduler = {
            "max_concurrency": config["max_concurrency"],
            "qps": config["qps"],
            "qpm": config["qpm"],
        }
        if merge:
            return GraphExtractionBatch(
                items=results,
                attempts=attempts,
                scheduler=scheduler,
                graph=deduplicate_chunk_graphs(results),
            )
        return GraphExtractionBatch(
            items=results,
            attempts=attempts,
            scheduler=scheduler,
            graphs=[item["graph"] for item in results],
        )


class EmbeddingService:
    @staticmethod
    def _vectors(texts: list[str]) -> list[list[float]]:
        try:
            vectors = batch_text_to_vectors(texts)
        except Exception as exc:
            message = str(exc)
            code = ErrorCode.EMBEDDING_MODEL_UNAVAILABLE if any(
                keyword in message.lower() for keyword in ("model", "not found", "no such file")
            ) else ErrorCode.EMBEDDING_FAILED
            raise CapabilityError(code, "向量化失败", "embedding", message) from exc
        if len(vectors) != len(texts):
            raise CapabilityError(
                ErrorCode.VECTOR_COUNT_MISMATCH,
                "向量数量与输入数量不一致",
                "embedding",
                {"input_count": len(texts), "vector_count": len(vectors)},
            )
        return vectors

    def texts(self, items: list[TextEmbeddingItem]) -> dict[str, Any]:
        vectors = self._vectors([item.text for item in items])
        return {
            "count": len(vectors),
            "dimension": len(vectors[0]) if vectors else 0,
            "items": [
                {"vector": vector}
                for vector in vectors
            ],
        }

    def graphs(self, items: list[GraphItem]) -> dict[str, Any]:
        texts = [json.dumps(item.graph, ensure_ascii=False, sort_keys=True) for item in items]
        vectors = self._vectors(texts)
        return {
            "count": len(vectors),
            "dimension": len(vectors[0]) if vectors else 0,
            "items": [
                {
                    "graph": item.graph,
                    "serialized_text": text,
                    "vector": vector,
                }
                for item, text, vector in zip(items, texts, vectors)
            ],
        }


def merge_graphs(items: list[GraphItem]) -> dict[str, Any]:
    graph = deduplicate_chunk_graphs([{"graph": item.graph} for item in items])
    return {"count": len(items), "graph": graph}


document_parsing_service = DocumentParsingService()
document_chunking_service = DocumentChunkingService()
graph_extraction_service = GraphExtractionService()
embedding_service = EmbeddingService()
graph_merge_service = merge_graphs
