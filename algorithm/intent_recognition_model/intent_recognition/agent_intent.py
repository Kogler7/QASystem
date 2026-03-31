from pydantic import BaseModel, Field
from ir_config.config import Config
from ir_utils.search_intent import HybridSearch
from langchain_openai import ChatOpenAI
from ir_utils.handle_data import utils
from langchain_core.tools import tool
from langchain.agents import create_agent
from langchain_core.prompts import ChatPromptTemplate
from data.intent_model import Intent
import json

#初始化工具类
utils = utils()

# 加载api_key
api_key = "qwen"

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

你需要从检索结果中选择最合适的意图，并按照以下 JSON 格式返回，确保字段名与 Intent 模型完全一致：

```json
{
  "intent_id": "意图唯一标识",
  "version": "意图版本号",
  "domain": "意图所属业务域",
  "create_time": "意图创建时间（ISO8601格式）",
  "update_time": "意图更新时间",
  "status": "意图状态（active/archive/draft）",
  "tags": ["业务标签1", "业务标签2"],
  "intent_name": "意图名称",
  "intent_description": "意图详细描述",
  "sample_utterances": ["典型用户表述1", "典型用户表述2"],
  "core_keywords": ["核心关键词1", "核心关键词2"],
  "intent_summary": "意图精简摘要",
  "user_goal": "用户核心目标",
  "tools": "意图所需要的工具的列表"
}
```

**重要提示**：
1. 从检索结果中选择一个最合适的意图，如果你觉得都不匹配用户的查询，就返回"没有匹配的意图"
2. 如果你觉得有适合的意图，就输出，不过一定要确保输出的 JSON 必须与 Intent 模型的字段名完全一致
3. 如果用户查询与所有意图都不匹配，就返回"没有匹配的意图"
3. 不要包含任何额外的文本或解释，只输出 JSON 格式的意图信息
"""


model = ChatOpenAI(
    base_url="http://localhost:8888/v1",
    api_key=api_key,
    model="Qwen/Qwen3-8B",
    temperature=0,
)

def run_tool_loop(query: str,max_loop: int = 2):
    """循环调用工具直到不再需要继续"""
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
                "tool_result": tool_results,
                "reasoning": reasoning,
                "json_content": json_content,
            }   #< class 'str'>

    return "达到最大步数"
        
if __name__ == "__main__":
    query = "公开采购与非公开采购金额构成如何？"
    final_answer = run_tool_loop(query)
    print(f"tool_result: {final_answer.get('tool_result')}")
    print(f"reasoning: {final_answer.get('reasoning')}")
    print(f"json_content: {final_answer.get('json_content')}")
    print(type(final_answer.get('json_content')))   # <class 'str'>
    print(type(json.loads(final_answer.get('json_content'))))
