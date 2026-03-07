"""
答案生成组件 API：进程内调用 algorithm/ans（Neo4j 知识图谱问答）。
"""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/component/answer-generation", tags=["component", "answer-generation"])


class AskRequestBody(BaseModel):
    question: str
    detailed: bool = False


class AskVisualizeRequestBody(BaseModel):
    question: str


@router.get("/health")
async def answer_generation_health() -> dict[str, Any]:
    return service.health()


@router.post("/ask")
async def ask(body: AskRequestBody) -> dict[str, Any]:
    if not (body.question or "").strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        result = await asyncio.to_thread(service.ask, question=body.question.strip(), detailed=body.detailed)
        return result
    except Exception as e:
        logger.exception("答案生成失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/ask/visualize")
async def ask_visualize(body: AskVisualizeRequestBody) -> dict[str, Any]:
    if not (body.question or "").strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        result = await asyncio.to_thread(service.ask_with_visualization, question=body.question.strip())
        return result
    except Exception as e:
        logger.exception("答案生成（含可视化）失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
