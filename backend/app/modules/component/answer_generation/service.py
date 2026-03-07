"""
答案生成组件：进程内调用 algorithm/ans（Neo4j 知识图谱问答）。
使用前需安装 algorithm/ans/QA 依赖并配置 Neo4j、LLM 等。
"""
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_ANS_ROOT = _ROOT / "algorithm" / "ans"
_ANS_QA = _ANS_ROOT / "QA"
if _ANS_ROOT.exists():
    if str(_ANS_ROOT) not in sys.path:
        sys.path.insert(0, str(_ANS_ROOT))
    if _ANS_QA.exists() and str(_ANS_QA) not in sys.path:
        sys.path.insert(0, str(_ANS_QA))


def _register_tog_alias():
    try:
        import QA
        sys.modules["ToG"] = QA
    except Exception:
        pass


_qa_service = None


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
        result = svc.get_solver_detail(question)
        answer = "无答案"
        try:
            if result.get("solver_result"):
                方案模型列表 = result["solver_result"].get("方案模型", [])
                if 方案模型列表:
                    outputs = 方案模型列表[0].get("输出", [])
                    if outputs:
                        answer = outputs[0]
        except Exception:
            pass
        return {
            "success": result.get("success", False),
            "question": result.get("question", question),
            "answer": answer,
            "timestamp": result.get("timestamp", ""),
            "details": result.get("solver_result") if result.get("success") else None,
            "error": result.get("error"),
        }
    result = svc.answer_question(question)
    return {
        "success": result.get("success", False),
        "question": result.get("question", question),
        "answer": result.get("answer", "无答案"),
        "timestamp": result.get("timestamp", ""),
        "details": result.get("solver_result") if result.get("success") else None,
        "error": result.get("error"),
    }


def ask_with_visualization(question: str) -> dict[str, Any]:
    svc = _get_qa_service()
    result = svc.answer_question_with_visualization(question, visualize=True)
    return {
        "success": result.get("success", False),
        "question": result.get("question", question),
        "answer": result.get("answer", "无答案"),
        "timestamp": result.get("timestamp", ""),
        "visualization": result.get("visualization"),
        "visualization_type": "html",
        "error": result.get("error"),
    }
