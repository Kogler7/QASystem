import torch
from torch.utils.data import Dataset
from typing import List, Dict
import json


class ConfidenceDataset(Dataset):
    """从知识抽取结果的 graph 中提取关系三元组作为训练样本"""
    
    def __init__(self, data_path: str, config=None):
        self.data = self._load_data(data_path)
        self.config = config
    
    def _load_data(self, path: str) -> List[Dict]:
        """从 graph 中提取关系三元组"""
        with open(path, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)
        
        graph = raw_data.get('graph', {})
        nodes = graph.get('nodes', [])
        relationships = graph.get('relationships', [])
        
        # 构建节点映射
        node_map = {node['uid']: node for node in nodes}
        
        result = []
        for rel in relationships:
            from_node = node_map.get(rel['from_uid'], {})
            to_node = node_map.get(rel['to_uid'], {})
            
            subject = self._extract_node_name(from_node)
            predicate = self._extract_predicate(rel.get('type', ''))
            obj = self._extract_node_name(to_node)
            
            if subject and obj:
                # 从原始数据中获取置信度标签（如果有）
                confidence = rel.get('confidence', 0.5)
                result.append({
                    'triple': {'subject': subject, 'predicate': predicate, 'object': obj},
                    'label': confidence,
                    # 保留完整的节点和关系信息，用于入库
                    'node_info': {
                        'from_node': from_node,
                        'to_node': to_node,
                        'relationship': rel
                    }
                })
        return result
    
    def _extract_node_name(self, node: dict) -> str:
        props = node.get('properties', {})
        if 'name' in props:
            return str(props['name'])
        if '术语名称' in props:
            return str(props['术语名称'])
        if 'RuleId' in props:
            return str(props['RuleId'])
        return node.get('label', '')
    
    def _extract_predicate(self, rel_type: str) -> str:
        if rel_type.startswith('HAS_'):
            return rel_type[4:].lower()
        return rel_type.lower()
    
    def __len__(self):
        return len(self.data)
    
    def __getitem__(self, idx):
        item = self.data[idx]
        triple = item['triple']
        label = torch.tensor(item['label'], dtype=torch.float)
        return triple, label
    
    @staticmethod
    def collate_fn(batch):
        triples = [item[0] for item in batch]
        labels = torch.stack([item[1] for item in batch])
        return triples, labels


def extract_relationships_from_schema_output(data_path: str, confidence_threshold: float = 0.7) -> List[Dict]:
    """
    从 schema_mapper 输出中提取关系，并按置信度阈值筛选
    返回需要通过的关系列表
    """
    with open(data_path, 'r', encoding='utf-8') as f:
        raw_data = json.load(f)
    
    graph = raw_data.get('graph', {})
    nodes = graph.get('nodes', [])
    relationships = graph.get('relationships', [])
    ontology_relations = graph.get('ontology_relations', [])
    
    node_map = {node['uid']: node for node in nodes}
    
    # 存储所有待入库的关系
    relations_to_add = []
    
    # 处理业务关系
    for rel in relationships:
        from_node = node_map.get(rel['from_uid'], {})
        to_node = node_map.get(rel['to_uid'], {})
        
        subject = _extract_name(from_node)
        predicate = _extract_predicate(rel.get('type', ''))
        obj = _extract_name(to_node)
        
        # 获取该关系的置信度（从模型计算或从原始数据获取）
        confidence = rel.get('confidence', 0.5)
        
        if confidence >= confidence_threshold:
            relations_to_add.append({
                'subject': subject,
                'predicate': predicate,
                'object': obj,
                'confidence': confidence,
                'source_node': from_node,
                'target_node': to_node,
                'relationship_type': rel.get('type')
            })
    
    # 处理本体关系
    for rel in ontology_relations:
        from_node = node_map.get(rel['from_uid'], {})
        subject = _extract_name(from_node)
        predicate = 'instance_of'
        obj = rel.get('class_name', '')
        
        confidence = rel.get('confidence', 0.5)
        
        if confidence >= confidence_threshold:
            relations_to_add.append({
                'subject': subject,
                'predicate': predicate,
                'object': obj,
                'confidence': confidence,
                'source_node': from_node,
                'relationship_type': 'INSTANCE_OF'
            })
    
    return relations_to_add


def _extract_name(node: dict) -> str:
    props = node.get('properties', {})
    if 'name' in props:
        return str(props['name'])
    if '术语名称' in props:
        return str(props['术语名称'])
    if 'RuleId' in props:
        return str(props['RuleId'])
    return node.get('label', '')


def _extract_predicate(rel_type: str) -> str:
    if rel_type.startswith('HAS_'):
        return rel_type[4:].lower()
    return rel_type.lower()