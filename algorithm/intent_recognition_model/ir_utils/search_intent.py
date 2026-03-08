import sys
import time
import os
from ir_config.config import Config
from ir_utils.encoder import HybridEncoder
from pymilvus import MilvusClient, AnnSearchRequest, RRFRanker, MilvusException
import logging

# 配置日志
config = Config()
log_path = config.log_path
if log_path:
    os.makedirs(log_path, exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(filename=os.path.join(log_path,"search.log"), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class HybridSearch:
    def __init__(self):
        self.config = Config()
        self.data_dir = self.config.data_dir
        self.collection_name = self.config.collection_name
        self.milvus_uri = self.config.milvus_uri

        print("正在加载编码器...")
        self.encoder = HybridEncoder()
        self.dense_dim = self.encoder.dense_dim
        self.sparse_dim = self.encoder.sparse_dim

        print(f"正在连接 Milvus: {self.milvus_uri}")
        self.milvus_client = self._init_milvus_client()
        self.collection_name = self.config.collection_name

        self.milvus_client.load_collection(self.collection_name)
        print(f"Collection '{self.collection_name}' 已加载到内存")

    def search(self, query, top_k = 3):
        """相似度查询"""
        dense_search_param = {
            "metric_type": "IP",
            "params": {
                "nprobe": 16,
                "radius": 0.4,
                "range_filter": 1
                }
        }

        sparse_search_param = {
            "metric_type": "IP",
            "params": {
                "topk": 100,
                "radius": 0.4,
                "range_filter": 1
                }
        }

        if self.collection_name is None or query is None:
            return

        #  问题编码
        query_vector = self.encoder.encode_text_hybrid(query)
        dense_vector = query_vector.get("dense")
        sparse_vector = query_vector.get("sparse")

        reqs = [
            # 密集向量检索
            AnnSearchRequest(data=[dense_vector], anns_field="dense_vector", param=dense_search_param, limit=top_k),

            # 稀疏向量检索
            AnnSearchRequest(data=[sparse_vector], anns_field="sparse_vector",param=sparse_search_param, limit=top_k),
        ]

        # reranker = WeightedRanker(0.03, 0.15, 0.07, 0.30, 0.45)  # 业务强依赖时用
        reranker = RRFRanker(k=60)  # 免调参、抗噪声、效果稳，k越小越强调top结果

        # 相似度查询
        logging.info("正在进行相似度搜索...")
        results = self.milvus_client.hybrid_search(
            collection_name=self.collection_name,
            reqs=reqs,
            ranker=reranker,
            limit=top_k,
            output_fields=["intent_id", "version", "domain", "create_time", "update_time", "status",
                           "intent_name_text", "intent_description_text", "intent_summary_text", "user_goal_text", "sample_utterances",
                           "tags","core_keywords"]
        )[0]
        print(results)
        print(f"共找到{len(results)}条相关数据")
        return results

    def _init_milvus_client(self) -> MilvusClient | None:
        """初始化 MilvusClient，带重试机制"""
        max_retries = 3
        retry_delay = 2
        for attempt in range(max_retries):
            try:
                client = MilvusClient(uri=self.milvus_uri)
                # 测试连接
                client.list_collections()
                print("Milvus 连接成功")
                return client
            except MilvusException as e:
                logger.warning(f"Milvus 连接失败（尝试 {attempt+1}/{max_retries}）: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error("Milvus 连接重试次数耗尽")
                    raise
            except Exception as e:
                logger.error(f"Milvus 客户端初始化异常: {e}")
                raise
        return None


if __name__ == "__main__":
    query = "电机部分需要检查哪些关键部位？"
    print(f"query: {query}")
    search = HybridSearch()
    result = search.search(query=query)




