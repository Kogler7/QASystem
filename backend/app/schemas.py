from typing import Any

from pydantic import BaseModel, Field


class BatchTextVectorRequest(BaseModel):
    texts: list[str] = Field(..., min_length=1, description="待向量化文本列表，保留输入顺序和空串")


class GraphRequest(BaseModel):
    graph: dict[str, Any] = Field(..., description="包含 nodes 和 relationships 的知识图谱数据")
