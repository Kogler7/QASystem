"""
向量转化存储与自然语言转 Cypher 组件 API：进程内调用 algorithm/NL_to_cypher。
"""
import asyncio
import logging
from typing import Any,Dict,List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ....deps.auth import require_admin
from . import service

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/component/nl2cypher",
    tags=["component", "nl2cypher"],
    dependencies=[Depends(require_admin)],
)


class GenerateRequestBody(BaseModel):
    question: str
    graph_schema: str = ""

class TextRequestBody(BaseModel):
    text: str

class TextVectorRequestBody(BaseModel):
    text: str
    source: str = "default_file_name"  # 默认值

@router.get("/health")
async def nl2cypher_health() -> dict[str, Any]:
    return service.health()

#自然语言转cypher
@router.post("/generate")
async def generate_cypher(body: GenerateRequestBody) -> dict[str, Any]:
    if not (body.question or "").strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        result = await asyncio.to_thread(
            service.generate,
            question=body.question.strip(),
            graph_schema=(body.graph_schema or "").strip(),
        )
        return result
    except Exception as e:
        logger.exception("自然语言转 Cypher 失败")
        raise HTTPException(status_code=500, detail=str(e)) from e

# 文本转向量并存储
@router.post("/text2vector")
async def text2vector(body: TextVectorRequestBody) -> dict[str, Any]:
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="文本不能为空")
    try:
        result = await asyncio.to_thread(
            service.text2vector,
            text=body.text.strip(),
            source=body.source
        )
        return result
    except Exception as e:
        logger.exception("文本向量化失败")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/split/character")
async def split_character(body: TextRequestBody) -> dict[str, Any]:
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="文本不能为空")
    try:
        result = await asyncio.to_thread(
            service.split_by_character,
            text=body.text.strip(),
        )
        return result
    except Exception as e:
        logger.exception("字符切片失败")
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post("/split/recursive")
async def split_recursive(body: TextRequestBody) -> dict[str, Any]:
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="文本不能为空")
    try:
        result = await asyncio.to_thread(
            service.split_recursively,
            text=body.text.strip(),
        )
        return result
    except Exception as e:
        logger.exception("递归字符切片失败")
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post("/split/markdown")
async def split_markdown(body: TextRequestBody) -> dict[str, Any]:
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="文本不能为空")
    try:
        result = await asyncio.to_thread(
            service.split_by_markdown,
            text=body.text.strip(),
        )
        return result
    except Exception as e:
        logger.exception("Markdown切片失败")
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post("/split/python")
async def split_python(body: TextRequestBody) -> dict[str, Any]:
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="文本不能为空")
    try:
        result = await asyncio.to_thread(
            service.split_by_language_python,
            text=body.text.strip(),
        )
        return result
    except Exception as e:
        logger.exception("Python语言切片失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
    
# 在router.py中增加
@router.get("/vectors")
async def get_all_vectors() -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(service.get_all_vectors)
        return result
    except Exception as e:
        logger.exception("获取向量数据失败")
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get("/mutiRetriever")
async def get_all_info() -> List[Dict[str, Any]]:
    try:
        results = await asyncio.to_thread(service.muti_retriever)
        return results
    except Exception as e:
        logger.exception("获取向量数据失败")
        raise HTTPException(status_code=500, detail=str(e)) from e

