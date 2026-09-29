"""latest 标记：始终指向当前最新版本的抽取算法。

默认配置 ``KG_EXTRACT_ALGO_VERSION=latest`` 即使用本模块；
当前 latest 解析为 v3，未来引入 v4 时只需修改这里，调用方无需改动。
"""
from __future__ import annotations

from typing import Any

from ..v3 import extract_aggregate, extract_detailed
from ..v3.context import ExtractionChunk


def extract(*args: Any, **kwargs: Any) -> Any:
    """Delegate to the current latest extraction implementation."""
    return extract_detailed(*args, **kwargs)


__all__ = ["extract", "extract_aggregate", "extract_detailed", "ExtractionChunk"]
