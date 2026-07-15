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


@router.post(V1_PATH)
@router.post("/api/knowledge/precipitation", deprecated=True)
async def knowledge_ingestion(
    http_request: Request,
    file: UploadFile = File(...),
    ontology: str = Form(...),
    options: str = Form("{}"),
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

    request = KnowledgeIngestionRequest(
        file_name=file.filename or "upload.bin",
        content=await file.read(),
        ontology=ontology,
        options=config,
    )
    try:
        data = await run_in_threadpool(service.process, request)
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
