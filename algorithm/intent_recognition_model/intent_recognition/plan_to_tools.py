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


def get_all_tools():
    """获取所有可用工具列表，从 tools.md 文件中读取"""
    try:
        with open(cfg.tools_file, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        return f"读取工具文件失败: {str(e)}"


class ToolParameter(BaseModel):
    """工具参数模型"""
    name: str = Field(description="参数名称")
    description: str = Field(description="参数描述")


class ToolInfo(BaseModel):
    """工具信息模型"""
    name: str = Field(description="工具名称")
    parameters: list[ToolParameter] = Field(description="工具参数列表")


class ToolMatch(BaseModel):
    """工具列表模型"""
    plan_id: str = Field(description="方案ID，从用户输入的方案里面获取")
    tools: list[ToolInfo] = Field(description="选择的工具信息列表")

class ToolList(BaseModel):
    """工具列表模型"""
    matches: list[ToolMatch] = Field(description="方案到工具的匹配列表")

def evaluation_tool_list(matches_list: list) -> dict:
    """评估工具列表"""
    l = len(matches_list)
    #如果所有的步骤都没找到工具
    count=0
    for match_dict in matches_list:
        if not match_dict["tools"]:
            count+=1
        
    if count==l:
        return {
            "success": False,
            "data": {
                "understanding": "工具生成失败,所有步骤都没有找到合适的工具",
                "pass": False,
                "matches": matches_list
            }
        }
    elif count>0:
        return {
            "success": True,
            "data": {
                "understanding": "工具生成成功,部分步骤没有找到合适的工具",
                "pass": True,
                "matches": matches_list
            }
        }
    else:
        return {
            "success": True,
            "data": {
                "understanding": "工具生成成功,所有步骤都有合适的工具",
                "pass": True,
                "matches": matches_list
            }
        }


def plan_tool(plan: str):
    """根据用户输入的方案生成所需要的工具"""
    # 首先获取所有工具列表
    tools_content = get_all_tools()
    
    # 使用 with_structured_output 来约束模型输出
    structured_model = model.with_structured_output(ToolList)
    
    # 构建提示词，让模型根据方案选择合适的工具和参数
    prompt = f"""
    你是一个工具选择专家，任务是根据用户的方案列表从提供的工具列表中为每一个方案都选择最适合的工具，并为每个工具指定必要的参数描述。  
    
    工具列表：
    {tools_content}
    
    用户方案：{plan}
    
    要求：
    1. 仔细分析用户方案，理解其核心需求
    2. 对于用户传入的每一个方案，选择最适合的工具
    3. 为每个选择的工具指定必要的参数描述
    4. 参数描述应基于用户方案中的信息
    5. 确保返回的工具名称与工具列表中的完全一致
    6. 确保返回的参数名称与工具列表中的完全一致
    7. 确保工具都是从工具列表中选择的，不能是其他工具
    
    请以结构化格式返回工具信息及其参数。
    """
    
    # 调用模型生成工具列表
    try:
        result = structured_model.invoke([{"role": "user", "content": prompt}])
        # 将 ToolList 对象转换为正确的字典结构
        matches_list = []
        # 处理 matches 字段（模型定义的字段）
        if hasattr(result, 'matches'):
            for tool_match in result.matches:
                match_dict = {
                    "plan_id": tool_match.plan_id,
                    "tools": []
                }
                if hasattr(tool_match, 'tools'):
                    for tool_info in tool_match.tools:
                        tool_dict = {
                            "name": tool_info.name,
                            "parameters": [
                                {"name": param.name, "value": param.description}
                                for param in tool_info.parameters
                            ]
                        }
                        match_dict["tools"].append(tool_dict)
                matches_list.append(match_dict)
        # 构建成功响应
        # 调用评估函数处理 ToolList 对象
        final_results = evaluation_tool_list(matches_list)  
        
        return final_results
    except Exception as e:
        # 构建失败响应
        error_response = {
            "success": False,
            "data": {
                "understanding": f"生成工具列表失败: {str(e)}",
                "pass": False
            }
        }
        return error_response

if __name__ == "__main__":
    # 采购管理域的看构成计划方案
    test_plan = {
        "plan": [
                {
                    "方案ID": "SOL-001",
                    "方案类别": "分析类问题解决方案",
                    "方案目标": "将结构化意图转换为指标描述的业务逻辑",
                    "输入": [
                        "结构化意图"
                    ],
                    "输出": [
                        "指标描述的业务逻辑"
                    ],
                    "约束": [
                        "基于业务指标体系匹配",
                        "意图语义准确解析",
                        "指标定义无歧义"
                    ],
                    "控制逻辑": None,
                    "置信度": 0.9
                },
                {
                    "方案ID": "SOL-002",
                    "方案类别": "分析类问题解决方案",
                    "方案目标": "将指标描述的业务逻辑转换为可执行SQL语句",
                    "输入": [
                        "指标描述的业务逻辑"
                    ],
                    "输出": [
                        "SQL描述的业务查询语句"
                    ],
                    "约束": [
                        "SQL语法规范合法",
                        "表字段与指标一一对应",
                        "避免逻辑错误与性能风险"
                    ],
                    "控制逻辑": None,
                    "置信度": 0.9
                },
                {
                    "方案ID": "SOL-003",
                    "方案类别": "分析类问题解决方案",
                    "方案目标": "执行SQL语句并完成指标数值计算",
                    "输入": [
                        "SQL查询语句"
                    ],
                    "输出": [
                        "指标计算结果数据集"
                    ],
                    "约束": [
                        "数据源权限合法",
                        "计算结果准确无误",
                        "执行超时控制"
                    ],
                    "控制逻辑": None,
                    "置信度": 0.9
                },
                {
                    "方案ID": "SOL-004",
                    "方案类别": "分析类问题解决方案",
                    "方案目标": "基于指标计算结果生成可视化图表与自然语言答案",
                    "输入": [
                        "指标计算结果数据集"
                    ],
                    "输出": [
                        "可视化图表",
                        "自然语言文字描述内容"
                    ],
                    "约束": [
                        "图表类型与数据匹配",
                        "描述语言简洁易懂",
                        "结果与计算数据一致"
                    ],
                    "控制逻辑": None,
                    "置信度": 0.9
                }
    ]
}
    
    plan_result = plan_tool(json.dumps(test_plan))
    print("测试计划：")
    print(test_plan)
    print("\n生成结果：")
    print(plan_result)
