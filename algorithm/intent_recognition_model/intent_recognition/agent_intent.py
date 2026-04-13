from pydantic import BaseModel, Field
from ir_config.config import Config
from ir_utils.search_intent import HybridSearch
from langchain_openai import ChatOpenAI
from ir_utils.handle_data import utils
from langchain_core.tools import tool
from langchain.agents import create_agent
from langchain_core.prompts import ChatPromptTemplate
from data.intent_model import Intent_Base_Recognition
from ir_utils.database_utils import DatabaseUtils
import json

#初始化工具类
utils = utils()

# 加载配置
cfg = Config()
api_key = cfg.api_key or "qwen"


def _normalize_openai_base_url(base_url: str) -> str:
    base_url = (base_url or "").rstrip("/")
    if not base_url:
        return "http://localhost:9999/v1"
    if base_url.endswith("/v1"):
        return base_url
    return f"{base_url}/v1"

def retrival_tool(problem_model: str):
    """从意图向量数据库中检索相关意图"""
    results = HybridSearch().basic_search(query=problem_model)
     
    dense_results = results.get("密集向量检索结果", [])
    sparse_results = results.get("稀疏向量检索结果", [])
    
    dense_processed = utils.handle_RepeatedScalarContainer(dense_results)
    sparse_processed = utils.handle_RepeatedScalarContainer(sparse_results)
    
    return {
        "密集向量检索结果": dense_processed,
        "稀疏向量检索结果": sparse_processed,
    }

system_prompt = """
你是一个意图识别专家，返回与用户输入的内容最合适的意图

**重要提示**：
1. 从检索结果中选择一个最合适的意图，如果你觉得都不匹配用户的查询，就返回"没有匹配的意图"，Intent_Base_Recognition字段全部为空。
2. 如果你觉得有适合的意图，就输出，不过一定要确保输出的 JSON 必须与 Intent_Base_Recognition 模型的字段名完全一致
3. 如果用户查询与所有意图都不匹配，就返回"没有匹配的意图"，Intent_Base_Recognition字段全部为空。
4. 不要包含任何额外的文本或解释，只输出 JSON 格式的意图信息
"""


model = ChatOpenAI(
    base_url=_normalize_openai_base_url(cfg.llm_base_url),
    api_key=api_key,
    model=cfg.llm_model,
    temperature=0,
)

model = model.with_structured_output(Intent_Base_Recognition)

def intent_recognition(problem_model: dict):
    """直接调用retrival_tool获取意图列表，然后注入给大模型判断"""

    # 检查输入是否为 JSON 字符串
    try:
        # 如果是 JSON 格式，构建查询文本
        if isinstance(problem_model, dict):
            problem_model = DatabaseUtils().build_query_from_json(problem_model)
    except (json.JSONDecodeError, TypeError):
        # 如果不是 JSON 格式，直接使用原始查询
        pass

    # 直接调用retrival_tool获取意图列表
    intent_results = retrival_tool(problem_model)
    
    # 构建包含意图列表的提示词
    enhanced_system_prompt = system_prompt + f"\n\n检索到的意图列表：\n{str(intent_results)}"

    messages = [
        {"role": "system", "content": enhanced_system_prompt},
        {"role": "user", "content": problem_model}
    ]

    # 调用模型进行意图判断
    response = model.invoke(messages)
    
    # json_content = utils.extract_json_content(content)
    
    # if json_content == "":
    #     json_content = "最终意图识别失败，未识别到相关意图"

    response = response.model_dump()
    return {
        "success": True,
        "data": {
            "understanding": "最终意图识别成功",
            "pass": True,
            "intent": response
        }
    }
        
if __name__ == "__main__":
    # 测试普通文本输入
    # query = "最近 12 个月的采购金额月度走势是怎样的？"
    
    # 测试 JSON 格式输入
    json_input = {
      "problem_model": {
        "问题ID": "QKB-001",
        "问题描述": "公开采购与非公开采购金额构成如何？",
        "问题类型": "描述型问题",
        "约束": ["需基于公开数据源"],
        "目标对象": "采购金额构成",
        "干系人": ["采购部门", "财务部门"],
        "目标": ["明确金额分配比例"],
        "当前状态": "分析阶段",
        "创建时间": "2026-04-09T00:54:30.393365",
        "相关实体": ["公开采购", "非公开采购", "金额构成"]
      }
    }
    
    final_answer = intent_recognition(json_input)
    print(final_answer)
    print(type(final_answer))   # <class 'dict'>
