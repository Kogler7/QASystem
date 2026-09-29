from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from algorithm.NL_to_cypher.vector import batch_text_to_vectors
from algorithm.knowledge_extract.v2 import deduplicate_chunk_graphs
from algorithm.knowledge_extract.v2.config import get_llm_config
from algorithm.knowledge_pipeline import chunk_document_items, normalize_document_content
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
from algorithm.knowledge_extract.adapter import extract_chunks


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
    quality: dict[str, Any] | None = None
    isolated_graph: dict[str, Any] | None = None
    isolated_graphs: list[dict[str, Any]] | None = None

    def graph_data(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        publishable = bool((self.quality or {}).get("graph_publishable", True))
        if self.graph is not None:
            data["graph"] = self.graph if publishable else {"nodes": [], "relationships": []}
            quarantined_graph = self.isolated_graph if self.isolated_graph is not None else self.graph
            if not publishable and quarantined_graph is not None:
                data["isolated_graph"] = quarantined_graph
            elif self.isolated_graph is not None:
                data["isolated_graph"] = self.isolated_graph
        else:
            data["graphs"] = (self.graphs or []) if publishable else []
            quarantined_graphs = self.isolated_graphs if self.isolated_graphs is not None else self.graphs
            if not publishable and quarantined_graphs is not None:
                data["isolated_graphs"] = quarantined_graphs
            elif self.isolated_graphs is not None:
                data["isolated_graphs"] = self.isolated_graphs
        data["graph_publishable"] = publishable
        if self.quality is not None:
            data["quality"] = self.quality
        return data

    def pipeline_data(self) -> dict[str, Any]:
        data = {
            "ranges": [item.get("ranges") for item in self.items],
            "metadata": [item.get("metadata") or {} for item in self.items],
            **self.graph_data(),
        }
        if self.quality is not None:
            data["quality"] = self.quality
        return data


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


#: Failure codes meaning "the model produced something we could not use".
#: Knowledge Extract V2 reports these as LLM_SCHEMA_INVALID /
#: LLM_RESPONSE_INCOMPLETE / LLM_EMPTY_RESPONSE where V1 only had
#: LLM_JSON_PARSE_FAILED; they all map onto the same client-facing error.
_UNUSABLE_RESPONSE_CODES = frozenset(
    {
        "LLM_JSON_PARSE_FAILED",
        "LLM_SCHEMA_INVALID",
        "LLM_RESPONSE_INCOMPLETE",
        "LLM_EMPTY_RESPONSE",
    }
)

#: V2 terminal codes meaning "the ontology cannot support extraction" --
#: empty ontology, zero class declarations, or requested types not in it.
_ONTOLOGY_FAILURE_CODES = frozenset({"ONTOLOGY_EMPTY", "TARGET_SCOPE_EMPTY"})


def _strict_chunk_failures() -> bool:
    """单片抽取失败是否直接失败整篇。

    默认 False（降级）：失败片以 error/warning 标记保留，其余片照常合并，
    避免"一片失败 → 整篇 500"；需要旧行为时设环境变量
    KG_EXTRACT_STRICT_CHUNK_FAILURES=1。
    """
    return str(os.environ.get("KG_EXTRACT_STRICT_CHUNK_FAILURES", "")).strip().lower() in {"1", "true", "yes"}


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
    if error_codes == {"LLM_TIMEOUT"}:
        return ErrorCode.LLM_TIMEOUT
    if error_codes and error_codes <= _UNUSABLE_RESPONSE_CODES:
        return ErrorCode.LLM_RESPONSE_INVALID
    if error_codes == {"LLM_REQUEST_FAILED"}:
        return ErrorCode.LLM_REQUEST_FAILED
    if error_codes and error_codes <= _ONTOLOGY_FAILURE_CODES:
        return ErrorCode.ONTOLOGY_INVALID
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
            if selection.get("force"):
                summary["force"] = True
        if metadata.get("fallback_from"):
            fallback = metadata["fallback_from"]
            summary["fallback"] = {
                "from_engine": fallback.get("engine"),
                "reason": fallback.get("error"),
            }
        if metadata.get("encoding"):
            summary["encoding"] = metadata["encoding"]
        if metadata.get("sheets"):
            summary["sheets"] = metadata["sheets"]
        if metadata.get("table_semantics"):
            summary["table_semantics"] = metadata["table_semantics"]
        return summary

    def resolve_engine_plan(self, file_name: str, content: bytes, config: ParserConfig) -> dict[str, Any] | None:
        """Resolve the actual parser once before queue fan-out."""
        selector = getattr(self.processor, "select_engine", None)
        if not callable(selector):
            return None
        safe_name = Path(file_name or "upload.bin").name
        suffix = Path(safe_name).suffix.lower()
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                handle.write(content)
                temp_path = handle.name
            with self._lock:
                engine, selection = selector(
                    temp_path,
                    requested=config.engine,
                    force=config.force,
                )
            return {"engine": engine, "selection": selection}
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    def parse(
        self,
        file_name: str,
        content: bytes,
        config: ParserConfig,
        *,
        page_range: tuple[int, int] | None = None,
        shard_index: int | None = None,
        total_shards: int | None = None,
        execution_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
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
                    force=config.force,
                    lang=config.mineru_options.language,
                    ocr_mode=config.mineru_options.ocr_mode,
                    table_enable=config.mineru_options.table_enable,
                    formula_enable=config.mineru_options.formula_enable,
                    start_page_id=page_range[0] if page_range else 0,
                    end_page_id=page_range[1] if page_range else None,
                    selected_engine=(execution_plan or {}).get("engine"),
                    engine_selection=(execution_plan or {}).get("selection"),
                    keep_page_edges=config.keep_page_edges,
                    page_edge_min_chars=config.page_edge_min_chars,
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
            empty_content = not markdown.strip()
            if empty_content and page_range is None:
                raise CapabilityError(ErrorCode.DOCUMENT_CONTENT_EMPTY, "文档解析结果为空", "document_parse")
            data = {
                "file_name": safe_name,
                "file_size": len(content),
                "file_type": suffix.lstrip("."),
                "markdown": markdown,
                "content_length": len(markdown),
                "parser": self.parser_summary(result.get("metadata") or {}),
            }
            if page_range is not None:
                data["page_range"] = {
                    "start_page_id": page_range[0],
                    "end_page_id": page_range[1],
                    "shard_index": shard_index,
                    "total_shards": total_shards,
                }
                data["empty"] = empty_content
            return data
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
        chunk_metadata: list[dict[str, Any]] = []
        for item in items:
            metadata = dict(getattr(item, "metadata", {}) or {})
            if isinstance(item, GraphExtractionItem):
                # API callers may attach provenance for round-tripping, but it
                # must not become a trusted document-routing signal.
                metadata["signal_source"] = "external"
            chunk_metadata.append(metadata)
        results = extract_chunks(
            ontology=ontology,
            # Chunk text is passed through verbatim: Knowledge Extract V2 reads
            # Markdown structure (numbered headings) to supplement entities, and
            # md_to_text strips the '#' markers it depends on.
            chunks=[item.text for item in items],
            model=model,
            base_url=base_url,
            ranges=[getattr(item, "ranges", None) for item in items],
            metadata=chunk_metadata,
        )
        for position, (source, result) in enumerate(zip(items, results)):
            result["index"] = position
            result["text"] = source.text
            ranges = getattr(source, "ranges", None)
            result["ranges"] = list(ranges) if ranges is not None else None
            result_metadata = dict(result.get("metadata") or {})
            result_metadata.update(chunk_metadata[position])
            result["metadata"] = result_metadata
        failed = [item for item in results if item.get("error")]
        if failed:
            failed_items = [
                {
                    "position": item["index"],
                    "error_code": item.get("error_code"),
                    "info": item.get("error"),
                    "attempts": item.get("attempts", []),
                    "raw_response": str(item.get("raw_response", ""))[:2000],
                }
                for item in failed
            ]
            all_failed = len(failed) == len(results)
            if all_failed or _strict_chunk_failures():
                raise CapabilityError(
                    _extraction_error(failed, len(results)),
                    "全部文本切片知识抽取失败" if all_failed else "部分文本切片知识抽取失败",
                    "knowledge_extract",
                    {"failed_count": len(failed), "failed_items": failed_items},
                )
            # 降级：单片失败不再作废整篇。失败片保留在 items 里（带 error 标记），
            # 其余片照常合并出图谱，并把失败明细落到日志，便于定位。
            logger.error("[算法能力] 部分文本切片知识抽取失败，已降级继续: %s", failed_items)
            for item in failed:
                item.setdefault("graph", {"nodes": [], "relationships": []})
                item.setdefault("warning", f"chunk 抽取失败: {item.get('error_code') or item.get('error')}")
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
        quality = dict(results[0].get("quality") or {}) if results else {
            "status": "succeeded",
            "error_code": None,
            "verification": {},
            "graph_publishable": True,
            "repair_stop_reason": None,
        }
        if failed:
            quality["status"] = "partial"
            quality["error_code"] = _extraction_error(failed, len(results)).name
            quality["failed_chunk_count"] = len(failed)
        if merge:
            merged_graph = deduplicate_chunk_graphs(results)
            graph_publishable = bool(quality.get("graph_publishable", True))
            return GraphExtractionBatch(
                items=results,
                attempts=attempts,
                scheduler=scheduler,
                graph=(
                    merged_graph
                    if graph_publishable
                    else {"nodes": [], "relationships": []}
                ),
                quality=quality,
                isolated_graph=None if graph_publishable else merged_graph,
            )
        graph_publishable = bool(quality.get("graph_publishable", True))
        extracted_graphs = [item["graph"] for item in results]
        return GraphExtractionBatch(
            items=results,
            attempts=attempts,
            scheduler=scheduler,
            graphs=(
                extracted_graphs
                if graph_publishable
                else [{"nodes": [], "relationships": []} for _ in extracted_graphs]
            ),
            quality=quality,
            isolated_graphs=None if graph_publishable else extracted_graphs,
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
