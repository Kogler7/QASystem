"""
知识空间与知识抽取 V2 模块。
- 知识空间初始化：创建 KnowledgeSpace 节点，连接 Dimension/Scale
- 知识抽取 V2：本体驱动的 LLM 抽取（无模板匹配）
- 抽取结果入库：将 graph 结构存入 Neo4j（按 uid 去重）
"""
from .knowledge_space import init_knowledge_space, init_knowledge_space_tree, init_knowledge_map_spaces, get_dimension_scales, store_extraction_to_neo4j
from .extractor import extract_v2

__all__ = [
    "init_knowledge_space",
    "init_knowledge_space_tree",
    "init_knowledge_map_spaces",
    "get_dimension_scales",
    "extract_v2",
    "store_extraction_to_neo4j",
]
