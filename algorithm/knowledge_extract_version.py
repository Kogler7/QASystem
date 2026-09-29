"""知识抽取算法版本选择。

Backend ``graph.extract`` 与 MCP ``kg_extract_v2`` 共用同一个开关：
环境变量 ``KG_EXTRACT_ALGO_VERSION`` 决定实际调用哪一版抽取算法，
评测脚本借此对同一评测集分别跑 v2 / v3 而无需改代码。
"""
from __future__ import annotations

import os

V2 = "v2"
V3 = "v3"
SUPPORTED_VERSIONS = (V2, V3)
DEFAULT_VERSION = V2


def get_extract_version() -> str:
    value = os.getenv("KG_EXTRACT_ALGO_VERSION", DEFAULT_VERSION).strip().lower()
    if value in SUPPORTED_VERSIONS:
        return value
    raise ValueError(
        f"KG_EXTRACT_ALGO_VERSION 仅支持 {' / '.join(SUPPORTED_VERSIONS)}，当前值: {value!r}"
    )
