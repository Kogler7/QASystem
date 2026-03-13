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
            
            # 使用 _ROOT 构建置信度计算模块路径
            confidence_root = _ROOT / "algorithm" / "confidence_calculate"
            
            # 确保路径存在
            if not confidence_root.exists():
                raise FileNotFoundError(f"置信度计算模块路径不存在: {confidence_root}")
            
            # 将路径添加到 sys.path
            confidence_root_str = str(confidence_root)
            if confidence_root_str not in sys.path:
                sys.path.insert(0, confidence_root_str)
            
            # 改变工作目录到 confidence_calculate
            os.chdir(confidence_root_str)
            
            # 打印调试信息
            logger.info(f"当前工作目录: {os.getcwd()}")
            logger.info(f"sys.path: {sys.path[:5]}")
            
            # 导入预测器
            from predict import ConfidencePredictor
            
            model_path = confidence_root / "final_model.pt"
            config_path = confidence_root / "config.yaml"
            
            _predictor = ConfidencePredictor(str(model_path), str(config_path))
            logger.info("置信度预测器加载成功")
            
        except Exception as e:
            logger.warning("加载置信度预测器失败: %s", e)
            err_str = str(e)
            if ("incomplete metadata" in err_str or "file not fully covered" in err_str or "deserializ" in err_str.lower()
                    or "pytorchstreamreader" in err_str.lower() or "central directory" in err_str.lower() or "zip archive" in err_str.lower()):
                raise RuntimeError(
                    "置信度模型或依赖文件不完整/损坏（反序列化失败）。"
                    " 若为 SBERT：删除 algorithm/confidence_calculate/sbert_model 后执行 python temp_utils/download_sbert.py；"
                    " 若为 entities.pt/relations.pt：执行 python train_transe_embeddings.py；"
                    " 若为 final_model.pt：请重新训练置信度模型。"
                ) from e
            raise
        finally:
            # 无论成功还是失败，都恢复原始工作目录
            os.chdir(original_cwd)
            
    return _predictor

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

def calculate_confidence(triples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    predictor = _get_predictor()
    predictions = predictor.predict_batch(triples)
    for i, pred in enumerate(predictions):
        triples[i]['confidence'] = pred['confidence']
    return triples