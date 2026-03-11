from __future__ import annotations
from rdflib import Node

"""Basic create/read/update/delete helpers that talk to Neo4j.

* **stateless** – uses neo4j_db singleton
* All heavy shape‑specific logic lives in ``shape_index``.
"""

from typing import Generator, List
import random
import logging

# 👉 核心替换：使用 neo4j_db 替代 virtuoso
from src.store import neo4j_db
from src.config import MAX_BATCH_SIZE
from src.utils.custom_types import Triple

logger = logging.getLogger(__name__)

__all__ = [
    "batch",
    "cleanup_temp_graphs",
    "count_triples_in_graph",
    "sample_triples_from_graph",
    "insert_triples",
    "delete_triples",
]

# ────────────────────────────────────────────────────────────────
# helper: batching iterator (保留原逻辑)
# ────────────────────────────────────────────────────────────────

def batch(values: List[any], size: int) -> Generator[List[any], None, None]:
    """Yield successive *non‑empty* slices of *values* of length ≤ *size*."""
    for i in range(0, len(values), size):
        yield values[i : i + size]

# ────────────────────────────────────────────────────────────────
# housekeeping helpers (针对 Neo4j 适配)
# ────────────────────────────────────────────────────────────────

def cleanup_temp_graphs() -> None:
    """在 Neo4j 中，我们通过删除特定前缀或所有节点来模拟清理。"""
    # 模拟原有的 DROP GRAPH 行为：清空全库
    neo4j_db.query("MATCH (n) DETACH DELETE n")
    print("[crud] Neo4j database cleared (simulating temp graph drop)")


def count_triples_in_graph(graph_uri: str = None) -> int:
    """统计 Neo4j 中的关系总数（等同于三元组数）。"""
    res = neo4j_db.query("MATCH ()-[r]->() RETURN count(r) as c")
    count = res[0]['c'] if res else 0
    print(f"[crud] Neo4j count -> {count} relations")
    return count

# ────────────────────────────────────────────────────────────────
# random sampling (适配 Cypher)
# ────────────────────────────────────────────────────────────────

def sample_triples_from_graph(graph_uri: str, sample_size: int) -> List[Triple]:
    """使用 Cypher 的 SKIP 和 LIMIT 进行随机采样。"""
    total = count_triples_in_graph()
    if total == 0:
        return []

    offset = random.randint(0, max(0, total - sample_size))
    limit = min(sample_size, total)

    # Cypher 采样查询
    q = """
    MATCH (s)-[r]->(o)
    RETURN s.id as s, type(r) as p, o.id as o
    SKIP $offset LIMIT $limit
    """
    res = neo4j_db.query(q, parameters={"offset": offset, "limit": limit})
    
    triples: List[Triple] = []
    for row in res:
        # 保持返回格式兼容：(s, p, (o, "uri", None))
        triples.append((row['s'], row['p'], (row['o'], "uri", None)))

    print(f"[crud] sampled {len(triples)} / {limit} relations from Neo4j")
    return triples

# ────────────────────────────────────────────────────────────────
# mutating helpers – 核心逻辑重写
# ────────────────────────────────────────────────────────────────

def _extract_id(node: any) -> str:
    """辅助函数：将 rdflib.Node 或字符串转换为纯 ID 字符串。"""
    val = str(node)
    return val.split('#')[-1].split('/')[-1]

def insert_triples(graph_uri: str, triples: List[tuple]):
    """将三元组批量存入 Neo4j。"""
    if not triples:
        return
    
    print(f"[crud] inserting {len(triples)} triples into Neo4j")
    
    # 针对 Neo4j 优化：分批执行 MERGE
    for part in batch(triples, 500):
        for s, p, o in part:
            sub_id = _extract_id(s)
            rel_type = _extract_id(p)
            # 处理 o 可能是一个元组 (val, type, lang) 的情况
            obj_val = o[0] if isinstance(o, tuple) else o
            obj_id = _extract_id(obj_val)

            cypher = f"""
            MERGE (a:Entity {{id: $sid}})
            MERGE (b:Entity {{id: $oid}})
            MERGE (a)-[r:{rel_type}]->(b)
            """
            neo4j_db.query(cypher, parameters={"sid": sub_id, "oid": obj_id})


def delete_triples(graph_uri: str, triples: List[Triple]):
    """从 Neo4j 中批量删除关系。"""
    if not triples:
        return

    print(f"[crud] deleting {len(triples)} triples from Neo4j")
    
    for part in batch(triples, 500):
        for s, p, o in part:
            sub_id = _extract_id(s)
            rel_type = _extract_id(p)
            obj_val = o[0] if isinstance(o, tuple) else o
            obj_id = _extract_id(obj_val)

            cypher = f"""
            MATCH (a {{id: $sid}})-[r:{rel_type}]->(b {{id: $oid}})
            DELETE r
            """
            neo4j_db.query(cypher, parameters={"sid": sub_id, "oid": obj_id})