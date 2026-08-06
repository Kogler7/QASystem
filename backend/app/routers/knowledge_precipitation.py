from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from backend.app.execution.api import ExecutionUnavailable, ManagedExecutionError, dispatch_operation, prefer_wait_seconds
from backend.app.responses import ErrorCode, deprecated_info, error_response, payload
from backend.app.schemas import PipelineOptions
from backend.app.services.algorithm_capabilities import (
    document_chunking_service,
    document_parsing_service,
    embedding_service,
    graph_extraction_service,
)
from backend.app.services.knowledge_precipitation import (
    BatchKnowledgeIngestionRequest,
    FileItem,
    KnowledgeIngestionRequest,
    KnowledgePrecipitationService,
    PipelineError,
)


router = APIRouter(tags=["knowledge-ingestion"])
service = KnowledgePrecipitationService(
    parsing=document_parsing_service,
    chunking=document_chunking_service,
    extraction=graph_extraction_service,
    embeddings=embedding_service,
)
V1_PATH = "/api/v1/pipelines/knowledge-ingestion"


def _info(info: str, deprecated: bool) -> str:
    return deprecated_info(info, V1_PATH) if deprecated else info


def _collect_files(
    file: UploadFile | None,
    files: list[UploadFile] | None,
) -> list[tuple[str, bytes]]:
    result: list[tuple[str, bytes]] = []
    if file is not None and file.filename:
        result.append((file.filename, file.file.read()))
    if files:
        for f in files:
            if f.filename:
                result.append((f.filename, f.file.read()))
    return result


@router.post(V1_PATH)
@router.post("/api/knowledge/precipitation", deprecated=True)
async def knowledge_ingestion(
    http_request: Request,
    ontology: str = Form(...),
    options: str = Form("{}"),
    file: UploadFile | None = File(None),
    files: list[UploadFile] | None = File(None),
) -> JSONResponse:
    deprecated = http_request.url.path != V1_PATH
    try:
        config = PipelineOptions.model_validate_json(options or "{}")
    except ValidationError as exc:
        return error_response(
            ErrorCode.INVALID_REQUEST,
            _info("options 无效", deprecated),
            stage="validation",
            details={"errors": exc.errors(include_url=False, include_input=False)},
            status_code=400,
        )

    collected = [(name, content) for name, content in _collect_files(file, files)]
    if not collected:
        return error_response(
            ErrorCode.INVALID_REQUEST,
            _info("至少需要上传一个文件（file 或 files）", deprecated),
            stage="validation",
            status_code=400,
        )

    try:
        result = await dispatch_operation(
            operation="knowledge.ingestion",
            resource_class="workflow.knowledge",
            payload={
                "ontology": ontology,
                "options": config.model_dump(),
                "files": [
                    {"file_name": name, "content": content}
                    for name, content in collected
                ],
                "batch": not (len(collected) == 1 and files is None),
            },
            total_units=len(collected),
            wait_seconds=prefer_wait_seconds(http_request.headers.get("Prefer")),
        )
        return JSONResponse(
            content=payload(result.data, _info("知识沉淀完成", deprecated), execution=result.execution),
            status_code=result.status_code,
        )
    except ExecutionUnavailable as exc:
        return error_response(ErrorCode.DEPENDENCY_UNAVAILABLE, _info("执行队列不可用", deprecated), stage="execution", details=str(exc), status_code=503)
    except ManagedExecutionError as exc:
        cause = exc.cause
        if isinstance(cause, PipelineError):
            return error_response(
                cause.code,
                _info(cause.info, deprecated),
                stage=cause.stage,
                details=cause.details,
                data=cause.partial_data,
                execution=exc.execution,
            )
        return error_response(
            ErrorCode.INTERNAL_ERROR,
            _info("知识沉淀处理异常", deprecated),
            stage="execution",
            details={"type": type(cause).__name__, "message": str(cause)},
            execution=exc.execution,
        )
    except PipelineError as exc:
        return error_response(
            exc.code,
            _info(exc.info, deprecated),
            stage=exc.stage,
            details=exc.details,
            data=exc.partial_data,
        )
    except Exception as exc:
        return error_response(
            ErrorCode.INTERNAL_ERROR,
            _info("知识沉淀处理异常", deprecated),
            stage="pipeline",
            details={"type": type(exc).__name__, "message": str(exc)},
            status_code=500,
        )
