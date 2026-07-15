from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from algorithm.knowledge_extract import store_extraction_to_neo4j
from algorithm.knowledge_pipeline import generate_understanding, store_vectors, upload_file
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor

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


class PipelineError(Exception):
    def __init__(self, code: ErrorCode, info: str, stage: str, details: Any = None, partial_data: Any = None):
        super().__init__(info)
        self.code = code
        self.info = info
        self.stage = stage
        self.details = details
        self.partial_data = partial_data


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
        file_service_url = os.getenv("FILE_SERVICE_URL", "").strip()
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

    def process(self, request: KnowledgeIngestionRequest) -> dict[str, Any]:
        if not request.ontology.strip():
            raise PipelineError(ErrorCode.ONTOLOGY_INVALID, "ontology 不能为空", "validation")
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
            "storage": {"enabled": bool(os.getenv("FILE_SERVICE_URL", "").strip())},
        }
        try:
            parsed = self.parsing.parse(file_name, request.content, request.options.parser)
            markdown = parsed["markdown"]
            parser_status = parsed["parser"]
            stats["document"].update(
                {
                    "content_length": parsed["content_length"],
                    "parser": parser_status,
                }
            )

            origin = self._upload_markdown(
                markdown.encode("utf-8"), f"{stem}.md", "text/markdown", request, "origin_storage"
            )
            if origin is not None:
                stats["storage"]["origin"] = origin

            chunked = self.chunking.chunk(markdown, request.options.chunking)
            chunks = chunked["items"]
            extracted_batch = self.extraction.extract(
                ontology=request.ontology,
                items=[
                    TextChunk(
                        text=item["text"],
                        ranges=item["range"],
                    )
                    for item in chunks
                ],
                model=request.options.extraction.model,
                merge=request.options.extraction.merge,
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
                "merge": request.options.extraction.merge,
                "nodes_extracted": node_count,
                "relationships_extracted": relationship_count,
                "attempts": extracted_batch.attempts,
                "scheduler": extracted_batch.scheduler,
            }

            if request.options.storage.persist_graph:
                if final_graph is not None:
                    database_result = store_extraction_to_neo4j(graph=final_graph)
                else:
                    database_items = [
                        {"position": position, **store_extraction_to_neo4j(graph=item["graph"])}
                        for position, item in enumerate(extracted_items)
                    ]
                    database_result = {
                        "success": all(item.get("success") for item in database_items),
                        "items": database_items,
                    }
                stats["extraction"]["database"] = database_result
                if not database_result.get("success"):
                    raise PipelineError(
                        ErrorCode.GRAPH_STORAGE_FAILED,
                        "知识图谱入库失败",
                        "graph_storage",
                        database_result,
                        {"artifacts": {"markdown": origin}} if origin is not None else None,
                    )

            vector_ids = None
            vector_dimension = None
            if origin is not None:
                embedded = self.embeddings.texts(
                    [TextEmbeddingItem(text=item["text"]) for item in chunks]
                )
                vector_items = [
                    {
                        "text": chunk["text"],
                        "vector": embedded_item["vector"],
                    }
                    for chunk, embedded_item in zip(chunks, embedded["items"])
                ]
                vector_result = store_vectors(
                    vector_items,
                    os.getenv("FILE_SERVICE_URL", "").strip(),
                    request.options.storage.knowledge_space_id,
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
                "strategy": request.options.chunking.strategy,
                "size": request.options.chunking.size,
                "separators": request.options.chunking.separators,
                "vector_storage_enabled": origin is not None,
                "vector_dimension": vector_dimension,
                "vector_source": "text_chunk",
            }
            stats["total_time_seconds"] = round((datetime.now() - started).total_seconds(), 3)

            result = {
                "name": stem,
                "summary": (
                    generate_understanding(final_graph)
                    if final_graph is not None
                    else f"共抽取 {len(final_graphs or [])} 个分片图谱，包含 {node_count} 个节点实例和 {relationship_count} 条关系实例。"
                ),
                "artifacts": (
                    {"markdown": origin, "vector_ids": vector_ids}
                    if origin is not None
                    else None
                ),
                "extraction": extracted_result,
                "stats": stats,
            }
            return result
        except CapabilityError as exc:
            partial = None
            if stats["storage"].get("origin"):
                partial = {"artifacts": {"markdown": stats["storage"]["origin"]}}
            raise PipelineError(exc.code, exc.info, exc.stage, exc.details, partial) from exc
