"""
知识图谱更新组件 API：进程内调用 algorithm/KGUpdate 算法（按函数封装），不启动独立服务。
"""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...services import kg_update_service

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


@router.get("/health")
async def kg_update_health() -> dict[str, Any]:
    """检查知识图谱更新模块是否可加载。"""
    return kg_update_service.health()


@router.post("/add")
async def add_triples(body: AddTriplesRequestBody) -> dict[str, Any]:
    """批量添加三元组，支持新增/更新/拒绝/冲突逻辑（见 algorithm/KGUpdate）。"""
    try:
        triples = [t.model_dump() for t in body.triples]
        result = await asyncio.to_thread(kg_update_service.add_triples, triples)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("知识图谱更新-添加失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/delete")
async def delete_triples(body: DeleteTriplesRequestBody) -> dict[str, Any]:
    """批量删除三元组。"""
    try:
        triples = [t.model_dump() for t in body.triples]
        result = await asyncio.to_thread(kg_update_service.delete_triples, triples)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("知识图谱更新-删除失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/statistics")
async def get_statistics() -> dict[str, Any]:
    """获取知识图谱统计信息（节点数、关系数、置信度分布）。"""
    try:
        result = await asyncio.to_thread(kg_update_service.get_statistics)
        return result
    except Exception as e:
        logger.exception("知识图谱更新-统计失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
