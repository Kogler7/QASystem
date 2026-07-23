from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from backend.app.responses import ErrorCode, deprecated_info, error_response, payload
from backend.app.schemas import (
    ChunkRequest,
    GraphEmbeddingRequest,
    GraphExtractionRequest,
    GraphMergeRequest,
    ParserConfig,
    TextEmbeddingRequest,
)
from backend.app.services.algorithm_capabilities import (
    CapabilityError,
    document_chunking_service,
    document_parsing_service,
    embedding_service,
    graph_extraction_service,
    graph_merge_service,
)


router = APIRouter(tags=["algorithm-capabilities"])


def _capability_error(exc: CapabilityError) -> JSONResponse:
    return error_response(
        exc.code,
        exc.info,
        stage=exc.stage,
        details=exc.details,
    )


@router.post("/api/v1/documents/parse")
async def parse_document(
    file: UploadFile = File(...),
    options: str = Form("{}"),
) -> JSONResponse:
    try:
        config = ParserConfig.model_validate_json(options or "{}")
    except ValidationError as exc:
        return error_response(
            ErrorCode.INVALID_REQUEST,
            "options 无效",
            stage="validation",
            details={"errors": exc.errors(include_url=False, include_input=False)},
            status_code=400,
        )
    try:
        result = await run_in_threadpool(
            document_parsing_service.parse,
            file.filename or "upload.bin",
            await file.read(),
            config,
        )
        return JSONResponse(content=payload(result, "文档解析完成"))
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/api/v1/documents/chunk")
async def chunk_document(body: ChunkRequest) -> JSONResponse:
    try:
        data = await run_in_threadpool(document_chunking_service.chunk, body.markdown, body.options)
        return JSONResponse(content=payload(data, "文档切片完成"))
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/api/v1/graphs/extract")
async def extract_graphs(body: GraphExtractionRequest) -> JSONResponse:
    try:
        result = await run_in_threadpool(
            graph_extraction_service.extract,
            body.ontology,
            body.items,
            model=body.options.model,
            merge=body.options.merge,
        )
        return JSONResponse(content=payload(result.graph_data(), "分片图谱抽取完成"))
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/api/v1/embeddings/texts")
@router.post("/api/component/nl2cypher/text-to-vectors", deprecated=True)
async def embed_texts(http_request: Request, body: TextEmbeddingRequest) -> JSONResponse:
    try:
        data = await run_in_threadpool(embedding_service.texts, body.items)
        info = f"文本向量化完成，共 {data['count']} 个向量"
        if http_request.url.path != "/api/v1/embeddings/texts":
            info = deprecated_info(info, "/api/v1/embeddings/texts")
        return JSONResponse(content=payload(data, info))
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/api/v1/graphs/merge")
async def merge_graphs(body: GraphMergeRequest) -> JSONResponse:
    try:
        data = await run_in_threadpool(graph_merge_service, body.items)
        return JSONResponse(content=payload(data, "Graph 去重完成"))
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/api/v1/embeddings/graphs")
async def embed_graphs(body: GraphEmbeddingRequest) -> JSONResponse:
    try:
        data = await run_in_threadpool(embedding_service.graphs, body.items)
        return JSONResponse(content=payload(data, f"Graph 向量化完成，共 {data['count']} 个向量"))
    except CapabilityError as exc:
        return _capability_error(exc)
