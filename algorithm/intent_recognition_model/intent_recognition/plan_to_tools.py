from pydantic import BaseModel, Field
from ir_config.config import Config
from langchain_openai import ChatOpenAI
from ir_utils.handle_data import utils
from langchain_core.tools import tool
from pathlib import Path
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


model = ChatOpenAI(
    base_url=_normalize_openai_base_url(cfg.llm_base_url),
    api_key=api_key,
    model=cfg.llm_model,
    temperature=0,
)

system_prompt = """
你是一个计划工具,你需要先调用获取所有工具的tool,再根据用户输入的计划生成所需要的工具。
注：
1,你只能调用获取所有工具的tool,不能调用其他tool。
2,你只能根据用户输入的计划生成所需要的工具,不能生成其他内容。
3,你只能生成所需要的工具,不能生成其他内容。
"""


def get_all_tools():
    """获取所有可用工具列表，从 tools.md 文件中读取"""
    tools_file_path = Path(__file__).resolve().parent.parent / "data" / "tools.md"
    try:
        with open(tools_file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"读取工具文件失败: {str(e)}"


class ToolParameter(BaseModel):
    """工具参数模型"""
    name: str = Field(description="参数名称")
    value: str = Field(description="参数值")


class ToolInfo(BaseModel):
    """工具信息模型"""
    name: str = Field(description="工具名称")
    parameters: list[ToolParameter] = Field(description="工具参数列表")


class ToolList(BaseModel):
    """工具列表模型"""
    tools: list[ToolInfo] = Field(description="选择的工具信息列表")

def plan_tool(plan: str):
    """根据用户输入的计划生成所需要的工具"""
    # 首先获取所有工具列表
    tools_content = get_all_tools()
    
    # 使用 with_structured_output 来约束模型输出
    structured_model = model.with_structured_output(ToolList)
    
    # 构建提示词，让模型根据计划选择合适的工具和参数
    prompt = f"""
    你是一个工具选择专家，任务是根据用户的计划从提供的工具列表中选择最适合的工具，并为每个工具指定必要的参数值。
    
    工具列表：
    {tools_content}
    
    用户计划：{plan}
    
    要求：
    1. 仔细分析用户计划，理解其核心需求
    2. 从工具列表中选择最适合完成该计划的工具
    3. 为每个选择的工具指定必要的参数值
    4. 参数值应基于用户计划中的信息，如果信息不足，可以使用合理的默认值
    5. 确保返回的工具名称与工具列表中的完全一致
    6. 确保返回的参数名称与工具列表中的完全一致
    
    请以结构化格式返回工具信息及其参数。
    """
    
    # 调用模型生成工具列表
    try:
        result = structured_model.invoke([{"role": "user", "content": prompt}])
        # 将 ToolInfo 对象转换为字典
        tools_dict = []
        for tool_info in result.tools:
            tool_dict = {
                "name": tool_info.name,
                "parameters": [
                    {"name": param.name, "value": param.value}
                    for param in tool_info.parameters
                ]
            }
            tools_dict.append(tool_dict)
        # 构建成功响应
        final_results = {
            "success": True,
            "data": {
                "understanding": "工具生成成功",
                "pass": True,
                "tools": tools_dict
            }
        }
        return json.dumps(final_results, ensure_ascii=False, indent=2)
    except Exception as e:
        # 构建失败响应
        error_response = {
            "success": False,
            "data": {
                "understanding": f"生成工具列表失败: {str(e)}",
                "pass": False
            }
        }
        return json.dumps(error_response, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    # 采购管理域的看构成计划方案
    test_plan = """
    采购管理域的看构成计划方案：
    1.上下文获取与指标匹配：输入结构化意图，输出用指标描述的业务；
    2.SQL转换：输入指标描述的业务，输出SQL描述的业务；
    3.SQL执行：输入SQL，输出指标计算结果；
    4.图表可视化与答案生成：输入计算结果，输出图表+文字内容描述。
    """
    
    plan_result = plan_tool(test_plan)
    print("测试计划：")
    print(test_plan)
    print("\n生成结果：")
    print(plan_result)
