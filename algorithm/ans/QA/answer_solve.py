from datetime import datetime
from typing import Dict, Any
import traceback
import sys
import os

current_dir = os.path.dirname(__file__)
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from algorithm.ans.QA.tools.problem_definition import ProblemDefinitionTool
from algorithm.ans.QA.tools.solution_planning import SolutionPlanningTool
from algorithm.ans.QA.tools.answer_generation import AnswerGenerationTool
from algorithm.ans.QA.neo4j_client import MultiServerWikidataQueryClient
from algorithm.ans.QA.neo4j_visualizer import Neo4jPathVisualizer, EnhancedPathVisualizer
from algorithm.ans.QA.config import config as default_config


class Neo4jQAService:
    """Neo4j知识图谱问答服务类 - 使用工具组件"""

    def __init__(self, config=None):
        if config is None:
            config = default_config
        self.config = config

        self._initialize_clients()
        self._initialize_tools()
        
        self.path_visualizer = Neo4jPathVisualizer(
            uri=self.config.NEO4J_URI,
            user=self.config.NEO4J_USER,
            password=self.config.NEO4J_PASSWORD
        )
        self.enhanced_visualizer = EnhancedPathVisualizer(self.path_visualizer)
        print("工具组件初始化成功")
        print("Neo4j知识图谱问答服务初始化完成")

    def _initialize_clients(self):
        try:
            self.wiki_client = MultiServerWikidataQueryClient(
                neo4j_uri=self.config.NEO4J_URI,
                username=self.config.NEO4J_USER,
                password=self.config.NEO4J_PASSWORD
            )
            print("Neo4j客户端初始化成功")
        except Exception as e:
            print(f"客户端初始化失败: {e}")
            raise

    def _initialize_tools(self):
        try:
            self.problem_definition_tool = ProblemDefinitionTool(self.wiki_client, self.config)
            print("问题定义工具初始化成功")
            
            self.solution_planning_tool = SolutionPlanningTool(self.wiki_client, self.config)
            print("方案规划工具初始化成功")
            
            self.answer_generation_tool = AnswerGenerationTool(self.wiki_client, self.config)
            print("答案生成工具初始化成功")
        except Exception as e:
            print(f"工具初始化失败: {e}")
            raise

    def answer_question(self, question: str) -> str:
        """
        回答问题 - 简洁版（直接返回answer字符串，供下一个组件调用）
        详细推理过程通过日志输出供检查
        """
        print(f"\n{'=' * 50}")
        print(f"问题: {question}")

        try:
            print("\n【组件1】问题定义")
            problem_result = self.problem_definition_tool.define_problem(question)
            
            if not problem_result.get("success"):
                return f"问题定义失败: {problem_result.get('error', '未知错误')}"
            
            problem_model = problem_result.get("problem_model")
            print(f"问题模型: {problem_model.get('问题ID')} - {problem_model.get('问题类型')}")
            
            print("\n【组件2】方案规划")
            solution_result = self.solution_planning_tool.plan_solution(problem_model)
            
            if not solution_result.get("success"):
                return f"方案规划失败: {solution_result.get('error', '未知错误')}"
            
            solution = solution_result.get("solution")
            graph_paths = solution_result.get("graph_paths")
            
            print("\n【组件3】答案生成")
            answer_result = self.answer_generation_tool.generate_answer(
                solution=solution,
                graph_paths=graph_paths,
                question=question
            )
            
            if not answer_result.get("success"):
                return f"答案生成失败: {answer_result.get('error', '未知错误')}"
            
            final_answer = answer_result.get("answer")
            
            return final_answer

        except Exception as e:
            print(f"处理问题失败: {e}")
            traceback.print_exc()
            return f"处理失败: {str(e)}"

    def answer_question_with_details(self, question: str) -> Dict[str, Any]:
        """
        回答问题 - 详细版（返回完整推理过程，供前端展示或调试使用）
        """
        print(f"\n{'=' * 50}")
        print(f"问题: {question}")

        try:
            print("\n【组件1】问题定义")
            problem_result = self.problem_definition_tool.define_problem(question)
            
            if not problem_result.get("success"):
                return {
                    "success": False,
                    "answer": f"问题定义失败: {problem_result.get('error', '未知错误')}",
                    "raw_details": None
                }
            
            problem_model = problem_result.get("problem_model")
            print(f"问题模型: {problem_model.get('问题ID')} - {problem_model.get('问题类型')}")
            
            print("\n【组件2】方案规划")
            solution_result = self.solution_planning_tool.plan_solution(problem_model)
            
            if not solution_result.get("success"):
                return {
                    "success": False,
                    "answer": f"方案规划失败: {solution_result.get('error', '未知错误')}",
                    "raw_details": None
                }
            
            solution = solution_result.get("solution")
            graph_paths = solution_result.get("graph_paths")
            
            print("\n【组件3】答案生成")
            answer_result = self.answer_generation_tool.generate_answer(
                solution=solution,
                graph_paths=graph_paths,
                question=question
            )
            
            if not answer_result.get("success"):
                return {
                    "success": False,
                    "answer": f"答案生成失败: {answer_result.get('error', '未知错误')}",
                    "raw_details": None
                }
            
            final_answer = answer_result.get("answer")
            
            return {
                "success": True,
                "answer": final_answer,
                "raw_details": {
                    "问题模型": problem_model,
                    "方案模型": [solution],
                    "图谱路径": graph_paths,
                    "推理日志": solution_result.get("reasoning_logs", [])
                }
            }

        except Exception as e:
            print(f"处理问题失败: {e}")
            traceback.print_exc()
            return {
                "success": False,
                "answer": f"处理失败: {str(e)}",
                "raw_details": None
            }

    def answer_question_with_visualization(self, question: str, visualize: bool = True, visualization_type="pyvis") -> Dict:
        print(f"\n{'=' * 50}")
        print(f"问题: {question}")

        try:
            result = self.answer_question_with_details(question)
            
            if not result.get("success"):
                return result
            
            visualization_html = None
            if visualize and result.get("answer") and result.get("answer") != "信息不足":
                try:
                    raw_details = result.get("raw_details", {})
                    answer_entities = self.enhanced_visualizer.extract_answer_entities_from_result(raw_details)
                    print(f"提取的答案实体: {answer_entities}")

                    if answer_entities:
                        visualization_html = self.enhanced_visualizer.highlight_answer_path(
                            question, answer_entities, visualization_type=visualization_type
                        )
                        print(f"可视化结果: {visualization_html}")
                    else:
                        print("未提取到答案实体")
                        visualization_html = "未提取到答案实体"
                except Exception as e:
                    print(f"生成可视化失败: {e}")
                    visualization_html = f"生成可视化失败: {str(e)}"

            return {
                "success": True,
                "question": question,
                "answer": result.get("answer"),
                "visualization": visualization_html,
                "raw_details": result.get("raw_details"),
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            print(f"处理问题失败: {e}")
            traceback.print_exc()
            return {
                "success": False,
                "question": question,
                "answer": f"处理失败: {str(e)}",
                "visualization": None,
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }
    
    def get_solver_detail(self, question: str) -> Dict[str, Any]:
        try:
            result = self.answer_question_with_details(question)
            
            if result.get("success"):
                return {
                    "success": True,
                    "question": question,
                    "raw_details": result.get("raw_details"),
                    "timestamp": datetime.now().isoformat()
                }
            else:
                return result
                
        except Exception as e:
            return {
                "success": False,
                "question": question,
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def close(self):
        if hasattr(self, 'wiki_client') and self.wiki_client:
            self.wiki_client.client.close()
            print("已关闭Neo4j连接")
