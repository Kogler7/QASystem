"""知识抽取算法包。

目录约定（运行版本由 ``KG_EXTRACT_ALGO_VERSION`` 选择，见 ``version.py``）：

- ``v1/``：旧 batch 契约（batch / extractor / ontology_schema / scheduler / uml）
- ``v2/``：V2 聚合流水线
- ``v3/``：V3 聚合流水线
- ``latest/``：当前默认版本（独立代码副本）
- ``jev_extract/``：latest 的实验副本，仅 ``KG_EXTRACT_ALGO_VERSION=jev`` 时进入
- ``version.py``：版本路由

为兼容历史调用，顶层仍重导出 V1 的公开 API；新代码请直接
``from algorithm.knowledge_extract.v1 import ...``。
"""
from .v1 import (  # noqa: F401
    deduplicate_chunk_graphs,
    extract_chunks,
    extract_v2,
    summarize_chunks,
    summarize_text,
)
from .v1 import (  # noqa: F401
    get_dimension_scales,
    init_knowledge_map_spaces,
    init_knowledge_space,
    init_knowledge_space_tree,
    store_extraction_to_neo4j,
)

__all__ = [
    "extract_v2",
    "extract_chunks",
    "deduplicate_chunk_graphs",
    "summarize_chunks",
    "summarize_text",
    "get_dimension_scales",
    "init_knowledge_map_spaces",
    "init_knowledge_space",
    "init_knowledge_space_tree",
    "store_extraction_to_neo4j",
]
