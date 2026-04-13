"""
知识抽取组件 API：进程内调用 algorithm/uie 算法。
符合 CONTRIBUTING：prefix 为 /api/component/knowledge-extract
"""
import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ....deps.auth import require_admin
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/component/knowledge-extract",
    tags=["component", "knowledge-extract"],
    dependencies=[Depends(require_admin)],
)


class ExtractRequestBody(BaseModel):
    main_object: str
    text: str
    use_templates: bool = True
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None


class StoreGraphRequestBody(BaseModel):
    graph: dict
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j2025"


@router.get("/health")
async def extract_health() -> dict[str, Any]:
    return service.health()


@router.post("/extract")
async def api_extract(body: ExtractRequestBody) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            service.extract,
            main_object=body.main_object,
            text=body.text,
            use_templates=body.use_templates,
            llm_base_url=body.llm_base_url,
            llm_model=body.llm_model,
            llm_api_key=body.llm_api_key,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("知识抽取失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/parse-chunked-json")
async def api_parse_chunked_json(
    file: UploadFile = File(...),
    main_object: str = Form("Term"),
    use_templates: bool = Form(True),
    llm_base_url: Optional[str] = Form(None),
    llm_model: Optional[str] = Form(None),
    llm_api_key: Optional[str] = Form(None),
) -> dict[str, Any]:
    """
    上传切块后的 JSON 文件（UTF-8），解析出文本片段后对每段执行知识抽取，合并为与 /extract 一致的 {raw, graph}。
    """
    if not file.filename or not str(file.filename).strip():
        raise HTTPException(status_code=400, detail="请选择 JSON 文件")
    try:
        raw_bytes = await file.read()
        result = await asyncio.to_thread(
            service.extract_from_chunked_json_file,
            raw_bytes,
            main_object.strip(),
            use_templates,
            llm_base_url,
            llm_model,
            llm_api_key,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("切块 JSON 解析抽取失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/store-graph")
async def api_store_graph(body: StoreGraphRequestBody) -> dict[str, Any]:
    try:
        await asyncio.to_thread(
            service.store_graph,
            graph_struct=body.graph,
            neo4j_uri=body.neo4j_uri,
            neo4j_user=body.neo4j_user,
            neo4j_password=body.neo4j_password,
        )
        return {"status": "ok", "message": "已写入 Neo4j"}
    except Exception as e:
        logger.exception("存储 Neo4j 失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
