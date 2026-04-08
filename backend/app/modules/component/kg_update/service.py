"""
知识图谱更新：进程内调用 algorithm/KGUpdate 算法，按函数封装。
使用前需安装 algorithm/KGUpdate 依赖；Neo4j 等优先读环境变量，其次 algorithm/KGUpdate/config.json。
"""
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_THIS = Path(__file__).resolve()
_ROOT = _THIS.parents[5]
_KG_UPDATE_ROOT = _ROOT / "algorithm" / "KGUpdate"
_CONFIDENCE_CALCULATE_ROOT = _ROOT / "algorithm" / "confidence_calculate"

if _KG_UPDATE_ROOT.exists() and str(_KG_UPDATE_ROOT) not in sys.path:
    sys.path.insert(0, str(_KG_UPDATE_ROOT))
if _CONFIDENCE_CALCULATE_ROOT.exists() and str(_CONFIDENCE_CALCULATE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CONFIDENCE_CALCULATE_ROOT))

_kg_updator_module = None
_predictor = None


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


def _get_predictor():
    global _predictor
    if _predictor is None:
        import os
        original_cwd = os.getcwd()
        try:
            import sys
            from pathlib import Path
            
            confidence_root = _ROOT / "algorithm" / "confidence_calculate"
            
            if not confidence_root.exists():
                raise FileNotFoundError(f"置信度计算模块路径不存在: {confidence_root}")
            
            confidence_root_str = str(confidence_root)
            if confidence_root_str not in sys.path:
                sys.path.insert(0, confidence_root_str)
            
            os.chdir(confidence_root_str)
            
            logger.info(f"当前工作目录: {os.getcwd()}")
            logger.info(f"sys.path: {sys.path[:5]}")
            
            from predict import ConfidencePredictor
            
            model_path = confidence_root / "final_model.pt"
            config_path = confidence_root / "config.yaml"
            
            _predictor = ConfidencePredictor(str(model_path), str(config_path))
            logger.info("置信度预测器加载成功")
            
        except Exception as e:
            logger.warning("加载置信度预测器失败: %s", e)
            raise
        finally:
            os.chdir(original_cwd)
            
    return _predictor


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


def calculate_confidence(triples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    predictor = _get_predictor()
    predictions = predictor.predict_batch(triples)
    return predictions


def process_schema_output(data: dict, confidence_threshold: float = 0.7) -> dict[str, Any]:
    """
    处理 schema_mapper 输出的 JSON，只计算置信度，不入库
    
    Args:
        data: schema_mapper 生成的 JSON（包含 raw 和 graph）
        confidence_threshold: 置信度阈值，用于判断高低置信度（仅用于标记，不用于自动入库）
    
    Returns:
        包含计算结果的字典，不含入库操作
    """
    logger.info("开始处理 schema_mapper 输出（仅计算，不入库）")
    
    predictor = _get_predictor()
    return predictor.extract_from_schema_output(data, confidence_threshold)


def add_relations_from_computed(
    high_confidence_relations: list,
    full_relations: list,
    predictions: list,
    confidence_threshold: float = 0.7
) -> dict[str, Any]:
    """
    从计算结果中入库高置信度的关系（保留所有属性）
    
    Args:
        high_confidence_relations: 高置信度的三元组列表
        full_relations: 完整的关系信息（包含节点属性）
        predictions: 预测结果（包含置信度和组件分数）
        confidence_threshold: 置信度阈值
    """
    logger.info(f"开始从计算结果入库，共 {len(high_confidence_relations)} 个关系")
    
    # 构建完整的关系信息用于入库
    relations_to_add = []
    
    # 建立索引映射
    relation_map = {}
    for i, rel in enumerate(full_relations):
        key = f"{rel['subject']}|{rel['predicate']}|{rel['object']}"
        relation_map[key] = {
            'info': rel,
            'confidence': predictions[i]['confidence'] if i < len(predictions) else 0.7
        }
    
    for rel in high_confidence_relations:
        key = f"{rel['subject']}|{rel['predicate']}|{rel['object']}"
        if key in relation_map:
            info = relation_map[key]['info']
            relations_to_add.append({
                'subject': rel['subject'],
                'predicate': rel['predicate'],
                'object': rel['object'],
                'confidence': relation_map[key]['confidence'],
                'source_node': info.get('source_node', {}),
                'target_node': info.get('target_node', {})
            })
    
    if not relations_to_add:
        return {
            "success": True,
            "message": "没有需要入库的关系",
            "statistics": {"total": 0, "added": 0, "failed": 0},
            "details": []
        }
    
    updator = _create_updator()
    try:
        result = updator.add_relationships_with_full_properties(relations_to_add)
        return result
    finally:
        updator.close()