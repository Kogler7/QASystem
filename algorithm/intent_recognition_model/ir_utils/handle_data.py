import logging
import os
import sys
import datetime
import json
import re


from ir_config.config import Config

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

    def _make_serializable(self, obj):
        """递归转换非JSON可序列化对象"""
        # 基础类型直接返回
        if obj is None or isinstance(obj, (int, float, bool)):
            return obj
        
        # 字符串类型，处理可能的数组字符串
        if isinstance(obj, str):
            # 检查是否是数组格式的字符串
            if obj.startswith('[') and obj.endswith(']'):
                try:
                    # 尝试将字符串转换为数组
                    import ast
                    result = ast.literal_eval(obj)
                    logger.info(f"将字符串转换为数组: {obj[:50]}... -> {type(result)}")
                    return result
                except (ValueError, SyntaxError) as e:
                    logger.info(f"字符串转换失败: {e}")
                    return obj
            return obj
        
        # 字典类型
        if isinstance(obj, dict):
            logger.info(f"处理字典，键: {list(obj.keys())}")
            return {k: self._make_serializable(v) for k, v in obj.items()}
        
        # 列表和元组
        if isinstance(obj, (list, tuple)):
            return [self._make_serializable(i) for i in obj]
        
        # 日期时间类型
        if isinstance(obj, (datetime.datetime, datetime.date)):
            return obj.isoformat()
        
        # 处理 protobuf 类型
        if hasattr(obj, "__class__") and "google._upb._message" in str(type(obj)):
            # 先检查是否有 __len__ 属性，避免无限递归
            if hasattr(obj, "__len__") and len(obj) > 1000:
                # 对于大型向量，只返回前100个元素和长度信息
                return f"[向量数据，长度: {len(obj)}]"
            
            if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
                try:
                    # 限制迭代长度，避免处理过大的数据
                    items = []
                    count = 0
                    for i in obj:
                        items.append(self._make_serializable(i))
                        count += 1
                        if count >= 100:  # 限制最大处理数量
                            items.append(f"... 共 {len(obj)} 个元素")
                            break
                    return items
                except (TypeError, OverflowError):
                    return str(obj)
            else:
                return str(obj)
        
        # 其他可迭代对象
        if hasattr(obj, "__iter__") and not isinstance(obj, (str, bytes)):
            try:
                return [self._make_serializable(i) for i in obj]
            except TypeError:
                return str(obj)
        
        # 默认返回
        return obj

    def handle_RepeatedScalarContainer(self, results):
        """处理重复标量容器,只在深度意图识别中使用"""
        final_results = []
        for r in results:
            # 直接提取entity字典，避免protobuf对象
            entity = r.get("entity")
            
            # 处理数组字符串字段
            if entity:
                # 处理 tags 字段
                if "tags" in entity and isinstance(entity["tags"], str):
                    try:
                        import ast
                        entity["tags"] = ast.literal_eval(entity["tags"])
                    except (ValueError, SyntaxError):
                        pass
                
                # 处理 core_keywords 字段
                if "core_keywords" in entity and isinstance(entity["core_keywords"], str):
                    try:
                        import ast
                        entity["core_keywords"] = ast.literal_eval(entity["core_keywords"])
                    except (ValueError, SyntaxError):
                        pass
                
                # 处理 sample_utterances 字段
                if "sample_utterances" in entity and isinstance(entity["sample_utterances"], str):
                    try:
                        import ast
                        entity["sample_utterances"] = ast.literal_eval(entity["sample_utterances"])
                    except (ValueError, SyntaxError):
                        pass
            
            # 确保所有字段都是JSON可序列化的
            serializable_entity = self._make_serializable(entity)

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
