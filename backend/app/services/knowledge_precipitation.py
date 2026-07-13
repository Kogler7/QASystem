from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from algorithm.NL_to_cypher.vector import batch_text_to_vectors
from algorithm.knowledge_extract import extract_v2, store_extraction_to_neo4j
from algorithm.knowledge_pipeline import (
    build_graphs_response,
    chunk_document,
    generate_understanding,
    md_to_text,
    normalize_document_content,
    parse_chunk_characters,
    store_artifact,
)
from algorithm.preproc.preproc_src.main_processor import DocumentProcessor

from backend.app.responses import ErrorCode


@dataclass(slots=True)
class PrecipitationRequest:
    file_name: str
    content: bytes
    ontology: str
    lang: str = "ch"
    parser_engine: str | None = None
    chunk_character: str | None = None
    chunk_size: int = 500
    chunk_type: int = 0
    similarity: float = 0.6
    save_to_db: bool = False
    model: str | None = None
    base_url: str | None = None
    knowledge_space_id: str | None = None
    file_service_url: str | None = None


class PipelineError(Exception):
    def __init__(self, code: ErrorCode, message: str, stage: str, details: Any = None, partial_data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.details = details
        self.partial_data = partial_data


class KnowledgePrecipitationService:
    def __init__(self, processor_factory=DocumentProcessor):
        self.processor_factory = processor_factory

    @staticmethod
    def _store_or_raise(
        content: bytes,
        file_name: str,
        content_type: str,
        request: PrecipitationRequest,
        stage: str,
    ) -> dict[str, Any]:
        result = store_artifact(
            content,
            file_name,
            content_type,
            request.file_service_url or os.getenv("FILE_SERVICE_URL", "").strip(),
            request.knowledge_space_id,
        )
        if not result.get("success"):
            raise PipelineError(
                ErrorCode.STORAGE_FAILED,
                f"{stage}产物存储失败",
                stage,
                result,
            )
        return result

    def process(self, request: PrecipitationRequest) -> dict[str, Any]:
        if not request.ontology.strip():
            raise PipelineError(ErrorCode.INVALID_REQUEST, "ontology 不能为空", "validation")
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
            "storage": {},
        }
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
                handle.write(request.content)
                temp_path = handle.name

            parse_result = self.processor_factory().process_file(
                temp_path,
                model_name=request.parser_engine,
                lang=request.lang,
            )
            if not parse_result.get("success"):
                raise PipelineError(
                    ErrorCode.DOCUMENT_PARSE_FAILED,
                    "文档解析失败",
                    "document_parse",
                    parse_result,
                )
            markdown = normalize_document_content(parse_result.get("content"))
            if not markdown.strip():
                raise PipelineError(ErrorCode.DOCUMENT_PARSE_FAILED, "文档解析结果为空", "document_parse")
            stats["document"]["content_length"] = len(markdown)
            stats["document"]["parser"] = parse_result.get("metadata", {})

            origin = self._store_or_raise(
                markdown.encode("utf-8"), f"{stem}.md", "text/markdown", request, "origin_storage"
            )
            stats["storage"]["origin"] = origin

            extraction = extract_v2(
                ontology=request.ontology,
                text=md_to_text(markdown),
                model=request.model,
                base_url=request.base_url,
            )
            if extraction.get("_error") or extraction.get("error"):
                raise PipelineError(
                    ErrorCode.KNOWLEDGE_EXTRACT_FAILED,
                    "知识抽取失败",
                    "knowledge_extract",
                    {
                        "error_code": extraction.get("error_code"),
                        "message": extraction.get("_error") or extraction.get("error"),
                        "attempts": extraction.get("attempts", []),
                        "raw_response": extraction.get("_raw_response", ""),
                    },
                    {"origin_url": origin.get("file_url")},
                )
            graph = extraction.get("graph") or {"nodes": [], "relationships": []}
            stats["extraction"] = {
                "nodes_extracted": len(graph.get("nodes", [])),
                "relationships_extracted": len(graph.get("relationships", [])),
                "llm_attempts": extraction.get("attempts", []),
            }

            if request.save_to_db:
                database_result = store_extraction_to_neo4j(graph=graph)
                stats["extraction"]["database"] = database_result
                if not database_result.get("success"):
                    raise PipelineError(
                        ErrorCode.STORAGE_FAILED,
                        "知识图谱入库失败",
                        "graph_storage",
                        database_result,
                        {"origin_url": origin.get("file_url"), "graph": graph},
                    )

            extraction_artifact = {
                "raw": extraction.get("raw", {}),
                "graph": graph,
            }
            extracted = self._store_or_raise(
                json.dumps(extraction_artifact, ensure_ascii=False, indent=2).encode("utf-8"),
                f"{stem}_extract.json",
                "application/json",
                request,
                "extract_storage",
            )
            stats["storage"]["extract"] = extracted

            try:
                separators = parse_chunk_characters(request.chunk_character)
                chunks = chunk_document(
                    markdown,
                    request.chunk_type,
                    request.chunk_size,
                    separators,
                    request.similarity,
                )
            except ValueError as exc:
                raise PipelineError(ErrorCode.CHUNK_FAILED, str(exc), "chunking") from exc
            if not chunks:
                raise PipelineError(ErrorCode.CHUNK_FAILED, "文档切片结果为空", "chunking")

            try:
                vectors = batch_text_to_vectors(chunks)
            except Exception as exc:
                raise PipelineError(ErrorCode.VECTORIZE_FAILED, "向量化失败", "vectorization", str(exc)) from exc
            stats["chunking"] = {
                "chunk_count": len(chunks),
                "chunk_type": request.chunk_type,
                "chunk_size": request.chunk_size,
                "separators": separators,
                "vector_dimension": len(vectors[0]) if vectors else 0,
            }
            vector_artifact = {
                "count": len(vectors),
                "dimension": stats["chunking"]["vector_dimension"],
                "items": [
                    {"index": index, "text": text, "vector": vector}
                    for index, (text, vector) in enumerate(zip(chunks, vectors))
                ],
            }
            vectorized = self._store_or_raise(
                json.dumps(vector_artifact, ensure_ascii=False).encode("utf-8"),
                f"{stem}_vectors.json",
                "application/json",
                request,
                "vector_storage",
            )
            stats["storage"]["vector"] = vectorized
            stats["total_time_seconds"] = round((datetime.now() - started).total_seconds(), 3)

            return {
                "name": stem,
                "understanding": generate_understanding(graph),
                "origin_url": origin.get("file_url"),
                "vector_url": vectorized.get("file_url"),
                "extract_url": extracted.get("file_url"),
                "graphs": build_graphs_response(graph),
                "stats": stats,
            }
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
