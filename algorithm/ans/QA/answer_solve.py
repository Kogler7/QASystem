import sys
import os

# 添加当前目录到Python路径
current_dir = os.path.dirname(__file__)
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from datetime import datetime
from typing import Dict, Any
import traceback
from algorithm.ans.QA.tools.problem_definition import ProblemDefinitionTool
from algorithm.ans.QA.tools.solution_planning import SolutionPlanningTool
from algorithm.ans.QA.tools.answer_generation import AnswerGenerationTool
from algorithm.ans.QA.neo4j_client import MultiServerWikidataQueryClient
from algorithm.ans.QA.neo4j_visualizer import Neo4jPathVisualizer, EnhancedPathVisualizer
import sys
import os

# 添加项目根目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from algorithm.ans.QA.config import config as default_config


class Neo4jQAService:
    """Neo4j知识图谱问答服务类 - 使用工具组件"""

    def __init__(self, config=None):
        """
        初始化服务

        Args:
            config: 配置参数，如果为None则使用默认配置
        """
        if config is None:
            config = default_config
        self.config = config

        # 初始化客户端
        self._initialize_clients()

        # 初始化工具组件
        self._initialize_tools()
        
        # 初始化可视化器
        self.path_visualizer = Neo4jPathVisualizer(
            uri=self.config.NEO4J_URI,
            user=self.config.NEO4J_USER,
            password=self.config.NEO4J_PASSWORD
        )
        self.enhanced_visualizer = EnhancedPathVisualizer(self.path_visualizer)
        print("工具组件初始化成功")
        print("Neo4j知识图谱问答服务初始化完成")

    def _initialize_clients(self):
        """初始化客户端"""
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
        """初始化工具组件"""
        try:
            # 初始化问题定义工具
            self.problem_definition_tool = ProblemDefinitionTool(self.wiki_client, self.config)
            print("问题定义工具初始化成功")
            
            # 初始化方案规划工具
            self.solution_planning_tool = SolutionPlanningTool(self.wiki_client, self.config)
            print("方案规划工具初始化成功")
            
            # 初始化答案生成工具
            self.answer_generation_tool = AnswerGenerationTool(self.wiki_client, self.config)
            print("答案生成工具初始化成功")
            
        except Exception as e:
            print(f"工具初始化失败: {e}")
            raise

    def answer_question(self, question: str) -> Dict[str, Any]:
        """
        回答单个问题 - 使用工具组件

        Args:
            question: 问题字符串

        Returns:
            包含答案和元数据的字典
        """
        print(f"\n{'=' * 50}")
        print(f"问题: {question}")

        try:
            # Step 1: 问题定义
            print("\n步骤1: 问题定义")
            problem_result = self.problem_definition_tool.define_problem(question)
            
            if not problem_result.get("success"):
                return {
                    "success": False,
                    "question": question,
                    "answer": f"问题定义失败: {problem_result.get('error', '未知错误')}",
                    "error": problem_result.get('error'),
                    "timestamp": datetime.now().isoformat()
                }
            
            problem_model = problem_result.get("problem_model")
            print(f"问题模型: {problem_model.get('问题ID')} - {problem_model.get('问题类型')}")
            
            # Step 2: 方案规划
            print("\n步骤2: 方案规划")
            solution_result = self.solution_planning_tool.plan_solution(problem_model)
            
            if not solution_result.get("success"):
                return {
                    "success": False,
                    "question": question,
                    "answer": f"方案规划失败: {solution_result.get('error', '未知错误')}",
                    "error": solution_result.get('error'),
                    "timestamp": datetime.now().isoformat()
                }
            
            solution = solution_result.get("solution")
            graph_paths = solution_result.get("graph_paths")
            answer = solution_result.get("answer")
            
            # Step 3: 答案生成
            print("\n步骤3: 答案生成")
            answer_result = self.answer_generation_tool.generate_answer(
                solution=solution,
                graph_paths=graph_paths,
                question=question
            )
            
            if not answer_result.get("success"):
                return {
                    "success": False,
                    "question": question,
                    "answer": f"答案生成失败: {answer_result.get('error', '未知错误')}",
                    "error": answer_result.get('error'),
                    "timestamp": datetime.now().isoformat()
                }
            
            final_answer = answer_result.get("answer")
            
            # 构建返回结果
            return {
                "success": True,
                "question": question,
                "answer": final_answer,
                "solver_result": {
                    "问题模型": problem_model,
                    "方案模型": [solution],
                    "图谱路径": graph_paths,
                    "推理日志": solution_result.get("reasoning_logs", [])
                },
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            print(f"处理问题失败: {e}")
            traceback.print_exc()

            return {
                "success": False,
                "question": question,
                "answer": f"处理失败: {str(e)}",
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def answer_question_with_visualization(self, question: str, visualize: bool = True, visualization_type="pyvis") -> Dict:
        """
        回答问题并生成可视化
        
        Args:
            question: 问题
            visualize: 是否生成可视化
            visualization_type: 可视化类型，可选值: "pyvis", "networkx", "both"
            
        Returns:
            包含答案和可视化结果的字典
        """
        print(f"\n{'=' * 50}")
        print(f"问题: {question}")

        try:
            # 调用问答服务
            result = self.answer_question(question)
            
            if not result.get("success"):
                return result
            
            # 生成可视化
            visualization_html = None
            if visualize and result.get("answer") and result.get("answer") != "信息不足":
                # 提取答案实体
                try:
                    solver_result = result.get("solver_result", {})
                    answer_entities = self.enhanced_visualizer.extract_answer_entities_from_result(solver_result)
                    print(f"提取的答案实体: {answer_entities}")

                    if answer_entities:
                        # 生成路径可视化
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

            # 构建返回结果
            return {
                "success": True,
                "question": question,
                "answer": result.get("answer"),
                "visualization": visualization_html,  # HTML文件路径或错误信息
                "solver_result": result.get("solver_result"),
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
        """
        获取完整的问题求解详情（包括问题模型、方案模型等）
        """
        try:
            # 调用问答服务获取详细信息
            result = self.answer_question(question)
            
            if result.get("success"):
                return {
                    "success": True,
                    "question": question,
                    "solver_result": result.get("solver_result"),
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
        """关闭连接"""
        if hasattr(self, 'wiki_client') and self.wiki_client:
            self.wiki_client.client.close()
            print("已关闭Neo4j连接")
