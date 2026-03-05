import logging
import sys
import time
from typing import Any, Optional

# 确保日志输出（uvicorn 启动时通常已配置，此处兜底）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

from fastapi import FastAPI
from pydantic import BaseModel

from ie.extraction_api import (
    extract_and_store,
    extract_with_templates_and_store,
    extract_only,
    extract_with_templates_only,
)
from ie.utils import BaseLLM, LocalLLM, ApiLLM

logger = logging.getLogger(__name__)

# 全局复用 backend，避免每次请求都加载一次模型
_backend_local: Optional[BaseLLM] = None
_backend_api: Optional[BaseLLM] = None


def _get_backend(use_local: bool) -> BaseLLM:
    global _backend_local, _backend_api
    if use_local:
        if _backend_local is None:
            _backend_local = LocalLLM()
        return _backend_local
    else:
        if _backend_api is None:
            _backend_api = ApiLLM()
        return _backend_api


app = FastAPI(
    title="Knowledge Extraction API",
    description="基于 UML 本体驱动的信息抽取服务（支持本地微调模型或外部 API）。",
    version="1.0.0",
)


class ExtractRequest(BaseModel):
    main_object: str
    text: str
    use_local: bool = True          # 默认使用本地微调模型
    use_templates: bool = True      # 默认启用模板匹配 + 置信度
    store_to_neo4j: bool = False    # 默认只返回结果，不写入 Neo4j


class ExtractResponse(BaseModel):
    data: Any


@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.post("/extract", response_model=ExtractResponse)
def extract(req: ExtractRequest):
    """
    调用本体驱动抽取：
    - main_object: "RuleType" / "SystemElement" / "Term" / "Glossary"
    - text: 待抽取文本
    - use_local: True 使用本地微调模型，False 使用外部 API
    - use_templates: True 时启用模板优先 + 置信度增强
    - store_to_neo4j: True 时写入 Neo4j，False 只返回 JSON
    """
    logger.info(
        "extract request: main_object=%s use_local=%s use_templates=%s store_to_neo4j=%s text_len=%d",
        req.main_object, req.use_local, req.use_templates, req.store_to_neo4j, len(req.text),
    )
    t0 = time.perf_counter()
    backend = _get_backend(req.use_local)

    if req.use_templates:
        if req.store_to_neo4j:
            data = extract_with_templates_and_store(
                main_object=req.main_object,
                text=req.text,
                backend=backend,
                use_local=req.use_local,
            )
        else:
            data = extract_with_templates_only(
                main_object=req.main_object,
                text=req.text,
                backend=backend,
                use_local=req.use_local,
            )
    else:
        if req.store_to_neo4j:
            data = extract_and_store(
                main_object=req.main_object,
                text=req.text,
                backend=backend,
                use_local=req.use_local,
            )
        else:
            data = extract_only(
                main_object=req.main_object,
                text=req.text,
                backend=backend,
                use_local=req.use_local,
            )

    elapsed = time.perf_counter() - t0
    out_summary = _summarize_data(data)
    logger.info("extract done: elapsed=%.2fs %s", elapsed, out_summary)
    return ExtractResponse(data=data)


def _summarize_data(data: Any) -> str:
    """简要描述返回结果，便于日志记录"""
    if isinstance(data, dict) and "raw" in data and "graph" in data:
        raw = data.get("raw")
        graph = data.get("graph") or {}
        nodes = graph.get("nodes") or []
        rels = graph.get("relationships") or []
        if isinstance(raw, list):
            return f"raw_items={len(raw)} graph_nodes={len(nodes)} graph_rels={len(rels)}"
        return f"graph_nodes={len(nodes)} graph_rels={len(rels)}"
    if isinstance(data, list):
        return f"items={len(data)}"
    if isinstance(data, dict):
        return "dict"
    return "str"


# 启动示例（在 uie 目录下）：
# uvicorn fastapi_app:app --host 0.0.0.0 --port 8001 --reload

