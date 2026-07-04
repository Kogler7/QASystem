"""
答案生成组件 API：进程内调用 algorithm/ans（Neo4j 知识图谱问答）。
"""
import asyncio
import logging
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ....deps.auth import require_admin
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/component/answer-generation",
    tags=["component", "answer-generation"],
    dependencies=[Depends(require_admin)],
)


class AskRequestBody(BaseModel):
    question: str
    detailed: bool = False


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


class ProblemDefinitionRequestBody(BaseModel):
    question: str



class AnswerGenerationRequestBody(BaseModel):
    intent: dict|str = None
    question: str = None

class DecomposeProblemRequestBody(BaseModel):
    problem_model: dict



@router.post("/problem-solving/problem-definition")
async def problem_definition(body: ProblemDefinitionRequestBody) -> dict[str, Any]:
    """问题定义工具"""
    if not (body.question or "").strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        result = await asyncio.to_thread(service.define_problem, question=body.question.strip())
        return result
    except Exception as e:
        logger.exception("问题定义失败")
        raise HTTPException(status_code=500, detail=str(e)) from e



@router.post("/tools/answer-generation")
async def answer_generation(body: AnswerGenerationRequestBody) -> dict[str, Any]:
    """答案生成工具
    支持两种输入格式：
    1. JSON格式: {"intent": {"intent_id": "...", "user_goal": "...", ...}}
    2. 直接字符串问题: {"question": "分析公开与非公开采购金额构成"}
    """
    try:
        # 优先使用intent字段，其次使用question字段
        input_data = body.intent if body.intent else body.question
        
        if not input_data:
            raise HTTPException(status_code=400, detail="intent或question字段不能为空")
        
        result = await asyncio.to_thread(
            service.generate_answer,
            question=input_data
        )
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("答案生成失败")
        raise HTTPException(status_code=500, detail=str(e)) from e

      
@router.post("/problem-solving/decompose")
async def decompose_problem(body: DecomposeProblemRequestBody) -> dict[str, Any]:
    """问题分解工具"""
    return {
        "code": 0,
        "info": "",
        "data": {
            "problem_model": {
            "problem_id": "QKB-001",
            "description": "公开采购与非公开采购金额构成如何？",
            "problem_type": "描述型问题",
            "constraints": ["需基于公开数据源"],
            "target_object": "采购金额构成",
            "stakeholders": ["采购部门", "财务部门"],
            "goals": ["明确金额分配比例"],
            "status": "分析阶段",
            "created_at": "2026-04-08T00:54:30.393365",
            "related_entities": ["公开采购", "非公开采购", "金额构成"],
            "children": [
                {
                "problem_id": "QKB-001-A",
                "description": "厘清公开/非公开采购口径与数据来源",
                "problem_type": "澄清型子问题",
                "constraints": [],
                "target_object": "统计口径",
                "stakeholders": [],
                "goals": [],
                "status": "待分析",
                "created_at": "2026-04-08T00:54:30.393365",
                "related_entities": [],
                "children": []
                },
                {
                "problem_id": "QKB-001-B",
                "description": "在口径一致前提下计算金额构成",
                "problem_type": "计算型子问题",
                "constraints": [],
                "target_object": "金额构成",
                "stakeholders": [],
                "goals": [],
                "status": "待分析",
                "created_at": "2026-04-08T00:54:30.393365",
                "related_entities": [],
                "children": []
                }
            ]
            },
            "steps": [],
            "details": {}
        }
}


class RecommendDimensionsRequestBody(BaseModel):
    requirement: dict
    selected_dimensions: list[dict] = []
    total_dimensions: list[dict] | None = None


class GenerateSpaceDraftRequestBody(BaseModel):
    requirement: dict
    dimensions: list[dict]


@router.post("/tools/recommend-dimensions")
async def recommend_dimensions(body: RecommendDimensionsRequestBody) -> dict[str, Any]:
    """维度推荐

    根据用户需求和预选维度，LLM分析推荐维度刻度。
    """
    try:
        result = await asyncio.to_thread(
            service.recommend_dimensions,
            requirement=body.requirement,
            selected_dimensions=body.selected_dimensions,
            total_dimensions=body.total_dimensions,
        )
        return result
    except Exception as e:
        logger.exception("维度推荐失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/tools/generate-space-draft")
async def generate_space_draft(body: GenerateSpaceDraftRequestBody) -> dict[str, Any]:
    """空间草稿

    根据用户确定的维度刻度，生成知识空间树草稿。
    """
    try:
        result = await asyncio.to_thread(
            service.generate_space_draft,
            requirement=body.requirement,
            dimensions=body.dimensions,
        )
        return result
    except Exception as e:
        logger.exception("空间草稿生成失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
