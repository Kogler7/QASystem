"""
意图识别组件 API：进程内调用 algorithm/intent_recognition_model。
"""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ....deps.auth import require_admin
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/component/intent-recognition",
    tags=["component", "intent-recognition"],
    dependencies=[Depends(require_admin)],
)


class RecognizeRequestBody(BaseModel):
    problem_model: dict|str  # 直接接受字典类型

class GetPlanRequestBody(BaseModel):
    problem_model: dict  # 直接接受字典类型

class GetToolsRequestBody(BaseModel):
    step: dict  # 直接接受字典类型


def _problem_model_nonempty(pm: dict | str) -> bool:
    if pm is None:
        return False
    if isinstance(pm, str):
        return bool(pm.strip())
    if isinstance(pm, dict):
        return bool(pm)
    return False


@router.get("/health")
async def intent_recognition_health() -> dict[str, Any]:
    return service.health()


@router.post("/recognize")
async def recognize(body: RecognizeRequestBody) -> dict[str, Any]:
    if not _problem_model_nonempty(body.problem_model):
        raise HTTPException(status_code=400, detail="问题模型不能为空")
    try:
        result = await asyncio.to_thread(service.recognize, body.problem_model)
        return result
    except Exception as e:
        logger.exception("基础意图识别失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/deep-recognize")
async def deep_recognize(body: RecognizeRequestBody) -> dict[str, Any]:
    if not body.problem_model:
        raise HTTPException(status_code=400, detail="问题模型不能为空")
    try:
        result = await asyncio.to_thread(service.deep_recognize, problem_model=body.problem_model)
        return result
    except Exception as e:
        logger.exception("深度意图识别失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/problem-solving/plan")
async def get_plan(body: GetPlanRequestBody) -> dict[str, Any]:
    if not body.problem_model:
        raise HTTPException(status_code=400, detail="问题模型不能为空")
    try:
        result = await asyncio.to_thread(service.get_plan, problem_model=body.problem_model)
        return result
    except Exception as e:
        logger.exception("获取计划失败")
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post("/problem-solving/tool-match")
async def get_tools(body: GetToolsRequestBody) -> dict[str, Any]:
    if not body.step:
        raise HTTPException(status_code=400, detail="步骤不能为空")
    try:
        result = await asyncio.to_thread(service.get_tool_match, step=body.step)
        return result
    except Exception as e:  
        logger.exception("获取工具匹配关系失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
