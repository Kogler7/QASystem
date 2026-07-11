import sys
import time
import os
from algorithm.intent_recognition_model.ir_config.config import Config
from algorithm.intent_recognition_model.ir_utils.encoder import HybridEncoder
from pymilvus import MilvusClient, MilvusException
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

    def HybridHits_to_dict(self, results):
        """将HybridHits转换为字典"""
        result_list = []
        for hit in results:
            result_dict = {
                "intent_id":hit.id,
                "similarity": hit.distance,
                "entity": {}
            }
            for field in ["intent_id", "domain", "scenario", "object","intent_name", "user_goal"]:
                result_dict["entity"][field] = hit.get(field)
            result_list.append(result_dict)

        return result_list

    def basic_search(self, query: str, top_k = 3):
        """基础相似度查询，不进行RRFRank重排"""
        dense_search_param = {
            "metric_type": "IP",
        }

        sparse_search_param = {
            "metric_type": "IP",
        }

        if self.collection_name is None or query is None:
            return

        #  问题编码
        query_vector = self.encoder.encode_text_hybrid(query)
        dense_vector = query_vector.get("dense")
        sparse_vector = query_vector.get("sparse")

        # 相似度查询
        logging.info("正在进行相似度搜索...")
        results_1 = self.milvus_client.search(
            collection_name=self.collection_name,
            data=[dense_vector],
            anns_field="dense_vector",
            limit=top_k,
            output_fields=["intent_id", "domain", "scenario", "object",
                           "intent_name","user_goal"],
            search_params=dense_search_param
        )[0]

        results_2 = self.milvus_client.search(
            collection_name=self.collection_name,
            data=[sparse_vector],
            anns_field="sparse_vector",
            limit=top_k,
            output_fields=["intent_id", "domain", "scenario", "object",
                           "intent_name","user_goal"],
            search_params=sparse_search_param
        )[0]
        logging.info("检索完成")
        return {
            "密集向量检索结果": self.HybridHits_to_dict(results_1),
            "稀疏向量检索结果": self.HybridHits_to_dict(results_2)
        }
