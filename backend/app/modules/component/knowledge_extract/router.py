"""
知识抽取组件 API：进程内调用 algorithm/uie 算法（按函数封装），不启动独立服务。
"""
import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/component/extract", tags=["component", "extract"])


class ExtractRequestBody(BaseModel):
    main_object: str
    text: str
    use_local: bool = True
    use_templates: bool = True
    store_to_neo4j: bool = False


@router.get("/health")
async def extract_health() -> dict[str, Any]:
    return service.health()


@router.post("")
async def extract(body: ExtractRequestBody) -> dict[str, Any]:
    try:
        result = await asyncio.to_thread(
            service.extract,
            main_object=body.main_object,
            text=body.text,
            use_local=body.use_local,
            use_templates=body.use_templates,
            store_to_neo4j=body.store_to_neo4j,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("知识抽取失败")
        raise HTTPException(status_code=500, detail=str(e)) from e
