import sys
import os
import re
import json
from typing import Dict, Optional
from datetime import datetime

# 路径导入
current_file = os.path.abspath(__file__)
tools_dir = os.path.dirname(current_file)
qa_dir = os.path.dirname(tools_dir)
ans_dir = os.path.dirname(qa_dir)
algorithm_dir = os.path.dirname(ans_dir)
project_root = os.path.dirname(algorithm_dir)
sys.path.insert(0, project_root)

from algorithm.ans.QA.models import ProblemModel
from algorithm.ans.QA.utils import run_llm
from algorithm.ans.QA.prompt_list import problem_definition_prompt
from algorithm.ans.QA.entity_linker import EntityLinker


class ProblemDefinitionTool:
    """
    问题定义工具（自带多轮迭代评估）
    功能：
    1. 生成结构化问题模型
    2. 多轮迭代评估，直到模型信息完整
    3. 信息完整后才输出最终模型
    
    注意：实体识别和链接在answer_generation中进行
    """

    def __init__(self, neo4j_client, args):
        self.client = neo4j_client
        self.args = args
        self.max_iterations = 3  # 最多迭代3轮，避免死循环
        self.entity_linker = EntityLinker(neo4j_client, args)

    def define_problem(self, user_input: str) -> Dict:
        """
        外部调用入口：自动多轮迭代，直到问题模型完整
        """
        current_query = user_input
        iteration = 0
        best_model = None
        best_eval = None

        # 多轮迭代核心逻辑
        while iteration < self.max_iterations:
            iteration += 1
            model_result = self._single_pass_define(current_query)

            if not model_result["success"]:
                break

            problem_model = model_result["problem_model"]
            eval_result = model_result["data"]

            best_model = problem_model
            best_eval = eval_result

            # 评估通过 → 结束
            if eval_result["pass"]:
                break

            # 不通过 → 自动优化下一轮问题
            current_query = f"{user_input}。补充说明：{eval_result['understanding']}"

        # 构建符合响应示例格式的响应
        if best_eval and best_eval["pass"]:
            return {
                "code": 0,
                "info": "",
                "data": {
                    "pass": best_eval["pass"],
                    "understranding": best_eval["understanding"],
                    "problem_model": best_model,
                    "details": {}
                }
            }
        else:
            return {
                "code": 1,
                "info": "问题定义失败",
                "data": {
                    "details": {}
                }
            }

    def _single_pass_define(self, user_input: str) -> Dict:
        """内部单轮：生成模型 → 评估"""

        # 生成问题模型
        prompt = problem_definition_prompt.format(
            user_input=user_input
        )
        response = run_llm(prompt, 0.2, 800, self.args.LLM_API_KEY, self.args.LLM_TYPE)
        data = self._extract_json(response)

        # 进行实体识别
        related_entities = self.entity_linker.recognize_entities(user_input)

        # 默认基础结构
        base_data = {
            "problem_id": f"QKB-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            "description": user_input,
            "problem_type": "描述型问题",
            "constraints": ["需基于公开数据源"],
            "target_object": user_input,
            "stakeholders": ["采购部门", "财务部门"],
            "goals": ["明确金额分配比例"],
            "status": "分析阶段",
            "created_at": datetime.now().isoformat(),
            "related_entities": related_entities,
            "children": []
        }

        try:
            if data:
                # 更新基础数据
                base_data.update(data)

            # 评估必填字段
            required_fields = ["description", "problem_type", "constraints", "target_object", "stakeholders", "goals", "status"]
            missing_fields = [f for f in required_fields if not base_data.get(f)]
            pass_eval = len(missing_fields) == 0

            if pass_eval:
                understanding = "问题描述清晰可进行下一步"
            else:
                understanding = f"问题不完整，请补充：{', '.join(missing_fields)}"

            problem_model = ProblemModel(**base_data)

            return {
                "success": True,
                "problem_model": problem_model.dict(),
                "data": {
                    "pass": pass_eval,
                    "understanding": understanding
                }
            }

        except Exception as e:
            return {
                "success": False,
                "problem_model": base_data,
                "data": {
                    "pass": False,
                    "understanding": f"模型解析失败，请重新描述问题: {str(e)}"
                }
            }

    def _extract_json(self, response: str) -> Optional[Dict]:
            """从响应中提取JSON"""
            try:
                # 匹配JSON代码块
                json_match = re.search(r'\{[\s\S]*\}', response)
                if json_match:
                    json_str = json_match.group(0)
                    return json.loads(json_str)
            except Exception:
                pass
            return None
