from ir_utils.encoder import HybridEncoder
import numpy as np
import time
from datetime import datetime
from pymilvus import connections, MilvusClient, FieldSchema, DataType, CollectionSchema, Collection, MilvusException
from ir_config.config import Config

class DatabaseUtils:
    def __init__(self):
        self.config = Config()
        self.milvus_uri = self.config.milvus_uri
        self.collection_name = self.config.collection_name

        self.encoder = HybridEncoder()
        self.dense_dim = self.encoder.dense_dim
        self.sparse_dim = self.encoder.sparse_dim

    def build_dense_embedding_text(self,intent_data:dict)->str:
        """构建用于向量编码的融合文本（保留业务语义权重）"""
        parts = []

        if intent_data.get("intent_name"):
            parts.append(f"意图名称: {intent_data['intent_name']}")
        if intent_data.get("user_goal"):
            parts.append(f"用户目标: {intent_data['user_goal']}")
        if intent_data.get("intent_summary"):
            parts.append(f"意图摘要: {intent_data['intent_summary']}")

        # 补充语义（中权重）
        if intent_data.get("intent_description"):
            parts.append(f"详细说明: {intent_data['intent_description']}")

        # 用户表达（高价值样本）
        if intent_data.get("sample_utterances"):
            # 选取最具代表性的3条（避免噪声）
            samples = intent_data["sample_utterances"][:3]
            samples_text = " | ".join([f"{s}" for s in samples])
            parts.append(f"典型表述: {samples_text}")   

        return "\n".join(parts)

    def build_sparse_embedding_text(self,intent_data:dict)->str:
        """构建用于向量编码的融合文本（保留业务语义权重）"""
        parts = []

        if intent_data.get("intent_name"):
            parts.append(f"意图名称: {intent_data['intent_name']}")
        
        if intent_data.get("scenario"):
            parts.append(f"场景: {intent_data['scenario']}")

        if intent_data.get("object"):
            parts.append(f"目标对象: {intent_data['object']}")

        if intent_data.get("tags"):
            # 选取最具代表性的3条（避免噪声）
            tags = intent_data["tags"]
            tags_text = " | ".join([f"{s}" for s in tags])
            parts.append(f"标签: {tags_text}") 
        
        if intent_data.get("core_keywords"):
            # 选取最具代表性的3条（避免噪声）
            core_keywords = intent_data["core_keywords"]
            core_keywords_text = " | ".join([f"{s}" for s in core_keywords])
            parts.append(f"核心关键词: {core_keywords_text}")
        return "\n".join(parts)

    def init_milvus_client(self)-> MilvusClient|None:
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
                print(f"Milvus 连接失败（尝试 {attempt + 1}/{max_retries}）: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    print("Milvus 连接重试次数耗尽，初始化失败")
                    raise
            except Exception as e:
                print(f"Milvus 客户端初始化异常: {e}")
                raise

    def build_query_from_json(self, json_input: dict) -> str:
        """从 JSON 输入构建查询文本"""
        # 检查是否有 problem_model 字段
        if "problem_model" in json_input:
            problem_model = json_input["problem_model"]
        else:
            problem_model = json_input
        
        parts = []

        # 问题描述（高权重）
        if problem_model.get("问题描述"):
            parts.append(f"问题描述: {problem_model['问题描述']}")

        # 目标对象（中权重）
        if problem_model.get("目标对象"):
            parts.append(f"目标对象: {problem_model['目标对象']}")

        # 目标（中权重）
        if problem_model.get("目标"):
            goals = problem_model["目标"]
            if goals:
                goals_text = " | ".join(goals)
                parts.append(f"目标: {goals_text}")

        # 相关实体（高价值样本）
        if problem_model.get("相关实体"):
            entities = problem_model["相关实体"]
            if entities:
                entities_text = " | ".join(entities)
                parts.append(f"相关实体: {entities_text}")

        return "\n".join(parts)

    def get_current_time_str(self) -> str:
        """生成当前时间的字符串格式"""
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")