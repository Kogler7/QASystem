from __future__ import annotations
from rdflib import Node

"""Graph‑traversal helpers that expand nodes / shapes.

Modified for Neo4j: No SPARQL, pure Cypher traversal for minimal impact subgraphs.
"""

from typing import Dict, Generator, List, Set, Tuple
from urllib.parse import urlparse
import logging

# 👉 核心替换：使用 store 中的 neo4j_db
from ..store import neo4j_db
from ..config import NODE_BATCH_SIZE, CLASS_BATCH_SIZE

logger = logging.getLogger(__name__)

__all__ = [
    "is_valid_uri",
    "expand_nodes",
    "expand_shapes",
]

# ────────────────────────────────────────────────────────────────
# URI utils (保持原样，用于基础验证)
# ────────────────────────────────────────────────────────────────

def is_valid_uri(uri: str) -> bool:
    """Cheap URI validator: *scheme* and *netloc* must be present."""
    try:
        parsed = urlparse(uri)
        return bool(parsed.scheme and parsed.netloc)
    except Exception:
        return False

def _extract_id(val: str) -> str:
    """辅助函数：从 URI 中提取最后的 ID 部分。"""
    return val.split('#')[-1].split('/')[-1]

# ────────────────────────────────────────────────────────────────
# Node expansion (↗ 使用 Neo4j 变长路径实现)
# ────────────────────────────────────────────────────────────────

def expand_nodes(initial_nodes, properties):
    # properties 可能是 {'speed', 'has_status', 'MANUFACTURED_BY'}
    preds = list(properties)
    
    # 👉 关键修改：匹配 :RELATION 标签，然后过滤 type 属性
    q = """
    MATCH (s)-[r:RELATION]->(o)
    WHERE s.id IN $node_slice 
      AND r.type IN $preds
    RETURN DISTINCT o.id AS oid
    """
    
    # 确保调用 query 时传入了 preds 参数
    res = neo4j_db.query(q, parameters={
        "node_slice": list(initial_nodes),
        "preds": preds
    })
    return {row['oid'] for row in res}

# ────────────────────────────────────────────────────────────────
# Shape expansion (针对本地 SHACL 逻辑优化)
# ────────────────────────────────────────────────────────────────

def expand_shapes(initial_shapes: Set[str]) -> Tuple[Set[str], Set[str]]:
    """
    递归收集所有可达的 Node-Shapes 和 Property-Shapes。
    
    注：因为我们采用“路线 A”，Shapes 的元数据关系（sh:property/sh:node）
    通常在本地 RDFLib 图中解析，不需要去 Neo4j 查。
    这里保留接口并使用 neo4j_db 模拟查询（如果你把规则也存进了 Neo4j）。
    """
    node_shapes: Set[str] = set(initial_shapes)
    property_shapes: Set[str] = set()
    frontier = set(initial_shapes)

    while frontier:
        next_frontier: Set[str] = set()
        
        # 将 URI 转为 ID
        batch_shapes = [_extract_id(s) for s in frontier]

        # (1) 查询 sh:property 关系
        q_prop = """
        MATCH (s {id: $sid})-[:HAS_PROPERTY]->(ps)
        RETURN ps.id AS psid
        """
        for sid in batch_shapes:
            res_prop = neo4j_db.query(q_prop, parameters={"sid": sid})
            new_ps_ids = {row["psid"] for row in res_prop}
            property_shapes.update(new_ps_ids)

            # (2) 查询嵌套的 sh:node 引用
            if new_ps_ids:
                q_node = """
                MATCH (ps {id: $psid})-[:HAS_NODE]->(ns)
                RETURN ns.id AS nsid
                """
                for psid in new_ps_ids:
                    res_node = neo4j_db.query(q_node, parameters={"psid": psid})
                    for row in res_node:
                        nsid = row["nsid"]
                        if nsid not in node_shapes:
                            next_frontier.add(nsid)
        
        frontier = next_frontier
        node_shapes.update(frontier)

    return node_shapes, property_shapes