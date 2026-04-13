"""
知识图谱更新组件 API：进程内调用 algorithm/KGUpdate 算法（按函数封装）。
"""
import asyncio
import logging
from typing import Any, Optional

import json

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ....deps.auth import require_admin
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/component/kg-update",
    tags=["component", "kg-update"],
    dependencies=[Depends(require_admin)],
)


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


class CalculateConfidenceRequestBody(BaseModel):
    triples: list[TripleDeleteItem] = Field(..., description="需要计算置信度的三元组（无置信度）")


class CalculateConfidenceResponseItem(BaseModel):
    subject: str
    predicate: str
    object: str
    confidence: float
    component_scores: dict = Field(..., description="各组件得分")


class CalculateConfidenceResponse(BaseModel):
    triples: list[CalculateConfidenceResponseItem]


class FullRelationItem(BaseModel):
    """完整的关系信息（用于入库）"""
    type: str
    subject: str
    predicate: str
    object: str
    source_node: dict = Field(default_factory=dict, description="源节点完整属性")
    target_node: dict = Field(default_factory=dict, description="目标节点完整属性")


class PredictionItem(BaseModel):
    """预测结果"""
    subject: str
    predicate: str
    object: str
    confidence: float
    component_scores: dict


class ProcessSchemaOutputRequest(BaseModel):
    data: dict = Field(..., description="schema_mapper 生成的 JSON（包含 raw 和 graph）")
    confidence_threshold: Optional[float] = Field(0.7, description="置信度阈值")


class ProcessSchemaOutputResponse(BaseModel):
    success: bool
    message: str
    statistics: dict
    relations_high: list
    relations_low: list
    full_relations: list[FullRelationItem] = Field(..., description="完整的关系信息")
    predictions: list[PredictionItem] = Field(..., description="预测结果")


class AddFromComputedRequest(BaseModel):
    relations_high: list = Field(..., description="高置信度的三元组列表")
    full_relations: list = Field(..., description="完整的关系信息（包含节点属性）")
    predictions: list = Field(..., description="预测结果")
    confidence_threshold: Optional[float] = Field(0.7, description="置信度阈值")


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
async def calculate_confidence(body: CalculateConfidenceRequestBody) -> CalculateConfidenceResponse:
    """计算三元组的置信度（不写入图谱）"""
    try:
        triples = [t.model_dump() for t in body.triples]
        result = await asyncio.to_thread(service.calculate_confidence, triples)
        return CalculateConfidenceResponse(triples=result)
    except Exception as e:
        logger.exception("置信度计算失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/process-schema-output")
async def process_schema_output(body: ProcessSchemaOutputRequest) -> ProcessSchemaOutputResponse:
    """
    处理 schema_mapper 输出的 JSON，提取关系，计算置信度（不入库）
    返回完整的计算结果，供前端展示和后续入库
    """
    try:
        result = await asyncio.to_thread(
            service.process_schema_output,
            body.data,
            body.confidence_threshold
        )
        return ProcessSchemaOutputResponse(
            success=result["success"],
            message=result["message"],
            statistics=result["statistics"],
            relations_high=result["relations_high"],
            relations_low=result["relations_low"],
            full_relations=result["full_relations"],
            predictions=result["predictions"]
        )
    except Exception as e:
        logger.exception("处理 schema_mapper 输出失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/add-from-computed")
async def add_from_computed(body: AddFromComputedRequest) -> dict[str, Any]:
    """
    从计算结果中入库高置信度的关系（保留所有属性）
    """
    try:
        result = await asyncio.to_thread(
            service.add_relations_from_computed,
            body.relations_high,
            body.full_relations,
            body.predictions,
            body.confidence_threshold
        )
        return result
    except Exception as e:
        logger.exception("从计算结果入库失败")
        raise HTTPException(status_code=500, detail=str(e)) from e