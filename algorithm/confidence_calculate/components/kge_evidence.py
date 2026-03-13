import torch
import torch.nn as nn
import numpy as np
from typing import Dict, Optional


class KGEEvidence(nn.Module):
    """
    知识图谱嵌入证据组件
    """
    def __init__(self, config):
        super().__init__()
        self.embedding_dim = config.get('kg_embedding_dim', 100)
        
        # 预训练的实体和关系嵌入
        self.entity_embeddings = self._load_entity_embeddings(config)
        self.relation_embeddings = self._load_relation_embeddings(config)
        
        # 特征处理层 - 移除BatchNorm
        self.feature_layer = nn.Sequential(
            nn.Linear(self.embedding_dim * 3, 256),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(256, 128),
            nn.ReLU()
        )
        
    def _load_entity_embeddings(self, config):
        """加载实体嵌入"""
        path = config.get('entity_emb_path', 'embeddings/entities.pt')
        try:
            return torch.load(path, map_location='cpu', weights_only=False)
        except Exception as e:
            err_msg = str(e).lower()
            if "deserializ" in err_msg or "incomplete metadata" in err_msg or "file not fully covered" in err_msg:
                raise RuntimeError(
                    f"实体嵌入文件不完整或损坏: {path}（{e}）。请重新训练 TransE 嵌入："
                    " 在 algorithm/confidence_calculate 下执行 python train_transe_embeddings.py"
                ) from e
            return None

    def _load_relation_embeddings(self, config):
        """加载关系嵌入"""
        path = config.get('relation_emb_path', 'embeddings/relations.pt')
        try:
            return torch.load(path, map_location='cpu', weights_only=False)
        except Exception as e:
            err_msg = str(e).lower()
            if "deserializ" in err_msg or "incomplete metadata" in err_msg or "file not fully covered" in err_msg:
                raise RuntimeError(
                    f"关系嵌入文件不完整或损坏: {path}（{e}）。请重新训练 TransE 嵌入："
                    " 在 algorithm/confidence_calculate 下执行 python train_transe_embeddings.py"
                ) from e
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