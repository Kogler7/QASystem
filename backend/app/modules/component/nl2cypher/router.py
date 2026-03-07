"""
向量转化存储与自然语言转 Cypher 组件 API：进程内调用 algorithm/NL_to_cypher。
"""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/component/nl2cypher", tags=["component", "nl2cypher"])


class GenerateRequestBody(BaseModel):
    question: str
    graph_schema: str = ""


@router.get("/health")
async def nl2cypher_health() -> dict[str, Any]:
    return service.health()


@router.post("/generate")
async def generate_cypher(body: GenerateRequestBody) -> dict[str, Any]:
    if not (body.question or "").strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        result = await asyncio.to_thread(
            service.generate,
            question=body.question.strip(),
            graph_schema=(body.graph_schema or "").strip(),
        )
        return result
    except Exception as e:
        logger.exception("自然语言转 Cypher 失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
