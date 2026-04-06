import sys
import os

# 添加项目根目录到Python路径
current_file = os.path.abspath(__file__)
tools_dir = os.path.dirname(current_file)
qa_dir = os.path.dirname(tools_dir)
ans_dir = os.path.dirname(qa_dir)
algorithm_dir = os.path.dirname(ans_dir)
project_root = os.path.dirname(algorithm_dir)
sys.path.insert(0, project_root)

from algorithm.ans.QA.models import ProblemModel
from algorithm.ans.QA.entity_linker import EntityLinker
from algorithm.ans.QA.utils import run_llm
from typing import Dict, Optional
from datetime import datetime


class ProblemDefinitionTool:
    """
    问题定义工具：将用户问题转化为结构化问题模型
    """
    
    def __init__(self, neo4j_client, args):
        self.client = neo4j_client
        self.args = args
        self.entity_linker = EntityLinker(neo4j_client, args)
    
    def define_problem(self, user_input: str) -> Dict:
        """
        定义问题
        输入：用户问题
        输出：结构化问题模型
        """
        # 1. 识别实体
        entity_texts = self.entity_linker.recognize_entities(user_input)
        
        # 2. 生成问题模型
        prompt = f"""
        你是一个问题分析专家。请将以下用户问题转化为结构化的问题模型。

        用户问题："{user_input}"

        从问题中提取的实体：{entity_texts}

        问题模型包含以下字段：
        - 问题ID：格式为 "QKB-三位数字"，如 QKB-001
        - 问题描述：清晰描述问题（可以用原问题）
        - 问题类型：从以下选择 [描述型问题, 诊断型问题, 预测型问题, 指导型问题, 评估型问题, 规划型问题]
        - 约束：解决问题的限制条件（列表，至少1项）
        - 目标对象：问题针对的主要对象
        - 干系人：涉及的角色（列表，至少1项）
        - 目标：要达到的目的（列表，至少1项）
        - 当前状态：当前所处阶段（如"初始阶段"、"分析阶段"等）

        请用JSON格式输出，只输出JSON，不要其他内容。
        """
        
        response = run_llm(prompt, 0.2, 800, self.args.LLM_API_KEY, self.args.LLM_TYPE)
        
        try:
            data = self._extract_json(response)
            if data:
                # 确保所有必要字段都存在
                if "问题ID" not in data:
                    data["问题ID"] = "PROB-001"
                if "相关实体" not in data:
                    data["相关实体"] = entity_texts
                if "创建时间" not in data:
                    data["创建时间"] = datetime.now().isoformat()
                
                problem_model = ProblemModel(**data)
                return {
                    "success": True,
                    "problem_model": problem_model.dict(),
                    "entities": entity_texts
                }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "message": "生成问题模型失败"
            }
        
        return {
            "success": False,
            "error": "无法解析LLM响应",
            "message": "生成问题模型失败"
        }
    
    def _extract_json(self, response: str) -> Optional[Dict]:
        """从响应中提取JSON"""
        try:
            # 匹配JSON代码块
            import re
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                json_str = json_match.group(0)
                import json
                return json.loads(json_str)
        except Exception:
            pass
        return None
