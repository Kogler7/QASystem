import os
import yaml
from typing import Dict, Any, Optional


def _merge_env_into_confidence_config(cfg: Dict[str, Any]) -> None:
    """用环境变量覆盖 YAML，便于联调/部署（与 backend/.env 一致）。"""
    text = cfg.setdefault("text", {})
    if os.getenv("CONFIDENCE_SBERT_MODEL_PATH"):
        text["sbert_model_path"] = os.environ["CONFIDENCE_SBERT_MODEL_PATH"]
    if os.getenv("CONFIDENCE_ES_HOSTS"):
        hosts = [h.strip() for h in os.environ["CONFIDENCE_ES_HOSTS"].split(",") if h.strip()]
        if hosts:
            text["es_hosts"] = hosts
    if os.getenv("CONFIDENCE_ES_USERNAME") is not None:
        text["es_username"] = os.environ.get("CONFIDENCE_ES_USERNAME") or None
    if os.getenv("CONFIDENCE_ES_PASSWORD") is not None:
        text["es_password"] = os.environ.get("CONFIDENCE_ES_PASSWORD") or None
    if os.getenv("CONFIDENCE_ES_INDEX"):
        text["es_index"] = os.environ["CONFIDENCE_ES_INDEX"]

    path_cfg = cfg.setdefault("path", {})
    neo = path_cfg.setdefault("neo4j", {})
    if os.getenv("NEO4J_URI"):
        neo["uri"] = os.environ["NEO4J_URI"]
    if os.getenv("NEO4J_USER"):
        neo["username"] = os.environ["NEO4J_USER"]
    if os.getenv("NEO4J_PASSWORD"):
        neo["password"] = os.environ["NEO4J_PASSWORD"]
    if os.getenv("NEO4J_DATABASE"):
        neo["database"] = os.environ["NEO4J_DATABASE"]


class Config:
    """配置类"""
    def __init__(self, config_path: Optional[str] = None):
        if config_path:
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = yaml.safe_load(f) or {}
            _merge_env_into_confidence_config(self.config)
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