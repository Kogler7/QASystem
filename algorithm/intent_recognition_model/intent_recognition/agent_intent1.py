from pydantic import BaseModel, Field
from ir_config.config import Config
from ir_utils.search_intent import HybridSearch
from langchain_openai import ChatOpenAI
from ir_utils.handle_data import utils
from langchain_core.tools import tool
from pydantic import BaseModel, Field

#初始化工具类
utils = utils()

# 加载api_key
api_key = "qwen"

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

@tool(description="从意图向量数据库中检索相关意图")
def retrival_tool(query: str):
    """从意图向量数据库中检索相关意图"""
    results = HybridSearch().basic_search(query=query)
    
    dense_results = results.get("密集向量检索结果", [])
    sparse_results = results.get("稀疏向量检索结果", [])
    
    # 修复：正确处理所有结果
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
  "user_goal": "用户核心目标"
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

tools_list = [retrival_tool]

def run_react_loop(query: str,max_loop: int = 2,tools: list = tools_list):
    """循环调用工具直到不再需要继续"""
    # 创建工具名称到工具对象的映射
    tools_by_name = {tool.name: tool for tool in tools}
    
    # 绑定工具到模型
    model_with_tools = model.bind_tools(tools)

    # 初始化messages
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query}
    ]

    # 定义工具结果列表
    tool_results = []

    # 循环调用工具  
    for iteration in range(max_loop):
        print(f"\n--- Iteration {iteration + 1} ---")

        # 调用模型
        response = model_with_tools.invoke(messages)
        messages.append(response)
        print(f"Model Response: {response}")

        if not response.tool_calls:
            print("No more tool calls - Final answer ready")
            return response.content

        for tool_call in response.tool_calls:
            print(f"Tool Call: {tool_call}")
            tool_name = tool_call['name']
            tool_args = tool_call['args']
            print(f"Action: {tool_name}({tool_args})")

            # Execute the tool
            tool_result = tools_by_name[tool_name].invoke(tool_args)
            print(f"Observation: {tool_result}")
            messages.append({"role": "tool", "content": str(tool_result), "tool_call_id": tool_call['id']})

    return "达到最大步数"
        
def main():
    query = "发现裂纹应该如何处理？"
    print(f"Query: {query}")
    
    result = run_react_loop(query)
    print(f"\n🤖 Final Answer: {result}")

if __name__ == "__main__":
    main()
