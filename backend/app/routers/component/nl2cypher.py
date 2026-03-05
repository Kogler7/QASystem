"""
向量转化存储与自然语言转 Cypher 组件 API：进程内调用 algorithm/NL_to_cypher，不启动独立服务。
"""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...services import nl2cypher_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/component/nl2cypher", tags=["component", "nl2cypher"])


class GenerateRequestBody(BaseModel):
    question: str
    graph_schema: str = ""


@router.get("/health")
async def nl2cypher_health() -> dict[str, Any]:
    """检查自然语言转 Cypher 模块是否可加载。"""
    return nl2cypher_service.health()


@router.post("/generate")
async def generate_cypher(body: GenerateRequestBody) -> dict[str, Any]:
    """根据自然语言问题与图谱 Schema 生成 Cypher 查询。"""
    if not (body.question or "").strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        result = await asyncio.to_thread(
            nl2cypher_service.generate,
            question=body.question.strip(),
            graph_schema=(body.graph_schema or "").strip(),
        )
        return result
    except Exception as e:
        logger.exception("自然语言转 Cypher 失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
