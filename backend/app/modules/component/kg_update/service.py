"""
知识图谱更新：进程内调用 algorithm/KGUpdate 算法，按函数封装。
使用前需安装 algorithm/KGUpdate 依赖，配置见 algorithm/KGUpdate/config.json。
"""
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_KG_UPDATE_ROOT = _ROOT / "algorithm" / "KGUpdate"
if _KG_UPDATE_ROOT.exists() and str(_KG_UPDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(_KG_UPDATE_ROOT))

_kg_updator_module = None


def _get_kg_updator_module():
    global _kg_updator_module
    if _kg_updator_module is None:
        try:
            from KGUpdator import create_kg_updator
            import utils as kg_utils
            _kg_updator_module = {"create_kg_updator": create_kg_updator, "utils": kg_utils}
        except Exception as e:
            logger.warning("加载知识图谱更新模块失败: %s", e)
            raise
    return _kg_updator_module


def _load_kg_config() -> dict[str, Any]:
    config_path = _KG_UPDATE_ROOT / "config.json"
    if not config_path.exists():
        return {
            "neo4j": {"uri": "bolt://127.0.0.1:7687", "username": "neo4j", "password": "neo4j", "database": "neo4j"},
            "system": {"confidence_threshold": 0.7},
        }
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("读取 KGUpdate 配置失败，使用默认: %s", e)
        return {
            "neo4j": {"uri": "bolt://127.0.0.1:7687", "username": "neo4j", "password": "neo4j", "database": "neo4j"},
            "system": {"confidence_threshold": 0.7},
        }


def _create_updator():
    mod = _get_kg_updator_module()
    config = _load_kg_config()
    neo4j = config.get("neo4j", {})
    system = config.get("system", {})
    return mod["create_kg_updator"](
        neo4j_uri=neo4j.get("uri", "bolt://127.0.0.1:7687"),
        neo4j_username=neo4j.get("username", "neo4j"),
        neo4j_password=neo4j.get("password", "neo4j"),
        database=neo4j.get("database", "neo4j"),
        confidence_threshold=float(system.get("confidence_threshold", 0.7)),
    )


def health() -> dict[str, Any]:
    try:
        updator = _create_updator()
        updator.close()
        return {"status": "ok"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"知识图谱更新模块不可用: {e}。请确认已安装 algorithm/KGUpdate 依赖并配置 algorithm/KGUpdate/config.json。",
        }


def add_triples(triples: list[dict[str, Any]], context: str | None = None) -> dict[str, Any]:
    updator = _create_updator()
    try:
        return updator.add_triples(triples, context=context)
    finally:
        updator.close()


def delete_triples(triples: list[dict[str, Any]]) -> dict[str, Any]:
    updator = _create_updator()
    try:
        return updator.delete_triples(triples)
    finally:
        updator.close()


def get_statistics() -> dict[str, Any]:
    updator = _create_updator()
    try:
        return updator.get_statistics()
    finally:
        updator.close()
