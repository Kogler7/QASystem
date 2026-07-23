from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

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
        if len(collected) == 1 and files is None:
            name, content = collected[0]
            request = KnowledgeIngestionRequest(
                file_name=name, content=content, ontology=ontology, options=config,
            )
            data = await run_in_threadpool(service.process, request)
            return JSONResponse(content=payload(data, _info("知识沉淀完成", deprecated)))
        else:
            file_items = [FileItem(file_name=name, content=content) for name, content in collected]
            request = BatchKnowledgeIngestionRequest(
                files=file_items, ontology=ontology, options=config,
            )
            data = await run_in_threadpool(service.process_batch, request)
            return JSONResponse(content=payload(data, _info("知识沉淀完成", deprecated)))
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
