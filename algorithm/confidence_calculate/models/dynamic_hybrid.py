import torch
import torch.nn as nn
from typing import Dict
from components.text_evidence import TextEvidence
from components.kge_evidence import KGEEvidence  
from components.path_evidence import PathEvidence

import base_model
BaseConfidenceModel = base_model.BaseConfidenceModel


class DynamicHybridModel(BaseConfidenceModel):
    def __init__(self, config):
        super().__init__(config)
        self.name = 'DynamicHybrid'
        
        # 初始化三大组件
        self.text_evidence = TextEvidence(config.get('text', {}))
        self.kg_evidence = KGEEvidence(config.get('kg', {}))  
        self.path_evidence = PathEvidence(config.get('path', {}))
        
        # 特征维度 - 从配置读取
        self.text_dim = config.get('text_dim', 128)
        self.kg_dim = config.get('kg_dim', 128)
        self.path_dim = config.get('path_dim', 64)
        
        print(f"配置维度: text={self.text_dim}, kg={self.kg_dim}, path={self.path_dim}")
        
        # 融合层 - 移除BatchNorm
        fusion_input_dim = self.text_dim + self.kg_dim + self.path_dim + 3
        print(f"融合层输入维度: {fusion_input_dim}")
        
        self.fusion_layer = nn.Sequential(
            nn.Linear(fusion_input_dim, 256),
            nn.ReLU(),
            nn.Dropout(0.5),
            nn.Linear(256, 128),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(128, 1),
            nn.Sigmoid()
        )
        
        self.score_weights = nn.Parameter(torch.ones(3) / 3)
    
    def forward_triples(self, triple_data):
        if isinstance(triple_data, dict):
            return self._forward_single(triple_data)
        else:
            predictions = []
            for triple in triple_data:
                pred = self._forward_single(triple)
                predictions.append(pred)
            return torch.stack(predictions)
    
    def _forward_single(self, triple: Dict) -> torch.Tensor:
        text_features = self.text_evidence(triple)
        kg_features = self.kg_evidence(triple)
        path_output = self.path_evidence(triple)
        path_features = path_output['path_features']
        
        # 确保所有特征在同一设备
        device = text_features.device
        
        text_score = self._compute_text_confidence(text_features)
        kg_score = self._compute_kg_confidence(kg_features)
        path_score = path_output['aggregate_score']
        
        # 将分数也移到正确设备
        scores_tensor = torch.tensor([text_score, kg_score, path_score], device=device)
        
        combined_features = torch.cat([
            text_features,
            kg_features,
            path_features,
            scores_tensor
        ])
        
        confidence = self.fusion_layer(combined_features.unsqueeze(0))
        return confidence.squeeze(0)
    
    def _compute_text_confidence(self, text_features: torch.Tensor) -> float:
        return float(torch.sigmoid(text_features.mean()))
    
    def _compute_kg_confidence(self, kg_features: torch.Tensor) -> float:
        return float(torch.sigmoid(kg_features.mean()))
    
    def get_component_scores(self, triple: Dict) -> Dict:
        with torch.no_grad():
            text_features = self.text_evidence(triple)
            kg_features = self.kg_evidence(triple)
            path_output = self.path_evidence(triple)
            
            text_score = self._compute_text_confidence(text_features)
            kg_score = self._compute_kg_confidence(kg_features)
            path_score = path_output['aggregate_score']
            
            final = self.forward_triples(triple)
            
            return {
                'text_score': text_score,
                'kg_score': kg_score,
                'path_score': path_score,
                'final_confidence': float(final),
                'best_path': path_output['best_path'],
                'path_scores': path_output['path_scores']
            }