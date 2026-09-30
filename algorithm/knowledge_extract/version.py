"""知识抽取算法版本选择。

统一放在 ``knowledge_extract`` 包下，与 ``v2`` / ``v3`` / ``latest`` 等具体 shim 并列。

``latest`` 是现行 ``v3`` 的同步副本（独立实体目录，非软链/映射）；
``KG_EXTRACT_ALGO_VERSION=latest``（默认）直接运行 ``latest`` 包，不做版本解析。
做算法版本对比评测时再显式设为 ``v2`` 或 ``v3``。
"""
from __future__ import annotations

import os

LATEST = "latest"
V2 = "v2"
V3 = "v3"
SUPPORTED_VERSIONS = (LATEST, V2, V3)
DEFAULT_VERSION = LATEST


def get_extract_version_label() -> str:
    """返回配置原值（可能是 latest / v2 / v3），不做 latest 解析。"""
    value = os.getenv("KG_EXTRACT_ALGO_VERSION", DEFAULT_VERSION).strip().lower()
    if value in SUPPORTED_VERSIONS:
        return value
    raise ValueError(
        f"KG_EXTRACT_ALGO_VERSION 仅支持 {' / '.join(SUPPORTED_VERSIONS)}，当前值: {value!r}"
    )


def get_extract_version() -> str:
    """返回实际要跑的算法版本：latest 使用独立的 latest 包，不再解析为 v3。"""
    label = get_extract_version_label()
    if label == LATEST:
        return LATEST
    return label
