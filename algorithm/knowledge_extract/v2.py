"""v2 抽取算法的统一入口（shim）。

实际实现在 ``algorithm.knowledge_extract_v2``；本模块只提供与 v3 / latest 一致的调用签名。
"""
from __future__ import annotations

from typing import Any, Iterable


def extract(
    ontology: str,
    chunks: Iterable[Any],
    *,
    target_types: list[str] | None = None,
    validation_mode: str = "permissive",
    best_effort: bool = True,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_workers: int | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Run the V2 aggregate pipeline over ``chunks``."""
    from algorithm.knowledge_extract_v2 import extract_harness

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
    )
