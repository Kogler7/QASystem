"""
知识图谱更新组件 API：进程内调用 algorithm/KGUpdate 算法（按函数封装）。
"""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ....deps.auth import require_admin
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/component/kg-update",
    tags=["component", "kg-update"],
    dependencies=[Depends(require_admin)],
)


# ==================== 请求模型 ====================

class TripleAddItem(BaseModel):
    subject: str = Field(..., description="主语实体")
    predicate: str = Field(..., description="谓语/关系")
    object: str = Field(..., description="宾语实体")
    confidence: float = Field(..., ge=0, le=1, description="置信度，范围0-1")


class AddTriplesRequestBody(BaseModel):
    triples: list[TripleAddItem] = Field(..., description="带置信度的三元组列表")


class TripleDeleteItem(BaseModel):
    subject: str
    predicate: str
    object: str


class DeleteTriplesRequestBody(BaseModel):
    triples: list[TripleDeleteItem] = Field(..., description="要删除的三元组列表")


class GraphRequest(BaseModel):
    """接收 graph 格式的请求体"""
    graph: dict = Field(..., description="包含 nodes 和 relationships 的知识图谱数据")


# ==================== 路由定义 ====================

@router.get("/health")
async def kg_update_health() -> dict[str, Any]:
    """健康检查"""
    return service.health()


@router.post("/add")
async def add_triples(body: AddTriplesRequestBody) -> dict[str, Any]:
    """批量添加带置信度的三元组到知识图谱"""
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
    """批量删除三元组"""
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
    """获取知识图谱统计信息"""
    try:
        result = await asyncio.to_thread(service.get_statistics)
        return result
    except Exception as e:
        logger.exception("知识图谱更新-统计失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/calculate-confidence")
async def calculate_confidence(request: GraphRequest) -> dict[str, Any]:
    """
    接收 graph JSON (nodes + relationships)，为每条关系计算置信度，
    将置信度写入 relationship.properties，并返回完整的 graph。
    """
    try:
        result = await asyncio.to_thread(
            service.calculate_confidence_from_graph,
            request.model_dump()
        )
        return result
    except Exception as e:
        logger.exception("置信度计算失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
