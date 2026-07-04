"""
意图识别组件：进程内调用 algorithm/intent_recognition_model 算法。
环境变量与后端其它模块一致，在 backend/.env（或 Docker 根目录 .env）中配置即可；
见 backend/.env.example 中「意图识别 / Milvus」相关项。单独跑 algorithm 下脚本时可用该目录 .env。
"""
import logging
import os
import sys
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_INTENT_ROOT = _ROOT / "algorithm" / "intent_recognition_model"


if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

if _INTENT_ROOT.exists():
    if str(_INTENT_ROOT) not in sys.path:
        sys.path.insert(0, str(_INTENT_ROOT))


def health() -> dict[str, Any]:
    try:
        from ir_config.config import Config

        cfg = Config()
        return {
            "status": "ok",
            "service": "intent-recognition",
            "config": {
                "milvus_uri": cfg.milvus_uri,
                "collection_name": cfg.collection_name,
                "model_path": cfg.model_path,
                "model_path_exists": Path(cfg.model_path).is_dir(),
                "llm_base_url": cfg.llm_base_url,
                "llm_model": cfg.llm_model,
                "llm_api_key_configured": bool(cfg.api_key and cfg.api_key != "qwen"),
                "intent_model_base_dir": os.getenv("INTENT_MODEL_BASE_DIR", ""),
            },
        }
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"意图识别模块不可用: {e}。请确认已安装 algorithm/intent_recognition_model 依赖，并在 backend/.env 中配置 Milvus / 模型路径 / LLM 等。",
        }


def _intent_query_text(problem_model: dict | str) -> str:
    """将前端传入的 problem_model（str 或 dict）转为检索用查询文本。"""
    if isinstance(problem_model, str):
        return problem_model.strip()
    if not problem_model:
        return ""
    try:
        from ir_utils.database_utils import build_intent_query_from_dict

        return build_intent_query_from_dict(problem_model)
    except Exception:
        import json

        return json.dumps(problem_model, ensure_ascii=False)


def recognize(problem_model: dict | str) -> dict[str, Any]:
    query = _intent_query_text(problem_model)
    if not query:
        return {"data": {"error": "空查询"}}
    return deep_recognize(problem_model)


def deep_recognize(problem_model: dict|str) -> dict[str, Any]:
    from models.agent_intent import intent_recognition
    result = intent_recognition(problem_model)
    return {
        "code":0,
        "info":"",
        "data": result
    }


def get_plan(problem_model: dict) -> dict[str, Any]:
    from models.plan_app import PlanApp
    plan_app = PlanApp()
    result = plan_app.get_plan_from_model(problem_model)
    return {
        "code":0,
        "info":"",
        "data": result
    }


def get_tool_match(step: dict) -> dict[str, Any]:
    from models.plan_to_tools import plan_tool
    result = plan_tool(step)
    return {
        "code":0,
        "info":"",
        "data": result
    }
