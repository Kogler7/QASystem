from pydantic import BaseModel, Field
from algorithm.intent_recognition_model.ir_config.config import Config
from langchain_openai import ChatOpenAI
from algorithm.intent_recognition_model.ir_utils.handle_data import utils
from langchain_core.tools import tool
from pathlib import Path
import json
from typing import Optional, Dict, List, Any
import os
import re

# 自定义输出解析器，清理 Markdown 包裹
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.exceptions import OutputParserException

class CleanedPydanticOutputParser(PydanticOutputParser):
    """清理 Markdown 包裹的 Pydantic 输出解析器"""
    def parse(self, text: str):
        # 清理 Markdown 代码块
        cleaned = text.strip()
        # 移除 ```json 或 ``` 开头和结尾
        cleaned = re.sub(r'^```json\n|^```\n|\n```$', '', cleaned, flags=re.MULTILINE)
        cleaned = cleaned.strip()
        try:
            return super().parse(cleaned)
        except Exception as e:
            # 如果还是失败，尝试提取中间的 JSON
            try:
                # 查找 JSON 内容
                json_match = re.search(r'\{[\s\S]*\}', cleaned, re.MULTILINE)
                if json_match:
                    return super().parse(json_match.group(0))
            except:
                pass
            raise OutputParserException(f"解析失败: {str(e)}")

#初始化工具类
utils = utils()

# 加载配置
cfg = Config()
api_key = cfg.api_key or "qwen"

def _normalize_openai_base_url(base_url: str) -> str:
    base_url = (base_url or "").rstrip("/")
    version = (os.getenv("LLM_API_VERSION", "v1") or "v1").strip("/")
    if not base_url:
        return f"http://localhost:9999/{version}"
    if base_url.endswith(f"/{version}"):
        return base_url
    return f"{base_url}/{version}"


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


class ParameterDefinition(BaseModel):
    """参数定义模型"""
    type: str = Field(description="参数类型")
    description: str = Field(description="参数描述")

class Parameters(BaseModel):
    """参数模型"""
    type: str = Field("object", description="参数类型")
    properties: Dict[str, ParameterDefinition] = Field(description="参数属性")
    required: List[str] = Field(description="必填参数")
    additionalProperties: bool = Field(False, description="是否允许额外属性")

class ToolInfo(BaseModel):
    """工具信息模型"""
    name: str = Field(description="工具名称")
    description: str = Field(description="工具描述")
    parameters: Parameters = Field(description="工具参数")
    confidence: float = Field(description="置信度")
    similar_options: List[Dict] = Field(default_factory=list, description="相似选项")

class ToolList(BaseModel):
    """工具列表模型"""
    tools: list[ToolInfo] = Field(description="选择的工具信息列表")

def evaluation_tool_list(tools_list: list) -> dict:
    """评估工具列表"""
    # 过滤出置信度高于0.9的工具
    filtered_tools = [tool for tool in tools_list if tool.get("confidence", 0) >= 0.9]
    l = len(filtered_tools)
    
    # 检查是否有工具
    if l == 0:
        return {
                "understanding": "工具生成失败,没有找到合适的工具",
                "pass": False,
                "tools": []
            }
    else:
        return {
                "understanding": "工具生成成功",
                "pass": True,
                "tools": filtered_tools
            }


def plan_tool(step: dict):
    """根据用户输入的步骤生成所需要的工具"""
    # 首先获取所有工具列表
    tools_content = get_all_tools()
    
    # 使用自定义解析器
    parser = CleanedPydanticOutputParser(pydantic_object=ToolList)
    
    # 构建提示词，让模型根据方案选择合适的工具和参数
    format_instructions = parser.get_format_instructions()
    
    # 构建提示词
    prompt = f"""
    你是一个工具选择专家，任务是根据用户的方案列表从提供的工具列表中为每一个方案都选择最适合的工具，并为每个工具指定必要的参数描述。  

    工具列表：
    {tools_content}

    用户步骤：{step}

    要求：
    1. 仔细分析用户输入的每个步骤，理解其核心需求和目标
    2. 对于每个步骤，从工具列表中选择最适合的工具
    3. 为每个选择的工具指定必要的参数，参数名称必须与工具列表中的完全一致
    4. 参数描述应基于用户输入步骤中的信息，确保参数值合理且符合工具要求
    5. 为每个工具评估并提供一个 0-1.0 之间的置信度值，表示该工具对当前步骤的匹配程度
    6. 如果有多个功能相似的工具，在 similar_options 字段中列出
    7. 确保返回的工具名称与工具列表中的完全一致
    8. 确保返回的参数结构符合 ToolList 模型的要求

    输出格式要求：
    - 必须返回 ToolList 结构，包含 tools 列表
    - 每个 tool 包含 name、description、parameters、confidence 和 similar_options
    - parameters 必须是 Parameters 结构，包含 type、properties、required 和 additionalProperties
    - properties 是参数名称到 ParameterDefinition 的映射
    - 对于必填参数，必须在 required 列表中指定
    - 只输出纯 JSON，不要包含任何 Markdown 代码块（不要用 ``` 包裹）

    {format_instructions}
    """

    # 调用模型生成工具列表
    try:
        # 先让模型生成响应
        response = model.invoke([{"role": "user", "content": prompt}])
        
        # 用自定义解析器解析
        result = parser.parse(response.content if hasattr(response, 'content') else str(response))
        
        # 将 ToolList 对象转换为正确的字典结构
        matches_list = []
        # 处理 tools 字段（模型定义的字段）
        if hasattr(result, 'tools'):
            for tool_info in result.tools:
                # 构建工具参数字典
                tool_dict = {
                    "name": tool_info.name,
                    "description": getattr(tool_info, 'description', ''),
                    "confidence": getattr(tool_info, 'confidence', 0.5),
                    "similar_options": getattr(tool_info, 'similar_options', [])
                }
                
                # 处理参数结构
                if hasattr(tool_info, 'parameters'):
                    params = tool_info.parameters
                    parameters_dict = {
                        "type": getattr(params, 'type', 'object'),
                        "properties": {},
                        "required": getattr(params, 'required', []),
                        "additionalProperties": getattr(params, 'additionalProperties', False)
                    }
                    
                    # 处理参数属性
                    if hasattr(params, 'properties'):
                        for param_name, param_def in params.properties.items():
                            parameters_dict["properties"][param_name] = {
                                "type": getattr(param_def, 'type', 'string'),
                                "description": getattr(param_def, 'description', ''),
                            }
                    
                    tool_dict["parameters"] = parameters_dict
                else:
                    # 提供默认参数结构
                    tool_dict["parameters"] = {
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False
                    }
                
                matches_list.append(tool_dict)
        
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
                "pass": False,
                "error_details": str(e)
            }
        }
        return error_response

if __name__ == "__main__":
    # 采购管理域的看构成计划方案
    test_plan = {
        "step": 
        {
            "objective": "将结构化意图转换为指标描述的业务逻辑",
            "input": ["结构化意图"],
            "output": ["指标描述的业务逻辑"],
            "constraints": ["基于业务指标体系匹配", "指标定义无歧义"],
        }
    }
    
    plan_result = plan_tool(test_plan)
    print("测试步骤：")
    print(test_plan)
    print("\n生成结果：")
    print(plan_result)
