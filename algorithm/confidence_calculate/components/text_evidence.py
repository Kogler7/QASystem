import numpy as np
from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer
from typing import Dict
import torch
import torch.nn as nn


class TextEvidence(nn.Module):
    """
    文本证据模块 
    """
    def __init__(self, config):
        super().__init__()
        
        # 获取 ES 配置
        es_hosts = config.get('es_hosts', ['https://10.126.62.88:9200'])
        es_timeout = config.get('es_timeout', 30)
        es_username = config.get('es_username', None)
        es_password = config.get('es_password', None)
        es_verify_certs = config.get('es_verify_certs', False)
        
        # ES8 客户端连接配置
        es_kwargs = {
            'hosts': es_hosts,
            'request_timeout': es_timeout,
            'verify_certs': es_verify_certs,
            'retry_on_timeout': True,
            'max_retries': 3,
        }
        
        # 添加认证信息
        if es_username and es_password:
            es_kwargs['basic_auth'] = (es_username, es_password)
        
        # 初始化 Elasticsearch 连接
        self.es = Elasticsearch(**es_kwargs)
        
        self.index_name = config.get('es_index', 'documents')
        self.size = config.get('es_size', 50)
        self.top_k = config.get('text_top_k', 10)
        self.max_bm25 = config.get('max_bm25_norm', 100.0)
        
        # 加载 SBERT 模型
        model_path = config.get('sbert_model_path', 'paraphrase-multilingual-MiniLM-L12-v2')
        print(f"加载SBERT模型从: {model_path}")
        try:
            self.sbert = SentenceTransformer(model_path)
        except Exception as e:
            err_msg = str(e).lower()
            if "deserializ" in err_msg or "incomplete metadata" in err_msg or "file not fully covered" in err_msg:
                raise RuntimeError(
                    f"SBERT 模型文件不完整或损坏（{e}）。请删除目录 {model_path} 后重新下载："
                    " 在 algorithm/confidence_calculate 下执行 python temp_utils/download_sbert.py"
                ) from e
            raise
        self.embed_dim = self.sbert.get_sentence_embedding_dimension()
        
        # 特征处理层 - 移除BatchNorm
        self.feature_layer = nn.Sequential(
            nn.Linear(self.top_k * (self.embed_dim + 1), 256),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(256, 128),
            nn.ReLU()
        )
        
        # 测试连接
        try:
            if not self.es.ping():
                print("警告: Elasticsearch 连接失败")
        except Exception as e:
            print(f"Elasticsearch 连接异常: {e}")
    
    def build_query(self, triple):
        """根据三元组构造查询字符串"""
        subj = triple.get("subject", "")
        pred = triple.get("predicate", "")
        obj = triple.get("object", "")
        return f"{subj} {pred} {obj}"
    
    def get_text_features(self, triple):
        """
        返回文本特征向量 - 适配 ES8
        """
        query_str = self.build_query(triple)
        if not query_str.strip():
            return np.zeros(self.top_k * (self.embed_dim + 1))
        
        # 1. 在 Elasticsearch 中执行 BM25 检索
        try:
            # ES8 使用 query 参数替代 body
            response = self.es.search(
                index=self.index_name,
                query={"match": {"content": query_str}},
                size=self.size
            )
        except Exception as e:
            print(f"ES 检索失败: {e}")
            return np.zeros(self.top_k * (self.embed_dim + 1))
        
        # ES8 响应结构与旧版本兼容
        hits = response["hits"]["hits"]
        features = []
        
        # 处理实际命中
        for hit in hits[:self.top_k]:
            content = hit["_source"].get("content", "")
            if content:
                vec = self.sbert.encode(content)
                bm25_raw = hit["_score"]
                bm25_norm = min(bm25_raw / self.max_bm25, 1.0)
            else:
                vec = np.zeros(self.embed_dim)
                bm25_norm = 0.0
            
            features.append(vec)
            features.append(np.array([bm25_norm]))
        
        # 用零填充
        while len(features) < self.top_k * 2:
            features.append(np.zeros(self.embed_dim))
            features.append(np.array([0.0]))
        
        # 合并
        concatenated = np.concatenate(features)
        return concatenated
    
    def forward(self, triple: Dict) -> torch.Tensor:
        """
        前向传播，返回处理后的文本特征
        """
        features = self.get_text_features(triple)
        features_tensor = torch.FloatTensor(features).unsqueeze(0)
        
        # 获取模型所在设备
        device = next(self.parameters()).device
        features_tensor = features_tensor.to(device)
        
        processed = self.feature_layer(features_tensor)
        return processed.squeeze(0)
    
    def _compute_text_confidence(self, text_features: torch.Tensor) -> float:
        """
        从文本特征计算置信度
        """
        return float(torch.sigmoid(text_features.mean()))