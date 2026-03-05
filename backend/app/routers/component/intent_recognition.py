"""
意图识别组件 API：进程内调用 algorithm/intent_recognition_model，不启动独立服务。
"""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...services import intent_recognition_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/component/intent-recognition", tags=["component", "intent-recognition"])


class RecognizeRequestBody(BaseModel):
    text: str


@router.get("/health")
async def intent_recognition_health() -> dict[str, Any]:
    """检查意图识别模块是否可加载。"""
    return intent_recognition_service.health()


@router.post("/recognize")
async def recognize(body: RecognizeRequestBody) -> dict[str, Any]:
    """对用户输入进行意图识别，返回最匹配的意图（结构化）。"""
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="输入文本不能为空")
    try:
        result = await asyncio.to_thread(
            intent_recognition_service.recognize,
            text=body.text.strip(),
        )
        return result
    except Exception as e:
        logger.exception("意图识别失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
