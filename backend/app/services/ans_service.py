"""
答案生成组件：进程内调用 algorithm/ans（Neo4j 知识图谱问答），按函数封装，不启动独立服务。
算法层使用 ToG（QA 包），需先注册 ToG 别名。使用前需安装 algorithm/ans/QA 依赖并配置 Neo4j、LLM 等。
"""
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BACKEND_APP = Path(__file__).resolve().parent.parent
_ROOT = _BACKEND_APP.parents[1]
_ANS_ROOT = _ROOT / "algorithm" / "ans"
_ANS_QA = _ANS_ROOT / "QA"
if _ANS_ROOT.exists():
    if str(_ANS_ROOT) not in sys.path:
        sys.path.insert(0, str(_ANS_ROOT))
    # QA 包内使用 from utils import run_llm，需使 algorithm/ans/QA 在 path 上以便 utils 解析为 QA/utils.py
    if _ANS_QA.exists() and str(_ANS_QA) not in sys.path:
        sys.path.insert(0, str(_ANS_QA))

# 算法内使用 from ToG.xxx，实际代码在 QA 包下，注册别名使 ToG 指向 QA
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
    """检查答案生成模块是否可加载（依赖 Neo4j、LLM 等由算法默认配置）。"""
    try:
        _get_qa_service()
        return {"status": "ok", "service": "answer-generation"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"答案生成模块不可用: {e}。请确认已安装 algorithm/ans/QA 依赖并配置 Neo4j、LLM。",
        }


def ask(question: str, detailed: bool = False) -> dict[str, Any]:
    """
    基于知识图谱回答问题。detailed=True 时返回完整求解详情。
    由路由层用 asyncio.to_thread 调用。
    """
    svc = _get_qa_service()
    if detailed:
        result = svc.get_solver_detail(question)
        # 与 api_service 一致：从 solver_result 中提取 answer 字段
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
    """回答问题并返回可视化 HTML 片段（若有）。"""
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
