"""
向量转化存储与自然语言转 Cypher 组件 API：进程内调用 algorithm/NL_to_cypher。
"""
import asyncio
import logging
from typing import Any,Dict,List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/component/nl2cypher", tags=["component", "nl2cypher"])


class GenerateRequestBody(BaseModel):
    question: str
    graph_schema: str = ""

class TextRequestBody(BaseModel):
    text: str
class TextsRequestBody(BaseModel):
    texts: list[str]

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

@router.post("/text-to-vector")
async def text_to_vector_api(body: TextRequestBody) -> dict[str, Any]:
    """
    将单段文本转换为向量
    
    Args:
        body: 请求体，包含待向量化文本
    
    Returns:
        向量化结果
    """
    # 参数校验
    if not (body.text or "").strip():
        raise HTTPException(status_code=400, detail="文本不能为空")
    
    try:
        # 异步调用服务层函数
        result = await asyncio.to_thread(
            service.text_to_vector_service,
            text=body.text.strip()
        )
        return result
    except Exception as e:
        # 异常处理
        raise HTTPException(status_code=500, detail=f"文本转向量失败: {str(e)}")

@router.post("/text-to-vectors")
async def batch_text_to_vectors_api(body:TextsRequestBody) -> dict[str, Any]:
    """
    批量将多段文本转换为向量
    
    接口地址: /api/component/nl2cypher/batch-text-to-vectors
    请求方式: POST
    请求格式: application/json; charset=utf-8
    认证方式: 无
    """
    # 参数校验
    if not body.texts:
        raise HTTPException(status_code=400, detail="文本列表不能为空")
    
    if len(body.texts) == 0:
        raise HTTPException(status_code=400, detail="文本列表至少需要一个元素")
    
    # 过滤空文本
    valid_texts = [t for t in body.texts if t and t.strip()]
    if not valid_texts:
        raise HTTPException(status_code=400, detail="文本列表中所有文本均为空")
    
    try:
        # 异步调用服务层函数
        result = await asyncio.to_thread(
            service.batch_text_to_vector_service,
            texts=valid_texts
        )
        
        # 如果服务层返回错误码，转为 HTTP 异常
        if result.get("code") != 0:
            raise HTTPException(
                status_code=result.get("code", 500),
                detail=result.get("message", "未知错误")
            )
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量文本转向量失败: {str(e)}")
    
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

