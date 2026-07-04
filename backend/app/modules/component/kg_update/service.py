"""
知识图谱更新：进程内调用 algorithm/KGUpdate 算法，按函数封装。
使用前需安装 algorithm/KGUpdate 依赖；Neo4j 等优先读环境变量，其次 algorithm/KGUpdate/config.json。
"""
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_KG_UPDATE_ROOT = _ROOT / "algorithm" / "KGUpdate"
_CONFIDENCE_CALCULATE_ROOT = _ROOT / "algorithm" / "confidence_calculate"

# 将算法目录加入 sys.path
if _KG_UPDATE_ROOT.exists() and str(_KG_UPDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(_KG_UPDATE_ROOT))
if _CONFIDENCE_CALCULATE_ROOT.exists() and str(_CONFIDENCE_CALCULATE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CONFIDENCE_CALCULATE_ROOT))

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


def _create_updator():
    mod = _get_kg_updator_module()
    return mod["create_kg_updator"]()


def health() -> dict[str, Any]:
    try:
        updator = _create_updator()
        updator.close()
        return {"status": "ok"}
    except Exception as e:
        return {
            "status": "unavailable",
            "detail": f"知识图谱更新模块不可用: {e}。请确认已安装 algorithm/KGUpdate 依赖，并配置 NEO4J_* 环境变量或 algorithm/KGUpdate/config.json。",
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


# ========== 置信度计算（新） ==========

def calculate_confidence_from_graph(data: dict) -> dict:
    """
    接收 graph JSON，为每条关系计算置信度并返回。
    实际计算逻辑在 algorithm/confidence_calculate/calculator.py
    """
    from calculator import calculate_graph_confidence
    return calculate_graph_confidence(data)



def add_relations_from_computed(
    high_confidence_relations: list,
    full_relations: list,
    predictions: list,
    confidence_threshold: float = 0.7
) -> dict[str, Any]:
    """
    旧版：从计算结果入库。已不适用，需要重新设计。
    """
    logger.warning("add_relations_from_computed 已弃用，请使用新流程")
    raise NotImplementedError("add_relations_from_computed 已弃用，请使用新流程")