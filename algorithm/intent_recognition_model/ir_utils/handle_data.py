import logging
import os
import sys
import datetime
import json
import re


from ir_config.config import Config
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union

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
    tools: Union[str, list, dict] = Field(..., description="意图技能描述")

class utils:
    def __init__(self):
        pass

    # def _make_serializable(self, obj):
    #     """递归转换非JSON可序列化对象"""
    #     # 基础类型直接返回
    #     if obj is None or isinstance(obj, (int, float, bool)):
    #         return obj
        
    #     # 字符串类型，处理可能的数组字符串
    #     if isinstance(obj, str):
    #         # 检查是否是数组格式的字符串
    #         if obj.startswith('[') and obj.endswith(']'):
    #             try:
    #                 # 尝试将字符串转换为数组
    #                 import ast
    #                 result = ast.literal_eval(obj)
    #                 logger.info(f"将字符串转换为数组: {obj[:50]}... -> {type(result)}")
    #                 return result
    #             except (ValueError, SyntaxError) as e:
    #                 logger.info(f"字符串转换失败: {e}")
    #                 return obj
    #         return obj
        
    #     # 字典类型
    #     if isinstance(obj, dict):
    #         logger.info(f"处理字典，键: {list(obj.keys())}")
    #         return {k: self._make_serializable(v) for k, v in obj.items()}
        
    #     # 列表和元组
    #     if isinstance(obj, (list, tuple)):
    #         return [self._make_serializable(i) for i in obj]
        
    #     # 日期时间类型
    #     if isinstance(obj, (datetime.datetime, datetime.date)):
    #         return obj.isoformat()
        
    #     # 处理 protobuf 类型
    #     if hasattr(obj, "__class__") and "google._upb._message" in str(type(obj)):
    #         # 先检查是否有 __len__ 属性，避免无限递归
    #         if hasattr(obj, "__len__") and len(obj) > 1000:
    #             # 对于大型向量，只返回前100个元素和长度信息
    #             return f"[向量数据，长度: {len(obj)}]"
            
    #         if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
    #             try:
    #                 # 限制迭代长度，避免处理过大的数据
    #                 items = []
    #                 count = 0
    #                 for i in obj:
    #                     items.append(self._make_serializable(i))
    #                     count += 1
    #                     if count >= 100:  # 限制最大处理数量
    #                         items.append(f"... 共 {len(obj)} 个元素")
    #                         break
    #                 return items
    #             except (TypeError, OverflowError):
    #                 return str(obj)
    #         else:
    #             return str(obj)
        
    #     # 其他可迭代对象
    #     if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
    #         try:
    #             return [self._make_serializable(i) for i in obj]
    #         except TypeError:
    #             return str(obj)
        
    #     # 默认返回
    #     return obj

    def handle_RepeatedScalarContainer(self, results):
        """处理重复标量容器,只在深度意图识别中使用"""
        final_results = []
        for r in results:
            # 直接提取entity字典，避免protobuf对象
            entity = r.get("entity")
            
            if entity:
                # 映射字段名，将数据库字段名映射到Intent模型字段名
                # 处理 tools 字段，解析 Unicode 转义字符
                tools = entity.get("tools")
                if tools:
                    try:
                        # 尝试解析 JSON 字符串
                        import json
                        tools = json.loads(tools)
                    except:
                        pass
                
                mapped_data = {
                    "intent_id": entity.get("intent_id"),
                    "version": entity.get("version"),
                    "domain": entity.get("domain"),
                    "create_time": entity.get("create_time"),
                    "update_time": entity.get("update_time"),
                    "status": entity.get("status"),
                    "tags": entity.get("tags"),
                    "core_keywords": entity.get("core_keywords"),
                    "sample_utterances": entity.get("sample_utterances"),
                    "intent_name": entity.get("intent_name_text"),
                    "intent_description": entity.get("intent_description_text"),
                    "intent_summary": entity.get("intent_summary_text"),
                    "user_goal": entity.get("user_goal_text"),
                    "tools": tools
                }
                
                # 使用Intent模型处理数据，利用Pydantic的自动类型转换
                try:
                    intent_obj = Intent(**mapped_data)
                    serializable_entity = intent_obj.model_dump()
                except Exception as e:
                    logger.info(f"使用Intent模型处理数据失败: {e}")
                    # 如果失败，使用原来的处理方式
                    # serializable_entity = self._make_serializable(entity)
            else:
                serializable_entity = None

            final_results.append({
                "intent_id": r.get("intent_id"),
                "similarity": r.get("distance") or r.get("similarity"),
                "entity": serializable_entity
            })
        return final_results

    def parse_multi_tool_calls(self, content: str):
        """解析多个<tool_call>"""
        calls = re.findall(r'<tool_call>\s*(\{.*\})\s*</tool_call>', content, re.DOTALL)
        return [json.loads(call) for call in calls]

    def shold_continue(self, content: str):
        """判断是否需要继续解析"""
        return '</think>' in content

    def extract_reasoning(self, content: str) -> str:
        """提取推理过程，从<think>到</think>之间的内容"""
        pattern = r'<think>\s*(.*?)\s*</think>'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""

    def extract_json_content(self, content: str) -> str:
        """提取JSON内容，从```json到```之间的内容"""
        pattern = r'```json\s*(.*?)\s*```'
        match = re.search(pattern, content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return ""
