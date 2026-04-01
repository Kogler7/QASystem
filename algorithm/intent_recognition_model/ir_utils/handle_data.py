import logging
import os
import sys
import datetime
import json
import re


from ir_config.config import Config
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union
from data.intent_model import Intent

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

class utils:
    def __init__(self):
        pass

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
