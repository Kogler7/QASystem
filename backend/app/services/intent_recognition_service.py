"""
意图识别组件：进程内调用 algorithm/intent_recognition_model 算法（按函数封装），不启动独立服务。
使用前需安装 algorithm/intent_recognition_model 依赖，并配置 .env（Milvus、DeepSeek API Key、模型路径等）。
"""
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BACKEND_APP = Path(__file__).resolve().parent.parent
_ROOT = _BACKEND_APP.parents[1]
_INTENT_ROOT = _ROOT / "algorithm" / "intent_recognition_model"

# 优先加载意图识别模块目录下的 .env，保证 MILVUS_URI、DEEPSEEK_API_KEY、MODEL_PATH 等可用
if _INTENT_ROOT.exists():
    _env_file = _INTENT_ROOT / ".env"
    if _env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(dotenv_path=_env_file, override=False)
        except Exception as e:
            logger.debug("加载意图识别 .env 跳过: %s", e)
    if str(_INTENT_ROOT) not in sys.path:
        sys.path.insert(0, str(_INTENT_ROOT))

_IntentRecognition = None
_recognizer_instance = None


def _get_intent_recognition():
    global _IntentRecognition, _recognizer_instance
    if _IntentRecognition is None:
        try:
            from intent_recognition.intent_app import IntentRecognition
            _IntentRecognition = IntentRecognition
        except Exception as e:
            logger.warning("加载意图识别模块失败: %s", e)
            raise
    if _recognizer_instance is None:
        _recognizer_instance = _IntentRecognition()
    return _recognizer_instance


def health() -> dict[str, Any]:
    """检查意图识别模块是否可加载（依赖 Milvus、DeepSeek、BGE 模型等由 .env 配置）。"""
    try:
        _get_intent_recognition()
        return {"status": "ok"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"意图识别模块不可用: {e}。请确认已安装 algorithm/intent_recognition_model 依赖并配置 .env（Milvus、DEEPSEEK_API_KEY、MODEL_PATH 等）。",
        }


def recognize(text: str) -> dict[str, Any]:
    """
    意图识别：根据用户文本返回最匹配的意图（结构化）。
    由路由层用 asyncio.to_thread 调用。
    """
    recognizer = _get_intent_recognition()
    result = recognizer.generate_result(text)
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    elif not isinstance(result, dict):
        result = {"raw": str(result)}
    return {"data": result}
