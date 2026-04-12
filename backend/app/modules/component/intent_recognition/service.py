"""
意图识别组件：进程内调用 algorithm/intent_recognition_model 算法。
环境变量与后端其它模块一致，在 backend/.env（或 Docker 根目录 .env）中配置即可；
见 backend/.env.example 中「意图识别 / Milvus」相关项。单独跑 algorithm 下脚本时可用该目录 .env。
"""
import logging
import sys
from pathlib import Path
from typing import Any



logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_INTENT_ROOT = _ROOT / "algorithm" / "intent_recognition_model"


if _INTENT_ROOT.exists():
    if str(_INTENT_ROOT) not in sys.path:
        sys.path.insert(0, str(_INTENT_ROOT))

_IntentRecognition = None
_recognizer_instance = None


def _get_intent_recognition():
    global _IntentRecognition, _recognizer_instance
    if _IntentRecognition is None:
        try:
            if str(_INTENT_ROOT) not in sys.path:
                sys.path.insert(0, str(_INTENT_ROOT))
            from intent_recognition.intent_app import IntentRecognition
            _IntentRecognition = IntentRecognition
        except Exception as e:
            logger.warning("加载意图识别模块失败: %s", e)
            raise
    if _recognizer_instance is None:
        _recognizer_instance = _IntentRecognition()
    return _recognizer_instance


def health() -> dict[str, Any]:
    try:
        _get_intent_recognition()
        return {"status": "ok"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"意图识别模块不可用: {e}。请确认已安装 algorithm/intent_recognition_model 依赖，并在 backend/.env 中配置 Milvus / 模型路径 / LLM 等。",
        }


def recognize(text: str) -> dict[str, Any]:
    recognizer = _get_intent_recognition()
    result = recognizer.generate_result(text)
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    elif not isinstance(result, dict):
        result = {"raw": str(result)}
    return {"data": result}


def deep_recognize(text: str) -> dict[str, Any]:
    from intent_recognition.agent_intent import run_tool_loop
    result = run_tool_loop(text)
    return {"data": result}

def get_plan(query: str) -> dict[str, Any]:
    from intent_recognition.plan_app import PlanApp
    plan_app = PlanApp()
    result = plan_app.get_plan_from_query(query)
    return {"data": result}

def get_tools(plan: str) -> dict[str, Any]:
    from intent_recognition.plan_to_tools import plan_tool
    result = plan_tool(plan)
    return {"data": result}