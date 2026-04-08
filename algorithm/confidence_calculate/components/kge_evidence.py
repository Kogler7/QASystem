import os
import yaml
import torch
import torch.nn as nn
import numpy as np
from typing import Dict, Optional
from pathlib import Path
from dotenv import load_dotenv

# 加载 backend/.env
env_path = Path(__file__).parent.parent.parent.parent / 'backend' / '.env'
load_dotenv(env_path)


class KGEEvidence(nn.Module):
    """
    知识图谱嵌入证据组件 - 从 config.yaml 加载模型参数
    """
    def __init__(self, config: Optional[Dict] = None, config_path: Optional[str] = None):
        super().__init__()
        
        # 加载 config.yaml
        if config_path is None:
            config_path = Path(__file__).parent.parent / 'config.yaml'
        
        with open(config_path, 'r', encoding='utf-8') as f:
            yaml_config = yaml.safe_load(f)
        
        kg_config = yaml_config.get('kg', {})
        
        # 合并传入的 config（如果有）
        if config:
            kg_config.update(config)
        
        self.embedding_dim = kg_config.get('kg_embedding_dim', 100)
        
        # 从 config.yaml 读取路径，支持环境变量覆盖
        entity_emb_path = kg_config.get('entity_emb_path', 'embeddings/entities.pt')
        relation_emb_path = kg_config.get('relation_emb_path', 'embeddings/relations.pt')
        
        # 如果是相对路径，转换为绝对路径
        if not os.path.isabs(entity_emb_path):
            entity_emb_path = str(Path(__file__).parent.parent / entity_emb_path)
        if not os.path.isabs(relation_emb_path):
            relation_emb_path = str(Path(__file__).parent.parent / relation_emb_path)
        
        # 预训练的实体和关系嵌入
        self.entity_embeddings = self._load_entity_embeddings(entity_emb_path)
        self.relation_embeddings = self._load_relation_embeddings(relation_emb_path)
        
        # 特征处理层 - 移除BatchNorm
        self.feature_layer = nn.Sequential(
            nn.Linear(self.embedding_dim * 3, 256),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(256, 128),
            nn.ReLU()
        )
        
    def _load_entity_embeddings(self, path: str):
        """加载实体嵌入"""
        try:
            return torch.load(path, map_location='cpu', weights_only=False)
        except Exception as e:
            err_msg = str(e).lower()
            if ("deserializ" in err_msg or "incomplete metadata" in err_msg or "file not fully covered" in err_msg
                    or "pytorchstreamreader" in err_msg or "central directory" in err_msg or "zip archive" in err_msg):
                raise RuntimeError(
                    f"实体嵌入文件不完整或损坏: {path}（{e}）。请重新训练 TransE 嵌入"
                ) from e
            print(f"实体嵌入加载失败: {e}，将使用随机嵌入")
            return None

    def _load_relation_embeddings(self, path: str):
        """加载关系嵌入"""
        try:
            return torch.load(path, map_location='cpu', weights_only=False)
        except Exception as e:
            err_msg = str(e).lower()
            if ("deserializ" in err_msg or "incomplete metadata" in err_msg or "file not fully covered" in err_msg
                    or "pytorchstreamreader" in err_msg or "central directory" in err_msg or "zip archive" in err_msg):
                raise RuntimeError(
                    f"关系嵌入文件不完整或损坏: {path}（{e}）。请重新训练 TransE 嵌入"
                ) from e
            print(f"关系嵌入加载失败: {e}，将使用随机嵌入")
            return None
    
    def _lookup_entity(self, entity_name: str) -> torch.Tensor:
        """动态查找或计算实体嵌入"""
        if self.entity_embeddings is not None and entity_name in self.entity_embeddings:
            return self.entity_embeddings[entity_name]
        else:
            return torch.randn(self.embedding_dim) * 0.1
    
    def _lookup_relation(self, relation_name: str) -> torch.Tensor:
        """动态查找或计算关系嵌入"""
        if self.relation_embeddings is not None and relation_name in self.relation_embeddings:
            return self.relation_embeddings[relation_name]
        else:
            return torch.randn(self.embedding_dim) * 0.1
    
    def forward(self, triple: Dict) -> torch.Tensor:
        """
        前向传播，返回处理后的KG特征
        """
        subject = triple.get('subject', '')
        obj = triple.get('object', '')
        pred = triple.get('predicate', '')
        
        # 动态获取嵌入
        s_emb = self._lookup_entity(subject)
        p_emb = self._lookup_relation(pred)
        o_emb = self._lookup_entity(obj)
        
        # 确保嵌入在正确设备上
        device = next(self.parameters()).device
        s_emb = s_emb.to(device)
        p_emb = p_emb.to(device)
        o_emb = o_emb.to(device)
        
        # 拼接
        combined = torch.cat([s_emb, p_emb, o_emb])
        
        # 通过特征层
        features = self.feature_layer(combined.unsqueeze(0))
        
        return features.squeeze(0)
    
    def get_confidence_score(self, triple: Dict) -> float:
        """获取 KG 置信度分数"""
        with torch.no_grad():
            features = self.forward(triple)
            score = torch.sigmoid(features.mean())
            return score.item()