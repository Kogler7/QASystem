"""
置信度计算统一入口。
所有置信度相关逻辑集中在这里，后端只需调用 calculate_graph_confidence()。
"""

import sys
from pathlib import Path
from datetime import datetime
from typing import Dict

# 确保当前目录在 sys.path 中，方便内部模块导入
_current_dir = Path(__file__).resolve().parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

from components.text_evidence import TextEvidence
from components.path_evidence import PathEvidence


def calculate_graph_confidence(data: dict) -> dict:
    """
    接收完整 graph JSON（nodes + relationships + ontology_relations），
    为每条关系计算置信度（文本证据 + 路径证据加权平均），
    将 confidence 写入 relationship.properties，并返回完整的 graph。

    参数
    ----
    data : dict
        包含 "graph" 字段的字典：
        {
            "graph": {
                "nodes": [...],
                "relationships": [...],
                "ontology_relations": [...]   # 可选
            }
        }

    返回
    ----
    dict
        {
            "code": 0,
            "info": "置信度评估完成",
            "data": {
                "evaluation_id": "EVAL-...",
                "total_count": 4,
                "graph": {
                    "nodes": [...],
                    "relationships": [...],   # 已附加 properties.confidence
                    "ontology_relations": [...]  # 已附加 properties.confidence
                },
                "details": {}
            }
        }
    """
    graph = data.get('graph', {})
    nodes = graph.get('nodes', [])
    relationships = graph.get('relationships', [])
    ontology_relations = graph.get('ontology_relations', [])

    # 构建 uid -> name 映射（用于提取实体名称作为 subject/object）
    name_map = {}
    for node in nodes:
        uid = node.get('uid', '')
        props = node.get('properties', {})
        name_map[uid] = props.get('name', uid)

    # 初始化两个证据组件（ES 和 Neo4j 连接信息从各自的环境变量读取）
    text_ev = TextEvidence()
    path_ev = PathEvidence()

    all_rels = relationships + ontology_relations

    for rel in all_rels:
        from_uid = rel.get('from_uid', '')
        to_uid = rel.get('to_uid', '')
        pred = rel.get('type', '')

        subj = name_map.get(from_uid, from_uid)
        obj = name_map.get(to_uid, to_uid)

        triple = {'subject': subj, 'predicate': pred, 'object': obj}

        # 计算两个证据分数
        text_score = text_ev.get_score(triple)
        path_score = path_ev.get_score(triple)

        print(f"[置信度计算] {subj} -- {pred} --> {obj}")
        print(f"  文本证据分数: {text_score:.4f}, 路径证据分数: {path_score:.4f}")

        # 加权平均（权重可从 config.yaml 读取）
        confidence = float(round(0.5 * text_score + 0.5 * path_score, 2))

        # 将置信度写入关系属性
        if 'properties' not in rel:
            rel['properties'] = {}
        rel['properties']['confidence'] = confidence

    path_ev.close()

    return {
        "code": 0,
        "info": "置信度评估完成",
        "data": {
            "evaluation_id": f"EVAL-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "total_count": len(all_rels),
            "graph": {
                "nodes": nodes,
                "relationships": relationships,
                "ontology_relations": ontology_relations
            },
            "details": {}
        }
    }