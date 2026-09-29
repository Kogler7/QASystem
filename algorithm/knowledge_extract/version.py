"""知识抽取算法版本选择。

统一放在 ``knowledge_extract`` 包下，与 ``v2`` / ``v3`` / ``latest`` 等具体 shim 并列。

默认 ``latest`` 会自动解析为当前最新版本；做算法版本对比评测时再显式设为 ``v2`` 或 ``v3``。
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
    """返回实际要跑的算法版本：latest 解析为当前最新版本。"""
    label = get_extract_version_label()
    if label == LATEST:
        return V3
    return label
