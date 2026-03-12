import datetime
import json
import logging
import os
import sys

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from ir_config.config import Config
from ir_utils.search_intent import HybridSearch
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langchain.tools import tool

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


# 加载api_key
api_key = "qwen"

model = ChatOpenAI(
    base_url="http://localhost:8888/v1",
    api_key=api_key,
    model="Qwen/Qwen3-8B",
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


@tool(description="根据用户的问题进行编码，从意图向量数据库中检索相关意图")
def retrival_tool(query: str):
    """根据用户的问题进行编码，从意图向量数据库中检索相关意图

    Args:
        query: 用户的输入
    """
    results = HybridSearch().basic_search(query=query)
    dense_results = results.get("密集向量检索结果")
    sparse_results = results.get("稀疏向量检索结果")

    logger.info(f"检索到的密集意图数量: {len(dense_results)}")
    logger.info(f"检索到的稀疏意图数量: {len(sparse_results)}")

    dense_results = IntentRecognition().handle_RepeatedScalarContainer(dense_results)
    sparse_results = IntentRecognition().handle_RepeatedScalarContainer(sparse_results)

    return {
        "密集向量检索结果": dense_results,
        "稀疏向量检索结果": sparse_results,
    }

agent = create_agent(
    model=model, 
    tools=[retrival_tool],
    system_prompt="你是一个意图识别的专家，需要你根据用户输入的询问,擅长调用工具来从向量数据库中获取可能的结构化意图，选取最合适的结构化意图，并将它原封不动的输出"
)


class IntentRecognition:
    def __init__(self):
        self.model = model
        self.search_tool = HybridSearch()

    def _make_serializable(self, obj):
        """递归转换非JSON可序列化对象"""
        # 基础类型直接返回
        if obj is None or isinstance(obj, (int, float, bool)):
            return obj
        
        # 字符串类型，处理可能的数组字符串
        if isinstance(obj, str):
            # 检查是否是数组格式的字符串
            if obj.startswith('[') and obj.endswith(']'):
                try:
                    # 尝试将字符串转换为数组
                    import ast
                    result = ast.literal_eval(obj)
                    logger.info(f"将字符串转换为数组: {obj[:50]}... -> {type(result)}")
                    return result
                except (ValueError, SyntaxError) as e:
                    logger.info(f"字符串转换失败: {e}")
                    return obj
            return obj
        
        # 字典类型
        if isinstance(obj, dict):
            logger.info(f"处理字典，键: {list(obj.keys())}")
            return {k: self._make_serializable(v) for k, v in obj.items()}
        
        # 列表和元组
        if isinstance(obj, (list, tuple)):
            return [self._make_serializable(i) for i in obj]
        
        # 日期时间类型
        if isinstance(obj, (datetime.datetime, datetime.date)):
            return obj.isoformat()
        
        # 处理 protobuf 类型
        if hasattr(obj, "__class__") and "google._upb._message" in str(type(obj)):
            # 先检查是否有 __len__ 属性，避免无限递归
            if hasattr(obj, "__len__") and len(obj) > 1000:
                # 对于大型向量，只返回前100个元素和长度信息
                return f"[向量数据，长度: {len(obj)}]"
            
            if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
                try:
                    # 限制迭代长度，避免处理过大的数据
                    items = []
                    count = 0
                    for i in obj:
                        items.append(self._make_serializable(i))
                        count += 1
                        if count >= 100:  # 限制最大处理数量
                            items.append(f"... 共 {len(obj)} 个元素")
                            break
                    return items
                except (TypeError, OverflowError):
                    return str(obj)
            else:
                return str(obj)
        
        # 其他可迭代对象
        if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
            try:
                return [self._make_serializable(i) for i in obj]
            except TypeError:
                return str(obj)
        
        # 默认返回
        return obj

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
            serializable_entity = self._make_serializable(entity)
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
            serializable_entity = self._make_serializable(entity)
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

        filter_result = result[0]
        print("filter_result", filter_result)
        return {
            "相关意图": result,
            "相似度": distance,
            "筛选的最终意图": filter_result
        }

    def generate_result(self, query: str):
        """答案生成函数"""
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

    def handle_RepeatedScalarContainer(self, results):
        final_results = []
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
            serializable_entity = self._make_serializable(entity)

            final_results.append({
                "intent_id": r.get("intent_id"),
                "distance": r.get("distance"),
                "entity": serializable_entity
            })
            return final_results

    def generate_result_chunk(self, query: str):
        """答案生成函数（流式输出）"""
        yield "正在检索相关意图..."
        yield "\n"
        retrival_result = self.retrival(query=query)

        if not retrival_result:
            yield "没有检索到相关意图"
            return

        yield f"找到{len(retrival_result)}个相关意图\n"
        yield f"相关意图列表：{retrival_result}\n"
        # 创建文本模型
        text_model = ChatOpenAI(
            base_url="http://localhost:8888/v1",
            api_key="qwen",
            model="Qwen/Qwen3-8B",
            temperature=0,
        )

        prompt = self.prompt_template.format_messages(
            query=query,
            intent=retrival_result,
        )

        # 流式输出友好文本
        yield "正在分析意图...\n"
        for chunk in text_model.stream(prompt):
            if hasattr(chunk, 'content') and chunk.content:
                yield chunk.content
            elif hasattr(chunk, 'text') and chunk.text:
                yield chunk.text

    def deep_recognition(self, query: str):
        """深度意图识别"""
        messages = {
            "messages": [
                {"role": "system", "content": "你是一个意图识别的专家，需要你根据用户输入的询问,擅长调用工具来从向量数据库中获取可能的结构化意图，选取最合适的结构化意图"},
                {"role": "user", "content": query},
            ]
        }
        full_result = {}
        for chunk in agent.stream({"messages": messages.get("messages")}, stream_mode="values"):
            full_result = chunk
        print(full_result) 
        return full_result
        
if __name__ == "__main__":
    intent_recognition = IntentRecognition()
    query = "发现裂纹后应如何处理？"
    # result = intent_recognition.retrival_tool(query)
    # print("result:", result)
    # print(type(result.get("密集向量检索结果")[0].get("entity").get("tags")))    #<class 'list'>

    result = intent_recognition.deep_recognition(query)