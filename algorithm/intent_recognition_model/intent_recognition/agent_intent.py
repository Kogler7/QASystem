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

@tool(description="从意图向量数据库中检索相关意图")
def retrival_tool(query: str):
    """从意图向量数据库中检索相关意图"""
    results = HybridSearch().basic_search(query=query)
     
    dense_results = results.get("密集向量检索结果", [])
    sparse_results = results.get("稀疏向量检索结果", [])
    
    dense_processed = utils.handle_RepeatedScalarContainer(dense_results)
    sparse_processed = utils.handle_RepeatedScalarContainer(sparse_results)
    
    return {
        "密集向量检索结果": dense_processed,
        "稀疏向量检索结果": sparse_processed,
    }

system_prompt = """
你是一个意图识别专家，你需要用(retrival_tool)工具从意图向量数据库中检索相关意图。并返回与用户输入的内容最合适的意图

你有工具：
retrival_tool(query: str): 从向量库检索意图

**工具调用格式**：一定要输出以下内容，arguments中的：具体查询替换为用户的查询
<tool_call>{"name": "retrival_tool", "arguments": {"query": "具体查询"}}</tool_call>

**你的推理格式**：一定要输出以下内容，替换为你自己的推理
<think>你的推理</think>

**最终输出格式**：

你需要从检索结果中选择最合适的意图，并按照以下 JSON 格式返回，确保字段名与 Intent_Base_Recognition 模型完全一致：

```json
{
  "intent_id": "意图唯一标识",
  "domain": "意图所属业务域",
  "scenario": "意图所属场景",
  "object": "意图对象",
  "intent_name": "意图名称",
  "user_goal": "用户核心目标",
}
```

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


def run_tool_loop(query: str, max_loop: int = 2):
    """循环调用工具直到不再需要继续"""
    # 检查输入是否为 JSON 字符串
    try:
        json_input = json.loads(query)
        # 如果是 JSON 格式，构建查询文本
        if isinstance(json_input, dict) and "problem_model" in json_input:
            query = DatabaseUtils().build_query_from_json(json_input)
    except (json.JSONDecodeError, TypeError):
        # 如果不是 JSON 格式，直接使用原始查询
        pass

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query}
    ]
    # 定义工具结果列表
    tool_results = []

    # 循环调用工具
    for step in range(max_loop):
        # 调用模型
        response = model.invoke(messages)
        content = response.content
        tool_call = utils.parse_multi_tool_calls(content)

        if tool_call:
             #执行tool
            for tool in tool_call:
                tool_result = retrival_tool.invoke(tool['arguments'])
                tool_results.append(
                    {
                        "name": tool['name'],
                        "arguments": tool['arguments'],
                        "result": tool_result,
                    }
                )

                #添加tool结果到messages
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "tool", "content": str(tool_result), "tool_call_id": "call_1"})
        else:
            # 提取推理过程和JSON内容
            reasoning = utils.extract_reasoning(content)
            json_content = utils.extract_json_content(content)
            
            if reasoning == "":
                reasoning = "模型推理失败"
            
            if json_content == "":
                json_content = "最终意图识别失败，未识别到相关意图"

            return {
                # "tool_result": tool_results,
                # "reasoning": reasoning,
                "json_content": json_content,
            }   #< class 'str'>

    return "达到最大步数"
        
if __name__ == "__main__":
    # 测试普通文本输入
    # query = "最近 12 个月的采购金额月度走势是怎样的？"
    
    # 测试 JSON 格式输入
    json_input = '''
    {
      "success": true,
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
      },
      "link_result": {
        "linked_entities": {
          "NEO_390": "CG_003 公开采购中标金额"
        }
      }
    }
    '''
    
    final_answer = run_tool_loop(json_input)
    print(f"tool_result: {final_answer.get('tool_result')}")
    print(f"reasoning: {final_answer.get('reasoning')}")
    print(f"json_content: {final_answer.get('json_content')}")
    print(type(final_answer.get('json_content')))   # <class 'str'>
    if isinstance(final_answer.get('json_content'), str):
        try:
            print(type(json.loads(final_answer.get('json_content'))))
        except json.JSONDecodeError:
            print("json_content 不是有效的 JSON 字符串")
