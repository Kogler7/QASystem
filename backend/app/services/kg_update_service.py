"""
知识图谱更新：进程内调用 algorithm/KGUpdate 算法，按函数封装，不依赖独立服务。
使用前需安装 algorithm/KGUpdate 依赖（neo4j 等），配置见 algorithm/KGUpdate/config.json。
"""
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_BACKEND_APP = Path(__file__).resolve().parent.parent  # backend/app
_ROOT = _BACKEND_APP.parents[1]  # qasystem
_KG_UPDATE_ROOT = _ROOT / "algorithm" / "KGUpdate"
if _KG_UPDATE_ROOT.exists() and str(_KG_UPDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(_KG_UPDATE_ROOT))

_kg_updator_module = None


def _get_kg_updator_module():
    global _kg_updator_module
    if _kg_updator_module is None:
        try:
            from KGUpdator import create_kg_updator  # noqa: F401
            import utils as kg_utils  # noqa: F401
            _kg_updator_module = {"create_kg_updator": create_kg_updator, "utils": kg_utils}
        except Exception as e:
            logger.warning("加载知识图谱更新模块失败: %s", e)
            raise
    return _kg_updator_module


def _load_kg_config() -> dict[str, Any]:
    """从 algorithm/KGUpdate/config.json 加载配置。"""
    config_path = _KG_UPDATE_ROOT / "config.json"
    if not config_path.exists():
        return {
            "neo4j": {
                "uri": "bolt://127.0.0.1:7687",
                "username": "neo4j",
                "password": "neo4j",
                "database": "neo4j",
            },
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
    """根据 config.json 创建并返回 KGUpdator 实例。"""
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
    """检查知识图谱更新模块是否可加载（含 Neo4j 连通性可选）。"""
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
    """
    批量添加三元组。每个三元组需包含 subject, predicate, object, confidence。
    由路由层用 asyncio.to_thread 调用。
    """
    updator = _create_updator()
    try:
        return updator.add_triples(triples, context=context)
    finally:
        updator.close()


def delete_triples(triples: list[dict[str, Any]]) -> dict[str, Any]:
    """批量删除三元组。每个三元组需包含 subject, predicate, object。"""
    updator = _create_updator()
    try:
        return updator.delete_triples(triples)
    finally:
        updator.close()


def get_statistics() -> dict[str, Any]:
    """获取知识图谱统计信息。"""
    updator = _create_updator()
    try:
        return updator.get_statistics()
    finally:
        updator.close()
