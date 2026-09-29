"""latest：独立、带完整代码的当前主版本抽取算法。

默认配置 ``KG_EXTRACT_ALGO_VERSION=latest`` 即使用本模块。
本目录是一份独立代码副本，不是 v2/v3 的软链或映射；后续对 v3 的实验性改动
不会自动影响正在运行的 latest，需要显式把 latest 内容同步/替换后才会生效。
"""
from __future__ import annotations

from .adapter import extract_aggregate
from .context import ExtractionChunk
from .harness import extract_detailed


def extract(chunks, ontology: str, **kwargs):
    """Return scored facts. Inspect status before using an incomplete result."""
    return extract_detailed(chunks, ontology, **kwargs)


__all__ = ["extract", "extract_aggregate", "extract_detailed", "ExtractionChunk"]
