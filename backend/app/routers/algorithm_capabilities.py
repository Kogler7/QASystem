from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from backend.app import observability
from backend.app.responses import ErrorCode, deprecated_info, error_response, payload
from backend.app.execution.api import ExecutionUnavailable, ManagedExecutionError, dispatch_operation, prefer_wait_seconds
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


def _capability_error(exc: CapabilityError, execution: dict | None = None) -> JSONResponse:
    return error_response(
        exc.code,
        exc.info,
        stage=exc.stage,
        details=exc.details,
        execution=execution,
    )


@router.post("/api/v1/documents/parse")
async def parse_document(
    http_request: Request,
    file: UploadFile = File(...),
    options: str = Form("{}"),
) -> JSONResponse:
    # 无执行上下文（同步等待）的 HTTP 调用在这里生成 request_id，贯穿日志与告警。
    with observability.request_id_scope():
        return await _parse_document(http_request, file, options)


async def _parse_document(http_request: Request, file: UploadFile, options: str) -> JSONResponse:
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
        result = await dispatch_operation(
            operation="document.parse",
            resource_class="cpu.document_orchestrator",
            payload={
                "file_name": file.filename or "upload.bin",
                "content": await file.read(),
                "config": config.model_dump(),
            },
            wait_seconds=prefer_wait_seconds(http_request.headers.get("Prefer")),
        )
        return JSONResponse(content=payload(result.data, "文档解析完成", execution=result.execution), status_code=result.status_code)
    except ExecutionUnavailable as exc:
        return error_response(ErrorCode.DEPENDENCY_UNAVAILABLE, "执行队列不可用", stage="execution", details=str(exc), status_code=503)
    except ManagedExecutionError as exc:
        if isinstance(exc.cause, CapabilityError):
            return _capability_error(exc.cause, exc.execution)
        return error_response(ErrorCode.INTERNAL_ERROR, "文档解析失败", stage="execution", details={"type": type(exc.cause).__name__, "message": str(exc.cause)}, execution=exc.execution)
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/api/v1/documents/chunk")
async def chunk_document(http_request: Request, body: ChunkRequest) -> JSONResponse:
    # 无执行上下文（同步等待）的 HTTP 调用在这里生成 request_id，贯穿日志与告警。
    with observability.request_id_scope():
        return await _chunk_document(http_request, body)


async def _chunk_document(http_request: Request, body: ChunkRequest) -> JSONResponse:
    try:
        result = await dispatch_operation(
            operation="document.chunk",
            resource_class="cpu.chunk",
            payload=body.model_dump(),
            wait_seconds=prefer_wait_seconds(http_request.headers.get("Prefer")),
        )
        return JSONResponse(content=payload(result.data, "文档切片完成", execution=result.execution), status_code=result.status_code)
    except ExecutionUnavailable as exc:
        return error_response(ErrorCode.DEPENDENCY_UNAVAILABLE, "执行队列不可用", stage="execution", details=str(exc), status_code=503)
    except ManagedExecutionError as exc:
        if isinstance(exc.cause, CapabilityError):
            return _capability_error(exc.cause, exc.execution)
        return error_response(ErrorCode.INTERNAL_ERROR, "文档切片失败", stage="execution", details={"type": type(exc.cause).__name__, "message": str(exc.cause)}, execution=exc.execution)
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/api/v1/graphs/extract")
async def extract_graphs(http_request: Request, body: GraphExtractionRequest) -> JSONResponse:
    # 无执行上下文（同步等待）的 HTTP 调用在这里生成 request_id，贯穿日志与告警。
    with observability.request_id_scope():
        return await _extract_graphs(http_request, body)


async def _extract_graphs(http_request: Request, body: GraphExtractionRequest) -> JSONResponse:
    try:
        result = await dispatch_operation(
            operation="graph.extract",
            resource_class="io.llm",
            payload=body.model_dump(),
            total_units=len(body.items),
            wait_seconds=prefer_wait_seconds(http_request.headers.get("Prefer")),
        )
        return JSONResponse(content=payload(result.data, "分片图谱抽取完成", execution=result.execution), status_code=result.status_code)
    except ExecutionUnavailable as exc:
        return error_response(ErrorCode.DEPENDENCY_UNAVAILABLE, "执行队列不可用", stage="execution", details=str(exc), status_code=503)
    except ManagedExecutionError as exc:
        if isinstance(exc.cause, CapabilityError):
            return _capability_error(exc.cause, exc.execution)
        return error_response(ErrorCode.INTERNAL_ERROR, "图谱抽取失败", stage="execution", details={"type": type(exc.cause).__name__, "message": str(exc.cause)}, execution=exc.execution)
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/api/v1/embeddings/texts")
@router.post("/api/component/nl2cypher/text-to-vectors", deprecated=True)
async def embed_texts(http_request: Request, body: TextEmbeddingRequest) -> JSONResponse:
    # 无执行上下文（同步等待）的 HTTP 调用在这里生成 request_id，贯穿日志与告警。
    with observability.request_id_scope():
        return await _embed_texts(http_request, body)


async def _embed_texts(http_request: Request, body: TextEmbeddingRequest) -> JSONResponse:
    try:
        result = await dispatch_operation(
            operation="embedding.texts",
            resource_class="gpu.embedding",
            payload=body.model_dump(),
            total_units=len(body.items),
            wait_seconds=prefer_wait_seconds(http_request.headers.get("Prefer")),
        )
        info = f"文本向量化完成，共 {result.data['count']} 个向量" if isinstance(result.data, dict) and "count" in result.data else "文本向量化任务已提交"
        if http_request.url.path != "/api/v1/embeddings/texts":
            info = deprecated_info(info, "/api/v1/embeddings/texts")
        return JSONResponse(content=payload(result.data, info, execution=result.execution), status_code=result.status_code)
    except ExecutionUnavailable as exc:
        return error_response(ErrorCode.DEPENDENCY_UNAVAILABLE, "执行队列不可用", stage="execution", details=str(exc), status_code=503)
    except ManagedExecutionError as exc:
        if isinstance(exc.cause, CapabilityError):
            return _capability_error(exc.cause, exc.execution)
        return error_response(ErrorCode.INTERNAL_ERROR, "文本向量化失败", stage="execution", details={"type": type(exc.cause).__name__, "message": str(exc.cause)}, execution=exc.execution)
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/api/v1/graphs/merge")
async def merge_graphs(http_request: Request, body: GraphMergeRequest) -> JSONResponse:
    # 无执行上下文（同步等待）的 HTTP 调用在这里生成 request_id，贯穿日志与告警。
    with observability.request_id_scope():
        return await _merge_graphs(http_request, body)


async def _merge_graphs(http_request: Request, body: GraphMergeRequest) -> JSONResponse:
    try:
        result = await dispatch_operation(
            operation="graph.merge",
            resource_class="cpu.graph",
            payload=body.model_dump(),
            total_units=len(body.items),
            wait_seconds=prefer_wait_seconds(http_request.headers.get("Prefer")),
        )
        return JSONResponse(content=payload(result.data, "Graph 去重完成", execution=result.execution), status_code=result.status_code)
    except ExecutionUnavailable as exc:
        return error_response(ErrorCode.DEPENDENCY_UNAVAILABLE, "执行队列不可用", stage="execution", details=str(exc), status_code=503)
    except ManagedExecutionError as exc:
        if isinstance(exc.cause, CapabilityError):
            return _capability_error(exc.cause, exc.execution)
        return error_response(ErrorCode.INTERNAL_ERROR, "Graph 去重失败", stage="execution", details={"type": type(exc.cause).__name__, "message": str(exc.cause)}, execution=exc.execution)
    except CapabilityError as exc:
        return _capability_error(exc)


@router.post("/api/v1/embeddings/graphs")
async def embed_graphs(http_request: Request, body: GraphEmbeddingRequest) -> JSONResponse:
    # 无执行上下文（同步等待）的 HTTP 调用在这里生成 request_id，贯穿日志与告警。
    with observability.request_id_scope():
        return await _embed_graphs(http_request, body)


async def _embed_graphs(http_request: Request, body: GraphEmbeddingRequest) -> JSONResponse:
    try:
        result = await dispatch_operation(
            operation="embedding.graphs",
            resource_class="gpu.embedding",
            payload=body.model_dump(),
            total_units=len(body.items),
            wait_seconds=prefer_wait_seconds(http_request.headers.get("Prefer")),
        )
        info = f"Graph 向量化完成，共 {result.data['count']} 个向量" if isinstance(result.data, dict) and "count" in result.data else "Graph 向量化任务已提交"
        return JSONResponse(content=payload(result.data, info, execution=result.execution), status_code=result.status_code)
    except ExecutionUnavailable as exc:
        return error_response(ErrorCode.DEPENDENCY_UNAVAILABLE, "执行队列不可用", stage="execution", details=str(exc), status_code=503)
    except ManagedExecutionError as exc:
        if isinstance(exc.cause, CapabilityError):
            return _capability_error(exc.cause, exc.execution)
        return error_response(ErrorCode.INTERNAL_ERROR, "Graph 向量化失败", stage="execution", details={"type": type(exc.cause).__name__, "message": str(exc.cause)}, execution=exc.execution)
    except CapabilityError as exc:
        return _capability_error(exc)
