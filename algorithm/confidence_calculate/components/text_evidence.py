import os
import threading
import yaml
import numpy as np
from pathlib import Path
from elasticsearch import Elasticsearch
from sentence_transformers import SentenceTransformer
from typing import Dict, Optional
from dotenv import load_dotenv

# 加载 backend/.env 补充环境变量（不覆盖已有变量）
env_path = Path(__file__).parent.parent.parent.parent / 'backend' / '.env'
if env_path.exists():
    load_dotenv(env_path, override=False)


class TextEvidence:
    """
    文本证据组件
    通过 Elasticsearch 检索文档，并利用 SBERT 计算语义相似度，
    结合 BM25 分数，输出一个 0-1 的标量文本证据分数。
    """

    def __init__(self, config: Optional[Dict] = None):
        # 加载配置文件（默认同目录下 config.yaml）
        config_path = Path(__file__).parent.parent / 'config.yaml'
        with open(config_path, 'r', encoding='utf-8') as f:
            yaml_config = yaml.safe_load(f)

        text_config = yaml_config.get('text', {})
        if config:
            text_config.update(config)

        # 从环境变量读取 Elasticsearch 连接信息
        es_host = os.getenv('CONFIDENCE_ES_HOSTS', os.getenv('ES_HOSTS', 'http://127.0.0.1:9200'))
        if ',' in es_host:
            es_hosts = [h.strip() for h in es_host.split(',')]
        else:
            es_hosts = [es_host]

        es_user = os.getenv('CONFIDENCE_ES_USERNAME', os.getenv('ES_USER'))
        es_password = os.getenv('CONFIDENCE_ES_PASSWORD', os.getenv('ES_PASSWORD'))
        verify_certs = (os.getenv('CONFIDENCE_ES_VERIFY_CERTS', os.getenv('ES_VERIFY_CERTS', 'false'))).lower() == 'true'

        # ES 客户端配置
        es_kwargs = {
            'hosts': es_hosts,
            'request_timeout': text_config.get('es_timeout', 30),
            'retry_on_timeout': True,
            'max_retries': 3,
            'verify_certs': verify_certs,
        }
        if es_user and es_password:
            es_kwargs['basic_auth'] = (es_user, es_password)

        self.es = Elasticsearch(**es_kwargs)
        if not self.es.ping():
            print("警告: Elasticsearch 连接失败")
        else:
            print(f"Elasticsearch 连接成功: {es_hosts}")

        self.index_name = os.getenv('CONFIDENCE_ES_INDEX', os.getenv('ES_INDEX', 'documents'))
        self.size = text_config.get('es_size', 50)          # ES 返回的文档数
        self.max_bm25 = text_config.get('max_bm25_norm', 100.0)  # BM25 归一化上限

        # 加载 SBERT 模型
        model_path = os.getenv('CONFIDENCE_SBERT_MODEL_PATH', os.getenv('SBERT_MODEL_PATH',
                               '/mnt/data3/srv/qasystem/models/sbert_model'))
        print(f"加载 SBERT 模型: {model_path}")
        if not os.path.exists(model_path):
            print(f"本地模型不存在，使用默认模型 paraphrase-multilingual-MiniLM-L12-v2")
            self.sbert = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
        else:
            self.sbert = SentenceTransformer(model_path)
        self._encode_lock = threading.Lock()

    def build_query(self, triple: Dict) -> str:
        """根据三元组构造查询字符串"""
        subj = triple.get('subject', '')
        pred = triple.get('predicate', '')
        obj = triple.get('object', '')
        return f"{subj} {pred} {obj}"

    def get_score(self, triple: Dict) -> float:
        """
        计算文本证据分数 (0-1)。
        返回所有候选文档中（语义相似度与 BM25 综合得分）的最高分。
        """
        query_str = self.build_query(triple)
        if not query_str.strip() or self.es is None:
            return 0.5   # 无法计算时返回默认值

        # Elasticsearch BM25 检索
        try:
            response = self.es.search(
                index=self.index_name,
                query={"match": {"content": query_str}},
                size=self.size
            )
        except Exception as e:
            print(f"ES 检索失败: {e}")
            return 0.5

        hits = response.get('hits', {}).get('hits', [])
        if not hits:
            return 0.5

        # 编码查询语句
        with self._encode_lock:
            query_vec = self.sbert.encode(query_str)
        query_norm = np.linalg.norm(query_vec)

        best_score = 0.0
        for hit in hits:
            content = hit['_source'].get('content', '')
            if not content:
                continue
            # 编码文档句子
            with self._encode_lock:
                doc_vec = self.sbert.encode(content)
            doc_norm = np.linalg.norm(doc_vec)
            # 余弦相似度
            cos_sim = np.dot(query_vec, doc_vec) / (query_norm * doc_norm + 1e-10)
            # BM25 归一化
            bm25_norm = min(hit['_score'] / self.max_bm25, 1.0)
            # 综合得分：语义权重 0.7，BM25 权重 0.3
            combined = 0.7 * cos_sim + 0.3 * bm25_norm
            if combined > best_score:
                best_score = combined

        return round(best_score, 4)

    def close(self):
        if self.es is not None:
            self.es.close()
