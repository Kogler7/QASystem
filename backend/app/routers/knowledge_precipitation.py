from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from algorithm.knowledge_pipeline.artifacts import resolve_local_artifact
from backend.app.responses import ErrorCode, error_response, payload
from backend.app.services.knowledge_precipitation import (
    KnowledgePrecipitationService,
    ParserConfig,
    PipelineError,
    PrecipitationRequest,
)


router = APIRouter(tags=["knowledge-precipitation"])
service = KnowledgePrecipitationService()


@router.post("/api/knowledge/precipitation")
async def knowledge_precipitation(
    file: UploadFile = File(...),
    ontology: str = Form(...),
    parser_config: str = Form(
        "{}",
        description="JSON 格式解析配置，包含 engine 和 mineru_options",
    ),
    chunk_character: Optional[str] = Form(None),
    chunk_size: int = Form(500),
    chunk_type: int = Form(0),
    similarity: float = Form(0.6),
    save_to_db: bool = Form(False),
    model: Optional[str] = Form(None),
    base_url: Optional[str] = Form(None),
    knowledge_space_id: Optional[str] = Form(None),
    file_service_url: Optional[str] = Form(None),
) -> JSONResponse:
    try:
        parser = ParserConfig.model_validate_json(parser_config or "{}")
    except ValidationError as exc:
        return error_response(
            ErrorCode.INVALID_REQUEST,
            "parser_config 无效",
            stage="validation",
            details={"errors": exc.errors(include_url=False, include_input=False)},
            status_code=400,
        )

    request = PrecipitationRequest(
        file_name=file.filename or "upload.bin",
        content=await file.read(),
        ontology=ontology,
        parser=parser,
        chunk_character=chunk_character,
        chunk_size=chunk_size,
        chunk_type=chunk_type,
        similarity=similarity,
        save_to_db=save_to_db,
        model=model,
        base_url=base_url,
        knowledge_space_id=knowledge_space_id,
        file_service_url=file_service_url,
    )
    try:
        data = await run_in_threadpool(service.process, request)
        return JSONResponse(content=payload(data, "知识沉淀完成"))
    except PipelineError as exc:
        return error_response(
            exc.code,
            exc.message,
            stage=exc.stage,
            details=exc.details,
            data=exc.partial_data,
            status_code=400 if exc.code == ErrorCode.INVALID_REQUEST else 500,
        )
    except Exception as exc:
        return error_response(
            ErrorCode.INTERNAL_ERROR,
            "知识沉淀处理异常",
            stage="pipeline",
            details={"type": type(exc).__name__, "message": str(exc)},
            status_code=500,
        )


@router.get("/file/{file_id}/{file_name}", include_in_schema=False)
async def get_local_artifact(file_id: str, file_name: str) -> FileResponse:
    path = resolve_local_artifact(file_id, file_name)
    if path is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path, filename=path.name)
