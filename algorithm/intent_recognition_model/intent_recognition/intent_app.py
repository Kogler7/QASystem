import logging
import os
import sys

from pydantic import BaseModel, Field
from ir_config.config import Config
from ir_utils.search_intent import HybridSearch
from langchain_openai import ChatOpenAI
from ir_utils.handle_data import utils

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

class Intent(BaseModel):
    intent_id: str = Field(..., description="意图唯一标识，如业务自定义ID，用于数据库主键/索引")
    version: str = Field(..., description="意图版本号，用于迭代管理")
    domain: str = Field(..., description="意图所属业务域，用于分类筛选")
    create_time: str = Field(..., description="意图创建时间（ISO8601格式），溯源用")
    update_time: str = Field(..., description="意图更新时间，版本管理用")
    status: str = Field(..., description="意图状态（active/archive/draft）")
    tags: list = Field(..., description="业务标签，用于分类检索（非语义检索）")
    intent_name: str = Field(..., description="意图名称（简洁描述），核心语义字段")
    intent_description: str = Field(..., description="意图详细描述，补充语义")
    sample_utterances: list = Field(..., description="意图的典型用户表述样本，多维度语义载体")
    core_keywords: list = Field(..., description="核心关键词")
    intent_summary: str = Field(..., description="意图精简摘要")
    user_goal: str = Field(..., description="用户核心目标")

class IntentRecognition:
    def __init__(self):
        self.model = model
        self.search_tool = HybridSearch()
        self.utils = utils()

    def retrival(self, query: str):
        """检索函数,不展示距离相似度"""
        results = self.search_tool.search(query=query)

        logger.info(f"检索到的意图数量: {len(results)}")
        result = []
        distance = []
        filter_result = []
        logger.info("正在处理搜索结果...")
        
        for r in results:
            # 直接提取entity字典，避免protobuf对象
            entity = r.get("entity")
            
            # 处理数组字符串字段
            if entity:
                # 处理 tags 字段
                if "tags" in entity and isinstance(entity["tags"], str):
                    try:
                        import ast
                        entity["tags"] = ast.literal_eval(entity["tags"])
                    except (ValueError, SyntaxError):
                        pass
                
                # 处理 core_keywords 字段
                if "core_keywords" in entity and isinstance(entity["core_keywords"], str):
                    try:
                        import ast
                        entity["core_keywords"] = ast.literal_eval(entity["core_keywords"])
                    except (ValueError, SyntaxError):
                        pass
                
                # 处理 sample_utterances 字段
                if "sample_utterances" in entity and isinstance(entity["sample_utterances"], str):
                    try:
                        import ast
                        entity["sample_utterances"] = ast.literal_eval(entity["sample_utterances"])
                    except (ValueError, SyntaxError):
                        pass
            
            # 确保所有字段都是JSON可序列化的
            serializable_entity = self.utils._make_serializable(entity)
            result.append(serializable_entity)
            # 提取距离
            dist = r.get("distance")
            distance.append(
                {
                    "意图ID": serializable_entity.get("intent_id"),
                    "与原始文本的相似度": float(dist) if dist is not None else None
                }
            )
        
        logger.info("完成处理搜索结果")
        print(result)
        print(distance)

        filter_result = result[0]
        print("filter_result", filter_result)
        return {
            "相关意图": result,
            "与原始文本的相似度": distance,
            "筛选的最终意图": filter_result
        }

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
            
            # 处理数组字符串字段
            if entity:
                # 处理 tags 字段
                if "tags" in entity and isinstance(entity["tags"], str):
                    try:
                        import ast
                        entity["tags"] = ast.literal_eval(entity["tags"])
                    except (ValueError, SyntaxError):
                        pass
                
                # 处理 core_keywords 字段
                if "core_keywords" in entity and isinstance(entity["core_keywords"], str):
                    try:
                        import ast
                        entity["core_keywords"] = ast.literal_eval(entity["core_keywords"])
                    except (ValueError, SyntaxError):
                        pass
                
                # 处理 sample_utterances 字段
                if "sample_utterances" in entity and isinstance(entity["sample_utterances"], str):
                    try:
                        import ast
                        entity["sample_utterances"] = ast.literal_eval(entity["sample_utterances"])
                    except (ValueError, SyntaxError):
                        pass
            
            # 确保所有字段都是JSON可序列化的
            serializable_entity = self.utils._make_serializable(entity)
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
    query = "发现裂纹后应如何处理？"
    
    # 直接调用 show_distance_retrieval 方法查看搜索结果
    result = intent_recognition.show_distance_retrieval(query)
    print("result:", result)
    
    # response = agent.invoke({
    #     "messages": [{"role": "user", "content": "发现裂纹后应如何处理？"}]
    # })
    # print(response['messages'][-1])