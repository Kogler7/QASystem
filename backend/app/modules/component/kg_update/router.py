"""
知识图谱更新组件 API：进程内调用 algorithm/KGUpdate 算法（按函数封装）。
"""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/component/kg-update", tags=["component", "kg-update"])


class TripleAddItem(BaseModel):
    subject: str
    predicate: str
    object: str
    confidence: float


class AddTriplesRequestBody(BaseModel):
    triples: list[TripleAddItem]


class TripleDeleteItem(BaseModel):
    subject: str
    predicate: str
    object: str


class DeleteTriplesRequestBody(BaseModel):
    triples: list[TripleDeleteItem]


class CalculateConfidenceRequestBody(BaseModel):
    triples: list[TripleDeleteItem]  # 无置信度

@router.get("/health")
async def kg_update_health() -> dict[str, Any]:
    return service.health()


@router.post("/add")
async def add_triples(body: AddTriplesRequestBody) -> dict[str, Any]:
    try:
        triples = [t.model_dump() for t in body.triples]
        result = await asyncio.to_thread(service.add_triples, triples)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("知识图谱更新-添加失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/delete")
async def delete_triples(body: DeleteTriplesRequestBody) -> dict[str, Any]:
    try:
        triples = [t.model_dump() for t in body.triples]
        result = await asyncio.to_thread(service.delete_triples, triples)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("知识图谱更新-删除失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/statistics")
async def get_statistics() -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(service.get_statistics)
        return result
    except Exception as e:
        logger.exception("知识图谱更新-统计失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/calculate-confidence")
async def calculate_confidence(body: CalculateConfidenceRequestBody) -> dict[str, Any]:
    try:
        triples = [t.model_dump() for t in body.triples]
        result = await asyncio.to_thread(service.calculate_confidence, triples)
        return {"triples": result}
    except Exception as e:
        logger.exception("置信度计算失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
