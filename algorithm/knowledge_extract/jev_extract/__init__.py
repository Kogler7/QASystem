"""jev：从 latest 整包复制的抽取副本，供 JEV 决策层迭代。

分叉自提交 ``e9d3015`` 的 ``algorithm/knowledge_extract/latest/``。
本目录是独立代码，不是 ``latest`` 的软链。``KG_EXTRACT_ALGO_VERSION`` 默认仍是
``latest``。只有显式设为 ``jev`` 才运行本包。``KG_JEV_ENABLED`` 默认关闭时，
抽取结果与分叉当时的 ``latest`` 相同。
"""
from __future__ import annotations

from .adapter import extract_aggregate
from .context import ExtractionChunk
from .harness import extract_detailed


def extract(chunks, ontology: str, **kwargs):
    """Return scored facts. Inspect status before using an incomplete result."""
    return extract_detailed(chunks, ontology, **kwargs)


__all__ = ["extract", "extract_aggregate", "extract_detailed", "ExtractionChunk"]
