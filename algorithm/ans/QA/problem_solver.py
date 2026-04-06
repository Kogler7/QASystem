from .models import ProblemModel, SolutionModel, ProblemSolutionMatch
from typing import Dict, List, Optional
from datetime import datetime
from .wiki_func import *
from .utils import *
from .entity_linker import EntityLinker
from .tools.problem_definition import ProblemDefinitionTool
from .tools.solution_planning import SolutionPlanningTool
from .tools.answer_generation import AnswerGenerationTool
import json


class ProblemSolver:

    def __init__(self, neo4j_client, args):
        self.client = neo4j_client
        self.args = args
        
        self.problem_definition_tool = ProblemDefinitionTool(neo4j_client, args)
        self.solution_planning_tool = SolutionPlanningTool(neo4j_client, args)
        self.answer_generation_tool = AnswerGenerationTool(neo4j_client, args)
        
        self.reasoning_logs = []

    def _log(self, step: str, message: str, data: any = None):
        """记录推理日志"""
        log_entry = {
            "step": step,
            "message": message,
            "data": data
        }
        self.reasoning_logs.append(log_entry)

    def solve(self, user_input: str) -> Dict:
        """
        主求解流程 - 直接调用三个组件
        返回完整的求解结果
        """
        self.reasoning_logs = []
        print(f"\n{'=' * 60}")
        print(f"开始问题求解: {user_input}")

        try:
            # Step 1: 问题定义
            print("\n步骤1: 问题定义")
            problem_result = self.problem_definition_tool.define_problem(user_input)
            
            if not problem_result.get("success"):
                return {
                    "success": False,
                    "question": user_input,
                    "answer": f"问题定义失败: {problem_result.get('error', '未知错误')}",
                    "error": problem_result.get('error'),
                    "timestamp": datetime.now().isoformat()
                }
            
            problem_model = problem_result.get("problem_model")
            print(f"问题模型: {problem_model.get('问题ID')} - {problem_model.get('问题类型')}")
            
            self._log("问题定义", "问题模型生成成功", problem_model)

            # Step 2: 方案规划
            print("\n步骤2: 方案规划")
            solution_result = self.solution_planning_tool.plan_solution(problem_model)
            
            if not solution_result.get("success"):
                return {
                    "success": False,
                    "question": user_input,
                    "answer": f"方案规划失败: {solution_result.get('error', '未知错误')}",
                    "error": solution_result.get('error'),
                    "timestamp": datetime.now().isoformat()
                }
            
            solution = solution_result.get("solution")
            graph_paths = solution_result.get("graph_paths")
            answer = solution_result.get("answer")
            
            self._log("方案规划", "方案规划成功", {
                "方案ID": solution.get("方案ID"),
                "图谱路径数量": len(graph_paths.get("answer_path", []))
            })

            # Step 3: 答案生成
            print("\n步骤3: 答案生成")
            answer_result = self.answer_generation_tool.generate_answer(
                solution=solution,
                graph_paths=graph_paths,
                question=user_input
            )
            
            if not answer_result.get("success"):
                return {
                    "success": False,
                    "question": user_input,
                    "answer": f"答案生成失败: {answer_result.get('error', '未知错误')}",
                    "error": answer_result.get('error'),
                    "timestamp": datetime.now().isoformat()
                }
            
            final_answer = answer_result.get("answer")
            
            self._log("答案生成", "答案生成成功", {"答案": final_answer})

            # 构建返回结果
            print(f"\n{'=' * 60}")
            print("求解完成!")
            
            return {
                "success": True,
                "question": user_input,
                "answer": final_answer,
                "solver_result": {
                    "问题模型": problem_model,
                    "方案模型": solution,
                    "图谱路径": graph_paths,
                    "推理日志": self.reasoning_logs
                },
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            print(f"处理问题失败: {e}")
            import traceback
            traceback.print_exc()

            return {
                "success": False,
                "question": user_input,
                "answer": f"处理失败: {str(e)}",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def solve_simple(self, user_input: str) -> str:
        """
        简化版求解流程 - 只返回答案字符串
        """
        result = self.solve(user_input)
        return result.get("answer", "无法回答该问题")
