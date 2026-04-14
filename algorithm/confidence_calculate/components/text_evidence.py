import os
import yaml
import numpy as np
from pathlib import Path
from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer
from typing import Dict, Optional
import torch
import torch.nn as nn
from dotenv import load_dotenv
from pathlib import Path

# 加载 backend/.env
env_path = Path(__file__).parent.parent.parent.parent / 'backend' / '.env'
load_dotenv(env_path)

_CC_ROOT = Path(__file__).resolve().parent.parent


class TextEvidence(nn.Module):
    """
    文本证据模块 - 从 config.yaml 加载模型参数，从 backend/.env 加载连接配置
    """
    def __init__(self, config: Optional[Dict] = None, config_path: Optional[str] = None):
        super().__init__()
        
        # 加载 config.yaml
        if config_path is None:
            config_path = Path(__file__).parent.parent / 'config.yaml'
        
        with open(config_path, 'r', encoding='utf-8') as f:
            yaml_config = yaml.safe_load(f)
        
        # 合并传入的 config（如果有）
        text_config = yaml_config.get('text', {})
        if config:
            text_config.update(config)
        
        # 从环境变量读取连接配置
        es_hosts = os.getenv('ES_HOSTS', 'http://127.0.0.1:9200')
        if ',' in es_hosts:
            es_hosts = [h.strip() for h in es_hosts.split(',')]
        else:
            es_hosts = [es_hosts]
        
        es_user = os.getenv('ES_USER')
        es_password = os.getenv('ES_PASSWORD')
        es_verify_certs = os.getenv('ES_VERIFY_CERTS', 'false').lower() == 'true'
        
        # 从环境变量读取索引名
        self.index_name = os.getenv('ES_INDEX', 'documents')
        
        # 从 config.yaml 读取业务参数
        self.size = text_config.get('es_size', 50)
        self.top_k = text_config.get('text_top_k', 10)
        self.max_bm25 = text_config.get('max_bm25_norm', 100.0)
        
        # 构建 ES 连接
        es_kwargs = {
            'hosts': es_hosts,
            'request_timeout': text_config.get('es_timeout', 30),
            'retry_on_timeout': True,
            'max_retries': 3,
            'verify_certs': es_verify_certs,
        }
        
        if es_user and es_password:
            es_kwargs['basic_auth'] = (es_user, es_password)
        
        # 初始化 Elasticsearch
        try:
            self.es = Elasticsearch(**es_kwargs)
            if not self.es.ping():
                print("警告: Elasticsearch 连接失败")
            else:
                print(f"Elasticsearch 连接成功: {es_hosts}")
        except Exception as e:
            print(f"Elasticsearch 连接异常: {e}")
            self.es = None
        
        # 从环境变量读取 SBERT 模型路径（默认指向仓库根 models 目录）
        model_path = os.getenv('SBERT_MODEL_PATH', str(_CC_ROOT.parent.parent / 'models' / 'sbert_model'))
        
        print(f"加载 SBERT 模型: {model_path}")
        
        if not os.path.exists(model_path):
            print(f"本地模型不存在: {model_path}")
            print("使用 HuggingFace 默认模型: paraphrase-multilingual-MiniLM-L12-v2")
            self.sbert = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
        else:
            try:
                self.sbert = SentenceTransformer(model_path)
                print(f"SBERT 模型加载成功")
            except Exception as e:
                print(f"SBERT 模型加载失败: {e}")
                print("使用 HuggingFace 默认模型: paraphrase-multilingual-MiniLM-L12-v2")
                self.sbert = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
        
        self.embed_dim = self.sbert.get_sentence_embedding_dimension()
        
        # 特征处理层（使用 config.yaml 中的维度）
        text_dim = yaml_config.get('text_dim', 128)
        self.feature_layer = nn.Sequential(
            nn.Linear(self.top_k * (self.embed_dim + 1), 256),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(256, text_dim),
            nn.ReLU()
        )
    
    def build_query(self, triple: Dict) -> str:
        """根据三元组构造查询字符串"""
        subj = triple.get("subject", "")
        pred = triple.get("predicate", "")
        obj = triple.get("object", "")
        return f"{subj} {pred} {obj}"
    
    def get_text_features(self, triple: Dict) -> np.ndarray:
        """返回文本特征向量"""
        query_str = self.build_query(triple)
        if not query_str.strip() or self.es is None:
            return np.zeros(self.top_k * (self.embed_dim + 1))
        
        try:
            response = self.es.search(
                index=self.index_name,
                query={"match": {"content": query_str}},
                size=self.size
            )
        except Exception as e:
            print(f"ES 检索失败: {e}")
            return np.zeros(self.top_k * (self.embed_dim + 1))
        
        hits = response["hits"]["hits"]
        features = []
        
        for hit in hits[:self.top_k]:
            content = hit["_source"].get("content", "")
            if content:
                vec = self.sbert.encode(content)
                bm25_norm = min(hit["_score"] / self.max_bm25, 1.0)
            else:
                vec = np.zeros(self.embed_dim)
                bm25_norm = 0.0
            
            features.append(vec)
            features.append(np.array([bm25_norm]))
        
        # 填充
        while len(features) < self.top_k * 2:
            features.append(np.zeros(self.embed_dim))
            features.append(np.array([0.0]))
        
        return np.concatenate(features)
    
    def forward(self, triple: Dict) -> torch.Tensor:
        """前向传播"""
        features = self.get_text_features(triple)
        features_tensor = torch.FloatTensor(features).unsqueeze(0)
        device = next(self.parameters()).device
        features_tensor = features_tensor.to(device)
        processed = self.feature_layer(features_tensor)
        return processed.squeeze(0)