import sys
import os

# 添加当前目录到Python路径
current_dir = os.path.dirname(__file__)
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from datetime import datetime
from typing import Dict, Any
import traceback
from problem_solver import ProblemSolver
from neo4j_client import MultiServerWikidataQueryClient
from entity_linker import EntityLinker
from neo4j_visualizer import Neo4jPathVisualizer, EnhancedPathVisualizer


class Neo4jQAService:
    """Neo4j知识图谱问答服务类 - 使用问题求解器"""

    def __init__(self, config=None):
        """
        初始化服务

        Args:
            config: 配置参数，如果为None则使用默认配置
        """
        if config is None:
            config = self.create_default_config()
        self.config = config

        # 初始化客户端
        self._initialize_clients()

        # 初始化问题求解器
        self.problem_solver = ProblemSolver(self.wiki_client, self.config)
        # 初始化可视化器
        self.path_visualizer = Neo4jPathVisualizer(
            uri=config.neo4j_uri,
            user=config.neo4j_user,
            password=config.neo4j_password
        )
        self.enhanced_visualizer = EnhancedPathVisualizer(self.path_visualizer)
        print("问题求解器初始化成功")
        print("Neo4j知识图谱问答服务初始化完成")

    @staticmethod
    def create_default_config():
        """创建默认配置"""

        class Config:
            pass

        config = Config()
        # 设置默认参数
        config.max_length = 256
        config.temperature_exploration = 0.4
        config.temperature_reasoning = 0
        config.width = 3
        config.depth = 3
        config.remove_unnecessary_rel = True
        config.LLM_type = "glm-4.7"
        config.opeani_api_keys = "92ede9374dca443485951a9b7396bb1a.0hoHMDMujZgXrgmk"
        config.num_retain_entity = 5
        config.prune_tools = "llm"
        config.auto_entity_linking = True

        # Neo4j连接参数
        config.neo4j_uri = "bolt://localhost:7687"
        config.neo4j_user = "neo4j"
        config.neo4j_password = "neo4j2025"

        return config

    def _initialize_clients(self):
        """初始化客户端"""
        try:
            self.wiki_client = MultiServerWikidataQueryClient(
                neo4j_uri=self.config.neo4j_uri,
                username=self.config.neo4j_user,
                password=self.config.neo4j_password
            )
            print("Neo4j客户端初始化成功")

            # 初始化实体链接器
            if self.config.auto_entity_linking:
                self.entity_linker = EntityLinker(self.wiki_client, self.config)
                print("实体链接器初始化成功")
            else:
                self.entity_linker = None

        except Exception as e:
            print(f"客户端初始化失败: {e}")
            raise

    def answer_question(self, question: str) -> Dict[str, Any]:
        """
        回答单个问题 - 通过问题求解器

        Args:
            question: 问题字符串

        Returns:
            包含答案和元数据的字典
        """
        print(f"\n{'=' * 50}")
        print(f"问题: {question}")

        try:
            # 调用问题求解器（执行问题定义→问题分解→方案获取）
            solver_result = self.problem_solver.solve(question)

            # 从求解结果中提取答案
            # 答案在方案模型的输出字段中
            answer = self._extract_answer_from_solver_result(solver_result)

            # 构建返回结果
            return {
                "success": True,
                "question": question,
                "answer": answer,
                "solver_result": {
                    "问题模型": solver_result.get("问题模型"),
                    "子问题": solver_result.get("子问题"),
                    "方案模型": solver_result.get("方案模型"),
                    "问题-方案匹配": solver_result.get("问题-方案匹配")
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

    def answer_question_with_visualization(self, question: str, visualize: bool = True) -> Dict:
        """
        回答问题并生成可视化
        """
        print(f"\n{'=' * 50}")
        print(f"问题: {question}")

        try:
            # 调用问题求解器
            solver_result = self.problem_solver.solve(question)

            # 提取答案
            answer = self._extract_answer_from_solver_result(solver_result)

            # 生成可视化
            visualization_html = None
            if visualize and answer and answer != "信息不足":
                # 提取答案实体
                answer_entities = self.enhanced_visualizer.extract_answer_entities_from_result(solver_result)

                if answer_entities:
                    # 生成路径可视化
                    visualization_html = self.enhanced_visualizer.highlight_answer_path(
                        question, answer_entities
                    )

            # 构建返回结果
            return {
                "success": True,
                "question": question,
                "answer": answer,
                "visualization": visualization_html,  # HTML文件路径
                "solver_result": {
                    "问题模型": solver_result.get("问题模型"),
                    "子问题": solver_result.get("子问题"),
                    "方案模型": solver_result.get("方案模型"),
                    "图谱路径": solver_result.get("图谱路径")
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
                "visualization": None,
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            }
    def _extract_answer_from_solver_result(self, solver_result: Dict) -> str:
        """
        从求解结果中提取答案
        答案在方案模型的输出字段中
        """
        try:
            # 获取方案模型
            方案模型列表 = solver_result.get("方案模型", [])
            if 方案模型列表:
                # 取第一个方案
                first_solution = 方案模型列表[0]
                # 答案在输出字段中
                outputs = first_solution.get("输出", [])
                if outputs:
                    return outputs[0]  # 返回第一个输出作为答案

            # 如果没有方案模型，返回默认信息
            return "未找到答案"

        except Exception as e:
            print(f"提取答案失败: {e}")
            return "答案提取失败"


    def get_solver_detail(self, question: str) -> Dict[str, Any]:
        """
        获取完整的问题求解详情（包括问题模型、方案模型等）
        """
        try:
            solver_result = self.problem_solver.solve(question)
            return {
                "success": True,
                "question": question,
                "solver_result": solver_result,
                "timestamp": datetime.now().isoformat()
            }
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