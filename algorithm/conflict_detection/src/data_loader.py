from __future__ import annotations

"""High‑level helpers to load, clear, and count data in Neo4j.

This module has been fully migrated from Virtuoso to Neo4j.
All Docker/isql dependencies have been removed.
"""

import os
import logging
from pathlib import Path
from rdflib import Graph

from src.store import neo4j_db  # 使用我们刚刚在 config 中注入的单例
from src.utils.custom_types import Triple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database Management (Neo4j 原生清空)
# ---------------------------------------------------------------------------

def delete_data(_unused_uri: str = None):
    """彻底清空 Neo4j 数据库中的所有节点和关系"""
    logger.info("[Neo4j] 正在执行全库清空 (DETACH DELETE)...")
    cypher = "MATCH (n) DETACH DELETE n"
    try:
        neo4j_db.query(cypher)
        logger.info("✅ 数据库已清空。")
    except Exception as e:
        logger.error(f"❌ 清空数据库失败: {e}")

# ---------------------------------------------------------------------------
# Data Loading (从 TTL 文件加载到 Neo4j)
# ---------------------------------------------------------------------------

def load_ttl_to_neo4j(file_path: str | os.PathLike):
    """解析本地 TTL 文件并将其作为三元组存入 Neo4j"""
    file_path = Path(file_path)
    if not file_path.exists():
        logger.error(f"❌ 找不到数据文件: {file_path}")
        return

    logger.info(f"[LOAD] 正在解析并加载文件: {file_path}")
    
    # 1. 使用 rdflib 解析本地 TTL
    g = Graph()
    g.parse(file_path, format="turtle")
    
    # 2. 将 RDF 三元组转换为 Neo4j 节点和关系
    # 注意：这里我们采用简单的“属性图”映射逻辑
    for s, p, o in g:
        # 提取简短名称
        sub_id = str(s).split('#')[-1].split('/')[-1]
        rel_type = str(p).split('#')[-1].split('/')[-1]
        obj_id = str(o).split('#')[-1].split('/')[-1]

        cypher = f"""
        MERGE (a {{id: $sub_id}})
        MERGE (b {{id: $obj_id}})
        MERGE (a)-[r:{rel_type}]->(b)
        """
        neo4j_db.query(cypher, parameters={"sub_id": sub_id, "obj_id": obj_id})

    logger.info(f"✅ 文件 {file_path.name} 已成功导入图数据库。")

# ---------------------------------------------------------------------------
# Utility Helpers
# ---------------------------------------------------------------------------

def count_triples_in_graph(_unused_uri: str = None) -> int:
    """统计 Neo4j 中的关系总数（近似于 RDF 中的三元组数）"""
    res = neo4j_db.query("MATCH ()-[r]->() RETURN count(r) as c")
    return res[0]['c'] if res else 0

def export_graph_raw(graph_uri: str, out_path: str | os.PathLike):
    """
    (占位函数) Neo4j 导出逻辑通常使用 APOC 或导出为 CSV/JSON。
    这里保留接口以兼容上层调用。
    """
    logger.warning("export_graph_raw 在 Neo4j 环境下暂未实现详细导出逻辑。")
    return str(out_path)

# ---------------------------------------------------------------------------
# Public Main API: load_data (保持接口兼容)
# ---------------------------------------------------------------------------

def load_data(data_file: str, shapes_file: str, data_uri: str = None, shapes_uri: str = None):
    """UpSHACL 标准数据加载入口"""
    logger.info("[LOAD] 正在初始化 Neo4j 数据环境...")
    
    # 1. 清空旧数据
    delete_data()
    
    # 2. 加载数据三元组
    load_ttl_to_neo4j(data_file)
    
    # 3. 加载形状(规则)数据（如果规则也想存进图里的话）
    # load_ttl_to_neo4j(shapes_file) 
    
    # 统计展示
    count = count_triples_in_graph()
    logger.info(f"✨ 加载完成，当前图谱包含 {count} 条关系。")