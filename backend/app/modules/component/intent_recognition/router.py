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
        logger.exception("意图识别失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/intent-recognition-stream")
async def stream_intent_recognition(body: RecognizeRequestBody):
    """流式意图识别API"""
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="输入文本不能为空")
    
    try:
        def generate():
            recognizer = service._get_intent_recognition()
            for chunk in recognizer.generate_result_chunk(body.text.strip()):
                yield f"data: {chunk}\n\n"  # SSE 格式
        
        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive"
            }
        )
    except Exception as e:
        logger.exception("流式意图识别失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
