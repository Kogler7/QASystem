"""
意图识别组件 API：进程内调用 algorithm/intent_recognition_model。
"""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from fastapi.responses import StreamingResponse

from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/component/intent-recognition", tags=["component", "intent-recognition"])


class RecognizeRequestBody(BaseModel):
    text: str


@router.get("/health")
async def intent_recognition_health() -> dict[str, Any]:
    return service.health()


@router.post("/recognize")
async def recognize(body: RecognizeRequestBody) -> dict[str, Any]:
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="输入文本不能为空")
    try:
        result = await asyncio.to_thread(service.recognize, text=body.text.strip())
        return result
    except Exception as e:
        logger.exception("基础意图识别失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/deep-recognize")
async def deep_recognize(body: RecognizeRequestBody) -> dict[str, Any]:
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="输入文本不能为空")
    try:
        result = await asyncio.to_thread(service.deep_recognize, text=body.text.strip())
        return result
    except Exception as e:
        logger.exception("深度意图识别失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/get-plan")
async def get_plan(body: RecognizeRequestBody) -> dict[str, Any]:
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="输入文本不能为空")
    try:
        result = await asyncio.to_thread(service.get_plan, text=body.text.strip())
        return result
    except Exception as e:
        logger.exception("获取计划失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
