"""
答案生成组件：进程内调用 algorithm/ans（Neo4j 知识图谱问答）。
使用前需安装 algorithm/ans/QA 依赖并配置 Neo4j、LLM 等。
"""
import logging
import sys
import json
from pathlib import Path
from typing import Any, List, Dict

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_ANS_ROOT = _ROOT / "algorithm" / "ans"
_ANS_QA = _ANS_ROOT / "QA"
# 插入 _ROOT 到 sys.path，这样 Python 就能找到 algorithm 模块
if _ROOT.exists():
    _root_str = str(_ROOT)
    if _root_str not in sys.path:
        sys.path.insert(0, _root_str)
# 插入 _ANS_ROOT 到 sys.path，这样 Python 就能找到 QA 模块
if _ANS_ROOT.exists():
    _ans_root_str = str(_ANS_ROOT)
    if _ans_root_str not in sys.path:
        sys.path.insert(0, _ans_root_str)
# 插入 _ANS_QA 到 sys.path，保证 QA 模块能被正确加载
if _ANS_QA.exists():
    _ans_qa_str = str(_ANS_QA)
    if _ans_qa_str not in sys.path:
        sys.path.insert(0, _ans_qa_str)


def _register_tog_alias():
    try:
        import QA
        sys.modules["ToG"] = QA
    except Exception:
        pass


_qa_service = None
_neo4j_driver = None


def _get_qa_service():
    global _qa_service
    if _qa_service is None:
        try:
            _register_tog_alias()
            from QA.answer_solve import Neo4jQAService
            _qa_service = Neo4jQAService()
        except Exception as e:
            logger.warning("加载答案生成模块失败: %s", e)
            raise
    return _qa_service


def _get_problem_definition_tool():
    """获取问题定义工具"""
    try:
        _register_tog_alias()
        from QA.answer_solve import Neo4jQAService
        qa_service = Neo4jQAService()
        from QA.tools import ProblemDefinitionTool
        return ProblemDefinitionTool(qa_service.wiki_client, qa_service.config)
    except Exception as e:
        logger.warning("加载问题定义工具失败: %s", e)
        raise


def _get_solution_planning_tool():
    """获取方案规划工具"""
    try:
        _register_tog_alias()
        from QA.answer_solve import Neo4jQAService
        qa_service = Neo4jQAService()
        from QA.tools import SolutionPlanningTool
        return SolutionPlanningTool(qa_service.wiki_client, qa_service.config)
    except Exception as e:
        logger.warning("加载方案规划工具失败: %s", e)
        raise


def _get_answer_generation_tool():
    """获取答案生成工具"""
    try:
        _register_tog_alias()
        from QA.answer_solve import Neo4jQAService
        qa_service = Neo4jQAService()
        from QA.tools import AnswerGenerationTool
        return AnswerGenerationTool(qa_service.wiki_client, qa_service.config)
    except Exception as e:
        logger.warning("加载答案生成工具失败: %s", e)
        raise


def _load_neo4j_config() -> Dict[str, Any]:
    try:
        # 导入QA模块的配置
        from QA.config import config as qa_config
        return {
            "uri": qa_config.NEO4J_URI,
            "user": qa_config.NEO4J_USER,
            "password": qa_config.NEO4J_PASSWORD,
        }
    except Exception as e:
        logger.warning("加载Neo4j配置失败: %s", e)
        raise


def _get_neo4j_driver():
    global _neo4j_driver
    if _neo4j_driver is None:
        try:
            from neo4j import GraphDatabase
            config = _load_neo4j_config()
            _neo4j_driver = GraphDatabase.driver(
                config["uri"],
                auth=(config["user"], config["password"])
            )
        except Exception as e:
            logger.warning("加载Neo4j驱动失败: %s", e)
            raise
    return _neo4j_driver


def close_neo4j_driver():
    global _neo4j_driver
    if _neo4j_driver is not None:
        try:
            _neo4j_driver.close()
            _neo4j_driver = None
        except Exception as e:
            logger.warning("关闭Neo4j驱动失败: %s", e)


def health() -> dict[str, Any]:
    try:
        _get_qa_service()
        return {"status": "ok", "service": "answer-generation"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"答案生成模块不可用: {e}。请确认已安装 algorithm/ans/QA 依赖并配置 Neo4j、LLM。",
        }


def ask(question: str, detailed: bool = False) -> dict[str, Any]:
    svc = _get_qa_service()
    
    if detailed:
        result = svc.answer_question_with_details(question)
        return {
            "success": result.get("success", False),
            "answer": result.get("answer", "无答案"),
            "raw_details": result.get("raw_details")
        }
    else:
        answer = svc.answer_question(question)
        return {
            "answer": answer
        }


def define_problem(question: str) -> dict[str, Any]:
    """问题定义工具"""
    tool = _get_problem_definition_tool()
    result = tool.define_problem(question)
    return result


def plan_solution(problem_model: Dict) -> dict[str, Any]:
    """方案规划工具"""
    tool = _get_solution_planning_tool()
    result = tool.plan_solution(problem_model)
    return result


def generate_answer(solution: Dict, graph_paths: Dict, question: str) -> dict[str, Any]:
    """答案生成工具"""
    tool = _get_answer_generation_tool()
    result = tool.generate_answer(solution, graph_paths, question)
    return result
