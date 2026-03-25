"""
知识抽取结果解析器
用于从 schema_mapper 输出的 JSON 中提取三元组
"""

from typing import Dict, Any, List, Tuple


def extract_node_name(node: Dict[str, Any], rel_type: str = None) -> str:
    """
    根据节点标签和关系类型提取合适的名称/值
    
    Args:
        node: 节点对象，包含 uid, label, properties
        rel_type: 关系类型（如 HAS_CHARACTERISTIC, HAS_TERMDEFINITION 等）
    
    Returns:
        提取的节点名称/值
    """
    props = node.get('properties', {})
    label = node.get('label', '')
    
    # TermDefinition 节点：提取上位概念（核心概念）
    if label == 'TermDefinition':
        if '上位概念' in props:
            return str(props['上位概念'])
        if '区别特征' in props:
            return str(props['区别特征'])
        return 'TermDefinition'
    
    # Characteristic 节点：优先使用特征描述，没有则用特征类型
    if label == 'Characteristic':
        if '特征描述' in props and props['特征描述']:
            return str(props['特征描述'])
        if '特征类型' in props and props['特征类型']:
            return str(props['特征类型'])
        return 'Characteristic'
    
    # Term 节点：提取术语名称
    if label == 'Term':
        if '术语名称' in props:
            return str(props['术语名称'])
        if 'name' in props:
            return str(props['name'])
        return label
    
    # SystemElement 节点：提取 name
    if label == 'SystemElement':
        if 'name' in props:
            return str(props['name'])
        return label
    
    # RuleType 节点：提取 RuleId
    if label == 'RuleType':
        if 'RuleId' in props:
            return str(props['RuleId'])
        return label
    
    # 其他节点：优先 name，否则用标签
    if 'name' in props:
        return str(props['name'])
    if '术语名称' in props:
        return str(props['术语名称'])
    if 'RuleId' in props:
        return str(props['RuleId'])
    if 'id' in props:
        return str(props['id'])
    
    return label


def extract_relations_from_graph(
    graph: Dict[str, Any]
) -> Tuple[List[Dict], List[Dict]]:
    """
    从 graph 中提取关系三元组
    
    Args:
        graph: schema_mapper 输出的 graph 结构
    
    Returns:
        (relations_to_evaluate, relation_info)
    """
    nodes = graph.get('nodes', [])
    relationships = graph.get('relationships', [])
    ontology_relations = graph.get('ontology_relations', [])
    
    node_map = {node['uid']: node for node in nodes}
    
    relations_to_evaluate = []
    relation_info = []
    
    # 处理业务关系
    for rel in relationships:
        from_node = node_map.get(rel['from_uid'], {})
        to_node = node_map.get(rel['to_uid'], {})
        
        subject = extract_node_name(from_node)
        predicate = rel.get('type', '')  # 直接使用原始关系类型
        obj = extract_node_name(to_node, predicate)
        
        if subject and obj:
            relations_to_evaluate.append({
                'subject': subject,
                'predicate': predicate,
                'object': obj
            })
            relation_info.append({
                'type': 'relationship',
                'subject': subject,
                'predicate': predicate,
                'object': obj,
                'source_node': from_node,
                'target_node': to_node,
                'original_relationship': rel
            })
    
    # 处理本体关系
    for rel in ontology_relations:
        from_node = node_map.get(rel['from_uid'], {})
        subject = extract_node_name(from_node)
        predicate = rel.get('type', '')  # 直接使用原始关系类型
        obj = rel.get('class_name', '')
        
        if subject and obj:
            relations_to_evaluate.append({
                'subject': subject,
                'predicate': predicate,
                'object': obj
            })
            relation_info.append({
                'type': 'ontology',
                'subject': subject,
                'predicate': predicate,
                'object': obj,
                'source_node': from_node,
                'original_relationship': rel
            })
    
    return relations_to_evaluate, relation_info