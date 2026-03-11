"""
知识抽取组件 API：进程内调用 algorithm/uie 算法。
符合 CONTRIBUTING：prefix 为 /api/component/knowledge-extract
"""
import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/component/knowledge-extract", tags=["component", "knowledge-extract"])


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
