from pydantic import BaseModel, Field
from ir_config.config import Config
from ir_utils.search_intent import HybridSearch
from langchain_openai import ChatOpenAI
from ir_utils.handle_data import utils
from langchain_core.tools import tool
from langchain.agents import create_agent
from langchain_core.prompts import ChatPromptTemplate
try:
    from algorithm.intent_recognition_model.data.intent_model import Intent_Base_Recognition
except ModuleNotFoundError:
    from data.intent_model import Intent_Base_Recognition
from ir_utils.database_utils import build_intent_query_from_dict
import json
import os
import re
from pydantic import ValidationError
from dotenv import load_dotenv
load_dotenv('backend/.env') 
#初始化工具类
utils = utils()

# 加载配置
cfg = Config()

# 从环境变量读取 API Key，优先读取 LLM_API_KEY，其次是 OPENAI_API_KEY
api_key = os.getenv("LLM_API_KEY") or ""



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
    base_url=cfg.llm_base_url,
    api_key=api_key,
    model=cfg.llm_model,
    temperature=0,
)

structured_model = model.with_structured_output(Intent_Base_Recognition)


def _empty_intent() -> dict:
    return {
        "intent_id": "",
        "domain": "",
        "scenario": "",
        "object": "",
        "intent_name": "",
        "user_goal": "",
    }


def _parse_json_from_text(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        pass

    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _normalize_intent_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return _empty_intent()
    # 兼容模型返回 {"Intent_Base_Recognition": {...}}
    if "Intent_Base_Recognition" in payload and isinstance(payload["Intent_Base_Recognition"], dict):
        payload = payload["Intent_Base_Recognition"]

    norm = _empty_intent()
    for k in norm.keys():
        v = payload.get(k, "")
        norm[k] = "" if v is None else str(v)
    return norm

def intent_recognition(problem_model: dict|str):
    """直接调用retrival_tool获取意图列表，然后注入给大模型判断"""

    # 检查输入是否为 JSON 字符串
    try:
        # 如果是 JSON 格式，构建查询文本
        if isinstance(problem_model, dict):
            problem_model = build_intent_query_from_dict(problem_model)
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

    # 调用模型进行意图判断（优先结构化；失败时回退文本解析，避免 500）
    response: dict
    try:
        structured_resp = structured_model.invoke(messages)
        response = structured_resp.model_dump()
    except ValidationError:
        raw_resp = model.invoke(messages)
        content = getattr(raw_resp, "content", "")
        parsed = _parse_json_from_text(content if isinstance(content, str) else str(content))
        response = _normalize_intent_payload(parsed)
    except Exception:
        raw_resp = model.invoke(messages)
        content = getattr(raw_resp, "content", "")
        parsed = _parse_json_from_text(content if isinstance(content, str) else str(content))
        response = _normalize_intent_payload(parsed)

    result = {
            "understanding": "最终意图识别成功",
            "pass": any(response.values()),
            "intent": response,
            "detail": {}
        }

    return result
        
if __name__ == "__main__":
    # 测试普通文本输入
    # query = "最近 12 个月的采购金额月度走势是怎样的？"
    
    # 测试 JSON 格式输入
    json_input = {
      "problem_model": {
        "problem_id": "QKB-001",
        "description": "公开采购与非公开采购金额构成如何？",
        "problem_type": "描述型问题",
        "constraints": ["需基于公开数据源"],
        "target_object": "采购金额构成",
        "stakeholders": ["采购部门", "财务部门"],
        "goals": ["明确金额分配比例"],
        "status": "分析阶段",
        "created_at": "2026-04-09T00:54:30.393365",
        "related_entities": ["公开采购", "非公开采购", "金额构成"],
        "children": []
      }
    }

    query = "公开采购与非公开采购金额构成如何？"
    
    final_answer = intent_recognition(query)
    print(final_answer)
    print(type(final_answer))   # <class 'dict'>
