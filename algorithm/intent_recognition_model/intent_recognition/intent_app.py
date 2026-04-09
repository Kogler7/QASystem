import logging
import os
import sys

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union
from ir_config.config import Config
from ir_utils.search_intent import HybridSearch
from langchain_openai import ChatOpenAI
from ir_utils.handle_data import utils
from data.intent_model import Intent
import json

# 配置日志
config = Config()
log_path = config.log_path
if log_path:
    os.makedirs(log_path, exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler(filename=os.path.join(log_path,"search.log"),encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


# LLM 配置：与 algorithm/common 统一，从 Config 读取（Config 优先使用 LLM_* / OPENAI_* 环境变量）
_config = Config()
_base = _config.llm_base_url.rstrip("/")
if not _base.endswith("/v1"):
    _base = _base + "/v1"

model = ChatOpenAI(
    base_url=_base,
    api_key=_config.api_key or "none",
    model=_config.llm_model,
    temperature=0,
    tool_choice="none"
)

class IntentRecognition:
    def __init__(self):
        self.model = model
        self.search_tool = HybridSearch()
        self.utils = utils()

    def show_distance_retrieval(self, query: str):
        """展示距离检索结果"""
        results = self.search_tool.multi_search(query=query)

        logger.info(f"检索到的意图数量: {len(results)}")
        logger.info(f"搜索结果详细信息: {results}")
        result = []
        distance = []
        filter_result = []
        logger.info("正在处理搜索结果...")
        
        for r in results:
            # 直接提取entity字典，避免protobuf对象
            entity = r.get("entity")
            
            serializable_entity = None
            if entity:
                # 映射字段名，将数据库字段名映射到Intent模型字段名
                
                mapped_data = {
                    "intent_id": entity.get("intent_id"),
                    "version": entity.get("version"),
                    "domain": entity.get("domain"),
                    "create_time": entity.get("create_time"),
                    "update_time": entity.get("update_time"),
                    "status": entity.get("status"),
                    "tags": entity.get("tags"),
                    "core_keywords": entity.get("core_keywords"),
                    "sample_utterances": entity.get("sample_utterances"),
                    "intent_name": entity.get("intent_name"),
                    "intent_description": entity.get("intent_description"),
                    "intent_summary": entity.get("intent_summary"),
                    "user_goal": entity.get("user_goal"),
                }
                
                # 使用Intent模型处理数据，利用Pydantic的自动类型转换
                try:
                    intent_obj = Intent(**mapped_data)
                    serializable_entity = intent_obj.model_dump()
                except Exception as e:
                    logger.info(f"使用Intent模型处理数据失败: {e}")
            else:
                serializable_entity = None
            result.append(serializable_entity)
            # 提取距离
            rrf_score = r.get("rrf_score")
            original_distance = r.get("original_distances")
            distance.append(
                {
                    "意图ID": serializable_entity.get("intent_id"),
                    "稠密向量的相似度": float(original_distance.get("dense_vector")) if original_distance.get("dense_vector") is not None else None,
                    "稀疏向量的相似度": float(original_distance.get("sparse_vector")) if original_distance.get("sparse_vector") is not None else None,
                    "RRF分数": float(rrf_score) if rrf_score is not None else None
                }
            )
        
        logger.info("完成处理搜索结果")
        print(result)
        print(distance)

        # 检查结果是否为空
        if not result:
            logger.info("没有检索到相关意图")
            return {
                "相关意图": [],
                "相似度": [],
                "筛选的最终意图": None
            }

        filter_result = result[0]
        print("filter_result", filter_result)
        return {
            "相关意图": result,
            "相似度": distance,
            "筛选的最终意图": filter_result
        }

    def generate_result(self, query: str):
        """答案生成函数,与show_distance_retrieval函数结合使用"""
        logger.info("正在进行答案生成...")
        retrival_result = self.show_distance_retrieval(query=query)

        logger.info("retrival_result 类型: %s", type(retrival_result))
        logger.info("相关意图数量: %s", len(retrival_result.get("相关意图", [])))
        logger.info("相似度数量: %s", len(retrival_result.get("相似度", [])))

        if not retrival_result["相关意图"] or not retrival_result["相似度"]:
            logger.info("没有检索到相关意图")
            return "没有检索到相关意图"

        result = retrival_result["相关意图"]
        distance = retrival_result["相似度"]

        logger.info("准备返回结果...")
        return {
            "相关意图": result,
            "相似度": distance,
            "筛选的最终意图": retrival_result.get("筛选的最终意图")
        }


if __name__ == "__main__":
    intent_recognition = IntentRecognition()
    query = "公开采购与非公开采购金额构成如何？"
    
    # 直接调用 show_distance_retrieval 方法查看搜索结果
    result = intent_recognition.generate_result(query)
    print("result:", result)