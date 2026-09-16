from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from algorithm.knowledge_pipeline import generate_understanding, store_vectors, upload_file
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor
from external_services.application import get_application_service_url

from backend.app.responses import ErrorCode
from backend.app.schemas import PipelineOptions, TextChunk, TextEmbeddingItem
from backend.app.services.algorithm_capabilities import (
    CapabilityError,
    DocumentChunkingService,
    DocumentParsingService,
    EmbeddingService,
    GraphExtractionService,
)


@dataclass(slots=True)
class KnowledgeIngestionRequest:
    file_name: str
    content: bytes
    ontology: str
    options: PipelineOptions = field(default_factory=PipelineOptions)


@dataclass(slots=True)
class FileItem:
    file_name: str
    content: bytes


@dataclass(slots=True)
class BatchKnowledgeIngestionRequest:
    files: list[FileItem]
    ontology: str
    options: PipelineOptions = field(default_factory=PipelineOptions)


class PipelineError(Exception):
    def __init__(self, code: ErrorCode, info: str, stage: str, details: Any = None, partial_data: Any = None):
        super().__init__(info)
        self.code = code
        self.info = info
        self.stage = stage
        self.details = details
        self.partial_data = partial_data


def _ontology_digest(ontology: str) -> dict[str, Any]:
    """回报本次请求实际收到的本体，供前端确认参数是否送达、能否解析。

    前端选了本体却抽不出数据时，第一件要确认的就是"服务端到底收到了什么"。
    这里把原始文本、长度、指纹和解析结果一并带出；本体为空也如实回报，
    这样"没收到本体"和"收到了但解析不出类"能一眼区分开。
    """
    text = ontology or ""
    info: dict[str, Any] = {
        "received": bool(text.strip()),
        "length": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else "",
        "classes": [],
        "property_count": 0,
        "parse_error": None,
        "text": text,
    }
    if not text.strip():
        return info
    try:
        from algorithm.knowledge_extract_v2.ontology_schema import OntologySchema

        schema = OntologySchema.parse(text)
        info["classes"] = [item.identifier for item in schema.classes]
        info["property_count"] = sum(len(item.properties) for item in schema.classes)
    except Exception as exc:  # noqa: BLE001 - 回报解析失败本身就是这个字段的用途
        info["parse_error"] = f"{type(exc).__name__}: {exc}"
    return info


class KnowledgePrecipitationService:
    def __init__(
        self,
        processor_factory=DocumentProcessor,
        parsing: DocumentParsingService | None = None,
        chunking: DocumentChunkingService | None = None,
        extraction: GraphExtractionService | None = None,
        embeddings: EmbeddingService | None = None,
    ):
        self.parsing = parsing or DocumentParsingService(processor_factory)
        self.chunking = chunking or DocumentChunkingService()
        self.extraction = extraction or GraphExtractionService()
        self.embeddings = embeddings or EmbeddingService()

    @staticmethod
    def _parser_summary(metadata: dict[str, Any]) -> dict[str, Any]:
        return DocumentParsingService.parser_summary(metadata)

    @staticmethod
    def _upload_markdown(
        content: bytes,
        file_name: str,
        content_type: str,
        request: KnowledgeIngestionRequest,
        stage: str,
    ) -> dict[str, Any] | None:
        file_service_url = get_application_service_url()
        if not file_service_url:
            return None
        result = upload_file(
            content,
            file_name,
            content_type,
            file_service_url,
            request.options.storage.knowledge_space_id,
        )
        if not result.get("success"):
            raise PipelineError(ErrorCode.FILE_SERVICE_FAILED, f"{stage}产物存储失败", stage, result)
        normalized = {key: result.get(key) for key in ("id", "name", "path", "url")}
        if not normalized["id"]:
            raise PipelineError(ErrorCode.FILE_SERVICE_FAILED, "文件服务未返回文件 id", stage, result)
        return normalized

    def _run_pipeline(
        self,
        markdown: str,
        upload_stem: str,
        ontology: str,
        options: PipelineOptions,
        origin: dict[str, Any] | None,
        started: datetime,
        stats: dict[str, Any],
    ) -> dict[str, Any]:
        chunked = self.chunking.chunk(markdown, options.chunking)
        chunks = chunked["items"]

        # 兼容两种抽取模式：传元模型走本体图谱抽取；不传元模型走非结构化摘要（切片+概要，不产图谱）
        has_ontology = bool((ontology or "").strip())
        if has_ontology:
            extracted_batch = self.extraction.extract(
                ontology=ontology,
                items=[
                    TextChunk(text=item["text"], ranges=item["range"])
                    for item in chunks
                ],
                model=options.extraction.model,
                merge=options.extraction.merge,
            )
            extracted_items = extracted_batch.items
            extracted_result = extracted_batch.pipeline_data()
            final_graph = extracted_batch.graph
            final_graphs = extracted_batch.graphs
            node_count = (
                len(final_graph.get("nodes", []))
                if final_graph is not None
                else sum(len(graph.get("nodes", [])) for graph in final_graphs or [])
            )
            relationship_count = (
                len(final_graph.get("relationships", []))
                if final_graph is not None
                else sum(len(graph.get("relationships", [])) for graph in final_graphs or [])
            )
            stats["extraction"] = {
                "chunks_extracted": len(extracted_items),
                "merge": options.extraction.merge,
                "nodes_extracted": node_count,
                "relationships_extracted": relationship_count,
                "attempts": extracted_batch.attempts,
                "scheduler": extracted_batch.scheduler,
            }

            if options.storage.persist_graph:
                raise PipelineError(
                    ErrorCode.EXTERNAL_CAPABILITY_UNAVAILABLE,
                    "知识图谱写入接口尚未开通",
                    "graph_storage",
                    {"capability": "graph.write"},
                    {"artifacts": {"markdown": origin}} if origin is not None else None,
                )
        else:
            from algorithm.knowledge_extract.batch import summarize_chunks
            from algorithm.knowledge_extract_v2.config import get_llm_config as _get_llm_config

            summaries = summarize_chunks(
                [item["text"] for item in chunks],
                model=options.extraction.model,
            )
            scheduler_cfg = _get_llm_config()
            final_graph = None
            final_graphs = None
            node_count = 0
            relationship_count = 0
            extracted_result = {
                "mode": "unstructured",
                "chunks": [
                    {
                        "index": item["index"],
                        "text": item["text"],
                        "range": chunks[item["index"]]["range"],
                        "summary": item["summary"],
                    }
                    for item in summaries
                ],
            }
            stats["extraction"] = {
                "mode": "unstructured",
                "chunks_summarized": len(summaries),
                "attempts": [
                    {
                        "position": item["index"],
                        "error": item.get("error"),
                        "error_code": item.get("error_code"),
                        "attempts": item.get("attempts", []),
                    }
                    for item in summaries
                    if item.get("error") or item.get("attempts")
                ],
                "scheduler": {
                    "max_concurrency": scheduler_cfg["max_concurrency"],
                    "qps": scheduler_cfg["qps"],
                    "qpm": scheduler_cfg["qpm"],
                },
            }

        vector_ids = None
        vector_dimension = None
        if origin is not None:
            embedded = self.embeddings.texts(
                [TextEmbeddingItem(text=item["text"]) for item in chunks]
            )
            vector_items = [
                {"text": chunk["text"], "vector": embedded_item["vector"]}
                for chunk, embedded_item in zip(chunks, embedded["items"])
            ]
            vector_result = store_vectors(
                vector_items,
                get_application_service_url(),
                options.storage.knowledge_space_id,
                origin["id"],
            )
            if not vector_result.get("success"):
                raise PipelineError(
                    ErrorCode.VECTOR_STORE_FAILED,
                    "向量存储失败",
                    "vector_storage",
                    vector_result,
                    {"artifacts": {"markdown": origin}, "extraction": extracted_result},
                )
            vector_ids = vector_result.get("ids")
            inserted_count = vector_result.get("inserted_count")
            if not isinstance(vector_ids, list) or inserted_count != len(vector_items) or len(vector_ids) != len(vector_items):
                raise PipelineError(
                    ErrorCode.VECTOR_ID_COUNT_MISMATCH,
                    "向量存储返回的 ids 数量与输入不一致",
                    "vector_storage",
                    {
                        "input_count": len(vector_items),
                        "inserted_count": inserted_count,
                        "id_count": len(vector_ids) if isinstance(vector_ids, list) else None,
                    },
                    {"artifacts": {"markdown": origin}, "extraction": extracted_result},
                )
            vector_dimension = embedded["dimension"]
            stats["storage"]["vectors"] = {"inserted_count": inserted_count}

        stats["chunking"] = {
            "chunk_count": chunked["count"],
            "strategy": options.chunking.strategy,
            "size": options.chunking.size,
            "separators": options.chunking.separators,
            "vector_storage_enabled": origin is not None,
            "vector_dimension": vector_dimension,
            "vector_source": "text_chunk",
        }
        stats["total_time_seconds"] = round((datetime.now() - started).total_seconds(), 3)

        if not has_ontology:
            summary_text = f"共生成 {len(chunks)} 个切片及摘要。"
        elif final_graph is not None:
            summary_text = generate_understanding(final_graph)
        else:
            summary_text = f"共抽取 {len(final_graphs or [])} 个分片图谱，包含 {node_count} 个节点实例和 {relationship_count} 条关系实例。"

        return {
            "name": upload_stem,
            "summary": summary_text,
            "artifacts": (
                {"markdown": origin, "vector_ids": vector_ids}
                if origin is not None
                else None
            ),
            "ontology": _ontology_digest(ontology),
            "extraction": extracted_result,
            "stats": stats,
        }

    def process(self, request: KnowledgeIngestionRequest) -> dict[str, Any]:
        if not request.content:
            raise PipelineError(ErrorCode.INVALID_REQUEST, "上传文件不能为空", "validation")

        started = datetime.now()
        file_name = Path(request.file_name or "upload.bin").name
        stem = Path(file_name).stem
        suffix = Path(file_name).suffix.lower()
        stats: dict[str, Any] = {
            "document": {"file_name": file_name, "file_size": len(request.content), "file_type": suffix.lstrip(".")},
            "extraction": {},
            "chunking": {},
            "storage": {"enabled": bool(get_application_service_url())},
        }
        try:
            parsed = self.parsing.parse(file_name, request.content, request.options.parser)
            markdown = parsed["markdown"]
            stats["document"].update(
                {"content_length": parsed["content_length"], "parser": parsed["parser"]}
            )
            origin = self._upload_markdown(
                markdown.encode("utf-8"), f"{stem}.md", "text/markdown", request, "origin_storage"
            )
            if origin is not None:
                stats["storage"]["origin"] = origin

            return self._run_pipeline(
                markdown, stem, request.ontology, request.options, origin, started, stats,
            )
        except CapabilityError as exc:
            partial = None
            if stats["storage"].get("origin"):
                partial = {"artifacts": {"markdown": stats["storage"]["origin"]}}
            raise PipelineError(exc.code, exc.info, exc.stage, exc.details, partial) from exc

    def process_batch(self, request: BatchKnowledgeIngestionRequest) -> dict[str, Any]:
        if not request.files:
            raise PipelineError(ErrorCode.INVALID_REQUEST, "至少需要上传一个文件", "validation")

        started = datetime.now()
        file_parsed: list[tuple[str, dict[str, Any]]] = []

        max_workers = min(len(request.files), os.cpu_count() or 4)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_file = {
                pool.submit(self.parsing.parse, f.file_name, f.content, request.options.parser): f
                for f in request.files
            }
            for future in as_completed(future_to_file):
                f = future_to_file[future]
                try:
                    parsed = future.result()
                except CapabilityError as exc:
                    raise PipelineError(
                        exc.code, f"文件 {f.file_name} 解析失败: {exc.info}", exc.stage, exc.details
                    ) from exc
                file_parsed.append((f.file_name, parsed))

        file_parsed.sort(key=lambda x: x[0])

        name_to_size = {f.file_name: len(f.content) for f in request.files}
        combined_parts: list[str] = []
        file_stats: list[dict[str, Any]] = []
        for file_name, parsed in file_parsed:
            combined_parts.append(f"\n## 文件: {file_name}\n\n{parsed['markdown']}")
            file_stats.append({
                "file_name": file_name,
                "file_size": name_to_size.get(file_name, 0),
                "file_type": Path(file_name).suffix.lower().lstrip("."),
                "content_length": parsed["content_length"],
                "parser": parsed["parser"],
            })

        combined_markdown = "\n".join(combined_parts)
        combined_stem = Path(request.files[0].file_name).stem + f"_等{len(request.files)}个文件"
        stats: dict[str, Any] = {
            "documents": file_stats,
            "extraction": {},
            "chunking": {},
            "storage": {"enabled": bool(get_application_service_url())},
        }

        # Build a dummy single-file request for _upload_markdown compatibility
        dummy_request = KnowledgeIngestionRequest(
            file_name=f"{combined_stem}.md",
            content=combined_markdown.encode("utf-8"),
            ontology=request.ontology,
            options=request.options,
        )
        origin = self._upload_markdown(
            combined_markdown.encode("utf-8"),
            f"{combined_stem}.md",
            "text/markdown",
            dummy_request,
            "origin_storage",
        )
        if origin is not None:
            stats["storage"]["origin"] = origin

        result = self._run_pipeline(
            combined_markdown, combined_stem, request.ontology, request.options, origin, started, stats,
        )
        # Augment summary with file count
        fc = len(request.files)
        if result["summary"]:
            result["summary"] += f"，来源：{fc} 个文件"
        return result
