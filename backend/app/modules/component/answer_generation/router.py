"""
答案生成组件 API：进程内调用 algorithm/ans（Neo4j 知识图谱问答）。
"""
import asyncio
import logging
from typing import Any, List

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


class QueryGraphRequestBody(BaseModel):
    node_labels: List[str]
    limit: int = 20


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


@router.post("/query-graph")
async def query_graph(body: QueryGraphRequestBody) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            service.query_graph_by_nodes,
            node_labels=body.node_labels,
            limit=body.limit
        )
        return {"success": True, "data": result}
    except Exception as e:
        logger.exception("查询知识图谱失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


class ProblemDefinitionRequestBody(BaseModel):
    question: str


class SolutionPlanningRequestBody(BaseModel):
    problem_model: dict


class AnswerGenerationRequestBody(BaseModel):
    solution: dict
    graph_paths: dict
    question: str


@router.post("/tools/problem-definition")
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


@router.post("/tools/solution-planning")
async def solution_planning(body: SolutionPlanningRequestBody) -> dict[str, Any]:
    """方案规划工具"""
    if not body.problem_model:
        raise HTTPException(status_code=400, detail="问题模型不能为空")
    try:
        result = await asyncio.to_thread(service.plan_solution, problem_model=body.problem_model)
        return result
    except Exception as e:
        logger.exception("方案规划失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/tools/answer-generation")
async def answer_generation(body: AnswerGenerationRequestBody) -> dict[str, Any]:
    """答案生成工具"""
    if not body.solution:
        raise HTTPException(status_code=400, detail="解决方案不能为空")
    if not body.graph_paths:
        raise HTTPException(status_code=400, detail="图谱路径不能为空")
    if not (body.question or "").strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        result = await asyncio.to_thread(
            service.generate_answer,
            solution=body.solution,
            graph_paths=body.graph_paths,
            question=body.question.strip()
        )
        return result
    except Exception as e:
        logger.exception("答案生成失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
