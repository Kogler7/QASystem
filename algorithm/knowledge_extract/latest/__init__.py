"""latest：独立、带完整代码的当前主版本抽取算法。

默认配置 ``KG_EXTRACT_ALGO_VERSION=latest`` 直接运行本包。
本目录是一份独立代码副本，不是 v2/v3 的软链或映射；后续对 v3 的实验性改动
不会自动影响正在运行的 latest，需要显式把 latest 内容同步/替换后才会生效。

本次同步：2026-10-01 同步自 v3（含 P0 止血：证据闸 warn 化
``KG_EXTRACT_EVIDENCE_ADMISSION``、修复循环默认 2 轮、实体/关系 LLM 主调用
多重试恢复）；来源为 ``v3`` 的全量代码副本（含 ``tests/``）。旧版实验
harness（adapter/client/context/facts/harness/prompts/schema）已随前次同步移除。

包入口：暴露 backend 适配器需要的 aggregate / public 契约。
"""
from collections.abc import Iterable
from typing import Any

from .batch import deduplicate_chunk_graphs
from .chunk_context import ExtractionChunk
from .harness_extractor import extract_harness
from .result_contract import to_public_result
from .split_extractor import extract_split


def extract(
    chunks: Iterable[ExtractionChunk],
    ontology: str,
    **kwargs: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Extract the stable public ``entities`` / ``relations`` contract."""
    return to_public_result(extract_harness(ontology=ontology, chunks=chunks, **kwargs))


def extract_aggregate(
    ontology: str,
    chunks: Iterable[ExtractionChunk],
    *,
    target_types: list[str] | None = None,
    validation_mode: str = "permissive",
    best_effort: bool = True,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_workers: int | None = None,
    llm: Any | None = None,
    use_scheduler: bool = True,
    **_: Any,
) -> dict[str, Any]:
    """Run the pipeline over ``chunks`` and return the aggregate contract."""
    return extract_harness(
        ontology=ontology,
        chunks=chunks,
        target_types=target_types,
        validation_mode=validation_mode,
        best_effort=best_effort,
        model=model,
        base_url=base_url,
        api_key=api_key,
        max_workers=max_workers,
        llm=llm,
        use_scheduler=use_scheduler,
    )


__all__ = [
    "extract",
    "extract_aggregate",
    "extract_split",
    "extract_harness",
    "deduplicate_chunk_graphs",
    "ExtractionChunk",
]
