import json
import os
import sys
import time
from typing import List, Dict, Any
import logging

import numpy as np
from ir_config.config import Config
from utils.encoder import HybridEncoder
from pymilvus import connections, MilvusClient, FieldSchema, DataType, CollectionSchema, Collection, MilvusException

# 配置日志
config = Config()
log_path = config.log_path

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(filename=os.path.join(log_path,"milvus_init.log"),encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class DatabaseInit:
    def __init__(self):
        self.config = Config()
        self.data_dir = self.config.data_dir
        self.collection_name = self.config.collection_name
        self.milvus_uri = self.config.milvus_uri

        # 初始化数据集以及编码器
        print("正在初始化数据集...")
        self.dataset = self._load_dataset()
        print(f"加载数据集完成，共 {len(self.dataset)} 条数据")

        print("正在初始化编码器...")
        self.encoder = HybridEncoder()
        self.dense_dim = self.encoder.dense_dim
        self.sparse_dim = self.encoder.sparse_dim

        print(f"正在连接到 Milvus: {self.milvus_uri}")
        connections.connect(uri=self.milvus_uri)
        self.milvus_client = self._init_milvus_client()
        self.collection = None

        self.consistency_level = getattr(self.config, "milvus_consistency_level", "Strong")
        self.max_drop_retries = getattr(self.config, "max_drop_retries", 3)
        self.drop_retry_delay = getattr(self.config, "drop_retry_delay", 2)

    def _load_dataset(self) -> List[Dict[str, Any]]:
        """加载数据集"""
        try:
            with open(self.data_dir, "r", encoding="utf-8") as f:
                dataset = json.load(f)
            # 数据校验：确保是列表且非空
            if not isinstance(dataset, list):
                raise ValueError(f"数据集格式错误，期望列表，实际 {type(dataset)}")
            return dataset
        except FileNotFoundError:
            logger.error(f"数据集文件不存在: {self.data_dir}")
            raise
        except json.JSONDecodeError:
            logger.error(f"数据集JSON格式错误: {self.data_dir}")
            raise
        except Exception as e:
            logger.error(f"加载数据集失败: {e}")
            raise


    def _init_milvus_client(self)-> MilvusClient|None:
        """初始化客户端MilvusClient"""
        max_retries = 3
        retry_delay = 2
        for attempt in range(max_retries):
            try:
                client = MilvusClient(uri=self.milvus_uri)

                # 测试链接
                client.list_collections()
                print("Milvus连接成功")
                return client
            except MilvusException as e:
                logger.warning(f"Milvus 连接失败（尝试 {attempt + 1}/{max_retries}）: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    logger.error("Milvus 连接重试次数耗尽，初始化失败")
                    raise
            except Exception as e:
                logger.error(f"Milvus 客户端初始化异常: {e}")
                raise

    def _safe_drop_collection(self) -> None:
        """安全删除集合"""
        if not self.milvus_client.has_collection(self.collection_name):
            print(f"集合 {self.collection_name} 不存在，无需删除")
            return

        for attempt in range(self.max_drop_retries):
            try:
                self.milvus_client.drop_collection(self.collection_name)
                print(f"成功删除已存在的集合: {self.collection_name}")
                return
            except MilvusException as e:
                if "node not match" in str(e) or "InvalidateCollectionMetaCache" in str(e):
                    logger.warning(f"删除集合失败（尝试 {attempt + 1}/{self.max_drop_retries}）: {e}")
                    if attempt < self.max_drop_retries - 1:
                        time.sleep(self.drop_retry_delay)
                else:
                    logger.error(f"删除集合异常: {e}")
                    raise
            except Exception as e:
                logger.error(f"删除集合未知错误: {e}")
                raise
        raise RuntimeError(f"删除集合 {self.collection_name} 重试 {self.max_drop_retries} 次仍失败")


    def _create_index(self):
        """创建索引"""
        print("正在创建索引...")
        try:
            # 密集向量索引
            dense_index_params = self.milvus_client.prepare_index_params()
            dense_index_params.add_index(
                field_name="dense_vector",
                index_type="AUTOINDEX",
                metric_type="IP"
            )
            self.milvus_client.create_index(collection_name=self.collection_name, index_params=dense_index_params)
            print("密集向量索引创建成功")

            # 稀疏向量索引
            sparse_index_params = self.milvus_client.prepare_index_params()
            sparse_index_params.add_index(
                field_name="sparse_vector",
                index_type="SPARSE_INVERTED_INDEX",
                metric_type="IP",
                params={}
            )
            self.milvus_client.create_index(collection_name=self.collection_name, index_params=sparse_index_params)
            print("稀疏向量索引创建成功")
        except MilvusException  as e:
            logger.error(f"创建索引失败: {e}")
            raise


    def create_collection(self):
        """创建Collection"""
        print(f"正在创建集合: {self.collection_name}")

        # 尝试删除已有集合
        self._safe_drop_collection()

        # 创建schema
        fields = [
            # 元数据
            FieldSchema(name="intent_id", dtype=DataType.VARCHAR, is_primary=True, max_length=100),
            FieldSchema(name="version", dtype=DataType.VARCHAR, max_length=10),
            FieldSchema(name="domain", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="create_time", dtype=DataType.VARCHAR, max_length=100),
            FieldSchema(name="update_time", dtype=DataType.VARCHAR, max_length=100),
            FieldSchema(name="status", dtype=DataType.VARCHAR, max_length=10),

            # 原始文本保留
            FieldSchema(name="intent_name_text", dtype=DataType.VARCHAR, max_length=256),
            FieldSchema(name="intent_description_text", dtype=DataType.VARCHAR, max_length=1024),
            FieldSchema(name="intent_summary_text", dtype=DataType.VARCHAR, max_length=1024),
            FieldSchema(name="user_goal_text", dtype=DataType.VARCHAR, max_length=1024),

            # 稠密向量字段
            FieldSchema(name="dense_vector", dtype=DataType.FLOAT_VECTOR, dim=self.dense_dim, description="BGE-M3 全字段融合 dense 向量"),

            #稀疏向量字段
            FieldSchema(name="sparse_vector", dtype=DataType.SPARSE_FLOAT_VECTOR, description="BGE-M3 全字段融合 sparse 向量"),

            # ARRAY字段
            FieldSchema(name="tags", dtype=DataType.ARRAY,
                        element_type=DataType.VARCHAR, max_capacity=10, max_length=50),
            FieldSchema(name="sample_utterances", dtype=DataType.ARRAY,
                        element_type=DataType.VARCHAR, max_capacity=20, max_length=300),
            FieldSchema(name="core_keywords", dtype=DataType.ARRAY,
                        element_type=DataType.VARCHAR, max_capacity=15, max_length=50),
        ]

        schema = CollectionSchema(fields=fields, description="轨道交通智能问答系统意图识别检索")


        # 创建集合
        try:
            self.milvus_client.create_collection(
                collection_name=self.collection_name,
                schema=schema,
                consistency_level=self.consistency_level
            )
            print(f"{self.collection_name} 集合创建成功")
        except MilvusException as e:
            logger.error(f"创建集合失败: {e}")
            raise

        # 创建索引
        self._create_index()
        self.collection = Collection(name=self.collection_name)

        # 打印集合信息
        coll_info = self.milvus_client.describe_collection(self.collection_name)
        print(f"集合创建完成，结构: {coll_info}")


    def aggregate_utterances(self, utterances):
        """差异化聚合：稠密用平均，稀疏用拼接"""
        if utterances is None:
            return {
                "dense":[0.0] * self.dense_dim,
                "sparse": {}
            }

        # 稠密向量使用平均聚合方法
        dense_vectors = [self.encoder.encode_only_dense(text_content=utterance) for utterance in utterances]
        dense_agg = np.mean(dense_vectors, axis=0).tolist()

        # 稀疏向量采用拼接后使用单次编码
        sparse_text = " ".join(utterances)
        sparse_vectors = self.encoder.encode_text_hybrid(sparse_text).get("sparse")

        return {
            "dense": dense_agg,
            "sparse": sparse_vectors
        }

    def insert_data(self):
        """插入数据"""
        # 先加载集合（确保集合处于可操作状态）
        self.collection = Collection(name=self.collection_name)

        if self.collection.num_entities > 0:
            print("--> Collection 非空，跳过插入")
            return

        print("开始插入数据...")

        # 构建行数据列表
        insert_data = []

        for idx, intent in enumerate(self.dataset, 1):
            try:
                # 构建单条数据字典
                single_data = {}

                # 元数据字段，直接取值
                single_data["intent_id"] = intent.get("intent_id")
                single_data["version"] = intent.get("version", "1.0")
                single_data["domain"] = intent.get("domain", "unknown")
                single_data["create_time"] = intent.get("create_time", "")
                single_data["update_time"] = intent.get("update_time", "")
                single_data["status"] = intent.get("status", "active")

                # 原始文本字段
                single_data["intent_name_text"] = intent.get("intent_name", "")
                single_data["intent_description_text"] = intent.get("intent_description", "")
                single_data["intent_summary_text"] = intent.get("intent_summary", "")
                single_data["user_goal_text"] = intent.get("user_goal", "")

                # 向量编码
                text = self.build_embedding_text(intent)
                encode_result = self.encoder.encode_text_hybrid(text)
                dense_vectors = encode_result.get("dense")
                sparse_vectors = encode_result.get("sparse")
                print(f"dense_vectors: {dense_vectors} | sparse_vectors: {sparse_vectors}")

                # 稠密向量字段
                single_data["dense_vector"] = dense_vectors

                # 稀疏向量字段
                single_data["sparse_vector"] = sparse_vectors

                # ARRAY字段
                single_data["tags"] = intent.get("tags", [])
                single_data["sample_utterances"] = intent.get("sample_utterances", [])
                single_data["core_keywords"] = intent.get("core_keywords", [])

                # 添加到插入列表
                insert_data.append(single_data)

            except Exception as e:
                print(f"处理第 {idx} 条数据时出错（intent_name: {intent.get('intent_name', 'N/A')}）: {e}")
                continue

        try:
            # 插入行数据列表
            insert_result = self.collection.insert(data=insert_data)
            # 刷新集合确保数据立即可见
            self.collection.flush()
            print(f"插入成功,共插入 {len(insert_data)} 条数据，插入ID: {insert_result.primary_keys}")
            print(f"当前Collection总数据量: {self.collection.num_entities}")
            print("\n")

        except Exception as e:
            print(f"插入失败: {type(e).__name__}: {e}")


    def build_embedding_text(self,intent_data:dict)->str:
        """构建用于向量编码的融合文本（保留业务语义权重）"""
        parts = []

        if intent_data.get("intent_name"):
            parts.append(f"[核心]意图名称: {intent_data['intent_name']}")
        if intent_data.get("user_goal"):
            parts.append(f"[目标]用户目标: {intent_data['user_goal']}")
        if intent_data.get("intent_summary"):
            parts.append(f"[摘要]意图摘要: {intent_data['intent_summary']}")

        # 补充语义（中权重）
        if intent_data.get("intent_description"):
            parts.append(f"[描述]详细说明: {intent_data['intent_description']}")
        if intent_data.get("core_keywords"):
            kw = "、".join(intent_data["core_keywords"])
            parts.append(f"[关键词]: {kw}")

        # 用户表达（高价值样本）
        if intent_data.get("sample_utterances"):
            # 选取最具代表性的3条（避免噪声）
            samples = intent_data["sample_utterances"][:3]
            samples_text = " | ".join([f"{s}" for s in samples])
            parts.append(f"[示例]典型表述: {samples_text}")

        return "\n".join(parts)


