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
                "drop_ratio_search": 0.2
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
                           "intent_name", "intent_description", "intent_summary", "user_goal", "sample_utterances",
                           "tags","core_keywords"]
        )[0]
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

    def RRF_rank(self, results_1, results_2):
        """对搜索结果进行RRF排名"""
        # 建立 ID 到实体的映射
        entity_map = {}
        original_distances = {}

        # results_1 和 results_2 是 Hits 对象
        # 使用 ids 和 distances 属性访问数据
        for i in range(len(results_1)):
            entity_id = results_1.ids[i]
            # 构建 entity 字典,包含所有 output_fields
            entity_data = {}
            for field in ["intent_id", "version", "domain", "create_time", "update_time", "status",
                        "intent_name", "intent_description", "intent_summary", 
                        "user_goal", "sample_utterances", "tags", "core_keywords"]:
                entity_data[field] = results_1[i].get(field)
            
            entity_map[entity_id] = entity_data
            original_distances[(entity_id, "dense_vector")] = results_1.distances[i]

        for i in range(len(results_2)):
            entity_id = results_2.ids[i]
            if entity_id not in entity_map:
                entity_data = {}
                for field in ["intent_id", "version", "domain", "create_time", "update_time", "status",
                            "intent_name", "intent_description", "intent_summary", 
                            "user_goal", "sample_utterances", "tags", "core_keywords"]:
                    entity_data[field] = results_2[i].get(field)
                entity_map[entity_id] = entity_data
            original_distances[(entity_id, "sparse_vector")] = results_2.distances[i]

        # 手动实现 RRF
        k = 60
        rrf_scores = {}

        # 遍历 IDs
        for rank, entity_id in enumerate(results_1.ids):
            rrf_scores[entity_id] = rrf_scores.get(entity_id, 0) + 1 / (k + rank + 1)

        for rank, entity_id in enumerate(results_2.ids):
            rrf_scores[entity_id] = rrf_scores.get(entity_id, 0) + 1 / (k + rank + 1)

        # 按分数排序并构建完整结果
        sorted_results = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)

        # 构建包含完整信息的结果
        final_results = []
        for entity_id, rrf_score in sorted_results:
            result = {
                'id': entity_id,
                'rrf_score': rrf_score,
                'entity': entity_map.get(entity_id, {}),
                'original_distances': {
                    'dense_vector': original_distances.get((entity_id, "dense_vector")),
                    'sparse_vector': original_distances.get((entity_id, "sparse_vector"))
                }
            }
            final_results.append(result)
        return final_results

    def multi_search(self, query: str, top_k = 3):
        """包含了query与每个意图的距离，以及RRF分数"""
        dense_search_param = {
            "metric_type": "IP",
            "params": {
                "nprobe": 16,
                "radius":  0.4,
                "range_filter" : 1
                }
        }

        sparse_search_param = {
            "metric_type": "IP",
            "params": {
                "radius": 0.1,
                "range_filter" : 1
                }
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
            output_fields=["intent_id", "version", "domain", "create_time", "update_time", "status",
                           "intent_name", "intent_description", "intent_summary", "user_goal", "sample_utterances",
                           "tags","core_keywords"],
            search_params=dense_search_param
        )[0]

        results_2 = self.milvus_client.search(
            collection_name=self.collection_name,
            data=[sparse_vector],
            anns_field="sparse_vector",
            limit=top_k,
            output_fields=["intent_id", "version", "domain", "create_time", "update_time", "status",
                           "intent_name", "intent_description", "intent_summary", "user_goal", "sample_utterances",
                           "tags","core_keywords"],
            search_params=sparse_search_param
        )[0]
        logging.info("检索完成")
        print("\n")
        results = self.RRF_rank(results_1, results_2)
        print("\n")
        print(results)
        return results

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



if __name__ == "__main__":
    query = "公开采购与非公开采购金额构成如何？"
    print(f"query: {query}")
    search = HybridSearch()
    result = search.multi_search(query=query)
    print(result)
