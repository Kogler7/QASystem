from __future__ import annotations

"""Delta & Reduced Graph Logic for Neo4j.

Calculates which (node, shape) pairs are affected by a delta update 
and extracts the precise neighborhood (Reduced Graph) from Neo4j.
"""

import uuid
import logging
import collections
from typing import Dict, List, Set, Tuple

# 👉 核心替换：使用 neo4j_db
from ..store import neo4j_db
from ..config import NODE_BATCH_SIZE
from .crud import batch as chunker
from .traversal import expand_nodes
from ..shacl_index import ShapeIndex

logger = logging.getLogger(__name__)

AffectedPair = Tuple[str, str]  # (nodeID, shapeID)

# ────────────────────────────────────────────────────────────────
# helper: ID 提取
# ────────────────────────────────────────────────────────────────

def _extract_id(val: any) -> str:
    """从 URI 或 rdflib 对象中提取纯 ID。"""
    return str(val).split('#')[-1].split('/')[-1]

# ────────────────────────────────────────────────────────────────
# 核心函数 1: 计算受影响的 (节点, 形状) 对
# ────────────────────────────────────────────────────────────────

def compute_affected_pairs(delta_insert: List[Tuple], delta_delete: List[Tuple]) -> List[AffectedPair]:
    """
    UpSHACL 算法核心：根据数据增量，计算需要重新验证的节点及其对应的 SHACL 形状。
    """
    idx = ShapeIndex()
    affected_pairs: Set[AffectedPair] = set()

    # 1. 提取变动涉及的初始节点
    subject_nodes = set()
    object_nodes = set()
    for s, p, o in (delta_insert + delta_delete):
        subject_nodes.add(_extract_id(s))
        if isinstance(o, tuple) or str(o).startswith("http"):
            object_nodes.add(_extract_id(o[0] if isinstance(o, tuple) else o))

    initial_nodes = subject_nodes | object_nodes

    # 2. 模式匹配：哪些节点因为属性/关系变动而触发了特定的 Shape？
    # 基于你之前生成的 ShapeIndex 进行内存匹配
    properties = set(idx.node_properties)
    
    # 3. 图扩展：顺着 SHACL 路径找到关联节点
    # 比如：修改了车轮，受影响的可能是整个轮对 (Wheelset)
    expanded_nodes = expand_nodes(initial_nodes, properties)

    # 4. 最终对齐：将受影响节点映射到对应的 SHACL Shape 上
    for shape, targets in idx.target_map.items():
        shape_id = _extract_id(shape)
        # 匹配 targetClass
        for node in expanded_nodes:
            # 简单的类匹配逻辑（在 Neo4j 中查询节点的 Label）
            cypher = "MATCH (n {id: $nid}) RETURN labels(n) as labels"
            res = neo4j_db.query(cypher, parameters={"nid": node})
            if res:
                node_labels = res[0]['labels']
                target_classes = [_extract_id(c) for c in targets.get("classes", [])]
                if any(lbl in target_classes for lbl in node_labels):
                    affected_pairs.add((node, shape_id))

    logger.info(f"✨ [Delta] 识别到 {len(affected_pairs)} 个受影响的验证任务。")
    return list(affected_pairs)

# ────────────────────────────────────────────────────────────────
# 核心函数 2: 构建缩减子图 (Reduced Graph)
# ────────────────────────────────────────────────────────────────

def build_reduced_graphs(affected_pairs: List[AffectedPair]) -> List[Tuple]:
    """
    从 Neo4j 中精准切取受影响节点周围的子图，作为 Clingo 的推演事实。
    """
    if not affected_pairs:
        return []

    idx = ShapeIndex()
    # 映射：每个 Shape 关注哪些属性关系
    allowed_props_map = idx.shape_triple_patterns
    
    visited_nodes = {p[0] for p in affected_pairs}
    all_triples = []

    # 🚀 使用 Cypher 批量拉取子图
    # 逻辑：对于受影响节点，只拉取 SHACL 规则里提到的那几种关系（最小影响子图）
    for node_id in visited_nodes:
        # 获取该节点涉及的所有 Shape 所关注的关系类型
        relevant_shapes = [p[1] for p in affected_pairs if p[0] == node_id]
        rel_types = set()
        for s in relevant_shapes:
            # 这里简化处理：获取所有可能的关系
            rel_types.update([_extract_id(p) for p in idx.path_predicates])
        
        rel_filter = "|".join(rel_types) if rel_types else "*"

        cypher = f"""
        MATCH (s {{id: $sid}})-[r:{rel_filter}]-(o)
        RETURN s.id AS s, type(r) AS p, o.id AS o
        """
        results = neo4j_db.query(cypher, parameters={"sid": node_id})
        for row in results:
            all_triples.append((row['s'], row['p'], (row['o'], "uri", None)))

    logger.info(f"📦 [Delta] 子图构建完成，包含 {len(all_triples)} 条关键三元组。")
    return list(set(all_triples))