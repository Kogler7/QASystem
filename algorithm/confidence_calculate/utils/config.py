import yaml
from typing import Dict


class Config:
    """配置类"""
    def __init__(self, config_path: str = None):
        if config_path:
            with open(config_path, 'r') as f:
                self.config = yaml.safe_load(f)
        else:
            self.config = self.default_config()
    
    def default_config(self) -> Dict:
        """默认配置"""
        return {
            'learning_rate': 0.001,
            'l2_lambda': 0.001,
            'batch_size': 32,
            'epochs': 100,
            'text_dim': 128,
            'kg_dim': 128,
            'path_dim': 64,
            
            'text': {
                'es_hosts': ['localhost:9200'],
                'es_index': 'documents',
                'es_timeout': 30,
                'es_size': 50,
                'text_top_k': 10,
                'max_bm25_norm': 100.0,
                'sbert_model_path': 'paraphrase-multilingual-MiniLM-L12-v2'
            },
            
            'kg': {
                'kg_embedding_dim': 100,
                'entity_emb_path': 'embeddings/entities.pt',
                'relation_emb_path': 'embeddings/relations.pt'
            },
            
            'path': {
                'max_path_length': 3,
                'max_paths_per_triple': 20,
                'path_embedding_dim': 64
            }
        }
    
    def get(self, key, default=None):
        """获取配置"""
        keys = key.split('.')
        value = self.config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
            else:
                return default
            if value is None:
                return default
        return value
    
    def __getitem__(self, key):
        return self.get(key)