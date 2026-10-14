"""知识抽取算法版本选择。

统一放在 ``knowledge_extract`` 包下，与 ``v2`` / ``v3`` / ``latest`` 等具体 shim 并列。

``latest`` 是现行主版本的同步副本（独立实体目录，非软链/映射）；
``KG_EXTRACT_ALGO_VERSION=latest``（默认）直接运行 ``latest`` 包，不做版本解析。
做算法版本对比评测时再显式选择具体版本。
对外版本号 ``v3.1`` 对应 Python 包 ``v3_1``（主模型生成 + Clef 语义裁决的通用 harness）。
对外版本号 ``v0`` 对应 Python 包 ``v0``（只做实体 + 属性抽取，不抽取任何关系）。
对外版本号 ``v2.1`` 对应 Python 包 ``v2_1``（v2 流水线 + rule 文档路由与规则抽取 prompt 扩展）。
对外版本号 ``v2.2`` 对应 Python 包 ``v2_2``（v2 流水线 + 可选 Clef 预判）。
对外版本号 ``v2.2-v31`` 对应 Python 包 ``v2_2_v31``（Stage1=v3.1 对齐实体 + Stage2=v2.2 关系）。
``jev`` 只在显式设置时运行 ``jev_extract``，不改变默认抽取。
"""
from __future__ import annotations

from algorithm.runtime_settings import getenv

LATEST = "latest"
V0 = "v0"
V2 = "v2"
V21 = "v2.1"
V22 = "v2.2"
V22_V31 = "v2.2-v31"
V3 = "v3"
V31 = "v3.1"
JEV = "jev"
SUPPORTED_VERSIONS = (LATEST, V0, V2, V21, V22, V22_V31, V3, V31, JEV)
DEFAULT_VERSION = LATEST


def get_extract_version_label() -> str:
    """返回配置原值（可能是 latest / v2 / v2.2 / v2.2-v31 / v3 / v3.1 / jev），不做 latest 解析。"""
    value = getenv("KG_EXTRACT_ALGO_VERSION", DEFAULT_VERSION).strip().lower()
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
